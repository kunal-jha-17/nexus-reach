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

    def seen_cards(self):
        """How many result cards the run has logged finding so far."""
        found = [l for l in self.ctx.lines if l.startswith("Found ")]
        return int(found[-1].split()[1]) if found else 0

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

    def test_a_server_near_its_memory_limit_gets_a_shorter_list_not_a_crash(self):
        # memory "fills up" as the list grows past ~20 cards, then is fine again once the list is closed
        self.serve(60)
        self.ctx = Ctx()
        state = {"list_open": True}
        real_close = scraper._close_all

        def close(pages):
            state["list_open"] = False
            real_close(pages)

        def used():
            return 470 if state["list_open"] and self.seen_cards() >= 20 else 300

        with mock.patch.object(scraper, "_memory_limit_mb", return_value=512), \
                mock.patch.object(scraper, "_used_mb", side_effect=used), \
                mock.patch.object(scraper, "_close_all", side_effect=close):
            msg, ctx = self.run_it(60, only_no_website=False, ctx=self.ctx)
        n = len(self.leads())
        self.assertGreaterEqual(n, 20)
        self.assertLess(n, 60)                                             # the list was cut short...
        self.assertIn(f"{n} scraped -> {n} new", msg)                      # ...and the run still finished normally
        self.assertTrue(any("close to its memory limit" in l for l in ctx.lines))
        self.assertTrue(any(l.startswith("Server memory:") for l in ctx.lines))

    def test_memory_that_stays_full_ends_the_run_cleanly_with_leads_saved(self):
        self.serve(20)
        calls = {"n": 0}

        def used():                                                        # fine during the list, full from then on
            calls["n"] += 1
            return 300 if not any(l.startswith("Scraped 4/") for l in self.ctx.lines) else 500

        self.ctx = Ctx()
        with mock.patch.object(scraper, "_memory_limit_mb", return_value=512), \
                mock.patch.object(scraper, "_used_mb", side_effect=used), \
                mock.patch.object(scraper.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                self.run_it(20, only_no_website=False, ctx=self.ctx)
        self.assertIn("memory limit", str(cm.exception))
        self.assertIn("already saved", str(cm.exception))
        self.assertGreaterEqual(len(self.leads()), 4)

    def test_the_guard_is_off_without_a_limit_and_measures_real_memory(self):
        with mock.patch.dict(os.environ, {"SCRAPER_MEMORY_LIMIT_MB": "0"}):
            self.serve(1)
            self.assertFalse(scraper._memory_tight())
        with mock.patch.dict(os.environ, {"SCRAPER_MEMORY_LIMIT_MB": "1"}):
            self.assertTrue(scraper._memory_tight())                       # this test process alone is over 1 MB
        self.assertGreater(scraper._tree_mb(), 10)
        self.assertGreaterEqual(scraper._used_mb(), scraper._tree_mb())
        self.assertEqual(scraper._container_mb(), 0)          # this machine reports no limit of its own

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


class TestManySearchesInOneRun(Base):
    """Several terms x several places, with the browser part replaced by a stand-in."""

    def run_many(self, fake, **params):
        ctx = Ctx()
        p = {"platform": "google_maps", "query": "AC repair; furnace repair", "location": "Garland, TX; Mesquite, TX",
             "max_results": 40, "campaign": "c", "only_no_website": False}
        p.update(params)
        with mock.patch.object(scraper, "scrape_google_maps", side_effect=fake) as m, \
                mock.patch.object(scraper, "_delay", return_value=False):
            try:
                return scraper.run_scrape(ctx, p), ctx, m
            except Exception as e:  # noqa: BLE001
                return e, ctx, m

    @staticmethod
    def rows(query, location, n=2):
        return [{"business_name": f"{query} {location} {i}", "phone": f"(214) 555-{abs(hash((query, location, i))) % 9000 + 1000}",
                 "address": f"{i} Main St, {location}"} for i in range(n)]

    def test_every_term_is_searched_in_every_place(self):
        def fake(query, location, *a, **k):
            return self.rows(query, location)
        msg, ctx, m = self.run_many(fake)
        self.assertEqual([c.args[:2] for c in m.call_args_list],
                         [("AC repair", "Garland, TX"), ("furnace repair", "Garland, TX"),
                          ("AC repair", "Mesquite, TX"), ("furnace repair", "Mesquite, TX")])
        self.assertIn("4 of 4 searches run: 8 scraped -> 8 new", msg)
        leads = db.list_leads({}, limit=50)[0]
        self.assertEqual(len(leads), 8)
        self.assertEqual({l["last_query"] for l in leads}, {"AC repair", "furnace repair"})   # each lead knows its own search
        self.assertTrue(any(l.startswith("Search 3 of 4: 'AC repair' Mesquite, TX") for l in ctx.lines))

    def test_one_search_failing_does_not_stop_the_others(self):
        def fake(query, location, *a, **k):
            if (query, location) == ("furnace repair", "Garland, TX"):
                raise scraper.ScrapeOutOfMemory("Stopped at listing 3 of 30: this server is at its memory limit")
            return self.rows(query, location)
        msg, ctx, m = self.run_many(fake)
        self.assertEqual(m.call_count, 4)
        self.assertIn("4 of 4 searches run: 6 scraped -> 6 new", msg)
        self.assertIn("1 ended early ('furnace repair' Garland, TX)", msg)

    def test_a_captcha_stops_the_whole_run_but_keeps_what_was_saved(self):
        def fake(query, location, *a, **k):
            if location == "Mesquite, TX":
                raise scraper.ScrapeBlocked("Google is showing a CAPTCHA")
            for r in self.rows(query, location):
                k["on_row"](r)
            return []
        err, ctx, m = self.run_many(fake)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("4 new leads were already saved", str(err))
        self.assertEqual(m.call_count, 3)                                  # the 4th search was not attempted

    def test_lists_and_strings_split_the_same_way_and_never_on_commas(self):
        self.assertEqual(scraper.split_multi(" Dallas, TX ;Garland, TX;\n dallas, tx ;; "), ["Dallas, TX", "Garland, TX"])
        self.assertEqual(scraper.split_multi(["a", "b ", "A"]), ["a", "b"])
        self.assertEqual(scraper.plan_searches({"platform": "google_maps", "query": "plumbers", "location": "Dallas, TX"}),
                         [("plumbers", "Dallas, TX")])
        self.assertEqual(scraper.plan_searches({"platform": "instagram", "query": "a;b", "location": "x;y"}), [("a", ""), ("b", "")])

    def test_too_many_searches_is_refused_up_front(self):
        with self.assertRaises(ValueError) as cm:
            scraper.plan_searches({"platform": "google_maps", "query": ";".join(f"t{i}" for i in range(7)),
                                   "location": ";".join(f"p{i}" for i in range(6))})
        self.assertIn("42 searches", str(cm.exception))


class TestScrapeRoute(ApiBase):
    def test_the_route_accepts_several_terms_and_places(self):
        with mock.patch.object(jobs, "start_job", return_value=7) as start, \
                mock.patch.object(scraper, "browser_available", return_value=True):
            r = self.client.post("/api/scrape", json={"platform": "google_maps", "query": "AC repair;furnace repair ; ",
                                                      "location": "Garland, TX; Mesquite, TX", "max_results": 40})
        self.assertEqual(r.status_code, 200, r.get_json())
        params = next(a for a in list(start.call_args[0]) + list(start.call_args[1].values()) if isinstance(a, dict))
        self.assertEqual((params["query"], params["location"]), ("AC repair; furnace repair", "Garland, TX; Mesquite, TX"))

    def test_the_route_refuses_an_oversized_or_empty_request(self):
        r = self.client.post("/api/scrape", json={"platform": "google_maps", "query": ";".join(f"t{i}" for i in range(41))})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/api/scrape", json={"platform": "google_maps", "query": " ; "}).status_code, 400)
        self.assertEqual(self.client.post("/api/scrape", json={"platform": "yelp", "query": "plumbers"}).status_code, 400)


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
        self.assertEqual(self.ask("scrape_query", "rep")[:2], ["furnace repair", "AC repair"])   # own history first
        self.assertEqual(self.ask("scrape_query", "heat")[0], "heating and air conditioning")

    def test_past_locations_and_lead_cities(self):
        got = self.ask("scrape_location", "gar")
        self.assertEqual(got[0], "Garland, TX")
        self.assertEqual(len(got), len({g.lower() for g in got}))            # no duplicates
        self.assertIn("Irving, TX", self.ask("scrape_location", "irv"))      # from a lead's city
        self.assertEqual(self.ask("scrape_location", "prairie")[0], "Grand Prairie, TX")

    def test_lead_search_matches_names_and_cities(self):
        got = self.ask("lead_search", "gar")
        self.assertIn("Garcia Heating & Air", got)
        self.assertIn("Garland Comfort Pros", got)
        self.assertIn("Garland", got)

    def test_the_built_in_directory_fills_in_what_history_doesnt_have(self):
        self.assertIn("Dentist", self.ask("scrape_query", "dent"))                  # never searched before
        self.assertIn("Plumber", self.ask("scrape_query", "plum"))
        self.assertEqual(self.ask("scrape_location", "mesq")[0], "Mesquite, TX")
        self.assertEqual(self.ask("scrape_location", "fort wor")[0], "Fort Worth, TX")
        self.assertEqual(self.ask("scrape_location", "garland t"), ["Garland, TX"])   # typed without the comma
        self.assertIn("São Paulo, Brazil", self.ask("scrape_location", "sao pau"))    # accents not needed
        got = self.ask("scrape_location", "spring")
        self.assertEqual(len(got), 8)
        self.assertTrue(got[0].endswith(", TX"), got)        # this account works in Texas, so Texas places lead
        self.assertEqual(self.ask("lead_search", "dent"), [])                        # the leads box stays history-only

    def test_a_new_account_with_no_history_still_gets_suggestions(self):
        other = self.signup("fresh@example.com")
        r = other.get("/api/suggest", query_string={"field": "scrape_location", "q": "bhop"})
        self.assertEqual(r.get_json()["suggestions"][0], "Bhopal, India")
        r = other.get("/api/suggest", query_string={"field": "scrape_query", "q": "hvac"})
        self.assertEqual(r.get_json()["suggestions"], ["HVAC contractor"])

    def test_places_like_the_ones_already_typed_come_first(self):
        other = self.signup("fresh2@example.com")
        ask = lambda ctx: other.get("/api/suggest", query_string={"field": "scrape_location", "q": "irvi", "context": ctx}).get_json()["suggestions"][0]
        self.assertEqual(ask("irvi"), "Irvine, CA")                               # biggest, with nothing else to go on
        self.assertEqual(ask("Garland, TX; Mesquite, TX; irvi"), "Irving, TX")

    def test_history_holding_several_terms_is_suggested_one_term_at_a_time(self):
        db.create_job("scrape", {"platform": "google_maps", "query": "boiler servicing; chimney lining",
                                 "location": "Rowlett, TX; Sachse, TX"})
        self.assertEqual(self.ask("scrape_query", "chimney l"), ["chimney lining"])
        self.assertEqual(self.ask("scrape_location", "sach")[0], "Sachse, TX")

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
