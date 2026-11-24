"""Scheduled scans (Phase 2): ``carousel scheduler`` enqueues each radar's scan once per period.

A radar's ``schedule`` is ``daily`` or ``weekly`` (``NULL`` = manual scans only).
Periods are UTC: a day starts at 00:00, a week on Monday at 00:00. A radar is
**due** when it is active, has a schedule, and has no run that started in the
current period. A manual scan in the period counts, so scheduled scans add to
what people run by hand instead of repeating it.

Pausing a radar (``state = 'paused'``) stops its scheduled scans and keeps every
run, item, and feedback event; resuming picks up at the next due period. Manual
scans still work while paused.

Each tick is safe to repeat and to run from more than one process:

- a radar with a scan already queued or running is skipped;
- the enqueue is guarded by a Redis key per radar and period (``SET NX`` with
  an expiry), so two schedulers never enqueue the same period twice. If that
  scan fails and leaves no run, the key expires and a later tick retries.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

SCHEDULES = ("daily", "weekly")
RETRY_AFTER = dt.timedelta(hours=1)  # how long a period's enqueue guard lasts
LOCK_PREFIX = "carousel:schedule:"

log = logging.getLogger("carousel.scheduler")


def _utc(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)


def period_start(schedule: str, now: dt.datetime) -> dt.datetime:
    """Start of the UTC period that contains ``now``."""
    now = _utc(now)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if schedule == "daily":
        return day
    if schedule == "weekly":
        return day - dt.timedelta(days=now.weekday())
    raise ValueError(f"unknown schedule {schedule!r}; expected one of {', '.join(SCHEDULES)}")


def period_length(schedule: str) -> dt.timedelta:
    if schedule not in SCHEDULES:
        raise ValueError(f"unknown schedule {schedule!r}; expected one of {', '.join(SCHEDULES)}")
    return dt.timedelta(days=1 if schedule == "daily" else 7)


def next_scan_at(schedule: Optional[str], state: str, last_run_at: Optional[dt.datetime],
                 now: dt.datetime) -> Optional[dt.datetime]:
    """When the scheduler will next start this radar's scan (``now`` if it is due), or ``None`` if it won't."""
    if not schedule or state != "active":
        return None
    start = period_start(schedule, now)
    if last_run_at is not None and _utc(last_run_at) >= start:
        return start + period_length(schedule)
    return _utc(now)


@dataclass(frozen=True)
class Due:
    radar_id: str
    schedule: str
    period: dt.datetime


def due_radars(session: Any, now: dt.datetime) -> List[Due]:
    """Active, scheduled radars with no run in their current period."""
    from sqlalchemy import func, select

    from carousel.database import models as m

    last = (select(m.RecommendationRun.radar_id, func.max(m.RecommendationRun.executed_at).label("at"))
            .group_by(m.RecommendationRun.radar_id).subquery())
    rows = session.execute(
        select(m.Radar.id, m.Radar.schedule, last.c.at)
        .outerjoin(last, last.c.radar_id == m.Radar.id)
        .where(m.Radar.state == "active", m.Radar.schedule.in_(SCHEDULES))
        .order_by(m.Radar.id)
    ).all()
    out = []
    for rid, schedule, at in rows:
        start = period_start(schedule, now)
        if at is None or _utc(at) < start:
            out.append(Due(rid, schedule, start))
    return out


def pending_radars(queue: Any) -> set:
    """Radars with a scan job waiting or running on ``queue``."""
    from rq.registry import StartedJobRegistry

    jobs = list(queue.get_jobs())
    started = StartedJobRegistry(queue=queue)
    jobs += [j for j in queue.job_class.fetch_many(started.get_job_ids(), connection=queue.connection) if j]
    return {(j.meta or {}).get("radar_id") for j in jobs}


def tick(session_factory: Callable[[], Any], queue: Any, now: Optional[dt.datetime] = None,
         enqueue: Optional[Callable[..., Any]] = None) -> List[Any]:
    """Enqueue every due radar's scan once. Returns the enqueued RQ jobs."""
    from carousel.workers.scan import enqueue_scan

    now = _utc(now or dt.datetime.now(dt.timezone.utc))
    enqueue = enqueue or enqueue_scan
    with session_factory() as s:
        due = due_radars(s, now)
    if not due:
        return []
    busy = pending_radars(queue)
    jobs = []
    for d in due:
        if d.radar_id in busy:
            log.info("%s: a scan is already queued or running; skipping", d.radar_id)
            continue
        key = f"{LOCK_PREFIX}{d.radar_id}:{d.period.isoformat()}"
        if not queue.connection.set(key, "1", nx=True, ex=int(RETRY_AFTER.total_seconds())):
            continue  # enqueued for this period within the last hour (by this or another scheduler)
        job = enqueue(queue, d.radar_id, trigger="scheduled")
        log.info("%s: %s scan enqueued for the period from %s (job %s)", d.radar_id, d.schedule,
                 d.period.date().isoformat(), job.id)
        jobs.append(job)
    return jobs


def run_forever(session_factory: Callable[[], Any], queue: Any, interval: float = 60.0,
                ticks: Optional[int] = None, sleep: Callable[[float], None] = time.sleep) -> None:
    """Tick every ``interval`` seconds (``ticks`` bounds the loop, for tests). Errors are logged, not fatal."""
    log.info("scheduler started: checking every %.0f s for daily/weekly radars that are due", interval)
    n = 0
    while ticks is None or n < ticks:
        try:
            tick(session_factory, queue)
        except Exception:  # keep scheduling through a database or Redis outage
            log.exception("scheduler tick failed; retrying in %.0f s", interval)
        n += 1
        if ticks is None or n < ticks:
            sleep(interval)


def validate(schedule: Optional[str]) -> Optional[str]:
    if schedule is not None and schedule not in SCHEDULES:
        raise ValueError(f"schedule must be one of {', '.join(SCHEDULES)}, or null for manual scans")
    return schedule


__all__: Sequence[str] = ("SCHEDULES", "period_start", "next_scan_at", "due_radars", "tick", "run_forever", "validate")
