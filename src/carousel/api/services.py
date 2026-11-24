"""Queries and commands over the Postgres schema, shared by the API and the scan worker.

Everything here takes a SQLAlchemy ``Session`` and returns plain dicts, so the
API layer only maps them to response models.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from carousel.core.config import RadarConfig
from carousel.database import models as m
from carousel.feedback import ACTIONS
from carousel.workers import schedule as sched


class NotFound(LookupError):
    pass


# -------------------------
# Radars
# -------------------------

def upsert_radar(s: Session, cfg: RadarConfig, *, owner_id: Optional[str] = None,
                 schedule: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
    """Create a radar, or add a new version if its config changed. Returns (radar, created).

    ``schedule`` ('daily' | 'weekly') is set when given; ``None`` leaves an existing radar's schedule alone.
    """
    sched.validate(schedule)
    rid = cfg.radar_id or "radar"
    radar = s.get(m.Radar, rid)
    created = radar is None
    if radar is None:
        radar = m.Radar(id=rid, title=cfg.radar_name, description=cfg.description, owner_id=owner_id, schedule=schedule)
        s.add(radar)
    else:
        radar.title, radar.description = cfg.radar_name, cfg.description
        if schedule is not None:
            radar.schedule = schedule
        radar.updated_at = func.now()
    s.flush()
    h = cfg.config_hash()
    version = s.scalars(select(m.RadarVersion).where(m.RadarVersion.radar_id == rid, m.RadarVersion.config_hash == h)).first()
    if version is None:
        s.add(m.RadarVersion(radar_id=rid, config_hash=h, schema_version=cfg.schema_version,
                             config=cfg.model_dump(mode="json")))
    elif version.config is None:  # imported from a store, which only knew the hash
        version.config = cfg.model_dump(mode="json")
    s.flush()
    return radar_detail(s, rid), created


def _slug(text_: str) -> str:
    return "-".join("".join(ch if ch.isalnum() else " " for ch in text_.lower()).split())[:60].strip("-") or "radar"


def propose_radar(s: Session, *, topic: str, research_question: str = "", description: str = "",
                  terms: Optional[List[str]] = None, exclude: Optional[List[str]] = None,
                  methods: Optional[List[str]] = None, seed_papers: Optional[List[str]] = None,
                  publication_types: Optional[List[str]] = None, sources: Optional[List[str]] = None,
                  mode: str = "balanced", window_days: int = 90, as_of: Optional[dt.date] = None) -> Dict[str, Any]:
    """Turn a topic (and any options) into a radar config plus the exact searches it will send.

    Nothing is saved: the caller shows the strategy, and creates the radar with
    the returned config once the user approves it. Deterministic (no model calls).
    Raises ``ValueError`` (user-facing) if the inputs don't make a valid config.
    """
    from carousel.connectors import CONNECTORS, VERIFIERS, SearchQuery
    from carousel.connectors.openalex import OpenAlexConnector, build_search
    from carousel.connectors.arxiv import build_query as arxiv_query
    from carousel.connectors.europepmc import build_query as epmc_query
    from carousel.connectors.pubmed import build_term

    topic = topic.strip()
    if not topic:
        raise ValueError("topic is required")
    base = _slug(topic)
    rid, n = base, 2
    while s.get(m.Radar, rid) is not None:
        rid, n = f"{base[:56]}-{n}", n + 1
    try:
        cfg = RadarConfig.model_validate({
            "radar_id": rid, "radar_name": topic, "description": description.strip(),
            "research_question": research_question.strip(), "queries": [t for t in (terms or []) if t.strip()] or [topic],
            "exclude": list(exclude or []), "methods_profile": list(methods or []), "seed_papers": list(seed_papers or []),
            "publication_types": list(publication_types or []),
            "sources": list(sources or ["pubmed", "openalex", "europepmc", "crossref"]),
            "mode": mode, "filters": {"date_range": f"last_{int(window_days)}_days"},
        })
    except Exception as exc:  # pydantic's message is already user-facing
        raise ValueError(str(exc)) from None

    if all(src.value in VERIFIERS for src in cfg.sources):
        raise ValueError("choose at least one source to search: Crossref only checks the papers the searches find")
    as_of = as_of or dt.datetime.now(dt.timezone.utc).date()
    start = as_of - dt.timedelta(days=cfg.filters.days - 1)
    q = SearchQuery(terms=list(cfg.queries), exclude=list(cfg.exclude), start=start, end=as_of)
    strategy: List[Dict[str, Any]] = []
    for src in cfg.sources:
        name = src.value
        if name == "pubmed":
            strategy.append({"source": name, "status": "ready", "query": build_term(q),
                             "filters": "publication date in the window", "notes": []})
        elif name == "openalex":
            notes = [] if os.environ.get("OPENALEX_API_KEY") else [
                "No OPENALEX_API_KEY on this server: each scan reads only the first page (up to 100 works)."]
            strategy.append({"source": name, "status": "ready", "query": build_search(q),
                             "filters": OpenAlexConnector(api_key="")._filter(q), "notes": notes})
        elif name == "europepmc":
            strategy.append({"source": name, "status": "ready", "query": epmc_query(q),
                             "filters": "titles and abstracts; MEDLINE, PubMed Central, and preprints; first publication date in the window",
                             "notes": ["For recommended papers, keeps open-access full texts under a Creative Commons licence "
                                       "(with that licence) and Europe PMC's text-mined annotations."]})
        elif name == "arxiv":
            strategy.append({"source": name, "status": "ready", "query": arxiv_query(q),
                             "filters": "titles and abstracts; first submission date in the window",
                             "notes": ["Preprints in physics, mathematics, computer science, quantitative biology, and "
                                       "statistics. arXiv allows one request every three seconds, so large scans are slower.",
                                       "A preprint's journal version, when arXiv lists one, is linked, not merged."]})
        elif name in VERIFIERS:
            strategy.append({"source": name, "status": "ready", "query": None,
                             "filters": "looks up the DOIs of the papers the searches find",
                             "notes": ["Checks each DOI for corrections, retractions (including Retraction Watch), and "
                                       "preprint links. Retracted papers are flagged and not recommended. It does not search."]})
        else:
            strategy.append({"source": name, "status": "ready" if name in CONNECTORS else "not_available",
                             "query": None, "filters": None,
                             "notes": ["No connector for this source yet (Phase 2); it will be listed as skipped."]})

    notes = []
    if not cfg.method_terms():
        notes.append("No methods given, so method match is inactive and relevance and novelty decide the ranking.")
    if not cfg.seed_papers:
        notes.append("No seed papers: the interest profile comes from the topic, question, and search terms.")
    return {
        "config": cfg.model_dump(mode="json"),
        "window": {"start": start, "end": as_of, "days": cfg.filters.days},
        "strategy": strategy,
        "ranking": {"mode": cfg.mode.value, "signals": ["relevance", "novelty", "method_match"],
                    "weights": {k: v for k, v in cfg.ranking.as_dict().items()
                                if k in ("relevance", "novelty", "method_match")}},
        "notes": notes,
    }


def radar_config(s: Session, radar_id: str) -> RadarConfig:
    """The radar's current config (its most recent version that has one)."""
    v = s.scalars(select(m.RadarVersion).where(m.RadarVersion.radar_id == radar_id, m.RadarVersion.config.isnot(None))
                  .order_by(m.RadarVersion.effective_at.desc(), m.RadarVersion.id.desc())).first()
    if v is None:
        if s.get(m.Radar, radar_id) is None:
            raise NotFound(f"radar {radar_id!r}")
        raise NotFound(f"radar {radar_id!r} has no stored configuration; POST it to /api/v1/radars")
    return RadarConfig.model_validate(v.config)


def _run_summary(r: m.RecommendationRun) -> Dict[str, Any]:
    return {
        "run_id": r.id, "radar_id": r.radar_id, "executed_at": r.executed_at, "finished_at": r.finished_at,
        "as_of": r.as_of, "status": r.status, "ranking_version": r.ranking_version, "model_version": r.model_version,
        "mode": r.mode, "config_hash": r.config_hash, "n_observed": r.n_observed, "n_candidates": r.n_candidates,
        "n_items": r.n_items, "coverage": r.coverage, "notes": r.notes, "trigger": r.trigger,
        "explanation_version": r.explanation_version,
        "feedback_effect": ((r.ranking or {}).get("stats") or {}).get("feedback_effect"),
        "profile": ((r.ranking or {}).get("stats") or {}).get("profile"),
    }


def radar_detail(s: Session, radar_id: str) -> Dict[str, Any]:
    radar = s.get(m.Radar, radar_id)
    if radar is None:
        raise NotFound(f"radar {radar_id!r}")
    try:
        cfg: Optional[Dict[str, Any]] = radar_config(s, radar_id).model_dump(mode="json")
    except NotFound:
        cfg = None
    latest = s.scalars(select(m.RecommendationRun).where(m.RecommendationRun.radar_id == radar_id)
                       .order_by(m.RecommendationRun.executed_at.desc())).first()
    n_runs = s.scalar(select(func.count()).select_from(m.RecommendationRun).where(m.RecommendationRun.radar_id == radar_id))
    return {
        "radar_id": radar.id, "title": radar.title, "description": radar.description, "state": radar.state,
        "schedule": radar.schedule, "owner_id": radar.owner_id, "created_at": radar.created_at,
        "updated_at": radar.updated_at, "config": cfg, "n_runs": int(n_runs or 0),
        "latest_run": _run_summary(latest) if latest else None,
        "next_scan_at": sched.next_scan_at(radar.schedule, radar.state, latest.executed_at if latest else None,
                                           dt.datetime.now(dt.timezone.utc)),
        "digest_emails": list(radar.digest_emails or []),
    }


def update_radar(s: Session, radar_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Change a radar's ``schedule`` (daily | weekly | None) and/or ``state`` (active | paused).

    Pausing stops scheduled scans only; runs, items, and feedback are kept, and resuming continues from them.
    """
    radar = s.get(m.Radar, radar_id)
    if radar is None:
        raise NotFound(f"radar {radar_id!r}")
    if "schedule" in changes:
        radar.schedule = sched.validate(changes["schedule"])
    if "state" in changes:
        if changes["state"] not in ("active", "paused"):
            raise ValueError("state must be 'active' or 'paused'")
        radar.state = changes["state"]
    if "digest_emails" in changes:
        radar.digest_emails = clean_emails(changes["digest_emails"] or [])
    radar.updated_at = func.now()
    s.flush()
    s.refresh(radar)
    return radar_detail(s, radar_id)


def list_radars(s: Session) -> List[Dict[str, Any]]:
    return [radar_detail(s, rid) for rid in s.scalars(select(m.Radar.id).order_by(m.Radar.id))]


# -------------------------
# Runs and recommendations
# -------------------------

def list_runs(s: Session, radar_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    if s.get(m.Radar, radar_id) is None:
        raise NotFound(f"radar {radar_id!r}")
    runs = s.scalars(select(m.RecommendationRun).where(m.RecommendationRun.radar_id == radar_id)
                     .order_by(m.RecommendationRun.executed_at.desc()).limit(limit))
    return [_run_summary(r) for r in runs]


def _run(s: Session, radar_id: str, run_id: Optional[str]) -> m.RecommendationRun:
    q = select(m.RecommendationRun).where(m.RecommendationRun.radar_id == radar_id)
    q = q.where(m.RecommendationRun.id == run_id) if run_id else q.order_by(m.RecommendationRun.executed_at.desc())
    run = s.scalars(q).first()
    if run is None:
        if s.get(m.Radar, radar_id) is None:
            raise NotFound(f"radar {radar_id!r}")
        raise NotFound(f"run {run_id!r} of radar {radar_id!r}" if run_id else f"radar {radar_id!r} has no runs yet")
    return run


def get_run(s: Session, run_id: str) -> Dict[str, Any]:
    run = s.get(m.RecommendationRun, run_id)
    if run is None:
        raise NotFound(f"run {run_id!r}")
    return {**_run_summary(run), "ranking": run.ranking}


def _identifiers(s: Session, paper_ids: List[str]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for t, v, pid in s.execute(select(m.PaperIdentifier.identifier_type, m.PaperIdentifier.value, m.PaperIdentifier.paper_id)
                               .where(m.PaperIdentifier.paper_id.in_(paper_ids))):
        out.setdefault(pid, {})[t] = v
    return out


ARRIVALS = ("newly_published", "newly_observed", "seen_before")


def arrivals(s: Session, run: m.RecommendationRun) -> Tuple[Optional[str], Dict[str, str]]:
    """How each of the run's recommended papers arrived, relative to this radar's earlier runs.

    Returns ``(previous_run_id, {paper_id: arrival})``; with no earlier run there is nothing to
    compare with, so the mapping is empty. A paper is

    - ``seen_before`` when any earlier run of the radar observed or recommended it;
    - otherwise ``newly_published`` when its earliest public date (a lower bound for partial
      dates) falls after the previous run's ``as_of``: it did not exist at the last scan;
    - otherwise ``newly_observed``: it is older, and this radar sees it for the first time
      (late indexing, a source added, a changed query, or the window moving).
    """
    earlier = (select(m.RecommendationRun.id).where(m.RecommendationRun.radar_id == run.radar_id,
                                                    m.RecommendationRun.executed_at < run.executed_at))
    previous = s.scalars(select(m.RecommendationRun).where(m.RecommendationRun.id.in_(earlier))
                         .order_by(m.RecommendationRun.executed_at.desc())).first()
    if previous is None:
        return None, {}
    items = dict(s.execute(select(m.RecommendationItem.paper_id, m.Paper.earliest_public_date)
                           .join(m.Paper, m.Paper.id == m.RecommendationItem.paper_id)
                           .where(m.RecommendationItem.run_id == run.id)).all())
    seen = set(s.scalars(select(m.RunObservation.paper_id).where(m.RunObservation.run_id.in_(earlier),
                                                                 m.RunObservation.paper_id.in_(items))))
    seen |= set(s.scalars(select(m.RecommendationItem.paper_id).where(m.RecommendationItem.run_id.in_(earlier),
                                                                      m.RecommendationItem.paper_id.in_(items))))
    # Records seen earlier under a paper ID that has since been merged into one of these papers.
    seen |= set(s.scalars(select(m.Paper.merged_into).where(
        m.Paper.merged_into.in_(items),
        m.Paper.id.in_(select(m.RunObservation.paper_id).where(m.RunObservation.run_id.in_(earlier))))))
    out = {}
    for pid, published in items.items():
        if pid in seen:
            out[pid] = "seen_before"
        elif published is not None and published > previous.as_of:
            out[pid] = "newly_published"
        else:
            out[pid] = "newly_observed"
    return previous.id, out


def ranked_papers(s: Session, radar_id: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    run = _run(s, radar_id, run_id)
    previous_run_id, arrival = arrivals(s, run)
    items = s.scalars(select(m.RecommendationItem).where(m.RecommendationItem.run_id == run.id)
                      .order_by(m.RecommendationItem.rank)).all()
    state = current_feedback(s, radar_id)
    active: Dict[str, List[str]] = {}
    for action in ACTIONS:
        for pid in state.get(action, []):
            active.setdefault(pid, []).append(action)
    return {
        "run": _run_summary(run),
        "items": [{
            "rank": it.rank, "paper_id": it.paper_id, "score": it.score, "features": it.features, "flags": it.flags,
            "paper": (it.explanation or {}).get("paper") or {}, "dismissed": "dismiss" in active.get(it.paper_id, []),
            "saved": "save" in active.get(it.paper_id, []), "feedback": active.get(it.paper_id, []),
            "arrival": arrival.get(it.paper_id),
            "why": ((it.explanation or {}).get("explanation") or {}).get("summary"),
        } for it in items],
        "previous_run_id": previous_run_id,
        "arrivals": {a: sum(1 for v in arrival.values() if v == a) for a in ARRIVALS} if previous_run_id else None,
        "events": event_counts(s, run.id),
    }


def delta(s: Session, radar_id: str) -> Dict[str, Any]:
    runs = s.scalars(select(m.RecommendationRun).where(m.RecommendationRun.radar_id == radar_id)
                     .order_by(m.RecommendationRun.executed_at.desc()).limit(2)).all()
    if not runs:
        _run(s, radar_id, None)  # raises the right NotFound
    def ranks(run: m.RecommendationRun) -> Dict[str, int]:
        return dict(s.execute(select(m.RecommendationItem.paper_id, m.RecommendationItem.rank)
                              .where(m.RecommendationItem.run_id == run.id)).all())
    now = ranks(runs[0])
    if len(runs) < 2:
        return {"run_id": runs[0].id, "previous_run_id": None, "entered": sorted(now, key=now.get), "left": [], "moved": [],
                "newly_published": [], "newly_observed": []}
    before = ranks(runs[1])
    _, arrival = arrivals(s, runs[0])
    return {
        "run_id": runs[0].id, "previous_run_id": runs[1].id,
        "newly_published": sorted((p for p, a in arrival.items() if a == "newly_published"), key=now.get),
        "newly_observed": sorted((p for p, a in arrival.items() if a == "newly_observed"), key=now.get),
        "entered": sorted((p for p in now if p not in before), key=now.get),
        "left": sorted((p for p in before if p not in now), key=before.get),
        "moved": [{"paper_id": p, "rank": now[p], "previous_rank": before[p], "change": before[p] - now[p]}
                  for p in sorted((p for p in now if p in before), key=now.get) if now[p] != before[p]],
    }


# -------------------------
# Papers
# -------------------------

def resolve_paper(s: Session, paper_id: str) -> m.Paper:
    seen = set()
    p = s.get(m.Paper, paper_id)
    while p is not None and p.merged_into and p.id not in seen:
        seen.add(p.id)
        p = s.get(m.Paper, p.merged_into)
    if p is None:
        raise NotFound(f"paper {paper_id!r}")
    return p


def paper_detail(s: Session, paper_id: str) -> Dict[str, Any]:
    p = resolve_paper(s, paper_id)
    authors = s.execute(select(m.Author.name, m.Author.orcid).join(m.PaperAuthor)
                        .where(m.PaperAuthor.paper_id == p.id).order_by(m.PaperAuthor.position)).all()
    sources = s.execute(select(m.PaperSource.provider, m.PaperSource.provider_id, m.PaperSource.first_seen,
                               m.PaperSource.last_checked).where(m.PaperSource.paper_id == p.id)
                        .order_by(m.PaperSource.provider)).all()
    return {
        "paper_id": p.id, "requested_id": paper_id, "title": p.title, "abstract": p.abstract, "venue": p.venue,
        "language": p.language, "url": p.url, "publication_types": p.publication_types, "is_retracted": p.is_retracted,
        "dates": {"published": p.published, "first_online": p.first_online, "preprint_posted": p.preprint_posted,
                  "version": p.version_date, "source_updated": p.source_updated,
                  "earliest_public_date": p.earliest_public_date},
        "first_seen": p.first_seen, "last_verified": p.last_verified,
        "identifiers": _identifiers(s, [p.id]).get(p.id, {}),
        "authors": [{"name": n, "orcid": o} for n, o in authors],
        "sources": [{"provider": a, "provider_id": b, "first_seen": c, "last_checked": d} for a, b, c, d in sources],
        "provenance": (p.canonical or {}).get("provenance", {}),
        "updates": (p.canonical or {}).get("updates", []),
        "relations": (p.canonical or {}).get("relations", []),
        "license": (p.canonical or {}).get("license"),
        "open_access": (p.canonical or {}).get("open_access"),
        "texts": _texts(s, p.id),
        "versions": _versions(s, p.id),
        "extractions": _extractions(s, p.id),
        "annotations": _annotations(s, p.id),
    }


def _extractions(s: Session, paper_id: str) -> List[Dict[str, Any]]:
    """The newest grounded extraction per provider (only cited passages are stored, so every quote is checkable)."""
    rows = s.scalars(select(m.PaperExtraction).where(m.PaperExtraction.paper_id == paper_id)
                     .order_by(m.PaperExtraction.created_at.desc(), m.PaperExtraction.id.desc())).all()
    out: Dict[str, Dict[str, Any]] = {}
    for e in rows:
        out.setdefault(e.provider, {
            "provider": e.provider, "model_version": e.model_version, "schema_version": e.schema_version,
            "location": e.location, "coverage": e.coverage, "fields": e.fields, "passages": e.passages,
            "rejected": len(e.rejected or []), "created_at": e.created_at})
    return list(out.values())


def _versions(s: Session, paper_id: str) -> List[Dict[str, Any]]:
    """The paper's other versions (a preprint and its publication), each its own paper."""
    rows = s.execute(select(m.PaperVersion, m.Paper).join(m.Paper, m.Paper.id == m.PaperVersion.related_paper_id)
                     .where(m.PaperVersion.paper_id == paper_id,
                            m.PaperVersion.version_type.in_(("preprint", "published")))
                     .order_by(m.PaperVersion.version_type, m.PaperVersion.date, m.Paper.id)).all()
    ids = _identifiers(s, [r.id for _, r in rows])
    return [{"paper_id": r.id, "role": v.version_type, "title": r.title, "venue": r.venue,
             "earliest_public_date": r.earliest_public_date, "doi": ids.get(r.id, {}).get("doi"), "url": r.url,
             "is_retracted": r.is_retracted,
             "linked_by": sorted({e.get("method", "source") for e in (v.details or {}).get("evidence", [])}),
             "evidence": (v.details or {}).get("evidence", []), "linked_at": (v.details or {}).get("linked_at")}
            for v, r in rows]


def _texts(s: Session, paper_id: str) -> List[Dict[str, Any]]:
    """What texts are kept and under which licence; the text itself is not served."""
    rows = s.scalars(select(m.PaperText).where(m.PaperText.paper_id == paper_id).order_by(m.PaperText.source,
                                                                                          m.PaperText.source_id))
    return [{"source": t.source, "source_id": t.source_id, "kind": t.kind, "license": t.license,
             "license_url": t.license_url, "license_source": t.license_source, "reuse": t.reuse, "url": t.url,
             "retrieved_at": t.retrieved_at, "sections": [x.get("title", "") for x in t.sections],
             "characters": sum(len(x.get("text", "")) for x in t.sections)} for t in rows]


ANNOTATIONS_SHOWN = 50


def _annotations(s: Session, paper_id: str) -> List[Dict[str, Any]]:
    """Annotations from every article of the paper, one row per entity, most-mentioned first (top 50)."""
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in s.scalars(select(m.PaperAnnotation).where(m.PaperAnnotation.paper_id == paper_id)):
        for a in row.annotations:
            key = (a["type"], (a.get("uri") or "").lower(), a["name"].lower())
            old = merged.get(key)
            if old is None or a["mentions"] > old["mentions"]:
                merged[key] = {**a, "via": row.source}
    return sorted(merged.values(), key=lambda a: (-a["mentions"], a["type"], a["name"].lower()))[:ANNOTATIONS_SHOWN]


def explanation(s: Session, paper_id: str, radar_id: Optional[str] = None, run_id: Optional[str] = None) -> Dict[str, Any]:
    """Why a paper was recommended, from the stored run only (latest run that ranked it, unless run_id is given)."""
    p = resolve_paper(s, paper_id)
    ids = {paper_id, p.id} | set(s.scalars(select(m.Paper.id).where(m.Paper.merged_into == p.id)))
    q = (select(m.RecommendationItem, m.RecommendationRun).join(m.RecommendationRun)
         .where(m.RecommendationItem.paper_id.in_(ids)).order_by(m.RecommendationRun.executed_at.desc()))
    if radar_id:
        q = q.where(m.RecommendationRun.radar_id == radar_id)
    if run_id:
        q = q.where(m.RecommendationRun.id == run_id)
    row = s.execute(q).first()
    if row is None:
        raise NotFound(f"paper {paper_id!r} was not recommended" + (f" by radar {radar_id!r}" if radar_id else "")
                       + (f" in run {run_id!r}" if run_id else ""))
    item, run = row
    rk = run.ranking or {}
    return {
        "paper_id": p.id, "run_id": run.id, "radar_id": run.radar_id, "rank": item.rank, "score": item.score,
        "features": item.features, "details": item.explanation, "flags": item.flags,
        "ranking_version": run.ranking_version, "model_version": run.model_version, "mode": run.mode,
        "explanation_version": run.explanation_version, "explanation": (item.explanation or {}).get("explanation"),
        "weights": rk.get("weights", {}), "inactive_signals": rk.get("inactive_signals", {}),
        "source_coverage": run.coverage, "executed_at": run.executed_at,
    }


def search_papers(s: Session, q: str, limit: int = 20) -> List[Dict[str, Any]]:
    rows = s.execute(
        select(m.Paper.id, m.Paper.title, m.Paper.venue, m.Paper.earliest_public_date,
               func.ts_rank(m.Paper.search, func.websearch_to_tsquery("english", q)).label("rank"))
        .where(m.Paper.status == "active", m.Paper.search.op("@@")(func.websearch_to_tsquery("english", q)))
        .order_by(text("rank DESC"), m.Paper.id).limit(limit)).all()
    return [{"paper_id": r.id, "title": r.title, "venue": r.venue, "earliest_public_date": r.earliest_public_date,
             "rank": float(r.rank)} for r in rows]


# -------------------------
# Feedback
# -------------------------

LOCAL_USER = "local"


def ensure_user(s: Session, user_id: str) -> None:
    if s.get(m.User, user_id) is None:
        s.add(m.User(id=user_id))
        s.flush()


def record_feedback(s: Session, *, radar_id: str, paper_id: str, action: str, remove: bool = False,
                    user_id: str = LOCAL_USER, at: Optional[dt.datetime] = None) -> Dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if s.get(m.Radar, radar_id) is None:
        raise NotFound(f"radar {radar_id!r}")
    p = resolve_paper(s, paper_id)
    ensure_user(s, user_id)
    row = m.UserFeedback(user_id=user_id, radar_id=radar_id, paper_id=p.id, action=action,
                         change="removed" if remove else "added", ref=paper_id, origin="app",
                         at=at or dt.datetime.now(dt.timezone.utc))
    s.add(row)
    s.flush()
    return {"id": row.id, "radar_id": radar_id, "paper_id": p.id, "action": action, "change": row.change, "at": row.at,
            "state": current_feedback(s, radar_id)}


def _feedback_state(s: Session, radar_id: Optional[str] = None) -> Dict[Tuple[str, str, str], dt.datetime]:
    """{(radar_id, paper_id, action): when it was turned on} for every action currently on.

    Replays the append-only events in order, so the last change wins.
    """
    q = (select(m.UserFeedback.radar_id, m.UserFeedback.paper_id, m.UserFeedback.action, m.UserFeedback.change,
                m.UserFeedback.at).where(m.UserFeedback.radar_id.isnot(None))
         .order_by(m.UserFeedback.at, m.UserFeedback.id))
    if radar_id is not None:
        q = q.where(m.UserFeedback.radar_id == radar_id)
    state: Dict[Tuple[str, str, str], Optional[dt.datetime]] = {}
    for rid, pid, action, change, at in s.execute(q):
        state[(rid, pid, action)] = at if change == "added" else None
    return {k: v for k, v in state.items() if v is not None}


def current_feedback(s: Session, radar_id: str) -> Dict[str, List[str]]:
    """{action: [paper_id]}: the radar's feedback as it stands now."""
    out: Dict[str, List[str]] = {a: [] for a in ACTIONS}
    for (_, pid, action) in sorted(_feedback_state(s, radar_id)):
        out[action].append(pid)
    return out


def paper_briefs(s: Session, paper_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Card-sized summaries (title, first authors, venue, date, identifiers, URL), keyed by paper ID."""
    if not paper_ids:
        return {}
    ids = _identifiers(s, paper_ids)
    authors: Dict[str, List[str]] = {}
    for pid, name in s.execute(select(m.PaperAuthor.paper_id, m.Author.name).join(m.Author)
                               .where(m.PaperAuthor.paper_id.in_(paper_ids)).order_by(m.PaperAuthor.position)):
        authors.setdefault(pid, []).append(name)
    out = {}
    for p in s.scalars(select(m.Paper).where(m.Paper.id.in_(paper_ids))):
        out[p.id] = {"title": p.title, "authors": authors.get(p.id, []), "venue": p.venue,
                     "earliest_public_date": p.earliest_public_date.isoformat() if p.earliest_public_date else None,
                     "url": p.url, "is_retracted": p.is_retracted, **ids.get(p.id, {})}
    return out


def library(s: Session, radar_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Saved papers, most recently saved first. Merged papers are shown under the paper they merged into."""
    if radar_id is not None and s.get(m.Radar, radar_id) is None:
        raise NotFound(f"radar {radar_id!r}")
    saved = [(rid, pid, at) for (rid, pid, action), at in _feedback_state(s, radar_id).items() if action == "save"]
    resolved = {pid: resolve_paper(s, pid).id for _, pid, _ in saved}
    briefs = paper_briefs(s, sorted(set(resolved.values())))
    titles = dict(s.execute(select(m.Radar.id, m.Radar.title)).all())
    rows = [{"radar_id": rid, "radar_title": titles.get(rid), "paper_id": resolved[pid], "saved_at": at,
             "paper": briefs.get(resolved[pid], {})} for rid, pid, at in saved]
    return sorted(rows, key=lambda r: (r["saved_at"], r["radar_id"], r["paper_id"]), reverse=True)


# -------------------------
# Digests (plan task 20)
# -------------------------

EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_DIGEST_EMAILS = 20
FEED_RUNS = 20


def clean_emails(addresses: List[str]) -> List[str]:
    """Trimmed, de-duplicated recipient addresses; raises ValueError naming any that don't look like one."""
    out: List[str] = []
    bad = []
    for a in addresses:
        a = (a or "").strip()
        if not a:
            continue
        if not EMAIL.match(a):
            bad.append(a)
        elif a.lower() not in {x.lower() for x in out}:
            out.append(a)
    if bad:
        raise ValueError("not an email address: " + ", ".join(bad))
    if len(out) > MAX_DIGEST_EMAILS:
        raise ValueError(f"at most {MAX_DIGEST_EMAILS} digest recipients per radar")
    return out


def _digest(s: Session, radar: m.Radar, run: m.RecommendationRun):
    from carousel.notifications import digest_from_run

    items = s.scalars(select(m.RecommendationItem).where(m.RecommendationItem.run_id == run.id)
                      .order_by(m.RecommendationItem.rank)).all()
    previous, arrival = arrivals(s, run)
    return digest_from_run(
        {"run_id": run.id, "radar_id": run.radar_id, "as_of": run.as_of.isoformat(), "n_candidates": run.n_candidates,
         "coverage": run.coverage, "ranking": run.ranking, "explanation_version": run.explanation_version},
        [{"rank": it.rank, "paper_id": it.paper_id, "flags": it.flags, "details": it.explanation} for it in items],
        radar_name=radar.title, arrivals=arrival if previous else None, executed_at=run.executed_at,
        app_url=os.environ.get("CAROUSEL_PUBLIC_URL"), events=list_events(s, radar.id, run.id, limit=500))


def _radar(s: Session, radar_id: str) -> m.Radar:
    radar = s.get(m.Radar, radar_id)
    if radar is None:
        raise NotFound(f"radar {radar_id!r}")
    return radar


def digest_markdown(s: Session, radar_id: str, run_id: Optional[str] = None) -> str:
    from carousel.notifications import to_markdown

    return to_markdown(_digest(s, _radar(s, radar_id), _run(s, radar_id, run_id)))


def digest_feed(s: Session, radar_id: str) -> str:
    """RSS over the radar's last :data:`FEED_RUNS` runs, each paper once (from the run that first recommended it)."""
    from carousel.notifications import to_rss

    radar = _radar(s, radar_id)
    runs = s.scalars(select(m.RecommendationRun).where(m.RecommendationRun.radar_id == radar_id)
                     .order_by(m.RecommendationRun.executed_at.desc()).limit(FEED_RUNS)).all()
    base = os.environ.get("CAROUSEL_PUBLIC_URL")
    return to_rss([_digest(s, radar, r) for r in runs], radar_id=radar_id, radar_name=radar.title,
                  link=f"{base.rstrip('/')}/?radar={radar_id}" if base else None)


def _record(s: Session, radar_id: str, run_id: str, channel: str, status: str, details: Dict[str, Any],
            at: Optional[dt.datetime]) -> m.DigestRun:
    row = m.DigestRun(radar_id=radar_id, run_id=run_id, channel=channel, delivery_status=status, details=details,
                      **({"generated_at": at} if at else {}))
    s.add(row)
    s.flush()
    return row


def send_digest_emails(s: Session, radar_id: str, run_id: Optional[str] = None, *, settings: Any = None,
                       transport: Any = None, at: Optional[dt.datetime] = None) -> List[Dict[str, Any]]:
    """Email the run's digest (latest by default) to each of the radar's recipients, recording every attempt."""
    from carousel.notifications import DeliveryError, SmtpSettings, send_email, to_email

    radar = _radar(s, radar_id)
    run = _run(s, radar_id, run_id)
    digest = _digest(s, radar, run)
    try:
        settings = settings or SmtpSettings.from_env()
        config_error = None
    except DeliveryError as exc:
        config_error = str(exc)
    rows = []
    for addr in radar.digest_emails or []:
        try:
            if config_error:
                raise DeliveryError(config_error)
            send_email(to_email(digest, sender=settings.sender, recipient=addr), settings, transport)
            rows.append(_record(s, radar_id, run.id, "email", "delivered", {"recipient": addr}, at))
        except DeliveryError as exc:
            rows.append(_record(s, radar_id, run.id, "email", "failed", {"recipient": addr, "error": str(exc)}, at))
    return [_delivery(r) for r in rows]


def deliver_digests(s: Session, radar_id: str, run_id: str, *, settings: Any = None, transport: Any = None,
                    at: Optional[dt.datetime] = None) -> List[Dict[str, Any]]:
    """What a scan produces: the run's Markdown digest, the updated RSS feed, and an email per recipient.

    Every delivery is recorded in ``digest_runs``, failures included (with the reason); a failure never
    stops the others.
    """
    rows = []
    for channel, render in (("markdown", lambda: digest_markdown(s, radar_id, run_id)),
                            ("rss", lambda: digest_feed(s, radar_id))):
        try:
            rows.append(_delivery(_record(s, radar_id, run_id, channel, "delivered", {"content": render()}, at)))
        except Exception as exc:  # a rendering bug must not hide the other digests
            rows.append(_delivery(_record(s, radar_id, run_id, channel, "failed",
                                          {"error": f"{type(exc).__name__}: {exc}"}, at)))
    return rows + send_digest_emails(s, radar_id, run_id, settings=settings, transport=transport, at=at)


def _delivery(r: m.DigestRun) -> Dict[str, Any]:
    d = r.details or {}
    return {"id": r.id, "radar_id": r.radar_id, "run_id": r.run_id, "channel": r.channel, "status": r.delivery_status,
            "generated_at": r.generated_at, "recipient": d.get("recipient"), "error": d.get("error"),
            "characters": len(d["content"]) if "content" in d else None}


def digest_deliveries(s: Session, radar_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    _radar(s, radar_id)
    rows = s.scalars(select(m.DigestRun).where(m.DigestRun.radar_id == radar_id)
                     .order_by(m.DigestRun.generated_at.desc(), m.DigestRun.id.desc()).limit(limit)).all()
    return [_delivery(r) for r in rows]


# -------------------------
# Research Delta events (plan task 21)
# -------------------------

def _event_label(e: m.ResearchEvent) -> str:
    return e.type if e.type == "new_publication" else (e.kind or e.type)


def event_counts(s: Session, run_id: str) -> Dict[str, int]:
    """Events a run produced, by label: new_publication, retraction, correction, new_version, ..."""
    out: Dict[str, int] = {}
    for e in s.scalars(select(m.ResearchEvent).where(m.ResearchEvent.run_id == run_id)):
        out[_event_label(e)] = out.get(_event_label(e), 0) + 1
    return dict(sorted(out.items()))


def list_events(s: Session, radar_id: str, run_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """The radar's events, newest first, each with the paper it is about and the evidence it rests on."""
    _radar(s, radar_id)
    q = (select(m.ResearchEvent, m.Paper).join(m.Paper, m.Paper.id == m.ResearchEvent.paper_id, isouter=True)
         .where(m.ResearchEvent.radar_id == radar_id))
    if run_id:
        q = q.where(m.ResearchEvent.run_id == run_id)
    rows = s.execute(q.order_by(m.ResearchEvent.date.desc(), m.ResearchEvent.type, m.ResearchEvent.kind,
                                m.ResearchEvent.id).limit(limit)).all()
    ids = _identifiers(s, [p.id for _, p in rows if p is not None])
    return [{
        "event_id": e.event_key or str(e.id), "type": e.type, "kind": e.kind, "radar_id": e.radar_id, "run_id": e.run_id,
        "date": e.date, "paper_id": e.paper_id, "title": p.title if p else None, "venue": p.venue if p else None,
        "earliest_public_date": p.earliest_public_date if p else None, "doi": ids.get(p.id, {}).get("doi") if p else None,
        "url": p.url if p else None, "is_retracted": p.is_retracted if p else None, "evidence": e.evidence,
        "events_version": e.events_version,
    } for e, p in rows]


# -------------------------
# Topics (plan task 24)
# -------------------------

def radar_topics(s: Session, radar_id: str, version: Optional[str] = None) -> Dict[str, Any]:
    """A radar's topic model (the active one, or ``version``): topics with sizes and the refit mapping."""
    _radar(s, radar_id)
    q = select(m.TopicModel).where(m.TopicModel.radar_id == radar_id)
    q = q.where(m.TopicModel.classifier_version == version) if version else q.where(m.TopicModel.active.is_(True))
    model = s.scalars(q).first()
    if model is None:
        raise NotFound(f"topic model for radar {radar_id!r}" + (f" version {version!r}" if version else "")
                       + " (a radar's first model is fitted once its scans have found enough papers)")
    topics = s.scalars(select(m.Topic).where(m.Topic.classifier_version == model.classifier_version)
                       .order_by(m.Topic.size.desc(), m.Topic.topic_key)).all()
    assigned = dict(s.execute(select(m.PaperTopic.topic_id, func.count()).where(
        m.PaperTopic.topic_id.in_([t.id for t in topics])).group_by(m.PaperTopic.topic_id)).all())
    mapping = s.scalars(select(m.TopicMapping).where(m.TopicMapping.to_version == model.classifier_version)
                        .order_by(m.TopicMapping.relation, m.TopicMapping.from_topic, m.TopicMapping.to_topic)).all()
    versions = s.scalars(select(m.TopicModel.classifier_version).where(m.TopicModel.radar_id == radar_id)
                         .order_by(m.TopicModel.fitted_at)).all()
    return {
        "radar_id": radar_id, "classifier_version": model.classifier_version, "embedder": model.embedder,
        "fitted_at": model.fitted_at, "active": model.active, "previous_version": model.previous_version,
        "k": model.params.get("k"), "silhouette": model.params.get("silhouette", {}), "versions": list(versions),
        "topics": [{"topic_id": t.topic_key, "label": t.label, "key_terms": t.key_terms, "fit_size": t.size,
                    "papers": int(assigned.get(t.id, 0))} for t in topics],
        "mapping": [{"relation": x.relation, "from_topic": x.from_topic, "to_topic": x.to_topic,
                     "shared_members": x.shared_members, "centroid_similarity": x.centroid_similarity} for x in mapping],
    }


def topic_series(s: Session, radar_id: str, as_of: Optional[dt.datetime] = None,
                 source: Optional[str] = None) -> Dict[str, Any]:
    """Each topic's weekly counts and the field denominator, as known at ``as_of`` (default: now)."""
    from carousel.intelligence.observations import FIELD, counts_as_of, series

    model = radar_topics(s, radar_id)
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    q = select(m.TopicObservation).where(m.TopicObservation.radar_id == radar_id,
                                         m.TopicObservation.classifier_version == model["classifier_version"])
    if source:
        q = q.where(m.TopicObservation.source == source)
    rows = [{"source": o.source, "topic_id": o.topic_key, "period_start": o.window_start.isoformat(),
             "period_end": o.window_end.isoformat(), "count": o.count, "status": o.status,
             "observed_at": o.observed_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
            for o in s.scalars(q)]
    known = counts_as_of(rows, as_of)
    sources = sorted({k[0] for k in known})
    return {
        "radar_id": radar_id, "classifier_version": model["classifier_version"], "as_of": as_of, "sources": sources,
        "field": {src: [{"week": w.isoformat(), "count": r["count"], "observed_at": r["observed_at"]}
                        for (sr, tp, w), r in sorted(known.items()) if sr == src and tp == FIELD] for src in sources},
        "topics": [{"topic_id": t["topic_id"], "label": t["label"],
                    "series": {src: series(known, src, t["topic_id"]) for src in sources}} for t in model["topics"]],
    }
