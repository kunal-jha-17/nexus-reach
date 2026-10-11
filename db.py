"""
db.py -- the single SQLite database behind the whole app (hub.db).

One `leads` table is the source of truth for every stage: scraped, enriched,
judged, drafted, sent, replied. Everything else (jobs, schedules, suppression
list, campaigns, events, settings) sits alongside it.
"""
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import config
import directory
import persistence
import schema
import userctx

_DB_PATH = None


def set_path(p):
    """Point the app at a different database file (used by tests)."""
    global _DB_PATH
    _DB_PATH = p


def path():
    """The database for the current user (see userctx); otherwise the test/legacy file."""
    return userctx.db_path() or _DB_PATH or config.db_path()


class LimitError(ValueError):
    """A per-account limit (see MAX_LEADS_PER_USER) was reached."""


@contextmanager
def get_conn():
    shared = getattr(_batch, "conn", None)
    if shared is not None:              # inside batch(): reuse the one open transaction
        yield shared
        return
    p = path()
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    before = conn.total_changes
    try:
        yield conn
        conn.commit()
        if conn.total_changes != before:          # something was written -> copy it to outside storage soon
            persistence.mark_dirty(p)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_batch = threading.local()


@contextmanager
def batch():
    """Run many db.* calls as ONE transaction on one connection (one disk sync instead of
    one per call). Makes imports and bulk edits many times faster, especially on the slow
    disks of free hosts. Everything inside commits together when the block ends."""
    if getattr(_batch, "conn", None) is not None:
        yield                           # already inside a batch
        return
    with get_conn() as conn:
        _batch.conn = conn
        try:
            yield
        finally:
            _batch.conn = None


def bulk_set(ids, column, value):
    """Set one simple column (stage / do_not_contact) on many leads with a single statement per chunk."""
    if column not in ("stage", "do_not_contact"):
        raise ValueError("Unsupported bulk column")
    if column == "stage" and value not in schema.STAGES:
        raise ValueError(f"invalid stage '{value}'")
    value = schema.to_bool_int(value) if column == "do_not_contact" else value
    now, n = schema.now_iso(), 0
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = list(ids[i:i + 500])
            n += conn.execute(
                f"UPDATE leads SET {column} = ?, updated_at = ? WHERE id IN ({','.join('?' for _ in chunk)})",
                (value, now, *chunk)).rowcount
    return n


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign TEXT DEFAULT '',
    source TEXT DEFAULT '',
    business_name TEXT DEFAULT '',
    contact_name TEXT DEFAULT '',
    trade TEXT DEFAULT '',
    category_raw TEXT DEFAULT '',
    address TEXT DEFAULT '',
    city TEXT DEFAULT '',
    state TEXT DEFAULT '',
    review_count INTEGER DEFAULT 0,
    phone TEXT DEFAULT '',
    phone_raw TEXT DEFAULT '',
    email TEXT DEFAULT '',
    website TEXT DEFAULT '',
    has_website INTEGER DEFAULT 0,
    instagram_url TEXT DEFAULT '',
    facebook_url TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',
    yelp_url TEXT DEFAULT '',
    google_maps_url TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    score INTEGER DEFAULT 0,
    score_reasons TEXT DEFAULT '',
    fit INTEGER,                         -- NULL = not judged yet, 1 = fits, 0 = rejected
    fit_confidence TEXT DEFAULT '',
    fit_reason TEXT DEFAULT '',
    enrichment_notes TEXT DEFAULT '',
    enriched_at TEXT,
    judged_at TEXT,
    channel TEXT DEFAULT 'unknown',
    channel_locked INTEGER DEFAULT 0,    -- 1 = channel was chosen by hand
    stage TEXT DEFAULT 'new',
    send_status TEXT DEFAULT 'unsent',
    message TEXT DEFAULT '',
    template_used TEXT DEFAULT '',
    sent_at TEXT,
    last_sent_at TEXT,
    follow_up_count INTEGER DEFAULT 0,
    email_message_id TEXT DEFAULT '',
    do_not_contact INTEGER DEFAULT 0,
    dnc_reason TEXT DEFAULT '',
    website_domain TEXT DEFAULT '',
    name_key TEXT DEFAULT '',
    last_query TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(website_domain);
CREATE INDEX IF NOT EXISTS idx_leads_namekey ON leads(name_key);
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    kind TEXT,
    detail TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    params TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',        -- queued / running / done / error / stopped
    progress INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    summary TEXT DEFAULT '',
    log TEXT DEFAULT '',
    created_at TEXT,
    finished_at TEXT,
    campaign TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT,
    query TEXT,
    location TEXT DEFAULT '',
    max_results INTEGER DEFAULT 30,
    campaign TEXT DEFAULT '',
    interval_hours INTEGER DEFAULT 24,
    last_run_at TEXT,
    next_run_at TEXT,
    active INTEGER DEFAULT 1,
    only_no_website INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suppression (
    value TEXT PRIMARY KEY,              -- lowercased email / E.164 phone / profile URL
    kind TEXT,
    reason TEXT DEFAULT '',
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    name TEXT PRIMARY KEY,
    criteria TEXT DEFAULT '',
    created_at TEXT,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'active',        -- active / archived
    business_info TEXT DEFAULT '',       -- overrides Settings > About you when not blank
    templates TEXT DEFAULT '',           -- JSON list; overrides global templates when set
    channel_priority TEXT DEFAULT ''     -- JSON list; overrides global channel order when set
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    month TEXT,
    key TEXT,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (month, key)
);
"""


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn):
    """Bring a database created by an older version up to date."""
    for table, cols in {
        "campaigns": {"description": "TEXT DEFAULT ''", "status": "TEXT DEFAULT 'active'",
                      "business_info": "TEXT DEFAULT ''", "templates": "TEXT DEFAULT ''",
                      "channel_priority": "TEXT DEFAULT ''"},
        "jobs": {"campaign": "TEXT DEFAULT ''"},
        "schedules": {"only_no_website": "INTEGER DEFAULT 0"},
    }.items():
        have = _columns(conn, table)
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # The old "default" criteria row becomes a plain setting (it isn't a campaign).
    row = conn.execute("SELECT criteria FROM campaigns WHERE name = 'default'").fetchone()
    if row is not None:
        if (row["criteria"] or "").strip():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('default_criteria', ?)",
                         (json.dumps(row["criteria"]),))
        conn.execute("DELETE FROM campaigns WHERE name = 'default'")
    # Every campaign tag already on a lead becomes a real campaign.
    now = schema.now_iso()
    for r in conn.execute("SELECT DISTINCT campaign FROM leads WHERE campaign != ''").fetchall():
        conn.execute("INSERT OR IGNORE INTO campaigns(name, created_at) VALUES(?, ?)", (r["campaign"], now))
    conn.execute("UPDATE campaigns SET status = 'active' WHERE status IS NULL OR status = ''")


def init_db():
    with get_conn() as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(SCHEMA)
        _migrate(conn)
        # A job can't survive a restart -- don't leave it showing as running.
        conn.execute(
            "UPDATE jobs SET status='error', summary='Interrupted by a restart', finished_at=? "
            "WHERE status IN ('queued','running')",
            (schema.now_iso(),),
        )
        # Run history is for looking back a little, not forever: keep the newest 300.
        conn.execute("DELETE FROM jobs WHERE id <= (SELECT MAX(id) FROM jobs) - 300")
    try:
        repair_missing_names()      # names an older import left in the notes (e.g. "Clinic Name: ...")
    except Exception:  # noqa: BLE001 -- a tidy-up must never stop the app from opening a database
        pass


# ------------------------------------------------------------------ settings

DEFAULT_SETTINGS = {
    "business_info": "",
    "templates": [],   # [{"name":..., "channel":..., "body":...}]
    "channel_priority": schema.DEFAULT_CHANNEL_PRIORITY,
    "email_daily_limit": config.env_int("EMAIL_DAILY_LIMIT", 40),
    "email_send_delay_seconds": config.env_int("EMAIL_SEND_DELAY_SECONDS", 45),
    "warmup_enabled": config.env_bool("EMAIL_WARMUP_ENABLED", True),
    "followup_delay_days": config.env_int("FOLLOWUP_DELAY_DAYS", 3),
    "max_followups": config.env_int("MAX_FOLLOWUPS", 2),
    "email_footer": "If you'd rather not hear from me, just reply STOP and I won't contact you again.",
    "sending_started_at": "",
    "default_criteria": "",   # fit criteria for leads whose campaign has none of its own
    "auto_remove_rejected": False,   # delete a lead outright the moment Judge fit rejects it
    "exact_templates": False,        # draft by filling in the template's blanks, with no AI rewriting
    "followup_template": "",         # fixed follow-up text (placeholders allowed); blank = the AI writes one
    "reply_check_minutes": 30,       # look for replies on a timer when the inbox is connected; 0 = only by hand
    "last_reply_check_at": "",
    "imap_state": {},                # where the last inbox check got to
    "timezone": "",                  # e.g. America/Chicago; blank = UTC. Used for "today" and sending hours
    "send_window_enabled": False,    # bulk sends only inside the hours below
    "send_days": [0, 1, 2, 3, 4],    # Monday = 0
    "send_start_hour": 8,
    "send_end_hour": 18,
}


def get_settings(conn=None):
    def _load(c):
        out = json.loads(json.dumps(DEFAULT_SETTINGS))
        for r in c.execute("SELECT key, value FROM settings").fetchall():
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                pass
        return out
    if conn is not None:
        return _load(conn)
    with get_conn() as c:
        return _load(c)


def get_setting(key):
    return get_settings().get(key)


def set_settings(values):
    with get_conn() as conn:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)),
                )
    return get_settings()


# --------------------------------------------------------------------- leads

LIFECYCLE_WRITABLE = {
    "campaign", "source", "stage", "send_status", "message", "template_used",
    "sent_at", "last_sent_at", "follow_up_count", "email_message_id",
    "do_not_contact", "dnc_reason", "fit", "fit_confidence", "fit_reason",
    "enrichment_notes", "enriched_at", "judged_at", "last_query",
}


def _d(row):
    return dict(row) if row is not None else None


def get_lead(lead_id):
    with get_conn() as conn:
        return _d(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())


def _priority(conn, campaign=""):
    """Channel order for a lead: its campaign's own order if set, else the global one."""
    p = None
    if campaign:
        r = conn.execute("SELECT channel_priority FROM campaigns WHERE name = ?", (campaign,)).fetchone()
        if r and r["channel_priority"]:
            try:
                p = json.loads(r["channel_priority"])
            except ValueError:
                p = None
    if not p:
        p = get_settings(conn).get("channel_priority") or schema.DEFAULT_CHANNEL_PRIORITY
    return [x for x in p if x in schema.CHANNELS] or schema.DEFAULT_CHANNEL_PRIORITY


def canonical_campaign(conn, name):
    """Campaign names are case-insensitive: 'DFW' and 'dfw' are the same campaign."""
    name = (name or "").strip()
    if not name:
        return ""
    r = conn.execute("SELECT name FROM campaigns WHERE lower(name) = lower(?)", (name,)).fetchone()
    return r["name"] if r else name


def ensure_campaign(conn, name):
    if name:
        conn.execute("INSERT OR IGNORE INTO campaigns(name, created_at) VALUES(?, ?)",
                     (name, schema.now_iso()))


def find_existing(conn, n, exclude_id=None):
    """Find a lead that is probably the same business. Checks, in order: phone,
    email, real-website domain, social/listing URLs, then business name (+city)."""
    excl = " AND id != ?" if exclude_id else ""
    xp = (exclude_id,) if exclude_id else ()
    for col in ("phone", "email", "website_domain", "instagram_url",
                "facebook_url", "yelp_url", "google_maps_url"):
        val = n.get(col)
        if val:
            row = conn.execute(f"SELECT * FROM leads WHERE {col} = ?{excl} LIMIT 1", (val, *xp)).fetchone()
            if row:
                return row
    key = n.get("name_key")
    if key:
        name_part, _, city_part = key.partition("|")
        for r in conn.execute(f"SELECT * FROM leads WHERE name_key LIKE ?{excl}", (name_part + "|%", *xp)).fetchall():
            row_city = (r["name_key"] or "").partition("|")[2]
            if not city_part or not row_city or city_part == row_city:
                return r
    return None


def _write_data(conn, lead_id, merged, extra=None):
    """Normalise a merged lead dict and write all data columns, plus the
    derived channel and score. `extra` = additional column values to set."""
    n = schema.normalize_lead(merged)
    score, reasons = schema.score_lead(n)
    cols = {c: n[c] for c in schema.DATA_COLUMNS}
    cols["score"], cols["score_reasons"] = score, reasons
    if not merged.get("channel_locked"):
        cols["channel"] = schema.derive_channel(n, _priority(conn, merged.get("campaign") or ""))
    if extra:
        cols.update(extra)
    cols["updated_at"] = schema.now_iso()
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", (*cols.values(), lead_id))


def upsert_lead(raw, campaign="", source="", query=""):
    """Insert a lead, or merge into an existing one (blank fields only -- a
    value you already have is never overwritten). Returns (id, is_new)."""
    n = schema.normalize_lead(raw)
    with get_conn() as conn:
        existing = find_existing(conn, n)
        if existing:
            merged = dict(existing)
            changed = False
            for k in schema.DATA_COLUMNS:
                if k in ("has_website", "website_domain", "name_key"):
                    continue
                cur, new = merged.get(k), n.get(k)
                if k == "review_count":
                    if new and new > (cur or 0):
                        merged[k], changed = new, True
                elif cur in ("", None) and new not in ("", None):
                    merged[k], changed = new, True
            if changed:
                _write_data(conn, existing["id"], merged)
            return existing["id"], False

        cap = config.env_int("MAX_LEADS_PER_USER", 5000)          # 0 = unlimited
        if cap and conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] >= cap:
            raise LimitError(f"You've reached this server's limit of {cap} leads per account. Export a CSV, then delete "
                             "leads you no longer need (or ask the admin to raise MAX_LEADS_PER_USER).")
        now = schema.now_iso()
        campaign = canonical_campaign(conn, campaign)
        ensure_campaign(conn, campaign)
        score, reasons = schema.score_lead(n)
        row = {c: n[c] for c in schema.DATA_COLUMNS}
        row.update({
            "campaign": campaign, "source": source or "", "last_query": query or "",
            "score": score, "score_reasons": reasons,
            "channel": schema.derive_channel(n, _priority(conn, campaign)),
            "created_at": now, "updated_at": now,
        })
        cur = conn.execute(
            f"INSERT INTO leads ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        lead_id = cur.lastrowid
        conn.execute(
            "INSERT INTO events(lead_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (lead_id, "created", source or "manual", now),
        )
        return lead_id, True


def update_lead(lead_id, fields):
    """Apply changes to a lead. Data fields are re-normalised (so a phone typed
    as '(214) 555-0100' is stored as +12145550100); channel and score are
    recomputed. Raises ValueError for invalid stage/status/channel values."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return None
        merged = dict(row)
        extra = {}
        f = dict(fields)

        if "stage" in f and f["stage"] not in schema.STAGES:
            raise ValueError(f"invalid stage '{f['stage']}'")
        if "send_status" in f and f["send_status"] not in schema.SEND_STATUSES:
            raise ValueError(f"invalid send_status '{f['send_status']}'")
        if "phone" in f:
            merged["phone_raw"] = f["phone"]
        if "channel" in f:
            ch = f.pop("channel")
            if ch in ("", None, "auto"):
                merged["channel_locked"] = 0
                extra["channel_locked"] = 0
            elif ch in schema.CHANNELS:
                merged["channel_locked"] = 1
                extra.update({"channel_locked": 1, "channel": ch})
            else:
                raise ValueError(f"invalid channel '{ch}'")
        for k, v in f.items():
            if k == "campaign":
                v = canonical_campaign(conn, v)
                ensure_campaign(conn, v)
                merged["campaign"] = v
                extra["campaign"] = v
            elif k in schema.DATA_COLUMNS:
                merged[k] = v
            elif k in LIFECYCLE_WRITABLE:
                if k == "do_not_contact":
                    v = schema.to_bool_int(v)
                if k == "fit" and v not in (None, ""):
                    v = 1 if str(v).lower() in ("1", "true", "yes") else 0
                if k == "fit" and v == "":
                    v = None
                extra[k] = v
        _write_data(conn, lead_id, merged, extra)
        return _d(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())


def delete_leads(ids):
    if not ids:
        return 0
    with get_conn() as conn:
        q = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM events WHERE lead_id IN ({q})", tuple(ids))
        return conn.execute(f"DELETE FROM leads WHERE id IN ({q})", tuple(ids)).rowcount


def recompute_channels(campaign=None):
    """Re-derive the channel for every lead that isn't hand-locked (after a
    channel-order setting changes). campaign=None means all campaigns."""
    with get_conn() as conn:
        cache = {}
        sql = "SELECT * FROM leads WHERE channel_locked = 0"
        params = ()
        if campaign is not None:
            sql += " AND campaign = ?"
            params = (campaign,)
        for r in conn.execute(sql, params).fetchall():
            camp = r["campaign"] or ""
            if camp not in cache:
                cache[camp] = _priority(conn, camp)
            ch = schema.derive_channel(dict(r), cache[camp])
            if ch != r["channel"]:
                conn.execute("UPDATE leads SET channel = ? WHERE id = ?", (ch, r["id"]))


_SORTS = {
    "score": "score DESC, id DESC",
    "newest": "id DESC",
    "oldest": "id ASC",
    "name": "business_name COLLATE NOCASE ASC",
}


def _where(f):
    clauses, params = [], []
    f = f or {}
    if f.get("ids"):
        clauses.append(f"id IN ({','.join('?' for _ in f['ids'])})")
        params += list(f["ids"])
    if f.get("q"):
        q = str(f["q"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q}%"
        clauses.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in (
            "business_name", "email", "phone", "phone_raw", "city", "notes", "contact_name")) + ")")
        params += [like] * 7
    if f.get("campaign") == "__none__":          # leads that aren't in any campaign
        clauses.append("campaign = ''")
    for col in ("campaign", "stage", "channel", "source", "trade", "send_status"):
        if f.get(col) and not (col == "campaign" and f[col] == "__none__"):
            clauses.append(f"{col} = ?")
            params.append(f[col])
    fit = f.get("fit")
    if fit in ("1", "0", 1, 0):
        clauses.append("fit = ?")
        params.append(int(fit))
    elif fit in ("null", "unjudged"):
        clauses.append("fit IS NULL")
    if f.get("not_rejected"):
        clauses.append("(fit IS NULL OR fit = 1)")
    if f.get("enriched") == "yes":
        clauses.append("enriched_at IS NOT NULL")
    elif f.get("enriched") == "no":
        clauses.append("enriched_at IS NULL")
    if f.get("drafted") == "yes":
        clauses.append("message != ''")
    elif f.get("drafted") == "no":
        clauses.append("message = ''")
    if f.get("min_score") not in (None, ""):
        clauses.append("score >= ?")
        params.append(int(f["min_score"]))
    if f.get("dnc") == "no":
        clauses.append("do_not_contact = 0")
    has = f.get("has")
    if has == "email":
        clauses.append("email != ''")
    elif has == "phone":
        clauses.append("(phone != '' OR phone_raw != '')")
    elif has == "no_email":
        clauses.append("email = ''")
    elif has == "website":
        clauses.append("has_website = 1")
    elif has == "no_website":
        clauses.append("has_website = 0")
    if f.get("sent") == "drafted":                # written, not sent yet
        clauses.append("message != '' AND send_status = 'unsent'")
    elif f.get("sent") == "none":                 # nothing written
        clauses.append("message = '' AND send_status = 'unsent'")
    elif f.get("sent") in ("sent", "failed"):
        clauses.append("send_status = ?")
        params.append(f["sent"])
    if f.get("hand"):                             # reachable, but not by email: sent by a person
        clauses.append("channel NOT IN ('email', 'unknown')")
    if f.get("open_stage"):
        clauses.append("stage IN ('new', 'contacted')")
    if f.get("reachable"):
        clauses.append("channel != 'unknown'")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_leads(filters=None, sort="score", limit=50, offset=0):
    where, params = _where(filters)
    order = _SORTS.get(sort, _SORTS["score"])
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM leads{where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM leads{where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def select_ids(filters=None, limit=None):
    where, params = _where(filters)
    sql = f"SELECT id FROM leads{where} ORDER BY score DESC, id ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with get_conn() as conn:
        return [r["id"] for r in conn.execute(sql, params).fetchall()]


def repair_missing_names():
    """Leads imported before name columns like "Clinic Name" were recognised have a blank
    business name, with the real one parked in their notes as "Clinic Name: Acme Dental".
    Move it back where it belongs. Safe to run any number of times. Returns how many were fixed."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, notes FROM leads WHERE business_name = '' AND notes LIKE '%:%'").fetchall()
    fixed = 0
    for r in rows:
        parts = [p for p in (r["notes"] or "").split("; ")]
        for i, part in enumerate(parts):
            header, sep, value = part.partition(": ")
            key = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
            if sep and value.strip() and len(header) <= 60 and schema.name_header_target(key) == "business_name":
                update_lead(r["id"], {"business_name": value.strip(),
                                      "notes": "; ".join(parts[:i] + parts[i + 1:])})
                fixed += 1
                break
    return fixed


def renormalize_phones():
    """Re-read every phone number that couldn't be understood before (for instance after the
    default country code changes). Returns how many now have a usable number."""
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM leads WHERE phone = '' AND phone_raw != ''").fetchall()]
    fixed = 0
    with batch():
        for i in ids:
            lead = update_lead(i, {})
            fixed += bool(lead and lead["phone"])
    return fixed


def replies_waiting(campaign="", limit=25):
    """Leads who answered and haven't been dealt with yet (stage is still 'replied'),
    newest reply first, each with the opening of what they wrote."""
    where, params = "l.stage = 'replied' AND l.do_not_contact = 0", []
    if campaign:
        where += " AND l.campaign = ?"
        params.append(campaign)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT l.id, l.business_name, l.campaign, l.channel, l.email, l.phone, l.updated_at,
                       (SELECT e.detail FROM events e WHERE e.lead_id = l.id AND e.kind = 'reply'
                        ORDER BY e.id DESC LIMIT 1) AS reply,
                       (SELECT e.created_at FROM events e WHERE e.lead_id = l.id AND e.kind IN ('reply', 'stage')
                        ORDER BY e.id DESC LIMIT 1) AS replied_at
                FROM leads l WHERE {where} ORDER BY replied_at DESC, l.id DESC LIMIT ?""", (*params, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["snippet"] = (json.loads(d.pop("reply") or "{}") or {}).get("snippet", "")
        except (ValueError, AttributeError):
            d["snippet"] = ""
        out.append(d)
    return out


def listing_urls():
    """Every Google Maps / Yelp listing URL already stored, so a scrape can skip
    listings it has saved before instead of opening them again."""
    with get_conn() as conn:
        rows = conn.execute("SELECT google_maps_url g, yelp_url y FROM leads "
                            "WHERE google_maps_url != '' OR yelp_url != ''").fetchall()
    return [u for r in rows for u in (r["g"], r["y"]) if u]


SUGGEST_FIELDS = ("scrape_query", "scrape_location", "lead_search")


def suggest(field, q, limit=8, context=""):
    """Autocomplete with no outside service. Search terms and places come first from this
    account's own past scrapes and leads, then from the built-in directory (business types;
    ~57,000 places). The All leads box suggests names / cities / trades of leads already here."""
    if field not in SUGGEST_FIELDS:
        raise ValueError("Unknown suggestion field")
    q = re.sub(r"\s+", " ", (q or "")).strip()
    if len(q) < 3:
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    limit = max(1, min(int(limit or 8), 20))
    cands = []
    with get_conn() as conn:
        if field in ("scrape_query", "scrape_location"):
            key = "query" if field == "scrape_query" else "location"
            for r in conn.execute("SELECT params FROM jobs WHERE kind = 'scrape' ORDER BY id DESC LIMIT 300"):
                try:
                    p = json.loads(r["params"] or "{}") or {}
                except ValueError:
                    continue
                if isinstance(p, dict):       # a run can hold several terms / places: "a; b; c"
                    cands += re.split(r"[;\n]+", str(p.get(key) or ""))
            for r in conn.execute(f"SELECT {key} v FROM schedules ORDER BY id DESC LIMIT 100"):
                cands += re.split(r"[;\n]+", str(r["v"] or ""))
            if field == "scrape_query":
                cands += [r["v"] for r in conn.execute(
                    "SELECT last_query v, COUNT(*) c FROM leads WHERE last_query LIKE ? ESCAPE '\\' "
                    "GROUP BY last_query ORDER BY c DESC LIMIT 20", (like,))]
            else:
                cands += [f"{r['city']}, {r['state']}" if r["state"] else r["city"] for r in conn.execute(
                    "SELECT city, state, COUNT(*) c FROM leads WHERE city LIKE ? ESCAPE '\\' "
                    "GROUP BY city, state ORDER BY c DESC LIMIT 20", (like,))]
        else:
            for col in ("business_name", "city", "contact_name", "trade"):
                cands += [r["v"] for r in conn.execute(
                    f"SELECT {col} v, COUNT(*) c FROM leads WHERE {col} LIKE ? ESCAPE '\\' "
                    f"GROUP BY {col} ORDER BY c DESC, {col} LIMIT ?", (like, limit))]
    ql, seen, starts, contains = q.lower(), set(), [], []
    for c in cands:
        c = re.sub(r"\s+", " ", str(c or "")).strip()
        cl = c.lower()
        if not c or cl in seen or ql not in cl or cl == ql:
            continue
        seen.add(cl)
        (starts if cl.startswith(ql) else contains).append(c[:120])
    out = starts + contains
    if field != "lead_search" and len(out) < limit:
        if field == "scrape_query":
            extra = directory.services(q, limit * 2)
        else:
            # places this account already works in (", TX" / ", India") are listed first
            # ...and so are places like the ones already typed into the box (`context`)
            typed = re.split(r"[;\n]+", str(context or ""))[:-1]
            prefer = {c.rsplit(", ", 1)[-1] for c in list(map(str, cands)) + typed if ", " in c}
            extra = directory.places(q, limit * 2, prefer={p.strip() for p in prefer})
        for c in extra:
            if c.lower() not in seen and c.lower() != ql:
                seen.add(c.lower())
                out.append(c)
    return out[:limit]


# -------------------------------------------------------------------- events

def add_event(lead_id, kind, detail=""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events(lead_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (lead_id, kind, detail if isinstance(detail, str) else json.dumps(detail), schema.now_iso()),
        )


def list_events(lead_id, limit=50):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE lead_id = ? ORDER BY id DESC LIMIT ?", (lead_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def event_exists(kind, needle):
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE kind = ? AND detail LIKE ? LIMIT 1", (kind, f"%{needle}%")
        ).fetchone() is not None


def local_zone(settings=None):
    """The account's time zone (Settings > Sending), UTC when none is set or it isn't recognised."""
    name = ((settings if settings is not None else get_settings()).get("timezone") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 -- unknown name, or no time-zone data on this machine
            pass
    return timezone.utc


def count_events_today(kinds, settings=None):
    """How many of these events happened today -- "today" being the account's own calendar day."""
    now = datetime.now(local_zone(settings))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    q = ",".join("?" for _ in kinds)
    with get_conn() as conn:
        return conn.execute(
            f"SELECT COUNT(*) c FROM events WHERE kind IN ({q}) AND created_at >= ?",
            (*kinds, start.isoformat(timespec="seconds")),
        ).fetchone()["c"]


# --------------------------------------------------------------------- stats

def stats(campaign=""):
    where = "WHERE campaign = ?" if campaign else ""
    params = (campaign,) if campaign else ()
    with get_conn() as conn:
        def grp(col):
            return {r[0]: r[1] for r in conn.execute(
                f"SELECT {col}, COUNT(*) FROM leads {where} GROUP BY {col}", params).fetchall()}

        def one(extra):
            w = f"{where} AND {extra}" if where else f"WHERE {extra}"
            return conn.execute(f"SELECT COUNT(*) FROM leads {w}", params).fetchone()[0]

        total = conn.execute(f"SELECT COUNT(*) FROM leads {where}", params).fetchone()[0]
        return {
            "total": total,
            "by_stage": grp("stage"),
            "by_channel": grp("channel"),
            "by_send_status": grp("send_status"),
            "fit": {"yes": one("fit = 1"), "no": one("fit = 0"), "unjudged": one("fit IS NULL")},
            "enriched": one("enriched_at IS NOT NULL"),
            "drafted": one("message != ''"),
            "dnc": one("do_not_contact = 1"),
        }


# ----------------------------------------------------------------- campaigns
# A campaign is a named push at one audience (e.g. "dallas-hvac"): its own leads,
# its own dashboard, and optionally its own fit criteria, "about you" text,
# message templates and channel order. Anything a campaign doesn't set falls back
# to the account-wide Settings.

CAMPAIGN_FIELDS = ("description", "status", "criteria", "business_info", "templates", "channel_priority")


def _campaign_row(r):
    d = dict(r)
    for k in ("templates", "channel_priority"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except ValueError:
            d[k] = None
    return d


def create_campaign(name, description=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the campaign a name")
    if len(name) > 60 or "/" in name or "\\" in name:
        raise ValueError("Campaign names can be up to 60 characters and can't contain / or \\")
    if name.lower() == "default":
        raise ValueError("'default' is reserved -- pick another name")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM campaigns WHERE lower(name) = lower(?)", (name,)).fetchone():
            raise ValueError(f"A campaign called '{name}' already exists")
        conn.execute("INSERT INTO campaigns(name, description, created_at) VALUES(?,?,?)",
                     (name, (description or "").strip(), schema.now_iso()))
    return name


def get_campaign(name):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM campaigns WHERE name = ?", (canonical_campaign(conn, name),)).fetchone()
    return _campaign_row(r) if r else None


def list_campaigns():
    """All campaigns with the headline numbers shown on their cards."""
    with get_conn() as conn:
        stats = {r["campaign"]: dict(r) for r in conn.execute(
            """SELECT campaign, COUNT(*) AS leads,
                      SUM(CASE WHEN fit = 1 THEN 1 ELSE 0 END) AS fit_yes,
                      SUM(CASE WHEN send_status = 'sent' THEN 1 ELSE 0 END) AS contacted,
                      SUM(CASE WHEN stage IN ('replied','qualified','won') THEN 1 ELSE 0 END) AS replied,
                      SUM(CASE WHEN stage = 'won' THEN 1 ELSE 0 END) AS won
               FROM leads WHERE campaign != '' GROUP BY campaign""").fetchall()}
        rows = conn.execute("SELECT * FROM campaigns ORDER BY (status = 'archived'), lower(name)").fetchall()
    out = []
    for r in rows:
        d = _campaign_row(r)
        st = stats.get(d["name"], {})
        d.update({"leads": st.get("leads", 0), "fit_yes": st.get("fit_yes") or 0,
                  "contacted": st.get("contacted") or 0, "replied": st.get("replied") or 0,
                  "won": st.get("won") or 0,
                  "has_overrides": bool(d["business_info"] or d["templates"] or d["channel_priority"])})
        out.append(d)
    return out


def count_unassigned():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM leads WHERE campaign = ''").fetchone()[0]


def save_campaign(name, fields):
    """Update a campaign's description, status, criteria and its settings overrides.
    templates / channel_priority: a list sets the override, None/[] clears it."""
    with get_conn() as conn:
        name = canonical_campaign(conn, name)
        if not conn.execute("SELECT 1 FROM campaigns WHERE name = ?", (name,)).fetchone():
            raise ValueError("Campaign not found")
        sets, vals = [], []
        for k, v in fields.items():
            if k not in CAMPAIGN_FIELDS:
                continue
            if k == "status" and v not in ("active", "archived"):
                raise ValueError("status must be active or archived")
            if k == "channel_priority":
                v = [c for c in (v or []) if c in schema.CHANNELS and c != "unknown"]
                v = json.dumps(v) if v else ""
            elif k == "templates":
                v = [t for t in (v or []) if (t.get("name") or "").strip() and (t.get("body") or "").strip()
                     and t.get("channel") in schema.CHANNELS]
                v = json.dumps([{"name": t["name"].strip(), "channel": t["channel"], "body": t["body"].strip()}
                                for t in v]) if v else ""
            else:
                v = (v or "").strip() if isinstance(v, str) or v is None else v
            sets.append(f"{k} = ?")
            vals.append(v)
        if sets:
            conn.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE name = ?", (*vals, name))
    if "channel_priority" in fields:
        recompute_channels(name)
    return get_campaign(name)


def set_campaign_criteria(name, criteria):
    """Save fit criteria for a campaign ('default' = the account-wide fallback)."""
    if (name or "").strip().lower() == "default":
        set_settings({"default_criteria": criteria})
        return
    with get_conn() as conn:
        canon = canonical_campaign(conn, name)
        ensure_campaign(conn, canon)
    save_campaign(canon, {"criteria": criteria})


def get_campaign_criteria(name):
    with get_conn() as conn:
        if name:
            r = conn.execute("SELECT criteria FROM campaigns WHERE name = ?",
                             (canonical_campaign(conn, name),)).fetchone()
            if r and (r["criteria"] or "").strip():
                return r["criteria"]
        return get_settings(conn).get("default_criteria", "") or ""


def delete_campaign(name, delete_leads=False):
    """Remove a campaign. Its leads are kept (un-tagged) unless delete_leads."""
    with get_conn() as conn:
        name = canonical_campaign(conn, name)
        ids = [r["id"] for r in conn.execute("SELECT id FROM leads WHERE campaign = ?", (name,)).fetchall()]
        if delete_leads and ids:
            q = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM events WHERE lead_id IN ({q})", ids)
            conn.execute(f"DELETE FROM leads WHERE id IN ({q})", ids)
        else:
            conn.execute("UPDATE leads SET campaign = '' WHERE campaign = ?", (name,))
        conn.execute("DELETE FROM schedules WHERE campaign = ?", (name,))
        conn.execute("DELETE FROM campaigns WHERE name = ?", (name,))
    recompute_channels("")
    return len(ids)


def effective_settings(campaign=""):
    """Account settings with the campaign's overrides applied (blank = inherit)."""
    with get_conn() as conn:
        s = get_settings(conn)
        if campaign:
            r = conn.execute("SELECT * FROM campaigns WHERE name = ?", (campaign,)).fetchone()
            if r:
                d = _campaign_row(r)
                if (d["business_info"] or "").strip():
                    s["business_info"] = d["business_info"]
                if d["templates"]:
                    s["templates"] = d["templates"]
                if d["channel_priority"]:
                    s["channel_priority"] = d["channel_priority"]
    return s


def _group(conn, col, campaign, limit=8):
    return [{"name": r[0] or "(none)", "count": r[1]} for r in conn.execute(
        f"SELECT {col}, COUNT(*) c FROM leads WHERE campaign = ? GROUP BY {col} ORDER BY c DESC LIMIT ?",
        (campaign, limit)).fetchall()]


def campaign_dashboard(name):
    """Everything the campaign's own dashboard shows."""
    with get_conn() as conn:
        name = canonical_campaign(conn, name)
        camp = conn.execute("SELECT * FROM campaigns WHERE name = ?", (name,)).fetchone()
        if camp is None:
            return None

        def one(extra=""):
            sql = "SELECT COUNT(*) FROM leads WHERE campaign = ?" + (f" AND {extra}" if extra else "")
            return conn.execute(sql, (name,)).fetchone()[0]

        in_play = "(fit IS NULL OR fit = 1) AND do_not_contact = 0"
        total = one()
        funnel = [
            {"key": "leads", "label": "Leads", "count": total},
            {"key": "in_play", "label": "Still in play", "count": one(in_play)},
            {"key": "contacted", "label": "Contacted", "count": one("send_status = 'sent'")},
            {"key": "replied", "label": "Replied", "count": one("stage IN ('replied','qualified','won')")},
            {"key": "qualified", "label": "Qualified", "count": one("stage IN ('qualified','won')")},
            {"key": "won", "label": "Won", "count": one("stage = 'won'")},
        ]
        f = {x["key"]: x["count"] for x in funnel}
        judged = one("fit IS NOT NULL")
        rates = {
            "reply_rate": round(100 * f["replied"] / f["contacted"], 1) if f["contacted"] else None,
            "win_rate": round(100 * f["won"] / f["contacted"], 1) if f["contacted"] else None,
            "fit_rate": round(100 * one("fit = 1") / judged, 1) if judged else None,
        }
        progress = {"enriched": one("enriched_at IS NOT NULL"), "judged": judged,
                    "drafted": one("message != ''"), "total": total}
        ready = "send_status = 'unsent' AND message != '' AND stage IN ('new','contacted') AND " + in_play
        queue = {
            "needs_enrich": one("enriched_at IS NULL AND do_not_contact = 0"),
            "needs_judge": one("fit IS NULL AND do_not_contact = 0"),
            "needs_draft": one(f"message = '' AND {in_play}"),
            "ready_email": one(f"channel = 'email' AND {ready}"),
            "hand_queue": one(f"channel NOT IN ('email','unknown') AND {ready}"),
        }

        # last 14 days of activity
        today = datetime.now(timezone.utc).date()
        days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
        sent = {d: 0 for d in days}
        replies = {d: 0 for d in days}
        for r in conn.execute(
                """SELECT substr(e.created_at, 1, 10) d, e.kind, COUNT(*) c FROM events e
                   JOIN leads l ON l.id = e.lead_id
                   WHERE l.campaign = ? AND e.created_at >= ?
                     AND e.kind IN ('email_sent','followup_sent','manual_sent','reply')
                   GROUP BY d, e.kind""", (name, days[0])).fetchall():
            target = replies if r["kind"] == "reply" else sent
            target[r["d"]] = target.get(r["d"], 0) + r["c"]
        series = [{"date": d, "sent": sent[d], "replies": replies[d]} for d in days]

        recent = [dict(r) for r in conn.execute(
            """SELECT e.id, e.kind, e.detail, e.created_at, l.id AS lead_id, l.business_name
               FROM events e JOIN leads l ON l.id = e.lead_id
               WHERE l.campaign = ? AND e.kind IN
                 ('email_sent','followup_sent','manual_sent','reply','optout','stage','enriched')
               ORDER BY e.id DESC LIMIT 12""", (name,)).fetchall()]

        return {
            "campaign": _campaign_row(camp), "funnel": funnel, "rates": rates, "progress": progress,
            "queue": queue, "series": series, "recent": recent,
            "by_channel": _group(conn, "channel", name), "by_trade": _group(conn, "trade", name),
            "by_city": _group(conn, "city", name, 6), "by_source": _group(conn, "source", name),
            "by_stage": _group(conn, "stage", name),
        }


# --------------------------------------------------------------- suppression

def suppression_values_for(lead):
    """Every identifier of a lead that a do-not-contact entry could match."""
    vals = [lead.get("email"), lead.get("phone"), lead.get("instagram_url"), lead.get("facebook_url")]
    return [v.lower() for v in vals if v]


def is_suppressed(lead):
    vals = suppression_values_for(lead)
    if not vals:
        return False
    with get_conn() as conn:
        q = ",".join("?" for _ in vals)
        return conn.execute(f"SELECT 1 FROM suppression WHERE value IN ({q}) LIMIT 1", vals).fetchone() is not None


def add_suppression(value, reason="manual"):
    """Add an email, phone, or Instagram/Facebook URL to the do-not-contact
    list, and flag every matching lead."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("value is required")
    email = schema.norm_email(raw)
    if email:
        v, kind = email, "email"
    elif schema.norm_phone(raw):
        v, kind = schema.norm_phone(raw), "phone"
    elif "instagram" in raw.lower() or raw.startswith("@"):
        v, kind = schema.norm_instagram(raw).lower(), "instagram"
    elif "facebook" in raw.lower() or "fb.com" in raw.lower():
        v, kind = schema.norm_facebook(raw).lower(), "facebook"
    else:
        raise ValueError("Enter an email, a phone number, or an Instagram/Facebook URL")
    if not v:
        raise ValueError("Couldn't read that value")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO suppression(value, kind, reason, added_at) VALUES(?,?,?,?) "
            "ON CONFLICT(value) DO UPDATE SET reason=excluded.reason",
            (v, kind, reason, schema.now_iso()),
        )
        col = {"email": "email", "phone": "phone", "instagram": "instagram_url", "facebook": "facebook_url"}[kind]
        conn.execute(
            f"UPDATE leads SET do_not_contact = 1, dnc_reason = ?, updated_at = ? WHERE lower({col}) = ?",
            (reason, schema.now_iso(), v),
        )
    return v


def list_suppression():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM suppression ORDER BY added_at DESC").fetchall()]


def remove_suppression(value):
    """Take an entry off the list, and un-flag the leads it had flagged -- unless another
    entry (say their phone number) still covers them."""
    v = (value or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute("SELECT kind FROM suppression WHERE value = ?", (v,)).fetchone()
        n = conn.execute("DELETE FROM suppression WHERE value = ?", (v,)).rowcount
        col = {"email": "email", "phone": "phone", "instagram": "instagram_url",
               "facebook": "facebook_url"}.get(row["kind"] if row else "")
        leads = [dict(r) for r in conn.execute(
            f"SELECT * FROM leads WHERE do_not_contact = 1 AND lower({col}) = ?", (v,)).fetchall()] if col else []
    for lead in leads:
        if not is_suppressed(lead):
            with get_conn() as conn:
                conn.execute("UPDATE leads SET do_not_contact = 0, dnc_reason = '', updated_at = ? WHERE id = ?",
                             (schema.now_iso(), lead["id"]))
    return n


# ---------------------------------------------------------------------- jobs

def create_job(kind, params=None):
    campaign = ((params or {}).get("campaign") or "") if isinstance(params, dict) else ""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(kind, params, status, created_at, campaign) VALUES(?,?,?,?,?)",
            (kind, json.dumps(params or {}), "queued", schema.now_iso(), campaign),
        )
        return cur.lastrowid


def update_job(job_id, **fields):
    if not fields:
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*fields.values(), job_id),
        )


def get_job(job_id):
    with get_conn() as conn:
        return _d(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


LOG_TAIL_LINES = 12


def list_jobs(kinds=None, limit=15, campaign=None, full_logs=None):
    """Recent runs, newest first. full_logs: None = every log in full; otherwise a set of job ids
    whose whole log is wanted -- the rest carry only their last few lines (log_more = True), which
    keeps the page's every-two-seconds refresh small."""
    clauses, params = [], []
    if kinds:
        clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
        params += list(kinds)
    if campaign is not None:
        clauses.append("campaign = ?")
        params.append(campaign)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM jobs{where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    out = [dict(r) for r in rows]
    if full_logs is not None:
        for j in out:
            lines = (j["log"] or "").split("\n")
            j["log_more"] = j["id"] not in full_logs and len(lines) > LOG_TAIL_LINES
            if j["log_more"]:
                j["log"] = "\n".join(lines[-LOG_TAIL_LINES:])
    return out


# ----------------------------------------------------------------- schedules

def create_schedule(platform, query, location, max_results, campaign, interval_hours, only_no_website=False):
    now = schema.now_iso()
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO schedules(platform, query, location, max_results, campaign, interval_hours, next_run_at, "
            "active, only_no_website) VALUES(?,?,?,?,?,?,?,1,?)",
            (platform, query, location, max_results, campaign, interval_hours, now, 1 if only_no_website else 0),
        ).lastrowid


def list_schedules():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()]


def set_schedule_active(sid, active):
    with get_conn() as conn:
        conn.execute("UPDATE schedules SET active = ? WHERE id = ?", (1 if active else 0, sid))


def delete_schedule(sid):
    with get_conn() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (sid,))


def due_schedules():
    now = schema.now_iso()
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE active = 1 AND next_run_at <= ?", (now,)).fetchall()]


def mark_schedule_ran(sid, interval_hours):
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(hours=interval_hours)).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute("UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                     (now.isoformat(timespec="seconds"), nxt, sid))


# -------------------------------------------------------------------- usage

def _month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage_get(key):
    with get_conn() as conn:
        r = conn.execute("SELECT count FROM usage WHERE month = ? AND key = ?", (_month(), key)).fetchone()
        return r["count"] if r else 0


def usage_add(key, n=1):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO usage(month, key, count) VALUES(?,?,?) "
            "ON CONFLICT(month, key) DO UPDATE SET count = count + excluded.count",
            (_month(), key, n),
        )
