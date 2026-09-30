"""Security hardening: SSRF guard, key rotation, first-run setup code, per-account limits."""
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base  # noqa: E402

import accounts  # noqa: E402
import cryptobox  # noqa: E402
import db  # noqa: E402
import importer  # noqa: E402
import netguard  # noqa: E402
import userctx  # noqa: E402


def fake_dns(mapping):
    def getaddrinfo(host, port, *a, **k):
        if host not in mapping:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in mapping[host]]
    return mock.patch.object(netguard.socket, "getaddrinfo", side_effect=getaddrinfo)


class FakeResponse:
    def __init__(self, status=200, body=b"<html>hi</html>", location=None, encoding="utf-8"):
        self.status_code, self._body, self.encoding = status, body, encoding
        self.headers = {"Location": location} if location else {}
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def close(self):
        self.closed = True


class TestNetguard(unittest.TestCase):
    def test_only_public_addresses_are_allowed(self):
        bad = ["127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.5", "169.254.169.254", "100.64.0.1",
               "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "::ffff:169.254.169.254"]
        for ip in bad:
            with fake_dns({"evil.example": [ip]}):
                with self.assertRaises(netguard.Blocked, msg=ip):
                    netguard.resolve_public("evil.example", 443)
        with fake_dns({"ok.example": ["93.184.216.34"]}):
            self.assertEqual(netguard.resolve_public("ok.example", 443), ["93.184.216.34"])

    def test_one_private_answer_among_public_ones_is_still_refused(self):
        with fake_dns({"mixed.example": ["93.184.216.34", "10.0.0.5"]}):
            with self.assertRaises(netguard.Blocked):
                netguard.resolve_public("mixed.example", 443)

    def test_unknown_host_and_empty_host(self):
        with fake_dns({}):
            with self.assertRaises(netguard.Blocked):
                netguard.resolve_public("nope.example", 443)
        with self.assertRaises(netguard.Blocked):
            netguard.resolve_public("", 443)

    def test_private_targets_can_be_allowed_for_self_hosters(self):
        with mock.patch.dict(os.environ, {"ALLOW_PRIVATE_HOSTS": "1"}):
            self.assertEqual(netguard.resolve_public("localhost", 25), ["localhost"])

    def test_url_rules(self):
        with fake_dns({"a.example": ["93.184.216.34"]}):
            netguard.check_url("https://a.example/page")
            netguard.check_url("http://a.example:8080/x")
            for url in ("ftp://a.example/", "file:///etc/passwd", "https://a.example:22/", "gopher://a.example"):
                with self.assertRaises(netguard.Blocked, msg=url):
                    netguard.check_url(url)

    def test_mail_port_rules(self):
        with fake_dns({"smtp.example": ["93.184.216.34"]}):
            netguard.check_mail_host("smtp.example", 587)
            with self.assertRaises(netguard.Blocked):
                netguard.check_mail_host("smtp.example", 6379)     # not a mail port
        with fake_dns({"smtp.example": ["10.0.0.1"]}):
            with self.assertRaises(netguard.Blocked):
                netguard.check_mail_host("smtp.example", 587)

    def test_redirect_to_the_cloud_metadata_service_is_blocked_on_the_second_hop(self):
        responses = [FakeResponse(302, location="http://169.254.169.254/latest/meta-data/"), FakeResponse(200)]
        with fake_dns({"a.example": ["93.184.216.34"], "169.254.169.254": ["169.254.169.254"]}), \
             mock.patch.object(netguard.requests, "get", side_effect=lambda *a, **k: responses.pop(0)) as get:
            with self.assertRaises(netguard.Blocked):
                netguard.safe_get("https://a.example/")
        self.assertEqual(get.call_count, 1)                          # never even connected to the internal address

    def test_redirects_are_followed_when_every_hop_is_public(self):
        responses = [FakeResponse(301, location="/new"), FakeResponse(200, body=b"final page")]
        with fake_dns({"a.example": ["93.184.216.34"]}), \
             mock.patch.object(netguard.requests, "get", side_effect=lambda *a, **k: responses.pop(0)):
            self.assertEqual(netguard.safe_get("https://a.example/old"), "final page")

    def test_redirect_loops_stop(self):
        with fake_dns({"a.example": ["93.184.216.34"]}), \
             mock.patch.object(netguard.requests, "get", side_effect=lambda *a, **k: FakeResponse(302, location="/again")):
            with self.assertRaises(netguard.Blocked):
                netguard.safe_get("https://a.example/")

    def test_downloads_are_size_capped_and_errors_return_nothing(self):
        with fake_dns({"a.example": ["93.184.216.34"]}):
            with mock.patch.object(netguard.requests, "get", return_value=FakeResponse(200, body=b"x" * 100_000)):
                self.assertLessEqual(len(netguard.safe_get("https://a.example/", max_bytes=20_000)), 20_000)
            with mock.patch.object(netguard.requests, "get", return_value=FakeResponse(404)):
                self.assertEqual(netguard.safe_get("https://a.example/"), "")


class KeyEnv(unittest.TestCase):
    """SECRET_KEY / SECRET_KEY_OLD handling in an isolated data folder."""

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {
            "DATA_DIR": self.data, "ADOPT_LEGACY_DB": "0", "HUB_DB": os.path.join(self.data, "none.db"),
            "SECRET_KEY": "", "SECRET_KEY_OLD": "", "SYNC_FOLDER": "", "S3_BUCKET": "", "S3_ACCESS_KEY_ID": "",
            "SIGNUP_CODE": "", "ALLOW_SIGNUP": "1", "MAX_USERS": "25", "TRUST_PROXY": ""})
        self._env.start()
        accounts._init_done.clear()
        cryptobox._file_key.cache_clear()

    def tearDown(self):
        self._env.stop()


class TestCryptobox(KeyEnv):
    def test_roundtrip_and_rotation(self):
        with mock.patch.dict(os.environ, {"SECRET_KEY": "old-key-aaaaaaaaaaaaaaaa"}):
            token = cryptobox.encrypt_text("hunter2")
            blob = cryptobox.encrypt_blob(b"database bytes")
        with mock.patch.dict(os.environ, {"SECRET_KEY": "new-key-bbbbbbbbbbbbbbbb"}):
            self.assertIsNone(cryptobox.decrypt_text(token))                 # new key alone can't read old data
            with self.assertRaises(cryptobox.KeyError_):
                cryptobox.decrypt_blob(blob)
        with mock.patch.dict(os.environ, {"SECRET_KEY": "new-key-bbbbbbbbbbbbbbbb",
                                          "SECRET_KEY_OLD": "old-key-aaaaaaaaaaaaaaaa"}):
            self.assertEqual(cryptobox.decrypt_text(token), "hunter2")       # ...but can while the old one is listed
            self.assertEqual(cryptobox.decrypt_blob(blob), b"database bytes")
            self.assertFalse(cryptobox.is_primary(token))
            fresh = cryptobox.encrypt_text("hunter2")
            self.assertTrue(cryptobox.is_primary(fresh))
            self.assertEqual(cryptobox.secret_key(), b"new-key-bbbbbbbbbbbbbbbb")
            self.assertEqual(cryptobox.fallback_keys(), [b"old-key-aaaaaaaaaaaaaaaa"])

    def test_key_file_is_created_locally_but_refused_when_data_is_synced_elsewhere(self):
        k1 = cryptobox.secret_key()
        self.assertEqual(cryptobox.secret_key(), k1)                         # stable across calls
        self.assertTrue(os.path.exists(os.path.join(self.data, "secret_key")))
        with mock.patch.dict(os.environ, {"SYNC_FOLDER": self.data + "-remote"}):
            with self.assertRaises(cryptobox.KeyError_) as cm:
                cryptobox.keys()
            self.assertIn("SECRET_KEY is not set", str(cm.exception))

    def test_saved_credentials_survive_a_key_rotation(self):
        with mock.patch.dict(os.environ, {"SECRET_KEY": "old-key-aaaaaaaaaaaaaaaa"}):
            u = accounts.create_user("a@example.com", "horse-battery-1")
            accounts.set_secrets(u["id"], {"SMTP_USER": "me@gmail.com", "SMTP_PASS": "app-password"})
        rotated = {"SECRET_KEY": "new-key-bbbbbbbbbbbbbbbb", "SECRET_KEY_OLD": "old-key-aaaaaaaaaaaaaaaa"}
        with mock.patch.dict(os.environ, rotated):
            self.assertEqual(accounts.get_user(u["id"])["secrets"]["SMTP_PASS"], "app-password")
            self.assertEqual(accounts.reencrypt_all(), 1)
            self.assertEqual(accounts.reencrypt_all(), 0)                    # nothing left to do
        with mock.patch.dict(os.environ, {"SECRET_KEY": "new-key-bbbbbbbbbbbbbbbb"}):   # old key can now be dropped
            self.assertEqual(accounts.get_user(u["id"])["secrets"]["SMTP_PASS"], "app-password")

    def test_wrong_key_is_reported_and_the_old_blob_is_kept_when_new_keys_are_saved(self):
        with mock.patch.dict(os.environ, {"SECRET_KEY": "old-key-aaaaaaaaaaaaaaaa"}):
            u = accounts.create_user("a@example.com", "horse-battery-1")
            accounts.set_secrets(u["id"], {"GROQ_API_KEY": "gsk_precious"})
        with mock.patch.dict(os.environ, {"SECRET_KEY": "new-key-bbbbbbbbbbbbbbbb"}):
            user = accounts.get_user(u["id"])
            self.assertEqual(user["secrets"], {})
            self.assertTrue(user["secrets_unreadable"])                       # the UI can tell them why
            accounts.set_secrets(u["id"], {"SMTP_USER": "me@gmail.com"})      # they start re-entering keys
            self.assertFalse(accounts.get_user(u["id"])["secrets_unreadable"])
            with accounts._conn() as c:
                backup = c.execute("SELECT secrets_backup FROM users").fetchone()[0]
            self.assertTrue(backup)                                            # ...and nothing was destroyed
        with mock.patch.dict(os.environ, {"SECRET_KEY": "old-key-aaaaaaaaaaaaaaaa"}):
            self.assertEqual(cryptobox.decrypt_text(backup) and True, True)   # restoring the old key can read it


class TestDisposableEmail(KeyEnv):
    def test_known_disposable_domains_are_rejected(self):
        for bad in ("a@mailinator.com", "A@Mailinator.com", "x@10minutemail.com", "x@yopmail.com",
                   "x@guerrillamail.com", "x@tempmail.com", "x@trashmail.com"):
            with self.assertRaises(ValueError, msg=bad) as cm:
                accounts.create_user(bad, "horse-battery-1")
            self.assertIn("temporary/disposable", str(cm.exception))
        self.assertEqual(accounts.count_users(), 0)

    def test_ordinary_domains_are_fine(self):
        for ok in ("kunal@gmail.com", "owner@neurospark.io", "a@company-name.com"):
            accounts.create_user(ok, "horse-battery-" + ok[:3])
        self.assertEqual(accounts.count_users(), 3)

    def test_malformed_email_is_still_caught_first(self):
        with self.assertRaises(ValueError) as cm:
            accounts.create_user("not-an-email", "horse-battery-1")
        self.assertIn("valid email", str(cm.exception))


class TestSetupCodeAndLimits(KeyEnv):
    def test_first_account_from_another_computer_needs_the_setup_code(self):
        self.assertFalse(accounts.signup_policy(local=True)["needs_code"])
        pol = accounts.signup_policy(local=False)
        self.assertTrue(pol["needs_code"])
        self.assertEqual(pol["code_kind"], "setup")
        with self.assertRaises(ValueError) as cm:
            accounts.create_user("a@example.com", "horse-battery-1", local=False)
        self.assertIn("setup code", str(cm.exception))
        with self.assertRaises(ValueError):
            accounts.create_user("a@example.com", "horse-battery-1", code="guess", local=False)
        u = accounts.create_user("a@example.com", "horse-battery-1", code=accounts.setup_code(), local=False)
        self.assertTrue(u["is_admin"])

    def test_local_first_run_needs_no_code_and_later_users_dont_need_the_setup_code(self):
        accounts.create_user("a@example.com", "horse-battery-1", local=True)
        self.assertFalse(accounts.signup_policy(local=False)["needs_code"])
        accounts.create_user("b@example.com", "horse-battery-2", local=False)

    def test_an_invite_code_beats_the_setup_code(self):
        with mock.patch.dict(os.environ, {"SIGNUP_CODE": "invite-me"}):
            self.assertEqual(accounts.signup_policy(local=True)["code_kind"], "invite")
            with self.assertRaises(ValueError):
                accounts.create_user("a@example.com", "horse-battery-1", code=accounts.setup_code(), local=False)
            accounts.create_user("a@example.com", "horse-battery-1", code="invite-me", local=False)

    def test_what_counts_as_a_local_request(self):
        self.assertTrue(accounts.is_local_request("127.0.0.1"))
        self.assertTrue(accounts.is_local_request("::1"))
        self.assertFalse(accounts.is_local_request("203.0.113.9"))
        self.assertFalse(accounts.is_local_request("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}))   # via a proxy
        with mock.patch.dict(os.environ, {"TRUST_PROXY": "1"}):
            self.assertFalse(accounts.is_local_request("127.0.0.1"))

    def test_user_cap(self):
        with mock.patch.dict(os.environ, {"MAX_USERS": "2"}):
            accounts.create_user("a@example.com", "horse-battery-1")
            accounts.create_user("b@example.com", "horse-battery-2")
            pol = accounts.signup_policy()
            self.assertFalse(pol["allowed"])
            self.assertTrue(pol["full"])
            with self.assertRaises(ValueError) as cm:
                accounts.create_user("c@example.com", "horse-battery-3")
            self.assertIn("user limit", str(cm.exception))

    def test_connection_fields_are_validated_before_they_are_stored(self):
        u = accounts.create_user("a@example.com", "horse-battery-1")
        for name, value in (("SMTP_HOST", "not a host!"), ("SMTP_HOST", "http://evil"), ("SMTP_PORT", "22"),
                            ("SMTP_PORT", "6379"), ("IMAP_HOST", "a b"), ("SERPAPI_MONTHLY_LIMIT", "lots")):
            with self.assertRaises(ValueError, msg=f"{name}={value}"):
                accounts.set_secrets(u["id"], {name: value})
        accounts.set_secrets(u["id"], {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SERPAPI_MONTHLY_LIMIT": "100"})
        self.assertEqual(accounts.get_user(u["id"])["secrets"]["SMTP_PORT"], "587")


class TestAccountLimits(Base):
    def test_lead_cap_stops_inserts_but_still_lets_existing_leads_merge(self):
        with mock.patch.dict(os.environ, {"MAX_LEADS_PER_USER": "2"}):
            db.upsert_lead({"business_name": "A", "phone": "2145550100"})
            db.upsert_lead({"business_name": "B", "phone": "2145550101"})
            with self.assertRaises(db.LimitError):
                db.upsert_lead({"business_name": "C", "phone": "2145550102"})
            _, is_new = db.upsert_lead({"business_name": "A", "phone": "2145550100", "email": "a@x.com"})
            self.assertFalse(is_new)                                          # merging into an existing lead is fine

    def test_import_reports_the_limit_instead_of_failing(self):
        csv = b"name,phone\n" + b"".join(f"L{i},214555{i:04d}\n".encode() for i in range(5))
        with mock.patch.dict(os.environ, {"MAX_LEADS_PER_USER": "3"}):
            s = importer.import_csv_bytes(csv)
        self.assertEqual(s["inserted"], 3)
        self.assertIn("limit of 3 leads", s["stopped_at_limit"])

    def test_rows_saved_before_the_limit_was_hit_are_really_committed(self):
        """A batch stopped part-way by the cap must still keep what it already inserted."""
        csv = b"name,phone\n" + b"".join(f"L{i},214555{i:04d}\n".encode() for i in range(50))
        with mock.patch.dict(os.environ, {"MAX_LEADS_PER_USER": "7"}):
            s = importer.import_csv_bytes(csv)
            self.assertEqual(s["inserted"], 7)
            self.assertEqual(db.stats()["total"], 7)                  # in the database, not just in the summary
            summary = importer.import_leads([{"business_name": f"S{i}", "phone": f"21455599{i:02d}"} for i in range(30)])
            self.assertEqual(db.stats()["total"], 7)                  # already full: nothing more, no crash
            self.assertIn("stopped_at_limit", summary)

    def test_bulk_edits_and_big_imports_are_batched(self):
        csv = b"name,phone,email_1\n" + b"".join(f"L{i},2145{i:06d},l{i}@x.com\n".encode() for i in range(450))
        s = importer.import_csv_bytes(csv, campaign="c")
        self.assertEqual((s["inserted"], db.stats()["total"]), (450, 450))
        ids = db.select_ids({"campaign": "c"})
        self.assertEqual(db.bulk_set(ids, "stage", "contacted"), 450)
        self.assertEqual(db.stats()["by_stage"], {"contacted": 450})
        self.assertEqual(db.bulk_set(ids[:10], "do_not_contact", True), 10)
        with self.assertRaises(ValueError):
            db.bulk_set(ids, "stage", "nonsense")
        with self.assertRaises(ValueError):
            db.bulk_set(ids, "business_name", "x")

    def test_oversized_import_is_rejected_up_front(self):
        with mock.patch.object(importer, "MAX_IMPORT_ROWS", 2):
            with self.assertRaises(ValueError):
                importer.import_csv_bytes(b"name,phone\nA,2145550100\nB,2145550101\nC,2145550102\n")


if __name__ == "__main__":
    unittest.main()
