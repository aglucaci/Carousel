"""Citation graph v1 (plan task 32): citation expansion (ranking stage A) and the graph relevance term (stage B).

**Anchors** are the papers a radar says it wants more of: its ``seed_papers`` and the papers its feedback saved or
marked relevant. For a radar with seed papers and OpenAlex among its sources, a scan

1. looks up seeds not yet in the store (or without an OpenAlex ID) by DOI, and reads every anchor's reference list;
2. asks OpenAlex for works published in the scan window that **cite** an anchor: these join the candidates
   (``retrieved_by`` ``citation``), bounded by the window, ``max_results``, and the run's credit budget;
3. reads the reference lists of the other candidates that have an OpenAlex ID and none on record yet.

Everything read is stored as append-only edges (:meth:`~carousel.database.local.PaperStore.save_citations`), each
with the time it was first observed. The graph term is then computed from :meth:`edges_as_of` the scan time only,
so a replay at ``t`` sees exactly the edges known at ``t`` (:func:`check_edges_no_leakage` enforces it).

Graph term ``g`` of a candidate, in [0, 1]:

- ``1`` if it cites an anchor or an anchor cites it (a direct link);
- otherwise ``COUPLING_WEIGHT · min(1, shared / COUPLING_SATURATION)``, where ``shared`` counts references it has in
  common with the anchors (bibliographic coupling);
- ``0`` with a status saying why when there is no citation data (no OpenAlex ID, or its references were never read).

Citation counts are never used: a link says a paper is *connected* to what the radar wants, not that it is good.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from carousel.connectors.base import ConnectorError

if TYPE_CHECKING:  # pragma: no cover
    from carousel.core.config import RadarConfig
    from carousel.database.local import PaperStore
    from carousel.feedback import Feedback

CITATIONS_VERSION = "0.1.0"
COUPLING_WEIGHT = 0.5
COUPLING_SATURATION = 5
DEFAULT_MAX_CITING = 100


class EdgeLeakageError(AssertionError):
    """A citation edge observed after the decision time was used."""


def check_edges_no_leakage(edges: Iterable[Mapping[str, str]], at: str) -> None:
    for e in edges:
        if e["observed_at"] > at:
            raise EdgeLeakageError(f"temporal leakage: edge {e['citing']} -> {e['cited']} (observed {e['observed_at']}) "
                                   f"used at {at}")


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def anchors_for(st: "PaperStore", radar: "RadarConfig", feedback: "Feedback") -> Dict[str, str]:
    """{paper_id: "seed" | "feedback"} for the anchors already in the store."""
    out: Dict[str, str] = {}
    for doi in radar.seed_papers:
        p = st.find("doi", doi)
        if p is not None:
            out[p.paper_id] = "seed"
    for pid in feedback.positive:
        try:
            out.setdefault(st.resolve(pid), "feedback")
        except Exception:
            continue
    return dict(sorted(out.items()))


def _work(st: "PaperStore", pid: str) -> Optional[str]:
    p = st.get(pid)
    return p.identifiers.openalex if p is not None else None


def gather_citations(st: "PaperStore", conn: Any, radar: "RadarConfig", feedback: "Feedback",
                     window: Tuple[dt.date, dt.date], at: dt.datetime,
                     max_citing: int = DEFAULT_MAX_CITING) -> Dict[str, Any]:
    """Steps 1-2 above. Returns ``{"hits": paper IDs that cite an anchor, "anchors": ..., "coverage": ...}``."""
    at_iso = _iso(at)
    cov: Dict[str, Any] = {"source": conn.name, "role": "citations", "status": "ok", "citations_version": CITATIONS_VERSION,
                           "requests": 0, "cost": 0.0, "cost_unit": conn.cost_unit, "references_read": 0, "new_edges": 0,
                           "retrieved": 0, "total_available": None, "truncated": False, "truncation_reason": None,
                           "errors": []}
    hits: Set[str] = set()

    def spent(res: Any) -> None:
        cov["requests"] += res.requests
        cov["cost"] = round(cov["cost"] + res.cost, 6)

    def keep(res: Any, observe: bool) -> Set[str]:
        spent(res)
        found = set()
        if observe:
            for rec in res.records:
                found.add(st.observe(rec, at).paper.paper_id)
        cov["new_edges"] += st.save_citations(res.references, at_iso, source=conn.name)
        cov["references_read"] += len(res.references)
        return {st.resolve(p) for p in found}

    try:
        # 1. seeds by DOI when the store lacks them (or their OpenAlex ID), then every anchor's references
        missing = sorted(d for d in radar.seed_papers if (p := st.find("doi", d)) is None or not p.identifiers.openalex)
        if missing:
            keep(conn.references(dois=missing), observe=True)
        anchors = anchors_for(st, radar, feedback)
        works = {pid: w for pid in anchors if (w := _work(st, pid))}
        cov["anchors"] = {"seed": sum(1 for r in anchors.values() if r == "seed"),
                          "feedback": sum(1 for r in anchors.values() if r == "feedback"),
                          "without_openalex_id": sorted(set(anchors) - set(works)),
                          "seeds_not_found": sorted(set(radar.seed_papers) - {st.get(p).identifiers.doi  # type: ignore[union-attr]
                                                                                for p, r in anchors.items() if r == "seed"})}
        read = st.reference_lists_as_of(at_iso)
        unread = sorted({w for w in works.values() if w not in read})
        if unread:
            keep(conn.references(ids=unread), observe=False)

        # 2. works in the window citing an anchor
        if works:
            res = conn.citing(sorted(set(works.values())), window[0], window[1], max_results=max_citing)
            hits = keep(res, observe=True)
            cov.update({"total_available": res.total_available, "retrieved": len(res.records),
                        "truncated": res.truncated, "truncation_reason": res.truncation_reason})
            cov["errors"] += list(res.errors)
            if res.errors:
                cov["status"] = "partial"
    except ConnectorError as exc:
        cov["status"] = "error"
        cov["errors"].append(str(exc))
        anchors = anchors_for(st, radar, feedback)
    return {"hits": hits, "anchors": anchors, "coverage": cov}


def read_candidate_references(st: "PaperStore", conn: Any, paper_ids: Iterable[str], at: dt.datetime,
                              cov: Dict[str, Any]) -> None:
    """Step 3: reference lists of candidates with an OpenAlex ID and none read yet (1 credit per 100 works)."""
    at_iso = _iso(at)
    read = st.reference_lists_as_of(at_iso)
    unread = sorted({w for pid in paper_ids if (w := _work(st, pid)) and w not in read})
    if not unread:
        return
    try:
        res = conn.references(ids=unread)
    except ConnectorError as exc:
        cov["status"] = "partial" if cov.get("status") == "ok" else cov.get("status")
        cov["errors"].append(str(exc))
        return
    cov["requests"] += res.requests
    cov["cost"] = round(cov["cost"] + res.cost, 6)
    cov["new_edges"] += st.save_citations(res.references, at_iso, source=conn.name)
    cov["references_read"] += len(res.references)


def graph_features(st: "PaperStore", anchors: Mapping[str, str], paper_ids: Iterable[str],
                   at: dt.datetime) -> Dict[str, Dict[str, Any]]:
    """The graph term of each paper from the edges observed by ``at``; ``value_seeds`` uses seed anchors only."""
    at_iso = _iso(at)
    edges = st.edges_as_of(at_iso)
    check_edges_no_leakage(edges, at_iso)
    read = st.reference_lists_as_of(at_iso)
    out_refs: Dict[str, Set[str]] = {}
    for e in edges:
        out_refs.setdefault(e["citing"], set()).add(e["cited"])
    anchor_work = {w: pid for pid in anchors if (w := _work(st, pid))}
    titles = {pid: (st.get(pid).title if st.get(pid) else None) for pid in anchors}  # type: ignore[union-attr]

    def refs_of(roles: Optional[str]) -> Set[str]:
        return set().union(*(out_refs.get(w, set()) for w, pid in anchor_work.items()
                             if roles is None or anchors[pid] == roles))

    all_refs, seed_refs = refs_of(None), refs_of("seed")
    result: Dict[str, Dict[str, Any]] = {}
    for pid in sorted(set(paper_ids)):
        w = _work(st, pid)
        if w is None:
            result[pid] = {"status": "no_openalex_id", "value": 0.0, "value_seeds": 0.0}
            continue
        cites = sorted(anchor_work[a] for a in out_refs.get(w, ()) if a in anchor_work)
        cited_by = sorted(anchor_work[a] for a in anchor_work if w in out_refs.get(a, ()))
        mine = out_refs.get(w, set())
        shared, shared_seeds = len(mine & all_refs), len(mine & seed_refs)

        def value(linked: List[str], k: int) -> float:
            return 1.0 if linked else round(COUPLING_WEIGHT * min(1.0, k / COUPLING_SATURATION), 6)

        status = "ok" if (w in read or cites or cited_by) else "references_not_read"
        link = lambda ids: [{"paper_id": a, "title": titles.get(a), "anchor": anchors[a]} for a in ids]  # noqa: E731
        result[pid] = {
            "status": status, "work": w, "cites": link(cites), "cited_by": link(cited_by), "shared_references": shared,
            "value": value(cites + cited_by, shared),
            "value_seeds": value([a for a in cites + cited_by if anchors[a] == "seed"], shared_seeds),
        }
    return result


__all__ = ["CITATIONS_VERSION", "EdgeLeakageError", "anchors_for", "check_edges_no_leakage", "gather_citations",
           "graph_features", "read_candidate_references"]
