"""Explore (plan task 33): a radar's literature graph, from stored citation edges and topics only.

**Nodes** are the papers of one run (the latest by default) plus the radar's anchors: its seed papers and the papers
saved or marked relevant. Each node says why it is there (``roles``), its rank in the run, and its topic under the
radar's active topic model.

**Links** come only from what is stored:

- ``citation``: a citation edge between two nodes (OpenAlex work IDs, task 32), directed citing → cited;
- ``shared_references``: two nodes whose reference lists share at least :data:`MIN_SHARED` works.

Every link carries what it rests on (when the edge was first observed, or how many references are shared), and the
graph is built from the edges observed by ``as_of``, so a past view is reproducible (:func:`check_edges_no_leakage`).
:func:`build_subgraph` is pure: the local store (:func:`store_subgraph`) and the API (Postgres) feed it the same rows.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Sequence

from carousel.search.citations import check_edges_no_leakage

if TYPE_CHECKING:  # pragma: no cover
    from carousel.database.local import PaperStore

GRAPH_VERSION = "explore-1"
MIN_SHARED = 2
ROLE_ORDER = ("seed", "saved", "relevant", "recommended")
UNASSIGNED = "__unassigned__"


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_subgraph(nodes: Sequence[Mapping[str, Any]], edges: Iterable[Mapping[str, str]], topics: Mapping[str, str], *,
                   radar_id: str, run_id: Optional[str], as_of: dt.datetime,
                   classifier_version: Optional[str] = None) -> Dict[str, Any]:
    """``nodes``: {paper_id, title, doi, work, roles, rank, topic_id, date}; ``edges``: stored citation edges
    {citing, cited, observed_at} touching the nodes' works; ``topics``: {topic_id: label}."""
    at = _iso(as_of)
    edges = [e for e in edges if e["observed_at"] <= at]
    check_edges_no_leakage(edges, at)
    by_work = {n["work"]: n["paper_id"] for n in nodes if n.get("work")}
    refs: Dict[str, set] = {}
    links: Dict[tuple, Dict[str, Any]] = {}
    for e in sorted(edges, key=lambda e: (e["citing"], e["cited"], e["observed_at"])):
        refs.setdefault(e["citing"], set()).add(e["cited"])
        a, b = by_work.get(e["citing"]), by_work.get(e["cited"])
        if a and b and a != b and (a, b) not in links:
            links[(a, b)] = {"kind": "citation", "source": a, "target": b, "observed_at": e["observed_at"], "shared": None}
    out_links = sorted(links.values(), key=lambda x: (x["source"], x["target"]))
    works = sorted(by_work)
    for i, wa in enumerate(works):
        for wb in works[i + 1:]:
            shared = len(refs.get(wa, set()) & refs.get(wb, set()))
            if shared >= MIN_SHARED:
                a, b = sorted((by_work[wa], by_work[wb]))
                out_links.append({"kind": "shared_references", "source": a, "target": b, "observed_at": None, "shared": shared})

    degree: Dict[str, int] = {}
    for x in out_links:
        for pid in (x["source"], x["target"]):
            degree[pid] = degree.get(pid, 0) + 1
    order = {t: i for i, t in enumerate(sorted(topics, key=lambda t: (topics[t] or t)))}
    out_nodes = []
    for n in nodes:
        tid = n.get("topic_id")
        roles = [r for r in ROLE_ORDER if r in (n.get("roles") or ())]
        out_nodes.append({
            "paper_id": n["paper_id"], "title": n.get("title"), "doi": n.get("doi"), "date": n.get("date"),
            "roles": roles, "rank": n.get("rank"), "topic_id": tid if tid in topics else None,
            "topic_label": topics.get(tid) if tid else None, "citation_data": bool(n.get("work") and n["work"] in refs),
            "links": degree.get(n["paper_id"], 0),
        })
    out_nodes.sort(key=lambda n: (order.get(n["topic_id"] or "", len(order)),
                                  n["rank"] is None, n["rank"] or 0, n["paper_id"]))
    groups = [{"topic_id": t, "label": topics[t], "papers": sum(1 for n in out_nodes if n["topic_id"] == t)}
              for t in sorted(topics, key=lambda t: order[t])]
    groups = [g for g in groups if g["papers"]]
    unassigned = sum(1 for n in out_nodes if n["topic_id"] is None)
    if unassigned:
        groups.append({"topic_id": None, "label": None, "papers": unassigned})
    return {
        "radar_id": radar_id, "run_id": run_id, "as_of": at, "graph_version": GRAPH_VERSION,
        "classifier_version": classifier_version, "nodes": out_nodes, "links": out_links, "topics": groups,
        "stats": {"nodes": len(out_nodes), "citation_links": sum(1 for x in out_links if x["kind"] == "citation"),
                  "shared_reference_links": sum(1 for x in out_links if x["kind"] == "shared_references"),
                  "without_citation_data": sum(1 for n in out_nodes if not n["citation_data"]),
                  "min_shared": MIN_SHARED},
    }


def store_subgraph(st: "PaperStore", radar_id: str, seed_dois: Sequence[str] = (), run_id: Optional[str] = None,
                   as_of: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """:func:`build_subgraph` from the local store: a run's papers, the radar's anchors, its active topic model."""
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    runs = [r for r in st.list_runs(radar_id) if r["started_at"] <= _iso(as_of)]
    run = st.get_run(run_id) if run_id else (st.get_run(runs[-1]["run_id"]) if runs else None)
    roles: Dict[str, set] = {}
    ranks: Dict[str, int] = {}
    for it in (run or {}).get("items", []):
        pid = st.resolve(it["paper_id"])
        roles.setdefault(pid, set()).add("recommended")
        ranks[pid] = it["rank"]
    for doi in seed_dois:
        p = st.find("doi", doi)
        if p is not None:
            roles.setdefault(p.paper_id, set()).add("seed")
    for r in st.db.execute("SELECT paper_id, action FROM feedback_state WHERE radar_id = ? AND action IN ('save', 'relevant')",
                           (radar_id,)):
        roles.setdefault(st.resolve(r["paper_id"]), set()).add("saved" if r["action"] == "save" else "relevant")
    model = st.active_topic_model(radar_id)
    assigned = st.paper_topics(model.classifier_version) if model else {}
    nodes = []
    for pid in sorted(roles):
        p = st.get(pid)
        if p is None:
            continue
        nodes.append({"paper_id": pid, "title": p.title, "doi": p.identifiers.doi, "work": p.identifiers.openalex,
                      "roles": sorted(roles[pid]), "rank": ranks.get(pid), "date": p.earliest_public_date,
                      "topic_id": (assigned.get(pid) or {}).get("topic_id")})
    works = [n["work"] for n in nodes if n["work"]]
    return build_subgraph(nodes, st.edges_as_of(_iso(as_of), works), {t.topic_id: t.label for t in model.topics} if model else {},
                          radar_id=radar_id, run_id=(run or {}).get("run_id"), as_of=as_of,
                          classifier_version=model.classifier_version if model else None)


__all__ = ["GRAPH_VERSION", "MIN_SHARED", "build_subgraph", "store_subgraph"]
