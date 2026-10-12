"""
app.py -- Nexus Reach: scrape/import -> enrich -> judge fit -> draft -> send,
for many users, each with their own accounts, campaigns and database.

Run:   python3 app.py          then open  http://localhost:5000
The UI (templates/ + static/) is a thin layer over the JSON API below, so it
can be redesigned freely without touching any logic.

Security model in one paragraph: everything except the login page and static
files needs a signed-in session. Each request and background job runs "as" one
user (see userctx.py): their own database file, their own stored credentials.
State-changing API calls must carry an X-Requested-With header, which a
malicious website can't add to a cross-site request (CSRF protection).
"""
import csv
import io
import os
import time
import traceback
from datetime import timedelta

from flask import Flask, g, jsonify, redirect, render_template, request, Response, session

import accounts
import config
import cryptobox
import db
import enrichment
import exports
import filtering
import importer
import jobs
import limits
import llm
import outreach
import persistence
import pipeline
import schema
import scraper
import userctx

PUBLIC_PATHS = {"/login", "/api/ping", "/api/auth/status", "/api/auth/login", "/api/auth/signup"}
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# Calls that spend real resources (browser, AI credits, mail) count extra against the rate limit.
_HEAVY = ("/api/enrich", "/api/judge", "/api/draft", "/api/scrape", "/api/send-bulk", "/api/import",
          "/api/check-replies", "/api/connections/test", "/api/admin/backup", "/api/pipeline",
          "/api/followups")
_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app(start_threads=True):
    # Restore everyone's data from outside storage (if configured) BEFORE any database is opened.
    # If the storage can't be reached this raises, and the app refuses to start -- on purpose.
    persistence.start_if_configured()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
    app.secret_key = accounts.secret_key()
    app.config["SECRET_KEY_FALLBACKS"] = cryptobox.fallback_keys()      # old keys still read during a rotation
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config._server_env("COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if config._server_env("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    accounts.ensure()
    per_min = config.env_int("RATE_LIMIT_PER_MIN", 600)
    user_limiter = limits.RateLimiter(per_min) if per_min else None
    ip_limiter = limits.RateLimiter(120) if per_min else None
    ready_users = set()   # users whose database file has been created/upgraded this run

    def prepare_user_db(user):
        if user["id"] not in ready_users:
            with userctx.use(user, accounts.db_path_for(user["id"])):
                db.init_db()      # also marks jobs interrupted by a restart
            ready_users.add(user["id"])

    for u in accounts.all_users():
        prepare_user_db(u)
    if start_threads and os.environ.get("HUB_START_THREADS", "1") != "0":
        accounts.announce_setup_code()

    # ------------------------------------------------------------- auth
    def current_user():
        user = accounts.get_user(session.get("uid"))
        if user and session.get("pv") != user["pv"]:
            return None       # password changed elsewhere -> sign in again
        return user

    def enter(user):
        """Run the rest of this request as `user`."""
        if getattr(g, "ctx_tokens", None):
            userctx.reset_context(g.ctx_tokens)
        prepare_user_db(user)
        g.user = user
        g.ctx_tokens = userctx.set_context(user, accounts.db_path_for(user["id"]))

    @app.before_request
    def _guard():
        if request.method not in _SAFE_METHODS and request.headers.get("X-Requested-With") != "fetch":
            return jsonify({"error": "Missing X-Requested-With header"}), 403
        if request.path.startswith("/static/"):
            return None
        if request.path in PUBLIC_PATHS:
            if ip_limiter:
                ok, wait = ip_limiter.allow(f"ip|{request.remote_addr}")
                if not ok:
                    return _too_many(wait)
            return None
        user = current_user()
        if not user:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Please sign in.", "auth": True}), 401
            return redirect("/login")
        enter(user)
        if user_limiter:
            cost = 10 if request.method != "GET" and request.path.startswith(_HEAVY) else 1
            ok, wait = user_limiter.allow(f"user|{user['id']}", cost)
            if not ok:
                return _too_many(wait)
        return None

    def _too_many(wait):
        r = jsonify({"error": f"Too many requests -- slow down and try again in {int(wait) + 1} seconds."})
        r.status_code = 429
        r.headers["Retry-After"] = str(int(wait) + 1)
        return r

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"          # never let a browser/proxy keep anyone's data
        return resp

    @app.teardown_request
    def _release(exc):
        tokens = g.pop("ctx_tokens", None)
        if tokens:
            userctx.reset_context(tokens)

    @app.errorhandler(ValueError)
    def _value_error(e):
        return jsonify({"error": str(e)}), 400

    @app.errorhandler(outreach.SendError)
    def _send_error(e):
        return jsonify({"error": str(e)}), e.code

    @app.errorhandler(jobs.JobBusy)
    def _busy(e):
        return jsonify({"error": str(e)}), 409

    @app.errorhandler(404)
    def _nf(e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(Exception)
    def _unexpected(e):
        """Anything not handled above. The page only ever reads JSON, so a bare HTML error
        page would show up as an unexplained "Request failed (500)"."""
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description or e.name}), e.code or 500
        traceback.print_exc()
        return jsonify({"error": f"Something went wrong on the server ({type(e).__name__}). "
                                 "Nothing was changed -- try again, and if it keeps happening check the server log."}), 500

    # ---------------------------------------------------------- helpers
    def body():
        return request.get_json(silent=True) or {}

    def lead_filters(src):
        f = {k: src.get(k) for k in ("q", "campaign", "stage", "channel", "source", "trade",
                                     "send_status", "fit", "enriched", "drafted", "min_score", "dnc",
                                     "has", "sent")
             if src.get(k) not in (None, "")}
        for flag in ("not_rejected", "hand", "open_stage", "reachable"):
            if src.get(flag) in ("1", "true", 1, True):
                f[flag] = True
        return f

    def whole_number(value, default, lo, hi):
        try:
            return max(lo, min(int(value), hi))
        except (TypeError, ValueError):
            return default

    def resolve_ids(b):
        """Rows chosen by ticking them (ids) or 'everything matching the filters'."""
        if b.get("ids"):
            return [int(i) for i in b["ids"]]
        return db.select_ids(b.get("filters") or {})

    def need_lead(lead_id):
        lead = db.get_lead(lead_id)
        if lead is None:
            raise outreach.SendError("Lead not found", 404)
        return lead

    def scrape_job(ctx, params):
        """A scrape -- and, when asked, the enrich > judge > draft pipeline on what it found."""
        summary = scraper.run_scrape(ctx, params)
        if params.get("then_pipeline") and not ctx.stopped:
            try:
                jobs.start_job("pipeline", {"campaign": params.get("campaign") or ""}, pipeline.run_pipeline)
                summary += " -- now enriching and judging the new leads"
            except jobs.JobBusy:
                summary += " (the enrich-and-judge run is already going, so it wasn't started twice)"
        return summary

    def launch_scrape(platform, query, location, max_results, campaign, only_no_website=False,
                      then_pipeline=False):
        params = {"platform": platform, "query": query, "location": location,
                  "max_results": max_results, "campaign": campaign,
                  "only_no_website": bool(only_no_website), "then_pipeline": bool(then_pipeline)}
        return jobs.start_job("scrape", params, scrape_job, queue_lock=jobs.SCRAPE_LOCK)

    # Asking Playwright where its browser is starts a helper process (tens of MB, half a second).
    # The answer doesn't change while the server runs, so ask once -- not on every page load,
    # and never in the middle of a scrape on a small server.
    browser_seen = {"ok": None, "at": 0.0}

    def browser_ready():
        if browser_seen["ok"] or (browser_seen["ok"] is False and time.time() - browser_seen["at"] < 300):
            return bool(browser_seen["ok"])
        browser_seen.update(ok=bool(scraper.browser_available()), at=time.time())
        return browser_seen["ok"]

    def is_admin():
        return bool(g.user and g.user["is_admin"])

    # ------------------------------------------------------------ pages
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/login")
    def login_page():
        if current_user():
            return redirect("/")
        return render_template("login.html")

    @app.route("/api/ping")
    def ping():
        """Point an uptime monitor here (keeps a sleeping free host awake, which keeps storage synced)."""
        st = persistence.status()
        out = {"ok": True}
        if st.get("enabled"):
            out["storage_ok"] = not (st.get("conflicts") or st.get("last_error"))
        return jsonify(out)

    # ------------------------------------------------- sign in / sign up
    def start_session(user):
        session.clear()
        session["uid"] = user["id"]
        session["pv"] = user["pv"]
        session.permanent = True

    def public_user(u):
        return {"id": u["id"], "email": u["email"], "name": u["name"], "is_admin": u["is_admin"]}

    def is_local():
        return accounts.is_local_request(request.remote_addr, request.headers)

    @app.route("/api/auth/status")
    def auth_status():
        u = current_user()
        return jsonify({"logged_in": bool(u), "user": public_user(u) if u else None,
                        **accounts.signup_policy(local=is_local())})

    @app.route("/api/auth/signup", methods=["POST"])
    def auth_signup():
        b = body()
        key = f"signup|{request.remote_addr}"
        if accounts.throttled(key):
            return jsonify({"error": "Too many attempts. Wait a few minutes and try again."}), 429
        try:
            user = accounts.create_user(b.get("email"), b.get("password"), b.get("name", ""),
                                        b.get("code", ""), local=is_local())
        except accounts.CodeError:
            accounts.record_failure(key)          # guessing codes counts; typos and weak passwords don't
            raise
        prepare_user_db(user)
        start_session(user)
        return jsonify({"ok": True, "user": public_user(user)})

    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        b = body()
        key = f"login|{request.remote_addr}|{(b.get('email') or '').strip().lower()}"
        if accounts.throttled(key):
            return jsonify({"error": "Too many failed attempts. Wait 15 minutes and try again."}), 429
        user = accounts.authenticate(b.get("email"), b.get("password"))
        if not user:
            accounts.record_failure(key)
            return jsonify({"error": "That email and password don't match."}), 401
        accounts.clear_failures(key)
        start_session(user)
        return jsonify({"ok": True, "user": public_user(user)})

    @app.route("/api/auth/logout", methods=["POST"])
    def auth_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/auth/me")
    def auth_me():
        return jsonify(public_user(g.user))

    @app.route("/api/auth/password", methods=["POST"])
    def auth_password():
        b = body()
        user = accounts.change_password(g.user["id"], b.get("current"), b.get("new"))
        start_session(user)   # keep this browser signed in; other sessions are signed out
        return jsonify({"ok": True})

    @app.route("/api/account", methods=["PUT"])
    def account_update():
        accounts.update_profile(g.user["id"], body().get("name", ""))
        return jsonify({"ok": True})

    # ------------------------------------------- connections (per user)
    @app.route("/api/connections", methods=["GET"])
    def connections_get():
        return jsonify({"fields": accounts.connections_view(), "is_admin": is_admin(),
                        "unreadable": bool(g.user.get("secrets_unreadable"))})

    @app.route("/api/connections", methods=["PUT"])
    def connections_put():
        b = body()
        accounts.set_secrets(g.user["id"], b.get("set") or {}, b.get("clear") or [])
        enter(accounts.get_user(g.user["id"]))     # so the reply reflects the new values
        reread = 0
        if "DEFAULT_COUNTRY_CODE" in (b.get("set") or {}) or "DEFAULT_COUNTRY_CODE" in (b.get("clear") or []):
            reread = db.renormalize_phones()       # numbers that made no sense before may make sense now
            db.recompute_channels()
        return jsonify({"fields": accounts.connections_view(), "is_admin": is_admin(),
                        "unreadable": bool(g.user.get("secrets_unreadable")), "phones_fixed": reread})

    @app.route("/api/connections/test", methods=["POST"])
    def connections_test():
        kind = body().get("kind")
        fn = {"smtp": outreach.test_smtp, "imap": outreach.test_imap, "llm": outreach.test_llm}.get(kind)
        if not fn:
            raise ValueError("Unknown test")
        return jsonify(fn())

    @app.route("/api/config-status")
    def config_status():
        return jsonify({
            "llm": llm.status(),
            "smtp": outreach.smtp_configured(),
            "email_via": outreach.email_transport(),
            "gmail_api_client": bool(config.env("GOOGLE_CLIENT_ID") and config.env("GOOGLE_CLIENT_SECRET")),
            "imap": outreach.imap_configured(),
            "serpapi": bool(config.env("SERPAPI_KEY")),
            "instagram_login": bool(config.env("IG_USERNAME") and config.env("IG_PASSWORD")),
            "playwright": browser_ready(),
            "user": public_user(g.user),
            "placeholders": schema.PLACEHOLDER_HELP,
            "country_code": config.env("DEFAULT_COUNTRY_CODE", "1").lstrip("+") or "1",
        })

    # --------------------------------------------------------- admin only
    @app.route("/api/admin/users")
    def admin_users():
        if not is_admin():
            return jsonify({"error": "Admins only."}), 403
        return jsonify(accounts.admin_list_users())

    @app.route("/api/admin/storage")
    def admin_storage():
        if not is_admin():
            return jsonify({"error": "Admins only."}), 403
        return jsonify({"sync": persistence.status(), "data_dir": str(config.data_dir()),
                        "limits": {"max_users": accounts._max_users(),
                                   "max_leads_per_user": config.env_int("MAX_LEADS_PER_USER", 5000)}})

    @app.route("/api/admin/backup-now", methods=["POST"])
    def admin_backup_now():
        if not is_admin():
            return jsonify({"error": "Admins only."}), 403
        inst = persistence.instance()
        if inst is None:
            raise ValueError("Outside storage isn't set up, so there's nothing to back up to. "
                             "Use 'Download a full backup' instead, or see the free-tier guide in the README.")
        inst.sync_once()
        return jsonify({"files_backed_up": inst.backup_now(), "status": inst.status()})

    @app.route("/api/admin/backup.zip")
    def admin_backup_zip():
        if not is_admin():
            return jsonify({"error": "Admins only."}), 403
        return Response(exports.full_backup_zip(), mimetype="application/zip",
                        headers={"Content-Disposition": "attachment; filename=nexus-reach-full-backup.zip"})

    @app.route("/api/export/my-data.zip")
    def export_my_data():
        return Response(exports.user_export_zip(), mimetype="application/zip",
                        headers={"Content-Disposition": "attachment; filename=my-nexus-reach-data.zip"})

    @app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
    def admin_delete_user(user_id):
        if not is_admin():
            return jsonify({"error": "Admins only."}), 403
        if user_id == g.user["id"]:
            raise ValueError("You can't delete your own account here.")
        return jsonify({"deleted": accounts.delete_user(user_id)})

    # ------------------------------------------------------------ leads
    @app.route("/api/leads", methods=["GET"])
    def leads_list():
        a = request.args
        rows, total = db.list_leads(lead_filters(a), sort=a.get("sort", "score"),
                                    limit=whole_number(a.get("limit"), 50, 1, 500),
                                    offset=whole_number(a.get("offset"), 0, 0, 10 ** 9))
        return jsonify({"leads": rows, "total": total})

    @app.route("/api/leads", methods=["POST"])
    def leads_create():
        b = body()
        if not (b.get("business_name") or "").strip():
            raise ValueError("Business name is required")
        lead_id, is_new = db.upsert_lead(b, campaign=b.get("campaign", ""), source="manual")
        return jsonify({"lead": db.get_lead(lead_id), "is_new": is_new})

    @app.route("/api/leads/<int:lead_id>", methods=["GET"])
    def lead_get(lead_id):
        lead = need_lead(lead_id)
        return jsonify({"lead": lead, "events": db.list_events(lead_id),
                        "templates": db.effective_settings(lead["campaign"])["templates"]})

    @app.route("/api/leads/<int:lead_id>", methods=["PATCH"])
    def lead_patch(lead_id):
        b = body()
        allowed = set(schema.EDITABLE_FIELDS)
        fields = {k: v for k, v in b.items() if k in allowed}
        old = need_lead(lead_id)["stage"]
        lead = db.update_lead(lead_id, fields)
        if "stage" in fields and fields["stage"] != old:
            db.add_event(lead_id, "stage", f"{old} -> {fields['stage']}")
        return jsonify({"lead": lead})

    @app.route("/api/leads/<int:lead_id>", methods=["DELETE"])
    def lead_delete(lead_id):
        return jsonify({"deleted": db.delete_leads([lead_id])})

    @app.route("/api/leads/bulk", methods=["POST"])
    def leads_bulk():
        b = body()
        ids = resolve_ids(b)
        action, value = b.get("action"), b.get("value")
        if not ids:
            raise ValueError("No leads selected")
        if action == "delete":
            return jsonify({"affected": db.delete_leads(ids)})
        mapping = {
            "set_stage": ("stage", value), "set_campaign": ("campaign", value or ""),
            "set_channel": ("channel", value), "set_dnc": ("do_not_contact", bool(value)),
        }
        if action not in mapping:
            raise ValueError("Unknown action")
        field, val = mapping[action]
        if field in ("stage", "do_not_contact"):
            return jsonify({"affected": db.bulk_set(ids, field, val)})
        with db.batch():                          # campaign / channel need each lead's channel + score re-worked out
            for i in ids:
                db.update_lead(i, {field: val})
        return jsonify({"affected": len(ids)})

    @app.route("/api/export.csv")
    def export_csv():
        rows, _ = db.list_leads(lead_filters(request.args), sort="score", limit=1_000_000)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(schema.EXPORT_COLUMNS)
        for r in rows:
            r = dict(r)
            r["fit"] = {1: "yes", 0: "no"}.get(r["fit"], "")
            w.writerow([exports.safe_cell(r.get(c)) for c in schema.EXPORT_COLUMNS])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=leads.csv"})

    # ----------------------------------------------------- import / scrape
    @app.route("/api/import", methods=["POST"])
    def import_csv():
        f = request.files.get("file")
        if not f:
            raise ValueError("Choose a CSV file")
        summary = importer.import_csv_bytes(
            f.read(), campaign=(request.form.get("campaign") or "").strip(),
            source=(request.form.get("source") or "import").strip(), filename=f.filename or "")
        summary.pop("ids", None)
        return jsonify(summary)

    @app.route("/api/scrape", methods=["POST"])
    def scrape_start():
        b = body()
        platform = b.get("platform")
        if platform not in ("google_maps", "yelp", "instagram"):
            raise ValueError("Choose a platform")
        # Several terms and/or places may be given, separated by ';' -- every pair is searched.
        pairs = scraper.plan_searches(b)                 # raises ValueError if empty or too many
        queries = "; ".join(dict.fromkeys(q for q, _ in pairs))
        locations = "; ".join(dict.fromkeys(loc for _, loc in pairs if loc))
        if platform == "yelp" and not locations:
            raise ValueError("Yelp needs a location, e.g. Dallas, TX")
        job_id = launch_scrape(platform, queries, locations, whole_number(b.get("max_results"), 30, 1, 200),
                               (b.get("campaign") or "").strip(), bool(b.get("only_no_website")),
                               bool(b.get("then_pipeline")))
        return jsonify({"job_id": job_id, "searches": len(pairs)})

    @app.route("/api/suggest")
    def suggest():
        # Autocomplete: the account's own history first, then the built-in directory (see db.suggest).
        return jsonify({"suggestions": db.suggest(request.args.get("field", ""), request.args.get("q", ""),
                                                  context=request.args.get("context", "")[:600])})

    @app.route("/api/schedules", methods=["GET"])
    def schedules_list():
        rows = db.list_schedules()
        c = request.args.get("campaign")
        return jsonify([r for r in rows if r["campaign"] == c] if c else rows)

    @app.route("/api/schedules", methods=["POST"])
    def schedules_add():
        b = body()
        if b.get("platform") not in ("google_maps", "yelp", "instagram"):
            raise ValueError("Choose a platform")
        pairs = scraper.plan_searches(b)                 # the same checks a one-off scrape gets
        queries = "; ".join(dict.fromkeys(q for q, _ in pairs))
        locations = "; ".join(dict.fromkeys(loc for _, loc in pairs if loc))
        if b["platform"] == "yelp" and not locations:
            raise ValueError("Yelp needs a location, e.g. Dallas, TX")
        sid = db.create_schedule(b["platform"], queries, locations, whole_number(b.get("max_results"), 30, 1, 200),
                                 (b.get("campaign") or "").strip(), whole_number(b.get("interval_hours"), 24, 1, 24 * 90),
                                 only_no_website=bool(b.get("only_no_website")))
        return jsonify({"id": sid})

    @app.route("/api/schedules/<int:sid>", methods=["PATCH"])
    def schedules_toggle(sid):
        db.set_schedule_active(sid, bool(body().get("active")))
        return jsonify({"ok": True})

    @app.route("/api/schedules/<int:sid>", methods=["DELETE"])
    def schedules_delete(sid):
        db.delete_schedule(sid)
        return jsonify({"ok": True})

    # -------------------------------------------------------------- jobs
    @app.route("/api/jobs")
    def jobs_list():
        kinds = request.args.get("kinds")
        # logs=3,7 -> those runs' logs in full, every other run's last few lines (the page asks
        # for the ones whose "Log" is open). Without it, everything comes back in full.
        logs = request.args.get("logs")
        full = None if logs is None else {int(x) for x in logs.split(",") if x.strip().isdigit()}
        return jsonify(db.list_jobs(kinds.split(",") if kinds else None,
                                    limit=whole_number(request.args.get("limit"), 15, 1, 100),
                                    campaign=request.args.get("campaign"), full_logs=full))

    @app.route("/api/jobs/<int:job_id>")
    def job_get(job_id):
        j = db.get_job(job_id)
        if not j:
            raise outreach.SendError("Job not found", 404)
        return jsonify(j)

    @app.route("/api/jobs/<int:job_id>/stop", methods=["POST"])
    def job_stop(job_id):
        return jsonify({"stopping": jobs.stop_job(job_id)})

    # ------------------------------------------------ enrich / judge / draft
    def job_params(b):
        p = {"ids": b.get("ids") or None, "campaign": b.get("campaign") or "",
             "force": bool(b.get("force")),
             "limit": whole_number(b["limit"], None, 1, 10 ** 6) if b.get("limit") else None}
        if b.get("filters") and not p["ids"]:
            p["ids"] = db.select_ids(b["filters"]) or None
        return p

    @app.route("/api/enrich", methods=["POST"])
    def enrich_start():
        return jsonify({"job_id": jobs.start_job("enrich", job_params(body()), enrichment.run_enrich)})

    @app.route("/api/judge", methods=["POST"])
    def judge_start():
        if not llm.is_configured("filter"):
            raise ValueError("Fit judging needs an AI key -- add one under Settings > Connections.")
        return jsonify({"job_id": jobs.start_job("judge", job_params(body()), filtering.run_judge)})

    def wants_exact(b, campaign=""):
        """Mail-merge mode for this request: said outright, or the account / campaign default."""
        if "exact" in b:
            return bool(b["exact"])
        return bool(db.effective_settings(campaign).get("exact_templates"))

    @app.route("/api/draft", methods=["POST"])
    def draft_start():
        b = body()
        p = job_params(b)
        p["exact"] = wants_exact(b, p["campaign"])
        if not p["exact"] and not llm.is_configured("draft"):
            raise ValueError("Drafting with AI needs an AI key (Settings > Connections). Without one, tick "
                             "\"Use my templates exactly as written\" to fill in your templates instead.")
        p["template"] = b.get("template", "")
        p["channel"] = b.get("channel", "")
        return jsonify({"job_id": jobs.start_job("draft", p, outreach.run_draft)})

    @app.route("/api/pipeline", methods=["POST"])
    def pipeline_start():
        """Enrich, judge and draft in one run (each step only does what's still outstanding)."""
        b = body()
        p = {"ids": b.get("ids") or None, "campaign": (b.get("campaign") or "").strip(),
             "skip_draft": bool(b.get("skip_draft"))}
        if b.get("filters") and not p["ids"]:
            p["ids"] = db.select_ids(b["filters"]) or None
        return jsonify({"job_id": jobs.start_job("pipeline", p, pipeline.run_pipeline)})

    @app.route("/api/usage")
    def usage():
        return jsonify({"serpapi_used": db.usage_get("serpapi"), "serpapi_limit": enrichment.serp_limit()})

    # --------------------------------------------------------- campaigns
    @app.route("/api/campaigns", methods=["GET"])
    def campaigns_list():
        return jsonify({"campaigns": db.list_campaigns(), "unassigned": db.count_unassigned(),
                        "default_criteria": filtering.DEFAULT_CRITERIA,
                        "saved_default_criteria": db.get_setting("default_criteria") or ""})

    @app.route("/api/campaigns", methods=["POST"])
    def campaigns_create():
        b = body()
        name = db.create_campaign(b.get("name"), b.get("description", ""))
        src = db.get_campaign(b["copy_from"]) if b.get("copy_from") else None
        if src:   # start from another campaign's criteria and overrides
            db.save_campaign(name, {k: src[k] for k in ("criteria", "business_info", "templates", "channel_priority")})
        return jsonify({"campaign": db.get_campaign(name)})

    @app.route("/api/campaigns/<path:name>", methods=["GET"])
    def campaign_get(name):
        d = db.campaign_dashboard(name)
        if d is None:
            raise outreach.SendError("Campaign not found", 404)
        canon = d["campaign"]["name"]
        d["queue"]["followups_due"] = sum(1 for l in outreach.followups_due() if l["campaign"] == canon)
        d["queue"]["hand_followups_due"] = sum(1 for l in outreach.hand_followups_due() if l["campaign"] == canon)
        d["replies"] = db.replies_waiting(canon, limit=8)
        d["jobs"] = db.list_jobs(limit=6, campaign=canon)
        d["email"] = outreach.email_status()
        d["saved_default_criteria"] = db.get_setting("default_criteria") or ""
        d["sample_criteria"] = filtering.DEFAULT_CRITERIA
        d["global"] = {k: v for k, v in db.get_settings().items()
                       if k in ("business_info", "templates", "channel_priority")}
        return jsonify(d)

    @app.route("/api/campaigns/<path:name>", methods=["PUT"])
    def campaign_put(name):
        b = body()
        if name.strip().lower() == "default":
            db.set_campaign_criteria("default", b.get("criteria", ""))
            return jsonify({"ok": True})
        if db.get_campaign(name) is None:
            raise outreach.SendError("Campaign not found", 404)
        return jsonify({"campaign": db.save_campaign(name, {k: b[k] for k in db.CAMPAIGN_FIELDS if k in b})})

    @app.route("/api/campaigns/<path:name>", methods=["DELETE"])
    def campaign_delete(name):
        if db.get_campaign(name) is None:
            raise outreach.SendError("Campaign not found", 404)
        n = db.delete_campaign(name, delete_leads=request.args.get("delete_leads") in ("1", "true"))
        return jsonify({"leads_affected": n})

    # ---------------------------------------------------------- outreach
    @app.route("/api/leads/<int:lead_id>/draft", methods=["POST"])
    def lead_draft(lead_id):
        b = body()
        exact = wants_exact(b, need_lead(lead_id)["campaign"])
        if not exact and not llm.is_configured("draft"):
            raise ValueError("Drafting with AI needs an AI key (Settings > Connections). Without one, switch on "
                             "\"Use my templates exactly as written\" under Settings > Messaging.")
        try:
            return jsonify(outreach.draft_message(lead_id, b.get("template", ""), exact=exact))
        except llm.LLMError as e:
            return jsonify({"error": f"The AI call failed: {e}"}), 502

    @app.route("/api/leads/<int:lead_id>/final-message", methods=["POST"])
    def lead_final_message(lead_id):
        # What will actually be sent / pasted: placeholders filled, or a 400 saying what's missing.
        text = body().get("message")
        return jsonify({"message": outreach.final_message(need_lead(lead_id), text if isinstance(text, str) else None)})

    @app.route("/api/leads/<int:lead_id>/send-email", methods=["POST"])
    def lead_send_email(lead_id):
        return jsonify(outreach.send_lead_email(lead_id, force=bool(body().get("force"))))

    @app.route("/api/leads/<int:lead_id>/mark-sent", methods=["POST"])
    def lead_mark_sent(lead_id):
        b = body()
        return jsonify(outreach.mark_sent(lead_id, b.get("channel", ""), followup=bool(b.get("followup"))))

    @app.route("/api/leads/<int:lead_id>/followup", methods=["POST"])
    def lead_followup(lead_id):
        return jsonify(outreach.send_followup(lead_id))

    @app.route("/api/leads/<int:lead_id>/followup-text", methods=["POST"])
    def lead_followup_text(lead_id):
        # the nudge for a lead you message by hand: nothing is sent, the text is just handed back
        return jsonify({"message": outreach.followup_text(need_lead(lead_id))})

    @app.route("/api/followups-due")
    def followups_due():
        rows = outreach.hand_followups_due() if request.args.get("kind") == "hand" else outreach.followups_due()
        c = request.args.get("campaign")
        return jsonify([r for r in rows if r["campaign"] == c] if c else rows)

    @app.route("/api/followups", methods=["POST"])
    def followups_send_all():
        if not outreach.smtp_configured():
            raise ValueError("Email isn't set up: add your email details under Settings > Connections.")
        p = {"campaign": (body().get("campaign") or "").strip()}
        return jsonify({"job_id": jobs.start_job("followups", p, outreach.run_followups)})

    @app.route("/api/replies")
    def replies_waiting():
        return jsonify(db.replies_waiting(request.args.get("campaign", "")))

    @app.route("/api/send-bulk", methods=["POST"])
    def send_bulk():
        if not outreach.smtp_configured():
            raise ValueError("Email isn't set up: add your email details under Settings > Connections.")
        b = body()
        p = {"campaign": b.get("campaign") or "", "only_fit": bool(b.get("only_fit")),
             "ids": b.get("ids") or None}
        return jsonify({"job_id": jobs.start_job("send_bulk", p, outreach.run_send_bulk)})

    @app.route("/api/check-replies", methods=["POST"])
    def check_replies():
        return jsonify(outreach.check_replies())

    @app.route("/api/email-status")
    def email_status():
        return jsonify(outreach.email_status())

    # ---------------------------------------------------------- settings
    @app.route("/api/settings", methods=["GET"])
    def settings_get():
        return jsonify(db.effective_settings(request.args.get("campaign", "")))

    @app.route("/api/settings", methods=["PUT"])
    def settings_put():
        b = body()
        clean = {}
        if "business_info" in b:
            clean["business_info"] = str(b["business_info"])
        if "email_footer" in b:
            clean["email_footer"] = str(b["email_footer"])
        if "templates" in b:
            tpls = []
            for t in b["templates"] or []:
                name, ch, txt = (t.get("name") or "").strip(), t.get("channel"), (t.get("body") or "").strip()
                if name and txt and ch in schema.CHANNELS:
                    tpls.append({"name": name, "channel": ch, "body": txt})
            clean["templates"] = tpls
        if "channel_priority" in b:
            pr = [c for c in b["channel_priority"] if c in schema.CHANNELS and c != "unknown"]
            clean["channel_priority"] = pr or schema.DEFAULT_CHANNEL_PRIORITY
        for k, lo, hi in (("email_daily_limit", 1, 2000), ("email_send_delay_seconds", 0, 3600),
                          ("followup_delay_days", 1, 365), ("max_followups", 0, 10),
                          ("reply_check_minutes", 0, 24 * 60), ("send_start_hour", 0, 23), ("send_end_hour", 1, 24)):
            if k in b:
                n = whole_number(b[k], None, lo, hi)
                if n is None:
                    raise ValueError(f"{k.replace('_', ' ').capitalize()} must be a whole number.")
                clean[k] = n
        for k in ("warmup_enabled", "auto_remove_rejected", "exact_templates", "send_window_enabled"):
            if k in b:
                clean[k] = bool(b[k])
        if "followup_template" in b:
            clean["followup_template"] = str(b["followup_template"] or "").strip()
        if "send_days" in b:
            clean["send_days"] = sorted({int(d) for d in (b["send_days"] or []) if str(d).isdigit() and 0 <= int(d) <= 6})
        if "timezone" in b:
            tz = str(b["timezone"] or "").strip()
            if tz:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(tz)
                except Exception:  # noqa: BLE001
                    raise ValueError(f"'{tz}' isn't a time zone name this server knows. Use one like "
                                     "America/Chicago, Europe/London or Asia/Kolkata.")
            clean["timezone"] = tz
        start = clean.get("send_start_hour", db.get_setting("send_start_hour"))
        end = clean.get("send_end_hour", db.get_setting("send_end_hour"))
        if ("send_start_hour" in clean or "send_end_hour" in clean) and start >= end:
            raise ValueError("Sending hours must end later than they start.")
        saved = db.set_settings(clean)
        if "channel_priority" in clean:
            db.recompute_channels()
        return jsonify(saved)

    @app.route("/api/suppression", methods=["GET"])
    def suppression_list():
        return jsonify(db.list_suppression())

    @app.route("/api/suppression", methods=["POST"])
    def suppression_add():
        b = body()
        return jsonify({"value": db.add_suppression(b.get("value"), b.get("reason") or "manual")})

    @app.route("/api/suppression", methods=["DELETE"])
    def suppression_remove():
        return jsonify({"removed": db.remove_suppression(body().get("value", ""))})

    # ------------------------------------------------------------- stats
    @app.route("/api/stats")
    def stats():
        s = db.stats(request.args.get("campaign", ""))
        s["email"] = outreach.email_status()
        s["serpapi_used"] = db.usage_get("serpapi")
        s["serpapi_limit"] = enrichment.serp_limit()
        return jsonify(s)

    # ------------------------------------------------------- background
    if start_threads and os.environ.get("HUB_START_THREADS", "1") != "0":
        jobs.start_scheduler(launch_scrape, outreach.auto_check_replies)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host=config._server_env("HOST", "127.0.0.1"), port=config.env_int("PORT", 5000),
            debug=False, threaded=True)
