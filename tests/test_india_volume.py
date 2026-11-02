"""
Indian leads (numbers, landlines, addresses, directory spreadsheets) and high-volume sending
(professional SMTP servers, connection reuse, warm-up, pacing, bounce pause, one-click unsubscribe).
"""
import os
import smtplib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from common import Base, ApiBase, FakeCtx  # noqa: E402

import db  # noqa: E402
import importer  # noqa: E402
import outreach  # noqa: E402
import schema  # noqa: E402


class India(Base):
    def setUp(self):
        super().setUp()
        self._cc = mock.patch.dict(os.environ, {"DEFAULT_COUNTRY_CODE": "91"})
        self._cc.start()

    def tearDown(self):
        self._cc.stop()
        super().tearDown()


class TestIndianNumbers(India):
    def test_every_common_way_of_writing_an_indian_number(self):
        cases = {
            "98765 43210": "+919876543210", "098765-43210": "+919876543210", "+91 98765 43210": "+919876543210",
            "91-98765-43210": "+919876543210", "0091 9876543210": "+919876543210", "+91-9876543210": "+919876543210",
            "011-2345 6789": "+911123456789", "080 2345 6789": "+918023456789", "(044) 2345 6789": "+914423456789",
            "+91 (0) 80 2345 6789": "+918023456789", "+91 080 2345 6789": "+918023456789",
            "1800 425 1234": "+9118004251234", "1860-500-1234": "+9118605001234",
            "98765 43210 / 98765 43211": "+919876543210", "Mob: 98765-43210, Ph: 022 2345 6789": "+919876543210",
            "9876543210 or 9123456780": "+919876543210",
            "12345": "", "9876543": "", "98765 4321": "",            # too short to be an Indian number
        }
        for raw, want in cases.items():
            self.assertEqual(schema.norm_phone(raw), want, raw)

    def test_mobiles_get_whatsapp_and_landlines_get_a_call(self):
        cases = {"98765 43210": ("mobile", "whatsapp"), "011-2345 6789": ("landline", "phone"),
                 "080 2345 6789": ("landline", "phone"), "+91 80 2345 6789": ("landline", "phone"),
                 "(079) 2654 3210": ("landline", "phone"), "1800 425 1234": ("landline", "phone"),
                 "70123 45678": ("mobile", "whatsapp"), "+91 80501 23456": ("mobile", "whatsapp")}
        for raw, (kind, channel) in cases.items():
            lead = db.get_lead(self.add(business_name=f"Clinic {raw}", phone=raw))
            self.assertEqual((lead["phone_kind"], lead["channel"]), (kind, channel), raw)


class TestIndianAddresses(India):
    def test_addresses(self):
        cases = {
            "No. 12, MG Road, Indiranagar, Bengaluru, Karnataka 560038, India": ("Bengaluru", "Karnataka"),
            "Shop 4, Sector 18, Noida, Uttar Pradesh 201301": ("Noida", "Uttar Pradesh"),
            "12 Park Street, Kolkata - 700016, India": ("Kolkata", ""),
            "Andheri West, Mumbai, Maharashtra 400053": ("Mumbai", "Maharashtra"),
            "Connaught Place, New Delhi, Delhi 110001, India": ("New Delhi", "Delhi"),
            "Plot 7, Jubilee Hills, Hyderabad, Telangana 500033": ("Hyderabad", "Telangana"),
            "Near Bus Stand, Madurai 625001, Tamil Nadu, India": ("Madurai", "Tamil Nadu"),
        }
        for addr, (city, state) in cases.items():
            n = schema.normalize_lead({"business_name": "x", "address": addr})
            self.assertEqual((n["city"], n["state"], n["country"]), (city, state, "IN"), addr)

    def test_us_and_uae_addresses_are_untouched(self):
        n = schema.normalize_lead({"business_name": "x", "address": "123 Main St, Dallas, TX 75201"})
        self.assertEqual((n["city"], n["state"], n["country"]), ("Dallas", "TX", "US"))
        n = schema.normalize_lead({"business_name": "x", "address": "Building 5, Al Reem Island, Abu Dhabi, United Arab Emirates"})
        self.assertEqual((n["city"], n["country"]), ("Abu Dhabi", "AE"))

    def test_state_codes_on_an_indian_list(self):
        n = schema.normalize_lead({"business_name": "x", "state": "UP", "phone": "98765 43210"})
        self.assertEqual((n["state"], n["country"]), ("Uttar Pradesh", "IN"))
        with mock.patch.dict(os.environ, {"DEFAULT_COUNTRY_CODE": "1"}):
            self.assertEqual(schema.normalize_lead({"business_name": "x", "state": "GA"})["state"], "GA")   # Georgia, not Goa

    def test_old_and_new_city_names_are_one_place(self):
        self.assertEqual(schema.make_name_key("Smile Dental Pvt Ltd", "Bangalore"), schema.make_name_key("Smile Dental", "Bengaluru"))
        self.assertEqual(schema.make_name_key("A", "Gurgaon"), schema.make_name_key("A", "Gurugram"))
        self.assertEqual(schema.make_name_key("Acme Plumbing LLC", "Dallas"), "acmeplumbing|dallas")   # unchanged for existing leads

    def test_names_in_indian_scripts_dedupe(self):
        a = self.add(business_name="शर्मा डेंटल क्लिनिक", city="Delhi")
        b = self.add(business_name="शर्मा डेंटल क्लिनिक", city="New Delhi", email="sharma@gmail.com")
        self.assertEqual(a, b)


JUSTDIAL_CSV = """Firm Name,Contact Person,Mobile No,Landline,Email ID,Address,Locality,City,State,Pincode,Rating
Sharma Dental Clinic,Dr. Anil Sharma,98765 43210 / 98765 43211,080-2345 6789,sharmadental@gmail.com,"No 12, 5th Cross",Indiranagar,Bangalore,Karnataka,560038,4.6
Smile Care Dental,Dr. Priya,+91-99001-12233,,info@smilecare.in,"Shop 4, Sector 18",,Noida,UP,201301,4.2
Sharma Dental Clinic,,9876543210,,,"No 12, 5th Cross",Indiranagar,Bengaluru,Karnataka,560038,
City Dental Hospital,,(044) 2345 6789,,contact@citydental.co.in,"Anna Salai, Chennai, Tamil Nadu 600002, India",,,,,
"""


class TestIndianSpreadsheets(India):
    def test_a_directory_export(self):
        r = importer.import_csv_bytes(JUSTDIAL_CSV.encode(), campaign="blr")
        self.assertEqual((r["inserted"], r["merged"]), (3, 1))           # Bangalore = Bengaluru
        self.assertEqual(r["column_mapping"]["Mobile No"], "phone")
        self.assertEqual(r["column_mapping"]["Email ID"], "email")
        leads = {db.get_lead(i)["business_name"]: db.get_lead(i) for i in set(r["ids"])}
        s = leads["Sharma Dental Clinic"]
        self.assertEqual((s["contact_name"], s["phone"], s["email"], s["city"], s["state"], s["country"], s["rating"]),
                         ("Dr. Anil Sharma", "+919876543210", "sharmadental@gmail.com", "Bangalore", "Karnataka", "IN", 4.6))
        self.assertEqual(s["address"], "No 12, 5th Cross, Indiranagar 560038")
        self.assertIn("080-2345 6789", s["notes"])                         # the second number is kept, not lost
        self.assertEqual(leads["Smile Care Dental"]["state"], "Uttar Pradesh")
        c = leads["City Dental Hospital"]
        self.assertEqual((c["city"], c["state"], c["phone_kind"], c["channel"]), ("Chennai", "Tamil Nadu", "landline", "email"))

    def test_a_us_list_with_a_zip_column_still_reads_city_and_state(self):
        with mock.patch.dict(os.environ, {"DEFAULT_COUNTRY_CODE": "1"}):
            r = importer.import_csv_bytes(b"Business Name,Address,Zip\nAcme Plumbing,\"12 Main St, Dallas, TX\",75201\n")
        lead = db.get_lead(r["ids"][0])
        self.assertEqual((lead["city"], lead["state"], lead["address"]), ("Dallas", "TX", "12 Main St, Dallas, TX 75201"))


# ------------------------------------------------------------------ high-volume sending

class FakeSMTP:
    """Counts connections and messages; can drop the connection once to test reconnecting."""
    connects = 0
    sent = []
    drop_next = False

    def __init__(self, host, port, timeout=None):
        FakeSMTP.connects += 1
        self.host, self.port = host, port

    def starttls(self):
        pass

    def login(self, u, p):
        self.user = u

    def send_message(self, m):
        if FakeSMTP.drop_next:
            FakeSMTP.drop_next = False
            raise smtplib.SMTPServerDisconnected("idle timeout")
        FakeSMTP.sent.append((self.host, self.port, m))

    def quit(self):
        pass


class Volume(Base):
    def setUp(self):
        super().setUp()
        FakeSMTP.connects, FakeSMTP.sent, FakeSMTP.drop_next = 0, [], False
        self._p = [mock.patch.object(outreach.smtplib, "SMTP", FakeSMTP),
                   mock.patch.object(outreach.smtplib, "SMTP_SSL", FakeSMTP),
                   mock.patch.object(outreach.netguard, "check_mail_host")]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        super().tearDown()

    def ses(self, **extra):
        env = {"SMTP_HOST": "email-smtp.ap-south-1.amazonaws.com", "SMTP_PORT": "2587", "SMTP_USER": "AKIAEXAMPLEKEY",
               "SMTP_PASS": "ses-secret", "SMTP_FROM_EMAIL": "kunal@neurospark.in", "SMTP_FROM_NAME": "Kunal",
               "GMAIL_REFRESH_TOKEN": "", "IMAP_USER": "", "IMAP_PASS": ""}
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def drafted(self, n):
        ids = []
        with db.batch():
            for i in range(n):
                lid = self.add(business_name=f"Clinic {i}", email=f"owner{i}@clinic{i}.in")
                db.update_lead(lid, {"message": f"Subject: Hi {i}\n\nHello"})
                ids.append(lid)
        return ids


class TestProfessionalMail(Volume):
    def test_a_sending_service_with_a_key_login_sends_from_your_address(self):
        lid = self.drafted(1)[0]
        with self.ses():
            self.assertEqual(outreach.email_transport(), "smtp")
            outreach.send_lead_email(lid)
            self.assertFalse(outreach.imap_configured())     # an SES key can't open an inbox
        host, port, msg = FakeSMTP.sent[0]
        self.assertEqual((host, port), ("email-smtp.ap-south-1.amazonaws.com", 2587))
        self.assertEqual(msg["From"], "Kunal <kunal@neurospark.in>")
        self.assertTrue(msg["Message-ID"].endswith("@neurospark.in>"))

    def test_your_own_mail_server_wins_over_a_saved_gmail_sign_in(self):
        with self.ses(GMAIL_REFRESH_TOKEN="tok", GOOGLE_CLIENT_ID="c", GOOGLE_CLIENT_SECRET="s"):
            self.assertEqual(outreach.email_transport(), "smtp")
            with mock.patch.dict(os.environ, {"EMAIL_SEND_VIA": "gmail_api"}):
                self.assertEqual(outreach.email_transport(), "gmail_api")
        with mock.patch.dict(os.environ, {"SMTP_HOST": "", "GMAIL_REFRESH_TOKEN": "tok", "GOOGLE_CLIENT_ID": "c",
                                          "GOOGLE_CLIENT_SECRET": "s"}):
            self.assertEqual(outreach.email_transport(), "gmail_api")    # plain Gmail setup: as before

    def test_one_connection_carries_a_hundred_emails(self):
        self.drafted(250)
        db.set_settings({"email_daily_limit": 5000, "warmup_enabled": False, "email_send_delay_seconds": 0})
        with self.ses():
            summary = outreach.run_send_bulk(FakeCtx(), {})
        self.assertEqual(summary, "250 sent, 0 failed, 0 skipped")
        self.assertEqual(len(FakeSMTP.sent), 250)
        self.assertEqual(FakeSMTP.connects, 3)

    def test_a_dropped_connection_is_reopened(self):
        self.drafted(3)
        db.set_settings({"email_daily_limit": 100, "warmup_enabled": False, "email_send_delay_seconds": 0})
        FakeSMTP.drop_next = True
        with self.ses():
            self.assertEqual(outreach.run_send_bulk(FakeCtx(), {}), "3 sent, 0 failed, 0 skipped")
        self.assertEqual(FakeSMTP.connects, 2)

    def test_implicit_tls_ports(self):
        lid = self.drafted(1)[0]
        with self.ses(SMTP_PORT="465", SMTP_HOST="smtp.hostinger.com", SMTP_USER="me@neurospark.in"), \
                mock.patch.object(outreach.smtplib, "SMTP", side_effect=AssertionError("465 is SSL from the start")):
            outreach.send_lead_email(lid)
        self.assertEqual(FakeSMTP.sent[0][1], 465)

    def test_provider_daily_caps(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": "smtp.gmail.com", "SMTP_USER": "me@gmail.com", "GMAIL_REFRESH_TOKEN": ""}):
            self.assertEqual(outreach.provider_daily_cap(), 500)
        with mock.patch.dict(os.environ, {"SMTP_HOST": "smtp.gmail.com", "SMTP_USER": "me@company.in", "GMAIL_REFRESH_TOKEN": ""}):
            self.assertEqual(outreach.provider_daily_cap(), 2000)
        with mock.patch.dict(os.environ, {"SMTP_HOST": "smtp.office365.com", "GMAIL_REFRESH_TOKEN": ""}):
            self.assertEqual(outreach.provider_daily_cap(), 10000)
        with self.ses():
            self.assertIsNone(outreach.provider_daily_cap())


class TestPacingAndSafety(Volume):
    def test_warmup_for_a_big_limit_grows_steadily(self):
        settings = db.get_settings()
        settings.update({"email_daily_limit": 5000, "warmup_enabled": True})
        caps = []
        for days in (0, 5, 10, 15, 20, 25, 40):
            settings["sending_started_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            caps.append(outreach.effective_daily_limit(settings))
        self.assertEqual(caps[0], 20)
        self.assertEqual(caps, sorted(caps))
        self.assertEqual(caps[-1], 5000)
        self.assertLess(caps[2], 500)                       # day 10: still a few hundred
        settings.update({"email_daily_limit": 40, "sending_started_at": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(outreach.effective_daily_limit(settings), 10)       # small limits: the old 10/20/30 ramp

    def test_automatic_pacing_spreads_the_day(self):
        settings = db.get_settings()
        settings.update({"email_daily_limit": 5000, "warmup_enabled": False, "auto_pace": True})
        d = outreach.send_delay(settings, sent_today=0)
        self.assertGreaterEqual(d, 1)
        self.assertLessEqual(d, 18)                          # at most a day's seconds / 5000
        settings["auto_pace"] = False
        self.assertEqual(outreach.send_delay(settings), int(settings["email_send_delay_seconds"]))

    def test_sending_pauses_when_too_many_bounce(self):
        ids = self.drafted(60)
        for lid in ids[:50]:
            db.add_event(lid, "email_sent", "{}")
        for lid in ids[:5]:
            db.add_event(lid, "bounced", "{}")              # 10% bounced
        db.set_settings({"email_daily_limit": 5000, "warmup_enabled": False, "email_send_delay_seconds": 0})
        with self.ses():
            with self.assertRaises(outreach.SendError) as cm:
                outreach.run_send_bulk(FakeCtx(), {})
        self.assertIn("bounced", str(cm.exception))
        self.assertEqual(FakeSMTP.sent, [])
        db.set_settings({"max_bounce_rate": 0})              # switched off: sends
        with self.ses():
            self.assertIn("sent", outreach.run_send_bulk(FakeCtx(), {"ids": ids[50:52]}))


class TestUnsubscribeAndLimits(ApiBase):
    def test_settings_accept_thousands_a_day(self):
        r = self.client.put("/api/settings", json={"email_daily_limit": 5000, "auto_pace": True, "max_bounce_rate": 3})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((r.get_json()["email_daily_limit"], r.get_json()["auto_pace"]), (5000, True))
        r = self.client.put("/api/settings", json={"email_daily_limit": 250000})
        self.assertEqual(r.get_json()["email_daily_limit"], 100000)          # capped

    def test_one_click_unsubscribe(self):
        lead_id, _ = db.upsert_lead({"business_name": "Acme", "email": "owner@acme.in"})
        db.update_lead(lead_id, {"message": "Subject: Hi\n\nHello"})
        sent = []
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "https://nexus.example"}), \
                mock.patch.object(outreach, "_deliver", side_effect=lambda m: sent.append(m)):
            self.client.post(f"/api/leads/{lead_id}/send-email", json={})
        msg = sent[0]
        self.assertIn("https://nexus.example/u/", msg["List-Unsubscribe"])
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        path = msg["List-Unsubscribe"].split("<")[1].split(">")[0].replace("https://nexus.example", "")
        anon = self.appmod.test_client()                     # a mail app: no session, no special headers
        self.assertEqual(anon.get(path).status_code, 200)
        self.assertEqual(db.get_lead(lead_id)["do_not_contact"], 0)      # opening the link changes nothing
        r = anon.post(path, data={"List-Unsubscribe": "One-Click"})
        self.assertEqual(r.status_code, 200)
        lead = db.get_lead(lead_id)
        self.assertEqual((lead["do_not_contact"], lead["stage"]), (1, "dead"))
        self.assertTrue(db.is_suppressed(lead))
        self.assertEqual(anon.post(path[:-3] + "xyz").status_code, 404)
        self.assertEqual(anon.get("/u/garbage").status_code, 404)

    def test_without_a_public_address_the_mailto_link_remains(self):
        lead_id, _ = db.upsert_lead({"business_name": "Acme", "email": "owner@acme.in"})
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "", "RENDER_EXTERNAL_URL": ""}):
            msg = outreach.build_email(db.get_lead(lead_id), "Hi", "Hello", db.get_settings())
        self.assertTrue(msg["List-Unsubscribe"].startswith("<mailto:"))
        self.assertIsNone(msg["List-Unsubscribe-Post"])


if __name__ == "__main__":
    unittest.main()
