"""
scraper.py -- find leads on Google Maps, Yelp and Instagram with a real
(headless) browser, and save them straight into the hub.

IMPORTANT: the selectors here follow the sites' usual markup and can break
without warning when a site changes its layout. Each one is a single, clearly
named line (the extract_* functions and the *_JS snippets below). A run that
matches nothing just reports 0 results -- it never crashes the app.

How a run works:
  * the results list is read in ONE browser call per scroll (no element handles
    are held between scrolls, so a re-rendered list can't hang the run)
  * every listing is saved the moment it's read, so a restart or an error
    half-way through never loses the listings already found
  * a listing that is already in your leads is not visited again, so running
    the same search after an interruption simply carries on where it stopped
  * with "only keep businesses with no website" on, a Google Maps result whose
    card already shows a Website button is skipped without opening its page
  * images are not downloaded, listing pages reuse tabs that are recycled regularly, and the
    run watches its own memory: on a small server it stops growing the list / reopens tabs /
    ends cleanly instead of being killed and restarted
  * never runs two browsers at once (jobs.SCRAPE_LOCK)

Tunable with environment variables (all optional):
  SCRAPER_SCROLL_DELAY_MIN / _MAX   pause between scrolls of the results list   (0.7 / 1.3 s)
  SCRAPER_DETAIL_DELAY_MIN / _MAX   pause between batches of listing pages      (0.4 / 0.9 s)
  SCRAPER_DETAIL_TABS               listing pages loaded at once, 1-4           (1)
  SCRAPER_MEMORY_LIMIT_MB           the server's memory; the scraper backs off at 85% of it
                                    (container limit if reported, 512 on Render, 0 = off)
  SCRAPER_RECYCLE_EVERY             open fresh tabs after this many listings    (20)
  SCRAPER_BLOCK_MEDIA               don't download images                       (true)
Lower delays and more tabs are faster but raise the odds of a CAPTCHA.

Playwright is imported lazily, so the rest of the app runs without it:
    pip install playwright && python3 -m playwright install chromium
"""
import os
import random
import re
import time
from urllib.parse import quote_plus

import config
import db
import importer
import schema

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{7,}\d)")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAPS_SEARCH_URL = "https://www.google.com/maps/search/"
YELP_BASE_URL = "https://www.yelp.com"

# Keeps Chromium small and stable inside a container: Docker's /dev/shm is only 64 MB, and a
# long results list overflows it (the tab crashes) unless shared memory is moved to /tmp.
_BROWSER_ARGS = ["--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
                 "--disable-background-networking", "--mute-audio", "--no-first-run",
                 # one renderer per tab instead of one per embedded site: far fewer processes
                 "--disable-site-isolation-trials", "--disable-features=IsolateOrigins,site-per-process"]
# Photos are most of a listing page's weight and none of its data.
_NO_IMAGES_ARG = "--blink-settings=imagesEnabled=false"


class ScrapeBlocked(RuntimeError):
    """The site is showing a CAPTCHA / login wall / checkpoint."""


class ScrapeOutOfMemory(RuntimeError):
    """The server is at its memory limit; this search was ended early (its leads are saved)."""


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


def _env_float(name, default):
    try:
        return max(0.0, float(config.env(name, "") or default))
    except (TypeError, ValueError):
        return default


def _delay_range(kind, lo, hi):
    """(min, max) seconds for SCRAPER_<kind>_DELAY_MIN / _MAX, always a valid range."""
    a = _env_float(f"SCRAPER_{kind}_DELAY_MIN", lo)
    b = _env_float(f"SCRAPER_{kind}_DELAY_MAX", hi)
    return (a, max(a, b))


def _env_count(name, default, lo, hi):
    return max(lo, min(config.env_int(name, default), hi))


def _delay(ctx, a=1.0, b=2.5):
    """Randomised human-ish pause; returns True if the job was stopped."""
    t = random.uniform(a, b)
    if ctx is not None:
        return ctx.sleep(t)
    time.sleep(t)
    return False


def _stopped(ctx):
    return bool(ctx is not None and ctx.stopped)


def _report(ctx, done, total):
    if ctx is not None and hasattr(ctx, "progress"):
        ctx.progress(done, total)


def _short(e, n=140):
    return str(e).strip().split("\n")[0][:n]


def _new_page(p):
    # SCRAPER_BROWSER_CHANNEL=chrome makes Playwright use your installed Google Chrome,
    # so there's no separate browser download (`playwright install chromium`) at all.
    args = list(_BROWSER_ARGS)
    if config.env_bool("SCRAPER_BLOCK_MEDIA", True):
        args.append(_NO_IMAGES_ARG)
    kwargs = {"headless": _headless(), "args": args}
    channel = config.env("SCRAPER_BROWSER_CHANNEL")
    if channel:
        kwargs["channel"] = channel
    elif not (p.chromium.executable_path and os.path.exists(p.chromium.executable_path)):
        raise BrowserNotInstalled()
    browser = p.chromium.launch(**kwargs)
    context = browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                  viewport={"width": 1000, "height": 720})
    context.set_default_timeout(15000)      # no single click/read may hang a run for 30 s
    return browser, context


def _blocked(page, needles, who):
    try:
        html = page.content().lower()
    except Exception:  # noqa: BLE001
        return
    if any(n in html for n in needles):
        raise ScrapeBlocked(f"{who} is showing a CAPTCHA / verification page. Wait a while, "
                            f"lower max results, or scrape by hand and import the CSV instead.")


# ------------------------------------------------------------ memory guard
# A small server (Render's 512 MB instances) is killed and restarted outright when the app plus
# the browser outgrow it -- which is what "Interrupted by a restart" on a large scrape is. So the
# scraper watches its own footprint and backs off BEFORE that point: it stops growing the results
# list, reopens tabs, or ends the run cleanly with everything found so far already saved.

def _tree_mb():
    """Memory used by this process and everything it started (the browser), in MB.
    Uses proportional size where the kernel offers it, so shared pages aren't counted twice.
    Returns 0 where /proc isn't available (macOS / Windows) -- the guard is then off."""
    try:
        parents = {}
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open(f"/proc/{name}/stat") as f:
                        parents[int(name)] = int(f.read().rsplit(")", 1)[1].split()[1])
                except (OSError, ValueError, IndexError):
                    continue
        tree, grew = {os.getpid()}, True
        while grew:
            grew = False
            for pid, ppid in parents.items():
                if ppid in tree and pid not in tree:
                    tree.add(pid)
                    grew = True
        kb = 0
        for pid in tree:
            got = None
            try:
                with open(f"/proc/{pid}/smaps_rollup") as f:
                    for line in f:
                        if line.startswith("Pss:"):
                            got = int(line.split()[1])
                            break
            except (OSError, ValueError):
                pass
            if got is None:
                try:
                    with open(f"/proc/{pid}/statm") as f:
                        got = int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
                except (OSError, ValueError, IndexError):
                    got = 0
            kb += got
        return kb / 1024
    except OSError:
        return 0


def _memory_limit_mb():
    """How much memory this server may use. SCRAPER_MEMORY_LIMIT_MB sets it (0 = no guard);
    otherwise the container's own limit if it reports one; otherwise 512 on Render; otherwise none."""
    raw = config.env("SCRAPER_MEMORY_LIMIT_MB", "")
    if raw != "":
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v.isdigit() and int(v) < (64 << 30):
                return int(v) / (1 << 20)
        except OSError:
            continue
    return 512.0 if os.environ.get("RENDER") else 0.0


def _container_mb():
    """What the container itself reports as in use (the number its out-of-memory killer goes by),
    in MB -- or 0 when it reports no limit of its own, since the figure would then be for the
    whole machine. Cache that the kernel can drop at will isn't counted."""
    for limit_path, used_path, stat_path, key in (
            ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", "inactive_file"),
            ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes",
             "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file")):
        try:
            with open(limit_path) as f:
                limit = f.read().strip()
            if not (limit.isdigit() and int(limit) < (64 << 30)):
                continue
            with open(used_path) as f:
                used = int(f.read().strip())
            try:
                with open(stat_path) as f:
                    for line in f:
                        if line.startswith(key + " "):
                            used -= int(line.split()[1])
                            break
            except (OSError, ValueError):
                pass
            return max(0, used) / (1 << 20)
        except (OSError, ValueError):
            continue
    return 0.0


def _used_mb():
    """Memory in use, by the stricter of two measures: our own process tree, and the container's count."""
    return max(_tree_mb(), _container_mb())


def _memory_tight():
    limit = _memory_limit_mb()
    return bool(limit) and _used_mb() > limit * 0.85


def _memory_note():
    """'412 MB of 512' for the run log, or '' when there's no limit to compare against."""
    limit = _memory_limit_mb()
    return f"{int(_used_mb())} MB of {int(limit)}" if limit else ""


def listing_key(url):
    """A stable identity for a Google Maps / Yelp listing URL, so the same place is
    recognised whatever tracking parameters the link carries. Google Maps links hold
    the place's feature id (!1s0x...:0x...); everything else falls back to the path."""
    u = (url or "").strip()
    if not u:
        return ""
    m = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", u)
    if m:
        return m.group(1).lower()
    return re.sub(r"^https?://(www\.)?", "", u.split("?")[0].split("#")[0]).rstrip("/").lower()


# ------------------------------------------------------- page extractors
# (pure functions of a Playwright page -- easy to fix when markup changes)

def _text(page, sel):
    # One atomic call: nothing is held between "find" and "read", so an element that the
    # page re-renders in between can't leave us waiting on a stale handle.
    return (page.evaluate("s => { const e = document.querySelector(s); return e ? (e.innerText || '') : ''; }",
                          sel) or "").strip()


def _attr(page, sel, name):
    return (page.evaluate("([s, n]) => { const e = document.querySelector(s); "
                          "return e ? (e.getAttribute(n) || '') : ''; }", [sel, name]) or "").strip()


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
    header_text = _text(page, "header")
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

_MAPS_FEED = 'div[role="feed"]'
_MAPS_STALL_SECONDS = 8
_READY_SECONDS = (10, 5)       # how long a listing page gets to draw: first try, then one retry
# Reads every result card currently in the list AND scrolls to the bottom, in one call.
# `website` is the card's own "Website" button, when Google shows one.
_MAPS_COLLECT_JS = """
(sel) => {
  const feed = document.querySelector(sel);
  if (!feed) return null;
  const items = [];
  for (const a of feed.querySelectorAll('a[href*="/maps/place/"]')) {
    const card = a.closest('[role="article"]') || a.parentElement;
    const w = card ? card.querySelector('a[data-value="Website"]') : null;
    items.push({href: a.href, website: w ? (w.href || '') : ''});
  }
  const tail = feed.lastElementChild ? (feed.lastElementChild.textContent || '') : '';
  feed.scrollTo(0, feed.scrollHeight);
  return {items: items, end: /reached the end of the list/i.test(tail)};
}
"""
_MAPS_NUDGE_JS = "(sel) => { const f = document.querySelector(sel); if (f) f.scrollBy(0, -500); }"
# A place page is ready once its name and its info block (address / phone / website) are drawn.
_MAPS_READY_JS = ("() => { const h = document.querySelector('h1'); "
                  "return !!(h && h.innerText.trim() && document.querySelector('[data-item-id]')); }")
_NAME_READY_JS = "() => { const h = document.querySelector('h1'); return !!(h && h.innerText.trim()); }"
_HREFS_JS = "(sel) => Array.from(document.querySelectorAll(sel), a => a.getAttribute('href') || '')"


def _wait_ready(page, js, seconds):
    """Poll until the page says it's drawn (or time runs out). Plain evaluate calls, so it
    works under any site's content-security policy and never raises mid-navigation."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if page.evaluate(js):
                return True
        except Exception:  # noqa: BLE001 -- still navigating
            pass
        page.wait_for_timeout(150)
    return False


def _close_all(pages):
    for pg in pages:
        try:
            pg.close()
        except Exception:  # noqa: BLE001
            pass


def _scrape_details(context, links, extract, finish, results, progress, ctx, on_row,
                    ready_js, who, needles, max_tabs=4):
    """Open each listing page, read it, and hand the row to on_row straight away.

    Pages load `tabs` at a time in tabs that are reused (and replaced every so often, which
    is what keeps a 100+ listing run from growing until the server kills it). One bad listing
    is skipped; several in a row means the site is refusing us, and the run stops with a
    clear message -- everything read up to that point is already saved."""
    total = len(links)
    n_tabs = min(_env_count("SCRAPER_DETAIL_TABS", 1, 1, 4), max_tabs)
    tight_rounds = 0
    recycle = _env_count("SCRAPER_RECYCLE_EVERY", 20, 5, 500)
    pause = _delay_range("DETAIL", 0.4, 0.9)
    tabs, since_recycle, done, fails = [], 0, 0, 0
    _report(ctx, 0, total)
    try:
        for start in range(0, total, n_tabs):
            if _stopped(ctx):
                break
            batch = links[start:start + n_tabs]
            if not tabs or since_recycle >= recycle:
                _close_all(tabs)
                tabs, since_recycle = [context.new_page() for _ in range(n_tabs)], 0
            opened = []
            for tab, link in zip(tabs, batch):          # start them all loading...
                try:
                    tab.goto(link, wait_until="commit", timeout=30000)
                    opened.append((tab, link, None))
                except Exception as e:  # noqa: BLE001
                    opened.append((tab, link, e))
            keep_going = True
            for tab, link, err in opened:               # ...then read each as it becomes ready
                done += 1
                since_recycle += 1
                row = None
                try:
                    if err is not None:
                        raise err
                    for attempt in (1, 2):
                        _wait_ready(tab, ready_js, _READY_SECONDS[attempt - 1])
                        tab.wait_for_timeout(200)       # let the rest of the info block settle
                        row = extract(tab)
                        if row.get("business_name"):
                            break
                        row = None
                    if row is None:
                        raise RuntimeError("the page didn't finish loading")
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    progress(f"Skipped listing {done}/{total} ({_short(e)})")
                    try:                                 # a tab that failed may be wedged; swap it
                        i = tabs.index(tab)
                        tab.close()
                        tabs[i] = context.new_page()
                    except Exception:  # noqa: BLE001
                        pass
                    _report(ctx, done, total)
                    continue
                fails = 0
                finish(row, link)
                results.append(row)
                progress(f"Scraped {done}/{total}: {row['business_name']}"
                         + (f"  [memory {_memory_note()}]" if done % 10 == 0 and _memory_note() else ""))
                _report(ctx, done, total)
                if on_row is not None and on_row(row) is False:
                    keep_going = False
                    break
            if not keep_going:
                break
            if fails >= 6:
                if tabs:
                    _blocked(tabs[0], needles, who)
                raise ScrapeBlocked(f"{fails} listings in a row wouldn't load -- {who} is probably rate-limiting "
                                    f"this server. Wait a while and run the same search again; it carries on "
                                    f"from where this one stopped.")
            if _memory_tight():
                # Fresh tabs give back what the old ones had accumulated. If that isn't enough twice
                # running, end here on our own terms rather than let the server be killed mid-run.
                tight_rounds += 1
                _close_all(tabs)
                tabs, n_tabs = [], 1
                time.sleep(1.0)
                if tight_rounds >= 2 and _memory_tight():
                    raise ScrapeOutOfMemory(f"Stopped at listing {done} of {total}: this server is at its memory limit "
                                        f"({int(_used_mb())} MB in use)")
            else:
                tight_rounds = 0
            _delay(ctx, *pause)
    finally:
        _close_all(tabs)


def scrape_google_maps(query, location="", max_results=30, progress=None, ctx=None,
                       on_row=None, skip_known=None, skip_site=None):
    """on_row(row) is called for each listing as soon as it's read (return False to stop).
    skip_known(url) / skip_site(card_website_url) return True for results that needn't be opened."""
    from playwright.sync_api import sync_playwright
    progress = progress or (lambda m: None)
    full_query = f"{query} {location}".strip() if location and location.lower() not in query.lower() else query
    results = []
    with sync_playwright() as p:
        browser, context = _new_page(p)
        try:
            page = context.new_page()
            page.goto(f"{MAPS_SEARCH_URL}{quote_plus(full_query)}", wait_until="domcontentloaded", timeout=60000)
            _blocked(page, ("unusual traffic", "recaptcha"), "Google")
            cards, seen = [], set()
            try:
                page.wait_for_selector(_MAPS_FEED, timeout=15000)
            except Exception:  # noqa: BLE001
                _blocked(page, ("unusual traffic", "recaptcha"), "Google")
                if "/maps/place/" in page.url:          # a search with exactly one match opens it directly
                    cards.append({"href": page.url, "website": ""})
                else:
                    progress("No results list appeared (no matches, or Google changed its layout).")
                    return results

            scroll_pause = _delay_range("SCROLL", 0.7, 1.3)
            stagnant, rounds, shown, grew_at = 0, 0, -1, time.monotonic()
            single = bool(cards)
            while not single and len(cards) < max_results:
                if _stopped(ctx) or rounds >= 2000:
                    break
                rounds += 1
                snap = page.evaluate(_MAPS_COLLECT_JS, _MAPS_FEED)
                if snap is None:
                    break
                before = len(cards)
                for it in snap["items"]:
                    href = it.get("href") or ""
                    key = listing_key(href)
                    if href and key not in seen:
                        seen.add(key)
                        cards.append(it)
                if len(cards) != shown:
                    shown = len(cards)
                    progress(f"Found {shown} listings so far...")
                if len(cards) >= max_results:
                    break
                if _memory_tight():
                    progress(f"Stopping the list at {len(cards)} listings: this server is close to its memory "
                             f"limit ({int(_used_mb())} MB in use). Carrying on with these -- run another, "
                             f"narrower search for more.")
                    break
                if snap["end"]:
                    progress(f"Google has no more results for this search ({len(cards)} in total). "
                             f"For more, run another search term or a neighbouring town.")
                    break
                if len(cards) != before:
                    stagnant, grew_at = 0, time.monotonic()
                else:
                    stagnant += 1
                # Give a slow list real time to load more (judged by the clock, so short delay
                # settings can't make the run give up early).
                if stagnant >= 5 and time.monotonic() - grew_at > _MAPS_STALL_SECONDS:
                    progress(f"The list stopped growing at {len(cards)} listings.")
                    break
                if stagnant >= 2:                       # a stalled list often resumes after a small scroll back
                    page.evaluate(_MAPS_NUDGE_JS, _MAPS_FEED)
                _delay(ctx, *scroll_pause)
            cards = cards[:max_results]

            todo, known, had_site = [], 0, 0
            for it in cards:
                if skip_known is not None and skip_known(it["href"]):
                    known += 1
                elif skip_site is not None and it.get("website") and skip_site(it["website"]):
                    had_site += 1
                else:
                    todo.append(it["href"])
            if known or had_site:
                progress(f"Of {len(cards)} listings: {known} already in your leads, {had_site} show a website "
                         f"on their card -- opening the other {len(todo)}.")
            before_close = _memory_note()
            _close_all([page])                          # the long results list is no longer needed; free it
            if before_close:
                progress(f"Server memory: {before_close} with the results list open, {_memory_note()} after closing it.")

            def finish(row, link):
                row["google_maps_url"] = link
                row["notes"] = f"scraped from Google Maps: {full_query}"

            _scrape_details(context, todo, extract_maps_detail, finish, results, progress, ctx, on_row,
                            _MAPS_READY_JS, "Google", ("unusual traffic", "recaptcha"))
        finally:
            browser.close()
    return results


def scrape_yelp(query, location, max_results=30, progress=None, ctx=None, on_row=None, skip_known=None):
    from playwright.sync_api import sync_playwright
    progress = progress or (lambda m: None)
    if not location:
        raise ValueError("Yelp needs a location, e.g. 'Dallas, TX'")
    results, links, start = [], [], 0
    with sync_playwright() as p:
        browser, context = _new_page(p)
        try:
            page = context.new_page()
            pause = _delay_range("SCROLL", 0.7, 1.3)
            while len(links) < max_results and start < 240 and not _stopped(ctx):
                page.goto(f"{YELP_BASE_URL}/search?find_desc={quote_plus(query)}"
                          f"&find_loc={quote_plus(location)}&start={start}",
                          wait_until="domcontentloaded", timeout=60000)
                _delay(ctx, *pause)
                _blocked(page, ("captcha", "are you a robot"), "Yelp")
                found = []
                for href in page.evaluate(_HREFS_JS, 'a[href*="/biz/"]'):
                    m = re.match(r"^(?:https?://[^/]+)?(/biz/[^/]+)$", href.split("?")[0])
                    if m:
                        url = YELP_BASE_URL + m.group(1)
                        if url not in links and url not in found:
                            found.append(url)
                if not found:
                    break
                links.extend(found)
                start += 10
                progress(f"Found {len(links)} Yelp listings so far...")
            links = links[:max_results]
            try:
                page.goto("about:blank")
            except Exception:  # noqa: BLE001
                pass

            todo = [u for u in links if not (skip_known is not None and skip_known(u))]
            if len(todo) != len(links):
                progress(f"Of {len(links)} listings: {len(links) - len(todo)} already in your leads -- "
                         f"opening the other {len(todo)}.")

            def finish(row, link):
                row["yelp_url"] = link
                row["notes"] = f"scraped from Yelp: {query} in {location}"

            # Yelp is quicker to challenge than Google, so its pages are opened one at a time.
            _scrape_details(context, todo, extract_yelp_detail, finish, results, progress, ctx, on_row,
                            _NAME_READY_JS, "Yelp", ("captcha", "are you a robot"), max_tabs=1)
        finally:
            browser.close()
    return results


def scrape_instagram(tag, ig_username, ig_password, max_results=30, progress=None, ctx=None, on_row=None):
    """Highest ban-risk scraper: use a BURNER account, keep max_results small (20-40).
    Deliberately NOT sped up -- Instagram's limits are the tightest of the three."""
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
            while len(posts) < max_results and stagnant < 5 and not _stopped(ctx):
                before = len(posts)
                for href in page.evaluate(_HREFS_JS, 'a[href*="/p/"]'):
                    if href:
                        full = f"https://www.instagram.com{href}" if href.startswith("/") else href
                        if full not in posts:
                            posts.append(full)
                stagnant = stagnant + 1 if len(posts) == before else 0
                page.mouse.wheel(0, 1200)
                _delay(ctx, 1.5, 3)
                progress(f"Found {len(posts)} posts so far...")

            usernames = []
            detail = context.new_page()
            try:
                for link in posts[:max_results]:
                    if _stopped(ctx):
                        break
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
                _close_all([detail])

            _report(ctx, 0, len(usernames))
            prof = context.new_page()
            try:
                for i, uname in enumerate(usernames, 1):
                    if _stopped(ctx):
                        break
                    try:
                        prof.goto(f"https://www.instagram.com/{uname}/", timeout=30000)
                        _delay(ctx, 2, 3)
                        row = extract_instagram_profile(prof, uname)
                        row["notes"] = f"scraped from Instagram #{tag}"
                    except Exception as e:  # noqa: BLE001
                        progress(f"Skipped @{uname} ({_short(e)})")
                        continue
                    results.append(row)
                    progress(f"Scraped profile {i}/{len(usernames)}: @{uname}")
                    _report(ctx, i, len(usernames))
                    if on_row is not None and on_row(row) is False:
                        break
                    _delay(ctx, 2, 4)
            finally:
                _close_all([prof])
        finally:
            browser.close()
    return results


# --------------------------------------------------------------- job entry

MAX_SEARCHES_PER_RUN = 40


def split_multi(value):
    """'AC repair; furnace repair' (or a list) -> ['AC repair', 'furnace repair'].
    Entries are separated by ';' or a new line -- never by a comma, because places
    are written 'Dallas, TX'. Blank and repeated entries are dropped."""
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[;\n]+", str(value or ""))
    out, seen = [], set()
    for p in parts:
        p = re.sub(r"\s+", " ", str(p or "")).strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def plan_searches(params):
    """Every (search term, place) pair a scrape request asks for, in the order they'll run:
    all the terms for the first place, then all the terms for the next place, and so on."""
    queries = split_multi(params.get("queries") or params.get("query"))
    locations = split_multi(params.get("locations") or params.get("location"))
    if params.get("platform") == "instagram":
        locations = []
    if not queries:
        raise ValueError("Enter a search query")
    pairs = [(q, loc) for loc in (locations or [""]) for q in queries]
    if len(pairs) > MAX_SEARCHES_PER_RUN:
        raise ValueError(f"That's {len(pairs)} searches ({len(queries)} terms x {len(locations)} places). "
                         f"The most one run can do is {MAX_SEARCHES_PER_RUN} -- split it into two runs.")
    return pairs


def run_scrape(ctx, params):
    """Background job: scrape one platform, saving each lead the moment it's found.

    Several search terms and/or several places (separated by ';') are run one after another,
    each as its own browser session -- so memory is handed back between searches, and a server
    too small for one 100-listing search can still work through many 30-40 listing ones."""
    platform = params["platform"]
    max_results = int(params.get("max_results") or 30)
    campaign = (params.get("campaign") or "").strip()
    only_no_website = bool(params.get("only_no_website"))
    searches = plan_searches(params)
    if platform not in ("google_maps", "yelp", "instagram"):
        raise ValueError(f"Unknown platform '{platform}'")
    if ctx.stopped:
        return "Stopped before starting"
    cur = {"query": searches[0][0]}

    st = {"checked": 0, "kept": 0, "inserted": 0, "merged": 0, "skipped_suppressed": 0,
          "known": 0, "limit": ""}
    handled = set()

    def has_real_site(url):
        return bool(schema.normalize_lead({"website": url})["has_website"])

    def save(row):
        """Filter + store ONE listing right now. Returns False when the run should stop."""
        handled.add(id(row))
        st["checked"] += 1
        if only_no_website and schema.normalize_lead(row)["has_website"]:
            # A listing that shows a real website is, by definition, not a "no website" prospect --
            # and it would be rejected at the fit step anyway, after spending enrichment, search
            # and AI budget on it. Drop it here instead.
            return True
        st["kept"] += 1
        s = importer.import_leads([row], campaign=campaign, source=platform, query=cur["query"])
        for k in ("inserted", "merged", "skipped_suppressed"):
            st[k] += s[k]
        if s.get("stopped_at_limit"):
            st["limit"] = s["stopped_at_limit"]
            return False
        return True

    def skip_site(card_website):
        # the results list already shows this business's website -- no need to open its page
        if only_no_website and has_real_site(card_website):
            st["checked"] += 1
            return True
        return False

    known_keys = []

    def skip_known(url):
        if not known_keys:               # read once, the first time a results list comes back
            known_keys.append({listing_key(u) for u in db.listing_urls()} - {""})
        if listing_key(url) in known_keys[0]:
            st["known"] += 1
            return True
        return False

    def one_search(query, location):
        rows = []
        if platform == "google_maps":
            rows = scrape_google_maps(query, location, max_results, ctx.log, ctx,
                                      on_row=save, skip_known=skip_known, skip_site=skip_site)
        elif platform == "yelp":
            rows = scrape_yelp(query, location, max_results, ctx.log, ctx, on_row=save, skip_known=skip_known)
        else:
            user, pw = config.env("IG_USERNAME"), config.env("IG_PASSWORD")
            if not user or not pw:
                raise RuntimeError("Set IG_USERNAME and IG_PASSWORD (a burner account) in .env first")
            rows = scrape_instagram(query, user, pw, max_results, ctx.log, ctx, on_row=save)
        for r in rows or []:             # anything a scraper returned without reporting as it went
            if id(r) not in handled and not st["limit"]:
                save(r)

    many, failed, ran = len(searches) > 1, [], 0
    if many:
        ctx.log(f"{len(searches)} searches to run, up to {max_results} listings each.")
    for n, (query, location) in enumerate(searches, 1):
        if ctx.stopped or st["limit"]:
            break
        cur["query"] = query
        label = f"'{query}' {location}".strip()
        ctx.log((f"Search {n} of {len(searches)}: " if many else f"Starting {platform} scrape for ") + label)
        before = st["inserted"]
        ran += 1
        try:
            one_search(query, location)
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
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            # A CAPTCHA will meet the next search too, so it ends the run. Anything else (one search
            # running out of memory, a page that wouldn't load) only costs that one search: the next
            # starts with a fresh browser.
            last = n == len(searches)
            if many and not isinstance(e, ScrapeBlocked) and not last:
                failed.append(label)
                ctx.log(f"  This search ended early: {_short(e, 240)}. Moving on to the next one.")
            elif many and not isinstance(e, ScrapeBlocked):
                failed.append(label)
                ctx.log(f"  This search ended early: {_short(e, 240)}.")
            elif st["checked"] or st["known"]:
                # Nothing found so far is lost: say so, and say how to carry on.
                raise RuntimeError(f"{_short(e, 300)} -- stopped after checking {st['checked']} listings; "
                                   f"{st['inserted']} new leads were already saved. Run the same search "
                                   f"again to carry on from here.") from e
            else:
                raise
        if many:
            ctx.log(f"  -> {st['inserted'] - before} new leads from this search ({st['inserted']} so far).")
            if n < len(searches) and not ctx.stopped:
                _delay(ctx, 2, 4)
    if many and failed and len(failed) == ran:
        raise RuntimeError(f"All {ran} searches ended early -- see the log. {st['inserted']} new leads were saved.")

    checked, kept = st["checked"], st["kept"]
    if only_no_website:
        ctx.log(f"Checked {checked} listings: kept {kept} with no website, "
                f"skipped {checked - kept} that already have one")
    tail = (f"{st['inserted']} new, {st['merged']} already known"
            + (f", {st['skipped_suppressed']} on do-not-contact" if st["skipped_suppressed"] else "")
            + (f", {st['known']} not re-opened (already in your leads)" if st["known"] else ""))
    ctx.log(f"{kept} scraped -> {tail}")
    msg = (f"{checked} checked, {kept} had no website -> {tail}" if only_no_website
           else f"{kept} scraped -> {tail}")
    if many:
        msg = f"{ran} of {len(searches)} searches run: " + msg
        if failed:
            msg += f" -- {len(failed)} ended early ({'; '.join(failed[:3])}{'...' if len(failed) > 3 else ''})"
    if st["limit"]:
        msg += f" -- stopped early: {st['limit']}"
    if not checked and not st["known"]:
        msg += " (0 results usually means the site's markup changed or a CAPTCHA appeared)"
    elif not kept and only_no_website and checked:
        msg += (" (every business checked already has a website -- try a more specific search, "
                "a suburb name, or a higher 'listings to check' number)")
    return msg
