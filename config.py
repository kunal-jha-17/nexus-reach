"""
config.py -- environment helpers.

Server-level settings (ports, data folder, secret key) come from environment
variables or a .env file. Credentials that belong to a person -- their AI keys,
their Gmail App Password, their SerpAPI key -- are stored per account and
looked up here first, so every user sends from their own mailbox and spends
their own quota. See USER_SETTABLE below for exactly which names that covers.

Day-to-day settings (templates, limits, channel order...) live in the Settings
tab and are stored in each user's own database instead.
"""
import os
from pathlib import Path

import userctx

BASE_DIR = Path(__file__).resolve().parent

try:  # python-dotenv is optional -- plain environment variables work too
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# Names an account can set for itself (Settings > Connections).
USER_SETTABLE = {
    "LLM_PROVIDER", "LLM_PROVIDER_DRAFT", "LLM_PROVIDER_ENRICH", "LLM_PROVIDER_FILTER",
    "GROQ_API_KEY", "GROQ_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL",
    "SMTP_USER", "SMTP_PASS", "SMTP_HOST", "SMTP_PORT", "SMTP_FROM_NAME",
    "IMAP_USER", "IMAP_PASS", "IMAP_HOST",
    "SERPAPI_KEY", "SERPAPI_MONTHLY_LIMIT",
    "IG_USERNAME", "IG_PASSWORD",
}
# The server owner may share their AI keys with everyone (SHARE_LLM_WITH_USERS=1).
# Email, Instagram and search credentials are never shared.
SHAREABLE = {n for n in USER_SETTABLE
             if n.startswith(("LLM_PROVIDER", "GROQ_", "OPENAI_", "GEMINI_"))}


def _server_env(name, default=""):
    return os.environ.get(name, default)


def env(name, default=""):
    user = userctx.get_user()
    if user is not None and name in USER_SETTABLE:
        own = (user.get("secrets") or {}).get(name)
        if own not in (None, ""):
            return str(own)
        # No value of their own: the admin (whose .env this is) falls back to the
        # server environment; other users only for shared AI keys, if enabled.
        if user.get("is_admin") or (name in SHAREABLE and env_bool_server("SHARE_LLM_WITH_USERS")):
            return _server_env(name, default)
        return default
    return _server_env(name, default)


def source_of(name):
    """Where the value of a user-settable name currently comes from."""
    user = userctx.get_user()
    if user is not None and (user.get("secrets") or {}).get(name) not in (None, ""):
        return "account"
    return "server" if _server_env(name) and env(name) else "none"


def env_bool_server(name, default=False):
    v = _server_env(name, None)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(env(name, default) or default)
    except (TypeError, ValueError):
        return default


def env_bool(name, default=False):
    v = env(name, None)
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def db_path():
    """The legacy single-user database file (only used for tests and for adopting
    an existing hub.db into the first account)."""
    return _server_env("HUB_DB", str(BASE_DIR / "hub.db"))


def data_dir():
    """Folder holding every user's database and the login secrets (DATA_DIR)."""
    p = Path(_server_env("DATA_DIR", str(BASE_DIR / "data")))
    (p / "users").mkdir(parents=True, exist_ok=True)
    return p


def sync_enabled():
    """True when data is being copied to outside storage (Supabase / any S3 service / a folder)."""
    return bool((_server_env("S3_BUCKET") and _server_env("S3_ACCESS_KEY_ID")) or _server_env("SYNC_FOLDER"))
