"""
enrichment.py -- find the contact details a lead is missing.

For each lead it works through cheap/free sources first and only spends a paid
web search on what's still missing:

  1. the lead's own Yelp / Google Maps page (if we have the link)
  2. the business's own website: emails, phone, social links (free)
  3. web searches via SerpAPI -- only for still-missing fields, capped per lead
     and per month (see SERPAPI_MONTHLY_LIMIT)
  4. an LLM reads what was gathered and extracts the missing fields

Differences from the original enrich script:
  * fills blank fields only, per lead -- never overwrites, never skips a
    column just because it exists
  * doesn't search for things the lead already has
  * every value the LLM returns must actually appear in the gathered text
    (emails, phones, URLs, names), otherwise it's dropped as a guess
  * works without an LLM or SerpAPI key (free website scan only)
  * progress is saved per lead, so a stopped run resumes where it left off
"""
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
import db
import llm
import netguard
import schema

HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

# Order matters: with a small search budget, the most useful fields go first.
SEARCH_PRIORITY = ["website", "facebook_url", "email", "contact_name",
                   "instagram_url", "phone", "linkedin_url", "address"]
QUERY_TEMPLATES = {
    "website": "{n}{l} official website",
    "facebook_url": "{n}{l} facebook",
    "email": "{n}{l} contact email",
    "contact_name": "{n}{l} owner name founder",
    "instagram_url": "{n}{l} instagram",
    "phone": "{n}{l} phone number",
    "linkedin_url": "{n}{l} linkedin",
    "address": "{n}{l} address",
}

_JUNK_EMAIL_PARTS = ("noreply", "no-reply", "wixpress", "sentry", "example.", "yourdomain",
                     "domain.com", "email.com", "@2x", "u003e")


# --------------------------------------------------------------- helpers

def missing_fields(lead):
    """Fields worth looking for, in search-priority order."""
    m = []
    for f in SEARCH_PRIORITY:
        if f == "website":
            if not lead.get("has_website"):
                m.append(f)
        elif not lead.get(f):
            m.append(f)
    return m


def _get(url, timeout=12):
    """Fetch a page as text. Only public internet addresses are allowed (see netguard)."""
    try:
        return netguard.safe_get(url, headers=HEADERS, timeout=timeout)
    except netguard.Blocked:
        return ""
    except requests.RequestException:
        if url.startswith("https://"):  # some tiny sites only serve http
            try:
                return netguard.safe_get("http://" + url[8:], headers=HEADERS, timeout=timeout)
            except (netguard.Blocked, requests.RequestException):
                return ""
        return ""


def visible_text(html, limit=6000):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)[:limit]


def fetch_profile_text(url):
    html = _get(url)
    return visible_text(html) if html else ""


def scan_website(url):
    """Free pass over a business's own site: emails, phones, social links, text."""
    found = {"emails": [], "phones": [], "instagram_url": "", "facebook_url": "",
             "linkedin_url": "", "text": ""}
    home = _get(url)
    if not home:
        return found
    pages = [home]
    soup = BeautifulSoup(home, "html.parser")
    seen = {url.rstrip("/")}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if len(pages) >= 3:
            break
        if re.search(r"contact|about", href, re.I) and not href.startswith(("mailto:", "tel:", "#")):
            full = urljoin(url, href).split("#")[0]
            if urlparse(full).netloc == urlparse(url).netloc and full.rstrip("/") not in seen:
                seen.add(full.rstrip("/"))
                html = _get(full)
                if html:
                    pages.append(html)

    texts = []
    for html in pages:
        s = BeautifulSoup(html, "html.parser")
        for a in s.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                e = schema.norm_email(href)
                if e and e not in found["emails"]:
                    found["emails"].append(e)
            elif href.lower().startswith("tel:"):
                p = schema.norm_phone(href[4:])
                if p and p not in found["phones"]:
                    found["phones"].append(p)
            elif "instagram.com" in href and not found["instagram_url"]:
                found["instagram_url"] = schema.norm_instagram(href)
            elif ("facebook.com" in href or "fb.com" in href) and not found["facebook_url"]:
                found["facebook_url"] = schema.norm_facebook(href)
            elif "linkedin.com" in href and not found["linkedin_url"]:
                found["linkedin_url"] = schema.norm_linkedin(href)
        texts.append(visible_text(html, 3000))
    text = " ".join(texts)
    for m in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", text):
        e = schema.norm_email(m)
        if e and e not in found["emails"]:
            found["emails"].append(e)
    found["emails"] = [e for e in found["emails"] if not any(j in e for j in _JUNK_EMAIL_PARTS)]
    found["text"] = text[:5000]
    return found


# ------------------------------------------------------------ web search

def serp_limit():
    return config.env_int("SERPAPI_MONTHLY_LIMIT", 100)


def serp_remaining():
    return max(0, serp_limit() - db.usage_get("serpapi"))


def serp_search(query, num=4):
    """One SerpAPI query -> [{title, link, snippet}]. Counts against the monthly
    budget. Swap this function to use a different search backend."""
    key = config.env("SERPAPI_KEY")
    if not key or serp_remaining() <= 0:
        return None
    db.usage_add("serpapi", 1)
    try:
        r = requests.get("https://serpapi.com/search",
                         params={"engine": "google", "q": query, "num": num, "api_key": key},
                         timeout=25)
        r.raise_for_status()
        return [{"title": x.get("title", ""), "link": x.get("link", ""), "snippet": x.get("snippet", "")}
                for x in r.json().get("organic_results", [])[:num]]
    except (requests.RequestException, ValueError):
        return []


# ------------------------------------------------------ validating results

def _tokens(s):
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 1]


def name_overlap(name, text):
    toks = [t for t in _tokens(name) if t not in ("llc", "inc", "the", "and", "co")]
    if not toks:
        return False
    low = (text or "").lower()
    return sum(1 for t in toks if t in low) >= max(1, (len(toks) + 1) // 2)


def supported_by(field, value, corpus):
    """Was this value actually seen in the material we gathered? (LLMs guess.)"""
    if not value or not corpus:
        return False
    low = corpus.lower()
    digits = re.sub(r"\D", "", corpus)
    if field == "email":
        return value.lower() in low
    if field == "phone":
        return value[-10:] in digits
    if field == "website":
        return schema.domain_of(value) in low
    if field == "instagram_url":
        return schema.instagram_handle(value).lower() in low
    if field in ("facebook_url", "linkedin_url"):
        seg = [s for s in urlparse(value).path.split("/") if s]
        return bool(seg) and seg[-1].lower() in low
    if field == "contact_name":
        return value.lower() in low
    if field == "address":
        toks = _tokens(value)
        return bool(toks) and sum(1 for t in toks if t in low) / len(toks) >= 0.6
    return False


_FIELD_NORMALISERS = {
    "email": schema.norm_email,
    "phone": schema.norm_phone,
    "website": schema.norm_url,
    "instagram_url": schema.norm_instagram,
    "facebook_url": schema.norm_facebook,
    "linkedin_url": schema.norm_linkedin,
    "contact_name": lambda v: re.sub(r"\s+", " ", str(v or "")).strip(),
    "address": lambda v: re.sub(r"\s+", " ", str(v or "")).strip(),
}


def _llm_extract(lead, missing, corpus):
    fields = ", ".join(missing)
    prompt = (
        "You are extracting contact details for ONE business from the material below.\n"
        f"Business: {lead['business_name']}\n"
        f"Location: {', '.join(x for x in (lead.get('city'), lead.get('state')) if x) or lead.get('address') or 'unknown'}\n\n"
        f"MATERIAL:\n{corpus}\n\n"
        f"Extract ONLY these fields: {fields}.\n"
        "Rules: use only what the material clearly states about THIS business (not similarly named "
        "businesses, directories or agencies). Never guess. Use null when not clearly present. "
        "URLs must be complete. 'contact_name' means the owner/founder/manager's personal name.\n"
        'Return ONLY a JSON object with those keys plus "confidence_notes" (a short string naming '
        "any field you are unsure about, or an empty string). No preamble, no markdown."
    )
    reply = llm.complete(prompt, task="enrich", max_tokens=900, temperature=0)
    return llm.extract_json(reply)


# ---------------------------------------------------------------- main API

def enrich_lead(lead, max_searches=None):
    """Look for the lead's missing contact details. Returns
    {"updates": {field: value}, "notes": str, "searches": int}. Doesn't write to the DB."""
    if max_searches is None:
        max_searches = config.env_int("ENRICH_MAX_SEARCHES", 3)
    missing = missing_fields(lead)
    updates, notes, searches = {}, [], 0
    if not missing:
        return {"updates": {}, "notes": "nothing missing", "searches": 0}

    corpus_parts = []

    # 1. the lead's own listing pages
    for label, url in (("Yelp page", lead.get("yelp_url")), ("Google Maps page", lead.get("google_maps_url"))):
        if url:
            t = fetch_profile_text(url)
            if t:
                corpus_parts.append(f"[{label}]\n{t}")

    # 2. the business's own website (free)
    site = lead.get("website") if lead.get("has_website") else ""
    if site:
        info = scan_website(site)
        if info["text"]:
            corpus_parts.append(f"[Business website]\n{info['text']}")
        if info["emails"] and not lead.get("email"):
            same = [e for e in info["emails"] if schema.domain_of(site) in e]
            updates["email"] = (same or info["emails"])[0]
        if info["phones"] and not lead.get("phone"):
            updates["phone"] = info["phones"][0]
        for f in ("instagram_url", "facebook_url", "linkedin_url"):
            if info[f] and not lead.get(f):
                updates[f] = info[f]

    # 3. web searches for what's still missing
    still = [f for f in missing if f not in updates]
    serp_results = []
    if config.env("SERPAPI_KEY") and max_searches > 0:
        for f in still:
            if searches >= max_searches or serp_remaining() <= 0:
                break
            loc = " " + ", ".join(x for x in (lead.get("city"), lead.get("state")) if x) if lead.get("city") else ""
            q = QUERY_TEMPLATES[f].format(n=lead["business_name"], l=loc)
            res = serp_search(q)
            if res is None:
                break
            searches += 1
            serp_results.append((f, res))
            corpus_parts.append(f"[Search: {q}]\n" + "\n".join(
                f"{r['title']} | {r['link']} | {r['snippet']}" for r in res))
        if searches and serp_remaining() <= 0:
            notes.append("monthly search budget used up")
    elif still:
        notes.append("no SERPAPI_KEY -- web search skipped")

    corpus = "\n\n".join(corpus_parts)[:14000]
    still = [f for f in missing if f not in updates]

    # 4. extract the rest
    if still and corpus:
        if llm.is_configured("enrich"):
            try:
                data = _llm_extract(lead, still, corpus)
                for f in still:
                    raw = data.get(f)
                    if raw in (None, "", "null"):
                        continue
                    val = _FIELD_NORMALISERS[f](raw)
                    if val and supported_by(f, val, corpus):
                        updates[f] = val
                    elif val:
                        notes.append(f"dropped unsupported {f}")
                if data.get("confidence_notes"):
                    notes.append(str(data["confidence_notes"]))
            except (llm.LLMError, ValueError) as e:
                notes.append(f"LLM extraction failed: {e}")
        else:
            # No LLM: accept a social profile only if the search result clearly names the business
            for f, res in serp_results:
                host = {"facebook_url": "facebook.com", "instagram_url": "instagram.com",
                        "linkedin_url": "linkedin.com"}.get(f)
                if not host or f in updates:
                    continue
                for r in res:
                    if host in r["link"] and name_overlap(lead["business_name"], r["title"] + " " + r["snippet"]):
                        val = _FIELD_NORMALISERS[f](r["link"])
                        if val:
                            updates[f] = val
                            break
            notes.append("LLM not configured -- only free website scan + obvious social matches used")

    # Never overwrite something that's already there
    updates = {k: v for k, v in updates.items()
               if v and (not lead.get(k) or (k == "website" and not lead.get("has_website")))}
    return {"updates": updates, "notes": "; ".join(n for n in notes if n), "searches": searches}


# ---------------------------------------------------------------- job entry

def run_enrich(ctx, params):
    filters = {"dnc": "no"}
    if params.get("ids"):
        filters["ids"] = params["ids"]
    if params.get("campaign"):
        filters["campaign"] = params["campaign"]
    if not params.get("force"):
        filters["enriched"] = "no"
    ids = db.select_ids(filters, limit=params.get("limit"))
    ctx.progress(0, len(ids))
    ctx.log(f"Enriching {len(ids)} leads (search budget left this month: {serp_remaining()})")

    total_found = total_searches = done = failed = 0
    for i, lead_id in enumerate(ids, 1):
        if ctx.stopped:
            ctx.log("Stopped.")
            break
        lead = db.get_lead(lead_id)
        try:
            res = enrich_lead(lead)
        except Exception as e:  # noqa: BLE001 -- one bad lead shouldn't stop the batch
            failed += 1
            ctx.log(f"{lead['business_name']}: failed ({e}) -- will retry next run")
            ctx.progress(i)
            continue
        fields = dict(res["updates"])
        fields["enriched_at"] = schema.now_iso()
        note = res["notes"]
        if note:
            fields["enrichment_notes"] = ((lead.get("enrichment_notes") or "") + " " + note).strip()
        db.update_lead(lead_id, fields)
        if res["updates"]:
            db.add_event(lead_id, "enriched", ", ".join(res["updates"]))
        total_found += len(res["updates"])
        total_searches += res["searches"]
        done += 1
        ctx.log(f"{lead['business_name']}: " + (", ".join(res["updates"]) or "nothing new found"))
        ctx.progress(i)
        if ctx.sleep(0.3):
            break
    return (f"{done} enriched, {total_found} new fields found, {total_searches} searches used"
            + (f", {failed} failed" if failed else ""))
