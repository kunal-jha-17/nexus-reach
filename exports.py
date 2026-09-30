"""
exports.py -- take your data with you.

  user_export_zip()   one person's leads, campaigns, settings, do-not-contact list and
                      activity as ordinary CSV/JSON files (never includes passwords or API keys)
  full_backup_zip()   admin only: every database file on the server, exactly as stored
                      (contains password hashes and ENCRYPTED saved keys -- keep it private)
Run inside a signed-in user's context (user_export_zip) or anywhere (full_backup_zip).
"""
import csv
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import schema


def _csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue().encode("utf-8")


def user_export_zip():
    leads, _ = db.list_leads({}, sort="oldest", limit=10_000_000)
    lead_rows = []
    for l in leads:
        l = dict(l)
        l["fit"] = {1: "yes", 0: "no"}.get(l["fit"], "")
        lead_rows.append([l.get(c) for c in schema.EXPORT_COLUMNS])
    with db.get_conn() as conn:
        events = [tuple(r) for r in conn.execute(
            "SELECT id, lead_id, kind, detail, created_at FROM events ORDER BY id").fetchall()]
        suppression = [tuple(r) for r in conn.execute(
            "SELECT value, kind, reason, added_at FROM suppression ORDER BY added_at").fetchall()]
    settings = db.get_settings()
    readme = (
        "Your Nexus Reach data, exported "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
        "leads.csv         every lead with all its fields (re-importable via Find leads > Import)\n"
        "campaigns.json    your campaigns, with their criteria, templates and settings overrides\n"
        "settings.json     your account-wide settings (templates, limits, channel order)\n"
        "suppression.csv   your do-not-contact list\n"
        "events.csv        the activity log (sent, replied, stage changes ...)\n\n"
        "Passwords and API keys are never included.\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("leads.csv", _csv_bytes(schema.EXPORT_COLUMNS, lead_rows))
        z.writestr("campaigns.json", json.dumps(db.list_campaigns(), indent=2, default=str))
        z.writestr("settings.json", json.dumps(settings, indent=2, default=str))
        z.writestr("suppression.csv", _csv_bytes(["value", "kind", "reason", "added_at"], suppression))
        z.writestr("events.csv", _csv_bytes(["id", "lead_id", "kind", "detail", "created_at"], events))
    return buf.getvalue()


def _sqlite_copy(path):
    """A consistent copy of a database file that may be in use."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src, dst = sqlite3.connect(str(path)), sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return Path(tmp).read_bytes()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def full_backup_zip():
    data = config.data_dir()
    files = ["accounts.db"] + [f"users/{p.name}" for p in sorted((data / "users").glob("*.db"))]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt",
                   "Full Nexus Reach backup: the database files exactly as stored on the server.\n"
                   "Contains password hashes and ENCRYPTED saved keys (readable only with SECRET_KEY).\n"
                   "To restore: stop the app, unzip into the data folder, start the app.\n")
        for rel in files:
            p = data / rel
            if p.exists():
                z.writestr(rel, _sqlite_copy(p))
    return buf.getvalue()
