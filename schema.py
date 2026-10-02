"""
schema.py -- the ONE lead format used by every part of the app.

Everything that used to differ between the scraper, the enrich/filter scripts
and the Outreach Desk (field names, phone/Instagram formats, status words) is
settled here, once:

  * one set of field names
  * phones stored as E.164 (+12145550100), original kept in phone_raw
  * social profiles stored as full URLs (handles are derived when needed)
  * one stage vocabulary and one send-status vocabulary
  * timestamps are ISO-8601 UTC text
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlunparse

import config

# --------------------------------------------------------------- vocabularies

STAGES = ["new", "contacted", "replied", "qualified", "won", "lost", "dead"]
# stages where we must not send anything further
CLOSED_STAGES = ("replied", "qualified", "won", "lost", "dead")
SEND_STATUSES = ["unsent", "sent", "failed"]
CHANNELS = ["email", "yelp", "facebook", "instagram", "whatsapp", "phone", "unknown"]
DEFAULT_CHANNEL_PRIORITY = ["email", "yelp", "facebook", "instagram", "whatsapp", "phone"]
TRADES = ["hvac", "plumbing", "electrical", "pest_control", "roofing", "other"]

# Columns that hold lead *data* (managed by normalize_lead).
DATA_COLUMNS = [
    "business_name", "contact_name", "trade", "category_raw",
    "address", "city", "state", "review_count",
    "phone", "phone_raw", "email", "website", "has_website",
    "instagram_url", "facebook_url", "linkedin_url", "yelp_url", "google_maps_url",
    "notes", "website_domain", "name_key",
]

# Columns a person may edit by hand in the UI.
EDITABLE_FIELDS = [
    "business_name", "contact_name", "trade", "category_raw", "address", "city", "state",
    "phone", "email", "website", "instagram_url", "facebook_url", "linkedin_url",
    "yelp_url", "google_maps_url", "notes", "campaign", "stage", "message",
    "send_status", "channel", "do_not_contact",
]

# Column order for CSV export.
EXPORT_COLUMNS = [
    "id", "campaign", "source", "business_name", "contact_name", "trade", "category_raw",
    "address", "city", "state", "review_count", "phone", "email", "website", "has_website",
    "instagram_url", "facebook_url", "linkedin_url", "yelp_url", "google_maps_url",
    "score", "score_reasons", "fit", "fit_confidence", "fit_reason", "enrichment_notes",
    "channel", "stage", "send_status", "message", "template_used", "sent_at",
    "follow_up_count", "do_not_contact", "notes", "created_at", "updated_at",
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- phones

def norm_phone(raw):
    """Return an E.164 phone number, or '' if it can't be trusted.
    US-first: 10 digits (or 11 starting with 1) become +1XXXXXXXXXX. Set
    DEFAULT_COUNTRY_CODE in .env to change the assumed country."""
    if not raw:
        return ""
    s = str(raw).strip()
    s = re.split(r"(?i)\s*(?:ext\.?|extension|x)\s*\d+\s*$", s)[0]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    cc = config.env("DEFAULT_COUNTRY_CODE", "1").lstrip("+")
    if s.startswith("+") or s.startswith("00"):
        if s.startswith("00"):
            digits = digits[2:]
        if digits.startswith("0"):      # E.164 country codes never start with 0
            return ""
        return "+" + digits if 8 <= len(digits) <= 15 else ""
    if cc == "1":
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10 and re.fullmatch(r"[2-9]\d{2}[2-9]\d{6}", digits):
            return "+1" + digits
        return ""
    if 8 <= len(digits) <= 12:
        return "+" + cc + digits.lstrip("0")
    return ""


def is_us_phone(e164):
    return bool(e164) and e164.startswith("+1")


# ------------------------------------------------------------------- emails

_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")
_BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico")


def norm_email(raw):
    if not raw:
        return ""
    e = str(raw).strip().lower()
    e = re.sub(r"^mailto:", "", e).split("?")[0].strip(" <>;,.\"'")
    if e.endswith(_BAD_EMAIL_SUFFIX) or not _EMAIL_RE.match(e):
        return ""
    return e


# --------------------------------------------------------------------- URLs

_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_", "ref=", "src=", "igshid")


def unwrap_redirect(url):
    """Yelp (biz_redir), Instagram (l.instagram.com) and Google (/url?q=)
    hand out redirect links instead of the real address. Return the target."""
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("/biz_redir"):
        url = "https://www.yelp.com" + url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host = p.netloc.lower()
    qs = parse_qs(p.query)
    if p.path.startswith("/biz_redir") and qs.get("url"):
        return qs["url"][0]
    if host.endswith("l.instagram.com") and qs.get("u"):
        return qs["u"][0]
    if host.endswith("google.com") and p.path == "/url":
        for key in ("q", "url"):
            if qs.get(key):
                return qs[key][0]
    return url


def norm_url(raw):
    """Clean a URL: unwrap redirects, add a scheme, lowercase the host, drop
    tracking parameters and fragments. Returns '' if it isn't a usable URL."""
    u = unwrap_redirect(raw)
    if not u:
        return ""
    u = u.strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    try:
        p = urlparse(u)
    except ValueError:
        return ""
    host = p.netloc.lower()
    if not host or "." not in host or " " in host or host.startswith("."):
        return ""
    query = "&".join(
        kv for kv in p.query.split("&")
        if kv and not kv.lower().startswith(_TRACKING_PARAMS)
    )
    path = p.path.rstrip("/") if p.path not in ("", "/") else ""
    return urlunparse((p.scheme.lower(), host, path, "", query, ""))


def domain_of(url):
    if not url:
        return ""
    try:
        host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host_is(host, domains):
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


SOCIAL_HOSTS = {
    "instagram_url": ("instagram.com",),
    "facebook_url": ("facebook.com", "fb.com", "fb.me"),
    "linkedin_url": ("linkedin.com",),
    "yelp_url": ("yelp.com",),
}
# Places that are NOT a business's own website. A Facebook page or a
# link-in-bio page as the only web presence still counts as "no website".
DIRECTORY_HOSTS = (
    "google.com", "goo.gl", "business.site", "linktr.ee", "linkin.bio", "beacons.ai",
    "tiktok.com", "twitter.com", "x.com", "youtube.com", "pinterest.com", "nextdoor.com",
    "angi.com", "angieslist.com", "homeadvisor.com", "thumbtack.com", "yellowpages.com",
    "bbb.org", "mapquest.com", "manta.com", "porch.com", "instagram.com", "facebook.com",
    "fb.com", "linkedin.com", "yelp.com",
)


def norm_instagram(raw):
    if not raw:
        return ""
    s = unwrap_redirect(str(raw).strip())
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", s)
    if m:
        handle = m.group(1)
    elif re.fullmatch(r"@?[A-Za-z0-9._]{1,30}", s):
        handle = s.lstrip("@")
    else:
        return ""
    if handle.lower() in {"p", "explore", "reel", "reels", "accounts", "stories", "tv", "direct"}:
        return ""
    return f"https://www.instagram.com/{handle}/"


def instagram_handle(url):
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", url or "")
    return m.group(1) if m else ""


def norm_facebook(raw):
    s = str(raw or "").strip()
    if not s:
        return ""
    if re.match(r"^(https?://)?((www|m)\.)?(facebook|fb)\.com", s, re.I):
        p = urlparse(norm_url(s))
        path = p.path.rstrip("/")
        first = path.split("/")[1].lower() if path else ""
        if not path or first in ("sharer", "sharer.php", "share.php", "tr", "plugins"):
            return ""
        q = p.query if "id=" in p.query else ""
        return f"https://www.facebook.com{path}" + (f"?{q}" if q else "")
    if re.fullmatch(r"[A-Za-z0-9.\-]{3,80}", s):
        return f"https://www.facebook.com/{s}"
    return ""


def norm_linkedin(raw):
    u = norm_url(raw)
    return u if u and _host_is(domain_of(u), ("linkedin.com",)) else ""


# ------------------------------------------------------------ address / trade

US_STATES = set("""AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY
NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC""".split())


def parse_city_state(address):
    """'123 Main St, Dallas, TX 75201' -> ('Dallas', 'TX')."""
    if not address:
        return "", ""
    m = re.search(
        r"([A-Za-z .'\-]+),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,\s*(?:USA|United States|US))?\s*$",
        str(address).strip(),
    )
    if m and m.group(2) in US_STATES:
        return m.group(1).strip(), m.group(2)
    return "", ""


_TRADE_KEYWORDS = [
    ("hvac", ("hvac", "heating", "air condition", "a/c", "furnace", "cooling", "heat pump")),
    ("plumbing", ("plumb", "drain", "sewer", "water heater", "rooter")),
    ("electrical", ("electric",)),
    ("pest_control", ("pest", "exterminat", "termite", "rodent", "mosquito")),
    ("roofing", ("roof",)),
]


def derive_trade(category, name=""):
    """Map free-text categories ('Plumber', 'Heating & Air') to our trade list."""
    for text in (category, name):
        t = (text or "").lower()
        for trade, words in _TRADE_KEYWORDS:
            if any(w in t for w in words):
                return trade
    return "other" if (category or "").strip() else ""


_NAME_SUFFIX = re.compile(r"\b(llc|inc|incorporated|co|company|corp|corporation|ltd|the)\b")


def make_name_key(name, city=""):
    n = re.sub(r"[^a-z0-9]", "", _NAME_SUFFIX.sub(" ", (name or "").lower()))
    c = re.sub(r"[^a-z0-9]", "", (city or "").lower())
    return f"{n}|{c}" if n else ""


def _to_int(v):
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else 0


# ------------------------------------------------------------- normalisation

_TEXT_KEYS = [
    "business_name", "contact_name", "trade", "category_raw", "address", "city", "state",
    "phone", "phone_raw", "email", "website", "instagram_url", "facebook_url",
    "linkedin_url", "yelp_url", "google_maps_url", "notes",
]


def normalize_lead(raw):
    """Take a dict with any subset of canonical fields and return a cleaned
    dict with ALL data columns present (blank when unknown) plus the derived
    ones (has_website, website_domain, name_key, city/state, trade)."""
    d = {k: ("" if raw.get(k) is None else str(raw.get(k)).strip()) for k in _TEXT_KEYS}
    d["review_count"] = _to_int(raw.get("review_count"))

    d["business_name"] = re.sub(r"\s+", " ", d["business_name"])

    # phone -----------------------------------------------------------------
    src = d["phone"] or d["phone_raw"]
    d["phone_raw"] = d["phone_raw"] or src
    d["phone"] = norm_phone(src)

    d["email"] = norm_email(d["email"])

    # URLs: clean, then route social/directory links to the right column ------
    website = norm_url(d["website"]) if d["website"] else ""
    d["instagram_url"] = norm_instagram(d["instagram_url"])
    d["facebook_url"] = norm_facebook(d["facebook_url"])
    d["linkedin_url"] = norm_linkedin(d["linkedin_url"])
    d["yelp_url"] = norm_url(d["yelp_url"]) if d["yelp_url"] else ""
    d["google_maps_url"] = norm_url(d["google_maps_url"]) if d["google_maps_url"] else ""

    if website:
        host = domain_of(website)
        path = urlparse(website).path
        if _host_is(host, SOCIAL_HOSTS["instagram_url"]):
            d["instagram_url"] = d["instagram_url"] or norm_instagram(website)
            website = ""
        elif _host_is(host, SOCIAL_HOSTS["facebook_url"]):
            d["facebook_url"] = d["facebook_url"] or norm_facebook(website)
            website = ""
        elif _host_is(host, SOCIAL_HOSTS["linkedin_url"]):
            d["linkedin_url"] = d["linkedin_url"] or norm_linkedin(website)
            website = ""
        elif _host_is(host, SOCIAL_HOSTS["yelp_url"]):
            d["yelp_url"] = d["yelp_url"] or website
            website = ""
        elif (host == "google.com" and path.startswith("/maps")) or host in ("maps.app.goo.gl", "goo.gl"):
            d["google_maps_url"] = d["google_maps_url"] or website
            website = ""
    d["website"] = website
    d["has_website"] = 1 if website and not _host_is(domain_of(website), DIRECTORY_HOSTS) else 0
    d["website_domain"] = domain_of(website) if d["has_website"] else ""

    # address -> city/state ---------------------------------------------------
    if d["address"] and not (d["city"] and d["state"]):
        city, state = parse_city_state(d["address"])
        d["city"] = d["city"] or city
        d["state"] = d["state"] or state
    d["state"] = d["state"].upper() if len(d["state"]) == 2 else d["state"]

    # trade -------------------------------------------------------------------
    t = re.sub(r"[\s\-]+", "_", d["trade"].lower())
    if t in TRADES:
        d["trade"] = t
    else:
        d["trade"] = derive_trade(d["trade"] or d["category_raw"], d["business_name"])

    d["name_key"] = make_name_key(d["business_name"], d["city"])
    return d


# ---------------------------------------------------------- scoring / channel

def score_lead(lead):
    """Rule-based 0-10 score (free, instant). Favours businesses that clearly
    need help -- no real website, thin review history -- but are still
    reachable. It's a first-pass sort order, not a verdict: the LLM 'fit'
    judgement (which reads your criteria) is the real filter."""
    score, reasons = 0, []
    if not lead.get("has_website"):
        score += 3
        reasons.append("no real website (+3)")
    else:
        reasons.append("has website (+0)")
    rc = _to_int(lead.get("review_count"))
    if 0 < rc < 20:
        score += 2
        reasons.append("few reviews, likely under-digitised (+2)")
    elif rc >= 20:
        reasons.append("established review base (+0)")
    if lead.get("phone") or lead.get("phone_raw"):
        score += 2
        reasons.append("phone available (+2)")
    if lead.get("email"):
        score += 1
        reasons.append("email available (+1)")
    if lead.get("instagram_url") or lead.get("facebook_url"):
        score += 1
        reasons.append("active on social (+1)")
    if lead.get("address"):
        score += 1
        reasons.append("physical address (+1)")
    return min(score, 10), "; ".join(reasons)


def derive_channel(lead, priority=None):
    """Pick the best way to reach a lead, following the priority list.
    WhatsApp is only suggested for non-US numbers (US business lines are
    rarely on it); a US phone falls through to 'phone' (call / text)."""
    priority = priority or DEFAULT_CHANNEL_PRIORITY
    for ch in priority:
        if ch == "email" and lead.get("email"):
            return "email"
        if ch == "yelp" and lead.get("yelp_url"):
            return "yelp"
        if ch == "facebook" and lead.get("facebook_url"):
            return "facebook"
        if ch == "instagram" and lead.get("instagram_url"):
            return "instagram"
        if ch == "whatsapp" and lead.get("phone") and not is_us_phone(lead["phone"]):
            return "whatsapp"
        if ch == "phone" and (lead.get("phone") or lead.get("phone_raw")):
            return "phone"
    return "unknown"


def to_bool_int(v):
    if isinstance(v, str):
        return 1 if v.strip().lower() in ("1", "true", "yes", "y", "on") else 0
    return 1 if v else 0


# ------------------------------------------------------- name columns & placeholders

# Bare column headers that mean "the business", whatever kind of business it is.
ENTITY_HEADERS = {
    "clinic", "practice", "shop", "store", "restaurant", "hospital", "firm", "agency", "salon",
    "studio", "brand", "vendor", "merchant", "establishment", "organisation", "org", "account",
    "dba", "venue", "hotel", "gym", "pharmacy", "office", "dealer", "dealership", "contractor",
}
# "<these> name" is a person, not the business.
_PERSON_WORDS = {"first", "full", "contact", "owner", "person", "manager", "doctor", "dr", "dentist",
                 "physician", "founder", "ceo", "director", "poc", "rep", "agent", "given"}
# "<these> name" is neither (kept in notes like any other unmatched column).
_NOT_A_NAME = {"last", "sur", "middle", "family", "nick", "maiden", "user", "file", "campaign",
               "template", "domain", "host", "city", "state", "country", "street", "county", "category",
               "source", "list", "sheet", "column", "product", "service", "plan", "package", "event",
               "job", "role", "position", "email", "phone", "sender", "from", "my", "our", "your"}


def name_header_target(key):
    """Which lead field a column header names, judged only from the header text:
    'business_name' for clinic_name / name_of_practice / shop / companyname ...,
    'contact_name' for first_name / owner_name / doctor_name ..., else None.
    `key` is a header lowercased with non-alphanumerics turned into underscores."""
    key = (key or "").strip("_")
    if not key:
        return None
    if key in ENTITY_HEADERS:
        return "business_name"
    tokens = [t for t in key.split("_") if t]
    if "name" not in tokens:
        if len(tokens) == 1 and key.endswith("name") and len(key) > 4:      # clinicname, firstname
            tokens = [key[:-4], "name"]
        elif len(tokens) == 1 and key in ("fname",):
            return "contact_name"
        else:
            return None
    rest = {t for t in tokens if t not in ("name", "of", "the", "s")}
    if rest & _NOT_A_NAME:
        return None
    if rest & _PERSON_WORDS:
        return "contact_name"
    return "business_name"


_PH_FIELDS = {
    "business_name": "business_name", "business": "business_name", "company": "business_name",
    "company_name": "business_name", "name": "business_name", "biz_name": "business_name",
    "organization": "business_name", "organization_name": "business_name",
    "contact_name": "contact_name", "contact": "contact_name", "owner": "contact_name",
    "owner_name": "contact_name", "full_name": "contact_name",
    "first_name": "first_name", "firstname": "first_name",
    "city": "city", "state": "state", "trade": "trade", "phone": "phone", "email": "email",
    "website": "website", "address": "address", "location": "location",
}
_PH_RE = re.compile(
    r"\{\{\s*([^{}\n]{1,40}?)\s*\}\}"            # {{Clinic Name}}
    r"|\{\s*([^{}\n]{1,40}?)\s*\}"               # {business_name}
    r"|<<\s*([^<>\n]{1,40}?)\s*>>"               # <<name>>
    r"|\[\s*([A-Za-z][A-Za-z _\-]{1,38}?)\s*\]"  # [Business Name]
)


def _placeholder_value(token, lead):
    key = re.sub(r"[^a-z0-9]+", "_", token.lower()).strip("_")
    field = _PH_FIELDS.get(key)
    if field is None:
        target = name_header_target(key)
        field = {"business_name": "business_name", "contact_name": "contact_name"}.get(target)
    if field is None:
        return None, False                       # not a placeholder we know
    if field == "first_name":
        words = [w for w in (lead.get("contact_name") or "").split()
                 if w.lower().strip(".") not in ("dr", "mr", "mrs", "ms", "miss", "prof", "sir")]
        value = words[0] if words else ""
    elif field == "location":
        value = ", ".join(x for x in (lead.get("city"), lead.get("state")) if x)
    elif field == "trade":
        value = (lead.get("trade") or "").replace("_", " ")
    else:
        value = lead.get(field) or ""
    return str(value).strip(), True


def fill_placeholders(text, lead):
    """Replace {name} / {{Clinic Name}} / [Business Name] / <<company>> style placeholders with
    the lead's real details. Returns (text, unresolved): `unresolved` lists the placeholders that
    are still in the text -- a known one whose value is blank for this lead, or any {curly} /
    {{double curly}} / <<angle>> token that isn't recognised. Square brackets are only touched
    when they hold a known placeholder, so ordinary [bracketed] text is left alone."""
    unresolved = []

    def sub(m):
        token = next(g for g in m.groups() if g is not None)
        square = m.group(4) is not None
        value, known = _placeholder_value(token, lead or {})
        if known and value:
            return value
        if known or not square:
            if m.group(0) not in unresolved:
                unresolved.append(m.group(0))
        return m.group(0)

    return _PH_RE.sub(sub, text or ""), unresolved
