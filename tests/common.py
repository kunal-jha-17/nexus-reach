"""
Shared test scaffolding: an isolated data folder, a scratch database, and a
signed-in test client. Everything external (LLM, SMTP, IMAP, SerpAPI, web
pages) is mocked, so the suite runs offline.
"""
import json
import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["HUB_JOBS_SYNC"] = "1"
os.environ["HUB_START_THREADS"] = "0"
os.environ["DATA_DIR"] = tempfile.mkdtemp()          # never touch the real ./data folder
os.environ["ADOPT_LEGACY_DB"] = "0"
os.environ.pop("APP_PASSWORD", None)

import config  # noqa: E402
import db  # noqa: E402
import schema  # noqa: E402
import importer  # noqa: E402
import enrichment  # noqa: E402
import filtering  # noqa: E402
import outreach  # noqa: E402
import llm  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.set_path(self._tmp.name)
        db.init_db()
        self._env = mock.patch.dict(os.environ, {
            "SMTP_USER": "me@example.org", "SMTP_PASS": "x", "GROQ_API_KEY": "k",
            "SERPAPI_KEY": "s", "SERPAPI_MONTHLY_LIMIT": "100"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.unlink(self._tmp.name)

    def add(self, **kw):
        kw.setdefault("business_name", "Acme Plumbing")
        lead_id, _ = db.upsert_lead(kw, campaign=kw.pop("campaign", ""), source="test")
        return lead_id


class FakeCtx:
    stopped = False

    def __init__(self):
        self.lines = []

    def log(self, m):
        self.lines.append(m)

    def progress(self, *a):
        pass

    def sleep(self, s):
        return False


class ApiBase(Base):
    """A fresh server data folder, an app, and a signed-in first user (the admin)."""

    def setUp(self):
        super().setUp()
        accounts_mod = __import__("accounts")
        accounts_mod._attempts.clear()             # the sign-in throttle is in memory: start each test clean
        self.data = tempfile.mkdtemp()
        self._e2 = mock.patch.dict(os.environ, {
            "DATA_DIR": self.data, "ADOPT_LEGACY_DB": "0", "HUB_DB": os.path.join(self.data, "none.db"),
            "SIGNUP_CODE": "", "ALLOW_SIGNUP": "1", "SHARE_LLM_WITH_USERS": ""})
        self._e2.start()
        import accounts
        accounts._init_done.clear()
        import app as appmod
        self.appmod = appmod.create_app(start_threads=False)
        self.client = self.new_client()
        r = self.client.post("/api/auth/signup", json={"email": "owner@example.com", "password": "correct horse"})
        assert r.status_code == 200, r.get_json()
        self.accounts = accounts
        db.set_path(accounts.db_path_for(1))        # direct db.* calls in tests hit the owner's database

    def new_client(self):
        c = self.appmod.test_client()
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "fetch"
        return c

    def signup(self, email, password="correct horse"):
        c = self.new_client()
        r = c.post("/api/auth/signup", json={"email": email, "password": password})
        assert r.status_code == 200, r.get_json()
        return c

    def tearDown(self):
        self._e2.stop()
        super().tearDown()


