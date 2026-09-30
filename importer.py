"""
importer.py -- get leads into the hub from any CSV.

Headers are matched automatically: first by exact name (business_name, Business
Name, full_address, location_link ...), then by simple keyword rules. Columns
that can't be mapped are appended to the lead's notes (short values only) and
reported back, so nothing is silently lost.
"""
import csv
import io
import re

import db
import schema

MAX_IMPORT_ROWS = 20000

# normalised header -> canonical field
EXACT = {
    # business
    "business_name": "business_name", "business": "business_name", "name": "business_name",
    "company": "business_name", "company_name": "business_name", "title": "business_name",
    "organization": "business_name",
    # person
    "owner_name": "contact_name", "owner": "contact_name", "contact_name": "contact_name",
    "contact": "contact_name", "contact_person": "contact_name", "manager": "contact_name",
    "full_name": "contact_name",
    # category / trade
    "trade": "trade", "category": "category_raw", "category_raw": "category_raw",
    "type": "category_raw", "industry": "category_raw", "main_category": "category_raw",
    "subtypes": "category_raw",
    # location
    "address": "address", "full_address": "address", "street_address": "address",
    "city": "city", "state": "state", "us_state": "state",
    # reviews
    "reviews": "review_count", "review_count": "review_count", "reviews_count": "review_count",
    "user_ratings_total": "review_count", "number_of_reviews": "review_count",
    # contact
    "phone": "phone", "phone_1": "phone", "phone_number": "phone", "telephone": "phone",
    "tel": "phone", "mobile": "phone", "contact_number": "phone", "whatsapp": "phone",
    "email": "email", "email_1": "email", "e_mail": "email", "email_address": "email",
    "website": "website", "site": "website", "url": "website", "website_url": "website",
    "domain": "website", "web": "website",
    # social / listings
    "instagram": "instagram_url", "instagram_url": "instagram_url", "ig": "instagram_url",
    "ig_handle": "instagram_url", "insta": "instagram_url",
    "facebook": "facebook_url", "facebook_url": "facebook_url", "fb": "facebook_url",
    "fb_page": "facebook_url",
    "linkedin": "linkedin_url", "linkedin_url": "linkedin_url",
    "yelp_url": "yelp_url", "yelp": "yelp_url", "yelp_link": "yelp_url",
    "google_business_url": "google_maps_url", "google_maps_url": "google_maps_url",
    "location_link": "google_maps_url", "maps_url": "google_maps_url",
    "google_url": "google_maps_url", "place_url": "google_maps_url",
    # misc
    "notes": "notes", "note": "notes", "description": "notes",
    "source": "source_label", "campaign": "campaign",
}

# fallbacks for messy headers (substring rules, checked in this order)
KEYWORDS = [
    ("email", "email"), ("phone", "phone"), ("instagram", "instagram_url"),
    ("facebook", "facebook_url"), ("linkedin", "linkedin_url"), ("yelp", "yelp_url"),
    ("website", "website"), ("address", "address"), ("categor", "category_raw"),
    ("review", "review_count"), ("business name", "business_name"),
]

# Pre-written outreach copy that may already sit in a spreadsheet. Imported as the
# lead's draft message when it matches the lead's channel.
MESSAGE_HEADERS = {
    "sms_whatsapp": "phone", "sms": "phone", "whatsapp_message": "phone", "text_message": "phone",
    "facebook_dm": "facebook", "facebook_message": "facebook",
    "instagram_dm": "instagram", "instagram_message": "instagram",
    "email_body": "email", "email_message": "email",
    "yelp_message": "yelp",
}
SUBJECT_HEADERS = ("email_subject", "subject")
# Words that mean a header holds message text, never contact data.
_TEXTY = ("subject", "body", "message", "dm", "template", "draft", "reply")

# Columns we know are handled/derived elsewhere -- don't dump into notes.
IGNORED = {"has_website", "id", "quality_score", "score", "status", "stage", "fit_confidence",
           "fit_reason", "confidence_notes", "score_reasons"}


def _norm_header(h):
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def map_headers(headers):
    """Return ({header: canonical_field}, [unmapped headers])."""
    mapping, used_targets, unmapped = {}, set(), []
    for h in headers:
        key = _norm_header(h)
        if key in MESSAGE_HEADERS or key in SUBJECT_HEADERS:
            continue  # handled separately as draft messages
        if key in IGNORED:
            unmapped.append(h)
            continue
        target = EXACT.get(key)
        if target is None and not any(w in key.split("_") for w in _TEXTY):
            for kw, tgt in KEYWORDS:
                if kw.replace(" ", "_") in key or kw in key:
                    target = tgt
                    break
        # first column wins for each target, except notes which can stack
        if target and (target not in used_targets or target == "notes"):
            mapping[h] = target
            used_targets.add(target)
        else:
            unmapped.append(h)
    return mapping, unmapped


def rows_from_csv_bytes(data):
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return reader.fieldnames or [], list(reader)


def import_rows(headers, rows, campaign="", source="import", query=""):
    """Import parsed CSV rows. Returns a summary dict."""
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"That file has {len(rows):,} rows; the limit is {MAX_IMPORT_ROWS:,} per import. Split it into smaller files.")
    mapping, unmapped = map_headers(headers)
    unmapped = [h for h in unmapped if _norm_header(h) not in MESSAGE_HEADERS
                and _norm_header(h) not in SUBJECT_HEADERS]
    msg_cols = {h: MESSAGE_HEADERS[_norm_header(h)] for h in headers if _norm_header(h) in MESSAGE_HEADERS}
    subj_col = next((h for h in headers if _norm_header(h) in SUBJECT_HEADERS), None)
    summary = {"inserted": 0, "merged": 0, "skipped_empty": 0, "skipped_suppressed": 0,
               "column_mapping": mapping,
               "unmapped_columns": [h for h in unmapped if _norm_header(h) not in IGNORED],
               "ids": []}
    for start in range(0, len(rows), 200):
        with db.batch():
            for row in rows[start:start + 200]:
                lead = {}
                row_campaign, row_source = campaign, source
                extra_notes = []
                for h, target in mapping.items():
                    v = (row.get(h) or "").strip()
                    if not v or v.lower() == "nan":
                        continue
                    if target == "campaign":
                        row_campaign = row_campaign or v
                    elif target == "source_label":
                        row_source = v
                    elif target == "notes":
                        extra_notes.append(v)
                    elif target in lead and lead[target]:
                        continue
                    else:
                        lead[target] = v
                for h in unmapped:
                    v = (row.get(h) or "").strip()
                    if v and v.lower() != "nan" and len(v) <= 200 and _norm_header(h) not in IGNORED:
                        extra_notes.append(f"{h}: {v}")
                lead["notes"] = "; ".join(extra_notes)

                # Nothing to go on -> skip
                has_contact = any(lead.get(k) for k in ("phone", "email", "website", "instagram_url",
                                                         "facebook_url", "yelp_url", "google_maps_url"))
                if not lead.get("business_name") and not has_contact:
                    summary["skipped_empty"] += 1
                    continue
                norm = schema.normalize_lead(lead)
                if db.is_suppressed(norm):
                    summary["skipped_suppressed"] += 1
                    continue
                try:
                    lead_id, is_new = db.upsert_lead(lead, campaign=row_campaign, source=row_source, query=query)
                except db.LimitError as e:
                    summary["stopped_at_limit"] = str(e)
                    break
                summary["inserted" if is_new else "merged"] += 1
                summary["ids"].append(lead_id)

                # carry over any ready-made message that matches this lead's channel
                if msg_cols:
                    saved = db.get_lead(lead_id)
                    if saved and not saved["message"]:
                        want = "phone" if saved["channel"] == "whatsapp" else saved["channel"]
                        for h, ch in msg_cols.items():
                            text = (row.get(h) or "").strip()
                            if text and ch == want:
                                if ch == "email" and subj_col and (row.get(subj_col) or "").strip():
                                    text = f"Subject: {row[subj_col].strip()}\n\n{text}"
                                db.update_lead(lead_id, {"message": text})
                                break
        if summary.get("stopped_at_limit"):
            break
    return summary


def import_leads(leads, campaign="", source="", query=""):
    """Import already-structured leads (list of canonical-field dicts), e.g.
    straight from a scraper. Same dedupe + do-not-contact rules as CSV import."""
    summary = {"inserted": 0, "merged": 0, "skipped_empty": 0, "skipped_suppressed": 0, "ids": []}
    for start in range(0, len(leads), 200):
        with db.batch():
            for lead in leads[start:start + 200]:
                has_contact = any(lead.get(k) for k in ("phone", "email", "website", "instagram_url",
                                                         "facebook_url", "yelp_url", "google_maps_url"))
                if not (lead.get("business_name") or has_contact):
                    summary["skipped_empty"] += 1
                    continue
                if db.is_suppressed(schema.normalize_lead(lead)):
                    summary["skipped_suppressed"] += 1
                    continue
                try:
                    lead_id, is_new = db.upsert_lead(lead, campaign=campaign, source=source, query=query)
                except db.LimitError as e:
                    summary["stopped_at_limit"] = str(e)
                    break
                summary["inserted" if is_new else "merged"] += 1
                summary["ids"].append(lead_id)
        if summary.get("stopped_at_limit"):
            break
    return summary


def import_csv_bytes(data, campaign="", source="import"):
    headers, rows = rows_from_csv_bytes(data)
    if not headers:
        raise ValueError("The file has no header row")
    return import_rows(headers, rows, campaign=campaign, source=source)
