"""
The single sending control (once / every day for N days) and phase 3: Connect Gmail in the browser,
password reset by email, two-step sign-in, teams and roles, plans and limits, public pages.
"""
import base64
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from common import ApiBase  # noqa: E402

import accounts  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
import mailer  # noqa: E402
import outreach  # noqa: E402


def totp_now(secret):
    return accounts._totp_at(secret, time.time())


class P3(ApiBase):
    def lead(self, client=None, **kw):
        kw.setdefault("business_name", "Acme Dental")
        r = (client or self.client).post("/api/leads", json=kw)
        assert r.status_code == 200, r.get_json()
        return r.get_json()["lead"]["id"] if "lead" in r.get_json() else r.get_json()["id"]


# ------------------------------------------------------------------ one sending control

class TestSendPlans(P3):
    def setUp(self):
        super().setUp()
        db.set_settings({"warmup_enabled": False, "email_send_delay_seconds": 0, "email_daily_limit": 40})
        for i in range(5):
            lid, _ = db.upsert_lead({"business_name": f"C{i}", "email": f"c{i}@clinic{i}.example"})
            db.update_lead(lid, {"message": "Subject: Hi\n\nHello"})

    def test_once_sends_that_many_now(self):
        with mock.patch.object(outreach, "_deliver") as deliver:
            r = self.client.post("/api/send-plan", json={"count": 3, "mode": "once"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(deliver.call_count, 3)
        self.assertEqual(db.get_settings()["send_plan"], {})                 # once leaves no plan behind

    def test_daily_plan_runs_once_a_day_until_its_last_day(self):
        with mock.patch.object(outreach, "_deliver") as deliver:
            r = self.client.post("/api/send-plan", json={"count": 2, "mode": "daily", "days": 3})
        d = r.get_json()
        self.assertEqual((d["mode"], d["plan"]["count"], d["plan"]["days"]), ("daily", 2, 3))
        self.assertEqual(deliver.call_count, 2)                              # today's share started at once
        self.assertIsNone(outreach.daily_send_due())                         # ...and only once today
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        due = outreach.daily_send_due(now=tomorrow)
        self.assertEqual((due["max"], due["campaign"]), (2, ""))
        after = datetime.now(timezone.utc) + timedelta(days=4)
        self.assertIsNone(outreach.daily_send_due(now=after))
        self.assertFalse(db.get_settings()["send_plan"]["active"])

    def test_a_daily_plan_raises_a_lower_daily_limit(self):
        with mock.patch.object(outreach, "_deliver"):
            r = self.client.post("/api/send-plan", json={"count": 300, "mode": "daily", "days": 5})
        self.assertTrue(r.get_json()["raised_limit"])
        self.assertEqual(db.get_settings()["email_daily_limit"], 300)

    def test_outside_sending_hours_the_plan_waits(self):
        db.set_settings({"send_window_enabled": True, "send_days": [], "send_start_hour": 9, "send_end_hour": 10})
        with mock.patch.object(outreach, "_deliver") as deliver:
            r = self.client.post("/api/send-plan", json={"count": 2, "mode": "daily", "days": 2})
        self.assertIsNone(r.get_json()["job_id"])
        deliver.assert_not_called()
        self.assertTrue(db.get_settings()["send_plan"]["active"])

    def test_stop_and_bad_input(self):
        with mock.patch.object(outreach, "_deliver"):
            self.client.post("/api/send-plan", json={"count": 2, "mode": "daily", "days": 2})
        self.client.delete("/api/send-plan")
        self.assertFalse(db.get_settings()["send_plan"]["active"])
        self.assertIsNone(outreach.daily_send_due(now=datetime.now(timezone.utc) + timedelta(days=1)))
        for bad in ({"count": 0}, {"count": "x"}, {"count": 5, "mode": "daily", "days": 0},
                    {"count": 5, "mode": "daily", "days": 500}, {"count": 5, "mode": "weekly"}):
            self.assertEqual(self.client.post("/api/send-plan", json=bad).status_code, 400, bad)


# ------------------------------------------------------------------ Connect Gmail

def id_token(email):
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class TestConnectGmail(P3):
    def start(self, target="main"):
        with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csec",
                                          "PUBLIC_URL": "https://nexus.example"}):
            r = self.client.get(f"/oauth/google/start?target={target}")
        self.assertEqual(r.status_code, 302)
        return parse_qs(urlparse(r.headers["Location"]).query)

    def callback(self, state, email="kunal@neurospark.in", scope="openid email https://www.googleapis.com/auth/gmail.send"):
        resp = mock.Mock(status_code=200, headers={"content-type": "application/json"})
        resp.json.return_value = {"refresh_token": "1//refresh", "scope": scope, "id_token": id_token(email)}
        with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csec",
                                          "PUBLIC_URL": "https://nexus.example"}), \
                mock.patch("gmailapi.requests.post", return_value=resp) as post:
            r = self.client.get(f"/oauth/google/callback?code=abc&state={state}")
        return r, post

    def test_main_inbox_is_connected_without_any_script(self):
        q = self.start()
        self.assertEqual(q["redirect_uri"], ["https://nexus.example/oauth/google/callback"])
        self.assertIn("https://www.googleapis.com/auth/gmail.send", q["scope"][0])
        self.assertEqual((q["access_type"], q["prompt"]), (["offline"], ["consent"]))
        r, post = self.callback(q["state"][0])
        self.assertIn("gmail=ok", r.headers["Location"])
        self.assertEqual(post.call_args.kwargs["data"]["redirect_uri"], "https://nexus.example/oauth/google/callback")
        user = accounts.get_user(1)
        self.assertEqual((user["secrets"]["GMAIL_REFRESH_TOKEN"], user["secrets"]["SMTP_USER"]),
                         ("1//refresh", "kunal@neurospark.in"))

    def test_extra_inbox_by_google_sign_in(self):
        q = self.start("inbox")
        r, _ = self.callback(q["state"][0], email="sales2@neurospark.in")
        ib = accounts.get_user(1)["secrets"]["INBOXES"][0]
        self.assertEqual((ib["from_email"], ib["gmail_refresh_token"], ib["send_via"]),
                         ("sales2@neurospark.in", "1//refresh", "gmail_api"))

    def test_a_forged_or_reused_state_is_refused(self):
        q = self.start()
        r, post = self.callback(q["state"][0].replace("main", "inbox"))
        self.assertIn("error", r.headers["Location"])
        post.assert_not_called()
        r, _ = self.callback(q["state"][0])                                  # the nonce was used up
        self.assertIn("error", r.headers["Location"])

    def test_missing_send_permission_is_explained(self):
        q = self.start()
        r, _ = self.callback(q["state"][0], scope="openid email")
        self.assertIn("error", r.headers["Location"])
        self.assertNotIn("GMAIL_REFRESH_TOKEN", accounts.get_user(1)["secrets"])

    def test_without_a_google_client_on_the_server(self):
        with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}):
            r = self.client.get("/oauth/google/start")
        self.assertIn("gmail=noclient", r.headers["Location"])


# ------------------------------------------------------------------ password reset

class TestPasswordReset(P3):
    def forgot(self, email):
        sent = []
        with mock.patch.object(mailer, "send", side_effect=lambda to, subj, text: sent.append((to, text)) or True), \
                mock.patch.dict(os.environ, {"PUBLIC_URL": "https://nexus.example"}):
            r = self.new_client().post("/api/auth/forgot", json={"email": email})
        return r, sent

    def test_reset_by_email_link(self):
        r, sent = self.forgot("owner@example.com")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sent[0][0], "owner@example.com")
        link = next(w for w in sent[0][1].split() if w.startswith("https://nexus.example/reset?t="))
        token = link.split("t=")[1]
        anon = self.new_client()
        self.assertEqual(anon.get(f"/reset?t={token}").headers["Location"], f"/login?reset={token}")
        r = anon.post("/api/auth/reset", json={"token": token, "password": "brand new horse"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(anon.get("/api/auth/me").get_json()["email"], "owner@example.com")      # signed in
        self.assertEqual(anon.post("/api/auth/reset", json={"token": token, "password": "another one here"}).status_code, 400)
        self.assertIsNotNone(accounts.authenticate("owner@example.com", "brand new horse"))

    def test_unknown_address_gets_the_same_answer_and_no_email(self):
        r1, sent = self.forgot("nobody@example.com")
        r2, _ = self.forgot("owner@example.com")
        self.assertEqual(sent, [])
        self.assertEqual(r1.get_json()["message"], r2.get_json()["message"])

    def test_expired_and_garbage_tokens(self):
        token = accounts.reset_token("owner@example.com")
        with mock.patch.object(accounts.time, "time", return_value=time.time() + 3 * 3600):
            with self.assertRaises(ValueError):
                accounts.reset_password(token, "brand new horse")
        with self.assertRaises(ValueError):
            accounts.reset_password("1.2.3", "brand new horse")


# ------------------------------------------------------------------ two-step sign-in

class TestTwoStep(P3):
    def turn_on(self):
        secret = self.client.post("/api/auth/2fa/setup").get_json()["secret"]
        r = self.client.post("/api/auth/2fa/enable", json={"code": totp_now(secret)})
        self.assertEqual(r.status_code, 200, r.get_json())
        return secret, r.get_json()["backup_codes"]

    def test_sign_in_needs_the_code(self):
        secret, backups = self.turn_on()
        self.assertEqual(len(backups), 8)
        c = self.new_client()
        r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse"})
        self.assertEqual((r.status_code, r.get_json()["need_code"]), (401, True))
        self.assertEqual(c.get("/api/leads").status_code, 401)
        r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse", "code": "000000"})
        self.assertEqual(r.status_code, 401)
        r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse", "code": totp_now(secret)})
        self.assertEqual(r.status_code, 200)

    def test_backup_codes_work_once(self):
        _, backups = self.turn_on()
        login = {"email": "owner@example.com", "password": "correct horse", "code": backups[0]}
        self.assertEqual(self.new_client().post("/api/auth/login", json=login).status_code, 200)
        self.assertEqual(self.new_client().post("/api/auth/login", json=login).status_code, 401)

    def test_wrong_setup_code_and_turning_off(self):
        secret = self.client.post("/api/auth/2fa/setup").get_json()["secret"]
        self.assertEqual(self.client.post("/api/auth/2fa/enable", json={"code": "123456" if totp_now(secret) != "123456" else "654321"}).status_code, 400)
        self.turn_on()
        self.assertEqual(self.client.post("/api/auth/2fa/disable", json={"password": "wrong"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/2fa/disable", json={"password": "correct horse"}).status_code, 200)
        r = self.new_client().post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse"})
        self.assertEqual(r.status_code, 200)

    def test_the_secret_is_stored_encrypted(self):
        secret, _ = self.turn_on()
        with accounts._conn() as c:
            stored = c.execute("SELECT totp_enc FROM users WHERE id = 1").fetchone()[0]
        self.assertNotIn(secret, stored)


# ------------------------------------------------------------------ teams

class TestTeams(P3):
    def invite(self, email="mate@example.com", role="member"):
        with mock.patch.object(mailer, "send", return_value=False):
            r = self.client.post("/api/team/invite", json={"email": email, "role": role})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["link"].split("t=")[1]

    def join(self, token, email="mate@example.com"):
        c = self.new_client()
        with mock.patch.dict(os.environ, {"ALLOW_SIGNUP": "0"}):             # invited people get in even when sign-up is closed
            r = c.post("/api/auth/signup", json={"email": email, "password": "team horse battery", "invite": token})
        self.assertEqual(r.status_code, 200, r.get_json())
        return c

    def test_a_teammate_works_on_the_owners_leads(self):
        lid = self.lead(business_name="Owner's lead")
        mate = self.join(self.invite())
        leads = mate.get("/api/leads").get_json()["leads"]
        self.assertEqual([l["business_name"] for l in leads], ["Owner's lead"])
        r = mate.post("/api/leads", json={"business_name": "Added by mate"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/leads").get_json()["total"], 2)
        team = self.client.get("/api/team").get_json()
        self.assertEqual([m["role"] for m in team["members"]], ["owner", "member"])

    def test_members_cant_change_connections_or_settings_but_admins_can(self):
        mate = self.join(self.invite())
        for path, method, body in (("/api/connections", "put", {"set": {"GROQ_API_KEY": "x"}}),
                                   ("/api/settings", "put", {"email_daily_limit": 5}),
                                   ("/api/inboxes", "post", {"smtp_user": "a@b.in", "smtp_pass": "p"}),
                                   ("/api/team/invite", "post", {"email": "x@y.in"})):
            self.assertEqual(getattr(mate, method)(path, json=body).status_code, 403, path)
        self.assertEqual(mate.get("/api/settings").status_code, 200)               # reading is fine
        mate_id = [m["id"] for m in self.client.get("/api/team").get_json()["members"] if m["role"] == "member"][0]
        self.client.post(f"/api/team/member/{mate_id}", json={"role": "admin"})
        self.assertEqual(mate.put("/api/settings", json={"email_daily_limit": 5}).status_code, 200)
        self.assertEqual(db.get_settings()["email_daily_limit"], 5)                # the owner's workspace changed

    def test_connections_are_shared_from_the_owner(self):
        self.client.put("/api/connections", json={"set": {"SMTP_USER": "owner@acme.in", "SMTP_PASS": "pw"}})
        mate = self.join(self.invite(role="admin"))
        self.assertTrue(mate.get("/api/config-status").get_json()["smtp"])

    def test_removing_someone_sends_them_back_to_their_own_workspace(self):
        self.lead(business_name="Team lead")
        mate = self.join(self.invite())
        mate_id = [m["id"] for m in self.client.get("/api/team").get_json()["members"] if m["role"] == "member"][0]
        self.client.delete(f"/api/team/member/{mate_id}")
        self.assertEqual(mate.get("/api/leads").get_json()["total"], 0)

    def test_an_invite_is_for_one_address_and_one_use(self):
        token = self.invite()
        c = self.new_client()
        r = c.post("/api/auth/signup", json={"email": "someone.else@example.com", "password": "team horse battery", "invite": token})
        self.assertEqual(r.status_code, 400)
        self.join(token)
        r = self.new_client().post("/api/auth/signup", json={"email": "mate@example.com", "password": "x horse battery", "invite": token})
        self.assertEqual(r.status_code, 400)

    def test_scheduler_runs_each_workspace_once(self):
        self.join(self.invite())
        seen = []
        jobs.run_scheduler_once(lambda *a, **k: None, lambda: seen.append(1))
        self.assertEqual(len(seen), 1)


# ------------------------------------------------------------------ plans

TINY = json.dumps({"tiny": {"label": "Tiny", "leads": 2, "emails_per_day": 1, "inboxes": 1, "members": 1,
                            "scrapes_per_day": 1, "apify": False},
                   "unlimited": {"label": "Unlimited"}})


class TestPlans(P3):
    def second_user(self):
        c = self.signup("small@example.com")
        return c

    def test_limits_apply_to_a_plan_but_never_to_the_admin(self):
        with mock.patch.dict(os.environ, {"PLANS_JSON": TINY, "DEFAULT_PLAN": "tiny"}):
            c = self.second_user()
            for i in range(2):
                self.assertEqual(c.post("/api/leads", json={"business_name": f"L{i}"}).status_code, 200)
            r = c.post("/api/leads", json={"business_name": "one too many"})
            self.assertNotEqual(r.status_code, 200)
            self.assertIn("limit", r.get_json()["error"])
            self.assertEqual(c.post("/api/inboxes", json={"smtp_user": "a@b.in", "smtp_pass": "p"}).status_code, 400)
            self.assertEqual(c.post("/api/team/invite", json={"email": "x@y.in"}).status_code, 400)
            r = c.post("/api/scrape", json={"platform": "apify_maps", "query": "x", "location": "Pune"})
            self.assertEqual(r.status_code, 400)
            plan = c.get("/api/plan").get_json()
            self.assertEqual((plan["plan"]["name"], plan["usage"]["leads"]), ("tiny", 2))
            for i in range(3):                                                  # the admin isn't limited
                self.assertEqual(self.client.post("/api/leads", json={"business_name": f"A{i}"}).status_code, 200)
            st = c.get("/api/email-status").get_json()
            self.assertLessEqual(st["daily_limit"], 1)

    def test_admin_changes_someones_plan(self):
        with mock.patch.dict(os.environ, {"PLANS_JSON": TINY, "DEFAULT_PLAN": "tiny"}):
            c = self.second_user()
            uid = c.get("/api/auth/me").get_json()["id"]
            r = self.client.post(f"/api/admin/users/{uid}/plan", json={"plan": "unlimited"})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(c.get("/api/plan").get_json()["plan"]["name"], "unlimited")
            self.assertEqual(c.post(f"/api/admin/users/{uid}/plan", json={"plan": "tiny"}).status_code, 403)

    def test_an_existing_install_is_unlimited_by_default(self):
        c = self.second_user()
        self.assertEqual(c.get("/api/plan").get_json()["plan"]["name"], "unlimited")


# ------------------------------------------------------------------ public pages

class TestPublicPages(P3):
    def test_homepage_terms_privacy(self):
        anon = self.new_client()
        r = anon.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Pro", r.data)
        for path in ("/welcome", "/terms", "/privacy"):
            self.assertEqual(anon.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/").status_code, 200)                 # signed in: the app itself
        self.assertIn(b'id="view-dashboard"', self.client.get("/").data)


if __name__ == "__main__":
    unittest.main()
