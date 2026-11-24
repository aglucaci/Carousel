"""Background scans (RQ on Redis).

The API enqueues :func:`run_scan`; ``carousel worker`` runs it. A scan:

1. reads the radar's current config and feedback from Postgres;
2. runs the Phase 1A engine (``Radar.scan``) against the shared engine store
   (SQLite, ``CAROUSEL_STORE``), which does retrieval, dedup, candidates,
   ranking, and run persistence exactly as on the command line;
3. imports the store into Postgres (idempotent), so the new run, papers, and
   embeddings become visible to the API, and records what triggered the run;
4. produces the run's digests (Markdown, the RSS feed, an email per
   recipient; :func:`carousel.api.services.deliver_digests`), recording each
   delivery, failed ones included. A digest failure never fails the scan.

``carousel scheduler`` (:mod:`carousel.workers.schedule`) enqueues scheduled scans.

``CAROUSEL_HTTP_REPLAY=<dir>`` makes scans read recorded responses from
``<dir>/<source>.json`` instead of the network (any unrecorded request fails
the scan). The web app's end-to-end test uses it; see ``apps/web/e2e``.

The engine store is a single SQLite file, so run **one** scan worker. Porting
the engine's store to Postgres is a later step; until then this keeps the web
app on the same tested code path as ``carousel scan`` and the daily Actions run.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("carousel.digests")

QUEUE = "carousel-scans"


def replay_connectors(directory: str) -> Dict[str, Any]:
    """Connectors that answer from recorded HTTP fixtures in ``directory`` (``pubmed.json``, ``openalex.json``,
    ``europepmc.json``, ``crossref.json``, ``arxiv.json``)."""
    from carousel.connectors import (ArxivConnector, CrossrefConnector, EuropePMCConnector, HttpClient, OpenAlexConnector,
                                     PubMedConnector, ReplayTransport)

    out: Dict[str, Any] = {}
    for name, cls in (("pubmed", PubMedConnector), ("openalex", OpenAlexConnector)):
        path = Path(directory) / f"{name}.json"
        if path.exists():
            out[name] = cls(client=HttpClient(transport=ReplayTransport(path)), api_key="")
    path = Path(directory) / "europepmc.json"
    if path.exists():
        out["europepmc"] = EuropePMCConnector(client=HttpClient(transport=ReplayTransport(path)))
    path = Path(directory) / "arxiv.json"
    if path.exists():
        out["arxiv"] = ArxivConnector(client=HttpClient(transport=ReplayTransport(path)))
    path = Path(directory) / "crossref.json"
    if path.exists():
        out["crossref"] = CrossrefConnector(client=HttpClient(transport=ReplayTransport(path)), mailto="")
    return out


def run_scan(radar_id: str, *, top_n: int = 25, max_results: int = 200, mode: Optional[str] = None,
             as_of: Optional[dt.date] = None, embedder: Optional[str] = None, database_url: Optional[str] = None,
             store_path: Optional[str] = None, trigger: Optional[str] = None) -> Dict[str, Any]:
    """Scan one radar. ``trigger`` ('manual' or 'scheduled') is recorded on the new run."""
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from carousel import feedback as fb
    from carousel.api import services
    from carousel.database import postgres
    from carousel.database.importer import import_store
    from carousel.radar import Radar

    engine = postgres.engine(database_url)
    try:
        with Session(engine) as s:
            cfg = services.radar_config(s, radar_id)
            state = services.current_feedback(s, radar_id)
        store = store_path or os.environ.get("CAROUSEL_STORE", "carousel.db")
        Path(store).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as d:
            feedback_file = Path(d) / f"{radar_id}.feedback.yml"
            fb.save(feedback_file, state, radar_id)
            replay = os.environ.get("CAROUSEL_HTTP_REPLAY")
            res = Radar(cfg, store=store, feedback_path=feedback_file).scan(
                top_n=top_n, max_results=max_results, mode=mode, as_of=as_of,
                connectors=replay_connectors(replay) if replay else None,
                embedder=embedder or os.environ.get("CAROUSEL_EMBEDDER", "auto"),
            )
        import_store(store, engine)
        if trigger:
            from carousel.database import models as m

            with Session(engine) as s:
                s.execute(update(m.RecommendationRun).where(m.RecommendationRun.id == res.run_id).values(trigger=trigger))
                s.commit()
        digests = deliver(engine, radar_id, res.run_id)
        return {"run_id": res.run_id, "status": res.run["status"], "n_items": len(res), "digests": digests}
    finally:
        engine.dispose()


def deliver(engine, radar_id: str, run_id: str) -> Dict[str, int]:
    """Produce the run's digests (Markdown, RSS, email per recipient), recording each; never fails the scan."""
    from sqlalchemy.orm import Session

    from carousel.api import services

    try:
        with Session(engine) as s:
            rows = services.deliver_digests(s, radar_id, run_id)
            s.commit()
    except Exception as exc:  # the scan itself succeeded; say so in the log and keep it
        log.warning("digests for %s run %s failed: %s", radar_id, run_id, exc)
        return {"delivered": 0, "failed": 0}
    for r in rows:
        if r["status"] == "failed":
            log.warning("digest %s%s failed: %s", r["channel"], f" to {r['recipient']}" if r["recipient"] else "", r["error"])
    return {"delivered": sum(r["status"] == "delivered" for r in rows), "failed": sum(r["status"] == "failed" for r in rows)}


def enqueue_scan(queue, radar_id: str, **kwargs: Any):
    """Enqueue a scan; returns the RQ job."""
    return queue.enqueue(run_scan, args=(radar_id,), kwargs=kwargs, job_id=f"job_{uuid.uuid4().hex[:16]}",
                         meta={"radar_id": radar_id}, job_timeout=60 * 30, result_ttl=60 * 60 * 24 * 7,
                         failure_ttl=60 * 60 * 24 * 7)
