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
import contextvars
import re
from contextlib import contextmanager
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
    "place_id", "rating", "opening_hours", "country", "phone_kind",
]
# Columns worked out from the others (never merged in from a second copy of a lead).
DERIVED_COLUMNS = ("has_website", "website_domain", "name_key", "phone_kind")

# Columns a person may edit by hand in the UI.
EDITABLE_FIELDS = [
    "business_name", "contact_name", "trade", "category_raw", "address", "city", "state", "country",
    "phone", "email", "website", "instagram_url", "facebook_url", "linkedin_url",
    "yelp_url", "google_maps_url", "notes", "campaign", "stage", "message",
    "send_status", "channel", "do_not_contact",
]

# Column order for CSV export.
EXPORT_COLUMNS = [
    "id", "campaign", "source", "business_name", "contact_name", "trade", "category_raw",
    "address", "city", "state", "country", "review_count", "rating", "opening_hours", "place_id",
    "phone", "email", "website", "has_website",
    "instagram_url", "facebook_url", "linkedin_url", "yelp_url", "google_maps_url",
    "score", "score_reasons", "fit", "fit_confidence", "fit_reason", "enrichment_notes",
    "channel", "stage", "send_status", "message", "template_used", "sent_at",
    "follow_up_count", "do_not_contact", "notes", "created_at", "updated_at",
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- phones

_CALLING_CODE = contextvars.ContextVar("calling_code", default="")


@contextmanager
def calling_code(cc):
    """Read numbers written without a country code as `cc` (a campaign's own country) inside
    this block, instead of the account's DEFAULT_COUNTRY_CODE."""
    # an empty code keeps whatever an outer block chose (a scrape of "Bengaluru" reads +91 even when
    # the campaign itself has no code of its own)
    token = _CALLING_CODE.set(re.sub(r"\D", "", str(cc or "")) or _CALLING_CODE.get())
    try:
        yield
    finally:
        _CALLING_CODE.reset(token)


_PHONE_SPLIT = re.compile(r"\s*(?:[,;/|\n]|\bor\b|&)\s*", re.I)


def norm_phone(raw):
    """Return an E.164 phone number, or '' if it can't be trusted.
    US-first: 10 digits (or 11 starting with 1) become +1XXXXXXXXXX. Set
    DEFAULT_COUNTRY_CODE in .env (or a campaign's own code) to change the assumed country.
    A cell holding several numbers ("98765 43210 / 080 2345 6789") gives the first usable one."""
    if not raw:
        return ""
    s = str(raw).strip()
    first = _norm_one(s)
    if first or not _PHONE_SPLIT.search(s):
        return first
    for part in _PHONE_SPLIT.split(s):
        n = _norm_one(part)
        if n:
            return n
    return ""


def _norm_one(s):
    s = re.split(r"(?i)\s*(?:ext\.?|extension|x)\s*\d+\s*$", s.strip())[0]
    s = re.sub(r"\(\s*0\s*\)", "", s)            # "+91 (0) 80 ..." / "+44 (0)20 ...": the 0 isn't dialled from abroad
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    cc = _CALLING_CODE.get() or config.env("DEFAULT_COUNTRY_CODE", "1").lstrip("+")
    if s.startswith("+") or s.startswith("00"):
        if s.startswith("00"):
            digits = digits[2:]
        if digits.startswith("0"):      # E.164 country codes never start with 0
            return ""
        for code in _TRUNK_ZERO_CODES:  # "+91 080 2345 6789": a national 0 kept after the country code
            if digits.startswith(code + "0") and len(digits) - len(code) - 1 >= 7:
                digits = code + digits[len(code) + 1:]
                break
        return "+" + digits if 8 <= len(digits) <= 15 else ""
    if cc == "1":
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10 and re.fullmatch(r"[2-9]\d{2}[2-9]\d{6}", digits):
            return "+1" + digits
        return ""
    national = digits.lstrip("0")
    if national.startswith(cc) and len(national) - len(cc) >= 9 and len(national) > 10:
        national = national[len(cc):]          # written with the country code but no "+": 91 98765 43210
    if cc == "91" and not (len(national) == 10 or (national.startswith(("1800", "1860")) and 10 <= len(national) <= 11)):
        return ""                              # Indian numbers are 10 digits (toll-free 1800/1860 a little longer)
    if 7 <= len(national) <= 12:
        return "+" + cc + national
    return ""


# Countries whose numbers are written with a leading 0 nationally (people sometimes keep it after the code).
_TRUNK_ZERO_CODES = ("971", "966", "91", "92", "44", "61", "64", "20", "27", "234", "254", "63", "60", "62",
                     "353", "33", "49", "39", "880", "94", "977")


def is_us_phone(e164):
    return bool(e164) and e164.startswith("+1")


# country code -> regex on the national number for MOBILE lines. Anything else in that
# country is a landline (or a shared/toll-free line), which can't receive WhatsApp.
_MOBILE_PATTERNS = {
    "971": r"5\d{8}",            # UAE 050/052/054/055/056/058
    "966": r"5\d{8}",            # Saudi Arabia
    "974": r"[3567]\d{7}",       # Qatar
    "965": r"[569]\d{7}",        # Kuwait
    "968": r"[79]\d{7}",         # Oman
    "973": r"3\d{7}",            # Bahrain
    "91": r"[6-9]\d{9}",         # India
    "92": r"3\d{9}",             # Pakistan
    "44": r"7\d{9}",             # UK
    "61": r"4\d{8}",             # Australia
    "64": r"2\d{7,9}",           # New Zealand
    "65": r"[89]\d{7}",          # Singapore
    "20": r"1\d{9}",             # Egypt
    "27": r"[678]\d{8}",         # South Africa
    "234": r"[789]\d{9}",        # Nigeria
    "254": r"[17]\d{8}",         # Kenya
    "63": r"9\d{9}",             # Philippines
    "60": r"1\d{8,9}",           # Malaysia
    "62": r"8\d{8,11}",          # Indonesia
    "353": r"8\d{8}",            # Ireland
    "33": r"[67]\d{8}",          # France
    "49": r"1[5-7]\d{8,9}",      # Germany
    "34": r"[67]\d{8}",          # Spain
    "39": r"3\d{8,9}",           # Italy
    "52": r"\d{10}",             # Mexico (mobiles and landlines look alike) -> treated as mobile
    "55": r"\d{2}9\d{8}",        # Brazil
}


# Landline / shared / toll-free numbers, for the countries where they're clearly told apart.
_LANDLINE_PATTERNS = {
    "971": r"[234679]\d{7}|[68]00\d{3,9}",   # UAE 02/03/04/06/07/09 + 800 / 600 numbers
    "966": r"1\d{7}|[89]\d{8,9}",
    "974": r"4\d{7}|800\d{4}",
    "965": r"[12]\d{7}|1800\d+",
    "968": r"2\d{7}|800\d+",
    "973": r"1\d{7}|80\d{6}",
    "91": r"[1-5]\d{9}|18[06]0\d{6,7}",
    "92": r"[2-9]\d{8,9}",
    "44": r"[123]\d{8,9}|80\d{7,8}",
    "61": r"[2378]\d{8}|1[38]00\d{6}",
    "65": r"6\d{7}|1800\d{7}",
    "20": r"[2-9]\d{7,8}",
    "27": r"[1-5]\d{8}",
}


# Indian landlines whose area code starts like a mobile (Bengaluru 080, Ahmedabad 079, ...): told apart
# by how they're written -- area code with its 0, then the subscriber number, which starts 2-6.
_IN_STD_WRITTEN = re.compile(r"^\s*(?:\+91[\s.\-]*\(?0?|\(?0)(79|80|612|641|674|712|731|751|761|771|781|788|821|824|"
                             r"831|836|863|866|870|877|891|895)\)?[\s.\-]+[2-6]\d")


def phone_kind_from_raw(raw, e164):
    """phone_kind, with what the raw text reveals that the number alone can't."""
    kind = phone_kind(e164)
    if kind == "mobile" and e164.startswith("+91") and _IN_STD_WRITTEN.match(str(raw or "")):
        return "landline"
    return kind


def phone_kind(e164):
    """'mobile', 'landline' or '' (can't tell -- US/Canada numbers, unknown countries, odd lengths)."""
    if not e164 or not e164.startswith("+"):
        return ""
    digits = e164[1:]
    for cc in sorted(_MOBILE_PATTERNS, key=len, reverse=True):
        if digits.startswith(cc):
            national = digits[len(cc):]
            if re.fullmatch(_MOBILE_PATTERNS[cc], national):
                return "mobile"
            if cc in _LANDLINE_PATTERNS and re.fullmatch(_LANDLINE_PATTERNS[cc], national):
                return "landline"
            return ""
    return ""


# ------------------------------------------------------------------- emails

_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")
_BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico")


_EMAIL_FIND = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}")


def norm_email(raw):
    """One clean, lowercased address, or ''. A cell holding several ("a@x.com; b@x.com",
    "Jo <jo@x.com>") gives the first usable one instead of nothing."""
    if not raw:
        return ""
    e = str(raw).strip().lower()
    e = re.sub(r"^mailto:", "", e).split("?")[0].strip(" <>;,.\"'")
    if _EMAIL_RE.match(e):
        return "" if e.endswith(_BAD_EMAIL_SUFFIX) else e
    for m in _EMAIL_FIND.findall(e):
        m = m.strip(".")
        if _EMAIL_RE.match(m) and not m.endswith(_BAD_EMAIL_SUFFIX):
            return m
    return ""


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
    # messaging / link pages
    "wa.me", "whatsapp.com", "t.me", "m.me", "bit.ly", "tinyurl.com", "taplink.cc", "bio.link",
    "carrd.co", "lnk.bio", "campsite.bio", "snapchat.com", "threads.net",
    # listing, booking and marketplace pages (a profile there is not the business's own site)
    "tripadvisor.com", "foursquare.com", "houzz.com", "nextdoor.com", "superpages.com",
    "chamberofcommerce.com", "alignable.com", "buildzoom.com", "homeguide.com", "expertise.com",
    "justdial.com", "sulekha.com", "indiamart.com", "tradeindia.com", "practo.com", "lybrate.com",
    "zocdoc.com", "healthgrades.com", "vitals.com", "webmd.com", "ratemds.com", "1mg.com",
    "booksy.com", "vagaro.com", "fresha.com", "styleseat.com", "mindbodyonline.com", "schedulicity.com",
    "opentable.com", "doordash.com", "ubereats.com", "grubhub.com", "zomato.com", "swiggy.com",
    "booking.com", "airbnb.com", "expedia.com", "makemytrip.com", "etsy.com", "amazon.com", "ebay.com",
    "apple.com", "play.google.com", "yell.com", "gumtree.com", "craigslist.org", "olx.in",
    # more Indian listing / booking sites (a profile there isn't the business's own website)
    "justdial.in", "jsdl.in", "sulekha.in", "magicpin.in", "dineout.co.in", "eazydiner.com", "urbancompany.com",
    "urbanclap.com", "nobroker.in", "99acres.com", "magicbricks.com", "housing.com", "bookmyshow.com",
    "clinicspots.com", "credihealth.com", "docprime.com", "practo.in", "quikr.com", "grotal.com",
    "asklaila.com", "yellowpages.in", "indiacom.com", "exportersindia.com", "dial4trade.com",
    "nearbuy.com", "zomato.in", "swiggy.in", "meesho.com", "flipkart.com", "amazon.in", "myntra.com",
    "instamojo.com", "dukaan.app", "mydukaan.io", "shiprocket.in", "wa.link", "api.whatsapp.com",
    "g.page", "maps.app.goo.gl",
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


COUNTRY_NAMES = {
    "united arab emirates": "AE", "uae": "AE", "u.a.e.": "AE", "india": "IN", "saudi arabia": "SA",
    "ksa": "SA", "qatar": "QA", "kuwait": "KW", "oman": "OM", "bahrain": "BH", "united kingdom": "GB",
    "uk": "GB", "england": "GB", "australia": "AU", "canada": "CA", "singapore": "SG", "pakistan": "PK",
    "egypt": "EG", "south africa": "ZA", "new zealand": "NZ", "ireland": "IE", "nigeria": "NG",
    "kenya": "KE", "philippines": "PH", "malaysia": "MY", "indonesia": "ID", "united states": "US",
    "usa": "US", "us": "US", "united states of america": "US", "germany": "DE", "france": "FR",
    "spain": "ES", "italy": "IT", "mexico": "MX", "brazil": "BR", "bangladesh": "BD", "sri lanka": "LK",
    "nepal": "NP",
}


def norm_country(raw):
    """Two-letter country code from 'AE', 'ae', 'United Arab Emirates', 'UAE' ... or ''."""
    v = (raw or "").strip()
    if not v:
        return ""
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return COUNTRY_NAMES.get(v.lower().strip(" ."), "")


def parse_city_country(address):
    """'Building 5, Al Reem Island, Abu Dhabi, United Arab Emirates' -> ('Abu Dhabi', 'AE').
    Only when the address ends with a country we know; otherwise ('', '')."""
    parts = [p.strip() for p in str(address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return "", ""
    country = COUNTRY_NAMES.get(re.sub(r"[\d\-]+", "", parts[-1]).strip(" .").lower(), "")
    if not country or country == "US":
        return "", country
    city = re.sub(r"\b\d[\d\- ]*\b", "", parts[-2]).strip(" -")
    return (city if re.search(r"[A-Za-z]", city) else ""), country


INDIAN_STATES = {
    "andhra pradesh": "Andhra Pradesh", "arunachal pradesh": "Arunachal Pradesh", "assam": "Assam", "bihar": "Bihar",
    "chhattisgarh": "Chhattisgarh", "goa": "Goa", "gujarat": "Gujarat", "haryana": "Haryana",
    "himachal pradesh": "Himachal Pradesh", "jharkhand": "Jharkhand", "karnataka": "Karnataka", "kerala": "Kerala",
    "madhya pradesh": "Madhya Pradesh", "maharashtra": "Maharashtra", "manipur": "Manipur", "meghalaya": "Meghalaya",
    "mizoram": "Mizoram", "nagaland": "Nagaland", "odisha": "Odisha", "orissa": "Odisha", "punjab": "Punjab",
    "rajasthan": "Rajasthan", "sikkim": "Sikkim", "tamil nadu": "Tamil Nadu", "tamilnadu": "Tamil Nadu",
    "telangana": "Telangana", "tripura": "Tripura", "uttar pradesh": "Uttar Pradesh", "uttarakhand": "Uttarakhand",
    "uttaranchal": "Uttarakhand", "west bengal": "West Bengal", "delhi": "Delhi", "new delhi": "Delhi",
    "nct of delhi": "Delhi", "national capital territory of delhi": "Delhi", "jammu and kashmir": "Jammu and Kashmir",
    "jammu & kashmir": "Jammu and Kashmir", "ladakh": "Ladakh", "puducherry": "Puducherry", "pondicherry": "Puducherry",
    "chandigarh": "Chandigarh", "andaman and nicobar islands": "Andaman and Nicobar Islands",
    "dadra and nagar haveli and daman and diu": "Dadra and Nagar Haveli and Daman and Diu", "lakshadweep": "Lakshadweep",
}
# Old and new names of the same Indian city, so "Bangalore" and "Bengaluru" listings are recognised as one place.
CITY_ALIASES = {
    "bangalore": "bengaluru", "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata", "gurgaon": "gurugram",
    "pondicherry": "puducherry", "trivandrum": "thiruvananthapuram", "cochin": "kochi", "ernakulam": "kochi",
    "mysore": "mysuru", "allahabad": "prayagraj", "baroda": "vadodara", "poona": "pune", "new delhi": "delhi",
    "mangalore": "mangaluru", "belgaum": "belagavi", "hubli": "hubballi", "vizag": "visakhapatnam",
    "benares": "varanasi", "banaras": "varanasi", "calicut": "kozhikode", "trichy": "tiruchirappalli",
    "navi mumbai": "navi mumbai", "secunderabad": "hyderabad",
}
IN_STATE_CODES = {
    "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam", "BR": "Bihar", "CG": "Chhattisgarh",
    "CT": "Chhattisgarh", "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana", "HP": "Himachal Pradesh", "JH": "Jharkhand",
    "KA": "Karnataka", "KL": "Kerala", "MP": "Madhya Pradesh", "MH": "Maharashtra", "MN": "Manipur", "ML": "Meghalaya",
    "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha", "OR": "Odisha", "PB": "Punjab", "RJ": "Rajasthan",
    "SK": "Sikkim", "TN": "Tamil Nadu", "TS": "Telangana", "TG": "Telangana", "TR": "Tripura", "UP": "Uttar Pradesh",
    "UK": "Uttarakhand", "UT": "Uttarakhand", "WB": "West Bengal", "DL": "Delhi", "JK": "Jammu and Kashmir",
    "LA": "Ladakh", "PY": "Puducherry", "CH": "Chandigarh",
}
CALLING_CODES = {"US": "1", "CA": "1", "IN": "91", "AE": "971", "SA": "966", "QA": "974", "KW": "965", "OM": "968",
                 "BH": "973", "GB": "44", "AU": "61", "NZ": "64", "SG": "65", "PK": "92", "BD": "880", "LK": "94",
                 "NP": "977", "EG": "20", "ZA": "27", "NG": "234", "KE": "254", "PH": "63", "MY": "60", "ID": "62",
                 "IE": "353", "DE": "49", "FR": "33", "ES": "34", "IT": "39", "MX": "52", "BR": "55"}
_COUNTRY_BY_CODE = {"91": "IN", "971": "AE", "966": "SA", "974": "QA", "965": "KW", "968": "OM", "973": "BH",
                    "44": "GB", "61": "AU", "64": "NZ", "65": "SG", "92": "PK", "880": "BD", "94": "LK", "977": "NP",
                    "20": "EG", "27": "ZA", "234": "NG", "254": "KE", "63": "PH", "60": "MY", "62": "ID", "353": "IE"}
_PIN_RE = re.compile(r"(?<!\d)([1-9]\d{2})\s?(\d{3})(?!\d)")


def indian_state(text):
    return INDIAN_STATES.get(re.sub(r"[^a-z& ]", "", (text or "").lower()).strip(), "")


def parse_indian_address(address):
    """'No. 12, MG Road, Indiranagar, Bengaluru, Karnataka 560038, India' ->
    {'city': 'Bengaluru', 'state': 'Karnataka', 'pin': '560038'}; {} when it isn't clearly Indian."""
    text = str(address or "").strip()
    if not text:
        return {}
    pin_m = None
    for pin_m in _PIN_RE.finditer(text):
        pass                                           # the last 6-digit group is the PIN
    pin = (pin_m.group(1) + pin_m.group(2)) if pin_m else ""
    parts = [p.strip(" -.") for p in re.split(r",|\n", text) if p.strip(" -.")]
    parts = [p for p in parts if p.lower() not in ("india", "bharat", "in")]
    state, state_at = "", -1
    for i in range(len(parts) - 1, -1, -1):
        st = indian_state(_PIN_RE.sub("", parts[i]).strip(" -"))
        if st:
            state, state_at = st, i
            break
    says_india = bool(re.search(r"\bindia\b", text, re.I))
    if not state and not (pin and says_india):
        return {}
    city = ""
    if state_at > 0:
        city = parts[state_at - 1]
    elif state_at == 0 and indian_state(parts[0]) == "Delhi":
        city = "New Delhi" if "new delhi" in text.lower() else "Delhi"
    elif not state and parts:
        city = parts[-1]
    city = re.sub(r"\s*-?\s*" + (pin_m.group(0) if pin_m else r"\b\d{6}\b"), "", city).strip(" -.")
    city = re.sub(r"\d", "", city).strip(" -.")
    if state == "Delhi" and not city:
        city = "Delhi"
    return {"city": city if re.search(r"[A-Za-z]", city) else "", "state": state, "pin": pin}


_PLACE_ID_RE = re.compile(r"(?:place_id[:=]|query_place_id=|placeid=)(ChI[A-Za-z0-9_\-]{10,})")
_CID_RE = re.compile(r"[?&]cid=(\d{5,})")
_FTID_RE = re.compile(r"(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)


def place_key(place_id="", maps_url=""):
    """One comparable id for a Google Maps place: 'pid:ChIJ...', 'cid:123...' or 'ftid:0x..:0x..'.
    Different kinds can't be compared with each other, so callers compare only like with like."""
    pid = (place_id or "").strip()
    if pid.startswith("ChI"):
        return "pid:" + pid
    if pid.startswith("0x") and ":" in pid:
        return "ftid:" + pid.lower()
    if pid.isdigit() and len(pid) >= 5:
        return "cid:" + pid
    url = maps_url or ""
    m = _PLACE_ID_RE.search(url)
    if m:
        return "pid:" + m.group(1)
    m = _CID_RE.search(url)
    if m:
        return "cid:" + m.group(1)
    m = _FTID_RE.search(url)
    if m:
        return "ftid:" + m.group(1).lower()
    return ""


def _to_rating(v):
    m = re.search(r"\d+(?:[.,]\d+)?", str(v if v is not None else ""))
    if not m:
        return None
    r = float(m.group(0).replace(",", "."))
    return round(r, 1) if 0 < r <= 5 else None


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


_NAME_SUFFIX_IN = re.compile(r"\b(pvt|private|limited|llp|opc)\b")


def make_name_key(name, city=""):
    # \W keeps letters of every script, so Hindi, Tamil or Arabic names dedupe too (ASCII names give the same keys as before)
    low = _NAME_SUFFIX_IN.sub(" ", _NAME_SUFFIX.sub(" ", (name or "").lower()))
    n = re.sub(r"[\W_]", "", low)
    c = (city or "").lower().strip()
    c = re.sub(r"[\W_]", "", CITY_ALIASES.get(c, c))
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
    "place_id", "opening_hours", "country",
]


# What spreadsheets put in a cell that has nothing in it.
_BLANK_WORDS = {"n/a", "na", "n.a.", "n.a", "#n/a", "none", "null", "nil", "nan", "-", "--", "---", "—",
                "not available", "not applicable", "unknown", "tbd", "?", "x", "."}
# ...and, in a link / email / phone column only, a yes-or-no answer is not a link.
_BLANK_FLAGS = {"no", "yes", "y", "n", "true", "false", "0", "1"}
_CONTACT_KEYS = {"phone", "phone_raw", "email", "website", "instagram_url", "facebook_url",
                 "linkedin_url", "yelp_url", "google_maps_url"}


def clean_cell(key, value):
    """'' for a placeholder such as "N/A" or "-", else the trimmed text."""
    v = "" if value is None else str(value).strip()
    low = v.lower()
    if low in _BLANK_WORDS and key != "notes":
        return ""
    if key in _CONTACT_KEYS and low in _BLANK_FLAGS:
        return ""
    return v


def normalize_lead(raw):
    """Take a dict with any subset of canonical fields and return a cleaned
    dict with ALL data columns present (blank when unknown) plus the derived
    ones (has_website, website_domain, name_key, city/state, trade)."""
    d = {k: clean_cell(k, raw.get(k)) for k in _TEXT_KEYS}
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
    d["country"] = norm_country(d["country"])
    assumed_cc = _CALLING_CODE.get() or config.env("DEFAULT_COUNTRY_CODE", "1").lstrip("+")
    if len(d["state"]) == 2 and d["state"].upper() in IN_STATE_CODES and (d["country"] == "IN" or (
            not d["country"] and (assumed_cc == "91" or d["phone"].startswith("+91")))):
        d["state"] = IN_STATE_CODES[d["state"].upper()]      # "UP", "KA" on an Indian list are states, not US ones
    if indian_state(d["state"]):
        d["state"] = indian_state(d["state"])
        d["country"] = d["country"] or "IN"
    if d["address"] and d["country"] in ("", "IN") and not (d["city"] and d["state"] and d["country"]):
        ind = parse_indian_address(d["address"])
        if ind:
            d["city"] = d["city"] or ind["city"]
            d["state"] = d["state"] or ind["state"]
            d["country"] = d["country"] or "IN"
    if d["address"] and (not d["city"] or not d["country"]):
        city, country = parse_city_country(d["address"])
        d["city"] = d["city"] or city
        d["country"] = d["country"] or country
    if not d["country"] and d["state"] in US_STATES:
        d["country"] = "US"
    if not d["country"] and d["phone"]:
        d["country"] = next((c for code, c in sorted(_COUNTRY_BY_CODE.items(), key=lambda x: -len(x[0]))
                             if d["phone"].startswith("+" + code)), "")

    # Google Maps place, rating, hours, line type ---------------------------------
    key = place_key(d["place_id"], d["google_maps_url"])
    d["place_id"] = key.split(":", 1)[1] if key else ""
    d["rating"] = _to_rating(raw.get("rating"))
    d["opening_hours"] = re.sub(r"\s+", " ", d["opening_hours"])[:300]
    d["phone_kind"] = phone_kind_from_raw(d["phone_raw"], d["phone"])

    # trade -------------------------------------------------------------------
    t = re.sub(r"[\s\-]+", "_", d["trade"].lower())
    if t in TRADES:
        d["trade"] = t
    else:
        d["trade"] = derive_trade(d["trade"] or d["category_raw"], d["business_name"])

    d["name_key"] = make_name_key(d["business_name"], d["city"])
    return d


# ---------------------------------------------------------- scoring / channel

def score_lead(lead, favor_no_website=True):
    """Rule-based 0-10 score (free, instant). Favours businesses that clearly
    need help -- no real website, thin review history -- but are still
    reachable. It's a first-pass sort order, not a verdict: the LLM 'fit'
    judgement (which reads your criteria) is the real filter."""
    score, reasons = 0, []
    if not favor_no_website:
        # selling something other than websites: a site of their own is neither good nor bad
        score += 2
        reasons.append("website not scored (+2 for everyone)")
    elif not lead.get("has_website"):
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
    rarely on it) that aren't landlines; those fall through to 'phone' (call / text)."""
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
        if (ch == "whatsapp" and lead.get("phone") and not is_us_phone(lead["phone"])
                and (lead.get("phone_kind") or phone_kind(lead["phone"])) != "landline"):
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


# Shown under every template editor.
PLACEHOLDER_HELP = ("{business_name}  {first_name}  {contact_name}  {city}  {state}  {location}  {trade}  "
                    "{phone}  {email}  {website}  {address}")


def _placeholder_value(token, lead):
    token, bar, fallback = token.partition("|")       # {first_name|there}: use "there" when blank
    value, known = _placeholder_lookup(token, lead)
    if known and not value and bar:
        return fallback.strip(), True
    return value, known


def _placeholder_lookup(token, lead):
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
    when they hold a known placeholder, so ordinary [bracketed] text is left alone.
    A fallback can be given after a bar: {first_name|there} becomes "there" when the lead has
    no contact name, instead of blocking the send."""
    unresolved = []

    def sub(m):
        token = next(g for g in m.groups() if g is not None)
        square = m.group(4) is not None
        value, known = _placeholder_value(token, lead or {})
        if known and (value or "|" in token):      # {first_name|} = "leave it out when blank"
            return value
        if known or not square:
            if m.group(0) not in unresolved:
                unresolved.append(m.group(0))
        return m.group(0)

    return _PH_RE.sub(sub, text or ""), unresolved
