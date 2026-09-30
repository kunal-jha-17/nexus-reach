"""
Checks that the scrapers' extraction code runs and reads the *assumed* markup
correctly, using small fixture pages. This does NOT prove the live Google Maps /
Yelp / Instagram pages look like the fixtures -- see the note at the top of
scraper.py. Skipped automatically if Playwright/Chromium isn't available.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import schema  # noqa: E402
import scraper  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch().close()
    HAVE_BROWSER = True
except Exception:  # noqa: BLE001
    HAVE_BROWSER = False

MAPS_HTML = """
<h1>Cowboy Services AC</h1>
<button data-item-id="address" aria-label="Address: 5 Elm St, Dallas, TX 75201"></button>
<button data-item-id="phone:tel:2145550101" aria-label="Phone: (214) 555-0101"></button>
<button jsaction="pane.rating.category">Air conditioning contractor</button>
<span aria-label="1,234 reviews"></span>
"""
YELP_HTML = """
<h1>Parsons Pest Control</h1>
<a href="/biz_redir?url=http%3A%2F%2Fwww.parsonspest.com&src_bizid=1">parsonspest.com</a>
<a href="tel:+12145550102">call</a><address>9 Oak Ave, Dallas, TX 75202</address>
"""
IG_HTML = """
<header><h2>acme</h2><span>Plumbing in Dallas. Email hello@acmeplumb.com  (214) 555-0103</span>
<a href="https://l.instagram.com/?u=https%3A%2F%2Facmeplumb.com%2F&e=x">acmeplumb.com</a></header>
"""


@unittest.skipUnless(HAVE_BROWSER, "Chromium not available")
class TestExtractors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def page(self, html):
        p = self.browser.new_page()
        p.set_content(html)
        return p

    def test_maps(self):
        row = scraper.extract_maps_detail(self.page(MAPS_HTML))
        n = schema.normalize_lead(row)
        self.assertEqual(n["business_name"], "Cowboy Services AC")
        self.assertEqual(n["phone"], "+12145550101")
        self.assertEqual((n["city"], n["state"], n["trade"]), ("Dallas", "TX", "hvac"))
        self.assertEqual(n["review_count"], 1234)

    def test_yelp_redirect_decoded(self):
        n = schema.normalize_lead(scraper.extract_yelp_detail(self.page(YELP_HTML)))
        self.assertEqual(n["website"], "http://www.parsonspest.com")
        self.assertEqual(n["has_website"], 1)
        self.assertEqual(n["phone"], "+12145550102")

    def test_instagram_profile(self):
        n = schema.normalize_lead(scraper.extract_instagram_profile(self.page(IG_HTML), "acme"))
        self.assertEqual(n["email"], "hello@acmeplumb.com")
        self.assertEqual(n["website"], "https://acmeplumb.com")
        self.assertEqual(n["instagram_url"], "https://www.instagram.com/acme/")


class TestBrowserAvailability(unittest.TestCase):
    def test_available_when_the_binary_actually_exists(self):
        self.assertEqual(scraper.browser_available(), HAVE_BROWSER)

    def test_reports_false_without_crashing_when_playwright_isnt_importable(self):
        import builtins
        real_import = builtins.__import__
        def blocked(name, *a, **k):
            if name.startswith("playwright"):
                raise ImportError("no playwright here")
            return real_import(name, *a, **k)
        with mock.patch("builtins.__import__", side_effect=blocked):
            self.assertFalse(scraper.browser_available())

    def test_reports_false_when_the_package_exists_but_the_binary_doesnt(self):
        # the exact situation a plain `pip install -r requirements.txt` (no `playwright install`)
        # leaves a server in -- this is what Render hits without the Dockerfile
        class FakeChromium:
            executable_path = "/nonexistent/chrome"
        class FakeBrowserType:
            chromium = FakeChromium()
        class FakePW:
            def __enter__(self): return FakeBrowserType()
            def __exit__(self, *a): return False
        with mock.patch("playwright.sync_api.sync_playwright", return_value=FakePW()):
            self.assertFalse(scraper.browser_available())

    def test_run_scrape_turns_a_missing_browser_into_a_friendly_runtime_error(self):
        with mock.patch.object(scraper, "scrape_google_maps", side_effect=scraper.BrowserNotInstalled()):
            ctx = mock.Mock(stopped=False)
            with self.assertRaises(RuntimeError) as cm:
                scraper.run_scrape(ctx, {"platform": "google_maps", "query": "plumbers", "max_results": 5})
        self.assertIn("isn't installed on this server", str(cm.exception))
        self.assertIn("Dockerfile", str(cm.exception))


class TestNoWebsiteFilter(unittest.TestCase):
    """The scrape option that stops established businesses (which the fit step would reject anyway)
    from being saved and enriched."""

    ROWS = [
        {"business_name": "Has Site HVAC", "phone": "(214) 555-0101", "website": "https://hassite.com", "category_raw": "HVAC contractor"},
        {"business_name": "No Site HVAC", "phone": "(214) 555-0102", "website": "", "category_raw": "HVAC contractor"},
        {"business_name": "Facebook Only HVAC", "phone": "(214) 555-0103", "website": "https://www.facebook.com/fbonly", "category_raw": "HVAC contractor"},
        {"business_name": "Linktree HVAC", "phone": "(214) 555-0104", "website": "https://linktr.ee/lt", "category_raw": "HVAC contractor"},
    ]

    def scrape_with(self, only_no_website):
        import tempfile, importlib
        tmp = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"DATA_DIR": tmp, "SYNC_FOLDER": "", "S3_BUCKET": ""}):
            import db, accounts, userctx
            accounts._init_done.clear()
            u = accounts.create_user("a@example.com", "horse-battery-1")
            with userctx.use(u, accounts.db_path_for(u["id"])):
                logs = []
                ctx = mock.Mock(stopped=False, log=logs.append)
                with mock.patch.object(scraper, "scrape_google_maps", return_value=[dict(r) for r in self.ROWS]):
                    msg = scraper.run_scrape(ctx, {"platform": "google_maps", "query": "hvac", "location": "Dallas, TX",
                                                    "max_results": 10, "campaign": "c", "only_no_website": only_no_website})
                names = sorted(l["business_name"] for l in db.list_leads({})[0])
        return names, msg, logs

    def test_filter_on_keeps_only_businesses_without_a_real_website(self):
        names, msg, logs = self.scrape_with(True)
        # a Facebook page or a link-in-bio page is not a website, so those businesses ARE prospects
        self.assertEqual(names, ["Facebook Only HVAC", "Linktree HVAC", "No Site HVAC"])
        self.assertIn("4 checked, 3 had no website", msg)
        self.assertTrue(any("skipped 1 that already have one" in l for l in logs))

    def test_filter_off_keeps_everything(self):
        names, msg, _ = self.scrape_with(False)
        self.assertEqual(len(names), 4)
        self.assertNotIn("checked", msg)

    def test_a_batch_where_everyone_has_a_website_says_what_to_do(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"DATA_DIR": tmp, "SYNC_FOLDER": "", "S3_BUCKET": ""}):
            import accounts, userctx
            accounts._init_done.clear()
            u = accounts.create_user("a@example.com", "horse-battery-1")
            with userctx.use(u, accounts.db_path_for(u["id"])):
                rows = [dict(self.ROWS[0]), {**self.ROWS[0], "business_name": "Also Has Site", "phone": "(214) 555-0199"}]
                with mock.patch.object(scraper, "scrape_google_maps", return_value=rows):
                    msg = scraper.run_scrape(mock.Mock(stopped=False), {
                        "platform": "google_maps", "query": "hvac", "max_results": 5, "only_no_website": True})
        self.assertIn("already has a website", msg)


if __name__ == "__main__":
    unittest.main()
