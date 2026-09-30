"""Operations: security headers, rate limits, first-run setup code, connection tests, exports, backups, and the
end-to-end promise that logins and data survive a wiped host."""
import io
import json
import os
import smtplib
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import ApiBase  # noqa: E402

import accounts  # noqa: E402
import cryptobox  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
import netguard  # noqa: E402
import outreach  # noqa: E402
import persistence  # noqa: E402
from test_hardening import fake_dns  # noqa: E402


class OpsBase(ApiBase):
    def fresh_app(self, env=None, remote="127.0.0.1"):
        """A brand-new app (and empty data folder) started with extra environment settings."""
        self.data2 = tempfile.mkdtemp()
        e = mock.patch.dict(os.environ, {"DATA_DIR": self.data2, **(env or {})})
        e.start()
        self.addCleanup(e.stop)
        accounts._init_done.clear()
        cryptobox._file_key.cache_clear()
        import app as appmod
        app = appmod.create_app(start_threads=False)
        c = app.test_client()
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "fetch"
        c.environ_base["REMOTE_ADDR"] = remote
        return app, c


class TestHeadersAndLimits(OpsBase):
    def test_security_headers_and_no_caching_of_private_pages(self):
        for path in ("/api/leads", "/", "/api/ping"):
            r = self.client.get(path)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff", path)
            self.assertEqual(r.headers["X-Frame-Options"], "DENY", path)
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"], path)
            self.assertEqual(r.headers["Cache-Control"], "no-store", path)
        css = self.client.get("/static/app.css")
        self.assertNotIn("no-store", css.headers.get("Cache-Control", ""))        # static files stay cacheable
        css.close()
        self.assertNotIn("Strict-Transport-Security", self.client.get("/api/ping").headers)
        with mock.patch.dict(os.environ, {"COOKIE_SECURE": "1"}):
            _, c = self.fresh_app({"COOKIE_SECURE": "1"})
            self.assertIn("Strict-Transport-Security", c.get("/api/ping").headers)

    def test_no_inline_scripts_so_the_csp_can_stay_strict(self):
        for path in ("/", "/login"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", path)   # every <script> has a src

    def test_a_busy_user_is_slowed_down_but_others_are_not(self):
        app, alice = self.fresh_app({"RATE_LIMIT_PER_MIN": "30"})
        alice.post("/api/auth/signup", json={"email": "alice@example.com", "password": "horse-battery-1"})
        bob = app.test_client()
        bob.environ_base.update(alice.environ_base)
        bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "horse-battery-2"})
        codes = [alice.get("/api/leads").status_code for _ in range(45)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)
        r = alice.get("/api/leads")
        self.assertEqual(r.status_code, 429)
        self.assertGreaterEqual(int(r.headers["Retry-After"]), 1)
        self.assertEqual(bob.get("/api/leads").status_code, 200)                    # Bob is unaffected

    def test_expensive_calls_cost_more(self):
        _, c = self.fresh_app({"RATE_LIMIT_PER_MIN": "30"})
        c.post("/api/auth/signup", json={"email": "a@example.com", "password": "horse-battery-1"})
        codes = [c.post("/api/enrich", json={}).status_code for _ in range(6)]
        self.assertIn(429, codes)                                                    # only ~3 heavy calls fit in a minute

    def test_public_pages_are_limited_per_address(self):
        _, c = self.fresh_app({"RATE_LIMIT_PER_MIN": "600"})
        codes = [c.get("/api/auth/status").status_code for _ in range(140)]
        self.assertIn(429, codes)

    def test_limits_can_be_switched_off(self):
        _, c = self.fresh_app({"RATE_LIMIT_PER_MIN": "0"})
        c.post("/api/auth/signup", json={"email": "a@example.com", "password": "horse-battery-1"})
        self.assertNotIn(429, [c.get("/api/leads").status_code for _ in range(60)])

    def test_scrape_queue_is_capped_per_person(self):
        for _ in range(3):
            db.create_job("scrape", {"campaign": ""})                                 # three already waiting
        r = self.client.post("/api/scrape", json={"platform": "google_maps", "query": "plumbers"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("share one browser", r.get_json()["error"])

    def test_weak_passwords_are_refused(self):
        for pw in ("password123", "aaaaaaaa", "owner-secret"):
            r = self.new_client().post("/api/auth/signup", json={"email": "owner@example.org", "password": pw})
            self.assertEqual(r.status_code, 400, pw)


class TestFirstRunSetupCode(OpsBase):
    def test_a_stranger_cannot_claim_a_fresh_public_server(self):
        _, c = self.fresh_app(remote="203.0.113.7")
        st = c.get("/api/auth/status").get_json()
        self.assertEqual((st["needs_first_user"], st["needs_code"], st["code_kind"]), (True, True, "setup"))
        body = {"email": "attacker@example.com", "password": "horse-battery-1"}
        self.assertEqual(c.post("/api/auth/signup", json=body).status_code, 400)
        self.assertEqual(c.post("/api/auth/signup", json={**body, "code": "guess"}).status_code, 400)
        self.assertEqual(accounts.count_users(), 0)
        r = c.post("/api/auth/signup", json={**body, "code": accounts.setup_code()})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["user"]["is_admin"])

    def test_only_wrong_codes_count_towards_the_lockout(self):
        _, c = self.fresh_app(remote="203.0.113.7")
        for _ in range(4):                                                            # weak passwords are just typos
            c.post("/api/auth/signup", json={"email": "a@example.com", "password": "password123", "code": accounts.setup_code()})
        self.assertEqual(c.post("/api/auth/signup", json={"email": "a@example.com", "password": "horse-battery-1",
                                                          "code": accounts.setup_code()}).status_code, 200)
        _, d = self.fresh_app(remote="203.0.113.8")
        for _ in range(5):                                                            # ...guessing the code is not
            d.post("/api/auth/signup", json={"email": "a@example.com", "password": "horse-battery-1", "code": "guess"})
        r = d.post("/api/auth/signup", json={"email": "a@example.com", "password": "horse-battery-1", "code": accounts.setup_code()})
        self.assertEqual(r.status_code, 429)

    def test_using_it_on_your_own_computer_needs_no_code(self):
        _, c = self.fresh_app(remote="127.0.0.1")
        self.assertFalse(c.get("/api/auth/status").get_json()["needs_code"])
        self.assertEqual(c.post("/api/auth/signup", json={"email": "me@example.com", "password": "horse-battery-1"}).status_code, 200)

    def test_behind_a_hosting_proxy_the_code_is_required_even_though_requests_look_local(self):
        _, c = self.fresh_app({"TRUST_PROXY": "1"}, remote="127.0.0.1")
        self.assertTrue(c.get("/api/auth/status").get_json()["needs_code"])

    def test_the_code_is_only_announced_in_the_log_for_a_real_start(self):
        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            self.fresh_app()                                # tests create apps with threads off
        self.assertNotIn("Setup code", buf.getvalue())
        buf = _io.StringIO()
        with redirect_stdout(buf):
            accounts.announce_setup_code()
        self.assertIn(accounts.setup_code(), buf.getvalue())


class TestConnectionTests(OpsBase):
    def setUp(self):
        super().setUp()
        self.client.put("/api/connections", json={"set": {"SMTP_USER": "me@gmail.com", "SMTP_PASS": "app-pass-1234"}})

    def test_smtp_success_and_bad_password_and_blocked_port(self):
        good = mock.MagicMock()
        with fake_dns({"smtp.gmail.com": ["142.250.1.109"]}), mock.patch.object(outreach.smtplib, "SMTP", return_value=good):
            good.__enter__.return_value = good
            r = self.client.post("/api/connections/test", json={"kind": "smtp"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("Nothing was sent", r.get_json()["message"])
            good.login.assert_called_once_with("me@gmail.com", "app-pass-1234")
            bad = mock.MagicMock()
            bad.__enter__.return_value = bad
            bad.login.side_effect = smtplib.SMTPAuthenticationError(535, b"nope")
            with mock.patch.object(outreach.smtplib, "SMTP", return_value=bad):
                r = self.client.post("/api/connections/test", json={"kind": "smtp"})
                self.assertEqual(r.status_code, 400)
                self.assertIn("App Password", r.get_json()["error"])
            with mock.patch.object(outreach.smtplib, "SMTP", side_effect=OSError("timed out")):
                r = self.client.post("/api/connections/test", json={"kind": "smtp"})
                self.assertEqual(r.status_code, 502)
                self.assertIn("Render", r.get_json()["error"])          # explains free hosts blocking mail ports

    def test_the_test_refuses_internal_mail_servers(self):
        self.client.put("/api/connections", json={"set": {"SMTP_HOST": "mail.internal-corp.example"}})
        with fake_dns({"mail.internal-corp.example": ["10.0.0.7"]}):
            r = self.client.post("/api/connections/test", json={"kind": "smtp"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("private or internal", r.get_json()["error"])

    def test_imap_and_llm_and_unknown(self):
        with fake_dns({"imap.gmail.com": ["142.250.1.109"]}), mock.patch.object(outreach.imaplib, "IMAP4_SSL") as imap:
            self.assertEqual(self.client.post("/api/connections/test", json={"kind": "imap"}).status_code, 200)
            imap.return_value.login.assert_called_once()
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}), mock.patch.object(outreach.llm, "complete", return_value="OK"):
            r = self.client.post("/api/connections/test", json={"kind": "llm"})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/api/connections/test", json={"kind": "bogus"}).status_code, 400)

    def test_unreadable_saved_keys_are_reported_after_a_key_change(self):
        bob = self.signup("bob@example.com")                      # (not the admin, so no fallback to the server's .env)
        with mock.patch.dict(os.environ, {"SECRET_KEY": "x" * 40}):
            bob.put("/api/connections", json={"set": {"SMTP_USER": "bob@gmail.com", "SMTP_PASS": "app-pass-1234"}})
            self.assertFalse(bob.get("/api/connections").get_json()["unreadable"])
            self.assertTrue(bob.get("/api/config-status").get_json()["smtp"])
        with mock.patch.dict(os.environ, {"SECRET_KEY": "y" * 40}):
            self.assertTrue(bob.get("/api/connections").get_json()["unreadable"])      # the page can tell them why
            self.assertFalse(bob.get("/api/config-status").get_json()["smtp"])
            r = bob.put("/api/connections", json={"set": {"SMTP_USER": "new@gmail.com"}}).get_json()
            self.assertFalse(r["unreadable"])                                          # re-entering fixes it; old blob kept aside


class TestExportsAndBackups(OpsBase):
    def test_each_person_can_download_their_own_data_without_secrets(self):
        self.client.put("/api/connections", json={"set": {"GROQ_API_KEY": "gsk_super_secret_key"}})
        self.client.post("/api/campaigns", json={"name": "dallas", "description": "d"})
        self.client.post("/api/leads", json={"business_name": "Acme Roofing", "phone": "(214) 555-0100", "campaign": "dallas"})
        r = self.client.get("/api/export/my-data.zip")
        self.assertEqual(r.headers["Content-Type"], "application/zip")
        z = zipfile.ZipFile(io.BytesIO(r.data))
        self.assertEqual(sorted(z.namelist()), ["README.txt", "campaigns.json", "events.csv", "leads.csv",
                                                "settings.json", "suppression.csv"])
        self.assertIn("Acme Roofing", z.read("leads.csv").decode())
        self.assertEqual(json.loads(z.read("campaigns.json"))[0]["name"], "dallas")
        everything = b"".join(z.read(n) for n in z.namelist())
        self.assertNotIn(b"gsk_super_secret_key", everything)
        self.assertNotIn(b"pbkdf2", everything)
        bob = self.signup("bob@example.com")
        self.assertNotIn(b"Acme Roofing", zipfile.ZipFile(io.BytesIO(bob.get("/api/export/my-data.zip").data)).read("leads.csv"))

    def test_the_admin_can_download_a_full_backup_and_others_cannot(self):
        self.client.post("/api/leads", json={"business_name": "Acme Roofing"})
        bob = self.signup("bob@example.com")
        self.assertEqual(bob.get("/api/admin/backup.zip").status_code, 403)
        z = zipfile.ZipFile(io.BytesIO(self.client.get("/api/admin/backup.zip").data))
        self.assertEqual(sorted(z.namelist()), ["README.txt", "accounts.db", "users/1.db", "users/2.db"])
        path = os.path.join(tempfile.mkdtemp(), "copy.db")
        open(path, "wb").write(z.read("users/1.db"))
        self.assertEqual(sqlite3.connect(path).execute("SELECT business_name FROM leads").fetchone()[0], "Acme Roofing")

    def test_backup_now_needs_outside_storage(self):
        r = self.client.post("/api/admin/backup-now")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Download a full backup", r.get_json()["error"])
        st = self.client.get("/api/admin/storage").get_json()
        self.assertEqual(st["sync"], {"enabled": False})
        self.assertEqual(self.signup("bob@example.com").get("/api/admin/storage").status_code, 403)


class TestSurvivesAWipedHost(OpsBase):
    """The headline promise: on a free host whose disk is wiped, logins and data come back."""

    def boot(self, disk, store_dir, env=None):
        d = os.path.join(self.tmp_root, disk)
        os.makedirs(d, exist_ok=True)
        e = mock.patch.dict(os.environ, {"DATA_DIR": d, "SYNC_FOLDER": store_dir, "SECRET_KEY": "s" * 48, **(env or {})})
        e.start()
        self.addCleanup(e.stop)
        persistence.reset_for_tests()
        accounts._init_done.clear()
        cryptobox._file_key.cache_clear()
        import app as appmod
        with mock.patch("persistence.signal.signal"), mock.patch("persistence.atexit.register"):
            app = appmod.create_app(start_threads=False)
        c = app.test_client()
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "fetch"
        return app, c

    def setUp(self):
        super().setUp()
        self.tmp_root = tempfile.mkdtemp()
        self.store_dir = os.path.join(self.tmp_root, "supabase-bucket")
        self.addCleanup(persistence.reset_for_tests)

    def test_logins_leads_and_saved_keys_come_back_after_the_disk_is_wiped(self):
        _, c = self.boot("host-disk-1", self.store_dir)
        self.assertEqual(c.post("/api/auth/signup", json={"email": "me@example.com", "password": "horse-battery-1"}).status_code, 200)
        c.put("/api/connections", json={"set": {"SMTP_USER": "me@gmail.com", "SMTP_PASS": "app-pass-1234"}})
        c.post("/api/campaigns", json={"name": "dallas-hvac"})
        c.post("/api/leads", json={"business_name": "Cold Air", "phone": "(214) 555-0100", "campaign": "dallas-hvac"})
        persistence.instance().flush()                                               # what a shutdown does

        _, fresh = self.boot("host-disk-2", self.store_dir)                          # a new host with an EMPTY disk
        r = fresh.post("/api/auth/login", json={"email": "me@example.com", "password": "horse-battery-1"})
        self.assertEqual(r.status_code, 200)                                         # login access was stored
        self.assertEqual([l["business_name"] for l in fresh.get("/api/leads").get_json()["leads"]], ["Cold Air"])
        self.assertEqual([c_["name"] for c_ in fresh.get("/api/campaigns").get_json()["campaigns"]], ["dallas-hvac"])
        self.assertTrue(fresh.get("/api/config-status").get_json()["smtp"])          # saved email details came back too

    def test_the_stored_copy_is_unreadable_without_the_secret_key(self):
        _, c = self.boot("host-disk-1", self.store_dir)
        c.post("/api/auth/signup", json={"email": "me@example.com", "password": "horse-battery-1"})
        c.post("/api/leads", json={"business_name": "Very Private Plumbing"})
        persistence.instance().flush()
        blobs = [p.read_bytes() for p in __import__("pathlib").Path(self.store_dir).rglob("*") if p.is_file()]
        self.assertTrue(blobs)
        for b in blobs:
            self.assertNotIn(b"Very Private Plumbing", b)
            self.assertNotIn(b"me@example.com", b)

    def test_the_app_refuses_to_start_if_storage_is_unreachable(self):
        with mock.patch.object(persistence.LocalStore, "list", side_effect=OSError("project paused")):
            with self.assertRaises(persistence.PersistenceError):
                self.boot("host-disk-1", self.store_dir)

    def test_the_app_refuses_to_start_without_a_secret_key_when_data_is_copied_elsewhere(self):
        d = os.path.join(self.tmp_root, "d")
        with mock.patch.dict(os.environ, {"DATA_DIR": d, "SYNC_FOLDER": self.store_dir, "SECRET_KEY": ""}):
            cryptobox._file_key.cache_clear()
            persistence.reset_for_tests()
            import app as appmod
            with self.assertRaises(cryptobox.KeyError_):
                appmod.create_app(start_threads=False)

    def test_ping_reports_when_storage_needs_attention(self):
        _, c = self.boot("host-disk-1", self.store_dir)
        c.post("/api/auth/signup", json={"email": "me@example.com", "password": "horse-battery-1"})
        self.assertTrue(c.get("/api/ping").get_json()["storage_ok"])
        persistence.instance().conflicts["accounts.db"] = {"why": "test"}
        self.assertFalse(c.get("/api/ping").get_json()["storage_ok"])

    def test_admin_can_see_storage_status_and_run_a_backup(self):
        _, c = self.boot("host-disk-1", self.store_dir)
        c.post("/api/auth/signup", json={"email": "me@example.com", "password": "horse-battery-1"})
        persistence.instance().flush()
        st = c.get("/api/admin/storage").get_json()["sync"]
        self.assertTrue(st["enabled"])
        self.assertEqual(st["kind"], "folder")
        r = c.post("/api/admin/backup-now").get_json()
        self.assertGreaterEqual(r["files_backed_up"], 2)


if __name__ == "__main__":
    unittest.main()
