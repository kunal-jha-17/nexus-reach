"""
outreach.py -- turn qualified leads into conversations.

  * drafting: an LLM writes each message from your business info + templates
    + everything known about the lead (trade, city, website status, notes...)
  * email: sent automatically, with a daily cap, a warm-up ramp and a delay
    between sends. Every send is logged, so the daily count is exact.
  * every other channel (Yelp, Facebook, Instagram, WhatsApp, phone/SMS) is
    human-sent: the app copies the message and opens the right place; you
    press send and mark it sent. (Automated DMs get accounts banned.)
  * follow-ups thread under the original email; reply checking (IMAP) marks
    leads as replied and honours unsubscribe requests

Fixes compared with the original Outreach Desk:
  * an opt-out is only detected in the reply's own text -- the quoted
    "Reply STOP" footer in our own email no longer triggers it
  * reply checking peeks at mail, so it doesn't mark your inbox as read
  * auto-replies (out-of-office) don't count as real replies
  * follow-ups keep the original message; each send is an event, not an overwrite
  * emails get real subjects, a Message-ID and threading headers
"""
import contextvars
import email
import imaplib
import json
import re
import smtplib
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, formataddr, parseaddr

import requests

import config
import db
import gmailapi
import inboxes
import llm
import netguard
import schema


class SendError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------- templates

def _family(ch):
    return "phone" if ch in ("phone", "whatsapp") else ch


def resolve_template(templates, channel, selection="", index=0):
    """Choose the template for a lead. selection: '' = match the lead's
    channel and rotate through matches; 'rotate-all'; or '<channel>::<name>'."""
    templates = [t for t in (templates or []) if t.get("body")]
    if not selection:
        matches = [t for t in templates if _family(t.get("channel")) == _family(channel)]
        if not matches:
            return None, None
        t = matches[index % len(matches)]
        return t.get("name"), t.get("body")
    if selection == "rotate-all":
        if not templates:
            return None, None
        t = templates[index % len(templates)]
        return t.get("name"), t.get("body")
    if "::" in selection:
        chan, name = selection.split("::", 1)
        for t in templates:
            if t.get("channel") == chan and t.get("name") == name:
                return t.get("name"), t.get("body")
    return None, None


_OPENING_STYLES = [
    "start with a genuine observation about their business",
    "start with a brief, specific compliment tied to what they do",
    "start with a light, casual question relevant to their work",
    "start by referencing something specific from the details, conversationally",
]

CHANNEL_WORDS = {"email": "email", "whatsapp": "WhatsApp", "phone": "phone / SMS", "instagram": "Instagram",
                 "facebook": "Facebook", "yelp": "Yelp", "unknown": "matching"}

CHANNEL_NOTES = {
    "email": ("This is an email. Write a first line 'Subject: <short, specific subject>', then a "
              "blank line, then the body. Normal greeting, a touch more detail is fine."),
    "whatsapp": "This is a WhatsApp message -- casual and brief, like texting someone.",
    "phone": "This is a text message (SMS) -- casual and brief, like texting someone.",
    "instagram": "This is an Instagram DM -- very casual and short, like a real DM, not an email.",
    "facebook": "This is a Facebook message -- casual and brief, like a real DM.",
    "yelp": "This is a Yelp message to the business -- brief, polite and plain-spoken.",
}


def lead_facts(lead):
    place = ", ".join(x for x in (lead.get("city"), lead.get("state")) if x)
    rows = [
        ("Business", lead.get("business_name")),
        ("Contact person (may be unverified)", lead.get("contact_name")),
        ("Trade", (lead.get("trade") or "").replace("_", " ") or lead.get("category_raw")),
        ("Location", place),
        ("Website", lead.get("website") if lead.get("has_website") else "none found"),
        ("Google/Yelp reviews", lead.get("review_count") or None),
        ("Why they were picked", lead.get("fit_reason")),
        ("Notes", lead.get("notes")),
    ]
    return "\n".join(f"{k}: {v}" for k, v in rows if v)


def build_prompt(lead, settings, template_text=None, seed=0):
    parts = []
    info = (settings.get("business_info") or "").strip()
    if info:
        parts.append(f"Background on who is sending this outreach:\n{info}\n")
    style = _OPENING_STYLES[seed % len(_OPENING_STYLES)]
    note = CHANNEL_NOTES.get(lead.get("channel"), "")
    if template_text:
        parts.append(
            "Use this template for structure, tone and the offer (keep any prices, terms and "
            "calls to action exactly as written), but personalise the specifics for this lead. "
            f"Don't copy it word for word, and keep about the same length:\n{template_text}\n"
        )
        length_rule = ""
    else:
        length_rule = ("Write a short, warm, personalised outreach opener (2-3 sentences) for a cold "
                       "lead. No filler like 'I hope this finds you well'. ")
    parts.append(
        f"{length_rule}Be specific and human -- write like a real person messaging someone they're "
        "genuinely interested in, not a mail-merge. Vary sentence rhythm. Use ONLY the facts listed "
        "below; never invent details (services, years in business, reviews, prices). Never leave "
        "a placeholder such as {name} or [Business Name] in the message -- use the real detail, or "
        "reword so it isn't needed. "
        f"For this message, {style}. {note}\n\n"
        f"ABOUT THE LEAD:\n{lead_facts(lead)}\n\n"
        "Return only the message text, nothing else."
    )
    return "\n".join(parts)


def _clean_message(text, channel):
    t = text.strip().strip('"').strip()
    if channel != "email":
        t = re.sub(r"^\s*subject:.*\n+", "", t, flags=re.I)
    return t.strip()


def final_message(lead, text=None):
    """The message exactly as it will go out: every {name} / [Business Name] style placeholder
    replaced with this lead's details. Refuses (SendError) while any placeholder is still
    unfilled, so a message can never be sent reading 'Hi {name}' or 'Hi ,'."""
    filled, missing = schema.fill_placeholders(lead["message"] if text is None else text, lead)
    if missing:
        raise SendError(
            f"This message still contains {', '.join(missing[:4])} and "
            f"{lead.get('business_name') or 'this lead'} has nothing to put there. Fill in the missing "
            "detail on the lead (e.g. its business name) or edit the message, then send again.", 400)
    return filled


def draft_message(lead_id, selection="", index=0, settings=None, exact=None):
    """Draft (or redraft) the message for one lead and save it.

    exact=True is a plain mail-merge: the template's own words with the blanks filled in, no AI
    involved (and no AI key needed). exact=None follows the account's "use templates exactly"
    setting. The result carries "unfilled": placeholders this lead had nothing for."""
    lead = db.get_lead(lead_id)
    if lead is None:
        raise SendError("Lead not found", 404)
    # the lead's campaign may override the about-you text, templates and channel order
    settings = settings or db.effective_settings(lead["campaign"])
    name, body = resolve_template(settings.get("templates"), lead["channel"], selection, index)
    if exact is None:
        exact = bool(settings.get("exact_templates"))
    if exact:
        if not body:
            raise SendError(f"There's no {CHANNEL_WORDS.get(lead['channel'], lead['channel'])} template to fill in for "
                            f"{lead['business_name'] or 'this lead'}. Add one under Settings > Messaging, or pick a "
                            "template by name.", 400)
        text, unfilled = schema.fill_placeholders(_clean_message(body, lead["channel"]), lead)
        db.update_lead(lead_id, {"message": text, "template_used": name or ""})
        db.add_event(lead_id, "drafted", f"{name} (template as written)")
        return {"message": text, "template_used": name or "", "unfilled": unfilled}
    if body:
        body, _ = schema.fill_placeholders(body, lead)      # the AI sees real names, not {name}
    prompt = build_prompt(lead, settings, body, seed=index)
    text = _clean_message(llm.complete(prompt, task="draft", max_tokens=900, temperature=0.8),
                          lead["channel"])
    text, _ = schema.fill_placeholders(text, lead)          # and anything it copied through
    db.update_lead(lead_id, {"message": text, "template_used": name or ""})
    db.add_event(lead_id, "drafted", name or "no template")
    return {"message": text, "template_used": name or "", "unfilled": []}


# ------------------------------------------------------------------- email

def from_address():
    """The address mail is sent from: SMTP_FROM_EMAIL (needed when the SMTP login isn't an email
    address, as with Amazon SES, SendGrid or Mailgun keys), else the login itself."""
    return (config.env("SMTP_FROM_EMAIL") or config.env("SMTP_USER")).strip()


def _smtp_login_ok():
    return bool(config.env("SMTP_USER") and config.env("SMTP_PASS"))


def email_transport():
    """How email leaves this account: "gmail_api", "smtp", or "" when it isn't set up.
    EMAIL_SEND_VIA can force one; on "auto" a mail server you typed in yourself (anything other than
    Gmail's) wins, then the Gmail API sign-in, then Gmail's SMTP with an App Password."""
    if not from_address():
        return ""
    via = (config.env("EMAIL_SEND_VIA") or "auto").strip().lower()
    if via == "smtp":
        return "smtp" if _smtp_login_ok() else ""
    if via == "gmail_api":
        return "gmail_api" if gmailapi.configured() else ""
    host = (config.env("SMTP_HOST") or "smtp.gmail.com").strip().lower()
    if _smtp_login_ok() and host != "smtp.gmail.com":
        return "smtp"
    return "gmail_api" if gmailapi.configured() else "smtp" if _smtp_login_ok() else ""


def smtp_configured():
    """Email can go out: an address to send from, plus the Gmail API sign-in or an SMTP login --
    in the main inbox or in at least one extra inbox."""
    if inboxes.current_id() != "main" or email_transport():
        return bool(email_transport())
    return any(True for _ in _usable_extras())


def _usable_extras():
    for ib in inboxes.extra():
        if ib.get("enabled", True):
            with inboxes.using(ib):
                if email_transport():
                    yield ib


def _inbox_state(settings, inbox_id):
    return dict((settings.get("inbox_state") or {}).get(inbox_id) or {})


def _put_inbox_state(inbox_id, **values):
    allst = dict(db.get_settings().get("inbox_state") or {})
    cur = dict(allst.get(inbox_id) or {})
    cur.update(values)
    allst[inbox_id] = cur
    db.set_settings({"inbox_state": allst})


def inbox_capacity(settings=None):
    """[(inbox or None for the main one, its limit today, sent today)] for every inbox that can send.
    Each has its own daily limit and its own warm-up (counted from its own first email)."""
    settings = settings or db.get_settings()
    sent = inboxes.sent_today_by_inbox(settings)
    out = []
    if email_transport():
        out.append((None, effective_daily_limit(settings), sent.get("main", 0)))
    for ib in _usable_extras():
        own = dict(settings, email_daily_limit=int(ib.get("daily_limit") or 50),
                   sending_started_at=_inbox_state(settings, ib["id"]).get("started_at", ""))
        out.append((ib, effective_daily_limit(own), sent.get(ib["id"], 0)))
    return out


def total_daily_limit(settings=None):
    """Today's limit across every inbox (just the main one's when there are no extras), never more
    than the account's plan allows."""
    import plans
    settings = settings or db.get_settings()
    caps = inbox_capacity(settings)
    if not caps or (len(caps) == 1 and caps[0][0] is None):
        total = effective_daily_limit(settings)
    else:
        total = sum(c for _, c, _ in caps)
    plan_cap = plans.cap("emails_per_day")
    return min(total, plan_cap) if plan_cap is not None else total


def pick_inbox(settings, inbox_id=None):
    """The inbox the next email goes from: the one with the most room left today (as a share of its
    limit), or the given one (a follow-up stays in its thread). Returns (inbox or None, ok)."""
    caps = inbox_capacity(settings)
    if inbox_id is not None:
        for ib, cap, sent in caps:
            if (ib["id"] if ib else "main") == inbox_id:
                return ib, sent < cap
        inbox_id = None                                     # that inbox was removed: use another
    room = [(sent / cap if cap else 1, i, ib) for i, (ib, cap, sent) in enumerate(caps) if sent < cap]
    if not room:
        return None, False
    room.sort(key=lambda x: (x[0], x[1]))
    return room[0][2], True


def _imap_login():
    """(user, password) for reply checking. The sending login is reused only when it is an email
    address -- an SES/SendGrid key can't open an inbox."""
    user = config.env("IMAP_USER")
    if user:
        return user, config.env("IMAP_PASS") or config.env("SMTP_PASS")
    smtp_user = config.env("SMTP_USER")
    if "@" in smtp_user:
        return smtp_user, config.env("IMAP_PASS") or config.env("SMTP_PASS")
    return "", ""


def imap_configured():
    user, pw = _imap_login()
    return bool(user and pw)


# What a mailbox provider lets ONE account send a day (approximate; providers change these).
def provider_daily_cap():
    t = email_transport()
    host = (config.env("SMTP_HOST") or "smtp.gmail.com").lower()
    sender = from_address().lower()
    if t == "gmail_api" or (t == "smtp" and host == "smtp.gmail.com"):
        return 500 if sender.endswith(("@gmail.com", "@googlemail.com")) else 2000
    if t == "smtp" and ("office365" in host or "outlook" in host):
        return 10000
    if t == "smtp" and "zoho" in host:
        return 500
    return None          # SES, SendGrid, Mailgun, Brevo, Postmark, your own server: set by your plan


WARMUP_SCHEDULE = [(3, 10), (7, 20), (14, 30)]  # (days since first send, daily cap) for small limits
# For big limits (a professional mail server): start at WARMUP_START a day and grow WARMUP_GROWTH x a day.
WARMUP_START, WARMUP_GROWTH = 20, 1.3


def effective_daily_limit(settings):
    limit = int(settings["email_daily_limit"])
    if not settings.get("warmup_enabled"):
        return limit
    started = settings.get("sending_started_at")
    days_in = 0
    if started:
        try:
            days_in = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).days
        except ValueError:
            days_in = 0
    if limit > 100:
        # 20, 26, 34, 44, ... about 3 weeks to 5,000 a day: inbox providers trust a sender that grows steadily
        return min(limit, int(WARMUP_START * WARMUP_GROWTH ** max(0, days_in)))
    for threshold, cap in WARMUP_SCHEDULE:
        if days_in < threshold:
            return min(cap, limit)
    return limit


def warmup_days_left(settings):
    """Days until the warm-up reaches the full daily limit (0 = there already)."""
    limit = int(settings["email_daily_limit"])
    if not settings.get("warmup_enabled") or limit <= 100:
        return 0
    import math
    need = math.ceil(math.log(limit / WARMUP_START) / math.log(WARMUP_GROWTH)) if limit > WARMUP_START else 0
    started = settings.get("sending_started_at")
    try:
        days_in = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).days if started else 0
    except ValueError:
        days_in = 0
    return max(0, need - days_in)


def send_delay(settings, sent_today=None):
    """Seconds to wait between two emails. With automatic pacing the rest of today's allowance is
    spread over what's left of the sending window (or of the day), never faster than one a second --
    5,000 over 9 hours is about one every 6.5 seconds."""
    if not settings.get("auto_pace"):
        return int(settings["email_send_delay_seconds"])
    remaining = max(1, effective_daily_limit(settings) - (emails_sent_today(settings) if sent_today is None else sent_today))
    zone = db.local_zone(settings)
    now = datetime.now(timezone.utc).astimezone(zone)
    end_hour = int(settings.get("send_end_hour", 24)) if settings.get("send_window_enabled") else 24
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=end_hour)
    seconds_left = max(60, (end - now).total_seconds())
    return max(1, min(3600, int(seconds_left / remaining)))


def bounce_rate(days=7):
    """(bounced, sent) over the last `days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.get_conn() as conn:
        sent = conn.execute("SELECT COUNT(*) FROM events WHERE kind IN ('email_sent','followup_sent') AND created_at >= ?",
                            (since,)).fetchone()[0]
        bounced = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'bounced' AND created_at >= ?",
                               (since,)).fetchone()[0]
    return bounced, sent


def too_many_bounces(settings):
    """A reason to stop sending, or ''. Over ~5% bounces tells inbox providers the list is bad --
    carrying on gets the sending domain blocked (and an SES/SendGrid account suspended)."""
    limit = float(settings.get("max_bounce_rate", 5) or 0)
    if limit <= 0:
        return ""
    bounced, sent = bounce_rate()
    if sent >= 40 and bounced / sent * 100 > limit:
        return (f"Paused: {bounced} of the last {sent} emails bounced ({bounced / sent:.0%}), above your "
                f"{limit:g}% limit. Clean the list (remove failed addresses) before sending more -- a high "
                "bounce rate gets your sending domain blocked. Change the limit under Settings > Sending.")
    return ""


def emails_sent_today(settings=None):
    return db.count_events_today(("email_sent", "followup_sent"), settings)


DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def send_window(settings=None, now=None):
    """Whether bulk sending is allowed right now. Returns (ok, why): with sending hours switched
    on, mail only goes out on the chosen days between the chosen hours, in the account's time zone
    -- so a batch started at midnight doesn't land in inboxes at 3 am."""
    settings = settings or db.get_settings()
    if not settings.get("send_window_enabled"):
        return True, ""
    zone = db.local_zone(settings)
    now = (now or datetime.now(timezone.utc)).astimezone(zone)
    days = [d for d in (settings.get("send_days") or []) if isinstance(d, int) and 0 <= d <= 6]
    start, end = int(settings.get("send_start_hour", 8)), int(settings.get("send_end_hour", 18))
    where = (settings.get("timezone") or "UTC")
    hours = f"{start:02d}:00-{end:02d}:00 ({where})"
    if not days or start >= end:
        return False, "Your sending hours leave no time to send in. Fix them under Settings > Sending."
    if now.weekday() not in days:
        return False, (f"Today ({DAY_NAMES[now.weekday()]}) isn't one of your sending days. "
                       f"Sending hours are {hours} on {', '.join(DAY_NAMES[d][:3] for d in sorted(days))}.")
    if not (start <= now.hour < end):
        return False, f"It's {now:%H:%M} there now -- outside your sending hours of {hours}."
    return True, ""


def email_status(settings=None):
    settings = settings or db.get_settings()
    eff = total_daily_limit(settings)
    sent = emails_sent_today(settings)
    window_ok, window_why = send_window(settings)
    caps = inbox_capacity(settings)
    full = int(settings["email_daily_limit"]) + sum(int(ib.get("daily_limit") or 0) for ib, _, _ in caps if ib)
    return {
        "window_open": window_ok, "window_note": window_why,
        "hand_followups_due": len(hand_followups_due(settings)),
        "reply_check_minutes": int(settings.get("reply_check_minutes") or 0),
        "last_reply_check_at": settings.get("last_reply_check_at") or "",
        "sent_today": sent,
        "daily_limit": eff,
        "max_limit": full if any(ib for ib, _, _ in caps) else int(settings["email_daily_limit"]),
        "remaining": max(0, eff - sent),
        "warmup_active": bool(settings.get("warmup_enabled")) and eff < (full if any(ib for ib, _, _ in caps)
                                                                        else int(settings["email_daily_limit"])),
        "inboxes": [{"id": ib["id"] if ib else "main", "label": (ib.get("label") if ib else from_address()),
                     "limit_today": cap, "sent_today": n} for ib, cap, n in caps],
        "smtp_configured": smtp_configured(),
        "imap_configured": imap_configured(),
        "followups_due": len(followups_due(settings)),
        "transport": email_transport(),
        "provider_cap": provider_daily_cap(),
        "warmup_days_left": warmup_days_left(settings),
        "delay_seconds": send_delay(settings, sent),
        "bounces_7d": dict(zip(("bounced", "sent"), bounce_rate())),
        "unsubscribe_link": bool(public_url()),
        "plan": send_plan_status(settings),
    }


def split_subject(message, lead):
    lines = (message or "").strip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0][8:].strip()
        body = "\n".join(lines[1:]).strip()
    else:
        subject, body = "", (message or "").strip()
    if not subject:
        subject = f"Quick question for {lead.get('business_name') or 'you'}"
    return subject[:150], body


def build_email(lead, subject, body, settings, in_reply_to=None):
    user = from_address()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.env("SMTP_FROM_NAME"), user)) if config.env("SMTP_FROM_NAME") else user
    msg["To"] = lead["email"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(user.split("@")[-1] if "@" in user else None))
    link = unsubscribe_link(lead)
    if link:
        # one-click unsubscribe (RFC 8058): Gmail and Yahoo require it from anyone sending 5,000+ a day
        msg["List-Unsubscribe"] = f"<{link}>, <mailto:{user}?subject=unsubscribe>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    else:
        msg["List-Unsubscribe"] = f"<mailto:{user}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    footer = (settings.get("email_footer") or "").strip()
    msg.set_content(body + (f"\n\n--\n{footer}" if footer else ""))
    return msg


def public_url():
    """This app's own web address, for links in emails. Render sets RENDER_EXTERNAL_URL itself."""
    return (config._server_env("PUBLIC_URL") or config._server_env("RENDER_EXTERNAL_URL") or "").rstrip("/")


def unsubscribe_token(user_id, lead_id):
    import hashlib
    import hmac
    key = (config._server_env("SECRET_KEY") or "dev").encode()
    sig = hmac.new(key, f"unsub:{user_id}:{lead_id}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{user_id}.{lead_id}.{sig}"


def read_unsubscribe_token(token):
    """(user_id, lead_id) for a genuine token, else None."""
    import hmac
    try:
        uid, lid, _ = token.split(".")
        uid, lid = int(uid), int(lid)
    except (ValueError, AttributeError):
        return None
    return (uid, lid) if hmac.compare_digest(unsubscribe_token(uid, lid), token) else None


def unsubscribe_link(lead):
    import userctx
    base, user = public_url(), userctx.get_user()
    if not base or not user or not lead.get("id"):
        return ""
    return f"{base}/u/{unsubscribe_token(user['id'], lead['id'])}"


def unsubscribe_lead(lead_id):
    """Someone used the unsubscribe link: never contact them again."""
    lead = db.get_lead(lead_id)
    if lead is None:
        return False
    if lead.get("email"):
        db.add_suppression(lead["email"], "unsubscribed via link")
    if not lead["do_not_contact"]:
        db.update_lead(lead_id, {"do_not_contact": 1, "dnc_reason": "unsubscribed via link",
                                 "stage": "dead" if lead["stage"] in ("new", "contacted") else lead["stage"],
                                 "reply_category": lead.get("reply_category") or "not_interested"})
        db.add_event(lead_id, "optout", "unsubscribe link")
    return True


def _explain_connect_error(e, host, port):
    if isinstance(e, smtplib.SMTPException):
        return str(e)
    return (f"Couldn't connect to {host}:{port} ({e}). If this app runs on a free hosting plan, the host may block "
            "outgoing email ports (Render's free plan blocks 25, 465 and 587). Use port 2525 (SendGrid, Mailgun, Brevo, "
            "Postmark) or 2587 (Amazon SES), or send through the Gmail API (HTTPS) -- see the README.")


class DeliveryError(RuntimeError):
    """Sending failed. recipient=True: the mail server turned down THIS address (the lead's
    problem). Otherwise it's ours -- wrong password, blocked port, daily quota -- and every
    other send would fail the same way."""

    def __init__(self, message, recipient=False):
        super().__init__(message)
        self.recipient = recipient


_SESSION = contextvars.ContextVar("smtp_session", default=None)
MESSAGES_PER_CONNECTION = 100        # reconnect after this many (some servers cap messages per connection)


def _smtp_settings():
    host = config.env("SMTP_HOST") or "smtp.gmail.com"
    port = config.env_int("SMTP_PORT", 587)
    return host, port, config.env("SMTP_USER"), config.env("SMTP_PASS")


def _smtp_connect(timeout=30):
    """An open, logged-in SMTP connection: implicit TLS on 465/2465, STARTTLS elsewhere."""
    host, port, user, pw = _smtp_settings()
    netguard.check_mail_host(host, port)
    if port in (465, 2465):
        s = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        s = smtplib.SMTP(host, port, timeout=timeout)
        s.starttls()                     # says EHLO first, and login() says it again over the encrypted line
    s.login(user, pw)
    return s


class SmtpSession:
    """One SMTP connection reused for a whole batch -- thousands of emails a day over a fresh
    connection each would be slow and look like abuse to the mail server."""

    def __init__(self):
        self.conn, self.count = None, 0

    def send(self, msg):
        if self.conn is None or self.count >= MESSAGES_PER_CONNECTION:
            self.close()
            self.conn, self.count = _smtp_connect(), 0
        try:
            self.conn.send_message(msg)
        except smtplib.SMTPServerDisconnected:
            self.conn, self.count = _smtp_connect(), 0           # dropped while idle: reconnect once
            self.conn.send_message(msg)
        self.count += 1

    def close(self):
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:  # noqa: BLE001
                pass
        self.conn = None


@contextmanager
def smtp_session():
    """Reuse one SMTP connection per inbox for every send inside this block (bulk runs, follow-ups)."""
    if _SESSION.get() is not None:
        yield _SESSION.get()
        return
    pool = {}
    token = _SESSION.set(pool)
    try:
        yield pool
    finally:
        _SESSION.reset(token)
        for sess in pool.values():
            sess.close()


def _deliver(msg):
    """Actually send the message: through the Gmail API (HTTPS) or over SMTP, whichever
    email_transport() picks. Returns Gmail's {"id", "threadId"} for an API send, else None.
    A follow-up can carry msg.gmail_thread_id to stay in the original Gmail conversation."""
    if email_transport() == "gmail_api":
        try:
            return gmailapi.send(msg, getattr(msg, "gmail_thread_id", None))
        except gmailapi.GmailError as e:
            raise DeliveryError(str(e), recipient=e.recipient)
    host, port, _, _ = _smtp_settings()
    pool = _SESSION.get()
    one_off = pool is None
    if one_off:
        sess = SmtpSession()
    else:
        sess = pool.setdefault(inboxes.current_id(), SmtpSession())
    try:
        sess.send(msg)
    except netguard.Blocked as e:
        raise DeliveryError(str(e))
    except smtplib.SMTPRecipientsRefused as e:
        why = "; ".join(f"{code} {(text.decode(errors='replace') if isinstance(text, bytes) else text)}"
                        for code, text in e.recipients.values())[:200]
        raise DeliveryError(f"The mail server refused this address ({why}).", recipient=True)
    except smtplib.SMTPAuthenticationError:
        sess.close()
        raise DeliveryError("Your mail server refused the login. For Gmail use an App Password; for SES, SendGrid "
                            "or Mailgun use the SMTP username and password (or API key) they gave you.")
    except (OSError, smtplib.SMTPException) as e:
        sess.close()
        raise DeliveryError(_explain_connect_error(e, host, port))
    finally:
        if one_off:
            sess.close()
    return None


def test_smtp():
    """Check the email details (the Gmail API sign-in, or the SMTP login). Sends nothing."""
    if not smtp_configured():
        raise SendError("Add your email address, plus a Gmail API refresh token, an App Password or your mail "
                        "server's SMTP login, first.", 400)
    if email_transport() == "gmail_api":
        try:
            return gmailapi.test()
        except gmailapi.GmailError as e:
            raise SendError(str(e), 400 if e.auth else 502)
    host, port, user, _ = _smtp_settings()
    try:
        s = _smtp_connect(timeout=12)
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass
    except netguard.Blocked as e:
        raise SendError(str(e), 400)
    except smtplib.SMTPAuthenticationError:
        raise SendError("The mail server refused the login. For Gmail use an App Password (Google Account > Security > "
                        "2-Step Verification > App passwords), not your normal password. For SES, SendGrid or Mailgun "
                        "use the SMTP credentials from their dashboard.", 400)
    except (OSError, smtplib.SMTPException) as e:
        raise SendError(_explain_connect_error(e, host, port), 502)
    cap = provider_daily_cap()
    note = f" This provider allows about {cap:,} emails a day per account." if cap else ""
    return {"ok": True, "message": f"Connected to {host}:{port} and logged in as {user}; mail will be sent from "
                                   f"{from_address()}. (Nothing was sent.){note}"}


def test_imap():
    """Log in to the inbox used for reply checking. Reads nothing."""
    if not imap_configured():
        raise SendError("Add your email address and App Password first.", 400)
    host = config.env("IMAP_HOST", "imap.gmail.com")
    user, pw = _imap_login()
    try:
        netguard.check_mail_host(host, 993)
    except netguard.Blocked as e:
        raise SendError(str(e), 400)
    try:
        mail = imaplib.IMAP4_SSL(host, timeout=12)
        mail.login(user, pw)
        mail.logout()
    except imaplib.IMAP4.error:
        raise SendError("The inbox refused the login. For Gmail, turn on IMAP in Gmail settings and use an App Password.", 400)
    except OSError as e:
        raise SendError(f"Couldn't connect to {host}:993 ({e}).", 502)
    return {"ok": True, "message": f"Connected to {host} and logged in as {user}."}


def test_llm():
    """One tiny request to check the AI key works."""
    if not llm.is_configured("draft"):
        raise SendError("Add an AI key first (Groq, OpenAI or Gemini).", 400)
    try:
        llm.complete("Reply with the single word OK.", task="draft", max_tokens=20, temperature=0)
    except llm.LLMError as e:
        raise SendError(f"The AI provider said: {e}", 502)
    st = llm.status()["draft"]
    return {"ok": True, "message": f"{st['provider']} ({st['model']}) answered."}


def _ensure_sending_started(settings):
    iid = inboxes.current_id()
    if iid != "main":
        if not _inbox_state(settings, iid).get("started_at"):
            _put_inbox_state(iid, started_at=schema.now_iso())
            settings["inbox_state"] = db.get_settings().get("inbox_state") or {}
        return
    if not settings.get("sending_started_at"):
        db.set_settings({"sending_started_at": schema.now_iso()})
        settings["sending_started_at"] = schema.now_iso()


def _check_can_email(lead, settings, followup=False):
    if lead is None:
        raise SendError("Lead not found", 404)
    if lead["do_not_contact"] or db.is_suppressed(lead):
        raise SendError("This lead is on the do-not-contact list.", 400)
    if not lead["email"]:
        raise SendError("This lead has no email address.", 400)
    if lead["stage"] in schema.CLOSED_STAGES:
        raise SendError(f"Lead is marked '{lead['stage']}' -- not sending.", 400)
    if not smtp_configured():
        raise SendError("Email isn't set up: add your email address and App Password under "
                        "Settings > Connections.", 400)
    eff = total_daily_limit(settings)
    sent = emails_sent_today(settings)
    if sent >= eff:
        note = " (still warming up the account)" if eff < int(settings["email_daily_limit"]) else ""
        raise SendError(f"Daily email limit reached ({sent}/{eff}){note}. This protects your sending "
                        "reputation -- try again tomorrow.", 429)


_DOMAIN_CHECKS = {}          # domain -> (verdict, checked_at); verdicts: ok | no_domain | no_mail | unknown
_DOMAIN_TTL = 24 * 3600


def check_email_domain(address):
    """Free check that an address's domain exists and accepts email (an MX record, or an address
    record as the fallback mail servers use). Asked over HTTPS (Google's public DNS) because hosts
    such as Render block other outgoing ports. 'unknown' (couldn't ask) never stops a send."""
    if not config.env_bool_server("EMAIL_DOMAIN_CHECK", True):
        return "ok"
    domain = (address or "").rpartition("@")[2].strip().lower()
    if not domain:
        return "no_domain"
    if domain in FREE_MAIL_DOMAINS:
        return "ok"
    hit = _DOMAIN_CHECKS.get(domain)
    now = datetime.now(timezone.utc).timestamp()
    if hit and now - hit[1] < _DOMAIN_TTL:
        return hit[0]

    def ask(rtype):
        r = requests.get("https://dns.google/resolve", params={"name": domain, "type": rtype}, timeout=5)
        r.raise_for_status()
        return r.json()
    try:
        mx = ask("MX")
        if mx.get("Status") == 3:
            verdict = "no_domain"
        elif any(a.get("type") == 15 for a in mx.get("Answer") or []):
            # "0 ." is a "null MX": the domain says outright that it takes no email
            null_mx = all((a.get("data") or "").strip() in ("0 .", "0.")
                          for a in mx["Answer"] if a.get("type") == 15)
            verdict = "no_mail" if null_mx else "ok"
        else:
            a = ask("A")
            verdict = "ok" if any(x.get("type") in (1, 28) for x in a.get("Answer") or []) else "no_mail"
    except Exception:  # noqa: BLE001 -- no answer: don't block the send over our own lookup failing
        return "unknown"
    _DOMAIN_CHECKS[domain] = (verdict, now)
    return verdict


_DOMAIN_PROBLEM = {"no_domain": "the email's domain doesn't exist", "no_mail": "the email's domain doesn't accept email"}


def _require_deliverable(lead):
    """Stop before sending to an address that can only bounce (bounces hurt your sender reputation)."""
    verdict = check_email_domain(lead.get("email"))
    if verdict in _DOMAIN_PROBLEM:
        why = _DOMAIN_PROBLEM[verdict]
        db.update_lead(lead["id"], {"send_status": "failed", "email_check": why})
        db.add_event(lead["id"], "send_failed", f"Not sent: {why} ({lead.get('email')})")
        raise SendError(f"Not sent: {why}. Find another address or reach them another way.", 502)
    if verdict == "ok" and lead.get("email_check") != "ok":
        db.update_lead(lead["id"], {"email_check": "ok"})


def send_lead_email(lead_id, force=False):
    settings = db.get_settings()
    lead = db.get_lead(lead_id)
    _check_can_email(lead, settings)
    if not lead["message"].strip():
        raise SendError("Write or generate a message first.", 400)
    if lead["send_status"] == "sent" and not force:
        raise SendError("Already sent. Use a follow-up instead.", 400)
    if not force:
        twin = db.already_emailed(lead["email"], lead_id)
        if twin:
            db.add_event(lead_id, "send_skipped", f"Same inbox already emailed via lead {twin['id']} "
                                                  f"({twin['business_name']})")
            raise SendError(f"Skipped: {lead['email']} was already emailed (lead '{twin['business_name']}'), "
                            "e.g. another branch sharing one inbox.", 409)
    _require_deliverable(lead)
    ib, ok = pick_inbox(settings)
    if not ok:
        raise SendError("Every sending inbox has reached its limit for today -- the rest go out tomorrow.", 429)
    with inboxes.using(ib):
        return _send_first_email(lead_id, lead, settings)


def _send_first_email(lead_id, lead, settings):
    _ensure_sending_started(settings)

    subject, body = split_subject(final_message(lead), lead)
    msg = build_email(lead, subject, body, settings)
    try:
        sent = _deliver(msg)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "recipient", False):
            # this address is the problem: record it against the lead and move on
            db.update_lead(lead_id, {"send_status": "failed"})
            db.add_event(lead_id, "send_failed", str(e)[:300])
            raise SendError(f"Sending failed: {e}", 502)
        # our side is the problem (login, connection, quota): the lead did nothing wrong,
        # so it stays ready to send once that's fixed
        raise SendError(f"Sending failed: {e}", 503)

    now = schema.now_iso()
    fields = {"send_status": "sent", "last_sent_at": now, "email_message_id": msg["Message-ID"]}
    if not lead["sent_at"]:
        fields["sent_at"] = now
    if lead["stage"] == "new":
        fields["stage"] = "contacted"
    db.update_lead(lead_id, fields)
    detail = {"subject": subject, "message_id": msg["Message-ID"],
              "thread_id": sent.get("threadId", "") if isinstance(sent, dict) else ""}
    if inboxes.current_id() != "main":
        detail["inbox"] = inboxes.current_id()
        detail["from"] = from_address()
    db.add_event(lead_id, "email_sent", json.dumps(detail))
    return {"ok": True, "subject": subject}


def mark_sent(lead_id, channel="", followup=False):
    """Record that you sent a message by hand (Yelp, Facebook, Instagram, WhatsApp, SMS...).
    followup=True records a follow-up nudge instead of the first message."""
    lead = db.get_lead(lead_id)
    if lead is None:
        raise SendError("Lead not found", 404)
    now = schema.now_iso()
    if followup:
        db.update_lead(lead_id, {"follow_up_count": lead["follow_up_count"] + 1, "last_sent_at": now})
        db.add_event(lead_id, "manual_followup", channel or lead["channel"])
        return {"ok": True}
    fields = {"send_status": "sent", "last_sent_at": now}
    if not lead["sent_at"]:
        fields["sent_at"] = now
    if lead["stage"] == "new":
        fields["stage"] = "contacted"
    db.update_lead(lead_id, fields)
    db.add_event(lead_id, "manual_sent", channel or lead["channel"])
    return {"ok": True}


# -------------------------------------------------------------- follow-ups

def _followup_cutoff(settings):
    return (datetime.now(timezone.utc) - timedelta(days=int(settings["followup_delay_days"]))
            ).isoformat(timespec="seconds")


MAX_SEQUENCE_STEPS = 10


def followup_plan(campaign="", settings=None):
    """The follow-up sequence for a campaign: [(days to wait after the previous message, text), ...].
    Text '' means the AI writes that step. From the campaign's own steps, else the account's steps
    (Settings > Messaging), else the single follow-up message repeated `max_followups` times."""
    eff = db.effective_settings(campaign) if settings is None else settings
    steps = [x for x in (eff.get("followup_steps") or []) if isinstance(x, dict)]
    if steps:
        out = []
        for x in steps[:MAX_SEQUENCE_STEPS]:
            try:
                days = max(1, min(365, int(x.get("days") or 1)))
            except (TypeError, ValueError):
                days = 3
            out.append((days, str(x.get("body") or "").strip()))
        return out
    return [(int(eff["followup_delay_days"]), (eff.get("followup_template") or "").strip())] * int(eff["max_followups"])


def _due_by_plan(rows):
    """Keep the leads whose next step's wait has passed (each campaign can have its own sequence)."""
    plans, now, out = {}, datetime.now(timezone.utc), []
    for r in rows:
        camp = r["campaign"] or ""
        if camp not in plans:
            plans[camp] = followup_plan(camp)
        plan, step = plans[camp], r["follow_up_count"] or 0
        if step >= len(plan):
            continue
        try:
            last = datetime.fromisoformat(r["last_sent_at"])
        except (TypeError, ValueError):
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now - last >= timedelta(days=plan[step][0]):
            out.append(dict(r))
    return out


def followups_due(settings=None):
    """Emailed leads who haven't answered and are due their next follow-up (sent as a reply in
    the same thread). Each campaign's own sequence decides how many and how far apart."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE email != '' AND email_message_id != '' AND send_status = 'sent'
                 AND stage = 'contacted' AND do_not_contact = 0
                 AND follow_up_count < ? AND last_sent_at IS NOT NULL
               ORDER BY last_sent_at""", (MAX_SEQUENCE_STEPS,)).fetchall()
    return _due_by_plan(rows)


def hand_followups_due(settings=None):
    """The same, for leads you messaged by hand (WhatsApp, Instagram, Facebook, Yelp, SMS):
    contacted, no reply recorded, and the next step's wait has passed."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE email_message_id = '' AND send_status = 'sent' AND channel != 'unknown'
                 AND stage = 'contacted' AND do_not_contact = 0
                 AND follow_up_count < ? AND last_sent_at IS NOT NULL
               ORDER BY last_sent_at""", (MAX_SEQUENCE_STEPS,)).fetchall()
    return _due_by_plan(rows)


def _first_email(lead_id):
    """What was recorded when this lead was first emailed (subject, message id, Gmail thread)."""
    for ev in reversed(db.list_events(lead_id, limit=100)):  # oldest first
        if ev["kind"] == "email_sent":
            try:
                return json.loads(ev["detail"]) or {}
            except ValueError:
                return {}
    return {}


def _original_subject(lead_id):
    return _first_email(lead_id).get("subject", "")


def followup_text(lead, settings=None):
    """The nudge to send this lead: your own follow-up message (Settings > Messaging) with the
    blanks filled in, or -- when that's empty -- a short one written by the AI."""
    settings = settings or db.effective_settings(lead.get("campaign") or "")
    plan = followup_plan(lead.get("campaign") or "", settings)
    step = lead.get("follow_up_count") or 0
    fixed = plan[step][1] if step < len(plan) else (settings.get("followup_template") or "").strip()
    if fixed:
        return final_message(lead, _clean_message(fixed, "phone"))
    if not llm.is_configured("draft"):
        raise SendError("There's no follow-up message to send: write one under Settings > Messaging, "
                        "or add an AI key (Settings > Connections) so one can be written for each lead.", 400)
    note = CHANNEL_NOTES.get(lead.get("channel"), "") if lead.get("channel") != "email" else ""
    prompt = (
        "Write a brief, friendly follow-up (1-2 sentences) to the earlier outreach message below, "
        "since there's been no reply. Light and low-pressure; don't repeat the whole pitch. "
        f"No subject line. {note}\n"
        f"Earlier message:\n{schema.fill_placeholders(lead['message'], lead)[0]}\n\n"
        f"Business: {lead['business_name']}\n"
        "Return only the follow-up text, nothing else."
    )
    try:
        text = _clean_message(llm.complete(prompt, task="draft", max_tokens=400, temperature=0.8), "phone")
    except llm.LLMError as e:
        raise SendError(f"Couldn't write the follow-up: {e}", 502)
    return final_message(lead, text)


def send_followup(lead_id):
    settings = db.get_settings()
    lead = db.get_lead(lead_id)
    _check_can_email(lead, settings)
    plan = followup_plan(lead["campaign"] or "")
    if lead["follow_up_count"] >= len(plan):
        raise SendError(f"Already sent all {len(plan)} follow-ups in this sequence.", 400)
    if not lead["email_message_id"]:
        raise SendError("This lead hasn't been emailed yet.", 400)
    first = _first_email(lead_id)
    ib, ok = pick_inbox(settings, first.get("inbox") or "main")
    if not ok:
        raise SendError("The inbox this lead was first emailed from has reached its limit for today.", 429)
    with inboxes.using(ib):
        return _send_followup_as(lead_id, lead, settings, first)


def _send_followup_as(lead_id, lead, settings, first):
    text = followup_text(lead)
    orig = first.get("subject") or f"Quick question for {lead['business_name'] or 'you'}"
    subject = orig if orig.lower().startswith("re:") else f"Re: {orig}"
    msg = build_email(lead, subject, text, settings, in_reply_to=lead["email_message_id"])
    msg.gmail_thread_id = first.get("thread_id") or None     # same conversation in Gmail
    try:
        _deliver(msg)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "recipient", False):
            db.update_lead(lead_id, {"send_status": "failed"})
            db.add_event(lead_id, "send_failed", str(e)[:300])
            raise SendError(f"Sending failed: {e}", 502)
        raise SendError(f"Sending failed: {e}", 503)

    db.update_lead(lead_id, {"follow_up_count": lead["follow_up_count"] + 1,
                             "last_sent_at": schema.now_iso()})
    detail = {"subject": subject, "message_id": msg["Message-ID"], "text": text}
    if inboxes.current_id() != "main":
        detail["inbox"] = inboxes.current_id()
    db.add_event(lead_id, "followup_sent", json.dumps(detail))
    return {"ok": True, "message": text}


# ---------------------------------------------------------- reply detection

_OPTOUT_RE = re.compile(
    r"(\b(unsubscribe|remove me|opt[\s-]?out|do not (contact|email)|don'?t (contact|email)|"
    r"take me off|no more (emails|messages)|stop (e-?mailing|contacting|messaging|texting|calling|sending))\b)"
    r"|(^\s*stop\s*[.!]*\s*$)",
    re.I | re.M,
)
_QUOTE_START = re.compile(r"^\s*(on .{5,200} wrote:|-{2,}\s*original message|from:\s.+@|sent from my)", re.I)


def strip_quoted(text, footer=""):
    """Keep only what the person actually wrote: drop quoted lines, the quoted
    original, and our own opt-out footer."""
    out = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith(">"):
            continue
        if _QUOTE_START.match(line) and out:
            break
        out.append(line)
    cleaned = "\n".join(out)
    if footer:
        cleaned = cleaned.replace(footer, "")
    return cleaned.strip()


def is_optout(reply_text, footer=""):
    return bool(_OPTOUT_RE.search(strip_quoted(reply_text, footer)[:600]))


def _is_auto_reply(msg):
    subj = (msg.get("Subject") or "").lower()
    return (
        (msg.get("Auto-Submitted") or "no").lower() != "no"
        or bool(msg.get("X-Autoreply") or msg.get("X-Autorespond"))
        or (msg.get("Precedence") or "").lower() in ("bulk", "auto_reply", "junk")
        or subj.startswith(("automatic reply", "auto:", "out of office", "autoreply"))
    )


def _body_text(msg):
    parts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="ignore"))
    return "\n".join(parts)


def _lead_for_reply(msg, from_addr):
    """Match an inbound message to a lead: by threading headers first, then by sender."""
    refs = " ".join(filter(None, [msg.get("In-Reply-To"), msg.get("References")]))
    ids = re.findall(r"<[^>]+>", refs)
    with db.get_conn() as conn:
        for mid in ids:
            ev = conn.execute(
                "SELECT lead_id FROM events WHERE kind IN ('email_sent','followup_sent') AND detail LIKE ? LIMIT 1",
                (f"%{mid}%",)).fetchone()
            if ev:
                return db.get_lead(ev["lead_id"])
        row = conn.execute(
            "SELECT * FROM leads WHERE lower(email) = ? AND send_status = 'sent' LIMIT 1", (from_addr,)
        ).fetchone()
        if row:
            return dict(row)
        # Someone else at the same business answering -- the owner replying to what reached
        # info@, a forwarded email with no thread headers. Matched on the company's own domain
        # (never gmail.com and the like), and only when exactly one emailed lead has it.
        domain = from_addr.rpartition("@")[2]
        if domain and domain not in FREE_MAIL_DOMAINS:
            rows = conn.execute(
                "SELECT * FROM leads WHERE send_status = 'sent' AND (website_domain = ? OR lower(email) LIKE ?) "
                "LIMIT 2", (domain, "%@" + domain)).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
        # Last: the owner answering from a personal address (gmail.com and the like) with a "Re:"
        # of a subject we sent to exactly one lead. A different company's address never matches.
        subject = _bare_subject(msg.get("Subject", ""))
        if domain in FREE_MAIL_DOMAINS and subject and len(subject) >= 12 and re.match(r"(?i)\s*(re|aw|sv|antw)\s*:", msg.get("Subject", "") or ""):
            hits = {r["lead_id"] for r in conn.execute(
                "SELECT lead_id FROM events WHERE kind IN ('email_sent','followup_sent') AND detail LIKE ? LIMIT 3",
                (f'%"subject": {json.dumps(subject)[:-1]}%',)).fetchall()}
            if len(hits) == 1:
                lead = db.get_lead(hits.pop())
                if lead and lead.get("send_status") == "sent":
                    return lead
    return None


FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "ymail.com", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com", "yandex.ru",
    "rediffmail.com", "qq.com", "163.com", "126.com",
}


def _bare_subject(subject):
    """'Re: RE: Fwd: Quick question' -> 'Quick question'."""
    s = (subject or "").strip()
    while True:
        t = re.sub(r"(?i)^\s*(re|aw|sv|antw|fw|fwd)\s*:\s*", "", s)
        if t == s:
            return s
        s = t


_BOUNCE_WORDS = ("undeliverable", "undelivered", "delivery status notification (failure)", "failure notice",
                 "returned to sender", "address not found", "couldn't be delivered", "could not be delivered",
                 "delivery has failed", "mail delivery failed", "delivery failure")


def _is_bounce(msg, from_addr=""):
    """A delivery report from a mail server, not a person."""
    local = (from_addr or "").split("@")[0]
    ctype = (msg.get("Content-Type") or "").lower()
    return (local in ("mailer-daemon", "postmaster") or bool(msg.get("X-Failed-Recipients"))
            or ("multipart/report" in ctype and "delivery-status" in ctype))


def _bounce_details(msg, raw_text):
    """(addresses that failed, True if it's permanent). A "still trying" notice is not a failure."""
    low = raw_text.lower()
    addrs = [a.strip() for a in (msg.get("X-Failed-Recipients") or "").lower().split(",") if a.strip()]
    addrs += re.findall(r"(?:final|original)-recipient:\s*rfc822;\s*<?([^\s;<>]+@[^\s;<>]+)", low)
    action = re.search(r"^action:\s*(\w+)", low, re.M)
    status = re.search(r"^status:\s*([245])\.\d+\.\d+", low, re.M)
    if action and action.group(1) in ("delayed", "delivered", "relayed", "expanded"):
        hard = False
    elif status:
        hard = status.group(1) == "5"
    else:
        hard = bool(action and action.group(1) == "failed") or any(
            w in (msg.get("Subject") or "").lower() for w in _BOUNCE_WORDS)
    return list(dict.fromkeys(schema.norm_email(a) for a in addrs if schema.norm_email(a))), hard


def _lead_for_bounce(msg, addrs, raw_text):
    with db.get_conn() as conn:
        for a in addrs:
            row = conn.execute("SELECT * FROM leads WHERE lower(email) = ? AND send_status = 'sent' LIMIT 1",
                               (a,)).fetchone()
            if row:
                return dict(row)
    lead = _lead_for_reply(msg, "")
    if lead:
        return lead
    with db.get_conn() as conn:                    # the report quotes the message it couldn't deliver
        for mid in list(dict.fromkeys(re.findall(r"<[^<>\s]+@[^<>\s]+>", raw_text)))[:40]:
            ev = conn.execute(
                "SELECT lead_id FROM events WHERE kind IN ('email_sent','followup_sent') AND detail LIKE ? LIMIT 1",
                (f"%{mid}%",)).fetchone()
            if ev:
                return db.get_lead(ev["lead_id"])
    return None


def process_inbound(raw_bytes, footer=""):
    """Handle one inbound email. Returns 'replied', 'optout', 'auto', 'bounced', or None if it isn't ours."""
    msg = email.message_from_bytes(raw_bytes)
    from_addr = parseaddr(msg.get("From", ""))[1].strip().lower()
    msgid = (msg.get("Message-ID") or "").strip()

    if _is_bounce(msg, from_addr):
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        addrs, hard = _bounce_details(msg, raw_text)
        lead = _lead_for_bounce(msg, addrs, raw_text)
        if not lead:
            return None
        if not hard:
            return "auto"                      # "delayed, still trying" -- nothing to act on yet
        if msgid and db.event_exists("bounced", msgid):
            return None
        # The address is dead: stop following up, and show the lead as failed so it can be
        # reached another way (or have its email corrected).
        db.add_event(lead["id"], "bounced", json.dumps(
            {"address": (addrs or [lead.get("email", "")])[0], "message_id": msgid,
             "subject": msg.get("Subject", "")}))
        db.update_lead(lead["id"], {"send_status": "failed"})
        return "bounced"

    if not from_addr:
        return None
    lead = _lead_for_reply(msg, from_addr)
    if not lead:
        return None
    if msgid and db.event_exists("reply", msgid):
        return None  # already processed on an earlier check
    if _is_auto_reply(msg):
        return "auto"

    body = _body_text(msg)
    optout = is_optout(body, footer)
    snippet = strip_quoted(body, footer)[:300]
    db.add_event(lead["id"], "reply", json.dumps(
        {"from": from_addr, "subject": msg.get("Subject", ""), "message_id": msgid, "snippet": snippet}))
    if lead["stage"] in ("new", "contacted"):
        db.update_lead(lead["id"], {"stage": "replied"})
    if optout:
        db.add_suppression(from_addr, "requested via reply")
        db.update_lead(lead["id"], {"stage": "dead", "dnc_reason": "requested via reply",
                                    "reply_category": "not_interested"})
        db.add_event(lead["id"], "optout", from_addr)
        return "optout"
    db.update_lead(lead["id"], {"reply_category": classify_reply(snippet or body)})
    return "replied"


REPLY_CATEGORIES = ("interested", "question", "not_now", "wrong_person", "not_interested", "other")
_REPLY_RULES = [   # checked in order; the first that matches wins (used when there's no AI key)
    ("not_interested", r"\b(not interested|no thanks|no thank you|not for us|please remove|unsubscribe|don'?t (?:contact|email)|"
                       r"we(?:'re| are) (?:all )?(?:set|good|covered)|already have (?:a|an|one|someone)|no need)\b"),
    ("wrong_person", r"\b(contact|speak to|reach out to|email|talk to|forward(?:ed)? (?:this|it) to)\b.{0,40}"
                     r"\b(manager|owner|director|colleague|marketing|partner|doctor|dr\.?|person in charge)\b|"
                     r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}"),
    ("not_now", r"\b(not (?:right )?now|later|next (?:month|quarter|year|week)|after (?:ramadan|eid|the holidays)|"
                r"busy (?:right now|at the moment)|maybe in|check back|circle back|get back to you)\b"),
    ("interested", r"\b(interested|sounds good|let'?s (?:talk|chat|meet)|call me|give me a call|book|schedule|"
                   r"set up a (?:call|meeting)|send (?:me )?(?:more )?(?:details|info)|tell me more|yes)\b"),
    ("question", r"\?|\b(how much|price|pricing|cost|what does|how does)\b"),
]


def classify_reply(text):
    """Sort a reply into one of REPLY_CATEGORIES so the hot ones are answered first. Uses the AI
    when a key is set (it reads tone and context), else simple word rules. Never raises."""
    text = (text or "").strip()
    if not text:
        return "other"
    if llm.is_configured("filter"):
        try:
            reply = llm.complete(
                "Classify this reply to a cold sales email into exactly one category:\n"
                "interested (wants to talk / a call / details), question (asks something, e.g. price, before deciding), "
                "not_now (maybe later), wrong_person (points to someone else), not_interested (declines), other.\n\n"
                f"REPLY:\n{text[:1500]}\n\nAnswer with the category word only.",
                task="filter", max_tokens=20, temperature=0)
            word = re.sub(r"[^a-z_]", "", (reply or "").strip().lower().split()[0] if (reply or "").strip() else "")
            if word in REPLY_CATEGORIES:
                return word
        except Exception:  # noqa: BLE001 -- the word rules below are a fine fallback
            pass
    low = text.lower()
    for cat, pattern in _REPLY_RULES:
        if re.search(pattern, low):
            return cat
    return "other"


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_INBOX_SCAN = 3000        # most messages looked at in one check (the newest ones)
_HEADER_FIELDS = ("FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES AUTO-SUBMITTED PRECEDENCE "
                  "CONTENT-TYPE X-FAILED-RECIPIENTS X-AUTOREPLY X-AUTORESPOND")


def _worth_reading(header_bytes):
    """From just the headers: could this be a reply to (or a bounce of) something we sent?"""
    hdr = email.message_from_bytes(header_bytes)
    from_addr = parseaddr(hdr.get("From", ""))[1].strip().lower()
    return _is_bounce(hdr, from_addr) or (bool(from_addr) and _lead_for_reply(hdr, from_addr) is not None)


def check_replies():
    """Look through every connected inbox (the main one and any extra sending inboxes) for
    replies to -- and bounces of -- emails we sent.

    Read or unread makes no difference -- a reply you already opened on your phone still counts.
    Nothing is marked as read. Each check carries on from where the last one stopped, so after
    the first run only new mail is looked at."""
    targets = [None] if imap_configured() else []
    for ib in inboxes.extra():
        if ib.get("enabled", True):
            with inboxes.using(ib):
                if imap_configured():
                    targets.append(ib)
    if not targets:
        raise SendError("Reply checking isn't set up: add your email details under Settings > Connections.", 400)
    total = {"checked": 0, "replied": 0, "suppressed": 0, "auto_replies": 0, "bounced": 0}
    problems = []
    for ib in targets:
        with inboxes.using(ib):
            try:
                c = _check_one_inbox()
            except SendError as e:
                if len(targets) == 1:
                    raise
                problems.append(f"{(ib or {}).get('label') or from_address()}: {e}")
                continue
        for k in total:
            total[k] += c.get(k, 0)
    db.set_settings({"last_reply_check_at": schema.now_iso()})
    if problems:
        total["problems"] = problems
    return total


def _check_one_inbox():
    """Look through the inbox for replies to (and bounces of) emails we sent.

    Read or unread makes no difference -- a reply you already opened on your phone still counts.
    Nothing is marked as read. Each check carries on from where the last one stopped, so after
    the first run only new mail is looked at."""
    settings = db.get_settings()
    iid = inboxes.current_id()
    since = datetime.now(timezone.utc) - timedelta(days=45)
    if settings.get("sending_started_at"):
        try:
            since = max(since, datetime.fromisoformat(settings["sending_started_at"]) - timedelta(days=1))
        except ValueError:
            pass
    since_str = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
    counts = {"checked": 0, "replied": 0, "suppressed": 0, "auto_replies": 0, "bounced": 0}
    imap_host = config.env("IMAP_HOST", "imap.gmail.com")
    user, imap_pw = _imap_login()
    try:
        netguard.check_mail_host(imap_host, 993)
    except netguard.Blocked as e:
        raise SendError(str(e), 400)
    state = (settings.get("imap_state") if iid == "main" else _inbox_state(settings, iid).get("imap_state")) or {}
    new_state = None
    try:
        mail = imaplib.IMAP4_SSL(imap_host, timeout=30)
        try:
            mail.login(user, imap_pw)
            mail.select("INBOX", readonly=True)
            try:
                validity = (mail.response("UIDVALIDITY")[1][0] or b"").decode()
            except Exception:  # noqa: BLE001
                validity = ""
            status, data = mail.uid("SEARCH", None, "SINCE", since_str)
            if status != "OK":
                raise SendError("Searching the inbox failed.", 502)
            uids = sorted(int(x) for x in (data[0] or b"").split())
            same_box = (state.get("user"), state.get("host"), str(state.get("validity"))) == (user, imap_host, validity)
            last = int(state.get("last_uid") or 0) if same_box else 0
            todo = [u for u in uids if u > last][-MAX_INBOX_SCAN:]
            for start in range(0, len(todo), 100):
                chunk = todo[start:start + 100]
                status, items = mail.uid("FETCH", ",".join(map(str, chunk)),
                                         f"(UID BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])")
                if status != "OK":
                    continue
                for item in items or []:
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue
                    m = re.search(rb"UID (\d+)", item[0] or b"")
                    if not m:
                        continue
                    counts["checked"] += 1
                    if not _worth_reading(item[1] or b""):
                        continue
                    status, full = mail.uid("FETCH", m.group(1).decode(), "(BODY.PEEK[])")
                    raw = next((x[1] for x in (full or []) if isinstance(x, tuple) and len(x) > 1), None)
                    if status != "OK" or not raw:
                        continue
                    result = process_inbound(raw, settings.get("email_footer", ""))
                    if result == "replied":
                        counts["replied"] += 1
                    elif result == "optout":
                        counts["replied"] += 1
                        counts["suppressed"] += 1
                    elif result == "auto":
                        counts["auto_replies"] += 1
                    elif result == "bounced":
                        counts["bounced"] += 1
            new_state = {"user": user, "host": imap_host, "validity": validity,
                         "last_uid": max(uids) if uids else last}
        finally:
            try:
                mail.logout()
            except Exception:  # noqa: BLE001
                pass
    except (imaplib.IMAP4.error, OSError) as e:
        raise SendError(f"Couldn't check the inbox: {e}", 502)
    if new_state and new_state != state:
        if iid == "main":
            db.set_settings({"imap_state": new_state})
        else:
            _put_inbox_state(iid, imap_state=new_state)
    return counts


def auto_check_replies(now=None):
    """Called on a timer for each account: check the inbox if it's connected, checking is switched
    on (Settings > Sending) and the chosen number of minutes has passed. Returns the counts, or None."""
    if not imap_configured():
        return None
    settings = db.get_settings()
    every = int(settings.get("reply_check_minutes") or 0)
    if every <= 0:
        return None
    last = settings.get("last_reply_check_at")
    if last:
        try:
            if (now or datetime.now(timezone.utc)) - datetime.fromisoformat(last) < timedelta(minutes=every):
                return None
        except ValueError:
            pass
    try:
        return check_replies()
    except SendError:
        db.set_settings({"last_reply_check_at": schema.now_iso()})   # don't hammer a mailbox that's refusing us
        return None


# ---------------------------------------------------------------- job entries

def run_draft(ctx, params):
    filters = {"dnc": "no"}
    if params.get("ids"):
        filters["ids"] = params["ids"]
    else:
        filters["not_rejected"] = True
        filters["reachable"] = True          # no point writing to someone there's no way to reach
    if params.get("only_fit"):
        filters.pop("not_rejected", None)
        filters["fit"] = "1"
    if params.get("campaign"):
        filters["campaign"] = params["campaign"]
    if params.get("channel"):
        filters["channel"] = params["channel"]
    if not params.get("force"):
        filters["drafted"] = "no"
    exact = params.get("exact")
    ids = db.select_ids(filters, limit=params.get("limit"))
    ctx.progress(0, len(ids))
    ctx.log(f"Drafting {len(ids)} messages" + (" from your templates exactly as written (no AI)" if exact else ""))
    ok = failed = blanks = 0
    no_template = 0
    for i, lead_id in enumerate(ids, 1):
        if ctx.stopped:
            ctx.log("Stopped.")
            break
        try:
            res = draft_message(lead_id, params.get("template", ""), index=i - 1, exact=exact)
            ok += 1
            if res.get("unfilled"):
                blanks += 1
                ctx.log(f"Lead {lead_id}: drafted, but {', '.join(res['unfilled'][:3])} had nothing to fill it "
                        f"-- it won't send until that's fixed (or give it a fallback: {{first_name|there}})")
        except Exception as e:  # noqa: BLE001
            failed += 1
            if isinstance(e, SendError) and "template to fill in" in str(e):
                no_template += 1
                if no_template <= 3:
                    ctx.log(f"Lead {lead_id}: {e}")
            else:
                ctx.log(f"Lead {lead_id}: {e}")
            if isinstance(e, llm.LLMError) and "not set" in str(e):
                raise
        ctx.progress(i)
    if no_template > 3:
        ctx.log(f"...and {no_template - 3} more leads had no template for their channel.")
    return (f"{ok} drafted" + (f", {blanks} with a blank still to fill" if blanks else "")
            + (f", {failed} failed" if failed else ""))


def _our_side_down(ctx, e):
    ctx.log(f"Stopping: {e}")
    ctx.log("Nothing was marked as failed -- fix the problem above and start sending again.")


# ---------------------------------------------------------------- send plans
MAX_PLAN_DAYS = 90


def _local_day(settings, now=None):
    return (now or datetime.now(timezone.utc)).astimezone(db.local_zone(settings)).date()


def make_send_plan(count, mode="once", days=1, campaign="", only_fit=False):
    """What to send: `count` emails, once now -- or every day for `days` days (today included).
    A daily plan raises the account's daily limit to `count` if it was lower; the warm-up still
    applies. Returns the plan saved (daily) or the run's parameters (once)."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise SendError("Enter how many emails to send, e.g. 200.", 400)
    if not 1 <= count <= 100000:
        raise SendError("Send between 1 and 100,000 emails.", 400)
    params = {"campaign": campaign or "", "only_fit": bool(only_fit), "max": count}
    if mode == "once":
        return {"mode": "once", "params": params}
    if mode != "daily":
        raise SendError("Choose once or every day.", 400)
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise SendError("Enter for how many days, e.g. 10.", 400)
    if not 1 <= days <= MAX_PLAN_DAYS:
        raise SendError(f"A daily plan runs for 1 to {MAX_PLAN_DAYS} days.", 400)
    settings = db.get_settings()
    today = _local_day(settings)
    plan = {"count": count, "days": days, "campaign": campaign or "", "only_fit": bool(only_fit),
            "start_day": today.isoformat(), "until_day": (today + timedelta(days=days - 1)).isoformat(),
            "last_day": "", "runs": 0, "active": True}
    changes = {"send_plan": plan}
    if int(settings["email_daily_limit"]) < count:
        changes["email_daily_limit"] = count
    db.set_settings(changes)
    return {"mode": "daily", "plan": plan, "raised_limit": "email_daily_limit" in changes}


def stop_send_plan():
    plan = dict(db.get_settings().get("send_plan") or {})
    if plan:
        plan["active"] = False
        db.set_settings({"send_plan": plan})
    return plan


def send_plan_status(settings=None):
    """The repeating send as the Outreach page shows it ({} when there is none)."""
    settings = settings or db.get_settings()
    plan = dict(settings.get("send_plan") or {})
    if not plan:
        return {}
    today = _local_day(settings).isoformat()
    if plan.get("active") and today > plan.get("until_day", ""):
        plan["active"] = False
        plan["finished"] = True
    plan["ran_today"] = plan.get("last_day") == today
    return plan


def daily_send_due(settings=None, now=None):
    """The send-run parameters for today's share of the daily plan, or None. Marks the day as done,
    so a plan runs at most once a day -- inside the sending hours when those are switched on."""
    settings = settings or db.get_settings()
    plan = dict(settings.get("send_plan") or {})
    if not plan.get("active"):
        return None
    today = _local_day(settings, now).isoformat()
    if today > plan.get("until_day", ""):
        plan["active"] = False
        db.set_settings({"send_plan": plan})
        return None
    if plan.get("last_day") == today or today < plan.get("start_day", today):
        return None
    if not send_window(settings, now)[0] or not smtp_configured():
        return None
    return {"campaign": plan.get("campaign", ""), "only_fit": bool(plan.get("only_fit")), "max": int(plan["count"]),
            "plan_day": today}


def mark_plan_ran(day):
    plan = dict(db.get_settings().get("send_plan") or {})
    if plan:
        plan["last_day"] = day
        plan["runs"] = int(plan.get("runs") or 0) + 1
        db.set_settings({"send_plan": plan})


def run_send_bulk(ctx, params):
    """Send drafted emails one by one, paced, until the daily limit is hit."""
    settings = db.get_settings()
    ok, why = send_window(settings)
    if not ok:
        raise SendError(why)
    halt = too_many_bounces(settings)
    if halt:
        raise SendError(halt)
    filters = {"send_status": "unsent", "channel": "email", "drafted": "yes", "dnc": "no", "open_stage": True}
    if params.get("only_fit"):
        filters["fit"] = "1"
    else:
        filters["not_rejected"] = True
    if params.get("campaign"):
        filters["campaign"] = params["campaign"]
    if params.get("ids"):
        filters["ids"] = params["ids"]
    ids = db.select_ids(filters)
    ctx.progress(0, len(ids))
    ctx.log(f"{len(ids)} emails queued (limit today: {total_daily_limit(settings)}, "
            f"already sent today: {emails_sent_today(settings)})"
            + (f", at most {params['max']} this run" if params.get("max") else ""))
    cap = provider_daily_cap()
    if cap and effective_daily_limit(settings) > cap:
        ctx.log(f"Note: your mail provider allows about {cap:,} emails a day per account; your limit is higher, so "
                "it may start refusing before you reach it. For more, use a sending service (Amazon SES, SendGrid, "
                "Mailgun, Brevo) under Settings > Connections.")
    sent = failed = skipped = 0
    stuck = None          # a lead whose send failed for a reason that may be ours, not theirs
    with smtp_session():
        sent, failed, skipped, stuck = _bulk_loop(ctx, ids, settings, int(params.get("max") or 0))
    if stuck is not None:
        ctx.log(f"Lead {stuck[0]} was left unsent: {stuck[1]}")
    return f"{sent} sent, {failed} failed, {skipped} skipped"


@contextmanager
def _nullcontext():
    yield


def _bulk_loop(ctx, ids, settings, most=0):
    sent = failed = skipped = 0
    stuck = None
    for i, lead_id in enumerate(ids, 1):
        if most and sent >= most:
            ctx.log(f"Sent the {most} you asked for in this run -- the rest stay queued.")
            break
        if ctx.stopped:
            ctx.log("Stopped.")
            break
        if not send_window(settings)[0]:
            ctx.log("Your sending hours have ended for today -- the rest stay queued.")
            break
        try:
            send_lead_email(lead_id)
            sent += 1
            ctx.log(f"Sent to lead {lead_id}")
            ctx.progress(i)
            if stuck is not None:
                # the very next email went through, so the server is fine: that one message was refused
                db.update_lead(stuck[0], {"send_status": "failed"})
                db.add_event(stuck[0], "send_failed", stuck[1][:300])
                failed += 1
                stuck = None
            if sent % 50 == 0:
                halt = too_many_bounces(settings)
                if halt:
                    ctx.log(halt)
                    break
            if i < len(ids) and ctx.sleep(send_delay(settings)):
                break
        except SendError as e:
            ctx.progress(i)
            if e.code == 429:
                ctx.log(str(e))
                break
            if e.code == 503:
                if stuck is not None or not smtp_configured():
                    _our_side_down(ctx, e)        # two in a row: it's the connection, not the leads
                    stuck = None
                    break
                stuck = (lead_id, str(e))
                ctx.log(f"Lead {lead_id}: {e} -- trying one more before stopping")
                continue
            if e.code == 502:
                failed += 1
            else:
                skipped += 1
            ctx.log(f"Lead {lead_id}: {e}")
    return sent, failed, skipped, stuck


def run_followups(ctx, params):
    """Send every email follow-up that's due, paced like a normal send. The inbox is checked
    first (when it's connected), so nobody who has already answered gets a nudge."""
    settings = db.get_settings()
    ok, why = send_window(settings)
    if not ok:
        raise SendError(why)
    if imap_configured():
        try:
            c = check_replies()
            ctx.log(f"Checked the inbox first: {c['replied']} new replies, {c['bounced']} bounced.")
        except SendError as e:
            ctx.log(f"Couldn't check the inbox first ({e}) -- carrying on.")
    due = [l for l in followups_due(settings) if not params.get("campaign") or l["campaign"] == params["campaign"]]
    ctx.progress(0, len(due))
    ctx.log(f"{len(due)} follow-ups due")
    halt = too_many_bounces(settings)
    if halt:
        raise SendError(halt)
    with smtp_session():
        return _followup_loop(ctx, due, settings)


def _followup_loop(ctx, due, settings):
    sent = failed = skipped = 0
    for i, lead in enumerate(due, 1):
        if ctx.stopped:
            ctx.log("Stopped.")
            break
        if not send_window(settings)[0]:
            ctx.log("Your sending hours have ended for today -- the rest stay due.")
            break
        try:
            send_followup(lead["id"])
            sent += 1
            ctx.log(f"Followed up with {lead['business_name'] or lead['email']}")
            ctx.progress(i)
            if i < len(due) and ctx.sleep(send_delay(settings)):
                break
        except SendError as e:
            ctx.progress(i)
            if e.code == 429:
                ctx.log(str(e))
                break
            if e.code == 503 or "no follow-up message to send" in str(e):
                _our_side_down(ctx, e)
                break
            failed += e.code == 502
            skipped += e.code != 502
            ctx.log(f"{lead['business_name'] or lead['email']}: {e}")
    return f"{sent} follow-ups sent, {failed} failed, {skipped} skipped"
