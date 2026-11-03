"""
inboxes.py -- send from several mailboxes and share the day's emails between them.

The account's main email settings (Settings > Connections > Email sending) are inbox "main".
Extra inboxes are saved with the account's other keys (encrypted) as a list of dicts:

    {"id", "label", "enabled", "daily_limit", "from_email", "from_name", "send_via",
     "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "gmail_refresh_token",
     "imap_host", "imap_user", "imap_pass", "started_at"}

Every send picks the usable inbox with the most room left today (each has its own daily limit
and its own warm-up), so 5 Workspace mailboxes at 400 a day send 2,000 a day between them.
A follow-up always leaves from the inbox the first email came from, so it stays in the same
conversation; replies and bounces are checked in every inbox.
"""
import contextvars
import json
import re
import uuid
from contextlib import contextmanager

import config
import userctx

MAX_INBOXES = 25
SECRET_FIELDS = ("smtp_pass", "gmail_refresh_token", "imap_pass")
TEXT_FIELDS = ("label", "from_email", "from_name", "send_via", "smtp_host", "smtp_port", "smtp_user",
               "imap_host", "imap_user")
# inbox field -> the setting name the sending code reads (config.env)
ENV_NAMES = {
    "from_email": "SMTP_FROM_EMAIL", "from_name": "SMTP_FROM_NAME", "send_via": "EMAIL_SEND_VIA",
    "smtp_host": "SMTP_HOST", "smtp_port": "SMTP_PORT", "smtp_user": "SMTP_USER", "smtp_pass": "SMTP_PASS",
    "gmail_refresh_token": "GMAIL_REFRESH_TOKEN", "imap_host": "IMAP_HOST", "imap_user": "IMAP_USER",
    "imap_pass": "IMAP_PASS",
}

_CURRENT = contextvars.ContextVar("inbox_id", default="main")


def current_id():
    """The inbox the code inside `using()` is sending as ('main' outside one)."""
    return _CURRENT.get()


def extra():
    """This account's extra inboxes (with passwords), as saved."""
    user = userctx.get_user() or {}
    return [dict(x) for x in ((user.get("secrets") or {}).get("INBOXES") or []) if isinstance(x, dict) and x.get("id")]


def get(inbox_id):
    if not inbox_id or inbox_id == "main":
        return None
    return next((x for x in extra() if x["id"] == inbox_id), None)


def env_of(inbox):
    """The settings an inbox sends with. Everything the sending code reads is listed, blank where the
    inbox has nothing, so none of the main inbox's details leak into it."""
    return {name: str(inbox.get(field) or "") for field, name in ENV_NAMES.items()}


@contextmanager
def using(inbox):
    """Run the block as this inbox (None = the main one)."""
    token = _CURRENT.set(inbox["id"] if inbox else "main")
    try:
        if inbox:
            with config.overrides(env_of(inbox)):
                yield
        else:
            yield
    finally:
        _CURRENT.reset(token)


def public(inbox, extra_info=None):
    """What the browser may see: no passwords, only whether they're saved."""
    out = {k: inbox.get(k, "") for k in ("id",) + TEXT_FIELDS}
    out["enabled"] = bool(inbox.get("enabled", True))
    out["daily_limit"] = int(inbox.get("daily_limit") or 0)
    for k in SECRET_FIELDS:
        out[k + "_set"] = bool(inbox.get(k))
    out.update(extra_info or {})
    return out


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def clean(incoming, existing=None):
    """Validate one inbox from the browser. Blank password boxes keep the saved password."""
    import accounts
    inbox = dict(existing or {})
    inbox.setdefault("id", uuid.uuid4().hex[:10])
    for k in TEXT_FIELDS:
        if k in incoming:
            inbox[k] = str(incoming.get(k) or "").strip()
    for k in SECRET_FIELDS:
        v = str(incoming.get(k) or "").strip()
        if v:
            inbox[k] = v
        elif incoming.get("clear_" + k):
            inbox.pop(k, None)
    if "enabled" in incoming:
        inbox["enabled"] = bool(incoming["enabled"])
    inbox.setdefault("enabled", True)
    if "daily_limit" in incoming:
        try:
            inbox["daily_limit"] = max(1, min(100000, int(incoming["daily_limit"])))
        except (TypeError, ValueError):
            raise ValueError("Daily limit must be a number, e.g. 400.")
    inbox.setdefault("daily_limit", 50)
    login = inbox.get("smtp_user", "")
    sender = inbox.get("from_email") or (login if "@" in login else "")
    if not sender or not _EMAIL.match(sender):
        raise ValueError("Give the inbox an address to send from (its email address).")
    if inbox.get("smtp_host"):
        accounts.validate_mail_value("SMTP_HOST", inbox["smtp_host"])
    if inbox.get("smtp_port"):
        accounts.validate_mail_value("SMTP_PORT", inbox["smtp_port"])
    if inbox.get("imap_host"):
        accounts.validate_mail_value("IMAP_HOST", inbox["imap_host"])
    if inbox.get("send_via") not in ("", None, "auto", "smtp", "gmail_api"):
        raise ValueError("Send through must be automatic, SMTP or the Gmail API.")
    if not (inbox.get("smtp_user") and inbox.get("smtp_pass")) and not inbox.get("gmail_refresh_token"):
        raise ValueError("The inbox needs its SMTP login and password (or a Gmail API refresh token).")
    inbox["label"] = inbox.get("label") or sender
    return inbox


def save_all(user_id, inboxes):
    import accounts
    if len(inboxes) > MAX_INBOXES:
        raise ValueError(f"Up to {MAX_INBOXES} extra inboxes per account.")
    accounts.set_inboxes(user_id, inboxes)


def sent_today_by_inbox(settings=None):
    """{inbox id: emails it sent today} -- from the send events, which record the inbox."""
    import db
    from datetime import datetime, timezone
    now = datetime.now(db.local_zone(settings))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    out = {}
    with db.get_conn() as conn:
        for r in conn.execute("SELECT detail FROM events WHERE kind IN ('email_sent','followup_sent') AND created_at >= ?",
                              (start.isoformat(timespec="seconds"),)).fetchall():
            try:
                iid = (json.loads(r["detail"] or "{}") or {}).get("inbox") or "main"
            except (ValueError, AttributeError):
                iid = "main"
            out[iid] = out.get(iid, 0) + 1
    return out
