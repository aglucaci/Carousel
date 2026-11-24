"""Topic events (plan task 28): Research Delta beyond single papers, derived from stored data only.

Like the paper events of task 21, topic events are a pure function of what the
store holds at a time ``as_of`` (topic models and their refit mappings, topic
assignments, papers, and topic observations made by ``as_of``), so
:func:`derive_topic_events` recomputes exactly what was stored. Every event
names the papers it rests on (at least one, or it isn't reported) and the
evidence behind it.

``topic_acceleration``
    The trend model (task 26) flags the topic in a source, and every artifact
    check (task 27) passed. Evidence: the source, rate ratio and interval, q
    value, recent against expected counts, the weekly counts with denominators,
    the checks, and the topic's papers in the recent weeks.
``new_methodological_connection``
    One of the radar's method terms appears in at least ``MIN_CONNECTION_PAPERS``
    of a topic's recent papers (the last ``RECENT_DAYS``) and in none of its
    earlier ones, of which there are at least ``MIN_EARLIER_PAPERS`` (so "new" means
    something). Evidence: the papers, and how many earlier ones lacked it.
``research_landscape_shift``
    A refit changed the topic map: a topic split, two or more merged, a new one
    appeared, or one was retired (task 24's mapping). Evidence: the mapping row and
    the topic's papers.

Event IDs are stable (the same flag in the same evaluation week, connection,
or mapping gives the same ID), so repeated scans don't repeat an event. Topic
IDs are lineages that survive refits while a topic stays the same (task 24),
so a finding recomputed after such a refit keeps its ID too.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

from carousel.core.paper import partial_date_bounds

TOPIC_EVENTS_VERSION = "topic-events-1"
TOPIC_EVENT_TYPES = ("topic_acceleration", "new_methodological_connection", "research_landscape_shift")
RECENT_DAYS = 28
MIN_CONNECTION_PAPERS = 2
MIN_EARLIER_PAPERS = 5
MAX_EVIDENCE_PAPERS = 10


def _key(*parts: Any) -> str:
    return "ev_" + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _paper(store: Any, pid: str, confidence: Optional[float] = None) -> Dict[str, Any]:
    p = store.get(pid)
    return {"paper_id": pid, "title": p.title if p else None, "doi": p.identifiers.doi if p else None,
            "earliest_public_date": p.earliest_public_date if p else None,
            **({"confidence": round(confidence, 4)} if confidence is not None else {})}


def _date(store: Any, pid: str) -> Optional[dt.date]:
    p = store.get(pid)
    if p is None or not p.earliest_public_date:
        return None
    try:
        return partial_date_bounds(p.earliest_public_date)[0]
    except ValueError:
        return None


def _event(kind: str, radar_id: str, run_id: str, day: str, title: str, papers: List[Dict[str, Any]],
           evidence: Dict[str, Any], key: Sequence[Any], sub: Optional[str] = None) -> Dict[str, Any]:
    return {"event_id": _key(kind, radar_id, *key), "type": kind, "kind": sub, "radar_id": radar_id, "run_id": run_id,
            "paper_id": papers[0]["paper_id"], "date": day, "title": title, "events_version": TOPIC_EVENTS_VERSION,
            "evidence": {**evidence, "title": title, "papers": papers}}


def derive_topic_events(store: Any, radar: Any, as_of: dt.datetime, run_id: str = "") -> Dict[str, Any]:
    """The radar's topic events as of ``as_of``: ``{events, withheld, notes}`` (withheld: no traceable paper)."""
    model = store.active_topic_model(radar.radar_id)
    out: Dict[str, Any] = {"events": [], "withheld": [], "notes": []}
    if model is None:
        out["notes"].append("no topic model yet")
        return out
    day = as_of.astimezone(dt.timezone.utc).date().isoformat()
    placed = store.paper_topics(model.classifier_version)
    labels = {t.topic_id: t.label for t in model.topics}
    by_topic: Dict[str, List[tuple]] = {}
    for pid, v in placed.items():
        if v["topic_id"]:
            by_topic.setdefault(v["topic_id"], []).append((pid, v["confidence"]))

    def emit(e: Optional[Dict[str, Any]], reason: str, what: Dict[str, Any]) -> None:
        if e is None:
            out["withheld"].append({**what, "reason": reason})
        else:
            out["events"].append(e)

    # ---- topic_acceleration ----
    try:
        from carousel.intelligence.checks import checked_trends, store_topic_papers
        from carousel.intelligence.observations import counts_as_of, series

        rows = store.topic_observations(radar.radar_id, model.classifier_version)
        cutoff = as_of.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        sources = sorted({r["source"] for r in rows if r["observed_at"] <= cutoff})
        lookup = store_topic_papers(store, model.classifier_version)
        for src in sources:
            rep = checked_trends(rows, list(labels), src, as_of, classifier_version=model.classifier_version,
                                 all_rows=store.topic_observations(radar.radar_id), topic_papers=lookup)
            for t in rep["topics"]:
                if not t["flagged"]:
                    continue
                weekly = [x for x in series(counts_as_of(rows, as_of), src, t["topic_id"])
                          if x["week"] <= rep["evaluated_through"]]
                recent_weeks = weekly[-rep["criteria"]["recent_weeks"]:]  # the weeks the model tested
                start = dt.date.fromisoformat(recent_weeks[0]["week"])
                through = dt.date.fromisoformat(recent_weeks[-1]["week"]) + dt.timedelta(days=6)
                conf = dict(by_topic.get(t["topic_id"], []))
                papers = sorted(lookup(t["topic_id"], start, through), key=lambda x: (-conf.get(x[0], 0), x[0]))
                shown = [_paper(store, pid, conf.get(pid)) for pid, _ in papers[:MAX_EVIDENCE_PAPERS]]
                evidence = {"source": src, "classifier_version": model.classifier_version,
                            "trend_model_version": rep["trend_model_version"], "checks_version": rep["checks_version"],
                            "evaluated_through": rep["evaluated_through"], "recent_window": [start.isoformat(), through.isoformat()],
                            "rate_ratio": t["rate_ratio"], "ci": [t["ci_low"], t["ci_high"]], "p_value": t["p_value"],
                            "q_value": t["q_value"], "recent_count": t["recent_count"], "expected_recent": t["expected_recent"],
                            "method": t["method"], "baseline": t["baseline"], "smoothed_share": t["smoothed_share"],
                            "checks": rep["checks"] + t.get("checks", []), "criteria": rep["criteria"],
                            "papers_in_window": len(papers), "baseline_share": t.get("baseline_share"),
                            "weekly": weekly}
                emit(_event("topic_acceleration", radar.radar_id, run_id, day, labels[t["topic_id"]], shown, evidence,
                            (src, t["topic_id"], rep["evaluated_through"]))
                     if shown else None, "no traceable paper in the store for the recent weeks",
                     {"type": "topic_acceleration", "topic_id": t["topic_id"], "source": src})
    except ImportError:
        out["notes"].append('topic acceleration needs the [trends] extra')
    except Exception as exc:  # the trend model being unavailable must not hide the other events
        if type(exc).__name__ == "TrendsUnavailable":
            out["notes"].append(str(exc))
        else:
            raise

    # ---- new_methodological_connection ----
    from carousel.search.lexical import matched_terms

    terms = radar.method_terms()
    since = as_of.astimezone(dt.timezone.utc).date() - dt.timedelta(days=RECENT_DAYS)
    if terms:
        for tid, members in sorted(by_topic.items()):
            recent, earlier = [], []
            for pid, conf in members:
                d = _date(store, pid)
                if d is None or d > as_of.astimezone(dt.timezone.utc).date():
                    continue
                (recent if d > since else earlier).append((pid, conf))
            if len(earlier) < MIN_EARLIER_PAPERS:
                continue
            uses: Dict[str, List[tuple]] = {}
            seen_before = set()
            for pid, conf in earlier:
                seen_before.update(matched_terms(store.get(pid), terms))
            for pid, conf in recent:
                for term in matched_terms(store.get(pid), terms):
                    if term not in seen_before:
                        uses.setdefault(term, []).append((pid, conf))
            for term, ps in sorted(uses.items()):
                if len(ps) < MIN_CONNECTION_PAPERS:
                    continue
                shown = [_paper(store, pid, conf) for pid, conf in sorted(ps, key=lambda x: (-x[1], x[0]))][:MAX_EVIDENCE_PAPERS]
                evidence = {"method_term": term, "topic_id": tid, "classifier_version": model.classifier_version,
                            "recent_window": [since.isoformat(), day], "recent_papers_with_term": len(ps),
                            "earlier_papers_without_term": len(earlier)}
                out["events"].append(_event("new_methodological_connection", radar.radar_id, run_id, day,
                                            f"{term} → {labels.get(tid, tid)}", shown, evidence,
                                            (tid, term), sub=term))

    # ---- research_landscape_shift ----
    for row in model.mapping:
        if row["relation"] == "same":
            continue
        tid = row["to_topic"] or row["from_topic"]
        if row["relation"] == "retired":
            prev = store.topic_model(model.previous_version) if model.previous_version else None
            members = [(p, 0.0) for p, t in (prev.members.items() if prev else []) if t == row["from_topic"]]
            label = (prev.topic(row["from_topic"]).label if prev and prev.topic(row["from_topic"]) else row["from_topic"])
        else:
            members = by_topic.get(row["to_topic"], [])
            label = labels.get(row["to_topic"], row["to_topic"])
        shown = [_paper(store, pid, conf or None) for pid, conf in sorted(members, key=lambda x: (-x[1], x[0]))][:MAX_EVIDENCE_PAPERS]
        evidence = {"relation": row["relation"], "from_topic": row["from_topic"], "to_topic": row["to_topic"],
                    "from_version": model.previous_version, "to_version": model.classifier_version,
                    "shared_members": row["shared_members"], "centroid_similarity": row["centroid_similarity"]}
        emit(_event("research_landscape_shift", radar.radar_id, run_id, model.fitted_at[:10], label, shown, evidence,
                    (model.classifier_version, row["relation"], row["from_topic"], row["to_topic"]), sub=row["relation"])
             if shown else None, "the topic has no papers", {"type": "research_landscape_shift", "topic_id": tid})
    stamp = as_of.astimezone(dt.timezone.utc).isoformat()
    for e in out["events"]:
        e["evidence"]["as_of"] = stamp  # what the evidence was computed from: recompute with the same as_of
    out["events"].sort(key=lambda e: (e["type"], e["kind"] or "", e["event_id"]))
    return out


__all__ = ["MIN_CONNECTION_PAPERS", "MIN_EARLIER_PAPERS", "RECENT_DAYS", "TOPIC_EVENTS_VERSION", "TOPIC_EVENT_TYPES",
           "derive_topic_events"]
