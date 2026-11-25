"""A topic map shared across radars (plan task 34), and the field distance it gives.

Per-radar topics (task 24) describe one radar's field; they cannot say that a paper comes from a *distant* field,
which Method Transfer (task 35) needs. The **shared map** is one more classifier, fitted with the same algorithm,
versioning, and refit mapping as task 24 (:func:`carousel.intelligence.topics.fit`), on papers from every radar:

- **Corpus**: each radar's most recently observed papers, up to :data:`PER_RADAR` each, interleaved radar by radar up
  to :data:`MAX_SHARED`. A radar's observed papers are what its own source searches returned, so this is a bounded
  sample of each radar's field; fitting sends no requests.
- **Storage**: a topic model like any other, under the reserved radar ID :data:`SHARED_MAP`, so a refit gets a new
  ``classifier_version`` and an explicit old→new mapping. After a fit, every radar's papers are assigned to it, and
  each scan assigns its new papers (:func:`assign_to_shared`).
- **Crosswalk**: each per-radar topic is mapped to the shared topics its papers fall in (shared members, their share,
  centroid similarity), with a primary shared topic: ``within`` when most of its papers are in one shared topic,
  ``spans`` otherwise.
- **Field distance** between two papers or topics: ``1 − cos`` between the centroids of the shared topics they sit in
  (0 within one shared topic). A paper sits in its assigned shared topic or, when unassigned, the nearest one; a radar
  topic in its primary shared topic.

Per-radar models, their observations, and their trends are not touched: the shared map only reads them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from carousel.intelligence.topics import (MIN_FIT_PAPERS, SAME_SHARE, TopicModel, _dot, _normalize,
                                          assign_papers, fit)

SHARED_MAP = "__shared__"
SHARED_K_MAX = 24
PER_RADAR = 300
MAX_SHARED = 1500


def radar_ids(store: Any) -> List[str]:
    return [r[0] for r in store.db.execute("SELECT DISTINCT radar_id FROM recommendation_runs ORDER BY radar_id")
            if r[0] != SHARED_MAP]


def shared_paper_ids(store: Any, per_radar: int = PER_RADAR, limit: int = MAX_SHARED) -> Tuple[List[str], Dict[str, int]]:
    """The fit corpus's paper IDs (interleaved radar by radar, newest first) and how many come from each radar."""
    lists = {r: store.radar_paper_ids(r)[:per_radar] for r in radar_ids(store)}
    chosen: List[str] = []
    seen = set()
    from_radar: Dict[str, int] = {r: 0 for r in lists}
    for i in range(max((len(v) for v in lists.values()), default=0)):
        for r in sorted(lists):
            if i < len(lists[r]) and lists[r][i] not in seen and len(chosen) < limit:
                seen.add(lists[r][i])
                chosen.append(lists[r][i])
                from_radar[r] += 1
    return chosen, from_radar


def fit_shared(store: Any, embedder: Any, fitted_at: str, k: Optional[int] = None, per_radar: int = PER_RADAR,
               limit: int = MAX_SHARED) -> Tuple[TopicModel, bool]:
    """Fit the shared map (mapped to the active one, if any), make it active, and assign every radar's papers.

    Returns ``(model, changed)``; the same papers, embedder, and settings give the same version (``changed`` False).
    """
    from carousel.search.embeddings import ensure_embeddings
    from carousel.search.lexical import paper_text

    ids, from_radar = shared_paper_ids(store, per_radar, limit)
    papers = [p for p in (store.get(pid) for pid in ids) if p is not None]
    vectors, _ = ensure_embeddings(store, papers, embedder)
    corpus = [(p.paper_id, vectors[p.paper_id], paper_text(p)) for p in papers if p.paper_id in vectors]
    if len(corpus) < MIN_FIT_PAPERS:
        raise ValueError(f"the shared map needs at least {MIN_FIT_PAPERS} papers with embeddings (got {len(corpus)})")
    previous = store.active_topic_model(SHARED_MAP)
    model = fit(SHARED_MAP, corpus, embedder.model_version, fitted_at, k=k, k_max=SHARED_K_MAX,
                previous=previous if previous and previous.embedder == embedder.model_version else None)
    if previous is not None and model.classifier_version == previous.classifier_version:
        return previous, False
    if previous is not None and model.previous_version is None:
        model.previous_version = previous.classifier_version  # another embedder: not comparable, so no mapping
    model.params["corpus_radars"] = from_radar  # not part of the version: the corpus IDs already are
    store.save_topic_model(model)
    at = dt.datetime.fromisoformat(fitted_at.replace("Z", "+00:00"))
    for r in radar_ids(store):
        assign_papers(store, model, embedder, store.radar_paper_ids(r), at)
    return model, True


def assign_to_shared(store: Any, embedder: Any, paper_ids: List[str], at: Any) -> Optional[Dict[str, Any]]:
    """The scan hook: place new papers on the shared map, if there is one under this embedder."""
    model = store.active_topic_model(SHARED_MAP)
    if model is None or embedder is None or model.embedder != embedder.model_version:
        return None
    return {"classifier_version": model.classifier_version, **assign_papers(store, model, embedder, paper_ids, at)}


def crosswalk(store: Any, radar_id: str, shared: Optional[TopicModel] = None) -> Dict[str, Any]:
    """Each topic of the radar's active model, mapped to the shared topics its papers are in."""
    shared = shared or store.active_topic_model(SHARED_MAP)
    local = store.active_topic_model(radar_id)
    if shared is None or local is None:
        raise LookupError("no shared map yet (carousel topicmap fit)" if shared is None
                          else f"radar {radar_id!r} has no topic model yet")
    placed = store.paper_topics(shared.classifier_version)
    mine = store.paper_topics(local.classifier_version)
    comparable = shared.embedder == local.embedder
    labels = {t.topic_id: t.label for t in shared.topics}
    rows = []
    for t in local.topics:
        members = sorted(p for p, v in mine.items() if v["topic_id"] == t.topic_id)
        counts: Dict[str, int] = {}
        for p in members:
            st = (placed.get(p) or {}).get("topic_id")
            if st:
                counts[st] = counts.get(st, 0) + 1
        n_placed = sum(1 for p in members if p in placed)
        sims = {s.topic_id: round(_dot(t.centroid, s.centroid), 6) for s in shared.topics} if comparable else {}
        links = sorted(({"shared_topic": s, "label": labels[s], "shared_members": c,
                         "member_share": round(c / len(members), 6) if members else 0.0, "centroid_similarity": sims.get(s)}
                        for s, c in counts.items()), key=lambda x: (-x["shared_members"], -(x["centroid_similarity"] or 0), x["shared_topic"]))
        primary = links[0]["shared_topic"] if links else (max(sims, key=lambda s: (sims[s], s)) if sims else None)
        relation = ("within" if links and links[0]["member_share"] >= SAME_SHARE else "spans") if links else "by_centroid"
        rows.append({"radar_topic": t.topic_id, "label": t.label, "members": len(members), "placed": n_placed,
                     "primary": primary, "primary_label": labels.get(primary) if primary else None,
                     "relation": relation if primary else "unmapped", "shared": links})
    return {"radar_id": radar_id, "classifier_version": local.classifier_version,
            "shared_version": shared.classifier_version, "comparable": comparable, "topics": rows}


def _paper_position(store: Any, shared: TopicModel, paper_id: str) -> Tuple[Optional[str], str]:
    pid = store.resolve(paper_id)
    placed = (store.paper_topics(shared.classifier_version).get(pid) or {}).get("topic_id")
    if placed:
        return placed, "assigned"
    vec = store.get_embeddings([pid], shared.embedder).get(pid)
    if vec is None or not shared.topics:
        return None, "no_embedding"
    v = _normalize(vec[1])
    best = max(shared.topics, key=lambda t: (_dot(v, t.centroid), t.topic_id))
    return best.topic_id, "nearest"


def field_distance(store: Any, a: str, b: str, radar_id: Optional[str] = None,
                   shared: Optional[TopicModel] = None) -> Dict[str, Any]:
    """Distance between two papers or topics on the shared map: ``1 − cos`` of their shared topics' centroids.

    ``a`` and ``b`` are paper IDs, shared topic IDs, or (with ``radar_id``) that radar's topic IDs.
    """
    shared = shared or store.active_topic_model(SHARED_MAP)
    if shared is None:
        raise LookupError("no shared map yet (carousel topicmap fit)")
    walk = {r["radar_topic"]: r for r in crosswalk(store, radar_id, shared)["topics"]} if radar_id else {}

    def position(x: str) -> Tuple[Optional[str], str]:
        if shared.topic(x) is not None:
            return x, "shared_topic"
        if x in walk:
            return walk[x]["primary"], f"radar_topic_{walk[x]['relation']}"
        return _paper_position(store, shared, x)

    (ta, how_a), (tb, how_b) = position(a), position(b)
    if ta is None or tb is None:
        return {"distance": None, "a": {"id": a, "shared_topic": ta, "basis": how_a},
                "b": {"id": b, "shared_topic": tb, "basis": how_b}, "shared_version": shared.classifier_version}
    ca, cb = shared.topic(ta), shared.topic(tb)
    d = 0.0 if ta == tb else round(max(0.0, 1.0 - _dot(ca.centroid, cb.centroid)), 6)  # type: ignore[union-attr]
    return {"distance": d, "same_topic": ta == tb, "shared_version": shared.classifier_version,
            "a": {"id": a, "shared_topic": ta, "label": ca.label, "basis": how_a},  # type: ignore[union-attr]
            "b": {"id": b, "shared_topic": tb, "label": cb.label, "basis": how_b}}  # type: ignore[union-attr]


__all__ = ["MAX_SHARED", "PER_RADAR", "SHARED_K_MAX", "SHARED_MAP", "assign_to_shared", "crosswalk", "field_distance",
           "fit_shared", "radar_ids", "shared_paper_ids"]
