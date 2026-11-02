"""
A Google Maps scrape of Indian businesses, end to end in a real browser against the stand-in
Maps (tests/fake_maps.py) serving Indian listings: numbers read as +91 even on an account set to
another country, landlines vs mobiles, JustDial / WhatsApp links not counted as websites, and
the address giving city, state and country.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base  # noqa: E402

import db  # noqa: E402
import scraper  # noqa: E402
import fake_maps  # noqa: E402
from test_scraper_flow import HAVE_BROWSER, FAST, Ctx  # noqa: E402


def indian_biz(i):
    kind = i % 5
    phone = {0: f"098765 {10000 + i}", 1: f"080 2345 {6000 + i}", 2: f"+91 99001 {10000 + i}",
             3: f"(044) 2345 {7000 + i}", 4: f"70123 {40000 + i}"}[kind]
    site = {0: "", 1: f"https://www.justdial.com/Bangalore/Clinic-{i}", 2: f"https://wa.me/9199001{10000 + i}",
            3: f"https://clinic{i}.in", 4: f"https://dentalclinic{i}.business.site"}[kind]
    return {"i": i, "name": f"Biz {i} Dental Clinic", "site": site, "card_site": site if kind in (1, 3) else "",
            "phone": phone, "addr": f"No. {i}, 5th Cross, Indiranagar, Bengaluru, Karnataka 5600{38 + kind}, India"}


@unittest.skipUnless(HAVE_BROWSER, "Chromium not available")
class TestIndianMapsScrape(Base):
    def setUp(self):
        super().setUp()
        self._p = [mock.patch.dict(os.environ, dict(FAST, DEFAULT_COUNTRY_CODE="971")),   # an account set to the UAE
                   mock.patch.object(fake_maps, "biz", indian_biz)]
        for p in self._p:
            p.start()
        self.srv = fake_maps.start(0, n=30)
        self._url = mock.patch.object(scraper, "MAPS_SEARCH_URL", f"http://127.0.0.1:{self.srv.server_address[1]}/maps/search/")
        self._url.start()
        self._ready = mock.patch.object(scraper, "browser_available", return_value=True)
        self._ready.start()

    def tearDown(self):
        self.srv.shutdown()
        for p in [self._url, self._ready] + self._p:
            p.stop()
        super().tearDown()

    def scrape(self, location, only_no_website=False, campaign="blr"):
        ctx = Ctx()
        msg = scraper.run_scrape(ctx, {"platform": "google_maps", "query": "dentist", "location": location,
                                       "max_results": 20, "campaign": campaign, "only_no_website": only_no_website})
        return msg, {l["business_name"]: l for l in db.list_leads({}, limit=100)[0]}

    def test_indian_listings_are_read_as_indian(self):
        msg, leads = self.scrape("Bengaluru, Karnataka")
        self.assertIn("20 scraped -> 20 new", msg)
        self.assertEqual(len(leads), 20)
        self.assertTrue(all(l["phone"].startswith("+91") for l in leads.values()), [l["phone"] for l in leads.values()])
        self.assertTrue(all((l["city"], l["state"], l["country"]) == ("Bengaluru", "Karnataka", "IN") for l in leads.values()))
        b0, b1, b2, b3, b4 = (leads[f"Biz {i} Dental Clinic"] for i in range(5))
        self.assertEqual((b0["phone"], b0["phone_kind"], b0["channel"]), ("+919876510000", "mobile", "whatsapp"))
        self.assertEqual((b1["phone"], b1["phone_kind"], b1["channel"]), ("+918023456001", "landline", "phone"))
        self.assertEqual((b3["phone_kind"], b3["channel"]), ("landline", "phone"))
        self.assertEqual(b4["phone_kind"], "mobile")
        # JustDial page, WhatsApp link and a free Google "business.site" page aren't the clinic's own website
        self.assertEqual([b["has_website"] for b in (b0, b1, b2, b3, b4)], [0, 0, 0, 1, 0])
        self.assertTrue(all("hl=en" in h for h in fake_maps.hits if h.startswith("/maps/search/")))

    def test_no_website_filter_keeps_justdial_only_clinics(self):
        msg, leads = self.scrape("Bangalore", only_no_website=True)
        self.assertEqual(len(leads), 16)                     # the 4 with their own site are dropped
        self.assertFalse(any(l["has_website"] for l in leads.values()))
        self.assertTrue(all(l["phone"].startswith("+91") for l in leads.values()))

    def test_a_campaigns_own_country_code_still_wins(self):
        db.create_campaign("blr")
        db.save_campaign("blr", {"country_code": "971"})
        _, leads = self.scrape("Bengaluru, Karnataka")
        self.assertTrue(leads["Biz 0 Dental Clinic"]["phone"].startswith("+971"))


class TestLocations(unittest.TestCase):
    def test_country_of_location(self):
        cases = {"Bengaluru, Karnataka": "IN", "Koramangala, Bengaluru": "IN", "Bangalore": "IN", "Gurgaon": "IN",
                 "Gurugram, Haryana": "IN", "Chennai, India": "IN", "Madras": "IN", "Noida, Uttar Pradesh": "IN",
                 "Garland, TX": "US", "Dubai": "AE", "Abu Dhabi, UAE": "AE", "London, UK": "GB", "": ""}
        for loc, want in cases.items():
            self.assertEqual(scraper.country_of_location(loc), want, loc)

    def test_yelp_is_refused_for_india_before_anything_runs(self):
        with self.assertRaises(ValueError) as cm:
            scraper.plan_searches({"platform": "yelp", "query": "dentist", "location": "Mumbai"})
        self.assertIn("Use Google Maps", str(cm.exception))
        self.assertEqual(len(scraper.plan_searches({"platform": "yelp", "query": "plumber", "location": "Dallas, TX"})), 1)

    def test_place_suggestions_know_renamed_cities(self):
        import directory
        self.assertIn("Bengaluru, India", directory.places("bangalore"))
        self.assertIn("Mumbai, India", directory.places("bombay"))
        self.assertIn("Gurgaon, India", directory.places("gurugram"))
        self.assertEqual(directory.places("garland", 1), ["Garland, TX"])


if __name__ == "__main__":
    unittest.main()
