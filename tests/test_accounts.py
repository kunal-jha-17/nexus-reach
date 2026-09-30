"""Accounts, sign-in, and the guarantee that one user can't see another's data or credentials."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase  # noqa: E402

import accounts  # noqa: E402
import manage  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import enrichment  # noqa: E402
import jobs  # noqa: E402
import userctx  # noqa: E402


class AccountsBase(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {
            "DATA_DIR": self.data, "ADOPT_LEGACY_DB": "0", "HUB_DB": os.path.join(self.data, "legacy.db"),
            "SIGNUP_CODE": "", "ALLOW_SIGNUP": "1", "SHARE_LLM_WITH_USERS": "",
            "SMTP_USER": "server@example.org", "SMTP_PASS": "server-pass", "GROQ_API_KEY": "server-groq-key"})
        self._env.start()
        accounts._init_done.clear()

    def tearDown(self):
        self._env.stop()

    def ctx(self, user):
        return userctx.use(user, accounts.db_path_for(user["id"]))


class TestAccounts(AccountsBase):
    def test_first_user_is_admin_and_gets_own_database(self):
        a = accounts.create_user("Owner@Example.com", "horse-battery-1", "Owner")
        b = accounts.create_user("second@example.com", "horse-battery-2")
        self.assertEqual((a["email"], a["is_admin"], b["is_admin"]), ("owner@example.com", True, False))
        self.assertNotEqual(accounts.db_path_for(a["id"]), accounts.db_path_for(b["id"]))
        self.assertTrue(os.path.exists(accounts.db_path_for(a["id"])))
        self.assertTrue(os.path.exists(accounts.db_path_for(b["id"])))

    def test_validation(self):
        accounts.create_user("a@example.com", "horse-battery-1")
        for email, pw in (("not-an-email", "horse-battery-1"), ("b@example.com", "short"), ("a@example.com", "horse-battery-1")):
            with self.assertRaises(ValueError):
                accounts.create_user(email, pw)

    def test_password_is_hashed_and_login_works(self):
        accounts.create_user("a@example.com", "horse-battery-1")
        with accounts._conn() as c:
            stored = c.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn("horse-battery-1", stored)
        self.assertTrue(stored.startswith("pbkdf2:"))          # works on Pythons without scrypt
        self.assertIsNotNone(accounts.authenticate("A@example.com", "horse-battery-1"))
        self.assertIsNone(accounts.authenticate("a@example.com", "wrong-password"))
        self.assertIsNone(accounts.authenticate("nobody@example.com", "horse-battery-1"))

    def test_data_is_stored_in_separate_databases(self):
        a = accounts.create_user("a@example.com", "horse-battery-1")
        b = accounts.create_user("b@example.com", "horse-battery-2")
        with self.ctx(a):
            db.upsert_lead({"business_name": "Only Alice's", "phone": "2145550100"})
            self.assertEqual(db.stats()["total"], 1)
        with self.ctx(b):
            self.assertEqual(db.stats()["total"], 0)
            self.assertIsNone(db.get_lead(1))                 # same id number, different database
            db.upsert_lead({"business_name": "Bob's", "phone": "2145550100"})   # same phone is fine
        with self.ctx(a):
            self.assertEqual([l["business_name"] for l in db.list_leads()[0]], ["Only Alice's"])

    def test_signup_code_and_closing_signups(self):
        with mock.patch.dict(os.environ, {"SIGNUP_CODE": "letmein"}):
            with self.assertRaises(ValueError):
                accounts.create_user("a@example.com", "horse-battery-1", code="nope")
            accounts.create_user("a@example.com", "horse-battery-1", code="letmein")
        with mock.patch.dict(os.environ, {"ALLOW_SIGNUP": "0"}):
            self.assertFalse(accounts.signup_policy()["allowed"])
            with self.assertRaises(ValueError):
                accounts.create_user("b@example.com", "horse-battery-1")

    def test_change_password_invalidates_old_sessions(self):
        u = accounts.create_user("a@example.com", "horse-battery-1")
        with self.assertRaises(ValueError):
            accounts.change_password(u["id"], "wrong", "new-horse-battery-1")
        new = accounts.change_password(u["id"], "horse-battery-1", "new-horse-battery-1")
        self.assertNotEqual(u["pv"], new["pv"])
        self.assertIsNone(accounts.authenticate("a@example.com", "horse-battery-1"))
        self.assertIsNotNone(accounts.authenticate("a@example.com", "new-horse-battery-1"))

    def test_throttle(self):
        for _ in range(5):
            self.assertFalse(accounts.throttled("k"))
            accounts.record_failure("k")
        self.assertTrue(accounts.throttled("k"))
        accounts.clear_failures("k")
        self.assertFalse(accounts.throttled("k"))

    def test_delete_user_removes_database(self):
        accounts.create_user("a@example.com", "horse-battery-1")
        b = accounts.create_user("b@example.com", "horse-battery-2")
        path = accounts.db_path_for(b["id"])
        self.assertEqual(accounts.delete_user(b["id"]), 1)
        self.assertFalse(os.path.exists(path))

    def test_existing_single_user_database_is_adopted_by_first_account_only(self):
        legacy = os.path.join(self.data, "legacy.db")
        db.set_path(legacy)
        db.init_db()
        db.upsert_lead({"business_name": "From The Old Days", "phone": "2145550100"})
        db.set_path(None)
        with mock.patch.dict(os.environ, {"ADOPT_LEGACY_DB": "1"}):
            first = accounts.create_user("a@example.com", "horse-battery-1")
            second = accounts.create_user("b@example.com", "horse-battery-2")
        with self.ctx(first):
            self.assertEqual([l["business_name"] for l in db.list_leads()[0]], ["From The Old Days"])
        with self.ctx(second):
            self.assertEqual(db.stats()["total"], 0)
        self.assertTrue(os.path.exists(legacy))               # the original is left untouched as a backup


class TestCredentials(AccountsBase):
    def test_secrets_are_encrypted_at_rest(self):
        u = accounts.create_user("a@example.com", "horse-battery-1")
        accounts.set_secrets(u["id"], {"SMTP_PASS": "super-secret-value", "SMTP_USER": "me@gmail.com",
                                       "NOT_ALLOWED": "x", "GROQ_API_KEY": "  "})
        with accounts._conn() as c:
            raw = c.execute("SELECT secrets_enc FROM users").fetchone()[0]
        self.assertNotIn("super-secret-value", raw)
        got = accounts.get_user(u["id"])["secrets"]
        self.assertEqual(got, {"SMTP_PASS": "super-secret-value", "SMTP_USER": "me@gmail.com"})
        accounts.set_secrets(u["id"], clear=["SMTP_PASS"])
        self.assertNotIn("SMTP_PASS", accounts.get_user(u["id"])["secrets"])

    def test_users_never_borrow_each_others_or_the_servers_email_credentials(self):
        admin = accounts.create_user("admin@example.com", "horse-battery-1")
        bob = accounts.create_user("bob@example.com", "horse-battery-2")
        with self.ctx(admin):     # the admin's .env keeps working
            self.assertEqual(config.env("SMTP_USER"), "server@example.org")
        with self.ctx(bob):       # everyone else starts with nothing
            self.assertEqual(config.env("SMTP_USER"), "")
            self.assertEqual(config.env("GROQ_API_KEY"), "")
        accounts.set_secrets(bob["id"], {"SMTP_USER": "bob@gmail.com", "SMTP_PASS": "bobpass"})
        bob = accounts.get_user(bob["id"])
        with self.ctx(bob):
            self.assertEqual((config.env("SMTP_USER"), config.env("SMTP_PASS")), ("bob@gmail.com", "bobpass"))
        with self.ctx(accounts.get_user(admin["id"])):
            self.assertEqual(config.env("SMTP_USER"), "server@example.org")

    def test_ai_keys_can_be_shared_but_email_never(self):
        accounts.create_user("admin@example.com", "horse-battery-1")
        bob = accounts.create_user("bob@example.com", "horse-battery-2")
        with mock.patch.dict(os.environ, {"SHARE_LLM_WITH_USERS": "1"}), self.ctx(bob):
            self.assertEqual(config.env("GROQ_API_KEY"), "server-groq-key")
            self.assertEqual(config.env("SMTP_PASS"), "")

    def test_own_key_beats_server_key_for_admin_too(self):
        admin = accounts.create_user("admin@example.com", "horse-battery-1")
        accounts.set_secrets(admin["id"], {"GROQ_API_KEY": "my-own"})
        with self.ctx(accounts.get_user(admin["id"])):
            self.assertEqual(config.env("GROQ_API_KEY"), "my-own")
            self.assertEqual(config.source_of("GROQ_API_KEY"), "account")

    def test_non_credential_settings_stay_server_wide(self):
        bob = accounts.create_user("a@example.com", "horse-battery-1")
        accounts.create_user("bob@example.com", "horse-battery-2")
        with mock.patch.dict(os.environ, {"ENRICH_MAX_SEARCHES": "7"}), self.ctx(bob):
            self.assertEqual(config.env_int("ENRICH_MAX_SEARCHES", 3), 7)

    def test_connections_view_never_contains_secret_values(self):
        u = accounts.create_user("a@example.com", "horse-battery-1")
        accounts.set_secrets(u["id"], {"SMTP_PASS": "abcdefghijklmnop", "SMTP_USER": "me@gmail.com"})
        with self.ctx(accounts.get_user(u["id"])):
            view = {f["name"]: f for f in accounts.connections_view()}
        self.assertNotIn("value", view["SMTP_PASS"])
        self.assertNotIn("abcdefghijklmnop", str(view["SMTP_PASS"]))
        self.assertTrue(view["SMTP_PASS"]["set"])
        self.assertEqual(view["SMTP_USER"]["value"], "me@gmail.com")


class TestJobsRunAsTheRightUser(AccountsBase):
    def test_background_thread_uses_the_starting_users_database_and_credentials(self):
        a = accounts.create_user("a@example.com", "horse-battery-1")
        b = accounts.create_user("b@example.com", "horse-battery-2")
        accounts.set_secrets(b["id"], {"SERPAPI_KEY": "bobs-key"})
        b = accounts.get_user(b["id"])
        seen = {}

        def work(ctx, params):
            seen["user"] = userctx.get_user()["email"]
            seen["key"] = config.env("SERPAPI_KEY")
            db.upsert_lead({"business_name": "Made by job", "phone": "2145550111"})
            return "ok"

        with mock.patch.dict(os.environ, {"HUB_JOBS_SYNC": ""}), self.ctx(b):
            job_id = jobs.start_job("enrich", {}, work)
            for _ in range(100):
                if db.get_job(job_id)["status"] in ("done", "error"):
                    break
                time.sleep(0.05)
            self.assertEqual(db.get_job(job_id)["status"], "done")
        self.assertEqual(seen, {"user": "b@example.com", "key": "bobs-key"})
        with self.ctx(b):
            self.assertEqual(db.stats()["total"], 1)
        with self.ctx(a):
            self.assertEqual(db.stats()["total"], 0)
            self.assertEqual(db.list_jobs(), [])              # A never sees B's job either

    def test_singleton_jobs_are_per_user(self):
        a = accounts.create_user("a@example.com", "horse-battery-1")
        b = accounts.create_user("b@example.com", "horse-battery-2")
        import threading
        release = threading.Event()

        def slow(ctx, params):
            release.wait(5)
            return "done"
        with mock.patch.dict(os.environ, {"HUB_JOBS_SYNC": ""}):
            with self.ctx(a):
                jobs.start_job("enrich", {}, slow)
                with self.assertRaises(jobs.JobBusy):          # A can't run two enrich jobs...
                    jobs.start_job("enrich", {}, slow)
            with self.ctx(b):
                jobs.start_job("enrich", {}, slow)             # ...but B isn't blocked by A
            release.set()
            time.sleep(0.3)

    def test_scheduler_runs_each_users_due_scrapes_as_that_user(self):
        a = accounts.create_user("a@example.com", "horse-battery-1")
        b = accounts.create_user("b@example.com", "horse-battery-2")
        for u, q in ((a, "plumbers"), (b, "roofers")):
            with self.ctx(u):
                db.create_schedule("google_maps", q, "Dallas, TX", 10, "camp", 24)
        launched = []
        jobs.run_scheduler_once(lambda platform, query, loc, n, camp:
                                launched.append((userctx.get_user()["email"], query, camp)))
        self.assertEqual(sorted(launched), [("a@example.com", "plumbers", "camp"), ("b@example.com", "roofers", "camp")])
        launched.clear()
        jobs.run_scheduler_once(lambda *a: launched.append(a))
        self.assertEqual(launched, [])                          # not due again for 24 hours


class TestUpgradeFromTheSingleUserVersion(AccountsBase):
    def test_old_hub_db_with_tags_is_adopted_and_upgraded_by_the_first_account(self):
        legacy = os.path.join(self.data, "legacy.db")
        db.set_path(legacy)
        db.init_db()
        db.upsert_lead({"business_name": "Old Lead", "phone": "2145550100"}, campaign="my-old-tag")
        with db.get_conn() as c:      # make it look like it was written before campaigns were real
            c.executescript("""DROP TABLE campaigns;
                CREATE TABLE campaigns (name TEXT PRIMARY KEY, criteria TEXT DEFAULT '', created_at TEXT);
                INSERT INTO campaigns(name, criteria) VALUES ('default', 'MY OLD CRITERIA');""")
        db.set_path(None)
        with mock.patch.dict(os.environ, {"ADOPT_LEGACY_DB": "1"}):
            user = accounts.create_user("a@example.com", "horse-battery-1")
        with self.ctx(user):
            self.assertEqual([c["name"] for c in db.list_campaigns()], ["my-old-tag"])
            self.assertEqual(db.list_campaigns()[0]["leads"], 1)
            self.assertEqual(db.get_campaign_criteria("anything"), "MY OLD CRITERIA")


class TestCommandLineTool(AccountsBase):
    def test_reset_password_and_make_admin(self):
        accounts.create_user("a@example.com", "horse-battery-1")
        b = accounts.create_user("b@example.com", "horse-battery-2")
        with mock.patch("manage.getpass.getpass", return_value="a brand new password"):
            manage.main(["manage.py", "reset-password", "b@example.com"])
        self.assertIsNone(accounts.authenticate("b@example.com", "horse-battery-2"))
        new = accounts.authenticate("b@example.com", "a brand new password")
        self.assertNotEqual(new["pv"], b["pv"])                       # old sessions die with the old password
        manage.main(["manage.py", "make-admin", "b@example.com"])
        self.assertTrue(accounts.get_user(b["id"])["is_admin"])
        with self.assertRaises(SystemExit):
            manage.main(["manage.py", "reset-password", "nobody@example.com"])


class TestAuthAPI(ApiBase):
    def test_everything_needs_sign_in(self):
        anon = self.new_client()
        self.assertEqual(anon.get("/api/leads").status_code, 401)
        self.assertEqual(anon.get("/api/campaigns").status_code, 401)
        self.assertEqual(anon.get("/api/export.csv").status_code, 401)
        r = anon.get("/")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/login"))
        self.assertEqual(anon.get("/api/ping").status_code, 200)
        self.assertEqual(anon.get("/static/app.css").status_code, 200)

    def test_signup_login_logout(self):
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.get_json()["email"], "owner@example.com")
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/leads").status_code, 401)
        r = self.client.post("/api/auth/login", json={"email": "OWNER@example.com", "password": "correct horse"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/leads").status_code, 200)

    def test_wrong_password_and_lockout(self):
        c = self.new_client()
        for _ in range(5):
            r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "nope-nope"})
            self.assertEqual(r.status_code, 401)
        r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse"})
        self.assertEqual(r.status_code, 429)                   # even the right password waits out the lockout

    def test_state_changing_calls_need_the_csrf_header(self):
        raw = self.appmod.test_client()                         # no X-Requested-With header
        raw.post("/api/auth/login", json={}, headers={"X-Requested-With": "fetch"})
        r = raw.post("/api/auth/login", json={"email": "x@y.co", "password": "abcdefgh"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/leads", json={"business_name": "X"}, headers={"X-Requested-With": ""})
        self.assertEqual(r.status_code, 403)

    def test_signup_can_be_closed_or_need_a_code(self):
        with mock.patch.dict(os.environ, {"ALLOW_SIGNUP": "0"}):
            r = self.new_client().post("/api/auth/signup", json={"email": "b@example.com", "password": "horse-battery-1"})
            self.assertEqual(r.status_code, 400)
        with mock.patch.dict(os.environ, {"SIGNUP_CODE": "abc"}):
            c = self.new_client()
            self.assertEqual(c.post("/api/auth/signup", json={"email": "b@example.com", "password": "horse-battery-1"}).status_code, 400)
            self.assertEqual(c.post("/api/auth/signup", json={"email": "b@example.com", "password": "horse-battery-1", "code": "abc"}).status_code, 200)

    def test_two_users_cannot_see_each_others_leads_campaigns_or_jobs(self):
        alice = self.client
        bob = self.signup("bob@example.com")
        import io
        alice.post("/api/import", data={"file": (io.BytesIO(b"name,phone\nAlice Plumbing,(214) 555-0100\n"), "a.csv"),
                                        "campaign": "alice-camp"}, content_type="multipart/form-data")
        self.assertEqual(alice.get("/api/leads").get_json()["total"], 1)
        self.assertEqual(bob.get("/api/leads").get_json()["total"], 0)
        self.assertEqual(bob.get("/api/leads/1").status_code, 404)
        self.assertEqual(bob.patch("/api/leads/1", json={"notes": "hijack"}).status_code, 404)
        self.assertEqual(bob.delete("/api/leads/1").get_json()["deleted"], 0)
        self.assertEqual(alice.get("/api/leads/1").get_json()["lead"]["notes"], "")
        self.assertEqual(bob.get("/api/campaigns").get_json()["campaigns"], [])
        self.assertEqual(bob.get("/api/campaigns/alice-camp").status_code, 404)
        self.assertEqual(bob.get("/api/export.csv").get_data(as_text=True).count("Alice"), 0)
        self.assertEqual(bob.get("/api/stats").get_json()["total"], 0)
        bob.post("/api/suppression", json={"value": "x@y.co"})
        self.assertEqual(alice.get("/api/suppression").get_json(), [])

    def test_each_user_connects_their_own_email(self):
        alice = self.client
        bob = self.signup("bob@example.com")
        self.assertTrue(alice.get("/api/config-status").get_json()["smtp"])     # admin: server .env works
        self.assertFalse(bob.get("/api/config-status").get_json()["smtp"])       # Bob: nothing yet
        self.assertEqual(bob.post("/api/send-bulk", json={}).status_code, 400)
        r = bob.put("/api/connections", json={"set": {"SMTP_USER": "bob@gmail.com", "SMTP_PASS": "bobs-app-password"}})
        fields = {f["name"]: f for f in r.get_json()["fields"]}
        self.assertTrue(fields["SMTP_PASS"]["set"])
        self.assertNotIn("bobs-app-password", r.get_data(as_text=True))          # secrets never come back
        self.assertTrue(bob.get("/api/config-status").get_json()["smtp"])
        self.assertEqual(bob.get("/api/connections").get_json()["is_admin"], False)
        r = bob.put("/api/connections", json={"clear": ["SMTP_PASS"]})
        self.assertFalse(bob.get("/api/config-status").get_json()["smtp"])

    def test_admin_area(self):
        bob = self.signup("bob@example.com")
        self.assertEqual(bob.get("/api/admin/users").status_code, 403)
        users = self.client.get("/api/admin/users").get_json()
        self.assertEqual([u["email"] for u in users], ["owner@example.com", "bob@example.com"])
        self.assertEqual(self.client.delete("/api/admin/users/1").status_code, 400)   # not yourself
        self.assertEqual(bob.delete("/api/admin/users/1").status_code, 403)
        self.assertEqual(self.client.delete("/api/admin/users/2").get_json()["deleted"], 1)
        self.assertEqual(bob.get("/api/leads").status_code, 401)                      # Bob's session is dead

    def test_changing_password_signs_out_other_browsers(self):
        other = self.new_client()
        other.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse"})
        self.assertEqual(other.get("/api/leads").status_code, 200)
        r = self.client.post("/api/auth/password", json={"current": "correct horse", "new": "brand new pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/leads").status_code, 200)              # this browser stays in
        self.assertEqual(other.get("/api/leads").status_code, 401)                    # the other one is out
        bad = self.client.post("/api/auth/password", json={"current": "wrong", "new": "another pass"})
        self.assertEqual(bad.status_code, 400)

    def test_jobs_started_over_the_api_run_in_the_users_own_database(self):
        bob = self.signup("bob@example.com")
        import io
        bob.post("/api/import", data={"file": (io.BytesIO(b"name,site\nBob Roofing,bobroof.com\n"), "b.csv"),
                                      "campaign": "bobs"}, content_type="multipart/form-data")
        with mock.patch.object(enrichment, "_get", return_value=""), \
             mock.patch.object(enrichment, "serp_search", return_value=None):
            jid = bob.post("/api/enrich", json={"campaign": "bobs"}).get_json()["job_id"]
        self.assertEqual(bob.get(f"/api/jobs/{jid}").get_json()["status"], "done")
        self.assertEqual(self.client.get("/api/jobs").get_json(), [])
        self.assertEqual(len(bob.get("/api/jobs?campaign=bobs").get_json()), 1)


if __name__ == "__main__":
    unittest.main()
