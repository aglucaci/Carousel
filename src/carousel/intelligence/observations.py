"""Topic observations (plan task 25): weekly counts per topic and their denominators, as known when observed.

A scan's own results can't measure a trend: they are capped and windowed, so
counting them measures the radar, not the literature. Observations ask the
sources for **counts** instead (count requests only; nothing is fetched):

- the radar's **field**: its queries and exclusions, per week (the
  denominator ``N_t``);
- each **topic**: the field query AND any of the topic's top key terms
  (``TOPIC_TERMS``), per week (``n_{c,t}``).

Every row records the source, the exact query sent, the week, the topic model
version, and **when it was observed**. Rows are append-only: a week observed
again later gets a new row, never an edit, so

- :func:`counts_as_of` rebuilds exactly what was known at any time ``t`` (it
  never reads a row observed after ``t``), which is what a time-forward
  evaluation needs; and
- the difference between observations of the same week (:func:`lag_profile`)
  measures how long a source takes to index a week fully.

Periods are ISO weeks (Monday to Sunday, UTC), complete weeks only.
:func:`observe` asks for the newest weeks first and stops at its request
budget, so a short budget still covers the recent past.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from carousel.connectors.base import BudgetExceeded, ConnectorError, SearchQuery

OBSERVATIONS_VERSION = "observations-1"
FIELD = "__field__"
"""``topic_id`` of the denominator rows: the radar's whole field."""
TOPIC_TERMS = 4
DEFAULT_WEEKS = 16  # 8 baseline + 4 recent + a week for the persistence check, with room to spare
DEFAULT_BUDGET = 400
COUNTING_SOURCES = ("pubmed", "europepmc", "openalex", "arxiv")


def counting_connectors(names: Sequence[str]) -> Dict[str, Any]:
    """Connectors for counting. PubMed counts by **entry date** (``edat``, when a record was added), not publication
    date: many journals publish only a month ("2024 Apr"), which PubMed files on the 1st, so weekly counts by
    publication date spike every four to five weeks."""
    from carousel.connectors import CONNECTORS, PubMedConnector

    return {n: (PubMedConnector(date_field="edat") if n == "pubmed" else CONNECTORS[n]())
            for n in names if n in COUNTING_SOURCES and n in CONNECTORS}

Week = Tuple[dt.date, dt.date]


def complete_weeks(before: dt.date, n: int) -> List[Week]:
    """The ``n`` most recent complete ISO weeks ending before ``before``, newest first."""
    last_sunday = before - dt.timedelta(days=before.isoweekday())  # a week isn't complete until its Sunday has passed
    out = []
    for i in range(n):
        end = last_sunday - dt.timedelta(days=7 * i)
        out.append((end - dt.timedelta(days=6), end))
    return out


def topic_terms(topic: Any, n: int = TOPIC_TERMS) -> List[str]:
    return list(topic.key_terms[:n])


def queries_for(radar: Any, model: Any, week: Week) -> List[Tuple[str, SearchQuery]]:
    """(topic_id, query) for the field and each topic of ``model`` in one week."""
    start, end = week
    base = {"terms": list(radar.queries), "exclude": list(radar.exclude), "start": start, "end": end, "max_results": 1}
    out = [(FIELD, SearchQuery(**base))]
    for t in model.topics:
        terms = topic_terms(t)
        if terms:
            out.append((t.topic_id, SearchQuery(**base, require=terms)))
    return out


def observe(store: Any, radar: Any, model: Any, connectors: Mapping[str, Any], *, at: dt.datetime,
            weeks: int = DEFAULT_WEEKS, budget: int = DEFAULT_BUDGET, today: Optional[dt.date] = None) -> Dict[str, Any]:
    """Count the field and each topic for the radar's counting sources, newest week first, within ``budget`` requests.

    ``connectors`` are the radar's sources that can count (``COUNTING_SOURCES``). Every request, failed or not,
    becomes a row (``status`` ok | error); nothing already stored is changed.
    """
    today = today or at.astimezone(dt.timezone.utc).date()
    sources = [s.value for s in radar.sources if s.value in COUNTING_SOURCES and s.value in connectors]
    stats: Dict[str, Any] = {"weeks": 0, "requests": 0, "ok": 0, "errors": 0, "truncated": False, "sources": sources}
    observed_at = at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for week in complete_weeks(today, weeks):
        if stats["requests"] >= budget:
            stats["truncated"] = True
            break
        rows = []
        for name in sources:
            conn = connectors[name]
            for topic_id, q in queries_for(radar, model, week):
                if stats["requests"] >= budget:
                    stats["truncated"] = True
                    break
                stats["requests"] += 1
                try:
                    res = conn.count(q)
                    rows.append({"topic_id": topic_id, "source": name, "count": res.count, "status": "ok",
                                 "query": res.native_query, "cost": res.cost, "error": None})
                    stats["ok"] += 1
                except (ConnectorError, BudgetExceeded) as exc:
                    rows.append({"topic_id": topic_id, "source": name, "count": None, "status": "error",
                                 "query": None, "cost": 0.0, "error": str(exc)[:300]})
                    stats["errors"] += 1
        store.save_topic_observations(radar.radar_id, model.classifier_version, week, rows, observed_at)
        stats["weeks"] += 1
    return stats


def counts_as_of(rows: Iterable[Mapping[str, Any]], t: dt.datetime) -> Dict[Tuple[str, str, dt.date], Dict[str, Any]]:
    """What was known at ``t``: per (source, topic_id, week start), the latest successful row observed by ``t``.

    ``rows`` are stored observations (any order). Rows observed after ``t`` are never read, so a replay at ``t``
    sees no later data.
    """
    t_iso = t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    out: Dict[Tuple[str, str, dt.date], Dict[str, Any]] = {}
    for r in rows:
        if r["status"] != "ok" or r["observed_at"] > t_iso:
            continue
        key = (r["source"], r["topic_id"], dt.date.fromisoformat(r["period_start"]))
        if key not in out or r["observed_at"] > out[key]["observed_at"]:
            out[key] = dict(r)
    return out


def series(known: Mapping[Tuple[str, str, dt.date], Mapping[str, Any]], source: str, topic_id: str) -> List[Dict[str, Any]]:
    """One topic's weekly counts with the field denominator, oldest week first (weeks the field lacks are left out)."""
    weeks = sorted(w for s, tp, w in known if s == source and tp == topic_id)
    out = []
    for w in weeks:
        field = known.get((source, FIELD, w))
        if field is None:
            continue
        n, total = known[(source, topic_id, w)]["count"], field["count"]
        out.append({"week": w.isoformat(), "count": n, "denominator": total,
                    "share": None if not total else n / total,
                    "observed_at": max(known[(source, topic_id, w)]["observed_at"], field["observed_at"])})
    return out


def lag_profile(rows: Iterable[Mapping[str, Any]], source: str, topic_id: str = FIELD) -> List[Dict[str, Any]]:
    """For each week observed more than once: its count at each observation, by days after the week ended.

    The ratio to the latest count shows how complete a week was when first counted (indexing lag; task 27).
    """
    by_week: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for r in rows:
        if r["status"] == "ok" and r["source"] == source and r["topic_id"] == topic_id:
            by_week[r["period_start"]].append((r["observed_at"], r["count"], r["period_end"]))  # type: ignore[arg-type]
    out = []
    for week, obs in sorted(by_week.items()):
        if len(obs) < 2:
            continue
        obs.sort()
        latest = obs[-1][1]
        end = dt.date.fromisoformat(obs[0][2])
        out.append({"week": week, "observations": [
            {"observed_at": o, "days_after_week": (dt.date.fromisoformat(o[:10]) - end).days, "count": c,
             "share_of_latest": None if not latest else round(c / latest, 4)} for o, c, _ in obs]})
    return out


__all__ = ["counting_connectors", "COUNTING_SOURCES", "DEFAULT_BUDGET", "DEFAULT_WEEKS", "FIELD", "OBSERVATIONS_VERSION", "TOPIC_TERMS",
           "complete_weeks", "counts_as_of", "lag_profile", "observe", "queries_for", "series", "topic_terms"]
