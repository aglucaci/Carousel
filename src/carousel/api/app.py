"""CAROUSEL REST API, v1 (FastAPI). ``carousel api serve`` runs it; ``carousel worker`` runs the scans.

Configuration (environment):

``CAROUSEL_DATABASE_URL``  Postgres with pgvector, migrated with ``carousel db upgrade`` (required)
``CAROUSEL_REDIS_URL``     Redis for the scan queue (default ``redis://localhost:6379/0``)
``CAROUSEL_API_TOKEN``     If set, every write (POST) needs ``Authorization: Bearer <token>``.
                           Reads are open. Unset = open API, suitable only for local use.
``CAROUSEL_STORE``         The engine's working store used by scans (default ``carousel.db``)

The worker (``carousel worker``) reads ``CAROUSEL_DATABASE_URL`` and ``CAROUSEL_STORE``
from its own environment; jobs carry only the scan settings.

Long operations never block a request: ``POST /radars/{id}/runs`` returns 202
with a job ID to poll at ``/jobs/{id}``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hmac
import os
from typing import Any, Iterator, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from carousel import __version__
from carousel.api import schemas, services
from carousel.core.config import RadarConfig

API_PREFIX = "/api/v1"


@dataclasses.dataclass
class Settings:
    database_url: Optional[str] = None
    redis_url: str = "redis://localhost:6379/0"
    api_token: Optional[str] = None
    store_path: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("CAROUSEL_DATABASE_URL"),
            redis_url=os.environ.get("CAROUSEL_REDIS_URL", "redis://localhost:6379/0"),
            api_token=os.environ.get("CAROUSEL_API_TOKEN") or None,
            store_path=os.environ.get("CAROUSEL_STORE"),
        )


def create_app(settings: Optional[Settings] = None, *, queue: Any = None) -> FastAPI:
    """Build the app. ``queue`` (an ``rq.Queue``) can be injected; tests use one on fakeredis."""
    from carousel.database import postgres

    settings = settings or Settings.from_env()
    engine = postgres.engine(settings.database_url)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    if queue is None:
        from redis import Redis
        from rq import Queue

        from carousel.workers.scan import QUEUE

        queue = Queue(QUEUE, connection=Redis.from_url(settings.redis_url))

    app = FastAPI(
        title="CAROUSEL API", version=__version__,
        description="Personalized scientific literature discovery: radars, ranked recommendations with their "
                    "reasons, papers, and feedback. Scores are priorities under a radar's stated configuration, "
                    "not measures of a paper's quality.",
        openapi_url=f"{API_PREFIX}/openapi.json", docs_url=f"{API_PREFIX}/docs", redoc_url=None,
    )
    app.state.settings, app.state.engine, app.state.queue = settings, engine, queue

    def session() -> Iterator[Session]:
        with SessionLocal() as s:
            yield s

    def require_token(authorization: Optional[str] = Header(None, description="Bearer token, when the server sets one")) -> None:
        if not settings.api_token:
            return
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), settings.api_token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid bearer token",
                                headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(services.NotFound)
    async def _not_found(request: Request, exc: services.NotFound):  # noqa: ANN202
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    def job_out(job: Any) -> schemas.JobOut:
        st = job.get_status(refresh=True)
        st = getattr(st, "value", st) or "unknown"
        result = job.return_value() if st == "finished" else None
        error = None
        if st == "failed":
            latest = job.latest_result() if hasattr(job, "latest_result") else None  # RQ >= 1.12
            info = (getattr(latest, "exc_string", None) if latest else None) or getattr(job, "exc_info", None) or ""
            error = (info.strip().splitlines() or ["scan failed"])[-1]
        return schemas.JobOut(
            job_id=job.id, radar_id=(job.meta or {}).get("radar_id"), status=st,
            run_id=(result or {}).get("run_id") if isinstance(result, dict) else None, error=error,
            enqueued_at=job.enqueued_at, started_at=job.started_at, ended_at=job.ended_at,
            status_url=f"{API_PREFIX}/jobs/{job.id}",
        )

    # ---- health ----

    @app.get(f"{API_PREFIX}/health", response_model=schemas.Health, tags=["meta"])
    def health() -> schemas.Health:
        db = queue_state = "ok"
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - reported, not raised
            db = f"error: {exc.__class__.__name__}"
        try:
            queue.connection.ping()
        except Exception as exc:  # pragma: no cover
            queue_state = f"error: {exc.__class__.__name__}"
        return schemas.Health(status="ok" if db == queue_state == "ok" else "degraded", version=__version__,
                              database=db, queue=queue_state)

    # ---- radars ----

    @app.get(f"{API_PREFIX}/radars", response_model=List[schemas.RadarOut], tags=["radars"])
    def list_radars(s: Session = Depends(session)) -> Any:
        return services.list_radars(s)

    @app.post(f"{API_PREFIX}/radars", response_model=schemas.RadarOut, status_code=201, tags=["radars"],
              dependencies=[Depends(require_token)],
              responses={200: {"description": "Existing radar updated (a new version if the config changed)"}})
    def create_radar(body: schemas.RadarIn, response: Response, s: Session = Depends(session)) -> Any:
        try:
            cfg = RadarConfig.model_validate(body.config)
        except Exception as exc:
            raise HTTPException(422, f"invalid radar config: {exc}") from None
        if cfg.radar_id is None:
            raise HTTPException(422, "config.radar_id is required")
        radar, created = services.upsert_radar(s, cfg, schedule=body.schedule)
        s.commit()
        if not created:
            response.status_code = 200
        return radar

    @app.post(f"{API_PREFIX}/radars/proposal", response_model=schemas.ProposalOut, tags=["radars"])
    def propose(body: schemas.ProposalIn, s: Session = Depends(session)) -> Any:
        """Propose a radar config and the exact searches it will run. Saves nothing; approve by POSTing the config."""
        try:
            return services.propose_radar(s, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}", response_model=schemas.RadarOut, tags=["radars"])
    def get_radar(radar_id: str, s: Session = Depends(session)) -> Any:
        return services.radar_detail(s, radar_id)

    @app.patch(f"{API_PREFIX}/radars/{{radar_id}}", response_model=schemas.RadarOut, tags=["radars"],
               dependencies=[Depends(require_token)])
    def update_radar(radar_id: str, body: schemas.RadarPatch, s: Session = Depends(session)) -> Any:
        """Set the scan schedule, pause and resume scheduled scans, or set digest recipients. History is never deleted."""
        try:
            out = services.update_radar(s, radar_id, body.model_dump(include=body.model_fields_set))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        s.commit()
        return out

    # ---- Research Delta ----

    @app.get(f"{API_PREFIX}/events/{{event_id}}", response_model=schemas.EventOut, tags=["research delta"])
    def event(event_id: str, s: Session = Depends(session)) -> Any:
        """One event with its evidence and the publications it rests on."""
        return services.get_event(s, event_id)

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/topics", response_model=schemas.TopicModelOut, tags=["research delta"])
    def topics(radar_id: str, version: Optional[str] = None, s: Session = Depends(session)) -> Any:
        """The radar's topic model (the active one, or a given version): topics, sizes, and the refit mapping."""
        return services.radar_topics(s, radar_id, version)

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/topics/trends", response_model=schemas.TopicTrendsOut,
             tags=["research delta"], responses={501: {"description": "The [trends] extra is not installed"}})
    def topic_trends(radar_id: str, as_of: Optional[dt.datetime] = None, source: Optional[str] = None,
                     s: Session = Depends(session)) -> Any:
        """The trend model for each topic and source, from observations as known at ``as_of`` (default now)."""
        from carousel.intelligence.trends import TrendsUnavailable

        try:
            return services.topic_trends(s, radar_id, as_of, source)
        except TrendsUnavailable as exc:
            raise HTTPException(501, str(exc)) from None

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/topics/series", response_model=schemas.TopicSeriesOut,
             tags=["research delta"])
    def topic_series(radar_id: str, as_of: Optional[dt.datetime] = None, source: Optional[str] = None,
                     s: Session = Depends(session)) -> Any:
        """Weekly topic counts with the field denominator, as known at ``as_of`` (default now; never later data)."""
        return services.topic_series(s, radar_id, as_of, source)

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/graph", response_model=schemas.GraphOut, tags=["explore"])
    def radar_graph(radar_id: str, run_id: Optional[str] = None, as_of: Optional[dt.datetime] = None,
                    s: Session = Depends(session)) -> Any:
        """Explore: a run's papers (the latest by default) and the radar's seed and saved papers, linked by stored
        citations and shared references as observed by ``as_of`` (default now), with each paper's topic."""
        return services.radar_graph(s, radar_id, run_id, as_of)

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/events", response_model=List[schemas.EventOut], tags=["research delta"])
    def events(radar_id: str, run_id: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
               s: Session = Depends(session)) -> Any:
        """What changed: new publications and revisions (corrections, retractions, new versions), newest first."""
        return services.list_events(s, radar_id, run_id, limit)

    # ---- digests ----

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/digest.md", tags=["digests"],
             response_class=PlainTextResponse, responses={200: {"content": {"text/markdown": {}}}})
    def digest_markdown(radar_id: str, run_id: Optional[str] = None, s: Session = Depends(session)) -> Any:
        """A run's digest as Markdown (the latest run by default)."""
        return PlainTextResponse(services.digest_markdown(s, radar_id, run_id), media_type="text/markdown; charset=utf-8")

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/feed.xml", tags=["digests"],
             response_class=PlainTextResponse, responses={200: {"content": {"application/rss+xml": {}}}})
    def digest_feed(radar_id: str, s: Session = Depends(session)) -> Any:
        """RSS 2.0 over the radar's recent runs; each paper appears once."""
        return PlainTextResponse(services.digest_feed(s, radar_id), media_type="application/rss+xml; charset=utf-8")

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/digests", response_model=List[schemas.DigestDelivery], tags=["digests"])
    def digest_deliveries(radar_id: str, limit: int = Query(30, ge=1, le=200), s: Session = Depends(session)) -> Any:
        """Digest deliveries, newest first, failures included with the reason."""
        return services.digest_deliveries(s, radar_id, limit)

    @app.post(f"{API_PREFIX}/radars/{{radar_id}}/digests/email", response_model=List[schemas.DigestDelivery],
              tags=["digests"], dependencies=[Depends(require_token)])
    def send_digest(radar_id: str, run_id: Optional[str] = None, s: Session = Depends(session)) -> Any:
        """Email a run's digest (latest by default) to the radar's recipients now; every attempt is recorded."""
        out = services.send_digest_emails(s, radar_id, run_id)
        s.commit()
        return out

    # ---- runs and jobs ----

    @app.post(f"{API_PREFIX}/radars/{{radar_id}}/runs", response_model=schemas.JobOut, status_code=202,
              tags=["runs"], dependencies=[Depends(require_token)])
    def start_scan(radar_id: str, body: Optional[schemas.ScanRequest] = None, s: Session = Depends(session)) -> Any:
        from carousel.workers.scan import enqueue_scan

        services.radar_config(s, radar_id)  # 404 if the radar or its config is missing
        body = body or schemas.ScanRequest()
        # Only the scan settings go on the queue: the worker takes the database URL (a credential)
        # and the store path from its own environment, so they never sit in Redis or its logs.
        return job_out(enqueue_scan(queue, radar_id, trigger="manual", **body.model_dump(exclude_none=True)))

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}", response_model=schemas.JobOut, tags=["runs"])
    def get_job(job_id: str) -> Any:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        try:
            job = Job.fetch(job_id, connection=queue.connection)
        except NoSuchJobError:
            raise HTTPException(404, f"not found: job {job_id!r}") from None
        return job_out(job)

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/runs", response_model=List[schemas.RunSummary], tags=["runs"])
    def list_runs(radar_id: str, limit: int = Query(50, ge=1, le=500), s: Session = Depends(session)) -> Any:
        return services.list_runs(s, radar_id, limit)

    @app.get(f"{API_PREFIX}/runs/{{run_id}}", response_model=schemas.RunDetail, tags=["runs"])
    def get_run(run_id: str, s: Session = Depends(session)) -> Any:
        return services.get_run(s, run_id)

    # ---- recommendations ----

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/papers", response_model=schemas.RankedPapersOut, tags=["recommendations"])
    def ranked(radar_id: str, run_id: Optional[str] = None, s: Session = Depends(session)) -> Any:
        return services.ranked_papers(s, radar_id, run_id)

    @app.get(f"{API_PREFIX}/radars/{{radar_id}}/delta", response_model=schemas.DeltaOut, tags=["recommendations"])
    def delta(radar_id: str, s: Session = Depends(session)) -> Any:
        return services.delta(s, radar_id)

    # ---- papers ----

    @app.get(f"{API_PREFIX}/papers", response_model=List[schemas.SearchHit], tags=["papers"])
    def search(q: str = Query(..., min_length=2, description="Full-text search over titles and abstracts"),
               limit: int = Query(20, ge=1, le=100), s: Session = Depends(session)) -> Any:
        return services.search_papers(s, q, limit)

    @app.get(f"{API_PREFIX}/papers/{{paper_id}}", response_model=schemas.PaperOut, tags=["papers"])
    def get_paper(paper_id: str, s: Session = Depends(session)) -> Any:
        return services.paper_detail(s, paper_id)

    @app.get(f"{API_PREFIX}/papers/{{paper_id}}/explanation", response_model=schemas.ExplanationOut, tags=["papers"])
    def explain(paper_id: str, radar_id: Optional[str] = None, run_id: Optional[str] = None,
                s: Session = Depends(session)) -> Any:
        return services.explanation(s, paper_id, radar_id, run_id)

    # ---- feedback ----

    @app.get(f"{API_PREFIX}/library", response_model=List[schemas.LibraryItem], tags=["feedback"])
    def library(radar_id: Optional[str] = None, s: Session = Depends(session)) -> Any:
        """Saved papers across radars (or one radar), most recently saved first."""
        return services.library(s, radar_id)

    @app.post(f"{API_PREFIX}/feedback", response_model=schemas.FeedbackOut, status_code=201, tags=["feedback"],
              dependencies=[Depends(require_token)])
    def feedback(body: schemas.FeedbackIn, s: Session = Depends(session)) -> Any:
        out = services.record_feedback(s, radar_id=body.radar_id, paper_id=body.paper_id, action=body.action,
                                       remove=body.remove)
        s.commit()
        return out

    return app


def openapi_document(app: Optional[FastAPI] = None) -> dict:
    """The OpenAPI schema, without needing a database or Redis."""
    if app is None:
        class _NoQueue:
            connection = None

        app = create_app(Settings(database_url="postgresql+psycopg://openapi@localhost/openapi"), queue=_NoQueue())
    return app.openapi()
