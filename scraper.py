"""
scraper.py -- find leads on Google Maps, Yelp and Instagram with a real
(headless) browser, and save them straight into the hub.

IMPORTANT: these scrapers were written from the sites' usual markup and have
NOT been run against the live sites. Expect to adjust CSS selectors on first
use (each one is a single, clearly named line in the extract_* functions).
A run that matches nothing just returns 0 results -- it never crashes the app.

Compared with the original scraper, this version:
  * keeps each listing's URL (google_maps_url / yelp_url) so enrichment can use it
  * decodes Yelp's biz_redir and Instagram's l.instagram.com links to the real website
  * stops with a clear message when Google/Yelp show a CAPTCHA, or Instagram
    asks for a checkpoint, instead of silently returning nothing
  * never runs two browsers at once (jobs.SCRAPE_LOCK)

Playwright is imported lazily, so the rest of the app runs without it:
    pip install playwright && python3 -m playwright install chromium
"""
import os
import random
import re
from urllib.parse import quote_plus

import config
import importer
import schema

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{7,}\d)")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class ScrapeBlocked(RuntimeError):
    """The site is showing a CAPTCHA / login wall / checkpoint."""


class BrowserNotInstalled(RuntimeError):
    """Playwright's Python package is present but the actual browser binary isn't."""


def browser_available():
    """True if a real, launchable Chromium exists -- without paying the cost of
    launching one. Playwright's pip package can be installed with no browser
    downloaded (e.g. a plain `pip install -r requirements.txt` on a host that
    never ran `playwright install`), which fails only once someone clicks Scrape."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
        return bool(path) and os.path.exists(path)
    except Exception:  # noqa: BLE001 -- any Playwright-internal failure just means "not available"
        return False


def _headless():
    return config.env_bool("SCRAPER_HEADLESS", True)


def _delay(ctx, a=1.0, b=2.5):
    """Randomised human-ish pause; returns True if the job was stopped."""
    t = random.uniform(a, b)
    if ctx is not None:
        return ctx.sleep(t)
    import time
    time.sleep(t)
    return False


def _new_page(p):
    # SCRAPER_BROWSER_CHANNEL=chrome makes Playwright use your installed Google Chrome,
    # so there's no separate browser download (`playwright install chromium`) at all.
    kwargs = {"headless": _headless()}
    channel = config.env("SCRAPER_BROWSER_CHANNEL")
    if channel:
        kwargs["channel"] = channel
    elif not (p.chromium.executable_path and os.path.exists(p.chromium.executable_path)):
        raise BrowserNotInstalled()
    browser = p.chromium.launch(**kwargs)
    context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
    return browser, context


def _blocked(page, needles, who):
    try:
        html = page.content().lower()
    except Exception:  # noqa: BLE001
        return
    if any(n in html for n in needles):
        raise ScrapeBlocked(f"{who} is showing a CAPTCHA / verification page. Wait a while, "
                            f"lower max results, or scrape by hand and import the CSV instead.")


# ------------------------------------------------------- page extractors
# (pure functions of a Playwright page -- easy to fix when markup changes)

def _text(page, sel):
    el = page.query_selector(sel)
    return el.inner_text().strip() if el else ""


def _attr(page, sel, name):
    el = page.query_selector(sel)
    return (el.get_attribute(name) or "").strip() if el else ""


def _digits(s):
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else 0


def extract_maps_detail(page):
    """Google Maps place page -> canonical lead dict."""
    phone = _attr(page, 'button[data-item-id^="phone:"]', "aria-label").replace("Phone:", "").strip()
    address = _attr(page, 'button[data-item-id="address"]', "aria-label").replace("Address:", "").strip()
    return {
        "business_name": _text(page, "h1"),
        "phone": phone,
        "website": _attr(page, 'a[data-item-id="authority"]', "href"),
        "address": address,
        "category_raw": _text(page, 'button[jsaction*="category"]'),
        "review_count": _digits(_attr(page, 'span[aria-label*="reviews"]', "aria-label")),
    }


def extract_yelp_detail(page):
    """Yelp business page -> canonical lead dict. The 'website' Yelp exposes is a
    biz_redir link; schema.normalize_lead decodes it to the real URL."""
    phone = _text(page, 'p[class*="phone"]') or _text(page, 'span[class*="phone"]')
    if not phone:
        tel = _attr(page, 'a[href^="tel:"]', "href")
        phone = tel.replace("tel:", "")
    return {
        "business_name": _text(page, "h1"),
        "phone": phone,
        "website": _attr(page, 'a[href*="biz_redir"]', "href"),
        "address": _text(page, "address"),
        "category_raw": _text(page, 'span[class*="category"]'),
        "review_count": _digits(_text(page, 'span[class*="reviewCount"]')),
    }


def extract_instagram_profile(page, uname):
    header_text = page.inner_text("header") if page.query_selector("header") else ""
    email = EMAIL_RE.search(header_text)
    phone = PHONE_RE.search(header_text)
    return {
        "business_name": uname,
        "phone": phone.group(0) if phone else "",
        "email": email.group(0) if email else "",
        "website": _attr(page, 'header a[href*="l.instagram.com"]', "href"),
        "instagram_url": f"https://www.instagram.com/{uname}/",
    }


# ------------------------------------------------------------- scrapers

def scrape_google_maps(query, location="", max_results=30, progress=None, ctx=None):
    from playwright.sync_api import sync_playwright
    progress = progress or (lambda m: None)
    full_query = f"{query} {location}".strip() if location and location.lower() not in query.lower() else query
    results = []
    with sync_playwright() as p:
        browser, context = _new_page(p)
        try:
            page = context.new_page()
            page.goto(f"https://www.google.com/maps/search/{quote_plus(full_query)}", timeout=60000)
            _delay(ctx, 2, 3)
            _blocked(page, ("unusual traffic", "recaptcha"), "Google")
            feed = 'div[role="feed"]'
            try:
                page.wait_for_selector(feed, timeout=15000)
            except Exception:  # noqa: BLE001
                progress("No results list appeared (no matches, or Google changed its layout).")
                return results

            links, stagnant = [], 0
            while len(links) < max_results and stagnant < 5 and not (ctx and ctx.stopped):
                before = len(links)
                for a in page.query_selector_all(f'{feed} a[href*="/maps/place/"]'):
                    href = a.get_attribute("href")
                    if href and href not in links:
                        links.append(href)
                stagnant = stagnant + 1 if len(links) == before else 0
                page.evaluate(f"document.querySelector('{feed}').scrollBy(0, 800)")
                _delay(ctx, 1, 2)
                progress(f"Found {len(links)} listings so far...")
            links = links[:max_results]

            for i, link in enumerate(links, 1):
                if ctx and ctx.stopped:
                    break
                detail = context.new_page()
                try:
                    detail.goto(link, timeout=30000)
                    _delay(ctx, 1.5, 2.5)
                    row = extract_maps_detail(detail)
                    row["google_maps_url"] = link
                    row["notes"] = f"scraped from Google Maps: {full_query}"
                    results.append(row)
                    progress(f"Scraped {i}/{len(links)}: {row['business_name'] or '(no name)'}")
                except Exception as e:  # noqa: BLE001
                    progress(f"Skipped one listing ({e})")
                finally:
                    detail.close()
        finally:
            browser.close()
    return results


def scrape_yelp(query, location, max_results=30, progress=None, ctx=None):
    from playwright.sync_api import sync_playwright
    progress = progress or (lambda m: None)
    if not location:
        raise ValueError("Yelp needs a location, e.g. 'Dallas, TX'")
    results, links, start = [], [], 0
    with sync_playwright() as p:
        browser, context = _new_page(p)
        try:
            page = context.new_page()
            while len(links) < max_results and start < 240 and not (ctx and ctx.stopped):
                page.goto(f"https://www.yelp.com/search?find_desc={quote_plus(query)}"
                          f"&find_loc={quote_plus(location)}&start={start}", timeout=60000)
                _delay(ctx, 1.5, 2.5)
                _blocked(page, ("captcha", "are you a robot"), "Yelp")
                found = []
                for a in page.query_selector_all('a[href*="/biz/"]'):
                    href = (a.get_attribute("href") or "").split("?")[0]
                    m = re.match(r"^(?:https://www\.yelp\.com)?(/biz/[^/]+)$", href)
                    if m:
                        url = "https://www.yelp.com" + m.group(1)
                        if url not in links and url not in found:
                            found.append(url)
                if not found:
                    break
                links.extend(found)
                start += 10
                progress(f"Found {len(links)} Yelp listings so far...")
            links = links[:max_results]

            for i, link in enumerate(links, 1):
                if ctx and ctx.stopped:
                    break
                detail = context.new_page()
                try:
                    detail.goto(link, timeout=30000)
                    _delay(ctx, 1.5, 2.5)
                    row = extract_yelp_detail(detail)
                    row["yelp_url"] = link
                    row["notes"] = f"scraped from Yelp: {query} in {location}"
                    results.append(row)
                    progress(f"Scraped {i}/{len(links)}: {row['business_name'] or '(no name)'}")
                except Exception as e:  # noqa: BLE001
                    progress(f"Skipped one listing ({e})")
                finally:
                    detail.close()
        finally:
            browser.close()
    return results


def scrape_instagram(tag, ig_username, ig_password, max_results=30, progress=None, ctx=None):
    """Highest ban-risk scraper: use a BURNER account, keep max_results small (20-40)."""
    from playwright.sync_api import sync_playwright
    progress = progress or (lambda m: None)
    tag = tag.strip().lstrip("#").replace(" ", "")
    results = []
    with sync_playwright() as p:
        browser, context = _new_page(p)
        try:
            page = context.new_page()
            page.goto("https://www.instagram.com/accounts/login/", timeout=60000)
            _delay(ctx, 2, 3)
            try:
                page.fill('input[name="username"]', ig_username)
                _delay(ctx, 0.5, 1)
                page.fill('input[name="password"]', ig_password)
                _delay(ctx, 0.5, 1)
                page.click('button[type="submit"]')
                _delay(ctx, 4, 6)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Instagram login failed -- check credentials/selectors: {e}")
            if any(x in page.url for x in ("challenge", "checkpoint", "suspended", "accounts/login")):
                raise ScrapeBlocked("Instagram wants verification (challenge/checkpoint) or the login "
                                    "failed. Log in once by hand with this account, then retry.")
            progress("Logged into Instagram, browsing the tag page...")

            page.goto(f"https://www.instagram.com/explore/tags/{tag}/", timeout=60000)
            _delay(ctx, 2, 3)
            posts, stagnant = [], 0
            while len(posts) < max_results and stagnant < 5 and not (ctx and ctx.stopped):
                before = len(posts)
                for a in page.query_selector_all('a[href*="/p/"]'):
                    href = a.get_attribute("href")
                    if href:
                        full = f"https://www.instagram.com{href}" if href.startswith("/") else href
                        if full not in posts:
                            posts.append(full)
                stagnant = stagnant + 1 if len(posts) == before else 0
                page.mouse.wheel(0, 1200)
                _delay(ctx, 1.5, 3)
                progress(f"Found {len(posts)} posts so far...")

            usernames = []
            for link in posts[:max_results]:
                if ctx and ctx.stopped:
                    break
                detail = context.new_page()
                try:
                    detail.goto(link, timeout=30000)
                    _delay(ctx, 1.5, 2.5)
                    href = _attr(detail, "header a", "href")
                    uname = href.strip("/").split("/")[-1] if href else ""
                    if uname and uname not in usernames:
                        usernames.append(uname)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    detail.close()

            for i, uname in enumerate(usernames, 1):
                if ctx and ctx.stopped:
                    break
                prof = context.new_page()
                try:
                    prof.goto(f"https://www.instagram.com/{uname}/", timeout=30000)
                    _delay(ctx, 2, 3)
                    row = extract_instagram_profile(prof, uname)
                    row["notes"] = f"scraped from Instagram #{tag}"
                    results.append(row)
                    progress(f"Scraped profile {i}/{len(usernames)}: @{uname}")
                    _delay(ctx, 2, 4)
                except Exception as e:  # noqa: BLE001
                    progress(f"Skipped @{uname} ({e})")
                finally:
                    prof.close()
        finally:
            browser.close()
    return results


# --------------------------------------------------------------- job entry

def run_scrape(ctx, params):
    """Background job: scrape one platform and save the leads."""
    platform = params["platform"]
    query = (params.get("query") or "").strip()
    location = (params.get("location") or "").strip()
    max_results = int(params.get("max_results") or 30)
    campaign = (params.get("campaign") or "").strip()
    if not query:
        raise ValueError("Enter a search query")
    if ctx.stopped:
        return "Stopped before starting"

    ctx.log(f"Starting {platform} scrape for '{query}' {location}".strip())
    try:
        if platform == "google_maps":
            rows = scrape_google_maps(query, location, max_results, ctx.log, ctx)
        elif platform == "yelp":
            rows = scrape_yelp(query, location, max_results, ctx.log, ctx)
        elif platform == "instagram":
            user, pw = config.env("IG_USERNAME"), config.env("IG_PASSWORD")
            if not user or not pw:
                raise RuntimeError("Set IG_USERNAME and IG_PASSWORD (a burner account) in .env first")
            rows = scrape_instagram(query, user, pw, max_results, ctx.log, ctx)
        else:
            raise ValueError(f"Unknown platform '{platform}'")
    except ImportError:
        raise RuntimeError("Playwright isn't installed. Run: pip install playwright && "
                           "python3 -m playwright install chromium")
    except BrowserNotInstalled:
        raise RuntimeError(
            "The scraping browser isn't installed on this server. This is expected on most free "
            "hosts (Render's native Python service doesn't let a build step install a browser) -- "
            "run scrapes on your own computer instead, then import the resulting CSV here under "
            "Find leads. If you deployed with the included Dockerfile, this shouldn't happen; check "
            "the build logs for a Playwright install error.")

    checked = len(rows)
    if params.get("only_no_website"):
        # A listing that shows a real website is, by definition, not a "no website" prospect -- and
        # every such lead would be rejected at the fit step anyway, after spending enrichment,
        # search and AI budget on it. Drop them here instead.
        rows = [r for r in rows if not schema.normalize_lead(r)["has_website"]]
        ctx.log(f"Checked {checked} listings: kept {len(rows)} with no website, "
                f"skipped {checked - len(rows)} that already have one")
    summary = importer.import_leads(rows, campaign=campaign, source=platform, query=query)
    msg = (f"{len(rows)} scraped -> {summary['inserted']} new, {summary['merged']} already known"
           + (f", {summary['skipped_suppressed']} on do-not-contact" if summary["skipped_suppressed"] else ""))
    ctx.log(msg)
    if params.get("only_no_website"):
        msg = f"{checked} checked, {len(rows)} had no website -> " + msg.split(" -> ", 1)[1]
    if not checked:
        msg += " (0 results usually means the site's markup changed or a CAPTCHA appeared)"
    elif not rows and params.get("only_no_website"):
        msg += (" (every business checked already has a website -- try a more specific search, "
                "a suburb name, or a higher 'listings to check' number)")
    return msg
