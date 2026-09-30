"""
accounts.py -- who can sign in, and where each person's data lives.

Layout on disk (folder set by DATA_DIR, default ./data):

    data/accounts.db      one row per user: email, password hash, encrypted credentials
    data/users/1.db       user 1's leads, campaigns, jobs, settings ... (their OWN database)
    data/users/2.db       user 2's, and so on
    data/secret_key       signs login cookies and encrypts stored credentials

Each user's database is a completely separate file, so one person's leads can
never show up in another's, and a user can be exported, backed up or deleted by
copying or removing one file. The first account created becomes the admin and
adopts any existing single-user hub.db, so nothing you built before is lost.
"""
import hashlib
import json
import os
import re
import secrets as pysecrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

import config
import cryptobox
import db
import persistence
import schema
import userctx

# pbkdf2 works on every Python build (scrypt is missing on some macOS/LibreSSL builds).
# 600,000 rounds is the current OWASP recommendation; on a very slow free-tier CPU you can
# lower it with PW_ITERATIONS (existing passwords keep working -- each hash records its own count).
PW_ITERATIONS = max(10_000, config.env_int("PW_ITERATIONS", 600_000))
PW_METHOD = f"pbkdf2:sha256:{PW_ITERATIONS}"
MIN_PASSWORD = 8

# Well-known disposable/temp-mail domains. This is a curated list of common ones, not a live,
# exhaustive database (those run to tens of thousands of entries and change constantly) -- it
# catches the popular services people actually use to dodge a sign-up, not every domain that has
# ever existed. Add to it freely; it's just a set.
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "mailinator.net", "mailinator.org", "sogetthis.com", "spamherelots.com",
    "tempmail.com", "temp-mail.org", "temp-mail.io", "tempmailo.com", "tempinbox.com",
    "10minutemail.com", "10minutemail.net", "10minutemail.co.za", "20minutemail.com",
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz",
    "guerrillamailblock.com", "sharklasers.com", "grr.la", "pokemail.net", "spam4.me",
    "yopmail.com", "yopmail.net", "yopmail.fr", "cool.fr.nf", "jetable.fr.nf",
    "dispostable.com", "throwawaymail.com", "throwam.com", "getnada.com", "getairmail.com",
    "fakeinbox.com", "fakemailgenerator.com", "maildrop.cc", "mailnesia.com", "mailcatch.com",
    "mintemail.com", "mytemp.email", "moakt.com", "moakt.cc", "emailondeck.com", "mohmal.com",
    "trashmail.com", "trashmail.net", "trash-mail.com", "trashmail.me", "discard.email",
    "discardmail.com", "spamgourmet.com", "spambox.us", "mail-temporaire.fr", "burnermail.io",
    "tempr.email", "temporary-mail.net", "emailfake.com", "harakirimail.com", "inboxkitten.com",
    "mailsac.com", "tmail.com", "tmpmail.org", "tmpmail.net", "tmpeml.com", "luxusmail.org",
    "correotemporal.org", "einrot.com", "wegwerfmail.de", "wegwerfmail.net", "wegwerfmail.org",
    "anonaddy.com", "33mail.com", "mailslurp.com", "dropmail.me", "crazymailing.com",
    "fakemail.net", "spoofmail.de", "mytrashmail.com", "tempsky.com", "tempmailaddress.com",
}


def is_disposable_email(email):
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    return domain in DISPOSABLE_EMAIL_DOMAINS

# The passwords attackers try first. (Not exhaustive -- it just stops the obvious ones.)
_COMMON_PASSWORDS = {
    "password", "password1", "password12", "password123", "passw0rd", "p@ssw0rd", "12345678", "123456789",
    "1234567890", "qwertyui", "qwerty123", "qwertyuiop", "iloveyou", "letmein1", "letmein123", "welcome1",
    "welcome123", "admin123", "administrator", "abc12345", "abcd1234", "11111111", "00000000", "1q2w3e4r",
    "1qaz2wsx", "qazwsxedc", "zaq12wsx", "asdfghjk", "football1", "baseball1", "sunshine1", "princess1",
    "monkey123", "dragon123", "trustno1", "changeme", "changeme123", "leadhub123", "nexusreach123", "neurospark",
}


class CodeError(ValueError):
    """A wrong setup / invite code -- the one sign-up mistake that counts as a suspicious attempt."""


def check_password(pw, email=""):
    """Raises ValueError with a helpful message if the password is too weak."""
    pw = pw or ""
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"Use a password of at least {MIN_PASSWORD} characters.")
    low = pw.lower()
    local = (email or "").split("@")[0].lower()
    if low in _COMMON_PASSWORDS or len(set(low)) <= 2:
        raise ValueError("That password is on the list of very common ones. Try a few unrelated words instead "
                         "(e.g. 'purple-tractor-window').")
    if len(local) >= 4 and local in low:
        raise ValueError("Your password shouldn't contain your email name.")
_DUMMY_HASH = generate_password_hash("not-a-real-password", method=PW_METHOD)   # same cost as a real check

# (name, is_secret) -- the fields shown in Settings > Connections
CONNECTION_FIELDS = [
    ("LLM_PROVIDER", False), ("GROQ_API_KEY", True), ("GROQ_MODEL", False),
    ("OPENAI_API_KEY", True), ("OPENAI_MODEL", False), ("GEMINI_API_KEY", True), ("GEMINI_MODEL", False),
    ("SMTP_USER", False), ("SMTP_PASS", True), ("SMTP_HOST", False), ("SMTP_PORT", False),
    ("SMTP_FROM_NAME", False), ("IMAP_USER", False), ("IMAP_PASS", True), ("IMAP_HOST", False),
    ("SERPAPI_KEY", True), ("SERPAPI_MONTHLY_LIMIT", False),
    ("IG_USERNAME", False), ("IG_PASSWORD", True),
]
_SECRET_NAMES = {n for n, s in CONNECTION_FIELDS if s}
_EDITABLE = {n for n, _ in CONNECTION_FIELDS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    secrets_enc TEXT DEFAULT '',
    secrets_backup TEXT DEFAULT '',      -- an older blob we couldn't read (wrong SECRET_KEY), kept just in case
    created_at TEXT,
    last_login_at TEXT
);
"""

_init_done = set()
_lock = threading.Lock()


# ---------------------------------------------------------------- locations

def data_dir():
    return config.data_dir()


def db_path_for(user_id):
    return str(data_dir() / "users" / f"{int(user_id)}.db")


@contextmanager
def _conn():
    ensure()
    path = str(data_dir() / "accounts.db")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    before = conn.total_changes
    try:
        yield conn
        conn.commit()
        if conn.total_changes != before:
            persistence.mark_dirty(path)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure():
    key = str(data_dir() / "accounts.db")
    if key in _init_done:
        return
    with _lock:
        c = sqlite3.connect(key, timeout=30)
        try:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
            if "secrets_backup" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN secrets_backup TEXT DEFAULT ''")
            c.commit()
        finally:
            c.close()
        _init_done.add(key)


# ------------------------------------------------------------ secret handling

def secret_key():
    return cryptobox.secret_key()


def _load_secrets(row):
    """A user's saved credentials as a dict. {} if there are none, or if they were
    encrypted with a key we no longer have (see secrets_unreadable)."""
    if not row["secrets_enc"]:
        return {}
    text = cryptobox.decrypt_text(row["secrets_enc"])
    if text is None:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _unreadable(row):
    return bool(row["secrets_enc"]) and cryptobox.decrypt_text(row["secrets_enc"]) is None


# ------------------------------------------------------------------- users

def _ctx_user(row):
    return {"id": row["id"], "email": row["email"], "name": row["name"] or "",
            "is_admin": bool(row["is_admin"]), "secrets": _load_secrets(row),
            "secrets_unreadable": _unreadable(row),
            # changes when the password changes, so old sessions stop working
            "pv": hashlib.sha256(row["password_hash"].encode()).hexdigest()[:12]}


def count_users():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# One-time code that protects "create the first (admin) account" on a server that's reachable from
# other computers. It's printed in the server log at startup; it changes every restart.
_setup_code = pysecrets.token_urlsafe(9)


def setup_code():
    return _setup_code


def is_local_request(remote_addr, headers=None):
    """True only for someone using the app on the same machine, directly (not through a proxy)."""
    headers = headers or {}
    if headers.get("X-Forwarded-For") or headers.get("Forwarded"):
        return False
    if config._server_env("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        return False
    return remote_addr in ("127.0.0.1", "::1")


def _max_users():
    try:
        return int(config._server_env("MAX_USERS", "25"))
    except ValueError:
        return 25


def signup_policy(local=True):
    n = count_users()
    env_code = config._server_env("SIGNUP_CODE")
    closed = config._server_env("ALLOW_SIGNUP", "1").strip().lower() in ("0", "false", "no", "off")
    cap = _max_users()
    full = n > 0 and cap > 0 and n >= cap
    needs_code = bool(env_code) or (n == 0 and not local)
    return {
        "needs_first_user": n == 0,
        "allowed": n == 0 or not (closed or full),
        "full": full and not closed,
        "needs_code": needs_code,
        "code_kind": ("setup" if n == 0 and not env_code else "invite") if needs_code else "",
    }


def announce_setup_code():
    """Print the first-run code where the host's log viewer will show it."""
    if count_users() == 0:
        print(f"\n[Nexus Reach] FIRST-TIME SETUP: open the site and create your admin account.\n"
              f"[Nexus Reach] Setup code (needed only when signing up from another computer): {_setup_code}\n", flush=True)


def _adopt_legacy(target_path):
    """First account only: take over an existing single-user hub.db."""
    legacy = Path(config.db_path())
    if config._server_env("ADOPT_LEGACY_DB", "1") in ("0", "false", "no"):
        return False
    try:
        if not legacy.exists() or legacy.stat().st_size == 0 or legacy.resolve() == Path(target_path).resolve():
            return False
        src = sqlite3.connect(str(legacy))
        dst = sqlite3.connect(target_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return True
    except (sqlite3.DatabaseError, OSError):
        try:
            os.unlink(target_path)
        except OSError:
            pass
        return False


def create_user(email, password, name="", code="", local=True):
    policy = signup_policy(local)
    if not policy["allowed"]:
        raise ValueError("This server has reached its user limit." if policy["full"] else "Sign-ups are closed on this server.")
    if policy["needs_code"]:
        want = config._server_env("SIGNUP_CODE") or _setup_code
        if not pysecrets.compare_digest(code or "", want):
            raise CodeError("That code isn't right." if policy["code_kind"] == "invite"
                            else "That setup code isn't right. It's printed in the server's log.")
    email = schema.norm_email(email)
    if not email:
        raise ValueError("Enter a valid email address.")
    if is_disposable_email(email):
        raise ValueError("Please use a permanent email address -- temporary/disposable email "
                         "addresses aren't accepted.")
    check_password(password, email)
    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise ValueError("An account with that email already exists.")
        first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        uid = c.execute(
            "INSERT INTO users(email, name, password_hash, is_admin, created_at, last_login_at) VALUES(?,?,?,?,?,?)",
            (email, (name or "").strip()[:80], generate_password_hash(password, method=PW_METHOD),
             1 if first else 0, schema.now_iso(), schema.now_iso()),
        ).lastrowid
    path = db_path_for(uid)
    if first:
        _adopt_legacy(path)
    user = get_user(uid)
    with userctx.use(user, path):
        db.init_db()
    return user


def _find(email):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE email = ?", (schema.norm_email(email) or "",)).fetchone()


def authenticate(email, password):
    row = _find(email)
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password or "")
    if not (row and ok):
        return None
    with _conn() as c:
        c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (schema.now_iso(), row["id"]))
    return _ctx_user(row)


def get_user(user_id):
    if not user_id:
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _ctx_user(row) if row else None


def all_users():
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_ctx_user(r) for r in rows]


def update_profile(user_id, name):
    with _conn() as c:
        c.execute("UPDATE users SET name = ? WHERE id = ?", ((name or "").strip()[:80], user_id))


def change_password(user_id, current, new):
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], current or ""):
            raise ValueError("Your current password isn't right.")
        check_password(new, row["email"])
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                  (generate_password_hash(new, method=PW_METHOD), user_id))
    return get_user(user_id)


def admin_list_users():
    out = []
    for u in all_users():
        leads = None
        try:
            c = sqlite3.connect(db_path_for(u["id"]))
            leads = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            c.close()
        except sqlite3.DatabaseError:
            pass
        with _conn() as c2:
            r = c2.execute("SELECT created_at, last_login_at FROM users WHERE id = ?", (u["id"],)).fetchone()
        out.append({"id": u["id"], "email": u["email"], "name": u["name"], "is_admin": u["is_admin"],
                    "leads": leads, "created_at": r["created_at"], "last_login_at": r["last_login_at"]})
    return out


def delete_user(user_id):
    """Remove an account and its database file. Irreversible."""
    with _conn() as c:
        n = c.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount
    base = db_path_for(user_id)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(base + suffix)
        except OSError:
            pass
    return n


# ------------------------------------------------------------- credentials

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
_MAIL_PORTS = {"25", "465", "587", "2525", "143", "993"}


def _validate_secret(name, value):
    if name in ("SMTP_HOST", "IMAP_HOST") and not _HOST_RE.match(value):
        raise ValueError(f"'{value}' doesn't look like a mail server name (for example smtp.gmail.com).")
    if name == "SMTP_PORT" and value not in _MAIL_PORTS:
        raise ValueError("Mail server port must be one of: " + ", ".join(sorted(_MAIL_PORTS, key=int)) + ".")
    if name == "SERPAPI_MONTHLY_LIMIT" and not value.isdigit():
        raise ValueError("Searches per month must be a number.")


def set_secrets(user_id, updates=None, clear=None):
    """Store a user's own keys/passwords (encrypted). Blank values are ignored;
    names in `clear` are removed."""
    updates = {k: str(v).strip() for k, v in (updates or {}).items() if k in _EDITABLE and str(v).strip()}
    for k, v in updates.items():
        _validate_secret(k, v)
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise ValueError("No such user")
        cur = _load_secrets(row)
        # If the saved blob can't be read (SECRET_KEY changed), keep it aside instead of destroying it.
        backup = row["secrets_enc"] if _unreadable(row) else row["secrets_backup"]
        cur.update(updates)
        for k in (clear or []):
            cur.pop(k, None)
        enc = cryptobox.encrypt_text(json.dumps(cur)) if cur else ""
        c.execute("UPDATE users SET secrets_enc = ?, secrets_backup = ? WHERE id = ?", (enc, backup, user_id))


def reencrypt_all():
    """After rotating SECRET_KEY: re-encrypt every saved credential with the new key."""
    n = 0
    with _conn() as c:
        for row in c.execute("SELECT id, secrets_enc FROM users WHERE secrets_enc != ''").fetchall():
            text = cryptobox.decrypt_text(row["secrets_enc"])
            if text is not None and not cryptobox.is_primary(row["secrets_enc"]):
                c.execute("UPDATE users SET secrets_enc = ? WHERE id = ?", (cryptobox.encrypt_text(text), row["id"]))
                n += 1
    return n


def connections_view():
    """What Settings > Connections shows, for the CURRENT user context. Secret
    values are never sent to the browser -- only whether they're set and the last 4 characters."""
    out = []
    for name, is_secret in CONNECTION_FIELDS:
        val = config.env(name)
        item = {"name": name, "secret": is_secret, "set": bool(val), "source": config.source_of(name)}
        if is_secret:
            item["hint"] = ("…" + val[-4:]) if val and len(val) > 8 else ("set" if val else "")
        else:
            item["value"] = val
        out.append(item)
    return out


# --------------------------------------------------------------- throttling

_attempts = {}
_MAX_ATTEMPTS, _WINDOW = 5, 15 * 60


def throttled(key):
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < _WINDOW]
    _attempts[key] = recent
    return len(recent) >= _MAX_ATTEMPTS


def record_failure(key):
    _attempts.setdefault(key, []).append(time.time())


def clear_failures(key):
    _attempts.pop(key, None)
