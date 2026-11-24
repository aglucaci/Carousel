"""First Research Delta events: new publication and publication revision (plan task 21, spec §6).

*"What has meaningfully changed in my research area since my last visit?"*
Phase 2 answers it for single papers; topic events (acceleration, new
connections, landscape shifts) follow in Phase 3.

``new_publication``
    A paper this run recommends that the radar has **never observed** in an
    earlier run **and** whose earliest public date falls inside this run's
    publication window. Both conditions: an old paper that a database only just
    indexed is new to the radar but is not a new publication, and a recent
    paper the radar already saw is not news either.

``publication_revision``
    A change to a paper the radar is **watching** (every paper an earlier run
    recommended, plus papers saved or marked relevant) between the previous run
    and this one. ``kind`` says which: ``retraction``, ``correction``,
    ``expression_of_concern``, other update types as the source names them
    (``erratum``, ``addendum``, ...), or ``new_version`` when a preprint is
    linked to its publication (or the reverse).

A radar's **first run is its baseline**: with nothing earlier to compare,
it produces no events.

**Reproducible from stored runs.** Each run stores a snapshot of the state of
every watched paper (its update notices, retraction status, and linked
versions; :func:`paper_state`). Events are a pure function of a run, the
radar's earlier runs, and the two snapshots (:func:`derive_events`), so
:func:`events_for_run` recomputes exactly what was stored, and every event's
``evidence`` names the runs, papers, notices, and links it rests on.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from carousel.core.paper import CanonicalPaper, partial_date_bounds

EVENTS_VERSION = "1"
EVENT_TYPES = ("new_publication", "publication_revision")


# -------------------------
# Watched papers and their state
# -------------------------

def paper_state(paper: CanonicalPaper, version_links: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """What a revision event compares: notices, retraction, and linked versions (each sorted, JSON-ready)."""
    notices = sorted(
        ({"type": u.type, "notice_doi": u.notice_doi, "asserted_by": u.asserted_by, "via": u.via, "date": u.date}
         for u in paper.updates),
        key=lambda n: (n["type"], n["notice_doi"] or "", n["asserted_by"] or "", n["via"] or ""))
    versions = sorted(({"paper_id": v["paper_id"], "role": v["role"],
                        "linked_by": sorted({e.get("method", "source") for e in v.get("evidence", [])})}
                       for v in version_links), key=lambda v: (v["role"], v["paper_id"]))
    return {"title": paper.title, "doi": paper.identifiers.doi, "retracted": paper.is_retracted,
            "notices": notices, "versions": versions}


def watched_papers(earlier_runs: Iterable[Mapping[str, Any]], items: Iterable[Mapping[str, Any]],
                   followed: Iterable[str]) -> Set[str]:
    """Every paper an earlier run recommended, this run's items, and papers saved or marked relevant."""
    out = {it["paper_id"] for r in earlier_runs for it in r.get("items", [])}
    out |= {it["paper_id"] for it in items}
    return out | set(followed)


# -------------------------
# Deriving events
# -------------------------

def _key(*parts: Any) -> str:
    return "ev_" + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _notice_key(n: Mapping[str, Any]) -> tuple:
    return (n["type"], n.get("notice_doi"), n.get("asserted_by"))


def _in_window(date: Optional[str], start: dt.date, end: dt.date) -> bool:
    if not date:
        return False
    try:
        lo, hi = partial_date_bounds(date)
    except ValueError:
        return False
    return lo <= end and hi >= start  # a partial date ("2026-11") counts when it overlaps the window


def derive_events(run: Mapping[str, Any], earlier_runs: Sequence[Mapping[str, Any]],
                  states: Mapping[str, Mapping[str, Any]],
                  previous_states: Optional[Mapping[str, Mapping[str, Any]]],
                  resolve=lambda pid: pid) -> List[Dict[str, Any]]:
    """The events one run produces, from stored data only (a pure function).

    ``run`` and ``earlier_runs`` are stored runs with ``items`` and ``observations``; ``states`` and
    ``previous_states`` are the watched-paper snapshots of this run and of the previous run of the radar.
    ``resolve`` maps a paper ID through merges (identity when papers are never merged).
    """
    if not earlier_runs:
        return []  # the baseline run
    previous = max(earlier_runs, key=lambda r: r["started_at"])
    day = run["started_at"][:10]
    window = ((run.get("ranking") or {}).get("window") or {})
    end = dt.date.fromisoformat(window.get("end") or run["as_of"])
    start = dt.date.fromisoformat(window["start"]) if window.get("start") else end
    seen = {resolve(it["paper_id"]) for r in earlier_runs for it in r.get("items", [])}
    seen |= {resolve(o[2]) for r in earlier_runs for o in r.get("observations", [])}
    events: List[Dict[str, Any]] = []

    for it in run.get("items", []):
        pid = resolve(it["paper_id"])
        paper = (it.get("details") or {}).get("paper") or {}
        date = paper.get("earliest_public_date")
        if pid in seen or not _in_window(date, start, end):
            continue
        events.append({
            "event_id": _key("new_publication", run["radar_id"], run["run_id"], pid),
            "type": "new_publication", "kind": None, "radar_id": run["radar_id"], "run_id": run["run_id"],
            "paper_id": pid, "date": day, "events_version": EVENTS_VERSION,
            "title": paper.get("title"),
            "evidence": {
                "run_id": run["run_id"], "previous_run_id": previous["run_id"], "rank": it["rank"],
                "earliest_public_date": date, "window": {"start": start.isoformat(), "end": end.isoformat()},
                "first_seen": (it.get("details") or {}).get("first_seen"),
                "sources": (it.get("details") or {}).get("sources", []),
                "doi": paper.get("doi"), "url": paper.get("url"),
                "rule": "not observed by any earlier run of this radar, and published within this run's window",
            },
        })

    for pid in sorted(states):
        now = states[pid]
        before = (previous_states or {}).get(pid)
        if before is None:
            continue  # not watched at the previous run: no baseline to compare with
        known = {_notice_key(n) for n in before.get("notices", [])}
        for n in now.get("notices", []):
            if _notice_key(n) in known:
                continue
            kind = n["type"]
            events.append({
                "event_id": _key("publication_revision", run["radar_id"], run["run_id"], pid, list(_notice_key(n))),
                "type": "publication_revision", "kind": kind, "radar_id": run["radar_id"], "run_id": run["run_id"],
                "paper_id": pid, "date": day, "events_version": EVENTS_VERSION, "title": now.get("title"),
                "evidence": {"run_id": run["run_id"], "previous_run_id": previous["run_id"], "notice": dict(n),
                             "doi": now.get("doi"),
                             "retracted": {"before": before.get("retracted", False), "now": now.get("retracted", False)}},
            })
        if now.get("retracted") and not before.get("retracted") and not any(
                e["paper_id"] == pid and e["kind"] in ("retraction", "removal", "withdrawal") for e in events):
            events.append({  # a source flags the retraction without a notice record
                "event_id": _key("publication_revision", run["radar_id"], run["run_id"], pid, "retracted"),
                "type": "publication_revision", "kind": "retraction", "radar_id": run["radar_id"],
                "run_id": run["run_id"], "paper_id": pid, "date": day, "events_version": EVENTS_VERSION,
                "title": now.get("title"),
                "evidence": {"run_id": run["run_id"], "previous_run_id": previous["run_id"], "doi": now.get("doi"),
                             "retracted": {"before": False, "now": True}},
            })
        linked = {(v["paper_id"], v["role"]) for v in before.get("versions", [])}
        for v in now.get("versions", []):
            if (v["paper_id"], v["role"]) in linked:
                continue
            events.append({
                "event_id": _key("publication_revision", run["radar_id"], run["run_id"], pid, "version", v["paper_id"]),
                "type": "publication_revision", "kind": "new_version", "radar_id": run["radar_id"],
                "run_id": run["run_id"], "paper_id": pid, "date": day, "events_version": EVENTS_VERSION,
                "title": now.get("title"),
                "evidence": {"run_id": run["run_id"], "previous_run_id": previous["run_id"], "doi": now.get("doi"),
                             "version": dict(v)},
            })
    return events


def events_for_run(store: Any, run_id: str) -> List[Dict[str, Any]]:
    """Recompute a stored run's events from the store's runs and snapshots (what :func:`derive_events` saw)."""
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    earlier = [store.get_run(r["run_id"]) for r in store.list_runs(run["radar_id"]) if r["started_at"] < run["started_at"]]
    previous = max(earlier, key=lambda r: r["started_at"]) if earlier else None
    return derive_events(run, earlier, store.paper_states(run_id),
                         store.paper_states(previous["run_id"]) if previous else None, resolve=store.resolve)


__all__ = ["EVENTS_VERSION", "EVENT_TYPES", "derive_events", "events_for_run", "paper_state", "watched_papers"]
