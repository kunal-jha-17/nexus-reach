"""
apify.py -- Google Maps leads through Apify's Google Maps Scraper, instead of a browser on this server.

Why: the built-in scraper runs a real Chrome here, so it's limited by this server's memory (about
40 listings a search on Render's free plan) and Google sometimes shows it a CAPTCHA. Apify runs the
scrape on its own machines and hands back the results as data -- hundreds or thousands of places a
search, with rating, hours, place id and (optionally) emails and social profiles found on each
business's website.

Needs APIFY_TOKEN (Settings > Connections; apify.com > Settings > Integrations). Apify charges your
account per place found (about $4 per 1,000 for this actor at the time of writing; every Apify
account gets some free usage each month) -- the run log reports what each run cost.
"""
import time

import requests

import config

API = "https://api.apify.com/v2"
DEFAULT_ACTOR = "compass~crawler-google-places"
MAX_PLACES_PER_SEARCH = 5000
POLL_SECONDS = 10
RUN_TIMEOUT_SECONDS = 3 * 3600


class ApifyError(RuntimeError):
    pass


def configured():
    return bool(config.env("APIFY_TOKEN"))


def _actor():
    return (config._server_env("APIFY_MAPS_ACTOR") or DEFAULT_ACTOR).replace("/", "~")


def _call(method, path, **kw):
    token = config.env("APIFY_TOKEN")
    if not token:
        raise ApifyError("Add your Apify API token under Settings > Connections first.")
    try:
        r = requests.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"},
                             timeout=kw.pop("timeout", 60), **kw)
    except requests.RequestException as e:
        raise ApifyError(f"Couldn't reach Apify ({e}).")
    if r.status_code == 401:
        raise ApifyError("Apify refused the API token. Copy it again from apify.com > Settings > Integrations.")
    if r.status_code == 402:
        raise ApifyError("Your Apify account is out of credit (or its monthly limit is reached). Top up on apify.com.")
    if r.status_code >= 400:
        try:
            why = (r.json().get("error") or {}).get("message") or r.text[:200]
        except ValueError:
            why = r.text[:200]
        raise ApifyError(f"Apify said: {why}")
    try:
        return r.json()
    except ValueError:
        raise ApifyError("Apify sent back something that isn't data.")


def test():
    me = _call("GET", "/users/me", timeout=20).get("data") or {}
    plan = (me.get("plan") or {}).get("id") or "free"
    return {"ok": True, "message": f"Connected to Apify as {me.get('username') or 'you'} ({plan} plan)."}


def build_input(query, location, max_places, contacts=False):
    """The actor's input. Images and reviews are switched off: they cost extra and aren't used."""
    data = {
        "searchStringsArray": [query],
        "maxCrawledPlacesPerSearch": int(max_places),
        "language": "en",
        "skipClosedPlaces": True,
        "maxImages": 0,
        "maxReviews": 0,
        "scrapeContacts": bool(contacts),
    }
    if location:
        data["locationQuery"] = location
    return data


def to_lead(item):
    """One Apify place -> the app's lead fields (anything missing is just left blank)."""
    def first(v):
        if isinstance(v, list):
            return next((x for x in v if x), "")
        return v or ""
    hours = item.get("openingHours")
    if isinstance(hours, list):
        hours = "; ".join(f"{h.get('day', '')}: {h.get('hours', '')}".strip(": ") for h in hours if isinstance(h, dict))
    category = item.get("categoryName") or first(item.get("categories"))
    address = item.get("address") or ", ".join(x for x in (item.get("street"), item.get("city"), item.get("state"),
                                                            item.get("postalCode")) if x)
    return {
        "business_name": item.get("title") or "",
        "phone": item.get("phone") or item.get("phoneUnformatted") or "",
        "website": item.get("website") or "",
        "address": address or "",
        "city": item.get("city") or "",
        "state": item.get("state") or "",
        "country": item.get("countryCode") or "",
        "rating": item.get("totalScore"),
        "review_count": item.get("reviewsCount") or 0,
        "category_raw": category or "",
        "google_maps_url": item.get("url") or "",
        "place_id": item.get("placeId") or "",
        "opening_hours": hours or "",
        "email": first(item.get("emails")),
        "instagram_url": first(item.get("instagrams")),
        "facebook_url": first(item.get("facebooks")),
        "linkedin_url": first(item.get("linkedIns")),
        "notes": f"Apify Google Maps: {item.get('searchString') or ''}".strip(),
    }


def run_search(query, location, max_places, ctx=None, on_row=None, contacts=False, log=None):
    """Run one search on Apify and hand each place to on_row as the results come in.
    Returns (places received, cost in USD or None). Stopping the job aborts the Apify run."""
    log = log or (lambda m: None)
    max_places = max(1, min(MAX_PLACES_PER_SEARCH, int(max_places)))
    run = _call("POST", f"/acts/{_actor()}/runs", json=build_input(query, location, max_places, contacts)).get("data") or {}
    run_id, dataset = run.get("id"), run.get("defaultDatasetId")
    if not run_id:
        raise ApifyError("Apify didn't start the run.")
    log(f"Apify run started (up to {max_places} places). Results are saved as they arrive.")
    offset, got, started, status, info = 0, 0, time.monotonic(), run.get("status"), run
    while True:
        stopped = bool(ctx and ctx.stopped)
        if stopped:
            try:
                _call("POST", f"/actor-runs/{run_id}/abort", timeout=20)
            except ApifyError:
                pass
            log("Stopped -- the Apify run was aborted.")
        # read whatever has arrived so far
        while dataset:
            items = _call("GET", f"/datasets/{dataset}/items", params={"clean": "true", "format": "json",
                                                                       "offset": offset, "limit": 500})
            if not isinstance(items, list) or not items:
                break
            offset += len(items)
            for item in items:
                if item.get("permanentlyClosed"):
                    continue
                got += 1
                if on_row is not None and on_row(to_lead(item)) is False:
                    stopped = True
                    break
            if stopped:
                break
        if stopped or status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"):
            break
        if time.monotonic() - started > RUN_TIMEOUT_SECONDS:
            raise ApifyError("The Apify run took more than 3 hours -- check it on apify.com.")
        if ctx is not None and hasattr(ctx, "sleep"):
            if ctx.sleep(POLL_SECONDS):
                continue
        else:
            time.sleep(POLL_SECONDS)
        info = _call("GET", f"/actor-runs/{run_id}", timeout=30).get("data") or {}
        status, dataset = info.get("status"), info.get("defaultDatasetId") or dataset
        if status == "SUCCEEDED":
            continue                                    # one more pass to read the last items
    if status in ("FAILED", "TIMED-OUT", "TIMED_OUT"):
        raise ApifyError(f"The Apify run ended with status {status} after {got} places -- see it on apify.com.")
    cost = info.get("usageTotalUsd")
    return got, (float(cost) if isinstance(cost, (int, float)) else None)
