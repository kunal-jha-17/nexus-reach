"""
jobs.py -- run long tasks (scrapes, enrichment, fit judging, drafting, bulk
sending) in background threads while the page polls for progress.

Every job is a row in the `jobs` table, so progress and logs survive page
reloads. Rules:
  * scrape jobs queue behind each other (only one browser at a time)
  * enrich / judge / draft / send_bulk: one of each kind at a time
  * any job can be stopped; work already saved stays saved, so re-running a
    stopped or crashed job simply carries on with what's left
"""
import threading
import time
import traceback

import accounts
import config
import db
import schema
import userctx

SCRAPE_LOCK = threading.Lock()
SINGLETON_KINDS = {"enrich", "judge", "draft", "send_bulk"}

_lock = threading.Lock()
_stops = {}      # (user_id, job_id) -> threading.Event
_active = {}     # (user_id, kind) -> job_id   (singleton kinds only)


class JobBusy(RuntimeError):
    pass


class JobContext:
    """What a running job uses to report progress and check for a stop request."""

    def __init__(self, job_id, stop_event):
        self.job_id = job_id
        self._stop = stop_event
        self._lines = []

    @property
    def stopped(self):
        return self._stop.is_set()

    def log(self, msg):
        self._lines.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        self._lines = self._lines[-300:]
        db.update_job(self.job_id, log="\n".join(self._lines))

    def progress(self, done, total=None):
        fields = {"progress": done}
        if total is not None:
            fields["total"] = total
        db.update_job(self.job_id, **fields)

    def sleep(self, seconds):
        """Interruptible sleep. Returns True if a stop was requested."""
        return self._stop.wait(seconds)


def start_job(kind, params, fn, queue_lock=None):
    """Create a job row and run fn(ctx, params) in a background thread.
    fn returns a short summary string. Raises JobBusy if a singleton kind is
    already running for this user. The job runs as the user who started it
    (their database, their credentials). With HUB_JOBS_SYNC=1 (tests) it runs inline."""
    user = userctx.get_user()
    db_path = userctx.db_path()
    uid = user["id"] if user else 0
    if kind == "scrape":
        cap = config.env_int("MAX_QUEUED_SCRAPES", 3)
        waiting = [j for j in db.list_jobs(kinds=["scrape"], limit=50) if j["status"] in ("queued", "running")]
        if cap and len(waiting) >= cap:
            raise JobBusy(f"You already have {len(waiting)} scrapes running or waiting. Let them finish (or stop one) "
                          "before starting another -- scrapes share one browser on this server.")
    with _lock:
        if kind in SINGLETON_KINDS and (uid, kind) in _active:
            raise JobBusy(f"A '{kind}' job is already running -- wait for it or stop it first.")
        job_id = db.create_job(kind, params)
        stop = threading.Event()
        _stops[(uid, job_id)] = stop
        if kind in SINGLETON_KINDS:
            _active[(uid, kind)] = job_id

    def runner():
        ctx = JobContext(job_id, stop)
        try:
            if queue_lock is not None:
                queue_lock.acquire()
            try:
                db.update_job(job_id, status="running")
                summary = fn(ctx, params) or ""
                status = "stopped" if ctx.stopped else "done"
                db.update_job(job_id, status=status, summary=str(summary),
                              finished_at=schema.now_iso())
            finally:
                if queue_lock is not None:
                    queue_lock.release()
        except Exception as e:  # noqa: BLE001 -- surface any failure in the UI
            ctx.log("ERROR: " + "".join(traceback.format_exception_only(type(e), e)).strip())
            db.update_job(job_id, status="error", summary=str(e)[:500], finished_at=schema.now_iso())
        finally:
            with _lock:
                _stops.pop((uid, job_id), None)
                if _active.get((uid, kind)) == job_id:
                    _active.pop((uid, kind), None)

    def runner_as_user():
        with userctx.use(user, db_path):   # threads don't inherit the request's user
            runner()

    if config.env_bool("HUB_JOBS_SYNC", False):
        runner()
    else:
        threading.Thread(target=runner_as_user, daemon=True, name=f"job-{uid}-{job_id}").start()
    return job_id


def stop_job(job_id):
    user = userctx.get_user()
    with _lock:
        ev = _stops.get((user["id"] if user else 0, job_id))
    if ev:
        ev.set()
        return True
    return False


# ---------------------------------------------------------------- scheduler

_scheduler_started = False


def run_scheduler_once(launch_scrape):
    """Start any recurring scrape that's due, for every user, each as that user."""
    for user in accounts.all_users():
        with userctx.use(user, accounts.db_path_for(user["id"])):
            for s in db.due_schedules():
                db.mark_schedule_ran(s["id"], s["interval_hours"])
                launch_scrape(s["platform"], s["query"], s["location"] or "",
                              s["max_results"], s["campaign"] or "")


def start_scheduler(launch_scrape):
    """Every minute, run whatever recurring scrapes are due.
    launch_scrape(platform, query, location, max_results, campaign)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop():
        while True:
            try:
                run_scheduler_once(launch_scrape)
            except Exception:  # noqa: BLE001 -- never let the loop die
                traceback.print_exc()
            time.sleep(60)

    threading.Thread(target=loop, daemon=True, name="scheduler").start()
