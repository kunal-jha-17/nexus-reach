"""
mailer.py -- the app's own emails (password resets, team invites), as opposed to outreach.

Sent through SYSTEM_SMTP_* on the server when those are set; otherwise through the admin account's
own email connection (its Gmail sign-in or SMTP login under Settings > Connections).
"""
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import config

SYSTEM_NAMES = {"SYSTEM_SMTP_HOST": "SMTP_HOST", "SYSTEM_SMTP_PORT": "SMTP_PORT", "SYSTEM_SMTP_USER": "SMTP_USER",
                "SYSTEM_SMTP_PASS": "SMTP_PASS", "SYSTEM_SMTP_FROM": "SMTP_FROM_EMAIL"}


def _system_overrides():
    if not (config._server_env("SYSTEM_SMTP_HOST") and config._server_env("SYSTEM_SMTP_USER")):
        return None
    values = {name: config._server_env(sys_name) for sys_name, name in SYSTEM_NAMES.items()}
    values.update({"EMAIL_SEND_VIA": "smtp", "SMTP_FROM_NAME": "Nexus Reach", "GMAIL_REFRESH_TOKEN": ""})
    return values


def available():
    """Can the app send its own emails at all?"""
    import accounts
    import outreach
    import userctx
    if _system_overrides():
        return True
    admin = next((u for u in accounts.all_users() if u["is_admin"]), None)
    if not admin:
        return False
    with userctx.use(admin, accounts.db_path_for(admin["id"])):
        return bool(outreach.email_transport())


def send(to, subject, text):
    """Send one plain-text email. Returns True when it went, False when there's no way to send."""
    import accounts
    import outreach
    import userctx

    def build():
        sender = outreach.from_address()
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("Nexus Reach", sender))
        msg["To"] = to
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
        msg.set_content(text)
        return msg

    ov = _system_overrides()
    if ov:
        with config.overrides(ov):
            outreach._deliver(build())
        return True
    admin = next((u for u in accounts.all_users() if u["is_admin"]), None)
    if not admin:
        return False
    with userctx.use(admin, accounts.db_path_for(admin["id"])):
        if not outreach.email_transport():
            return False
        outreach._deliver(build())
    return True
