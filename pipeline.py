"""
pipeline.py -- run the three "get them ready" steps in one go.

    enrich  ->  judge fit  ->  draft messages

Each step is the same job you can start on its own (enrichment.run_enrich, filtering.run_judge,
outreach.run_draft) and, like them, only touches leads that still need it -- so running the
pipeline twice does no work twice, and a run that was stopped or interrupted simply carries on.
Nothing is ever sent from here.
"""
import db
import enrichment
import filtering
import llm
import outreach


class _Step:
    """The job's context, with every log line labelled by the step it came from."""

    def __init__(self, ctx, label):
        self._ctx, self._label = ctx, label

    @property
    def stopped(self):
        return self._ctx.stopped

    def log(self, msg):
        self._ctx.log(f"[{self._label}] {msg}")

    def progress(self, done, total=None):
        self._ctx.progress(done, total)

    def sleep(self, seconds):
        return self._ctx.sleep(seconds)


def run_pipeline(ctx, params):
    campaign = (params.get("campaign") or "").strip()
    base = {"campaign": campaign, "ids": params.get("ids") or None}
    done = []

    done.append("Enrich: " + enrichment.run_enrich(_Step(ctx, "enrich"), dict(base)))
    if ctx.stopped:
        return " | ".join(done) + " | stopped"

    judged = llm.is_configured("filter")
    if judged:
        done.append("Judge: " + filtering.run_judge(_Step(ctx, "judge"), dict(base)))
    else:
        ctx.log("[judge] skipped -- no AI key yet (Settings > Connections)")
        done.append("Judge: skipped (no AI key)")
    if ctx.stopped:
        return " | ".join(done) + " | stopped"

    if params.get("skip_draft"):
        return " | ".join(done)
    exact = bool(db.effective_settings(campaign).get("exact_templates"))
    if exact or llm.is_configured("draft"):
        # once leads have been judged, only the ones that fit are worth writing to
        done.append("Draft: " + outreach.run_draft(_Step(ctx, "draft"), {**base, "only_fit": judged}))
    else:
        ctx.log("[draft] skipped -- needs an AI key, or switch on 'use my templates exactly as written'")
        done.append("Draft: skipped (no AI key)")
    return " | ".join(done)
