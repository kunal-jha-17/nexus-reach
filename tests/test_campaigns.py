"""Campaigns as first-class things: their own dashboard, criteria and settings overrides."""
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase, FakeCtx  # noqa: E402

import db  # noqa: E402
import filtering  # noqa: E402
import llm  # noqa: E402
import outreach  # noqa: E402


def lead(name, phone, campaign="", **kw):
    lid, _ = db.upsert_lead({"business_name": name, "phone": phone, **kw}, campaign=campaign, source="test")
    return lid


class TestCampaignBasics(Base):
    def test_create_validate_and_duplicates(self):
        db.create_campaign("Dallas HVAC", "  trades in DFW ")
        self.assertEqual(db.get_campaign("dallas hvac")["description"], "trades in DFW")   # case-insensitive lookup
        for bad in ("dallas hvac", "DEFAULT", "", "a/b", "x" * 61):
            with self.assertRaises(ValueError, msg=bad):
                db.create_campaign(bad)

    def test_tagging_a_lead_creates_the_campaign_and_names_are_case_insensitive(self):
        lead("A", "2145550100", "DFW")
        b = lead("B", "2145550101", "dfw")
        self.assertEqual([c["name"] for c in db.list_campaigns()], ["DFW"])
        self.assertEqual(db.get_lead(b)["campaign"], "DFW")
        db.update_lead(b, {"campaign": "Houston"})
        self.assertEqual({c["name"] for c in db.list_campaigns()}, {"DFW", "Houston"})

    def test_list_shows_card_numbers(self):
        a = lead("A", "2145550100", "x")
        b = lead("B", "2145550101", "x")
        lead("C", "2145550102", "x")
        lead("Unassigned", "2145550103")
        db.update_lead(a, {"send_status": "sent", "stage": "won"})
        db.update_lead(b, {"send_status": "sent", "stage": "replied", "fit": 1})
        c = db.list_campaigns()[0]
        self.assertEqual((c["leads"], c["contacted"], c["replied"], c["won"], c["fit_yes"]), (3, 2, 2, 1, 1))
        self.assertEqual(db.count_unassigned(), 1)

    def test_filter_for_leads_without_a_campaign(self):
        lead("In", "2145550100", "c")
        lead("Out", "2145550101")
        rows, total = db.list_leads({"campaign": "__none__"})
        self.assertEqual([r["business_name"] for r in rows], ["Out"])
        self.assertEqual(db.select_ids({"campaign": "c"}), [1])

    def test_archive_and_edit(self):
        db.create_campaign("c1")
        db.save_campaign("c1", {"status": "archived", "description": "old"})
        self.assertEqual(db.get_campaign("c1")["status"], "archived")
        with self.assertRaises(ValueError):
            db.save_campaign("c1", {"status": "weird"})
        with self.assertRaises(ValueError):
            db.save_campaign("nope", {"description": "x"})

    def test_delete_keeps_leads_by_default(self):
        db.create_campaign("gone")
        a = lead("A", "2145550100", "gone")
        db.create_schedule("google_maps", "q", "", 10, "gone", 24)
        db.delete_campaign("gone")
        self.assertIsNone(db.get_campaign("gone"))
        self.assertEqual(db.get_lead(a)["campaign"], "")
        self.assertEqual(db.list_schedules(), [])

    def test_delete_with_leads(self):
        lead("A", "2145550100", "gone")
        lead("B", "2145550101", "keep")
        self.assertEqual(db.delete_campaign("gone", delete_leads=True), 1)
        self.assertEqual([l["business_name"] for l in db.list_leads()[0]], ["B"])


class TestCampaignDashboard(Base):
    def test_funnel_rates_queue_and_breakdowns(self):
        db.create_campaign("c")
        ids = [lead(f"L{i}", f"21455501{i:02d}", "c", email=f"l{i}@x.com" if i < 4 else "",
                    category_raw="HVAC contractor" if i < 4 else "Plumber") for i in range(6)]
        db.update_lead(ids[0], {"send_status": "sent", "stage": "won", "message": "hi"})
        db.update_lead(ids[1], {"send_status": "sent", "stage": "replied", "message": "hi"})
        db.update_lead(ids[2], {"send_status": "sent", "stage": "contacted", "message": "hi"})
        db.update_lead(ids[3], {"fit": 0})                            # rejected
        db.update_lead(ids[4], {"message": "draft", "fit": 1})        # phone lead ready to hand-send
        db.update_lead(ids[5], {"do_not_contact": True})
        d = db.campaign_dashboard("c")
        f = {x["key"]: x["count"] for x in d["funnel"]}
        self.assertEqual(f, {"leads": 6, "in_play": 4, "contacted": 3, "replied": 2, "qualified": 1, "won": 1})
        self.assertEqual(d["rates"]["reply_rate"], 66.7)
        self.assertEqual(d["rates"]["win_rate"], 33.3)
        self.assertEqual(d["rates"]["fit_rate"], 50.0)                 # 1 of the 2 judged
        self.assertEqual(d["progress"]["total"], 6)
        q = d["queue"]
        self.assertEqual((q["needs_judge"], q["hand_queue"], q["ready_email"]), (3, 1, 0))
        self.assertEqual(d["by_trade"][0], {"name": "hvac", "count": 4})
        self.assertEqual(len(d["series"]), 14)

    def test_activity_series_counts_sends_and_replies(self):
        a = lead("A", "2145550100", "c")
        db.add_event(a, "email_sent", "{}")
        db.add_event(a, "followup_sent", "{}")
        db.add_event(a, "reply", "{}")
        other = lead("Other campaign", "2145550101", "d")
        db.add_event(other, "email_sent", "{}")
        today = db.campaign_dashboard("c")["series"][-1]
        self.assertEqual((today["sent"], today["replies"]), (2, 1))
        self.assertEqual(len(db.campaign_dashboard("c")["recent"]), 3)

    def test_unknown_campaign(self):
        self.assertIsNone(db.campaign_dashboard("nope"))


class TestCampaignOverrides(Base):
    def test_channel_order_override_changes_channels_and_can_be_cleared(self):
        db.create_campaign("yelp-first")
        a = lead("A", "2145550100", "yelp-first", email="a@x.com", yelp_url="https://www.yelp.com/biz/a")
        b = lead("B", "2145550101", "", email="b@x.com", yelp_url="https://www.yelp.com/biz/b")
        self.assertEqual((db.get_lead(a)["channel"], db.get_lead(b)["channel"]), ("email", "email"))
        db.save_campaign("yelp-first", {"channel_priority": ["yelp", "email"]})
        self.assertEqual((db.get_lead(a)["channel"], db.get_lead(b)["channel"]), ("yelp", "email"))
        c = lead("C", "2145550102", "yelp-first", email="c@x.com", yelp_url="https://www.yelp.com/biz/c")
        self.assertEqual(db.get_lead(c)["channel"], "yelp")            # new leads pick it up too
        db.save_campaign("yelp-first", {"channel_priority": []})
        self.assertEqual(db.get_lead(a)["channel"], "email")

    def test_effective_settings_fall_back_to_the_account_settings(self):
        db.set_settings({"business_info": "GLOBAL", "templates": [{"name": "G", "channel": "email", "body": "g"}]})
        db.create_campaign("c")
        self.assertEqual(db.effective_settings("c")["business_info"], "GLOBAL")
        db.save_campaign("c", {"business_info": "ONLY FOR C",
                               "templates": [{"name": "T", "channel": "email", "body": "t"},
                                             {"name": "", "channel": "email", "body": "dropped: no name"}]})
        s = db.effective_settings("c")
        self.assertEqual((s["business_info"], [t["name"] for t in s["templates"]]), ("ONLY FOR C", ["T"]))
        other = db.effective_settings("")
        self.assertEqual((other["business_info"], other["templates"][0]["name"]), ("GLOBAL", "G"))
        db.save_campaign("c", {"business_info": "", "templates": None})
        self.assertEqual(db.effective_settings("c")["business_info"], "GLOBAL")

    def test_drafting_uses_the_leads_campaign_settings(self):
        db.set_settings({"business_info": "GLOBAL TEXT"})
        db.create_campaign("c")
        db.save_campaign("c", {"business_info": "CAMPAIGN TEXT", "templates": [
            {"name": "Only-in-c", "channel": "email", "body": "campaign template body"}]})
        a = lead("A", "2145550100", "c", email="a@x.com")
        b = lead("B", "2145550101", "", email="b@x.com")
        prompts = []
        with mock.patch.object(llm, "complete", side_effect=lambda p, **kw: prompts.append(p) or "hello"):
            outreach.draft_message(a)
            outreach.draft_message(b)
        self.assertIn("CAMPAIGN TEXT", prompts[0])
        self.assertIn("campaign template body", prompts[0])
        self.assertIn("GLOBAL TEXT", prompts[1])
        self.assertNotIn("campaign template body", prompts[1])
        self.assertEqual(db.get_lead(a)["template_used"], "Only-in-c")

    def test_criteria_per_campaign_with_account_default(self):
        db.create_campaign("c")
        self.assertEqual(db.get_campaign_criteria("c"), "")
        db.set_campaign_criteria("default", "GLOBAL CRITERIA")
        self.assertEqual(db.get_campaign_criteria("c"), "GLOBAL CRITERIA")
        db.set_campaign_criteria("c", "C CRITERIA")
        self.assertEqual(db.get_campaign_criteria("c"), "C CRITERIA")
        self.assertEqual(db.get_campaign_criteria("unknown"), "GLOBAL CRITERIA")
        self.assertNotIn("default", [c["name"] for c in db.list_campaigns()])   # not shown as a campaign

    def test_judging_uses_each_leads_campaign_criteria(self):
        db.set_campaign_criteria("a-camp", "CRITERIA FOR A")
        db.set_campaign_criteria("b-camp", "CRITERIA FOR B")
        lead("A", "2145550100", "a-camp")
        lead("B", "2145550101", "b-camp")
        prompts = []

        def fake(p, **kw):
            prompts.append(p)
            return '{"fits": true, "confidence": "high", "reason": "ok"}'
        with mock.patch.object(llm, "complete", side_effect=fake):
            filtering.run_judge(FakeCtx(), {})
        self.assertTrue(any("CRITERIA FOR A" in p for p in prompts))
        self.assertTrue(any("CRITERIA FOR B" in p for p in prompts))

    def test_jobs_are_tagged_with_their_campaign(self):
        a = db.create_job("enrich", {"campaign": "c"})
        db.create_job("enrich", {"campaign": "other"})
        db.create_job("judge", {})
        self.assertEqual([j["id"] for j in db.list_jobs(campaign="c")], [a])
        self.assertEqual(len(db.list_jobs()), 3)


class TestMigration(Base):
    def test_old_database_is_upgraded_in_place(self):
        """A hub.db from before campaigns were real: tags become campaigns, 'default' becomes a setting."""
        with db.get_conn() as c:
            c.executescript("""
                DROP TABLE campaigns;
                CREATE TABLE campaigns (name TEXT PRIMARY KEY, criteria TEXT DEFAULT '', created_at TEXT);
                DROP TABLE jobs;
                CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, params TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'queued', progress INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                    summary TEXT DEFAULT '', log TEXT DEFAULT '', created_at TEXT, finished_at TEXT);
                INSERT INTO campaigns(name, criteria) VALUES ('default', 'OLD DEFAULT CRITERIA');
                INSERT INTO campaigns(name, criteria) VALUES ('dfw', 'OLD DFW CRITERIA');
                INSERT INTO jobs(kind, status) VALUES ('enrich', 'running');
                INSERT INTO leads(business_name, campaign, phone, created_at, updated_at)
                    VALUES ('Tagged', 'from-tag-only', '+12145550100', 'x', 'x');
            """)
        db.init_db()
        db.init_db()                                                   # running it twice is harmless
        names = {c["name"]: c for c in db.list_campaigns()}
        self.assertEqual(set(names), {"dfw", "from-tag-only"})
        self.assertEqual(names["dfw"]["criteria"], "OLD DFW CRITERIA")
        self.assertEqual(names["dfw"]["status"], "active")
        self.assertEqual(db.get_setting("default_criteria"), "OLD DEFAULT CRITERIA")
        self.assertEqual(db.list_jobs()[0]["status"], "error")         # the interrupted job is closed out
        self.assertEqual(db.list_jobs()[0]["campaign"], "")


class TestCampaignAPI(ApiBase):
    def test_create_edit_dashboard_delete(self):
        c = self.client
        r = c.post("/api/campaigns", json={"name": "Dallas HVAC", "description": "DFW trades"})
        self.assertEqual(r.get_json()["campaign"]["name"], "Dallas HVAC")
        self.assertEqual(c.post("/api/campaigns", json={"name": "dallas hvac"}).status_code, 400)
        r = c.put("/api/campaigns/Dallas%20HVAC", json={
            "criteria": "Only HVAC", "business_info": "I fix ACs", "channel_priority": ["yelp", "email"],
            "templates": [{"name": "T", "channel": "email", "body": "hi {name}"}]})
        camp = r.get_json()["campaign"]
        self.assertEqual((camp["criteria"], camp["channel_priority"]), ("Only HVAC", ["yelp", "email"]))
        c.post("/api/leads", json={"business_name": "Cold Air", "phone": "(214) 555-0100",
                                   "email": "a@b.co", "yelp_url": "https://www.yelp.com/biz/x", "campaign": "dallas hvac"})
        d = c.get("/api/campaigns/Dallas%20HVAC").get_json()
        self.assertEqual(d["funnel"][0]["count"], 1)
        self.assertEqual(d["queue"]["needs_judge"], 1)
        self.assertEqual(d["by_channel"][0]["name"], "yelp")           # the campaign's channel order applied
        self.assertEqual(d["global"]["channel_priority"][0], "email")   # ...while the account default is untouched
        self.assertEqual(c.get("/api/campaigns/nope").status_code, 404)
        r = c.delete("/api/campaigns/Dallas%20HVAC")
        self.assertEqual(r.get_json()["leads_affected"], 1)
        self.assertEqual(c.get("/api/leads").get_json()["leads"][0]["campaign"], "")

    def test_copy_from_another_campaign(self):
        c = self.client
        c.post("/api/campaigns", json={"name": "base"})
        c.put("/api/campaigns/base", json={"criteria": "BASE CRITERIA", "business_info": "BASE INFO"})
        r = c.post("/api/campaigns", json={"name": "copy", "copy_from": "base"})
        self.assertEqual((r.get_json()["campaign"]["criteria"], r.get_json()["campaign"]["business_info"]),
                         ("BASE CRITERIA", "BASE INFO"))

    def test_default_criteria_endpoint_and_listing(self):
        c = self.client
        c.put("/api/campaigns/default", json={"criteria": "MY DEFAULT"})
        d = c.get("/api/campaigns").get_json()
        self.assertEqual((d["saved_default_criteria"], d["campaigns"]), ("MY DEFAULT", []))
        self.assertIn("TARGET PROFILE", d["default_criteria"])

    def test_jobs_scoped_to_a_campaign_show_on_its_dashboard(self):
        c = self.client
        c.post("/api/campaigns", json={"name": "c"})
        c.post("/api/leads", json={"business_name": "A", "website": "a.com", "campaign": "c"})
        with mock.patch.object(__import__("enrichment"), "_get", return_value=""), \
             mock.patch.object(__import__("enrichment"), "serp_search", return_value=None):
            c.post("/api/enrich", json={"campaign": "c"})
        d = c.get("/api/campaigns/c").get_json()
        self.assertEqual([j["kind"] for j in d["jobs"]], ["enrich"])
        self.assertEqual(d["progress"]["enriched"], 1)

    def test_lead_detail_includes_the_campaigns_templates(self):
        c = self.client
        c.post("/api/campaigns", json={"name": "c"})
        c.put("/api/campaigns/c", json={"templates": [{"name": "CT", "channel": "email", "body": "x"}]})
        c.post("/api/leads", json={"business_name": "A", "campaign": "c", "email": "a@b.co"})
        lid = c.get("/api/leads?campaign=c").get_json()["leads"][0]["id"]
        self.assertEqual([t["name"] for t in c.get(f"/api/leads/{lid}").get_json()["templates"]], ["CT"])


if __name__ == "__main__":
    unittest.main()
