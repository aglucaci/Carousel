"""Queries and commands over the Postgres schema, shared by the API and the scan worker.

Everything here takes a SQLAlchemy ``Session`` and returns plain dicts, so the
API layer only maps them to response models.
"""

from __future__ import annotations

import datetime as dt
import os
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
            "sources": list(sources or ["pubmed", "openalex", "crossref"]),
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
        } for it in items],
        "previous_run_id": previous_run_id,
        "arrivals": {a: sum(1 for v in arrival.values() if v == a) for a in ARRIVALS} if previous_run_id else None,
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
    }


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
