"""
Phase 1 fixes from the product gap report: lead merging, landlines, Google Maps columns,
hidden emails, colleague replies, reply sorting, email checks, campaign country codes, scoring.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from email.message import EmailMessage
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase  # noqa: E402

import db  # noqa: E402
import enrichment  # noqa: E402
import importer  # noqa: E402
import llm  # noqa: E402
import outreach  # noqa: E402
import schema  # noqa: E402




class TestMerging(Base):
    def setUp(self):
        super().setUp()
        self._cc = mock.patch.dict(os.environ, {"DEFAULT_COUNTRY_CODE": "971"})
        self._cc.start()

    def tearDown(self):
        self._cc.stop()
        super().tearDown()

    def pair(self, a, b):
        ia, _ = db.upsert_lead(dict(a), campaign="t", source="t")
        ib, new = db.upsert_lead(dict(b), campaign="t", source="t")
        return ia, ib

    def test_two_clinics_with_one_name_stay_separate(self):
        ia, ib = self.pair(
            {"business_name": "Smile Dental Clinic", "city": "Abu Dhabi", "phone": "02 555 0001", "address": "Al Reem Island"},
            {"business_name": "Smile Dental Clinic", "city": "Abu Dhabi", "phone": "02 555 0002", "address": "Mussafah Industrial"})
        self.assertNotEqual(ia, ib)
        self.assertEqual(db.get_lead(ib)["phone"], "+97125550002")

    def test_different_google_place_ids_are_different_places(self):
        ia, ib = self.pair(
            {"business_name": "Pearl Dental", "phone": "02 555 0100", "place_id": "ChIJAAAAAAAAAAAA"},
            {"business_name": "Pearl Dental", "phone": "02 555 0100", "place_id": "ChIJBBBBBBBBBBBB"})
        self.assertNotEqual(ia, ib)

    def test_same_place_id_merges_even_with_other_details_different(self):
        ia, ib = self.pair(
            {"business_name": "Pearl Dental", "phone": "02 555 0100", "place_id": "ChIJAAAAAAAAAAAA"},
            {"business_name": "Pearl Dental Centre", "phone": "050 123 4567",
             "google_maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJAAAAAAAAAAAA"})
        self.assertEqual(ia, ib)

    def test_chain_branches_sharing_a_central_number_are_kept(self):
        ia, ib = self.pair(
            {"business_name": "Prime Dental - Al Reem", "phone": "800 1234 567", "address": "Reem"},
            {"business_name": "Prime Dental - Yas Island", "phone": "800 1234 567", "address": "Yas"})
        self.assertNotEqual(ia, ib)
        self.assertEqual([b["id"] for b in db.branches_of(db.get_lead(ia))], [ib])

    def test_chain_branches_sharing_a_website_are_kept(self):
        ia, ib = self.pair(
            {"business_name": "Aster Clinic Khalifa", "website": "https://asterclinic.ae/khalifa", "phone": "02 555 0010"},
            {"business_name": "Aster Clinic Mussafah", "website": "https://asterclinic.ae/mussafah", "phone": "02 555 0011"})
        self.assertNotEqual(ia, ib)

    def test_the_same_clinic_written_two_ways_still_merges(self):
        ia, ib = self.pair({"business_name": "Pearl Dental Center", "city": "Abu Dhabi", "phone": "02 555 0099"},
                           {"business_name": "Pearl Dental Centre LLC", "city": "Abu Dhabi", "phone": "+971 2 555 0099"})
        self.assertEqual(ia, ib)

    def test_maps_listing_and_instagram_profile_of_one_clinic_merge(self):
        # a landline on Maps, a mobile on Instagram, no address on the Instagram side
        ia, ib = self.pair(
            {"business_name": "Glow Dental", "city": "Dubai", "phone": "04 555 1234", "address": "Jumeirah Beach Road"},
            {"business_name": "Glow Dental", "city": "Dubai", "phone": "050 555 1234", "instagram_url": "https://instagram.com/glowdental"})
        self.assertEqual(ia, ib)
        self.assertTrue(db.get_lead(ia)["instagram_url"])

    def test_exact_relisting_merges(self):
        row = {"business_name": "Vita Dental", "phone": "02 555 7777", "google_maps_url": "https://maps.google.com/?cid=12345678"}
        ia, ib = self.pair(row, row)
        self.assertEqual(ia, ib)


class TestLandlines(Base):
    def test_phone_kinds(self):
        cases = {"+971501234567": "mobile", "+97125551234": "landline", "+9718001234": "landline",
                 "+919876543210": "mobile", "+911123456789": "landline", "+447700900123": "mobile",
                 "+12145550100": "", "+91987654321": ""}
        for number, kind in cases.items():
            self.assertEqual(schema.phone_kind(number), kind, number)

    def test_landline_goes_to_phone_and_mobile_to_whatsapp(self):
        land = self.add(business_name="A", phone="+97125551234")
        mob = self.add(business_name="B", phone="+971501234567")
        self.assertEqual(db.get_lead(land)["channel"], "phone")
        self.assertEqual(db.get_lead(land)["phone_kind"], "landline")
        self.assertEqual(db.get_lead(mob)["channel"], "whatsapp")

    def test_upgrading_an_old_database_moves_landlines_off_whatsapp(self):
        path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        old_schema = db.SCHEMA.replace("    place_id TEXT DEFAULT '',\n", "").replace("    rating REAL,\n", "") \
            .replace("    opening_hours TEXT DEFAULT '',\n", "").replace("    country TEXT DEFAULT '',\n", "") \
            .replace("    phone_kind TEXT DEFAULT '',\n", "").replace("    reply_category TEXT DEFAULT '',\n", "") \
            .replace("    email_check TEXT DEFAULT '',\n", "")
        conn = sqlite3.connect(path)
        conn.executescript(old_schema)
        conn.execute("INSERT INTO leads(business_name, phone, channel, google_maps_url) VALUES "
                     "('Old Clinic', '+97125551234', 'whatsapp', 'https://maps.google.com/?cid=99999999')")
        conn.execute("INSERT INTO leads(business_name, phone, channel, channel_locked) VALUES "
                     "('Locked', '+97125551235', 'whatsapp', 1)")
        conn.commit()
        conn.close()
        db.set_path(path)
        db.init_db()
        rows = {r["business_name"]: r for r in db.list_leads({}, limit=10)[0]}
        self.assertEqual((rows["Old Clinic"]["channel"], rows["Old Clinic"]["phone_kind"]), ("phone", "landline"))
        self.assertEqual(rows["Old Clinic"]["place_id"], "99999999")
        self.assertEqual(rows["Locked"]["channel"], "whatsapp")     # chosen by hand: left alone
        db.set_path(self._tmp.name)
        os.unlink(path)


APIFY_CSV = (
    "title,totalScore,reviewsCount,street,city,countryCode,website,phone,categoryName,url,placeId,"
    "openingHours/0/day,openingHours/0/hours,openingHours/1/day,openingHours/1/hours,permanentlyClosed\n"
    "Smile Dental,4.7,120,Reem,Abu Dhabi,AE,https://smile.ae,+971 2 555 0001,Dentist,"
    "https://www.google.com/maps/search/?api=1&query_place_id=ChIJsmile0000001,ChIJsmile0000001,Monday,9 AM-9 PM,Tuesday,9 AM-9 PM,false\n"
    "Gone Dental,3.1,4,Yas,Abu Dhabi,AE,,+971 2 555 0002,Dentist,https://maps.google.com/?cid=1,ChIJgone00000001,,,,,true\n"
)

OUTSCRAPER_CSV = (
    "name,site,phone,full_address,rating,reviews,working_hours,place_id,location_link,business_status,country_code\n"
    "Pearl Dental,https://pearl.ae,+971 2 555 0003,\"Building 5, Al Bateen, Abu Dhabi, United Arab Emirates\",4.4,30,"
    "\"Sat-Thu 9-9\",ChIJpearl000001,https://www.google.com/maps/place/?q=place_id:ChIJpearl000001,OPERATIONAL,AE\n"
    "Closed Clinic,,+971 2 555 0004,\"Mussafah, Abu Dhabi, United Arab Emirates\",4.0,3,,ChIJclosed00001,,CLOSED_PERMANENTLY,AE\n"
)


class TestGoogleMapsExports(Base):
    def test_apify_columns(self):
        r = importer.import_csv_bytes(APIFY_CSV.encode(), campaign="c")
        self.assertEqual((r["inserted"], r["skipped_closed"]), (1, 1))
        lead = db.get_lead(r["ids"][0])
        self.assertEqual((lead["rating"], lead["review_count"], lead["place_id"], lead["country"]),
                         (4.7, 120, "ChIJsmile0000001", "AE"))
        self.assertEqual(lead["website"], "https://smile.ae")
        self.assertIn("query_place_id", lead["google_maps_url"])
        self.assertEqual(lead["opening_hours"], "Monday: 9 AM-9 PM; Tuesday: 9 AM-9 PM")
        self.assertNotIn("openingHours", lead["notes"])

    def test_outscraper_columns(self):
        r = importer.import_csv_bytes(OUTSCRAPER_CSV.encode(), campaign="c")
        self.assertEqual((r["inserted"], r["skipped_closed"]), (1, 1))
        lead = db.get_lead(r["ids"][0])
        self.assertEqual((lead["rating"], lead["opening_hours"], lead["place_id"], lead["city"], lead["country"]),
                         (4.4, "Sat-Thu 9-9", "ChIJpearl000001", "Abu Dhabi", "AE"))

    def test_importing_the_same_export_twice_adds_nothing(self):
        importer.import_csv_bytes(APIFY_CSV.encode())
        r = importer.import_csv_bytes(APIFY_CSV.encode())
        self.assertEqual((r["inserted"], r["merged"]), (0, 1))

    def test_export_has_the_new_columns(self):
        importer.import_csv_bytes(APIFY_CSV.encode())
        for col in ("rating", "opening_hours", "place_id", "country"):
            self.assertIn(col, schema.EXPORT_COLUMNS)


class TestHiddenEmails(Base):
    def test_deobfuscation(self):
        for raw, want in (("hello [at] clinic [dot] ae", "hello@clinic.ae"), ("info (at) smile.ae", "info@smile.ae"),
                          ("x {at} y {dot} co {dot} uk", "x@y.co.uk"), ("care [@] dent.ae", "care@dent.ae")):
            self.assertEqual(enrichment.deobfuscate_emails(raw), want)
        for plain in ("meet at 5pm", "book at our clinic dot com", "a@b.com"):
            self.assertEqual(enrichment.deobfuscate_emails(plain), plain)

    def test_scan_finds_obfuscated_and_cloudflare_emails(self):
        key = 0x5a
        enc = "%02x" % key + "".join("%02x" % (ord(c) ^ key) for c in "front@pearl.ae")
        pages = {"https://pearl.ae": f'<p>Write to hello [at] pearl [dot] ae</p><span class="__cf_email__" data-cfemail="{enc}">[email protected]</span>'}
        with mock.patch.object(enrichment, "_get", side_effect=lambda u, timeout=12: pages.get(u.rstrip("/"), "")):
            found = enrichment.scan_website("https://pearl.ae")
        self.assertIn("hello@pearl.ae", found["emails"])
        self.assertIn("front@pearl.ae", found["emails"])
        self.assertTrue(enrichment.supported_by("email", "hello@pearl.ae", enrichment.visible_text(pages["https://pearl.ae"])))


class TestReplies(Base):
    def emailed(self, **kw):
        kw.setdefault("business_name", "Acme Dental")
        kw.setdefault("email", "info@acme.ae")
        a = self.add(**kw)
        db.update_lead(a, {"message": "Subject: Who answers after hours at Acme Dental\n\nHi there"})
        sent = []
        with mock.patch.object(outreach, "_deliver", side_effect=lambda m: sent.append(m)):
            outreach.send_lead_email(a)
        return a, sent[0]

    def inbound(self, body, sender, subject="Re: Who answers after hours at Acme Dental", in_reply_to=None):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = sender, "me@example.org", subject
        m["Message-ID"] = f"<{abs(hash(body + sender))}@x>"
        if in_reply_to:
            m["In-Reply-To"] = in_reply_to
        m.set_content(body)
        return m.as_bytes()

    def no_ai(self):
        return mock.patch.dict(os.environ, {"GROQ_API_KEY": ""})

    def test_a_colleague_at_the_same_clinic_is_matched(self):
        a, _ = self.emailed(website="https://acme.ae")
        with self.no_ai():
            r = outreach.process_inbound(self.inbound("Owner here - what does it cost?", "dr.ali@acme.ae", subject="pricing"))
        self.assertEqual(r, "replied")
        lead = db.get_lead(a)
        self.assertEqual((lead["stage"], lead["reply_category"]), ("replied", "question"))

    def test_owner_on_gmail_replying_to_our_subject_is_matched(self):
        a, _ = self.emailed()
        with self.no_ai():
            self.assertEqual(outreach.process_inbound(self.inbound("Yes, call me tomorrow", "ali.owner@gmail.com")), "replied")
        self.assertEqual(db.get_lead(a)["reply_category"], "interested")

    def test_strangers_are_still_ignored(self):
        self.emailed()
        self.assertIsNone(outreach.process_inbound(self.inbound("hi", "someone@other.com")))
        self.assertIsNone(outreach.process_inbound(self.inbound("hi", "rando@gmail.com", subject="hello there friend")))

    def test_a_shared_domain_with_two_emailed_leads_is_not_guessed(self):
        self.emailed(business_name="Chain - Reem", email="reem@chain.ae", website="https://chain.ae/reem", phone="+97125550001")
        self.emailed(business_name="Chain - Yas", email="yas@chain.ae", website="https://chain.ae/yas", phone="+97125550002")
        self.assertIsNone(outreach.process_inbound(self.inbound("hello", "boss@chain.ae", subject="x")))

    def test_reply_sorting_rules(self):
        cases = {"Yes, interested. Call me tomorrow.": "interested",
                 "Please contact our marketing manager sara@other.ae instead.": "wrong_person",
                 "Not interested, thanks.": "not_interested", "We already have a receptionist": "not_interested",
                 "Maybe after Ramadan": "not_now", "How much is it?": "question", "ok": "other"}
        with self.no_ai():
            for text, want in cases.items():
                self.assertEqual(outreach.classify_reply(text), want, text)

    def test_reply_sorting_uses_the_ai_when_there_is_a_key(self):
        with mock.patch.object(llm, "complete", return_value="not_now"):
            self.assertEqual(outreach.classify_reply("We'll see"), "not_now")
        with mock.patch.object(llm, "complete", side_effect=llm.LLMError("down")):
            self.assertEqual(outreach.classify_reply("Not interested"), "not_interested")   # falls back to rules

    def test_interested_replies_are_listed_first(self):
        a, first = self.emailed()
        b, second = self.emailed(business_name="Bee Dental", email="hi@bee.ae")
        with self.no_ai():
            outreach.process_inbound(self.inbound("Not interested", "info@acme.ae", in_reply_to=first["Message-ID"]))
            outreach.process_inbound(self.inbound("Sounds good, let's talk", "hi@bee.ae", subject="Re: Who answers after hours at Acme Dental",
                                                  in_reply_to=second["Message-ID"]))
        self.assertEqual([r["id"] for r in db.replies_waiting()], [b, a])


class TestSendingChecks(Base):
    def ready(self, **kw):
        a = self.add(**kw)
        db.update_lead(a, {"message": "Subject: Hi\n\nHello"})
        return a

    def test_a_domain_that_takes_no_email_is_not_sent_to(self):
        a = self.ready(business_name="X", email="info@nowhere-clinic.ae")
        with mock.patch.object(outreach, "check_email_domain", return_value="no_mail"), \
                mock.patch.object(outreach, "_deliver", side_effect=AssertionError("must not send")):
            with self.assertRaises(outreach.SendError) as cm:
                outreach.send_lead_email(a)
        self.assertEqual(cm.exception.code, 502)
        lead = db.get_lead(a)
        self.assertEqual(lead["send_status"], "failed")
        self.assertIn("doesn't accept email", lead["email_check"])

    def test_an_unanswered_lookup_never_blocks(self):
        a = self.ready(business_name="X", email="info@clinic.ae")
        with mock.patch.object(outreach, "check_email_domain", return_value="unknown"), \
                mock.patch.object(outreach, "_deliver") as deliver:
            outreach.send_lead_email(a)
        deliver.assert_called_once()

    def test_domain_check_reads_dns_answers(self):
        def resp(payload):
            m = mock.Mock()
            m.json.return_value = payload
            m.raise_for_status.return_value = None
            return m
        outreach._DOMAIN_CHECKS.clear()
        with mock.patch.dict(os.environ, {"EMAIL_DOMAIN_CHECK": "1"}):
            with mock.patch.object(outreach.requests, "get", return_value=resp({"Status": 0, "Answer": [{"type": 15, "data": "10 mx.a.ae."}]})):
                self.assertEqual(outreach.check_email_domain("x@good.ae"), "ok")
            with mock.patch.object(outreach.requests, "get", return_value=resp({"Status": 3})):
                self.assertEqual(outreach.check_email_domain("x@gone.ae"), "no_domain")
            with mock.patch.object(outreach.requests, "get", return_value=resp({"Status": 0, "Answer": [{"type": 15, "data": "0 ."}]})):
                self.assertEqual(outreach.check_email_domain("x@nullmx.ae"), "no_mail")
            with mock.patch.object(outreach.requests, "get", side_effect=OSError("offline")):
                self.assertEqual(outreach.check_email_domain("x@unreachable.ae"), "unknown")
            self.assertEqual(outreach.check_email_domain("someone@gmail.com"), "ok")
        outreach._DOMAIN_CHECKS.clear()

    def test_one_inbox_shared_by_branches_is_emailed_once(self):
        with mock.patch.dict(os.environ, {"DEFAULT_COUNTRY_CODE": "971"}):
            a = self.ready(business_name="Prime Dental - Reem", email="info@prime.ae", phone="02 555 0001", place_id="ChIJprime0000001")
            b = self.ready(business_name="Prime Dental - Yas", email="info@prime.ae", phone="02 555 0002", place_id="ChIJprime0000002")
        self.assertNotEqual(a, b)
        with mock.patch.object(outreach, "_deliver") as deliver:
            outreach.send_lead_email(a)
            with self.assertRaises(outreach.SendError) as cm:
                outreach.send_lead_email(b)
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(deliver.call_count, 1)


class TestCampaignCountryAndScoring(ApiBase):
    def test_a_campaign_reads_numbers_with_its_own_country_code(self):
        self.client.post("/api/campaigns", json={"name": "uae"})
        self.client.post("/api/campaigns", json={"name": "india"})
        self.client.put("/api/campaigns/uae", json={"country_code": "+971"})
        self.client.put("/api/campaigns/india", json={"country_code": "91"})
        a, _ = db.upsert_lead({"business_name": "A", "phone": "050 123 4567"}, campaign="uae")
        b, _ = db.upsert_lead({"business_name": "B", "phone": "98765 43210"}, campaign="india")
        self.assertEqual(db.get_lead(a)["phone"], "+971501234567")
        self.assertEqual(db.get_lead(b)["phone"], "+919876543210")

    def test_changing_a_campaigns_code_rereads_its_numbers(self):
        self.client.post("/api/campaigns", json={"name": "uae"})
        a, _ = db.upsert_lead({"business_name": "A", "phone": "050 123 4567"}, campaign="uae")
        self.assertEqual(db.get_lead(a)["phone"], "")           # US default can't read it
        r = self.client.put("/api/campaigns/uae", json={"country_code": "971"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(db.get_lead(a)["phone"], "+971501234567")
        self.assertEqual(self.client.put("/api/campaigns/uae", json={"country_code": "12345"}).status_code, 400)

    def test_scoring_can_stop_favouring_businesses_without_a_website(self):
        site, _ = db.upsert_lead({"business_name": "Has Site", "website": "https://a.com", "phone": "+12145550100"})
        none, _ = db.upsert_lead({"business_name": "No Site", "phone": "+12145550101"})
        self.assertGreater(db.get_lead(none)["score"], db.get_lead(site)["score"])
        r = self.client.put("/api/settings", json={"score_no_website": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.get_lead(none)["score"], db.get_lead(site)["score"])
        self.client.post("/api/campaigns", json={"name": "web"})
        self.client.put("/api/campaigns/web", json={"score_no_website": "yes"})
        w, _ = db.upsert_lead({"business_name": "No Site 2", "phone": "+12145550102"}, campaign="web")
        self.assertEqual(db.get_lead(w)["score"], db.get_lead(none)["score"] + 1)

    def test_lead_api_lists_branches(self):
        with mock.patch.dict(os.environ, {"DEFAULT_COUNTRY_CODE": "971"}):
            a, _ = db.upsert_lead({"business_name": "Prime - Reem", "phone": "800 1234 567"})
            b, _ = db.upsert_lead({"business_name": "Prime - Yas", "phone": "800 1234 567"})
        d = self.client.get(f"/api/leads/{a}").get_json()
        self.assertEqual([x["id"] for x in d["branches"]], [b])


if __name__ == "__main__":
    unittest.main()
