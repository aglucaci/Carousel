"""Artifact checks (plan task 27): reasons a "trend" may be the sources' doing, checked before any is reported.

Each check either passes, fires (and withholds the flag, naming the check), or
doesn't apply. They wrap the trend model (task 26):

``indexing_lag``
    Sources index a week's papers over days to weeks, so a recent week's count
    starts low and grows. Each source's **settling time** is measured from
    re-observations (task 25): for weeks seen incomplete (below ``SETTLED_SHARE``
    of their latest count), the first later observation that reached it; the
    slowest such week counts, once ``MIN_LAG_WEEKS`` weeks were measured (until
    then a conservative default per source). Weeks never seen incomplete say
    nothing about how long indexing took and are left out. Weeks that haven't settled are **provisional**: the trend
    is evaluated only through the last settled week, and the report says which
    weeks were held back. This check never fires; it moves the evaluation window.
``denominator_shift``
    If the field's own weekly count in the recent weeks jumps or drops against
    its baseline (mean recent / median baseline outside ``DENOMINATOR_BAND``), the
    source's indexing or coverage probably changed (a journal added, a backlog
    loaded), and shares would move for that reason alone: every flag from that
    source is withheld.
``classifier_change``
    Counts made under another topic-model version are never mixed in. When the
    current version has too little history but earlier versions were observed,
    the report says the refit is why (``insufficient_history`` alone would hide it).
``duplicate_surge``
    One work can appear as several records (a preprint and its article, a
    preprint's versions). If a flagged topic's recent papers in the store are
    largely versions of the same works (distinct works / records below
    ``DISTINCT_SHARE``), the rise may be double counting: the flag is withheld.

Every report carries ``CHECKS_VERSION``.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from carousel.intelligence.observations import FIELD, counts_as_of, lag_profile, series

CHECKS_VERSION = "checks-1"
SETTLED_SHARE = 0.95
MIN_LAG_WEEKS = 3
DEFAULT_SETTLING_DAYS = {"pubmed": 21, "europepmc": 21, "openalex": 28, "arxiv": 3}
FALLBACK_SETTLING_DAYS = 28
DENOMINATOR_BAND = (2 / 3, 1.5)
DISTINCT_SHARE = 0.7
MIN_DUPLICATE_RECORDS = 5

# (topic_id, first week, last week) -> [(paper_id, work_key)]: the topic's papers in those weeks, with a key that is
# the same for every version of one work (a preprint and its article share it).
TopicPapers = Callable[[str, dt.date, dt.date], List[Tuple[str, str]]]


def settling_days(rows: Sequence[Mapping[str, Any]], source: str) -> Tuple[int, str]:
    """(days, basis): measured from re-observed weeks of the field, or the source's default."""
    measured = []
    for week in lag_profile(rows, source, FIELD):
        obs = week["observations"]
        below = [o["days_after_week"] for o in obs if (o["share_of_latest"] or 0) < SETTLED_SHARE]
        if not below:
            continue  # never seen incomplete: says nothing about how long it took
        after = [o["days_after_week"] for o in obs if o["days_after_week"] > max(below)
                 and (o["share_of_latest"] or 0) >= SETTLED_SHARE]
        if after:
            measured.append(min(after))
    if len(measured) >= MIN_LAG_WEEKS:
        return max(measured), f"measured from {len(measured)} re-observed weeks"
    return DEFAULT_SETTLING_DAYS.get(source, FALLBACK_SETTLING_DAYS), "default (too few re-observed weeks)"


def settled_through(as_of: dt.date, days: int) -> dt.date:
    """The Sunday ending the last week whose counts have settled by ``as_of``."""
    last = as_of - dt.timedelta(days=days)
    return last - dt.timedelta(days=(last.isoweekday() % 7))  # back to a Sunday (a Sunday stays)


def _check(name: str, status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **extra}


def checked_trends(rows: Sequence[Mapping[str, Any]], topic_ids: Sequence[str], source: str, as_of: dt.datetime,
                   *, classifier_version: str, all_rows: Optional[Sequence[Mapping[str, Any]]] = None,
                   topic_papers: Optional[TopicPapers] = None, recent: Optional[int] = None) -> Dict[str, Any]:
    """The trend report for one source with every check applied (rows: this classifier version's observations)."""
    from carousel.intelligence.trends import RECENT_WEEKS, TREND_MODEL_VERSION, evaluate

    recent = recent or RECENT_WEEKS
    days, basis = settling_days(rows, source)
    through = settled_through(as_of.astimezone(dt.timezone.utc).date(), days)
    known = counts_as_of(rows, as_of)
    all_weeks = sorted({w for s, _, w in known if s == source})
    settled = [w for w in all_weeks if w + dt.timedelta(days=6) <= through]
    provisional = [w.isoformat() for w in all_weeks if w not in settled]
    report_checks = [_check("indexing_lag", "pass", f"weeks ending after {through.isoformat()} are provisional "
                            f"(settling time {days} days, {basis}); the trend is evaluated through {through.isoformat()}",
                            settling_days=days, basis=basis, provisional_weeks=provisional)]

    def cut(s: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [x for x in s if dt.date.fromisoformat(x["week"]) in set(settled)]

    by_topic = {tid: cut(series(known, source, tid)) for tid in topic_ids}
    report = evaluate(by_topic, recent)

    # ---- denominator shift (whole source) ----
    field = [known[(source, FIELD, w)]["count"] for w in settled if (source, FIELD, w) in known]
    denom_fired = False
    if len(field) > recent + 2:
        base, rec = field[:-recent], field[-recent:]
        median = statistics.median(base)
        ratio = (sum(rec) / len(rec)) / median if median else None
        lo, hi = DENOMINATOR_BAND
        if ratio is not None and not lo <= ratio <= hi:
            denom_fired = True
            report_checks.append(_check("denominator_shift", "fired", f"the field's recent weekly count is {ratio:.2f}x "
                                        f"its baseline median: the source's coverage likely changed", ratio=round(ratio, 4)))
        else:
            report_checks.append(_check("denominator_shift", "pass", "the field's weekly count is stable",
                                        ratio=None if ratio is None else round(ratio, 4)))
    else:
        report_checks.append(_check("denominator_shift", "not_applicable", "too few settled weeks to compare"))

    # ---- classifier change (whole source) ----
    others = sorted({r["classifier_version"] for r in (all_rows or []) if r["classifier_version"] != classifier_version
                     and r.get("source") == source})
    short = any(t["status"] == "insufficient_history" for t in report["topics"])
    if others and short:
        report_checks.append(_check("classifier_change", "fired", f"this topic model ({classifier_version}) has too little "
                                    f"history; earlier weeks were counted under {', '.join(others)} and are not mixed in",
                                    other_versions=others))
    else:
        report_checks.append(_check("classifier_change", "pass" if not others else "not_applicable",
                                    "one topic-model version throughout" if not others else
                                    f"earlier versions ({', '.join(others)}) are not mixed in", other_versions=others))

    # ---- per topic: duplicates, then the final flag ----
    weeks_recent = settled[-recent:]
    for t in report["topics"]:
        checks = []
        if denom_fired:
            checks.append(_check("denominator_shift", "fired", "withheld: the field's count shifted"))
        if t["flagged"] and topic_papers is not None and weeks_recent:
            papers = topic_papers(t["topic_id"], weeks_recent[0], weeks_recent[-1] + dt.timedelta(days=6))
            works = {w for _, w in papers}
            if len(papers) >= MIN_DUPLICATE_RECORDS and len(works) / len(papers) < DISTINCT_SHARE:
                checks.append(_check("duplicate_surge", "fired", f"{len(papers)} records but {len(works)} distinct works: "
                                     "versions of the same works may inflate the count", records=len(papers),
                                     works=len(works)))
            else:
                checks.append(_check("duplicate_surge", "pass", f"{len(papers)} records, {len(works)} distinct works",
                                     records=len(papers), works=len(works)))
        fired = [c["check"] for c in checks if c["status"] == "fired"]
        t["checks"] = checks
        if fired:
            t["flagged"] = False
            t["failed"] = list(t["failed"]) + fired
    return {"source": source, "as_of": as_of.isoformat(), "trend_model_version": TREND_MODEL_VERSION,
            "checks_version": CHECKS_VERSION, "classifier_version": classifier_version,
            "evaluated_through": through.isoformat(), "provisional_weeks": provisional, "checks": report_checks,
            "criteria": report["criteria"], "topics": report["topics"]}


def store_topic_papers(store: Any, classifier_version: str) -> TopicPapers:
    """Topic papers from the local store: assigned under ``classifier_version``, by earliest public date."""
    from carousel.core.paper import partial_date_bounds

    placed = store.paper_topics(classifier_version)

    def lookup(topic_id: str, start: dt.date, end: dt.date) -> List[Tuple[str, str]]:
        out = []
        for pid, v in placed.items():
            if v["topic_id"] != topic_id:
                continue
            paper = store.get(pid)
            date = paper.earliest_public_date if paper else None
            if not date:
                continue
            lo, _ = partial_date_bounds(date)
            if start <= lo <= end:
                linked = sorted([pid] + [x["paper_id"] for x in store.version_links(pid)])
                out.append((pid, linked[0]))  # every version of a work shares its smallest paper ID
        return out

    return lookup


__all__ = ["CHECKS_VERSION", "DEFAULT_SETTLING_DAYS", "DENOMINATOR_BAND", "DISTINCT_SHARE", "checked_trends",
           "settled_through", "settling_days", "store_topic_papers"]
