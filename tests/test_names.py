"""
Name columns ("Clinic Name", "Shop", "Name of Practice" ...) are recognised on import, leads
an older import left nameless are repaired, and {name}-style placeholders are filled -- or the
send is refused -- before anything goes out.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase  # noqa: E402

import db  # noqa: E402
import importer  # noqa: E402
import outreach  # noqa: E402
import schema  # noqa: E402

CLINIC_CSV = ("Clinic Name,Doctor Name,Phone,Email,City\n"
              "Smile Dental Care,Dr. Asha Rao,(214) 555-0101,hello@smiledental.com,Dallas\n"
              "Bright Eyes Optical,,(214) 555-0102,,Irving\n")


class TestNameColumns(Base):
    def test_any_business_name_column_is_detected(self):
        for h in ("Clinic Name", "clinic_name", "ClinicName", "Name of Clinic", "Practice", "Shop Name",
                  "Hospital Name", "Restaurant", "Store name", "Business Name", "Company", "Lead Name",
                  "Account Name", "Firm", "Name"):
            self.assertEqual(importer.map_headers([h])[0].get(h), "business_name", h)

    def test_person_columns_go_to_the_contact_not_the_business(self):
        for h in ("Doctor Name", "First Name", "Owner's Name", "Contact Name", "Full Name"):
            self.assertEqual(importer.map_headers([h])[0].get(h), "contact_name", h)

    def test_things_that_are_not_names_are_left_alone(self):
        for h in ("Last Name", "Campaign Name", "File Name", "Username", "City Name", "Template Name"):
            self.assertNotIn(importer.map_headers([h])[0].get(h), ("business_name", "contact_name"), h)
        m, _ = importer.map_headers(["Email", "Phone", "Website", "Email Subject", "Email Body"])
        self.assertEqual((m["Email"], m["Phone"], m["Website"]), ("email", "phone", "website"))

    def test_bare_name_beside_an_explicit_business_column_is_the_person(self):
        m, _ = importer.map_headers(["Name", "Clinic Name", "Phone"])
        self.assertEqual((m["Clinic Name"], m["Name"]), ("business_name", "contact_name"))
        m, _ = importer.map_headers(["name", "phone"])                    # on its own it's still the business
        self.assertEqual(m["name"], "business_name")

    def test_the_friends_clinic_csv_imports_with_names(self):
        s = importer.import_csv_bytes(CLINIC_CSV.encode(), campaign="clinics")
        self.assertEqual((s["inserted"], s["missing_name"]), (2, 0))
        names = sorted(l["business_name"] for l in db.list_leads({})[0])
        self.assertEqual(names, ["Bright Eyes Optical", "Smile Dental Care"])
        lead = next(l for l in db.list_leads({})[0] if l["business_name"] == "Smile Dental Care")
        self.assertEqual(lead["contact_name"], "Dr. Asha Rao")
        self.assertNotIn("Clinic Name", lead["notes"])

    def test_a_file_with_no_name_column_says_so(self):
        s = importer.import_csv_bytes(b"Phone,City\n(214) 555-0101,Dallas\n")
        self.assertEqual((s["inserted"], s["missing_name"]), (1, 1))

    def _old_style_nameless_lead(self):
        # exactly what the previous importer stored for a "Clinic Name" column
        lead_id, _ = db.upsert_lead({"phone": "(214) 555-0101", "email": "hello@smiledental.com",
                                     "notes": "Clinic Name: Smile Dental Care; Rating: 4.8"}, campaign="clinics")
        self.assertEqual(db.get_lead(lead_id)["business_name"], "")
        return lead_id

    def test_existing_nameless_leads_are_repaired_when_the_database_opens(self):
        lead_id = self._old_style_nameless_lead()
        db.init_db()
        lead = db.get_lead(lead_id)
        self.assertEqual(lead["business_name"], "Smile Dental Care")
        self.assertEqual(lead["notes"], "Rating: 4.8")
        self.assertEqual(db.repair_missing_names(), 0)                    # nothing left to do the second time

    def test_re_importing_the_same_file_fills_names_without_duplicating(self):
        db.upsert_lead({"phone": "(214) 555-0101"}, campaign="clinics")
        db.upsert_lead({"phone": "(214) 555-0102"}, campaign="clinics")
        s = importer.import_csv_bytes(CLINIC_CSV.encode(), campaign="clinics")
        self.assertEqual((s["inserted"], s["merged"]), (0, 2))
        self.assertEqual(sorted(l["business_name"] for l in db.list_leads({})[0]),
                         ["Bright Eyes Optical", "Smile Dental Care"])


class TestPlaceholders(unittest.TestCase):
    LEAD = {"business_name": "Smile Dental Care", "contact_name": "Dr. Asha Rao", "city": "Dallas",
            "state": "TX", "trade": "pest_control"}

    def test_every_common_style_is_filled(self):
        for raw in ("{name}", "{business_name}", "{{Clinic Name}}", "{{ company }}", "[Business Name]",
                    "[clinic name]", "<<Company Name>>", "{Practice Name}", "{BUSINESS}"):
            self.assertEqual(schema.fill_placeholders(f"Hi {raw} team", self.LEAD), ("Hi Smile Dental Care team", []), raw)

    def test_other_fields(self):
        text, missing = schema.fill_placeholders("Hi {first_name}, {city}, {state} / {location} / {trade}", self.LEAD)
        self.assertEqual(text, "Hi Asha, Dallas, TX / Dallas, TX / pest control")
        self.assertEqual(missing, [])

    def test_a_blank_value_is_reported_not_silently_dropped(self):
        text, missing = schema.fill_placeholders("Hi {name}, about [Business Name]", {"business_name": ""})
        self.assertEqual(text, "Hi {name}, about [Business Name]")
        self.assertEqual(missing, ["{name}", "[Business Name]"])

    def test_unknown_curly_tokens_are_flagged_but_ordinary_brackets_are_not(self):
        text, missing = schema.fill_placeholders("See [the link] and {{promo_code}}", self.LEAD)
        self.assertEqual((text, missing), ("See [the link] and {{promo_code}}", ["{{promo_code}}"]))


class TestPlaceholdersOnSend(ApiBase):
    def lead(self, name="Smile Dental Care", message="Subject: A site for {{Clinic Name}}\n\nHi [Business Name] team, quick idea."):
        lead_id = self.add(business_name=name, email="hello@smiledental.com")
        db.update_lead(lead_id, {"message": message})
        return lead_id

    def test_email_goes_out_with_the_real_name_in_subject_and_body(self):
        lead_id = self.lead()
        with mock.patch.object(outreach, "_deliver") as deliver:
            r = self.client.post(f"/api/leads/{lead_id}/send-email", json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        sent = deliver.call_args[0][0]
        self.assertEqual(sent["Subject"], "A site for Smile Dental Care")
        self.assertIn("Hi Smile Dental Care team", sent.get_content())
        self.assertNotIn("{", sent["Subject"] + sent.get_content())
        self.assertNotIn("[Business Name]", sent.get_content())

    def test_a_nameless_lead_is_refused_and_nothing_is_sent(self):
        lead_id = self.lead(name="")
        with mock.patch.object(outreach, "_deliver") as deliver:
            r = self.client.post(f"/api/leads/{lead_id}/send-email", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("{{Clinic Name}}", r.get_json()["error"])
        deliver.assert_not_called()
        self.assertEqual(db.get_lead(lead_id)["send_status"], "unsent")

    def test_hand_send_copy_gets_the_filled_message_or_a_clear_refusal(self):
        lead_id = self.lead(message="Hey {name}, saw you're in {city}")
        r = self.client.post(f"/api/leads/{lead_id}/final-message", json={})
        self.assertEqual(r.status_code, 400)                              # no city on this lead
        r = self.client.post(f"/api/leads/{lead_id}/final-message", json={"message": "Hey {name}!"})
        self.assertEqual(r.get_json(), {"message": "Hey Smile Dental Care!"})

    def test_drafting_gives_the_ai_the_real_name_and_cleans_its_output(self):
        lead_id = self.lead(message="")
        db.set_settings({"templates": [{"name": "t", "channel": "email", "body": "Hi {name}, we build sites for {trade} firms."}]})
        with mock.patch.object(outreach.llm, "complete", return_value="Subject: Hello\n\nHi [Business Name], idea for you.") as ai:
            out = outreach.draft_message(lead_id, selection="email::t")
        self.assertIn("Hi Smile Dental Care, we build sites", ai.call_args[0][0])
        self.assertIn("Hi Smile Dental Care, idea for you.", out["message"])


if __name__ == "__main__":
    unittest.main()
