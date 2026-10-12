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
import email
import imaplib
import json
import re
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, formataddr, parseaddr

import config
import db
import gmailapi
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

def smtp_configured():
    """Email can go out: an address to send from, plus either the Gmail API sign-in or an SMTP password."""
    return bool(config.env("SMTP_USER") and (gmailapi.configured() or config.env("SMTP_PASS")))


def email_transport():
    """How email leaves this account: "gmail_api", "smtp", or "" when it isn't set up."""
    if not config.env("SMTP_USER"):
        return ""
    return "gmail_api" if gmailapi.configured() else "smtp" if config.env("SMTP_PASS") else ""


def imap_configured():
    return bool((config.env("IMAP_USER") or config.env("SMTP_USER"))
                and (config.env("IMAP_PASS") or config.env("SMTP_PASS")))


WARMUP_SCHEDULE = [(3, 10), (7, 20), (14, 30)]  # (days since first send, daily cap)


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
    for threshold, cap in WARMUP_SCHEDULE:
        if days_in < threshold:
            return min(cap, limit)
    return limit


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
    eff = effective_daily_limit(settings)
    sent = emails_sent_today(settings)
    window_ok, window_why = send_window(settings)
    return {
        "window_open": window_ok, "window_note": window_why,
        "hand_followups_due": len(hand_followups_due(settings)),
        "reply_check_minutes": int(settings.get("reply_check_minutes") or 0),
        "last_reply_check_at": settings.get("last_reply_check_at") or "",
        "sent_today": sent,
        "daily_limit": eff,
        "max_limit": int(settings["email_daily_limit"]),
        "remaining": max(0, eff - sent),
        "warmup_active": bool(settings.get("warmup_enabled")) and eff < int(settings["email_daily_limit"]),
        "smtp_configured": smtp_configured(),
        "imap_configured": imap_configured(),
        "followups_due": len(followups_due(settings)),
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
    user = config.env("SMTP_USER")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.env("SMTP_FROM_NAME"), user)) if config.env("SMTP_FROM_NAME") else user
    msg["To"] = lead["email"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(user.split("@")[-1] if "@" in user else None))
    msg["List-Unsubscribe"] = f"<mailto:{user}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    footer = (settings.get("email_footer") or "").strip()
    msg.set_content(body + (f"\n\n--\n{footer}" if footer else ""))
    return msg


def _explain_connect_error(e, host, port):
    if isinstance(e, smtplib.SMTPException):
        return str(e)
    return (f"Couldn't connect to {host}:{port} ({e}). If this app runs on a free hosting plan, the host may block "
            "outgoing email ports (Render's free plan blocks 25, 465 and 587). Send through the Gmail API instead "
            "(it uses HTTPS, which isn't blocked) -- see 'Sending through the Gmail API' in the README.")


class DeliveryError(RuntimeError):
    """Sending failed. recipient=True: the mail server turned down THIS address (the lead's
    problem). Otherwise it's ours -- wrong password, blocked port, daily quota -- and every
    other send would fail the same way."""

    def __init__(self, message, recipient=False):
        super().__init__(message)
        self.recipient = recipient


def _deliver(msg):
    """Actually send the message. Through the Gmail API (HTTPS) when this account has a Gmail
    sign-in; otherwise over SMTP. Returns Gmail's {"id", "threadId"} for an API send, else None.
    A follow-up can carry msg.gmail_thread_id to stay in the original Gmail conversation."""
    if gmailapi.configured():
        try:
            return gmailapi.send(msg, getattr(msg, "gmail_thread_id", None))
        except gmailapi.GmailError as e:
            raise DeliveryError(str(e), recipient=e.recipient)
    host = config.env("SMTP_HOST", "smtp.gmail.com")
    port = config.env_int("SMTP_PORT", 587)
    user, pw = config.env("SMTP_USER"), config.env("SMTP_PASS")
    try:
        netguard.check_mail_host(host, port)
    except netguard.Blocked as e:
        raise DeliveryError(str(e))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)
    except smtplib.SMTPRecipientsRefused as e:
        why = "; ".join(f"{code} {(text.decode(errors='replace') if isinstance(text, bytes) else text)}"
                        for code, text in e.recipients.values())[:200]
        raise DeliveryError(f"The mail server refused this address ({why}).", recipient=True)
    except smtplib.SMTPAuthenticationError:
        raise DeliveryError("Your mail server refused the login. For Gmail use an App Password "
                            "(Settings > Connections > Email sending), not your normal password.")
    except (OSError, smtplib.SMTPException) as e:
        raise DeliveryError(_explain_connect_error(e, host, port))


def test_smtp():
    """Check the email details (the Gmail API sign-in, or the SMTP login). Sends nothing."""
    if not smtp_configured():
        raise SendError("Add your email address, plus a Gmail API refresh token or an App Password, first.", 400)
    if gmailapi.configured():
        try:
            return gmailapi.test()
        except gmailapi.GmailError as e:
            raise SendError(str(e), 400 if e.auth else 502)
    host = config.env("SMTP_HOST", "smtp.gmail.com")
    port = config.env_int("SMTP_PORT", 587)
    user, pw = config.env("SMTP_USER"), config.env("SMTP_PASS")
    try:
        netguard.check_mail_host(host, port)
    except netguard.Blocked as e:
        raise SendError(str(e), 400)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=12) as s:
                s.login(user, pw)
        else:
            with smtplib.SMTP(host, port, timeout=12) as s:
                s.starttls()
                s.login(user, pw)
    except smtplib.SMTPAuthenticationError:
        raise SendError("The mail server refused the login. For Gmail use an App Password (Google Account > Security > "
                        "2-Step Verification > App passwords), not your normal password.", 400)
    except (OSError, smtplib.SMTPException) as e:
        raise SendError(_explain_connect_error(e, host, port), 502)
    return {"ok": True, "message": f"Connected to {host} and logged in as {user}. (Nothing was sent.)"}


def test_imap():
    """Log in to the inbox used for reply checking. Reads nothing."""
    if not imap_configured():
        raise SendError("Add your email address and App Password first.", 400)
    host = config.env("IMAP_HOST", "imap.gmail.com")
    user = config.env("IMAP_USER") or config.env("SMTP_USER")
    pw = config.env("IMAP_PASS") or config.env("SMTP_PASS")
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
    eff = effective_daily_limit(settings)
    sent = emails_sent_today(settings)
    if sent >= eff:
        note = " (still warming up the account)" if eff < int(settings["email_daily_limit"]) else ""
        raise SendError(f"Daily email limit reached ({sent}/{eff}){note}. This protects your sending "
                        "reputation -- try again tomorrow.", 429)


def send_lead_email(lead_id, force=False):
    settings = db.get_settings()
    lead = db.get_lead(lead_id)
    _check_can_email(lead, settings)
    if not lead["message"].strip():
        raise SendError("Write or generate a message first.", 400)
    if lead["send_status"] == "sent" and not force:
        raise SendError("Already sent. Use a follow-up instead.", 400)
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
    db.add_event(lead_id, "email_sent", json.dumps({"subject": subject, "message_id": msg["Message-ID"],
                                                    "thread_id": sent.get("threadId", "") if isinstance(sent, dict) else ""}))
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


def followups_due(settings=None):
    """Emailed leads who haven't answered and are due a nudge (sent as a reply in the same thread)."""
    settings = settings or db.get_settings()
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE email != '' AND email_message_id != '' AND send_status = 'sent'
                 AND stage = 'contacted' AND do_not_contact = 0
                 AND follow_up_count < ? AND last_sent_at IS NOT NULL AND last_sent_at < ?
               ORDER BY last_sent_at""",
            (int(settings["max_followups"]), _followup_cutoff(settings)),
        ).fetchall()
    return [dict(r) for r in rows]


def hand_followups_due(settings=None):
    """The same, for leads you messaged by hand (WhatsApp, Instagram, Facebook, Yelp, SMS):
    contacted, no reply recorded, and the follow-up delay has passed."""
    settings = settings or db.get_settings()
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE email_message_id = '' AND send_status = 'sent' AND channel != 'unknown'
                 AND stage = 'contacted' AND do_not_contact = 0
                 AND follow_up_count < ? AND last_sent_at IS NOT NULL AND last_sent_at < ?
               ORDER BY last_sent_at""",
            (int(settings["max_followups"]), _followup_cutoff(settings)),
        ).fetchall()
    return [dict(r) for r in rows]


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
    fixed = (settings.get("followup_template") or "").strip()
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
    if lead["follow_up_count"] >= int(settings["max_followups"]):
        raise SendError(f"Already sent the maximum of {settings['max_followups']} follow-ups.", 400)
    if not lead["email_message_id"]:
        raise SendError("This lead hasn't been emailed yet.", 400)

    text = followup_text(lead)
    orig = _original_subject(lead_id) or f"Quick question for {lead['business_name'] or 'you'}"
    subject = orig if orig.lower().startswith("re:") else f"Re: {orig}"
    msg = build_email(lead, subject, text, settings, in_reply_to=lead["email_message_id"])
    msg.gmail_thread_id = _first_email(lead_id).get("thread_id") or None     # same conversation in Gmail
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
    db.add_event(lead_id, "followup_sent",
                 json.dumps({"subject": subject, "message_id": msg["Message-ID"], "text": text}))
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
    return dict(row) if row else None


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
        db.update_lead(lead["id"], {"stage": "dead", "dnc_reason": "requested via reply"})
        db.add_event(lead["id"], "optout", from_addr)
        return "optout"
    return "replied"


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
    """Look through the inbox for replies to (and bounces of) emails we sent.

    Read or unread makes no difference -- a reply you already opened on your phone still counts.
    Nothing is marked as read. Each check carries on from where the last one stopped, so after
    the first run only new mail is looked at."""
    if not imap_configured():
        raise SendError("Reply checking isn't set up: add your email details under Settings > Connections.", 400)
    settings = db.get_settings()
    since = datetime.now(timezone.utc) - timedelta(days=45)
    if settings.get("sending_started_at"):
        try:
            since = max(since, datetime.fromisoformat(settings["sending_started_at"]) - timedelta(days=1))
        except ValueError:
            pass
    since_str = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
    counts = {"checked": 0, "replied": 0, "suppressed": 0, "auto_replies": 0, "bounced": 0}
    imap_host = config.env("IMAP_HOST", "imap.gmail.com")
    user = config.env("IMAP_USER") or config.env("SMTP_USER")
    try:
        netguard.check_mail_host(imap_host, 993)
    except netguard.Blocked as e:
        raise SendError(str(e), 400)
    state = settings.get("imap_state") or {}
    new_state = None
    try:
        mail = imaplib.IMAP4_SSL(imap_host, timeout=30)
        try:
            mail.login(user, config.env("IMAP_PASS") or config.env("SMTP_PASS"))
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
    saved = {"last_reply_check_at": schema.now_iso()}
    if new_state and new_state != state:
        saved["imap_state"] = new_state
    db.set_settings(saved)
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


def run_send_bulk(ctx, params):
    """Send drafted emails one by one, paced, until the daily limit is hit."""
    settings = db.get_settings()
    ok, why = send_window(settings)
    if not ok:
        raise SendError(why)
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
    ctx.log(f"{len(ids)} emails queued (limit today: {effective_daily_limit(settings)}, "
            f"already sent today: {emails_sent_today(settings)})")
    sent = failed = skipped = 0
    stuck = None          # a lead whose send failed for a reason that may be ours, not theirs
    for i, lead_id in enumerate(ids, 1):
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
            if i < len(ids) and ctx.sleep(int(settings["email_send_delay_seconds"])):
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
    if stuck is not None:
        ctx.log(f"Lead {stuck[0]} was left unsent: {stuck[1]}")
    return f"{sent} sent, {failed} failed, {skipped} skipped"


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
            if i < len(due) and ctx.sleep(int(settings["email_send_delay_seconds"])):
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
