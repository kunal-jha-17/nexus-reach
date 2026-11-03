"""
Phase 2: daily limit / per-run cap, follow-up sequences, several sending inboxes, the Apify
Google Maps source, parallel enrichment and judging, and the "what's working" numbers.
"""
import json
import os
import smtplib
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase, FakeCtx  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import enrichment  # noqa: E402
import filtering  # noqa: E402
import inboxes  # noqa: E402
import llm  # noqa: E402
import outreach  # noqa: E402
import scraper  # noqa: E402


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec="seconds")


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def starttls(self):
        pass

    def login(self, u, p):
        self.user = u

    def send_message(self, m):
        FakeSMTP.sent.append((self.host, self.user, m))

    def quit(self):
        pass


class SmtpBase(Base):
    def setUp(self):
        super().setUp()
        FakeSMTP.sent = []
        self._p = [mock.patch.object(outreach.smtplib, "SMTP", FakeSMTP),
                   mock.patch.object(outreach.smtplib, "SMTP_SSL", FakeSMTP),
                   mock.patch.object(outreach.netguard, "check_mail_host"),
                   mock.patch.dict(os.environ, {"SMTP_HOST": "smtp.main.example", "SMTP_USER": "me@main.example",
                                                "SMTP_PASS": "x", "GMAIL_REFRESH_TOKEN": ""})]
        for p in self._p:
            p.start()
        db.set_settings({"warmup_enabled": False, "email_send_delay_seconds": 0})

    def tearDown(self):
        for p in self._p:
            p.stop()
        super().tearDown()

    def drafted(self, n, **kw):
        ids = []
        for i in range(n):
            lid = self.add(business_name=f"Clinic {i}", email=f"dr{i}@clinic{i}.example", **kw)
            db.update_lead(lid, {"message": f"Subject: Hello {i}\n\nHi"})
            ids.append(lid)
        return ids


# ------------------------------------------------------------------ per-run cap

class TestPerRunCap(SmtpBase):
    def test_send_at_most_n_in_one_run(self):
        self.drafted(10)
        db.set_settings({"email_daily_limit": 100})
        msg = outreach.run_send_bulk(FakeCtx(), {"max": 4})
        self.assertEqual(msg, "4 sent, 0 failed, 0 skipped")
        self.assertEqual(len(FakeSMTP.sent), 4)
        self.assertEqual(outreach.run_send_bulk(FakeCtx(), {}), "6 sent, 0 failed, 0 skipped")


class TestDailyLimitApi(ApiBase):
    def test_outreach_page_can_change_the_daily_limit(self):
        r = self.client.put("/api/settings", json={"email_daily_limit": 3000})
        self.assertEqual(r.get_json()["email_daily_limit"], 3000)
        st = self.client.get("/api/email-status").get_json()
        self.assertEqual(st["max_limit"], 3000)


# ------------------------------------------------------------------ sequences

class TestSequences(SmtpBase):
    def contacted(self, follow_ups=0, sent_days_ago=10, campaign=""):
        lid = self.add(business_name="Acme", email="owner@acme.example", campaign=campaign)
        db.update_lead(lid, {"message": "Subject: First\n\nHi", "send_status": "sent", "stage": "contacted",
                             "email_message_id": "<m1@x>", "follow_up_count": follow_ups,
                             "last_sent_at": days_ago(sent_days_ago), "sent_at": days_ago(sent_days_ago)})
        db.add_event(lid, "email_sent", json.dumps({"subject": "First", "message_id": "<m1@x>"}))
        return lid

    def test_each_step_has_its_own_wait_and_message(self):
        db.set_settings({"followup_steps": [{"days": 2, "body": "Step one for {business_name}"},
                                            {"days": 5, "body": "Step two"}, {"days": 7, "body": ""}]})
        lid = self.contacted(follow_ups=1, sent_days_ago=3)          # step 2 waits 5 days: not yet
        self.assertEqual(outreach.followups_due(), [])
        db.update_lead(lid, {"last_sent_at": days_ago(6)})
        self.assertEqual([l["id"] for l in outreach.followups_due()], [lid])
        outreach.send_followup(lid)
        self.assertIn("Step two", FakeSMTP.sent[-1][2].get_content())
        self.assertEqual(db.get_lead(lid)["follow_up_count"], 2)

    def test_first_step_text_and_blank_step_uses_the_ai(self):
        db.set_settings({"followup_steps": [{"days": 2, "body": "Hi {business_name}, any thoughts?"}, {"days": 3, "body": ""}]})
        lid = self.contacted(sent_days_ago=3)
        outreach.send_followup(lid)
        self.assertIn("Hi Acme, any thoughts?", FakeSMTP.sent[-1][2].get_content())
        db.update_lead(lid, {"last_sent_at": days_ago(4)})
        with mock.patch.object(llm, "complete", return_value="Just checking in.") as ai:
            outreach.send_followup(lid)
        ai.assert_called_once()
        with self.assertRaises(outreach.SendError):               # sequence finished
            outreach.send_followup(lid)
        db.update_lead(lid, {"last_sent_at": days_ago(30)})
        self.assertEqual(outreach.followups_due(), [])

    def test_without_steps_the_old_settings_still_apply(self):
        db.set_settings({"followup_steps": [], "followup_delay_days": 3, "max_followups": 1,
                         "followup_template": "Old style nudge"})
        lid = self.contacted(sent_days_ago=4)
        self.assertEqual(len(outreach.followups_due()), 1)
        outreach.send_followup(lid)
        self.assertIn("Old style nudge", FakeSMTP.sent[-1][2].get_content())
        db.update_lead(lid, {"last_sent_at": days_ago(10)})
        self.assertEqual(outreach.followups_due(), [])

    def test_a_campaign_can_have_its_own_sequence(self):
        db.create_campaign("fast")
        db.save_campaign("fast", {"followup_steps": [{"days": 1, "body": "Fast nudge"}]})
        db.set_settings({"followup_steps": [{"days": 9, "body": "Slow nudge"}]})
        fast = self.contacted(sent_days_ago=2, campaign="fast")
        self.assertEqual([l["id"] for l in outreach.followups_due()], [fast])
        outreach.send_followup(fast)
        self.assertIn("Fast nudge", FakeSMTP.sent[-1][2].get_content())

    def test_bad_steps_are_refused(self):
        with self.assertRaises(ValueError):
            db.clean_followup_steps([{"days": 0, "body": "x"}])
        with self.assertRaises(ValueError):
            db.clean_followup_steps([{"days": "soon"}])
        self.assertEqual(len(db.clean_followup_steps([{"days": 2}] * 15)), 10)


# ------------------------------------------------------------------ several inboxes

EXTRA = [{"id": "ib2", "label": "Second", "enabled": True, "daily_limit": 3, "from_email": "two@second.example",
          "smtp_host": "smtp.second.example", "smtp_port": "587", "smtp_user": "two@second.example", "smtp_pass": "p2"},
         {"id": "ib3", "label": "Third", "enabled": True, "daily_limit": 3, "from_email": "three@third.example",
          "smtp_host": "smtp.third.example", "smtp_port": "587", "smtp_user": "three@third.example", "smtp_pass": "p3"}]


class TestInboxRotation(SmtpBase):
    def with_inboxes(self, boxes):
        import userctx
        user = dict(userctx.get_user() or {"id": 1, "is_admin": True})
        user["secrets"] = dict(user.get("secrets") or {}, INBOXES=boxes)
        return mock.patch.object(userctx, "get_user", return_value=user)

    def test_the_day_is_shared_between_inboxes(self):
        self.drafted(12)
        db.set_settings({"email_daily_limit": 3})
        with self.with_inboxes(EXTRA):
            self.assertEqual(outreach.total_daily_limit(), 9)
            msg = outreach.run_send_bulk(FakeCtx(), {})
            status = outreach.email_status()
        self.assertEqual(msg, "9 sent, 0 failed, 0 skipped")
        hosts = [h for h, _, _ in FakeSMTP.sent]
        self.assertEqual({hosts.count(h) for h in set(hosts)}, {3})
        self.assertEqual(set(hosts), {"smtp.main.example", "smtp.second.example", "smtp.third.example"})
        froms = {m["From"] for _, _, m in FakeSMTP.sent}
        self.assertEqual(froms, {"me@main.example", "two@second.example", "three@third.example"})
        self.assertEqual([i["sent_today"] for i in status["inboxes"]], [3, 3, 3])
        self.assertEqual(hosts[:3], ["smtp.main.example", "smtp.second.example", "smtp.third.example"])   # round robin

    def test_a_follow_up_leaves_from_the_inbox_of_the_first_email(self):
        ids = self.drafted(3)
        with self.with_inboxes(EXTRA):
            for lid in ids:
                outreach.send_lead_email(lid)
            first_from = {lid: db.list_events(lid)[0] for lid in ids}
            target = next(lid for lid in ids if "ib3" in first_from[lid]["detail"])
            db.update_lead(target, {"last_sent_at": days_ago(5)})
            db.set_settings({"followup_steps": [{"days": 2, "body": "Nudge"}]})
            FakeSMTP.sent = []
            outreach.send_followup(target)
        host, _, msg = FakeSMTP.sent[0]
        self.assertEqual((host, msg["From"]), ("smtp.third.example", "three@third.example"))
        self.assertTrue(msg["Subject"].startswith("Re: "))

    def test_paused_or_broken_inboxes_are_skipped(self):
        self.drafted(4)
        boxes = [dict(EXTRA[0], enabled=False), dict(EXTRA[1], smtp_pass="")]
        with self.with_inboxes(boxes):
            outreach.run_send_bulk(FakeCtx(), {})
        self.assertEqual({h for h, _, _ in FakeSMTP.sent}, {"smtp.main.example"})

    def test_extra_inboxes_alone_are_enough(self):
        self.drafted(2)
        with mock.patch.dict(os.environ, {"SMTP_USER": "", "SMTP_PASS": ""}), self.with_inboxes(EXTRA[:1]):
            self.assertTrue(outreach.smtp_configured())
            self.assertEqual(outreach.run_send_bulk(FakeCtx(), {}), "2 sent, 0 failed, 0 skipped")
        self.assertEqual({h for h, _, _ in FakeSMTP.sent}, {"smtp.second.example"})

    def test_replies_are_checked_in_every_inbox(self):
        logins = []

        class IMAP:
            def __init__(self, host, timeout=None):
                self.host = host

            def login(self, u, p):
                logins.append((self.host, u, p))

            def select(self, *a, **k):
                return "OK", [b"0"]

            def response(self, code):
                return code, [b"1"]

            def uid(self, cmd, *a):
                return "OK", [b""]

            def logout(self):
                pass
        boxes = [dict(EXTRA[0], imap_host="imap.second.example")]
        with self.with_inboxes(boxes), mock.patch.object(outreach.imaplib, "IMAP4_SSL", IMAP):
            r = outreach.check_replies()
        self.assertEqual(r["checked"], 0)
        self.assertEqual({h for h, _, _ in logins}, {"imap.gmail.com", "imap.second.example"})
        self.assertIn(("imap.second.example", "two@second.example", "p2"), logins)

    def test_settings_never_leak_between_inboxes(self):
        with self.with_inboxes(EXTRA):
            with inboxes.using(EXTRA[0]):
                self.assertEqual((outreach.from_address(), config.env("SMTP_HOST"), config.env("IMAP_PASS")),
                                 ("two@second.example", "smtp.second.example", ""))
            self.assertEqual(outreach.from_address(), "me@main.example")


class TestInboxApi(ApiBase):
    def test_add_edit_remove_without_ever_showing_the_password(self):
        r = self.client.post("/api/inboxes", json={"label": "Sales 2", "smtp_user": "s2@acme.in", "smtp_pass": "secret-pass",
                                                   "smtp_host": "smtp.zoho.in", "smtp_port": "587", "daily_limit": 400})
        self.assertEqual(r.status_code, 200, r.get_json())
        ib = r.get_json()["inboxes"][0]
        self.assertTrue(ib["smtp_pass_set"])
        self.assertNotIn("secret-pass", json.dumps(r.get_json()))
        self.assertEqual(ib["daily_limit"], 400)
        r = self.client.post("/api/inboxes", json={"id": ib["id"], "daily_limit": 250, "smtp_pass": ""})
        self.assertEqual(r.get_json()["inboxes"][0]["daily_limit"], 250)
        self.assertTrue(r.get_json()["inboxes"][0]["smtp_pass_set"])            # blank box kept the password
        self.assertEqual(self.client.post("/api/inboxes", json={"smtp_user": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/inboxes", json={"smtp_user": "a@b.in", "smtp_pass": "p",
                                                                 "smtp_port": "1234"}).status_code, 400)
        r = self.client.delete(f"/api/inboxes/{ib['id']}")
        self.assertEqual(r.get_json()["inboxes"], [])

    def test_inbox_test_runs_with_that_inboxs_login(self):
        r = self.client.post("/api/inboxes", json={"smtp_user": "s2@acme.in", "smtp_pass": "pw2", "smtp_host": "smtp.acme.in"})
        iid = r.get_json()["inboxes"][0]["id"]
        seen = []

        class S(FakeSMTP):
            def login(self, u, p):
                seen.append((self.host, u, p))
        with mock.patch.object(outreach.smtplib, "SMTP", S), mock.patch.object(outreach.netguard, "check_mail_host"):
            r = self.client.post(f"/api/inboxes/{iid}/test", json={"kind": "smtp"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(seen, [("smtp.acme.in", "s2@acme.in", "pw2")])


# ------------------------------------------------------------------ Apify

class FakeApify:
    def __init__(self, items, statuses=("RUNNING", "SUCCEEDED"), cost=1.23):
        self.items, self.statuses, self.cost, self.calls, self.polls = items, list(statuses), cost, [], 0

    def __call__(self, method, url, headers=None, timeout=None, json=None, params=None):
        self.calls.append((method, url, json, params))
        r = mock.Mock(status_code=200)
        if method == "POST" and url.endswith("/runs"):
            r.json.return_value = {"data": {"id": "run1", "defaultDatasetId": "ds1", "status": "RUNNING"}}
        elif "/datasets/ds1/items" in url:
            # results appear over time: half while running, the rest once it has finished
            visible = self.items[: len(self.items) // 2] if self.polls == 0 else self.items
            r.json.return_value = visible[params["offset"]: params["offset"] + params["limit"]]
        elif "/actor-runs/run1" in url and method == "GET":
            self.polls += 1
            st = self.statuses[min(self.polls, len(self.statuses) - 1)]
            r.json.return_value = {"data": {"id": "run1", "defaultDatasetId": "ds1", "status": st, "usageTotalUsd": self.cost}}
        elif url.endswith("/abort"):
            r.json.return_value = {"data": {"status": "ABORTED"}}
        elif url.endswith("/users/me"):
            r.json.return_value = {"data": {"username": "kunal", "plan": {"id": "FREE"}}}
        return r


def place(i, **kw):
    d = {"title": f"Smile Dental {i}", "totalScore": 4.5, "reviewsCount": 30 + i, "phone": f"098765 {10000 + i}",
         "website": "" if i % 2 else f"https://smile{i}.in", "address": f"No {i}, MG Road, Bengaluru, Karnataka 560001, India",
         "city": "Bengaluru", "state": "Karnataka", "countryCode": "IN", "categoryName": "Dentist",
         "url": f"https://www.google.com/maps/search/?api=1&query_place_id=ChIJsmile{i:07d}", "placeId": f"ChIJsmile{i:07d}",
         "openingHours": [{"day": "Monday", "hours": "9 AM to 8 PM"}], "emails": [f"hi@smile{i}.in"] if i % 3 == 0 else [],
         "searchString": "dentist"}
    d.update(kw)
    return d


class TestApify(Base):
    def run_it(self, fake, **params):
        p = {"platform": "apify_maps", "query": "dentist", "location": "Bengaluru, Karnataka", "max_results": 1000,
             "campaign": "blr"}
        p.update(params)
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "tok", "DEFAULT_COUNTRY_CODE": "971"}), \
                mock.patch("apify.requests.request", side_effect=fake), mock.patch("apify.POLL_SECONDS", 0):
            return scraper.run_scrape(FakeCtx(), p)

    def test_places_are_imported_with_their_details(self):
        items = [place(i) for i in range(20)] + [place(99, permanentlyClosed=True)]
        fake = FakeApify(items)
        msg = self.run_it(fake)
        self.assertIn("20 scraped -> 20 new", msg)
        self.assertIn("Apify cost $1.23", msg)
        leads = {l["business_name"]: l for l in db.list_leads({}, limit=100)[0]}
        a = leads["Smile Dental 0"]
        self.assertEqual((a["phone"], a["rating"], a["review_count"], a["place_id"], a["country"], a["city"]),
                         ("+919876510000", 4.5, 30, "ChIJsmile0000000", "IN", "Bengaluru"))
        self.assertEqual((a["email"], a["opening_hours"]), ("hi@smile0.in", "Monday: 9 AM to 8 PM"))
        start = next(c for c in fake.calls if c[1].endswith("/runs"))
        self.assertEqual(start[2]["searchStringsArray"], ["dentist"])
        self.assertEqual(start[2]["locationQuery"], "Bengaluru, Karnataka")
        self.assertEqual((start[2]["maxImages"], start[2]["maxReviews"], start[2]["scrapeContacts"]), (0, 0, False))

    def test_no_website_filter_and_rerun_dedupes(self):
        items = [place(i) for i in range(10)]
        self.run_it(FakeApify(items), only_no_website=True)
        self.assertEqual(len(db.list_leads({}, limit=100)[0]), 5)
        msg = self.run_it(FakeApify(items))
        self.assertIn("5 new, 5 already known", msg)

    def test_stopping_aborts_the_apify_run(self):
        fake = FakeApify([place(i) for i in range(4)], statuses=("RUNNING", "RUNNING", "RUNNING"))
        ctx = FakeCtx()
        calls = {"n": 0}

        def sleep(s):
            calls["n"] += 1
            ctx.stopped = calls["n"] >= 2
            return ctx.stopped
        ctx.sleep = sleep
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "tok"}), mock.patch("apify.requests.request", side_effect=fake):
            scraper.run_scrape(ctx, {"platform": "apify_maps", "query": "dentist", "location": "Pune", "max_results": 50})
        self.assertTrue(any(c[1].endswith("/abort") for c in fake.calls))

    def test_bad_token_is_explained(self):
        def refuse(*a, **k):
            return mock.Mock(status_code=401, text="", json=lambda: {})
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "bad"}), mock.patch("apify.requests.request", side_effect=refuse):
            with self.assertRaises(RuntimeError) as cm:
                scraper.run_scrape(FakeCtx(), {"platform": "apify_maps", "query": "x", "location": "Pune"})
        self.assertIn("refused the API token", str(cm.exception))


class TestApifyApi(ApiBase):
    def test_needs_a_token_and_allows_big_runs(self):
        r = self.client.post("/api/scrape", json={"platform": "apify_maps", "query": "dentist", "location": "Pune"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Apify API token", r.get_json()["error"])
        self.client.put("/api/connections", json={"set": {"APIFY_TOKEN": "tok"}})
        started = []
        with mock.patch.object(self.appmod_jobs(), "start_job", side_effect=lambda kind, p, fn, **k: started.append(p) or 1):
            r = self.client.post("/api/scrape", json={"platform": "apify_maps", "query": "dentist", "location": "Pune",
                                                      "max_results": 3000, "contacts": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((started[0]["max_results"], started[0]["contacts"]), (3000, True))
        self.assertEqual(self.client.get("/api/config-status").get_json()["apify"], True)

    def appmod_jobs(self):
        import jobs
        return jobs


# ------------------------------------------------------------------ parallel steps

class TestParallel(Base):
    def test_enrichment_runs_side_by_side_and_keeps_the_account(self):
        for i in range(8):
            self.add(business_name=f"L{i}", phone=f"(214) 555-01{10 + i}")
        threads, seen_keys = set(), set()

        def slow(lead, *a, **k):
            threads.add(threading.current_thread().name)
            seen_keys.add(config.env("GROQ_API_KEY"))           # the account's settings reach every worker
            time.sleep(0.3)
            return {"updates": {"notes": "x"}, "notes": "", "searches": 0}
        t = time.time()
        with mock.patch.object(enrichment, "enrich_lead", side_effect=slow), mock.patch.dict(os.environ, {"ENRICH_WORKERS": "4"}):
            msg = enrichment.run_enrich(FakeCtx(), {})
        self.assertIn("8 enriched", msg)
        self.assertLess(time.time() - t, 1.6)                   # 8 x 0.3 s one at a time would be 2.4 s
        self.assertGreater(len(threads), 1)
        self.assertEqual(seen_keys, {"k"})

    def test_judging_in_parallel_counts_correctly(self):
        for i in range(9):
            self.add(business_name=f"Fit {i}" if i % 3 else f"Nope {i}", phone=f"(214) 555-02{10 + i}")

        def ai(prompt, task="filter", **k):
            return json.dumps({"fits": "Fit" in prompt, "confidence": "high", "reason": "r"})
        with mock.patch.object(llm, "complete", side_effect=ai), mock.patch.dict(os.environ, {"FILTER_DELAY_MS": "0"}):
            msg = filtering.run_judge(FakeCtx(), {})
        self.assertEqual(msg, "6 fit, 3 rejected")

    def test_a_missing_ai_key_still_stops_judging(self):
        for i in range(6):
            self.add(business_name=f"L{i}", phone=f"(214) 555-03{10 + i}")
        with mock.patch.object(llm, "complete", side_effect=llm.LLMError("GROQ_API_KEY not set")) as ai:
            with self.assertRaises(llm.LLMError):
                filtering.run_judge(FakeCtx(), {})
        self.assertLessEqual(ai.call_count, 6)


# ------------------------------------------------------------------ what's working

class TestPerformance(Base):
    def test_rates_by_template(self):
        for i in range(10):
            lid = self.add(business_name=f"A{i}", email=f"a{i}@x.example")
            db.update_lead(lid, {"template_used": "Short", "sent_at": days_ago(3), "send_status": "sent"})
            if i < 3:
                db.add_event(lid, "reply", "{}")
                db.update_lead(lid, {"reply_category": "interested" if i < 2 else "not_interested"})
            if i == 9:
                db.add_event(lid, "bounced", "{}")
        for i in range(5):
            lid = self.add(business_name=f"B{i}", email=f"b{i}@x.example")
            db.update_lead(lid, {"template_used": "Long", "sent_at": days_ago(3), "send_status": "sent"})
        lid = self.add(business_name="C", email="c@x.example")
        db.update_lead(lid, {"template_used": "Long", "sent_at": days_ago(5), "send_status": "sent", "follow_up_count": 1})
        db.add_event(lid, "followup_sent", "{}")
        db.add_event(lid, "reply", "{}")
        self.add(business_name="Not contacted")
        p = db.performance()
        t = {r["name"]: r for r in p["by_template"]}
        self.assertEqual((t["Short"]["contacted"], t["Short"]["replied"], t["Short"]["interested"], t["Short"]["bounced"]),
                         (10, 3, 2, 1))
        self.assertEqual((t["Short"]["reply_rate"], t["Short"]["bounce_rate"]), (30.0, 10.0))
        self.assertEqual((t["Long"]["contacted"], t["Long"]["replied"], t["Long"]["replied_after_followup"]), (6, 1, 1))
        self.assertEqual(sum(r["contacted"] for r in p["by_channel"]), 16)


if __name__ == "__main__":
    unittest.main()
