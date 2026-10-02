"""
End-to-end checks of the scraper's FLOW against a stand-in for Google Maps (tests/fake_maps.py):
a results list that lazy-loads and keeps re-rendering its cards, and place pages that draw their
details late. This proves the saving / filtering / resuming / stopping logic with a real browser.
It does NOT prove that Google's live markup still matches the selectors. Skipped automatically
if Chromium isn't available.

Also covers the search-suggestion endpoint, which needs no browser.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase  # noqa: E402  (sets up the isolated environment)

import db  # noqa: E402
import jobs  # noqa: E402
import scraper  # noqa: E402
import fake_maps  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch().close()
    HAVE_BROWSER = True
except Exception:  # noqa: BLE001
    HAVE_BROWSER = False

FAST = {"SCRAPER_SCROLL_DELAY_MIN": "0.05", "SCRAPER_SCROLL_DELAY_MAX": "0.1",
        "SCRAPER_DETAIL_DELAY_MIN": "0", "SCRAPER_DETAIL_DELAY_MAX": "0.05"}


class Ctx:
    """Like jobs.JobContext, with an optional 'stop after N listings were read'."""

    def __init__(self, stop_after=None):
        self.lines, self.last, self.stop_after, self.stopped = [], None, stop_after, False

    def log(self, m):
        self.lines.append(m)
        if self.stop_after and sum(1 for x in self.lines if x.startswith("Scraped ")) >= self.stop_after:
            self.stopped = True

    def progress(self, done, total=None):
        self.last = (done, total)

    def sleep(self, s):
        return self.stopped


@unittest.skipUnless(HAVE_BROWSER, "Chromium not available")
class TestScrapeFlow(Base):
    def setUp(self):
        super().setUp()
        self._fast = mock.patch.dict(os.environ, FAST)
        self._fast.start()
        self._ready = mock.patch.object(scraper, "_READY_SECONDS", (3, 1))
        self._ready.start()
        self.addCleanup(self._ready.stop)

    def tearDown(self):
        self.srv.shutdown()
        self._fast.stop()
        super().tearDown()

    def serve(self, n, broken=()):
        self.srv = fake_maps.start(0, n=n, broken=broken)
        self._url = mock.patch.object(scraper, "MAPS_SEARCH_URL",
                                      f"http://127.0.0.1:{self.srv.server_address[1]}/maps/search/")
        self._url.start()
        self.addCleanup(self._url.stop)

    def run_it(self, max_results, only_no_website=True, ctx=None):
        ctx = ctx or Ctx()
        msg = scraper.run_scrape(ctx, {"platform": "google_maps", "query": "hvac", "location": "Garland, TX",
                                       "max_results": max_results, "campaign": "c",
                                       "only_no_website": only_no_website})
        return msg, ctx

    def opened(self):
        return sum(1 for h in fake_maps.hits if "/maps/place/" in h)

    def leads(self):
        return db.list_leads({}, limit=1000)[0]

    def test_a_large_run_over_a_constantly_re_rendering_list_completes(self):
        # 60 results, loaded 7 at a time, every card rebuilt ~8x a second -- the conditions that
        # used to end a big run with "ElementHandle.get_attribute: Timeout 30000ms exceeded".
        self.serve(80)
        msg, ctx = self.run_it(60)
        self.assertIn("60 checked, 30 had no website -> 30 new", msg)
        leads = self.leads()
        self.assertEqual(len(leads), 30)
        self.assertEqual(sum(l["has_website"] for l in leads), 0)          # Facebook-only still counts as "no website"
        self.assertTrue(any(l["facebook_url"] for l in leads))
        self.assertTrue(all(l["phone"] and l["city"] == "Garland" for l in leads))
        # 15 of the 60 show a Website button on their card and were never opened
        self.assertEqual(self.opened(), 45)
        self.assertEqual(ctx.last, (45, 45))

    def test_the_list_running_out_ends_the_run_cleanly(self):
        self.serve(12)
        msg, ctx = self.run_it(100, only_no_website=False)
        self.assertIn("12 scraped -> 12 new", msg)
        self.assertTrue(any("no more results" in l for l in ctx.lines))

    def test_an_interrupted_run_keeps_what_it_found_and_the_next_run_carries_on(self):
        # listings 9..16 never load -> six failures in a row -> the run gives up...
        self.serve(24, broken=range(9, 17))
        with self.assertRaises(RuntimeError) as cm:
            self.run_it(24, only_no_website=False)
        self.assertIn("already saved", str(cm.exception))
        saved = len(self.leads())
        self.assertGreaterEqual(saved, 8)                                  # ...but everything before it is in the database
        # the site recovers; the same search opens only what isn't saved yet
        fake_maps.BROKEN.clear()
        fake_maps.hits.clear()
        msg, _ = self.run_it(24, only_no_website=False)
        self.assertEqual(self.opened(), 24 - saved)
        self.assertEqual(len(self.leads()), 24)
        self.assertIn(f"{saved} not re-opened (already in your leads)", msg)

    def test_one_bad_listing_is_skipped_not_fatal(self):
        self.serve(10, broken=[4])
        msg, ctx = self.run_it(10, only_no_website=False)
        self.assertIn("9 scraped -> 9 new", msg)
        self.assertTrue(any(l.startswith("Skipped listing") for l in ctx.lines))

    def test_stop_keeps_the_leads_read_so_far(self):
        self.serve(30)
        msg, ctx = self.run_it(30, only_no_website=False, ctx=Ctx(stop_after=6))
        n = len(self.leads())
        self.assertGreaterEqual(n, 6)
        self.assertLess(n, 30)

    def test_the_per_account_lead_limit_stops_the_run_instead_of_crashing_it(self):
        self.serve(20)
        with mock.patch.dict(os.environ, {"MAX_LEADS_PER_USER": "5"}):
            msg, _ = self.run_it(20, only_no_website=False)
        self.assertEqual(len(self.leads()), 5)
        self.assertIn("stopped early", msg)
        self.assertLess(self.opened(), 20)


class TestListingKey(unittest.TestCase):
    def test_same_place_with_different_tracking_is_one_key(self):
        a = "https://www.google.com/maps/place/Acme/data=!4m7!3m6!1s0x864c19f7:0xb9ec9ba4!8m2!3d32.7?authuser=0&hl=en&rclk=1"
        b = "https://www.google.com/maps/place/Acme+HVAC/data=!4m7!3m6!1s0x864c19f7:0xB9EC9BA4!8m2!3d32.7"
        self.assertEqual(scraper.listing_key(a), scraper.listing_key(b))

    def test_two_branches_with_the_same_name_stay_separate(self):
        a = "https://www.google.com/maps/place/Acme/data=!1s0x1:0x2"
        b = "https://www.google.com/maps/place/Acme/data=!1s0x1:0x3"
        self.assertNotEqual(scraper.listing_key(a), scraper.listing_key(b))

    def test_yelp_and_blank(self):
        self.assertEqual(scraper.listing_key("https://www.yelp.com/biz/acme-dallas?osq=x"), "yelp.com/biz/acme-dallas")
        self.assertEqual(scraper.listing_key(""), "")

    def test_delay_settings_fall_back_safely(self):
        with mock.patch.dict(os.environ, {"SCRAPER_SCROLL_DELAY_MIN": "abc", "SCRAPER_SCROLL_DELAY_MAX": "0.2",
                                          "SCRAPER_DETAIL_DELAY_MIN": "3", "SCRAPER_DETAIL_DELAY_MAX": "1"}):
            self.assertEqual(scraper._delay_range("SCROLL", 0.7, 1.3), (0.7, 0.7))   # bad min -> default; max never below min
            self.assertEqual(scraper._delay_range("DETAIL", 0.4, 0.9), (3.0, 3.0))


class TestSuggest(ApiBase):
    def setUp(self):
        super().setUp()
        db.create_job("scrape", {"platform": "google_maps", "query": "AC repair", "location": "Garland, TX"})
        db.create_job("scrape", {"platform": "google_maps", "query": "furnace repair", "location": "Mesquite, TX"})
        db.create_job("enrich", {"query": "repair ignored - not a scrape"})
        db.create_schedule("google_maps", "heating and air conditioning", "Grand Prairie, TX", 40, "", 24)
        self.add(business_name="Garcia Heating & Air", address="1 Main St, Garland, TX 75040", phone="(214) 555-0101")
        self.add(business_name="Garland Comfort Pros", address="2 Main St, Irving, TX 75060", phone="(214) 555-0102")

    def ask(self, field, q):
        r = self.client.get("/api/suggest", query_string={"field": field, "q": q})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["suggestions"]

    def test_past_searches(self):
        self.assertEqual(self.ask("scrape_query", "rep"), ["furnace repair", "AC repair"])
        self.assertEqual(self.ask("scrape_query", "heat"), ["heating and air conditioning"])

    def test_past_locations_and_lead_cities(self):
        got = self.ask("scrape_location", "gar")
        self.assertEqual(got[0], "Garland, TX")
        self.assertEqual(len(got), len({g.lower() for g in got}))            # no duplicates
        self.assertIn("Irving, TX", self.ask("scrape_location", "irv"))      # from a lead's city
        self.assertEqual(self.ask("scrape_location", "prairie"), ["Grand Prairie, TX"])

    def test_lead_search_matches_names_and_cities(self):
        got = self.ask("lead_search", "gar")
        self.assertIn("Garcia Heating & Air", got)
        self.assertIn("Garland Comfort Pros", got)
        self.assertIn("Garland", got)

    def test_needs_three_characters_and_treats_wildcards_literally(self):
        self.assertEqual(self.ask("scrape_query", "re"), [])
        self.assertEqual(self.ask("lead_search", "%%%"), [])
        self.assertEqual(self.ask("lead_search", "___"), [])

    def test_unknown_field_is_rejected(self):
        self.assertEqual(self.client.get("/api/suggest?field=password&q=abc").status_code, 400)

    def test_requires_sign_in_and_never_crosses_accounts(self):
        self.assertIn(self.appmod.test_client().get("/api/suggest?field=scrape_query&q=repair").status_code, (401, 302))
        other = self.signup("second@example.com")
        r = other.get("/api/suggest", query_string={"field": "lead_search", "q": "gar"})
        self.assertEqual(r.get_json()["suggestions"], [])


if __name__ == "__main__":
    unittest.main()
