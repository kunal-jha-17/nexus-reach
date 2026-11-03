"""
plans.py -- what each account may use: leads kept, emails a day, sending inboxes, team members,
scrapes a day, and whether the Apify source is on.

Plans are set per account by the server admin (Settings > Account > People using this server).
PLANS_JSON on the server replaces the built-in list; DEFAULT_PLAN is what new accounts get
("unlimited" unless set, so an existing install behaves exactly as before). The admin's own
account is always unlimited. Payment isn't wired in yet: a paid plan is switched on by hand.
"""
import json

import config
import userctx

BUILT_IN = {
    "free": {"label": "Free", "leads": 500, "emails_per_day": 50, "inboxes": 1, "members": 1,
             "scrapes_per_day": 5, "apify": False},
    "pro": {"label": "Pro", "leads": 10000, "emails_per_day": 1000, "inboxes": 5, "members": 3,
            "scrapes_per_day": 50, "apify": True},
    "agency": {"label": "Agency", "leads": 100000, "emails_per_day": 10000, "inboxes": 25, "members": 15,
               "scrapes_per_day": 500, "apify": True},
    "unlimited": {"label": "Unlimited", "leads": None, "emails_per_day": None, "inboxes": None, "members": None,
                  "scrapes_per_day": None, "apify": True},
}
KEYS = ("leads", "emails_per_day", "inboxes", "members", "scrapes_per_day")


class PlanLimit(ValueError):
    """Something the account's plan doesn't allow (shown to the person as is)."""


def all_plans():
    raw = config._server_env("PLANS_JSON")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                return {k: {**BUILT_IN["unlimited"], **v} for k, v in data.items() if isinstance(v, dict)}
        except ValueError:
            pass
    return dict(BUILT_IN)


def plan_name(user):
    if not user:
        return "unlimited"
    if user.get("is_admin"):
        return "unlimited"
    plans = all_plans()
    name = user.get("plan") or config._server_env("DEFAULT_PLAN") or "unlimited"
    return name if name in plans else "unlimited"


def limits_for(user):
    name = plan_name(user)
    return {"name": name, **all_plans()[name]}


def current():
    """Limits for the workspace this request/job works in."""
    return limits_for(userctx.get_user())


def cap(key, user=None):
    """The plan's number for key, or None for no limit."""
    v = (limits_for(user) if user is not None else current()).get(key)
    return None if v in (None, "", 0) else int(v)


def check_members(owner, count_with_new):
    c = cap("members", owner)
    if c is not None and count_with_new + 1 > c:
        raise PlanLimit(f"The {limits_for(owner)['label']} plan allows {c} people in a team. Upgrade to add more.")


def check_inboxes(count_with_new):
    c = cap("inboxes")
    if c is not None and count_with_new + 1 > c:                # +1: the main inbox counts too
        raise PlanLimit(f"The {current()['label']} plan allows {c} sending inbox{'es' if c != 1 else ''}. Upgrade to add more.")


def check_scrape(platform, runs_today):
    lim = current()
    if platform == "apify_maps" and not lim.get("apify"):
        raise PlanLimit(f"Google Maps via Apify isn't included in the {lim['label']} plan.")
    c = cap("scrapes_per_day")
    if c is not None and runs_today >= c:
        raise PlanLimit(f"The {lim['label']} plan allows {c} scrape runs a day -- that's today's done.")
