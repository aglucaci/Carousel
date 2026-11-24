"""Weekly topic observations as background jobs (plan task 27).

``carousel scheduler`` enqueues one :func:`run_observe` per radar per ISO week
(:func:`carousel.workers.schedule.tick_observations`) for active radars that
have a topic model. The job counts the weeks not yet observed and re-observes
the most recent ``REOBSERVE_WEEKS`` (which is how each source's indexing lag is
measured), then imports the engine store into Postgres.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("carousel.observe")

FIRST_WEEKS = 16
REOBSERVE_WEEKS = 6


def weeks_to_observe(store: Any, radar_id: str, classifier_version: str, today: dt.date) -> int:
    """16 complete weeks the first time; afterwards the new weeks plus the last ``REOBSERVE_WEEKS`` again."""
    from carousel.intelligence.observations import complete_weeks

    rows = store.topic_observations(radar_id, classifier_version)
    if not rows:
        return FIRST_WEEKS
    latest = max(dt.date.fromisoformat(r["period_start"]) for r in rows)
    new = sum(1 for start, _ in complete_weeks(today, FIRST_WEEKS) if start > latest)
    return min(FIRST_WEEKS, new + REOBSERVE_WEEKS)


def run_observe(radar_id: str, *, database_url: Optional[str] = None, store_path: Optional[str] = None,
                today: Optional[dt.date] = None) -> Dict[str, Any]:
    from sqlalchemy.orm import Session

    from carousel.api import services
    from carousel.connectors import CONNECTORS
    from carousel.database import PaperStore, postgres
    from carousel.database.importer import import_store
    from carousel.intelligence.observations import COUNTING_SOURCES, observe
    from carousel.workers.scan import replay_connectors

    engine = postgres.engine(database_url)
    try:
        with Session(engine) as s:
            cfg = services.radar_config(s, radar_id)
        store = store_path or os.environ.get("CAROUSEL_STORE", "carousel.db")
        now = dt.datetime.now(dt.timezone.utc)
        today = today or now.date()
        replay = os.environ.get("CAROUSEL_HTTP_REPLAY")
        with PaperStore(store) as st:
            model = st.active_topic_model(radar_id)
            if model is None:
                return {"status": "skipped", "reason": "no topic model yet"}
            conns = replay_connectors(replay) if replay else {
                s.value: CONNECTORS[s.value]() for s in cfg.sources if s.value in COUNTING_SOURCES}
            stats = observe(st, cfg, model, conns, at=now, today=today,
                            weeks=weeks_to_observe(st, radar_id, model.classifier_version, today))
        import_store(store, engine)
        return {"status": "observed", **stats}
    finally:
        engine.dispose()


def enqueue_observe(queue: Any, radar_id: str, **kwargs: Any) -> Any:
    return queue.enqueue(run_observe, args=(radar_id,), kwargs=kwargs, job_id=f"obs_{uuid.uuid4().hex[:16]}",
                         meta={"radar_id": radar_id, "kind": "observe"}, job_timeout=60 * 60,
                         result_ttl=60 * 60 * 24 * 7, failure_ttl=60 * 60 * 24 * 7)


__all__ = ["FIRST_WEEKS", "REOBSERVE_WEEKS", "enqueue_observe", "run_observe", "weeks_to_observe"]
