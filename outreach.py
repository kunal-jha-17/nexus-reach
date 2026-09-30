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
        "below; never invent details (services, years in business, reviews, prices). "
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


def draft_message(lead_id, selection="", index=0, settings=None):
    """Draft (or redraft) the message for one lead and save it."""
    lead = db.get_lead(lead_id)
    if lead is None:
        raise SendError("Lead not found", 404)
    # the lead's campaign may override the about-you text, templates and channel order
    settings = settings or db.effective_settings(lead["campaign"])
    name, body = resolve_template(settings.get("templates"), lead["channel"], selection, index)
    prompt = build_prompt(lead, settings, body, seed=index)
    text = _clean_message(llm.complete(prompt, task="draft", max_tokens=900, temperature=0.8),
                          lead["channel"])
    db.update_lead(lead_id, {"message": text, "template_used": name or ""})
    db.add_event(lead_id, "drafted", name or "no template")
    return {"message": text, "template_used": name or ""}


# ------------------------------------------------------------------- email

def smtp_configured():
    return bool(config.env("SMTP_USER") and config.env("SMTP_PASS"))


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


def emails_sent_today():
    return db.count_events_today(("email_sent", "followup_sent"))


def email_status(settings=None):
    settings = settings or db.get_settings()
    eff = effective_daily_limit(settings)
    sent = emails_sent_today()
    return {
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
            "outgoing email ports (Render's free plan blocks 25, 465 and 587). Use a host that allows email, or an "
            "email relay service that accepts port 2525.")


def _deliver(msg):
    """Actually send via SMTP (Gmail by default; use an App Password)."""
    host = config.env("SMTP_HOST", "smtp.gmail.com")
    port = config.env_int("SMTP_PORT", 587)
    user, pw = config.env("SMTP_USER"), config.env("SMTP_PASS")
    try:
        netguard.check_mail_host(host, port)
    except netguard.Blocked as e:
        raise RuntimeError(str(e))
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
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(_explain_connect_error(e, host, port))


def test_smtp():
    """Log in to the user's mail server to check the details. Sends nothing."""
    if not smtp_configured():
        raise SendError("Add your email address and App Password first.", 400)
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
        raise SendError("Email isn't set up: add SMTP_USER and SMTP_PASS (a Gmail App Password) to .env.", 400)
    eff = effective_daily_limit(settings)
    sent = emails_sent_today()
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

    subject, body = split_subject(lead["message"], lead)
    msg = build_email(lead, subject, body, settings)
    try:
        _deliver(msg)
    except Exception as e:  # noqa: BLE001
        db.update_lead(lead_id, {"send_status": "failed"})
        db.add_event(lead_id, "send_failed", str(e)[:300])
        raise SendError(f"Sending failed: {e}", 502)

    now = schema.now_iso()
    fields = {"send_status": "sent", "last_sent_at": now, "email_message_id": msg["Message-ID"]}
    if not lead["sent_at"]:
        fields["sent_at"] = now
    if lead["stage"] == "new":
        fields["stage"] = "contacted"
    db.update_lead(lead_id, fields)
    db.add_event(lead_id, "email_sent", json.dumps({"subject": subject, "message_id": msg["Message-ID"]}))
    return {"ok": True, "subject": subject}


def mark_sent(lead_id, channel=""):
    """Record that you sent a message by hand (Yelp, Facebook, Instagram, WhatsApp, SMS...)."""
    lead = db.get_lead(lead_id)
    if lead is None:
        raise SendError("Lead not found", 404)
    now = schema.now_iso()
    fields = {"send_status": "sent", "last_sent_at": now}
    if not lead["sent_at"]:
        fields["sent_at"] = now
    if lead["stage"] == "new":
        fields["stage"] = "contacted"
    db.update_lead(lead_id, fields)
    db.add_event(lead_id, "manual_sent", channel or lead["channel"])
    return {"ok": True}


# -------------------------------------------------------------- follow-ups

def followups_due(settings=None):
    settings = settings or db.get_settings()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(settings["followup_delay_days"]))
              ).isoformat(timespec="seconds")
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE email != '' AND email_message_id != '' AND send_status = 'sent'
                 AND stage = 'contacted' AND do_not_contact = 0
                 AND follow_up_count < ? AND last_sent_at IS NOT NULL AND last_sent_at < ?
               ORDER BY last_sent_at""",
            (int(settings["max_followups"]), cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def _original_subject(lead_id):
    for ev in reversed(db.list_events(lead_id, limit=100)):  # oldest first
        if ev["kind"] == "email_sent":
            try:
                return json.loads(ev["detail"]).get("subject", "")
            except ValueError:
                return ""
    return ""


def send_followup(lead_id):
    settings = db.get_settings()
    lead = db.get_lead(lead_id)
    _check_can_email(lead, settings)
    if lead["follow_up_count"] >= int(settings["max_followups"]):
        raise SendError(f"Already sent the maximum of {settings['max_followups']} follow-ups.", 400)
    if not lead["email_message_id"]:
        raise SendError("This lead hasn't been emailed yet.", 400)

    prompt = (
        "Write a brief, friendly follow-up (1-2 sentences) to the earlier outreach message below, "
        "since there's been no reply. Light and low-pressure; don't repeat the whole pitch.\n"
        f"Earlier message:\n{lead['message']}\n\nBusiness: {lead['business_name']}\n"
        "Return only the follow-up text, nothing else."
    )
    try:
        text = _clean_message(llm.complete(prompt, task="draft", max_tokens=400, temperature=0.8), "phone")
    except llm.LLMError as e:
        raise SendError(f"Couldn't write the follow-up: {e}", 502)

    orig = _original_subject(lead_id) or f"Quick question for {lead['business_name']}"
    subject = orig if orig.lower().startswith("re:") else f"Re: {orig}"
    msg = build_email(lead, subject, text, settings, in_reply_to=lead["email_message_id"])
    try:
        _deliver(msg)
    except Exception as e:  # noqa: BLE001
        db.add_event(lead_id, "send_failed", str(e)[:300])
        raise SendError(f"Sending failed: {e}", 502)

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


def process_inbound(raw_bytes, footer=""):
    """Handle one inbound email. Returns 'replied', 'optout', 'auto', or None if it isn't ours."""
    msg = email.message_from_bytes(raw_bytes)
    from_addr = parseaddr(msg.get("From", ""))[1].strip().lower()
    if not from_addr:
        return None
    lead = _lead_for_reply(msg, from_addr)
    if not lead:
        return None
    msgid = (msg.get("Message-ID") or "").strip()
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


def check_replies():
    """Look through recent unread mail (without marking it read) for replies to
    emails we sent."""
    if not imap_configured():
        raise SendError("Reply checking isn't set up: add IMAP_USER/IMAP_PASS (or SMTP_USER/SMTP_PASS) to .env.", 400)
    settings = db.get_settings()
    since = datetime.now(timezone.utc) - timedelta(days=45)
    if settings.get("sending_started_at"):
        try:
            since = max(since, datetime.fromisoformat(settings["sending_started_at"]) - timedelta(days=1))
        except ValueError:
            pass
    since_str = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
    counts = {"checked": 0, "replied": 0, "suppressed": 0, "auto_replies": 0}
    imap_host = config.env("IMAP_HOST", "imap.gmail.com")
    try:
        netguard.check_mail_host(imap_host, 993)
    except netguard.Blocked as e:
        raise SendError(str(e), 400)
    try:
        mail = imaplib.IMAP4_SSL(imap_host)
        mail.login(config.env("IMAP_USER") or config.env("SMTP_USER"),
                   config.env("IMAP_PASS") or config.env("SMTP_PASS"))
        mail.select("inbox", readonly=True)
        status, data = mail.search(None, "UNSEEN", "SINCE", since_str)
        if status != "OK":
            raise SendError("Searching the inbox failed.", 502)
        for num in data[0].split():
            status, msg_data = mail.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            counts["checked"] += 1
            result = process_inbound(msg_data[0][1], settings.get("email_footer", ""))
            if result == "replied":
                counts["replied"] += 1
            elif result == "optout":
                counts["replied"] += 1
                counts["suppressed"] += 1
            elif result == "auto":
                counts["auto_replies"] += 1
        mail.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        raise SendError(f"Couldn't check the inbox: {e}", 502)
    return counts


# ---------------------------------------------------------------- job entries

def run_draft(ctx, params):
    filters = {"dnc": "no"}
    if params.get("ids"):
        filters["ids"] = params["ids"]
    else:
        filters["not_rejected"] = True
    if params.get("campaign"):
        filters["campaign"] = params["campaign"]
    if params.get("channel"):
        filters["channel"] = params["channel"]
    if not params.get("force"):
        filters["drafted"] = "no"
    ids = db.select_ids(filters, limit=params.get("limit"))
    ctx.progress(0, len(ids))
    ctx.log(f"Drafting {len(ids)} messages")
    ok = failed = 0
    for i, lead_id in enumerate(ids, 1):
        if ctx.stopped:
            ctx.log("Stopped.")
            break
        try:
            draft_message(lead_id, params.get("template", ""), index=i - 1)
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            ctx.log(f"Lead {lead_id}: {e}")
            if isinstance(e, llm.LLMError) and "not set" in str(e):
                raise
        ctx.progress(i)
    return f"{ok} drafted" + (f", {failed} failed" if failed else "")


def run_send_bulk(ctx, params):
    """Send drafted emails one by one, paced, until the daily limit is hit."""
    settings = db.get_settings()
    filters = {"send_status": "unsent", "channel": "email", "drafted": "yes", "dnc": "no"}
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
            f"already sent today: {emails_sent_today()})")
    sent = failed = skipped = 0
    for i, lead_id in enumerate(ids, 1):
        if ctx.stopped:
            ctx.log("Stopped.")
            break
        try:
            send_lead_email(lead_id)
            sent += 1
            ctx.log(f"Sent to lead {lead_id}")
            ctx.progress(i)
            if i < len(ids) and ctx.sleep(int(settings["email_send_delay_seconds"])):
                break
        except SendError as e:
            if e.code == 429:
                ctx.log(str(e))
                break
            if e.code == 502:
                failed += 1
            else:
                skipped += 1
            ctx.log(f"Lead {lead_id}: {e}")
            ctx.progress(i)
            if not smtp_configured():
                break
    return f"{sent} sent, {failed} failed, {skipped} skipped"
