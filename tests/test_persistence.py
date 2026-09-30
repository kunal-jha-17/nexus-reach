"""
Keeping data safe on hosts whose disk is wiped: encrypted copies in outside storage,
restored on start-up. Uses a plain folder as the "outside storage" (same code path as
Supabase/S3), plus a fake S3 client for the S3 adapter.
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import common  # noqa: F401,E402  (isolated environment)

import accounts  # noqa: E402
import cryptobox  # noqa: E402
import db  # noqa: E402
import persistence  # noqa: E402
import userctx  # noqa: E402

KEY_A = "test-secret-key-A-0123456789abcdef"
KEY_B = "test-secret-key-B-0123456789abcdef"


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.remote = os.path.join(self.root, "remote")
        self._env = mock.patch.dict(os.environ, {
            "SECRET_KEY": KEY_A, "SECRET_KEY_OLD": "", "SIGNUP_CODE": "", "ADOPT_LEGACY_DB": "0",
            "ALLOW_SIGNUP": "1", "MAX_USERS": "25", "SYNC_FOLDER": "", "S3_BUCKET": "", "S3_ACCESS_KEY_ID": "",
            "HUB_DB": os.path.join(self.root, "none.db")})
        self._env.start()
        self.disk("disk-1")

    def tearDown(self):
        persistence.reset_for_tests()
        self._env.stop()

    def disk(self, name):
        """Point the app at a fresh, empty data folder -- like a host that just wiped its disk."""
        persistence.reset_for_tests()
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        os.environ["DATA_DIR"] = d
        accounts._init_done.clear()
        cryptobox._file_key.cache_clear()
        return d

    def boot(self, store=None, **attrs):
        """What start-up does: create the syncer, restore from storage, then run."""
        s = persistence.Sync(store or persistence.LocalStore(self.remote), prefix="lh/")
        for k, v in attrs.items():
            setattr(s, k, v)
        s.restore_on_boot()
        persistence._inst = s
        return s

    def make_user(self, email="a@example.com", lead="Acme Plumbing"):
        u = accounts.create_user(email, "horse-battery-1")
        with userctx.use(u, accounts.db_path_for(u["id"])):
            db.upsert_lead({"business_name": lead, "phone": "2145550100"})
        return u


class TestEncryptedCopiesAndRestore(SyncBase):
    def test_a_wiped_disk_is_restored_with_logins_and_leads(self):
        s = self.boot()
        u = self.make_user()
        accounts.set_secrets(u["id"], {"GROQ_API_KEY": "gsk_secret_value"})
        self.assertGreaterEqual(s.sync_once(), 2)
        store = persistence.LocalStore(self.remote)
        keys = {o["key"] for o in store.list("lh/current/")}
        self.assertEqual(keys, {"lh/current/accounts.db", "lh/current/users/1.db"})
        # what's stored is unreadable without the SECRET_KEY
        for k in keys:
            blob = store.get(k)
            self.assertNotIn(b"SQLite format", blob)
            self.assertNotIn(b"Acme Plumbing", blob)
            self.assertNotIn(b"gsk_secret_value", blob)

        # --- the host restarts with an empty disk ---
        self.disk("disk-2")
        self.boot()
        self.assertTrue(os.path.exists(accounts.db_path_for(1)))
        user = accounts.authenticate("a@example.com", "horse-battery-1")
        self.assertIsNotNone(user)                                             # login access survived
        self.assertEqual(user["secrets"]["GROQ_API_KEY"], "gsk_secret_value")   # ...and so did their saved keys
        with userctx.use(user, accounts.db_path_for(user["id"])):
            self.assertEqual([l["business_name"] for l in db.list_leads()[0]], ["Acme Plumbing"])

    def test_only_changed_files_are_uploaded(self):
        s = self.boot()
        u = self.make_user()
        s.sync_once()
        self.assertEqual(s.sync_once(), 0)                                      # nothing changed
        with userctx.use(u, accounts.db_path_for(u["id"])):
            db.upsert_lead({"business_name": "Second", "phone": "2145550111"})
        self.assertEqual(s.sync_once(), 1)                                      # just that user's database
        self.assertEqual(s.sync_once(), 0)

    def test_writes_made_during_an_upload_are_not_lost(self):
        s = self.boot()
        u = self.make_user()
        s.sync_once()
        with userctx.use(u, accounts.db_path_for(u["id"])):
            db.upsert_lead({"business_name": "Late", "phone": "2145550122"})
        self.assertIn("users/1.db", s.dirty)
        s.sync_once()
        self.disk("disk-2")
        self.boot()
        with userctx.use(accounts.get_user(1), accounts.db_path_for(1)):
            self.assertEqual({l["business_name"] for l in db.list_leads()[0]}, {"Acme Plumbing", "Late"})

    def test_wrong_secret_key_at_restore_gives_a_clear_error(self):
        s = self.boot()
        self.make_user()
        s.sync_once()
        self.disk("disk-2")
        with mock.patch.dict(os.environ, {"SECRET_KEY": KEY_B}):
            with self.assertRaises(persistence.PersistenceError) as cm:
                self.boot()
        self.assertIn("SECRET_KEY", str(cm.exception))

    def test_rotated_key_still_restores(self):
        s = self.boot()
        self.make_user()
        s.sync_once()
        self.disk("disk-2")
        with mock.patch.dict(os.environ, {"SECRET_KEY": KEY_B, "SECRET_KEY_OLD": KEY_A}):
            self.boot()
            self.assertIsNotNone(accounts.authenticate("a@example.com", "horse-battery-1"))

    def test_deleted_user_files_are_not_resurrected_by_stale_dirty_flags(self):
        s = self.boot()
        self.make_user()
        u2 = self.make_user("b@example.com", "Beta")
        s.sync_once()
        accounts.delete_user(u2["id"])                                          # removes users/2.db locally
        s.sync_once()                                                           # must not crash on the vanished file
        self.assertIsNone(accounts.authenticate("nobody@example.com", "x"))


class TestSafetyRules(SyncBase):
    def test_start_refuses_when_storage_cannot_be_reached(self):
        class Down(persistence.LocalStore):
            def list(self, prefix):
                raise OSError("connection timed out")
        with self.assertRaises(persistence.PersistenceError) as cm:
            persistence.Sync(Down(self.remote), prefix="lh/").restore_on_boot()
        self.assertIn("paused", str(cm.exception))                              # points at the Supabase pause

    def test_a_second_copy_of_the_app_never_overwrites_the_stored_data(self):
        s = self.boot()
        u = self.make_user()
        s.sync_once()
        # another copy of the app (say, on a laptop) changes the stored database behind our back
        store = persistence.LocalStore(self.remote)
        store.put("lh/current/users/1.db", cryptobox.encrypt_blob(b"the other copy's database"))
        with userctx.use(u, accounts.db_path_for(u["id"])):
            db.upsert_lead({"business_name": "Mine", "phone": "2145550133"})
        s.sync_once()
        self.assertIn("users/1.db", s.conflicts)
        self.assertEqual(cryptobox.decrypt_blob(store.get("lh/current/users/1.db")), b"the other copy's database")
        kept = [o["key"] for o in store.list("lh/conflicts/")]
        self.assertEqual(len(kept), 1)                                          # our version was saved aside, not lost
        self.assertIn("CONFLICT", s.status()["last_error"])
        with userctx.use(u, accounts.db_path_for(u["id"])):                     # further writes don't try again
            db.upsert_lead({"business_name": "Mine 2", "phone": "2145550144"})
        self.assertNotIn("users/1.db", s.dirty)
        self.assertEqual(s.sync_once(), 0)

    def test_existing_local_files_that_differ_from_storage_are_a_conflict_not_an_overwrite(self):
        s = self.boot()
        self.make_user()
        s.sync_once()
        self.disk("disk-2")                                                     # a laptop with its own, different data
        accounts.create_user("laptop@example.com", "horse-battery-1")
        s2 = self.boot()
        self.assertIn("accounts.db", s2.conflicts)
        self.assertTrue(list((Path(os.environ["DATA_DIR"]) / "conflicts").glob("*accounts.db.stored")))
        self.assertIsNotNone(accounts.authenticate("laptop@example.com", "horse-battery-1"))   # local copy left alone


class TestBackupsAndKeepAlive(SyncBase):
    def test_a_backup_is_taken_once_a_day_and_old_ones_are_dropped(self):
        s = self.boot(keep_days=3)
        self.make_user()
        s.sync_once()
        store = persistence.LocalStore(self.remote)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertTrue(store.head(f"lh/backups/{today}/accounts.db"))          # taken by the first sync of the day
        self.assertEqual(s.status()["last_backup_day"], today)
        for i in range(1, 8):                                                   # pretend a week of history
            day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
            s.backup_now(day)
        days = sorted(s._backups_by_day())
        self.assertEqual(len(days), 3)
        self.assertEqual(days[-1], today)                                       # the newest is never the one dropped
        s.backup_now(today)                                                     # (the loop above rewound the marker)
        before = s._read_meta()                                                 # not taken again the same day:
        s.dirty.add("accounts.db")
        s.sync_once()
        self.assertEqual(s._read_meta(), before)

    def test_backups_are_dropped_oldest_first_when_over_the_storage_budget(self):
        s = self.boot(keep_days=30)
        self.make_user()
        s.sync_once()
        for i in range(1, 5):
            s.backup_now((datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d"))
        self.assertGreater(len(s._backups_by_day()), 1)
        s.budget_mb = 0.0001                                                    # an impossibly small allowance
        s._purge_old_backups()
        remaining = sorted(s._backups_by_day())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0], max(remaining))                          # what's left is the newest

    def test_restore_a_dated_backup(self):
        s = self.boot()
        u = self.make_user()
        s.sync_once()
        with userctx.use(u, accounts.db_path_for(u["id"])):
            db.upsert_lead({"business_name": "Added after the backup", "phone": "2145550155"})
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertGreaterEqual(s.restore_backup(day), 2)
        with userctx.use(u, accounts.db_path_for(u["id"])):
            self.assertEqual([l["business_name"] for l in db.list_leads()[0]], ["Acme Plumbing"])
        with self.assertRaises(persistence.PersistenceError):
            s.restore_backup("1999-01-01")

    def test_keep_alive_pings_storage(self):
        s = self.boot()
        s.store.ping = mock.Mock(return_value=True)
        s.last_ping = 0
        s.sync_once()
        s.store.ping.assert_called_once()

    def test_status_is_safe_to_show_an_admin(self):
        s = self.boot()
        self.make_user()
        s.sync_once()
        st = s.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["waiting_to_upload"], [])
        self.assertGreater(st["used_mb"], 0)
        self.assertNotIn(KEY_A, str(st))


class TestStartupWiring(SyncBase):
    def test_start_if_configured_needs_a_secret_key_and_restores(self):
        with mock.patch("persistence.signal.signal"), mock.patch("persistence.atexit.register"):
            with mock.patch.dict(os.environ, {"SYNC_FOLDER": self.remote, "SECRET_KEY": ""}):
                cryptobox._file_key.cache_clear()
                with self.assertRaises(cryptobox.KeyError_):
                    persistence.start_if_configured()
            with mock.patch.dict(os.environ, {"SYNC_FOLDER": self.remote}):
                s = persistence.start_if_configured()
                self.assertIs(persistence.instance(), s)
                self.make_user()
                self.assertIn("accounts.db", s.dirty)                            # hooks in db/accounts mark files dirty
                s.stop()

    def test_nothing_happens_without_storage_configured(self):
        self.assertIsNone(persistence.start_if_configured())
        persistence.mark_dirty("/tmp/whatever.db")                               # harmless no-op
        self.assertEqual(persistence.status(), {"enabled": False})

    def test_databases_outside_the_data_folder_are_ignored(self):
        s = self.boot()
        s.mark_dirty(os.path.join(self.root, "scratch.db"))
        self.assertEqual(s.dirty, set())


# ------------------------------------------------------------------ S3 adapter
class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Just enough of boto3's S3 client to exercise persistence.S3Store."""

    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": types.SimpleNamespace(read=lambda: self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("404")
        return {"ETag": f'"{hash(self.objects[Key]) & 0xffffffff:x}"', "ContentLength": len(self.objects[Key]),
                "LastModified": datetime.now(timezone.utc)}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]                                             # tiny pages to test pagination
        more = start + 2 < len(keys)
        return {"Contents": [{"Key": k, "ETag": '"x"', "Size": len(self.objects[k]),
                              "LastModified": datetime.now(timezone.utc)} for k in page],
                "IsTruncated": more, "NextContinuationToken": str(start + 2)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def head_bucket(self, Bucket):
        return {}


class TestS3Store(SyncBase):
    def setUp(self):
        super().setUp()
        fake_botocore = types.ModuleType("botocore")
        fake_exc = types.ModuleType("botocore.exceptions")
        fake_exc.ClientError = FakeClientError
        self._mods = mock.patch.dict(sys.modules, {"botocore": fake_botocore, "botocore.exceptions": fake_exc})
        self._mods.start()
        self.s3 = FakeS3()
        self.store = persistence.S3Store.__new__(persistence.S3Store)
        self.store.bucket, self.store.c = "nexusreach", self.s3

    def tearDown(self):
        self._mods.stop()
        super().tearDown()

    def test_basic_operations(self):
        self.store.put("a/b.bin", b"hello")
        self.assertEqual(self.store.get("a/b.bin"), b"hello")
        self.assertIsNone(self.store.get("a/missing"))                            # NoSuchKey -> None, not an error
        self.assertIsNone(self.store.head("a/missing"))
        self.assertEqual(self.store.head("a/b.bin")["size"], 5)
        self.assertTrue(self.store.ping())
        self.store.delete("a/b.bin")
        self.assertIsNone(self.store.get("a/b.bin"))

    def test_listing_follows_pagination(self):
        for i in range(5):
            self.store.put(f"p/{i}", b"x")
        self.store.put("other/1", b"x")
        self.assertEqual(sorted(o["key"] for o in self.store.list("p/")), [f"p/{i}" for i in range(5)])

    def test_a_full_sync_and_restore_through_the_s3_adapter(self):
        s = self.boot(store=self.store)
        self.make_user()
        s.sync_once()
        self.assertTrue("lh/current/accounts.db" in self.s3.objects)
        self.disk("disk-2")
        self.boot(store=self.store)
        self.assertIsNotNone(accounts.authenticate("a@example.com", "horse-battery-1"))

    def test_other_errors_are_not_swallowed(self):
        self.s3.get_object = mock.Mock(side_effect=FakeClientError("AccessDenied"))
        with self.assertRaises(FakeClientError):
            self.store.get("x")


class TestManageStorageCommands(SyncBase):
    def run_manage(self, *args, answers=()):
        import io as _io
        import manage
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with mock.patch.dict(os.environ, {"SYNC_FOLDER": self.remote, "S3_PREFIX": "lh/"}), \
             mock.patch("persistence.signal.signal"), mock.patch("persistence.atexit.register"), \
             mock.patch("builtins.input", side_effect=list(answers)), redirect_stdout(buf):
            manage.main(["manage.py", *args])
        return buf.getvalue()

    def seed(self):
        s = self.boot()
        u = self.make_user()
        s.sync_once()
        return s, u

    def test_status_and_backups_list_what_is_stored(self):
        self.seed()
        out = self.run_manage("sync-status")
        self.assertIn("accounts.db", out)
        self.assertIn("users/1.db", out)
        self.assertIn("Last daily backup", out)
        self.assertRegex(self.run_manage("backups"), r"\d{4}-\d{2}-\d{2}")

    def test_storage_commands_say_so_when_nothing_is_configured(self):
        with self.assertRaises(SystemExit) as cm:
            import manage
            with mock.patch.dict(os.environ, {"SYNC_FOLDER": "", "S3_BUCKET": ""}):
                manage.main(["manage.py", "sync-status"])
        self.assertIn("isn't configured", str(cm.exception))

    def test_push_and_pull_ask_first_and_overwrite_the_right_side(self):
        s, u = self.seed()
        with self.assertRaises(SystemExit):                                       # answering anything else cancels
            self.run_manage("push", answers=["no"])
        with userctx.use(u, accounts.db_path_for(u["id"])):
            db.upsert_lead({"business_name": "Only Local", "phone": "2145550166"})
        self.assertIn("Uploaded 2", self.run_manage("push", answers=["yes"]))
        self.disk("disk-2")
        self.assertIn("Downloaded 2", self.run_manage("pull", answers=["yes"]))
        with userctx.use(accounts.get_user(1), accounts.db_path_for(1)):
            self.assertEqual({l["business_name"] for l in db.list_leads()[0]}, {"Acme Plumbing", "Only Local"})

    def test_restore_a_backup_from_the_command_line(self):
        s, u = self.seed()
        with userctx.use(u, accounts.db_path_for(u["id"])):
            db.upsert_lead({"business_name": "After the backup", "phone": "2145550177"})
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIn("Restored", self.run_manage("restore-backup", day, answers=["yes"]))
        with userctx.use(u, accounts.db_path_for(u["id"])):
            self.assertEqual([l["business_name"] for l in db.list_leads()[0]], ["Acme Plumbing"])

    def test_rotate_key_re_encrypts_saved_keys_and_stored_copies(self):
        s, u = self.seed()
        accounts.set_secrets(u["id"], {"SMTP_PASS": "app-pass-1234"})
        s.sync_once()
        persistence.reset_for_tests()
        rotated = {"SECRET_KEY": KEY_B, "SECRET_KEY_OLD": KEY_A}
        with mock.patch.dict(os.environ, rotated):
            out = self.run_manage("rotate-key")
        self.assertIn("Re-encrypted the saved keys of 1 account", out)
        self.assertIn("Re-uploaded", out)
        # the old key can now be dropped: a wiped host restores fine with only the new one
        self.disk("disk-2")
        with mock.patch.dict(os.environ, {"SECRET_KEY": KEY_B, "SECRET_KEY_OLD": ""}):
            self.boot()
            self.assertEqual(accounts.get_user(1)["secrets"]["SMTP_PASS"], "app-pass-1234")

    def test_rotate_key_refuses_without_an_old_key(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_manage("rotate-key")
        self.assertIn("SECRET_KEY_OLD", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
