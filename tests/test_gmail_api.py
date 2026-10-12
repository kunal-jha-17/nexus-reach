"""
Sending through the Gmail API instead of SMTP: same message, HTTPS transport, automatic token
refresh, threads kept together, and a clear "redo the sign-in" when the refresh token stops working.
Google is never contacted: the token refresh and the send call are both stood in for.
"""
import base64
import email
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase, FakeCtx  # noqa: E402

import db  # noqa: E402
import gmailapi  # noqa: E402
import outreach  # noqa: E402

GMAIL_ENV = {"GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "csecret",
             "GMAIL_REFRESH_TOKEN": "1//refresh", "SMTP_USER": "jha.kunal.neurospark@gmail.com",
             "SMTP_PASS": "", "SMTP_FROM_NAME": "Kunal from NeuroSpark"}


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data if data is not None else {"id": "m1", "threadId": "t1"}
        self.ok = status < 400
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class GmailBase(Base):
    def setUp(self):
        super().setUp()
        gmailapi._creds.clear()
        self._env2 = mock.patch.dict(os.environ, GMAIL_ENV)
        self._env2.start()
        self.refreshes = []

        def refresh(creds_self, request):                # stands in for the call to oauth2.googleapis.com
            self.refreshes.append(creds_self.refresh_token)
            creds_self.token = f"access-{len(self.refreshes)}"
            import datetime
            creds_self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=1)
        self._ref = mock.patch("google.oauth2.credentials.Credentials.refresh", refresh)
        self._ref.start()
        self.posts = []
        self.replies = []

        def post(url, json=None, headers=None, timeout=None):
            self.posts.append((url, json, headers))
            return self.replies.pop(0) if self.replies else Resp()
        self._post = mock.patch.object(gmailapi.requests, "post", side_effect=post)
        self._post.start()
        db.set_settings({"email_send_delay_seconds": 0, "warmup_enabled": False})

    def tearDown(self):
        for p in (self._post, self._ref, self._env2):
            p.stop()
        gmailapi._creds.clear()
        super().tearDown()

    def lead(self, email_addr="owner@acme.com", name="Acme Plumbing"):
        lead_id = self.add(business_name=name, email=email_addr)
        db.update_lead(lead_id, {"message": "Subject: A site for {business_name}\n\nHi there, quick idea for {business_name}."})
        return lead_id

    def sent_message(self, i=0):
        raw = self.posts[i][1]["raw"]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw))


class TestGmailSending(GmailBase):
    def test_the_same_email_goes_out_over_https_with_a_bearer_token(self):
        lead_id = self.lead()
        with mock.patch.object(outreach.smtplib, "SMTP", side_effect=AssertionError("SMTP must not be used")):
            r = outreach.send_lead_email(lead_id)
        self.assertEqual(r["subject"], "A site for Acme Plumbing")
        url, body, headers = self.posts[0]
        self.assertEqual(url, "https://gmail.googleapis.com/gmail/v1/users/me/messages/send")
        self.assertEqual(headers["Authorization"], "Bearer access-1")
        m = self.sent_message()
        self.assertEqual(m["From"], "Kunal from NeuroSpark <jha.kunal.neurospark@gmail.com>")      # sender name kept
        self.assertEqual((m["To"], m["Subject"]), ("owner@acme.com", "A site for Acme Plumbing"))
        self.assertIn("quick idea for Acme Plumbing", m.get_payload(decode=True).decode())
        self.assertIn("reply STOP", m.get_payload(decode=True).decode())                          # footer kept
        self.assertTrue(m["List-Unsubscribe"] and m["Message-ID"])
        lead = db.get_lead(lead_id)
        self.assertEqual((lead["send_status"], lead["stage"]), ("sent", "contacted"))
        ev = json.loads(next(e for e in db.list_events(lead_id) if e["kind"] == "email_sent")["detail"])
        self.assertEqual(ev["thread_id"], "t1")

    def test_one_access_token_is_reused_until_it_runs_out(self):
        for i in range(3):
            outreach.send_lead_email(self.lead(f"o{i}@biz{i}.com", f"Biz {i}"))
        self.assertEqual(len(self.refreshes), 1)
        creds = next(iter(gmailapi._creds.values()))
        import datetime
        creds.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=1)              # an hour later
        outreach.send_lead_email(self.lead("o9@biz9.com", "Biz 9"))
        self.assertEqual(len(self.refreshes), 2)
        self.assertEqual(self.posts[-1][2]["Authorization"], "Bearer access-2")

    def test_a_token_revoked_early_is_refreshed_and_the_send_retried_once(self):
        self.replies = [Resp(401, {"error": {"message": "Invalid Credentials"}}), Resp()]
        outreach.send_lead_email(self.lead())
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(len(self.refreshes), 2)

    def test_follow_up_goes_into_the_same_gmail_conversation(self):
        lead_id = self.lead()
        outreach.send_lead_email(lead_id)
        db.update_lead(lead_id, {"last_sent_at": "2026-01-01T10:00:00+00:00"})
        db.set_settings({"followup_template": "Any thoughts, {business_name}?"})
        outreach.send_followup(lead_id)
        self.assertEqual(self.posts[1][1]["threadId"], "t1")
        m = self.sent_message(1)
        self.assertEqual(m["Subject"], "Re: A site for Acme Plumbing")
        self.assertEqual(m["In-Reply-To"], db.get_lead(lead_id)["email_message_id"])


class TestGmailFailures(GmailBase):
    def revoke(self):
        from google.auth.exceptions import RefreshError

        def refresh(creds_self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})
        return mock.patch("google.oauth2.credentials.Credentials.refresh", refresh)

    def test_a_dead_refresh_token_says_to_redo_the_sign_in_and_marks_nobody_failed(self):
        ids = [self.lead(f"o{i}@biz{i}.com", f"Biz {i}") for i in range(3)]
        ctx = FakeCtx()
        with self.revoke():
            msg = outreach.run_send_bulk(ctx, {})
        self.assertEqual(msg, "0 sent, 0 failed, 0 skipped")
        self.assertEqual({db.get_lead(i)["send_status"] for i in ids}, {"unsent"})
        log = "\n".join(ctx.lines)
        self.assertIn("tools/gmail_auth.py", log)
        self.assertIn("GMAIL_REFRESH_TOKEN", log)
        self.assertEqual(self.posts, [])

    def test_single_send_and_the_connection_test_explain_it_too(self):
        lead_id = self.lead()
        with self.revoke():
            with self.assertRaises(outreach.SendError) as cm:
                outreach.send_lead_email(lead_id)
            self.assertEqual(cm.exception.code, 503)
            self.assertIn("Redo the one-time Gmail sign-in", str(cm.exception))
            with self.assertRaises(outreach.SendError) as cm:
                outreach.test_smtp()
            self.assertIn("tools/gmail_auth.py", str(cm.exception))
        self.assertEqual(db.get_lead(lead_id)["send_status"], "unsent")

    def test_a_refused_address_fails_only_that_lead(self):
        a, b = self.lead("bad@nowhere.example", "Bad Co"), self.lead("good@biz.com", "Good Co")
        db.update_lead(a, {"notes": "first"})
        self.replies = []
        order = db.select_ids({"ids": [a, b]})
        self.replies = [Resp(400, {"error": {"message": "Invalid To header"}}) if i == a else Resp() for i in order]
        msg = outreach.run_send_bulk(FakeCtx(), {"ids": [a, b]})
        self.assertEqual(msg, "1 sent, 1 failed, 0 skipped")
        self.assertEqual((db.get_lead(a)["send_status"], db.get_lead(b)["send_status"]), ("failed", "sent"))

    def test_api_switched_off_and_quota_messages(self):
        lead_id = self.lead()
        self.replies = [Resp(403, {"error": {"message": "Gmail API has not been used in project 1 before or it is disabled.",
                                             "errors": [{"reason": "accessNotConfigured"}]}})]
        with self.assertRaises(outreach.SendError) as cm:
            outreach.send_lead_email(lead_id)
        self.assertIn("press Enable", str(cm.exception))
        self.replies = [Resp(429, {"error": {"message": "User-rate limit exceeded",
                                             "errors": [{"reason": "userRateLimitExceeded"}]}})]
        with self.assertRaises(outreach.SendError) as cm:
            outreach.send_lead_email(lead_id)
        self.assertIn("sending limit", str(cm.exception))
        self.assertEqual(db.get_lead(lead_id)["send_status"], "unsent")


class TestTransportChoice(Base):
    def test_smtp_is_still_used_when_there_is_no_gmail_sign_in(self):
        lead_id = self.add(business_name="Acme", email="a@acme.com")
        db.update_lead(lead_id, {"message": "Subject: Hi\n\nHello"})
        sent = []

        class S:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, u, p): pass
            def send_message(self, m): sent.append(m)
        with mock.patch.dict(os.environ, {"GMAIL_REFRESH_TOKEN": "", "SMTP_PASS": "app-pass"}), \
                mock.patch.object(outreach.smtplib, "SMTP", S), mock.patch.object(outreach.netguard, "check_mail_host"), \
                mock.patch.object(gmailapi.requests, "post", side_effect=AssertionError("Gmail API must not be used")):
            self.assertEqual(outreach.email_transport(), "smtp")
            outreach.send_lead_email(lead_id)
        self.assertEqual(len(sent), 1)

    def test_a_gmail_sign_in_alone_is_enough_to_send(self):
        with mock.patch.dict(os.environ, {**GMAIL_ENV, "SMTP_PASS": ""}):
            self.assertTrue(outreach.smtp_configured())
            self.assertEqual(outreach.email_transport(), "gmail_api")
        with mock.patch.dict(os.environ, {**GMAIL_ENV, "GOOGLE_CLIENT_SECRET": "", "SMTP_PASS": ""}):
            self.assertFalse(outreach.smtp_configured())                  # a token without the OAuth client is no use


class TestConnectionsField(ApiBase):
    def test_refresh_token_is_a_secret_field_and_never_comes_back_to_the_browser(self):
        r = self.client.put("/api/connections", json={"set": {"GMAIL_REFRESH_TOKEN": "1//my-secret-refresh-token"}})
        self.assertEqual(r.status_code, 200, r.get_json())
        field = next(f for f in r.get_json()["fields"] if f["name"] == "GMAIL_REFRESH_TOKEN")
        self.assertTrue(field["secret"] and field["set"])
        self.assertNotIn("my-secret-refresh-token", json.dumps(r.get_json()))
        st = self.client.get("/api/config-status").get_json()
        self.assertIn("email_via", st)
        self.assertIn("gmail_api_client", st)


class TestSignInScript(unittest.TestCase):
    def test_script_requests_only_the_send_permission_and_prints_the_three_values(self):
        import importlib.util
        import tempfile
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("gmail_auth", os.path.join(here, "tools", "gmail_auth.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.SCOPE, gmailapi.SCOPE)
        self.assertEqual(mod.SCOPE, "https://www.googleapis.com/auth/gmail.send")
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"web": {"client_id": "cid", "client_secret": "sec", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                           "token_uri": "https://oauth2.googleapis.com/token"}}, f)
        f.close()
        flow = mock.Mock()
        flow.run_local_server.return_value = mock.Mock(refresh_token="1//new-token")
        out = []
        with mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", return_value=flow) as make, \
                mock.patch("sys.argv", ["gmail_auth.py", f.name, "--email", "jha.kunal.neurospark@gmail.com"]), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            mod.main()
        self.assertEqual(make.call_args.kwargs["scopes"], ["https://www.googleapis.com/auth/gmail.send"])
        kw = flow.run_local_server.call_args.kwargs
        self.assertEqual((kw["access_type"], kw["prompt"], kw["port"], kw["login_hint"]),
                         ("offline", "consent", 8080, "jha.kunal.neurospark@gmail.com"))
        text = "\n".join(out)
        for line in ("GOOGLE_CLIENT_ID=cid", "GOOGLE_CLIENT_SECRET=sec", "GMAIL_REFRESH_TOKEN=1//new-token", "http://localhost:8080/"):
            self.assertIn(line, text)
        os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
