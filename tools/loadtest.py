#!/usr/bin/env python3
"""
tools/loadtest.py -- pretend to be many people using the app at once.

    python3 tools/loadtest.py                      # starts a private copy of the app and hammers it
    python3 tools/loadtest.py --users 30 --url http://localhost:5000   # against one you already run
                                                   #   (needs RATE_LIMIT_PER_MIN=0 and SIGNUP allowed there)

Each pretend person signs up, imports a CSV of leads, searches and filters, opens and edits leads, makes a
campaign and opens its dashboard, exports their data, and runs enrichment -- all at the same time as everyone
else. It also fires a burst of simultaneous sign-ins (password hashing is deliberately slow, so this is the
first thing to hurt on a tiny server). Prints latency per action and any errors.
"""
import argparse
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
HEAD = {"X-Requested-With": "fetch"}


def make_csv(n, seed):
    rnd = random.Random(seed)
    trades = ["HVAC contractor", "Plumber", "Electrician", "Roofing contractor", "Pest control service"]
    rows = ["name,phone,full_address,category,reviews,email_1,site"]
    for i in range(n):
        area = rnd.randint(200, 989)
        rows.append(f'Biz {seed}-{i},({area}) 555-{i:04d},"{i} Main St, City{i % 40}, TX 75{i % 100:03d}",'
                    f'{rnd.choice(trades)},{rnd.randint(0, 300)},{"b%d_%d@example.com" % (seed, i) if i % 3 == 0 else ""},'
                    f'{"site%d-%d.example" % (seed, i) if i % 4 == 0 else ""}')
    return "\n".join(rows).encode()


class Stats:
    def __init__(self):
        self.lat, self.errors, self.lock = {}, {}, threading.Lock()

    def record(self, name, seconds, ok, detail=""):
        with self.lock:
            self.lat.setdefault(name, []).append(seconds)
            if not ok:
                self.errors.setdefault(name, []).append(detail)

    def report(self):
        print(f"\n{'action':<28}{'calls':>6}{'p50':>9}{'p95':>9}{'max':>9}{'errors':>8}")
        for name in sorted(self.lat):
            v = sorted(self.lat[name])
            p95 = v[min(len(v) - 1, int(len(v) * 0.95))]
            print(f"{name:<28}{len(v):>6}{statistics.median(v):>8.2f}s{p95:>8.2f}s{v[-1]:>8.2f}s{len(self.errors.get(name, [])):>8}")
        bad = {k: v for k, v in self.errors.items() if v}
        if bad:
            print("\nERRORS")
            for k, v in bad.items():
                print(f"  {k}: {len(v)} e.g. {v[0]}")
        return sum(len(v) for v in self.errors.values())


def timed(stats, name, fn, expect=(200,)):
    t = time.time()
    try:
        r = fn()
        ok = r.status_code in expect
        stats.record(name, time.time() - t, ok, f"HTTP {r.status_code}: {r.text[:120]}")
        return r
    except Exception as e:  # noqa: BLE001
        stats.record(name, time.time() - t, False, repr(e)[:120])
        return None


def one_person(base, idx, stats, leads, start_gate):
    s = requests.Session()
    s.headers.update(HEAD)
    email, pw = f"load{idx}-{random.randint(1000, 9999)}@example.com", f"purple-tractor-{idx}-window"
    start_gate.wait()
    timed(stats, "sign up", lambda: s.post(base + "/api/auth/signup", json={"email": email, "password": pw}))
    camp = f"camp-{idx}"
    timed(stats, "create campaign", lambda: s.post(base + "/api/campaigns", json={"name": camp}))
    csv = make_csv(leads, idx)
    timed(stats, f"import {leads} leads", lambda: s.post(base + "/api/import", data={"campaign": camp},
                                                           files={"file": ("l.csv", csv)}))
    ids = []
    for i in range(12):
        r = timed(stats, "list leads (page)", lambda: s.get(base + "/api/leads", params={"campaign": camp, "limit": 50, "offset": (i % 3) * 50}))
        if r is not None and r.ok and not ids:
            ids = [l["id"] for l in r.json()["leads"]]
        timed(stats, "search leads", lambda: s.get(base + "/api/leads", params={"q": f"Biz {idx}-1", "campaign": camp}))
        timed(stats, "filter leads", lambda: s.get(base + "/api/leads", params={"campaign": camp, "channel": "email", "sort": "score"}))
    for lid in ids[:6]:
        timed(stats, "open lead", lambda: s.get(base + f"/api/leads/{lid}"))
        timed(stats, "edit lead", lambda: s.patch(base + f"/api/leads/{lid}", json={"notes": "spoke to owner", "stage": "contacted"}))
    for _ in range(4):
        timed(stats, "campaign dashboard", lambda: s.get(base + f"/api/campaigns/{camp}"))
        timed(stats, "home dashboard stats", lambda: s.get(base + "/api/stats"))
    timed(stats, "bulk set stage", lambda: s.post(base + "/api/leads/bulk", json={"filters": {"campaign": camp}, "action": "set_stage", "value": "contacted"}))
    timed(stats, "export my data (zip)", lambda: s.get(base + "/api/export/my-data.zip"))
    timed(stats, "export leads csv", lambda: s.get(base + "/api/export.csv", params={"campaign": camp}))
    r = timed(stats, "start enrich job", lambda: s.post(base + "/api/enrich", json={"campaign": camp, "limit": 20}))
    if r is not None and r.ok:
        jid, t0 = r.json()["job_id"], time.time()
        while time.time() - t0 < 120:
            j = timed(stats, "poll job", lambda: s.get(base + f"/api/jobs/{jid}"))
            if j is not None and j.ok and j.json()["status"] in ("done", "error", "stopped"):
                stats.record("enrich job (start->done)", time.time() - t0, j.json()["status"] == "done", str(j.json())[:120])
                break
            time.sleep(0.5)
    return email, pw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=15)
    ap.add_argument("--leads", type=int, default=300, help="leads imported per person")
    ap.add_argument("--burst", type=int, default=30, help="simultaneous sign-ins in the login-spike test")
    ap.add_argument("--url", help="test an app that's already running instead of starting one")
    a = ap.parse_args()

    proc = None
    if not a.url:
        port = 5099
        data = tempfile.mkdtemp()
        env = dict(os.environ, DATA_DIR=data, RATE_LIMIT_PER_MIN="0", HUB_START_THREADS="0", PORT=str(port),
                   ADOPT_LEGACY_DB="0", HUB_DB=os.path.join(data, "none.db"), MAX_USERS="500", SYNC_FOLDER="", S3_BUCKET="",
                   MAX_LEADS_PER_USER="5000")
        proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        a.url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                requests.get(a.url + "/api/ping", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.5)
    base = a.url.rstrip("/")
    stats = Stats()
    print(f"{a.users} people at once, {a.leads} leads each, against {base}  (CPUs here: {os.cpu_count()})")

    try:
        gate, people, threads = threading.Event(), [], []
        def run(i):
            people.append(one_person(base, i, stats, a.leads, gate))
        for i in range(a.users):
            t = threading.Thread(target=run, args=(i,))
            t.start()
            threads.append(t)
        t0 = time.time()
        gate.set()
        for t in threads:
            t.join()
        print(f"everyone finished in {time.time() - t0:.1f}s")

        # login spike: many simultaneous sign-ins (slow password hashing is on purpose)
        spike = Stats()
        gate2, ths = threading.Event(), []
        chosen = [people[i % len(people)] for i in range(a.burst)] if people else []
        def login(email, pw):
            gate2.wait()
            s = requests.Session(); s.headers.update(HEAD)
            timed(spike, f"login (burst of {a.burst})", lambda: s.post(base + "/api/auth/login", json={"email": email, "password": pw}))
        for email, pw in chosen:
            t = threading.Thread(target=login, args=(email, pw)); t.start(); ths.append(t)
        gate2.set()
        for t in ths:
            t.join()
        for k, v in spike.lat.items():
            stats.lat.setdefault(k, []).extend(v)
        for k, v in spike.errors.items():
            stats.errors.setdefault(k, []).extend(v)
        errors = stats.report()
        print("\nVERDICT:", "no errors" if not errors else f"{errors} errors -- see above")
        return 1 if errors else 0
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
