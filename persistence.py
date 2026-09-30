"""
persistence.py -- keep everyone's data safe on hosts whose disk is wiped.

Free hosting (Render's free plan, for one) throws away the server's files every
time it restarts or goes to sleep. This module keeps an encrypted copy of every
database in outside storage and restores it on start-up:

    <prefix>current/accounts.db          who can sign in
    <prefix>current/users/<id>.db        each person's leads, campaigns, settings ...
    <prefix>backups/<YYYY-MM-DD>/...     one snapshot a day (7 kept by default)

Storage can be any S3-compatible service -- Supabase Storage (free 1 GB), Cloudflare
R2, Backblaze B2, AWS S3 -- or a plain folder (SYNC_FOLDER, e.g. one your computer
already syncs to Dropbox/Google Drive). Copies are gzip-compressed and encrypted with
SECRET_KEY before they leave the server, so the bucket never holds readable data.

How it stays safe
  * Only files that changed are uploaded, about once a minute, and everything is
    flushed when the app is told to shut down.
  * Before overwriting a stored copy it checks nobody else changed it (say a second
    copy of the app on your laptop). If someone did, it does NOT overwrite: it saves
    your version under conflicts/ and pauses that file until you decide.
  * If the storage can't be reached at start-up the app refuses to start rather than
    come up empty and later overwrite good data with nothing.
  * A small request every ~12 hours keeps a free Supabase project from being paused
    for inactivity (it pauses after a week with no activity).
"""
import atexit
import json
import logging
import os
import signal
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import cryptobox

log = logging.getLogger("nexusreach.persistence")

STATE_FILE = ".sync_state.json"


class PersistenceError(RuntimeError):
    pass


# ---------------------------------------------------------------------- stores

class LocalStore:
    """A plain folder. Used in tests, and for 'back up into a folder my computer syncs'."""
    kind = "folder"

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key):
        return self.root / key

    def put(self, key, data):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def get(self, key):
        p = self._p(key)
        return p.read_bytes() if p.exists() else None

    def head(self, key):
        p = self._p(key)
        if not p.exists():
            return None
        st = p.stat()
        return {"key": key, "token": f"{st.st_mtime_ns}-{st.st_size}", "size": st.st_size, "modified": st.st_mtime}

    def list(self, prefix):
        out = []
        for p in self.root.rglob("*"):
            if p.is_file() and not p.name.endswith(".tmp"):
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    out.append(self.head(key))
        return out

    def delete(self, key):
        try:
            self._p(key).unlink()
        except FileNotFoundError:
            pass

    def ping(self):
        return True


class S3Store:
    """Any S3-compatible bucket. For Supabase: the S3 endpoint + S3 access keys from
    Dashboard > Storage > S3 (path-style addressing, region = your project's region)."""
    kind = "s3"

    def __init__(self, endpoint, region, key_id, secret, bucket):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise PersistenceError("Storage sync needs the 'boto3' package: pip install boto3 "
                                   "(it's in requirements-cloud.txt).")
        self.bucket = bucket
        self.c = boto3.client(
            "s3", endpoint_url=endpoint or None, region_name=region or None,
            aws_access_key_id=key_id, aws_secret_access_key=secret,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=10, read_timeout=40))

    @staticmethod
    def _missing(e):
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        return code in ("NoSuchKey", "404", "NotFound")

    def put(self, key, data):
        self.c.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key):
        from botocore.exceptions import ClientError
        try:
            return self.c.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if self._missing(e):
                return None
            raise

    def head(self, key):
        from botocore.exceptions import ClientError
        try:
            r = self.c.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if self._missing(e):
                return None
            raise
        return {"key": key, "token": (r.get("ETag") or str(r["LastModified"])).strip('"'),
                "size": r.get("ContentLength", 0), "modified": r["LastModified"].timestamp()}

    def list(self, prefix):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.c.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                out.append({"key": o["Key"], "token": (o.get("ETag") or str(o["LastModified"])).strip('"'),
                            "size": o["Size"], "modified": o["LastModified"].timestamp()})
            if not r.get("IsTruncated"):
                return out
            token = r.get("NextContinuationToken")

    def delete(self, key):
        self.c.delete_object(Bucket=self.bucket, Key=key)

    def ping(self):
        self.c.head_bucket(Bucket=self.bucket)
        return True


def store_from_env():
    if config._server_env("S3_BUCKET") and config._server_env("S3_ACCESS_KEY_ID"):
        return S3Store(config._server_env("S3_ENDPOINT"), config._server_env("S3_REGION"),
                       config._server_env("S3_ACCESS_KEY_ID"), config._server_env("S3_SECRET_ACCESS_KEY"),
                       config._server_env("S3_BUCKET"))
    if config._server_env("SYNC_FOLDER"):
        return LocalStore(config._server_env("SYNC_FOLDER"))
    return None


# ------------------------------------------------------------------- the syncer

def _is_db_name(rel):
    return rel == "accounts.db" or (rel.startswith("users/") and rel.endswith(".db") and "/" not in rel[6:])


class Sync:
    def __init__(self, store, prefix="nexusreach/"):
        self.store = store
        self.prefix = prefix if (not prefix or prefix.endswith("/")) else prefix + "/"
        self.data = config.data_dir().resolve()
        self.lock = threading.RLock()          # guards `dirty` and `state`
        self.sync_lock = threading.Lock()      # one sync at a time
        self.dirty = set()
        self.conflicts = {}                    # rel -> when/what
        self.last_ok = None
        self.last_error = ""
        self.last_ping = 0.0
        self._stop = threading.Event()
        self._thread = None
        self.interval = max(10, config.env_int("SYNC_INTERVAL_SECONDS", 60))
        self.keep_days = max(1, config.env_int("BACKUP_KEEP_DAYS", 7))
        self.budget_mb = config.env_int("STORAGE_BUDGET_MB", 800)
        self.state = self._load_state()

    # ---- helpers
    def _cur(self, rel=""):
        return f"{self.prefix}current/{rel}"

    def _state_path(self):
        return self.data / STATE_FILE

    def _load_state(self):
        try:
            return json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self._state_path())

    def rel_for(self, path):
        try:
            rel = Path(path).resolve().relative_to(self.data).as_posix()
        except (ValueError, OSError):
            return None
        return rel if _is_db_name(rel) else None

    def mark_dirty(self, path):
        rel = self.rel_for(path)
        if rel and rel not in self.conflicts:
            with self.lock:
                self.dirty.add(rel)

    def _snapshot(self, rel):
        """A consistent copy of a live SQLite file (safe while it's being written to)."""
        src = sqlite3.connect(str(self.data / rel))
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
            return Path(tmp).read_bytes()
        finally:
            src.close()
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _local_files(self):
        out = []
        if (self.data / "accounts.db").exists():
            out.append("accounts.db")
        udir = self.data / "users"
        if udir.exists():
            out += [f"users/{p.name}" for p in sorted(udir.glob("*.db"))]
        return out

    # ---- start-up restore
    def restore_on_boot(self):
        try:
            remote = {o["key"][len(self._cur()):]: o for o in self.store.list(self._cur())}
        except Exception as e:  # noqa: BLE001
            raise PersistenceError(
                f"Can't reach the outside storage ({e}). The app won't start without it, because starting with an "
                "empty database could later overwrite your real data. If you use a free Supabase project, check "
                "it hasn't been paused (Dashboard > restore project), then restart.") from e
        for rel, obj in remote.items():
            if not _is_db_name(rel):
                continue
            local = self.data / rel
            known = self.state.get(rel)
            if local.exists():
                if known and known.get("token") == obj["token"]:
                    continue                                   # this computer is already in step with storage
                self._conflict(rel, "the stored copy differs from the one on this computer", obj)
                continue
            blob = self.store.get(obj["key"])
            if blob is None:
                continue
            try:
                raw = cryptobox.decrypt_blob(blob)
            except cryptobox.KeyError_ as e:
                raise PersistenceError(str(e))
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(raw)
            self.state[rel] = {"token": obj["token"], "at": time.time()}
            log.info("restored %s from storage", rel)
        self._save_state()
        with self.lock:            # anything that exists here but not in storage still needs its first upload
            for rel in self._local_files():
                if rel not in remote and rel not in self.conflicts:
                    self.dirty.add(rel)

    def _conflict(self, rel, why, remote_obj=None):
        """Don't overwrite either side. Keep the stored copy aside for inspection."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        info = {"why": why, "at": stamp, "kept_local_copy": None}
        try:
            if remote_obj is not None:
                blob = self.store.get(remote_obj["key"])
                if blob is not None:
                    dest = self.data / "conflicts" / f"{stamp}_{rel.replace('/', '_')}.stored"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(cryptobox.decrypt_blob(blob))
                    info["stored_copy_saved_to"] = str(dest)
        except Exception as e:  # noqa: BLE001
            info["note"] = f"couldn't save the stored copy: {e}"
        self.conflicts[rel] = info
        with self.lock:
            self.dirty.discard(rel)
        self.last_error = f"CONFLICT on {rel}: {why}. Nothing was overwritten. See manage.py sync-status."
        log.error(self.last_error)

    # ---- uploading
    def _upload(self, rel):
        blob = cryptobox.encrypt_blob(self._snapshot(rel))
        key = self._cur(rel)
        remote = self.store.head(key)
        known = self.state.get(rel, {}).get("token")
        if remote is not None and remote["token"] != known:
            # someone else changed the stored copy since we last synced: keep ours aside, overwrite nothing
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.store.put(f"{self.prefix}conflicts/{stamp}/{rel}", blob)
            self._conflict(rel, "the stored copy was changed by another copy of the app", remote)
            return
        self.store.put(key, blob)
        new = self.store.head(key)
        with self.lock:
            self.state[rel] = {"token": (new or {}).get("token", ""), "at": time.time()}
        self._save_state()

    def sync_once(self, deadline=None):
        """Upload whatever changed. Returns how many files were uploaded."""
        if not self.sync_lock.acquire(blocking=False):
            return 0
        done = 0
        try:
            with self.lock:
                todo = sorted(self.dirty)
            for rel in todo:
                if deadline and time.time() > deadline:
                    break
                if not (self.data / rel).exists():
                    with self.lock:
                        self.dirty.discard(rel)
                    continue
                with self.lock:
                    self.dirty.discard(rel)        # writes after this point re-mark it
                try:
                    self._upload(rel)
                    if rel not in self.conflicts:
                        done += 1
                except Exception as e:  # noqa: BLE001 -- keep it dirty and retry next round
                    with self.lock:
                        self.dirty.add(rel)
                    self.last_error = f"upload of {rel} failed: {e}"
                    log.warning(self.last_error)
                    return done
            self.last_ok = time.time()
            if not self.conflicts:
                self.last_error = ""
            self._housekeeping()
        finally:
            self.sync_lock.release()
        return done

    # ---- daily backup, retention, keep-alive
    def _meta_key(self):
        return f"{self.prefix}meta.json"

    def _read_meta(self):
        try:
            raw = self.store.get(self._meta_key())
            return json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    def _housekeeping(self):
        now = time.time()
        if now - self.last_ping > 12 * 3600:
            try:
                self.store.ping()
                self.last_ping = now
            except Exception as e:  # noqa: BLE001
                self.last_error = f"storage keep-alive failed: {e}"
                return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            clean = not self.dirty
        if clean and not self.conflicts and self._read_meta().get("last_backup_day") != today:
            self.backup_now(today)

    def backup_now(self, day=None):
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        n = 0
        for rel in self._local_files():
            if rel in self.conflicts:
                continue
            self.store.put(f"{self.prefix}backups/{day}/{rel}", cryptobox.encrypt_blob(self._snapshot(rel)))
            n += 1
        self.store.put(self._meta_key(), json.dumps({"last_backup_day": day}).encode())
        self._purge_old_backups()
        return n

    def _backups_by_day(self):
        days = {}
        for o in self.store.list(f"{self.prefix}backups/"):
            parts = o["key"][len(self.prefix) + len("backups/"):].split("/", 1)
            if len(parts) == 2:
                days.setdefault(parts[0], []).append(o)
        return days

    def _purge_old_backups(self):
        days = self._backups_by_day()
        keep = sorted(days)[-self.keep_days:]
        for d in sorted(days):
            if d not in keep:
                for o in days[d]:
                    self.store.delete(o["key"])
        # stay inside the storage allowance: drop the oldest days first (never the newest)
        def used():
            return sum(o["size"] for o in self.store.list(self.prefix)) / 1_000_000
        keep = sorted(self._backups_by_day())
        while len(keep) > 1 and self.budget_mb and used() > self.budget_mb:
            for o in self._backups_by_day().get(keep[0], []):
                self.store.delete(o["key"])
            log.warning("storage budget of %s MB exceeded: dropped backup %s", self.budget_mb, keep[0])
            keep.pop(0)

    # ---- restoring
    def pull_all(self):
        """Overwrite local files with the stored copies (used by manage.py restore)."""
        n = 0
        for o in self.store.list(self._cur()):
            rel = o["key"][len(self._cur()):]
            if _is_db_name(rel):
                (self.data / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.data / rel).write_bytes(cryptobox.decrypt_blob(self.store.get(o["key"])))
                self.state[rel] = {"token": o["token"], "at": time.time()}
                n += 1
        self.conflicts.clear()
        self._save_state()
        return n

    def restore_backup(self, day):
        """Write a dated backup into the data folder (stop the app first)."""
        days = self._backups_by_day()
        if day not in days:
            raise PersistenceError(f"No backup for {day}. Available: {', '.join(sorted(days)) or 'none'}")
        n = 0
        for o in days[day]:
            rel = o["key"][len(f"{self.prefix}backups/{day}/"):]
            if _is_db_name(rel):
                (self.data / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.data / rel).write_bytes(cryptobox.decrypt_blob(self.store.get(o["key"])))
                self.dirty.add(rel)
                n += 1
        self.state = {}      # forget stored versions: the restored files are now the truth
        self._save_state()
        return n

    def push_all(self):
        """Overwrite the stored copies with the local files (used by manage.py push)."""
        self.conflicts.clear()
        for rel in self._local_files():
            self.store.put(self._cur(rel), cryptobox.encrypt_blob(self._snapshot(rel)))
            self.state[rel] = {"token": (self.store.head(self._cur(rel)) or {}).get("token", ""), "at": time.time()}
        self._save_state()
        return len(self._local_files())

    # ---- lifecycle
    def start(self):
        if self._thread:
            return

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.sync_once()
                except Exception:  # noqa: BLE001
                    log.exception("sync loop error")

        self._thread = threading.Thread(target=loop, daemon=True, name="storage-sync")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def flush(self, seconds=20):
        try:
            self.sync_once(deadline=time.time() + seconds)
        except Exception:  # noqa: BLE001
            log.exception("final flush failed")

    def status(self):
        with self.lock:
            dirty = sorted(self.dirty)
        try:
            used = sum(o["size"] for o in self.store.list(self.prefix)) / 1_000_000
        except Exception:  # noqa: BLE001
            used = None
        return {
            "enabled": True, "kind": self.store.kind, "prefix": self.prefix,
            "last_ok": (datetime.fromtimestamp(self.last_ok, timezone.utc).isoformat(timespec="seconds")
                        if self.last_ok else None),
            "last_error": self.last_error, "waiting_to_upload": dirty, "conflicts": self.conflicts,
            "last_backup_day": self._read_meta().get("last_backup_day"),
            "used_mb": round(used, 2) if used is not None else None, "budget_mb": self.budget_mb,
            "interval_seconds": self.interval, "keep_days": self.keep_days,
        }


# ------------------------------------------------------------------ module API

_inst = None


def instance():
    return _inst


def mark_dirty(path):
    """Called after any database write; a no-op unless outside storage is configured."""
    if _inst is not None:
        _inst.mark_dirty(path)


def status():
    return _inst.status() if _inst else {"enabled": False}


def start_if_configured(store=None, prefix=None):
    """Called once at start-up, BEFORE any database is opened."""
    global _inst
    if _inst is not None:
        return _inst
    store = store or store_from_env()
    if store is None:
        return None
    cryptobox.keys()      # fails early, with a clear message, if SECRET_KEY is missing
    s = Sync(store, prefix if prefix is not None else config._server_env("S3_PREFIX", "nexusreach/"))
    s.restore_on_boot()
    s.start()
    _inst = s
    atexit.register(s.flush)
    try:
        previous = signal.getsignal(signal.SIGTERM)

        def on_term(signum, frame):
            s.flush()
            if callable(previous):
                previous(signum, frame)
            else:
                raise SystemExit(0)
        signal.signal(signal.SIGTERM, on_term)
    except ValueError:
        pass                        # not the main thread (e.g. under a test runner)
    return s


def reset_for_tests():
    global _inst
    if _inst:
        _inst.stop()
    _inst = None
