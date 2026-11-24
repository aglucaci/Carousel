"""Queries and commands over the Postgres schema, shared by the API and the scan worker.

Everything here takes a SQLAlchemy ``Session`` and returns plain dicts, so the
API layer only maps them to response models.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from carousel.core.config import RadarConfig
from carousel.database import models as m
from carousel.feedback import ACTIONS


class NotFound(LookupError):
    pass


# -------------------------
# Radars
# -------------------------

def upsert_radar(s: Session, cfg: RadarConfig, *, owner_id: Optional[str] = None,
                 schedule: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
    """Create a radar, or add a new version if its config changed. Returns (radar, created)."""
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
        "n_items": r.n_items, "coverage": r.coverage, "notes": r.notes,
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
    }


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


def ranked_papers(s: Session, radar_id: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    run = _run(s, radar_id, run_id)
    items = s.scalars(select(m.RecommendationItem).where(m.RecommendationItem.run_id == run.id)
                      .order_by(m.RecommendationItem.rank)).all()
    dismissed = set(current_feedback(s, radar_id).get("dismiss", []))
    return {
        "run": _run_summary(run),
        "items": [{
            "rank": it.rank, "paper_id": it.paper_id, "score": it.score, "features": it.features, "flags": it.flags,
            "paper": (it.explanation or {}).get("paper") or {}, "dismissed": it.paper_id in dismissed,
        } for it in items],
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
        return {"run_id": runs[0].id, "previous_run_id": None, "entered": sorted(now, key=now.get), "left": [], "moved": []}
    before = ranks(runs[1])
    return {
        "run_id": runs[0].id, "previous_run_id": runs[1].id,
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


def current_feedback(s: Session, radar_id: str) -> Dict[str, List[str]]:
    """{action: [paper_id]}: replay the radar's feedback events in order (last change wins)."""
    state: Dict[Tuple[str, str], bool] = {}
    for pid, action, change in s.execute(select(m.UserFeedback.paper_id, m.UserFeedback.action, m.UserFeedback.change)
                                         .where(m.UserFeedback.radar_id == radar_id)
                                         .order_by(m.UserFeedback.at, m.UserFeedback.id)):
        state[(pid, action)] = change == "added"
    out: Dict[str, List[str]] = {a: [] for a in ACTIONS}
    for (pid, action), on in sorted(state.items()):
        if on:
            out[action].append(pid)
    return out
