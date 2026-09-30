"""
Run with:  python3 -m unittest discover -s tests -v

Everything external (LLM, SMTP, IMAP, SerpAPI, web pages) is mocked, so the
suite runs offline. It tests the pipeline logic, not the live websites.
"""
import json
import os
import sys
import unittest
from email.message import EmailMessage
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase, FakeCtx  # noqa: E402  (sets up the isolated environment)

import config  # noqa: E402
import db  # noqa: E402
import schema  # noqa: E402
import importer  # noqa: E402
import enrichment  # noqa: E402
import filtering  # noqa: E402
import outreach  # noqa: E402
import llm  # noqa: E402


# ------------------------------------------------------------------ schema
class TestSchema(unittest.TestCase):
    def test_phone_us_formats(self):
        for raw in ("(214) 555-0100", "214-555-0100", "214.555.0100", "+1 214 555 0100",
                    "1-214-555-0100", "(214) 555-0100 ext 5"):
            self.assertEqual(schema.norm_phone(raw), "+12145550100", raw)

    def test_phone_rejects_junk(self):
        for raw in ("", "12345", "000-000-0000", "1234567890123456789"):
            self.assertEqual(schema.norm_phone(raw), "", raw)

    def test_phone_international_kept(self):
        self.assertEqual(schema.norm_phone("+91 98765 43210"), "+919876543210")

    def test_email(self):
        self.assertEqual(schema.norm_email(" MAILTO:Info@Foo.COM?subject=x "), "info@foo.com")
        self.assertEqual(schema.norm_email("logo@2x.png"), "")
        self.assertEqual(schema.norm_email("not an email"), "")

    def test_yelp_redirect_absolute_and_relative(self):
        a = "https://www.yelp.com/biz_redir?url=http%3A%2F%2Fwww.acme.com%2F&src_bizid=abc"
        r = "/biz_redir?url=http%3A%2F%2Fwww.acme.com&cachebuster=1"
        self.assertEqual(schema.norm_url(a), "http://www.acme.com")
        self.assertEqual(schema.norm_url(r), "http://www.acme.com")

    def test_instagram_forms(self):
        want = "https://www.instagram.com/acme.plumbing/"
        for raw in ("@acme.plumbing", "acme.plumbing", "https://instagram.com/acme.plumbing?igshid=1",
                    "https://l.instagram.com/?u=https%3A%2F%2Fwww.instagram.com%2Facme.plumbing%2F"):
            self.assertEqual(schema.norm_instagram(raw), want, raw)
        self.assertEqual(schema.norm_instagram("https://instagram.com/p/ABC/"), "")
        self.assertEqual(schema.instagram_handle(want), "acme.plumbing")

    def test_facebook_page_as_website_is_not_a_website(self):
        n = schema.normalize_lead({"business_name": "X", "website": "https://www.facebook.com/acmeplumbing"})
        self.assertEqual(n["has_website"], 0)
        self.assertEqual(n["website"], "")
        self.assertEqual(n["facebook_url"], "https://www.facebook.com/acmeplumbing")

    def test_yelp_and_maps_links_routed(self):
        n = schema.normalize_lead({"business_name": "X", "website": "https://www.yelp.com/biz/acme-dallas"})
        self.assertEqual(n["yelp_url"], "https://www.yelp.com/biz/acme-dallas")
        self.assertEqual(n["has_website"], 0)
        n = schema.normalize_lead({"business_name": "X", "website": "https://www.google.com/maps/place/Acme"})
        self.assertTrue(n["google_maps_url"])

    def test_linktree_is_not_a_real_website(self):
        n = schema.normalize_lead({"business_name": "X", "website": "linktr.ee/acme"})
        self.assertEqual(n["has_website"], 0)
        n2 = schema.normalize_lead({"business_name": "X", "website": "acme.com"})
        self.assertEqual((n2["has_website"], n2["website_domain"]), (1, "acme.com"))

    def test_city_state_and_trade(self):
        self.assertEqual(schema.parse_city_state("123 Main St, Fort Worth, TX 76102"), ("Fort Worth", "TX"))
        self.assertEqual(schema.parse_city_state("Dallas, TX, USA"), ("Dallas", "TX"))
        self.assertEqual(schema.derive_trade("Heating & Air Conditioning"), "hvac")
        self.assertEqual(schema.derive_trade("Plumber"), "plumbing")
        self.assertEqual(schema.derive_trade("Pest control service"), "pest_control")
        self.assertEqual(schema.derive_trade("Bakery"), "other")
        self.assertEqual(schema.derive_trade(""), "")

    def test_channel_derivation(self):
        d = schema.derive_channel
        self.assertEqual(d({"email": "a@b.co", "facebook_url": "x"}), "email")
        self.assertEqual(d({"yelp_url": "y", "facebook_url": "f"}), "yelp")
        self.assertEqual(d({"phone": "+12145550100"}), "phone")          # US -> not WhatsApp
        self.assertEqual(d({"phone": "+919876543210"}), "whatsapp")      # non-US -> WhatsApp ok
        self.assertEqual(d({}), "unknown")
        self.assertEqual(d({"email": "a@b.co", "yelp_url": "y"}, ["yelp", "email"]), "yelp")

    def test_score(self):
        s, _ = schema.score_lead({"has_website": 0, "review_count": 5, "phone": "+1", "email": "a@b.co",
                                  "facebook_url": "f", "address": "x"})
        self.assertEqual(s, 10)


# ---------------------------------------------------------------------- db
class TestDedupe(Base):
    def test_same_business_different_phone_formats_merge(self):
        a = self.add(business_name="Acme", phone="(214) 555-0100")
        b, new = db.upsert_lead({"business_name": "Acme Co", "phone": "+1 214-555-0100", "email": "x@acme.com"})
        self.assertFalse(new)
        self.assertEqual(a, b)
        self.assertEqual(db.get_lead(a)["email"], "x@acme.com")   # blank filled by the merge

    def test_merge_never_overwrites_existing(self):
        a = self.add(business_name="Acme", phone="2145550100", email="first@acme.com")
        db.upsert_lead({"business_name": "Acme", "phone": "2145550100", "email": "other@acme.com"})
        self.assertEqual(db.get_lead(a)["email"], "first@acme.com")

    def test_two_yelp_leads_without_phone_do_not_collide(self):
        # Original scraper collapsed these via the 'yelp.com' domain key.
        a, _ = db.upsert_lead({"business_name": "Alpha HVAC", "website": "/biz_redir?url=http%3A%2F%2Falpha.com"})
        b, new = db.upsert_lead({"business_name": "Beta Roofing", "website": "/biz_redir?url=http%3A%2F%2Fbeta.com"})
        self.assertTrue(new)
        self.assertNotEqual(a, b)
        self.assertEqual(db.get_lead(a)["website"], "http://alpha.com")

    def test_name_and_city_match(self):
        a = self.add(business_name="Bob's Roofing LLC", address="1 A St, Dallas, TX 75001")
        _, new = db.upsert_lead({"business_name": "Bobs Roofing", "address": "9 B St, Dallas, TX 75002"})
        self.assertFalse(new)
        _, new = db.upsert_lead({"business_name": "Bobs Roofing", "address": "9 B St, Austin, TX 73301"})
        self.assertTrue(new)   # same name, different city -> different business
        self.assertTrue(a)

    def test_update_normalises_and_channel_lock(self):
        a = self.add(email="a@b.co")
        self.assertEqual(db.get_lead(a)["channel"], "email")
        db.update_lead(a, {"phone": "(214) 555-0100"})
        self.assertEqual(db.get_lead(a)["phone"], "+12145550100")
        db.update_lead(a, {"channel": "phone"})
        db.update_lead(a, {"email": "c@d.co"})
        self.assertEqual(db.get_lead(a)["channel"], "phone")       # locked by hand
        db.update_lead(a, {"channel": "auto"})
        self.assertEqual(db.get_lead(a)["channel"], "email")

    def test_invalid_values_rejected(self):
        a = self.add()
        with self.assertRaises(ValueError):
            db.update_lead(a, {"stage": "converted"})
        with self.assertRaises(ValueError):
            db.update_lead(a, {"channel": "carrier-pigeon"})

    def test_suppression_flags_and_blocks(self):
        a = self.add(email="no@acme.com")
        db.add_suppression("No@Acme.com", "asked")
        self.assertEqual(db.get_lead(a)["do_not_contact"], 1)
        s = importer.import_leads([{"business_name": "Again", "email": "no@acme.com"}])
        self.assertEqual(s["skipped_suppressed"], 1)
        # phone suppression works too, not just email
        b = self.add(business_name="Phone Guy", phone="2145550111")
        db.add_suppression("(214) 555-0111")
        self.assertEqual(db.get_lead(b)["do_not_contact"], 1)


# ---------------------------------------------------------------- importer
class TestImporter(Base):
    def test_outscraper_style(self):
        csv_text = ("name,site,phone,full_address,category,reviews,location_link,email_1,facebook,extra\n"
                    "Cowboy Air,,(214) 555-0101,\"5 Elm St, Dallas, TX 75201\",HVAC contractor,12,"
                    "https://www.google.com/maps/place/x,al@cowboyair.com,https://facebook.com/cowboyair,hello\n")
        s = importer.import_csv_bytes(csv_text.encode(), campaign="dfw")
        self.assertEqual(s["inserted"], 1)
        lead = db.get_lead(s and db.select_ids()[0])
        self.assertEqual((lead["trade"], lead["city"], lead["state"]), ("hvac", "Dallas", "TX"))
        self.assertEqual(lead["phone"], "+12145550101")
        self.assertEqual(lead["channel"], "email")
        self.assertIn("extra: hello", lead["notes"])
        self.assertEqual(lead["campaign"], "dfw")
        self.assertEqual(lead["has_website"], 0)

    def test_html_tool_columns_with_ready_made_messages(self):
        csv_text = ("Business Name,Phone,Yelp URL,Trade,City,Has Website,SMS-WhatsApp,Facebook DM,"
                    "Email Subject,Email Body,Yelp Message\n"
                    "Parsons Pest,(214) 555-0102,https://www.yelp.com/biz/parsons,pest control,Dallas,No,"
                    "sms text,fb text,Website for Parsons,email text,yelp text\n")
        s = importer.import_csv_bytes(csv_text.encode())
        self.assertEqual(s["inserted"], 1)
        lead = db.get_lead(db.select_ids()[0])
        self.assertEqual(lead["yelp_url"], "https://www.yelp.com/biz/parsons")
        self.assertEqual(lead["email"], "")             # 'Email Subject' must NOT be read as an email
        self.assertEqual(lead["channel"], "yelp")
        self.assertEqual(lead["message"], "yelp text")   # message for the lead's own channel
        self.assertEqual(lead["trade"], "pest_control")

    def test_row_with_nothing_is_skipped(self):
        s = importer.import_csv_bytes(b"name,phone\n,\n")
        self.assertEqual((s["inserted"], s["skipped_empty"]), (0, 1))


# --------------------------------------------------------------- filtering
class TestAutoRemove(ApiBase):
    def test_off_by_default_rejected_leads_are_kept(self):
        a = self.add(business_name="Has Site", category_raw="Roofer")
        with mock.patch.object(llm, "complete", return_value='{"fits": false, "confidence": "high", "reason": "has a site"}'):
            filtering.run_judge(FakeCtx(), {})
        self.assertIsNotNone(db.get_lead(a))
        self.assertEqual(db.get_lead(a)["fit"], 0)

    def test_on_untouched_rejected_lead_is_deleted(self):
        db.set_settings({"auto_remove_rejected": True})
        a = self.add(business_name="Has Site", category_raw="Roofer")
        with mock.patch.object(llm, "complete", return_value='{"fits": false, "confidence": "high", "reason": "has a site"}'):
            out = filtering.run_judge(FakeCtx(), {})
        self.assertIsNone(db.get_lead(a))
        self.assertIn("1 auto-removed", out)

    def test_on_but_a_fit_lead_is_never_touched(self):
        db.set_settings({"auto_remove_rejected": True})
        a = self.add(business_name="No Site", category_raw="Roofer")
        with mock.patch.object(llm, "complete", return_value='{"fits": true, "confidence": "high", "reason": "no site"}'):
            filtering.run_judge(FakeCtx(), {})
        self.assertIsNotNone(db.get_lead(a))

    def test_on_but_a_lead_with_notes_a_draft_or_activity_is_kept(self):
        db.set_settings({"auto_remove_rejected": True})
        drafted = self.add(business_name="Drafted")
        db.update_lead(drafted, {"message": "hello"})
        noted = self.add(business_name="Noted")
        db.update_lead(noted, {"notes": "spoke to owner"})
        contacted = self.add(business_name="Contacted")
        db.update_lead(contacted, {"contact_name": "Jane"})
        with mock.patch.object(llm, "complete", return_value='{"fits": false, "confidence": "high", "reason": "no"}'):
            out = filtering.run_judge(FakeCtx(), {})
        for lid in (drafted, noted, contacted):
            lead = db.get_lead(lid)
            self.assertIsNotNone(lead, lid)
            self.assertEqual(lead["fit"], 0)   # still correctly marked rejected, just not deleted
        self.assertNotIn("auto-removed", out)

    def test_setting_persists_and_is_read_back_via_the_api(self):
        r = self.client.put("/api/settings", json={"auto_remove_rejected": True})
        self.assertTrue(r.get_json()["auto_remove_rejected"])
        self.assertTrue(self.client.get("/api/settings").get_json()["auto_remove_rejected"])


class TestFiltering(Base):
    def test_judge_and_failure_leaves_unjudged(self):
        a = self.add(business_name="Good HVAC", category_raw="HVAC contractor")
        b = self.add(business_name="Flaky Roofing", category_raw="Roofer")
        replies = ['{"fits": true, "confidence": "high", "reason": "no website"}', llm.LLMError("boom")]

        def fake(prompt, **kw):
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(llm, "complete", side_effect=fake):
            out = filtering.run_judge(FakeCtx(), {})
        la, lb = db.get_lead(a), db.get_lead(b)
        self.assertEqual((la["fit"], la["fit_confidence"]), (1, "high"))
        self.assertIsNone(lb["fit"])                  # NOT silently rejected
        self.assertIn("1 could not be judged", out)

    def test_prompt_contains_trade_and_city(self):
        a = self.add(category_raw="Plumber", address="1 A St, Dallas, TX 75001")
        p = filtering.build_prompt(db.get_lead(a), "criteria here")
        self.assertIn("trade: plumbing", p)
        self.assertIn("city: Dallas", p)
        self.assertIn("has_website: no", p)


# -------------------------------------------------------------- enrichment
class TestEnrichment(Base):
    def test_fills_blank_only_and_free_website_scan(self):
        a = self.add(business_name="Acme", website="acme.com", phone="2145550100")
        html = ('<html><body><a href="mailto:owner@acme.com">mail</a>'
                '<a href="https://facebook.com/acme">fb</a><p>Call us</p></body></html>')
        with mock.patch.object(enrichment, "_get", return_value=html), \
             mock.patch.object(llm, "complete", return_value="{}"):
            res = enrichment.enrich_lead(db.get_lead(a), max_searches=0)
        self.assertEqual(res["updates"]["email"], "owner@acme.com")
        self.assertEqual(res["updates"]["facebook_url"], "https://www.facebook.com/acme")
        self.assertNotIn("phone", res["updates"])          # already had one

    def test_llm_guesses_are_dropped(self):
        a = self.add(business_name="Acme HVAC")
        serp = [{"title": "Acme HVAC - contact", "link": "https://x.com", "snippet": "Email info@acmehvac.com"}]
        llm_reply = json.dumps({"email": "info@acmehvac.com", "phone": "214-555-9999",
                                "contact_name": "Made Up Person", "confidence_notes": ""})
        with mock.patch.object(enrichment, "serp_search", return_value=serp), \
             mock.patch.object(llm, "complete", return_value=llm_reply):
            res = enrichment.enrich_lead(db.get_lead(a), max_searches=2)
        self.assertEqual(res["updates"].get("email"), "info@acmehvac.com")   # appears in results
        self.assertNotIn("phone", res["updates"])                            # invented
        self.assertNotIn("contact_name", res["updates"])                     # invented

    def test_search_budget_respected(self):
        a = self.add(business_name="Acme")
        calls = []
        with mock.patch.dict(os.environ, {"SERPAPI_MONTHLY_LIMIT": "2"}), \
             mock.patch("enrichment.requests.get") as g:
            g.return_value.json.return_value = {"organic_results": []}
            g.return_value.raise_for_status = lambda: None
            with mock.patch.object(llm, "complete", return_value="{}"):
                enrichment.enrich_lead(db.get_lead(a), max_searches=5)
                calls.append(g.call_count)
        self.assertEqual(calls[0], 2)                 # stopped at the monthly cap
        self.assertEqual(db.usage_get("serpapi"), 2)

    def test_job_marks_enriched_and_is_resumable(self):
        a = self.add(business_name="One", website="one.com")
        b = self.add(business_name="Two", website="two.com")
        with mock.patch.object(enrichment, "_get", return_value=""), \
             mock.patch.object(enrichment, "serp_search", return_value=None), \
             mock.patch.object(llm, "complete", return_value="{}"):
            enrichment.run_enrich(FakeCtx(), {"ids": [a]})
            self.assertIsNotNone(db.get_lead(a)["enriched_at"])
            self.assertIsNone(db.get_lead(b)["enriched_at"])
            out = enrichment.run_enrich(FakeCtx(), {})          # only the not-yet-done one
        self.assertIn("1 enriched", out)


# ---------------------------------------------------------------- outreach
class TestOutreach(Base):
    def test_template_rotation_and_prompt(self):
        tpls = [{"name": "A", "channel": "email", "body": "tA"}, {"name": "B", "channel": "email", "body": "tB"},
                {"name": "S", "channel": "phone", "body": "tS"}]
        self.assertEqual(outreach.resolve_template(tpls, "email", "", 0)[0], "A")
        self.assertEqual(outreach.resolve_template(tpls, "email", "", 1)[0], "B")
        self.assertEqual(outreach.resolve_template(tpls, "whatsapp", "", 0)[0], "S")
        self.assertEqual(outreach.resolve_template(tpls, "yelp", "", 0), (None, None))
        self.assertEqual(outreach.resolve_template(tpls, "yelp", "email::B")[0], "B")

    def test_draft_saves_message_and_strips_subject_for_dms(self):
        a = self.add(business_name="Acme", facebook_url="https://facebook.com/acme")
        with mock.patch.object(llm, "complete", return_value='"Subject: hi\n\nHey Acme!"'):
            out = outreach.draft_message(a)
        self.assertEqual(db.get_lead(a)["message"], "Hey Acme!")
        self.assertNotIn("Subject", out["message"])

    def _emailed(self):
        a = self.add(business_name="Acme", email="owner@acme.com")
        db.update_lead(a, {"message": "Subject: Website for Acme\n\nHi there"})
        sent = []
        with mock.patch.object(outreach, "_deliver", side_effect=lambda m: sent.append(m)):
            outreach.send_lead_email(a)
        return a, sent[0]

    def test_send_email(self):
        a, msg = self._emailed()
        lead = db.get_lead(a)
        self.assertEqual((lead["send_status"], lead["stage"]), ("sent", "contacted"))
        self.assertEqual(msg["Subject"], "Website for Acme")
        self.assertIn("reply STOP", msg.get_content())
        self.assertTrue(lead["email_message_id"].startswith("<"))
        self.assertEqual(outreach.emails_sent_today(), 1)

    def test_no_double_send_and_guards(self):
        a, _ = self._emailed()
        with self.assertRaises(outreach.SendError):
            outreach.send_lead_email(a)
        b = self.add(business_name="NoMail")
        with self.assertRaises(outreach.SendError):
            outreach.send_lead_email(b)
        c = self.add(business_name="DNC", email="dnc@x.com")
        db.update_lead(c, {"message": "hi", "do_not_contact": True})
        with self.assertRaises(outreach.SendError):
            outreach.send_lead_email(c)

    def test_warmup_limit(self):
        s = db.get_settings()
        self.assertEqual(outreach.effective_daily_limit(s), 10)      # day 1 of warm-up
        s["warmup_enabled"] = False
        self.assertEqual(outreach.effective_daily_limit(s), s["email_daily_limit"])
        db.set_settings({"email_daily_limit": 1, "warmup_enabled": False})
        a, _ = self._emailed()
        b = self.add(business_name="Second", email="two@acme.com")
        db.update_lead(b, {"message": "hello"})
        with self.assertRaises(outreach.SendError) as cm:
            outreach.send_lead_email(b)
        self.assertEqual(cm.exception.code, 429)

    def test_followup_threads_under_original(self):
        a, first = self._emailed()
        db.set_settings({"followup_delay_days": 1})
        with db.get_conn() as c:
            c.execute("UPDATE leads SET last_sent_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (a,))
        self.assertEqual([l["id"] for l in outreach.followups_due()], [a])
        sent = []
        with mock.patch.object(llm, "complete", return_value="Just bumping this."), \
             mock.patch.object(outreach, "_deliver", side_effect=lambda m: sent.append(m)):
            outreach.send_followup(a)
        self.assertEqual(sent[0]["Subject"], "Re: Website for Acme")
        self.assertEqual(sent[0]["In-Reply-To"], first["Message-ID"])
        lead = db.get_lead(a)
        self.assertEqual(lead["follow_up_count"], 1)
        self.assertIn("Hi there", lead["message"])                   # original kept, not overwritten

    def _inbound(self, body, sender="owner@acme.com", in_reply_to=None, **headers):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = sender, "me@example.org", "Re: Website for Acme"
        m["Message-ID"] = f"<{abs(hash(body))}@acme>"
        if in_reply_to:
            m["In-Reply-To"] = in_reply_to
        for k, v in headers.items():
            m[k.replace("_", "-")] = v
        m.set_content(body)
        return m.as_bytes()

    def test_reply_quoting_our_footer_is_not_an_optout(self):
        a, first = self._emailed()
        footer = db.get_settings()["email_footer"]
        body = (f"Sounds interesting, call me tomorrow.\n\nOn Mon, me wrote:\n> Hi there\n> --\n> {footer}\n> reply STOP")
        r = outreach.process_inbound(self._inbound(body, in_reply_to=first["Message-ID"]), footer)
        self.assertEqual(r, "replied")
        lead = db.get_lead(a)
        self.assertEqual((lead["stage"], lead["do_not_contact"]), ("replied", 0))
        # processing the same mail again does nothing
        self.assertIsNone(outreach.process_inbound(self._inbound(body, in_reply_to=first["Message-ID"]), footer))

    def test_real_optout_detected(self):
        a, first = self._emailed()
        r = outreach.process_inbound(self._inbound("Please remove me from your list.", in_reply_to=first["Message-ID"]))
        self.assertEqual(r, "optout")
        lead = db.get_lead(a)
        self.assertEqual((lead["stage"], lead["do_not_contact"]), ("dead", 1))
        self.assertTrue(db.is_suppressed(lead))

    def test_stop_by_the_shop_is_not_an_optout(self):
        self.assertFalse(outreach.is_optout("Stop by the shop tomorrow and we can talk."))
        self.assertTrue(outreach.is_optout("STOP"))
        self.assertTrue(outreach.is_optout("stop emailing"))
        self.assertTrue(outreach.is_optout("Unsubscribe me please"))

    def test_auto_reply_ignored_and_unknown_sender_ignored(self):
        a, first = self._emailed()
        r = outreach.process_inbound(self._inbound("I am away", in_reply_to=first["Message-ID"], Auto_Submitted="auto-replied"))
        self.assertEqual(r, "auto")
        self.assertEqual(db.get_lead(a)["stage"], "contacted")
        self.assertIsNone(outreach.process_inbound(self._inbound("hi", sender="stranger@else.com")))

    def test_bulk_send_stops_at_limit(self):
        db.set_settings({"email_daily_limit": 2, "warmup_enabled": False, "email_send_delay_seconds": 0})
        for i in range(4):
            a = self.add(business_name=f"L{i}", email=f"l{i}@x.com")
            db.update_lead(a, {"message": "hello"})
        with mock.patch.object(outreach, "_deliver"):
            out = outreach.run_send_bulk(FakeCtx(), {})
        self.assertIn("2 sent", out)
        self.assertEqual(db.stats()["by_send_status"].get("sent"), 2)

    def test_mark_sent_manual_channel(self):
        a = self.add(yelp_url="https://www.yelp.com/biz/acme")
        outreach.mark_sent(a)
        lead = db.get_lead(a)
        self.assertEqual((lead["send_status"], lead["stage"]), ("sent", "contacted"))


# --------------------------------------------------------------------- API
class TestAPI(ApiBase):

    def test_import_list_patch_bulk_export(self):
        data = {"file": (__import__("io").BytesIO(b"name,phone,email\nAcme,(214) 555-0100,a@acme.com\n"), "l.csv"),
                "campaign": "dfw"}
        r = self.client.post("/api/import", data=data, content_type="multipart/form-data")
        self.assertEqual(r.get_json()["inserted"], 1)
        leads = self.client.get("/api/leads?campaign=dfw").get_json()["leads"]
        lid = leads[0]["id"]
        self.assertEqual(leads[0]["phone"], "+12145550100")
        r = self.client.patch(f"/api/leads/{lid}", json={"stage": "qualified"})
        self.assertEqual(r.get_json()["lead"]["stage"], "qualified")
        self.assertEqual(self.client.patch(f"/api/leads/{lid}", json={"stage": "nope"}).status_code, 400)
        r = self.client.post("/api/leads/bulk", json={"ids": [lid], "action": "set_campaign", "value": "new-c"})
        self.assertEqual(r.get_json()["affected"], 1)
        csv_text = self.client.get("/api/export.csv").get_data(as_text=True)
        self.assertIn("Acme", csv_text)
        self.assertIn("+12145550100", csv_text)
        events = self.client.get(f"/api/leads/{lid}").get_json()["events"]
        self.assertTrue(any(e["kind"] == "stage" for e in events))

    def test_enrich_job_via_api(self):
        self.add(business_name="A", website="a.com")
        with mock.patch.object(enrichment, "_get", return_value=""), \
             mock.patch.object(enrichment, "serp_search", return_value=None):
            jid = self.client.post("/api/enrich", json={}).get_json()["job_id"]
        job = self.client.get(f"/api/jobs/{jid}").get_json()
        self.assertEqual(job["status"], "done")
        self.assertIn("1 enriched", job["summary"])

    def test_judge_needs_llm_key(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            r = self.client.post("/api/judge", json={})
        self.assertEqual(r.status_code, 400)

    def test_settings_roundtrip_and_channel_recompute(self):
        a = self.add(email="a@b.co", yelp_url="https://www.yelp.com/biz/x")
        self.assertEqual(db.get_lead(a)["channel"], "email")
        r = self.client.put("/api/settings", json={
            "channel_priority": ["yelp", "email"], "email_daily_limit": 25,
            "templates": [{"name": "T", "channel": "email", "body": "hi"}, {"name": "", "channel": "email", "body": "x"}]})
        s = r.get_json()
        self.assertEqual((s["email_daily_limit"], len(s["templates"])), (25, 1))
        self.assertEqual(db.get_lead(a)["channel"], "yelp")

    def test_suppression_api(self):
        r = self.client.post("/api/suppression", json={"value": "Stop@Me.com"})
        self.assertEqual(r.get_json()["value"], "stop@me.com")
        self.assertEqual(self.client.post("/api/suppression", json={"value": "garbage"}).status_code, 400)
        self.assertEqual(len(self.client.get("/api/suppression").get_json()), 1)

    def test_index_page_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
