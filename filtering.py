"""
filtering.py -- decide whether each lead is worth contacting.

An LLM reads your plain-language criteria (edited per campaign in the
Pipeline tab) and judges every lead against them. Compared with the original
filter script:

  * the LLM now sees the lead's trade, city, category, review count, website
    status, notes -- everything in the shared lead record
  * a failed judgement leaves the lead UNJUDGED (fit is blank) instead of
    silently rejecting it, so it's retried on the next run
  * rejected leads are never deleted -- filter the Leads tab by Fit = No to
    spot-check them
"""
import config
import db
import llm
import schema

DEFAULT_CRITERIA = """TARGET PROFILE:
US-based local service businesses in these trades: HVAC, plumbing, electrical,
pest control, roofing.

The business should currently have NO website, or an outdated/generic one (for
example a bare directory listing, a Facebook page used as the only web
presence, or a template site that looks abandoned).

Good signals:
- Trade matches one of the target trades above
- has_website is "no", or the notes suggest the site is old or generic
- Based in the US
- Looks like a small, independently run business (not a large chain or
  franchise HQ)

Disqualifying signals:
- Already has a modern, professional, actively maintained website
- Not one of the target trades
- Not US-based
- Part of a large national chain (not a fit for a $400 flat-rate local website offer)

When in doubt, lean toward REJECT rather than wasting an outreach touch on a
poor-fit lead."""


def lead_summary(lead):
    rows = [
        ("business_name", lead.get("business_name")),
        ("trade", lead.get("trade")),
        ("category", lead.get("category_raw")),
        ("city", lead.get("city")),
        ("state", lead.get("state")),
        ("address", lead.get("address")),
        ("has_website", "yes" if lead.get("has_website") else "no"),
        ("website", lead.get("website")),
        ("review_count", lead.get("review_count") or None),
        ("phone", lead.get("phone") or lead.get("phone_raw")),
        ("email", lead.get("email")),
        ("instagram", lead.get("instagram_url")),
        ("facebook", lead.get("facebook_url")),
        ("linkedin", lead.get("linkedin_url")),
        ("yelp", lead.get("yelp_url")),
        ("owner/contact", lead.get("contact_name")),
        ("notes", lead.get("notes")),
        ("enrichment_notes", lead.get("enrichment_notes")),
    ]
    return "\n".join(f"{k}: {v}" for k, v in rows if v)


def build_prompt(lead, criteria):
    return (
        "You are screening a sales lead against a target client profile. Judge ONLY from the "
        "criteria and lead data given -- do not invent facts about the business.\n\n"
        f"TARGET CLIENT CRITERIA:\n{criteria}\n\n"
        f"LEAD DATA:\n{lead_summary(lead) or '(no data available)'}\n\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        '  "fits": true or false\n'
        '  "confidence": "high", "medium" or "low"\n'
        '  "reason": one short sentence\n'
        "No preamble, no markdown."
    )


def judge_lead(lead, criteria):
    """Returns {"fits": bool, "confidence": str, "reason": str}. Raises on failure."""
    reply = llm.complete(build_prompt(lead, criteria), task="filter", max_tokens=500, temperature=0)
    data = llm.extract_json(reply)
    fits = data.get("fits")
    if isinstance(fits, str):
        fits = fits.strip().lower() in ("true", "yes", "1")
    conf = str(data.get("confidence", "low")).lower()
    return {
        "fits": bool(fits),
        "confidence": conf if conf in ("high", "medium", "low") else "low",
        "reason": str(data.get("reason", "")).strip()[:300],
    }


def run_judge(ctx, params):
    auto_remove = db.get_setting("auto_remove_rejected")
    filters = {"dnc": "no"}
    if params.get("ids"):
        filters["ids"] = params["ids"]
    if params.get("campaign"):
        filters["campaign"] = params["campaign"]
    if not params.get("force"):
        filters["fit"] = "null"
    ids = db.select_ids(filters, limit=params.get("limit"))
    ctx.progress(0, len(ids))
    ctx.log(f"Judging {len(ids)} leads")

    criteria_cache = {}
    kept = rejected = failed = removed = 0
    for i, lead_id in enumerate(ids, 1):
        if ctx.stopped:
            ctx.log("Stopped.")
            break
        lead = db.get_lead(lead_id)
        if lead is None:   # deleted by something else mid-run
            ctx.progress(i)
            continue
        camp = lead.get("campaign") or ""
        if camp not in criteria_cache:
            criteria_cache[camp] = db.get_campaign_criteria(camp) or DEFAULT_CRITERIA
        try:
            res = judge_lead(lead, criteria_cache[camp])
        except Exception as e:  # noqa: BLE001
            failed += 1
            ctx.log(f"{lead['business_name']}: could not judge ({e}) -- left unjudged")
            ctx.progress(i)
            if isinstance(e, llm.LLMError) and "not set" in str(e):
                raise  # no API key: no point trying the rest
            continue
        db.update_lead(lead_id, {
            "fit": 1 if res["fits"] else 0,
            "fit_confidence": res["confidence"],
            "fit_reason": res["reason"],
            "judged_at": schema.now_iso(),
        })
        kept += res["fits"]
        rejected += (not res["fits"])
        ctx.log(f"{lead['business_name']}: {'FIT' if res['fits'] else 'reject'} ({res['confidence']}) {res['reason']}")
        if not res["fits"] and auto_remove:
            # Only a lead nobody has touched yet -- never one that's been drafted, sent, replied to
            # or edited by hand. Auto-remove is for keeping a fresh scrape tidy, not for erasing work.
            untouched = (lead["stage"] == "new" and not lead["message"] and lead["send_status"] == "unsent"
                        and not lead["notes"] and not lead["contact_name"])
            if untouched:
                db.delete_leads([lead_id])
                removed += 1
                ctx.log(f"  -> removed (auto-remove is on, and nothing had been done with this lead yet)")
            else:
                ctx.log(f"  -> kept despite rejecting: it already has notes, a draft, or activity on it")
        ctx.progress(i)
        if ctx.sleep(config.env_int("FILTER_DELAY_MS", 700) / 1000):
            break
    return (f"{kept} fit, {rejected} rejected" + (f" ({removed} auto-removed)" if removed else "")
            + (f", {failed} could not be judged (left blank, will retry)" if failed else ""))
