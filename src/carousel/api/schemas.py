"""Request and response models for the v1 API (they define the OpenAPI schema)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RadarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Dict[str, Any] = Field(description="A radar config, as in a radar YAML file (validated as RadarConfig)")
    schedule: Optional[Literal["daily", "weekly"]] = Field(
        None, description="Scan automatically once per UTC day or week (`carousel scheduler`); omit for manual only")


class RadarPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule: Optional[Literal["daily", "weekly"]] = Field(
        None, description="daily | weekly, or null for manual scans only. Omit to leave unchanged")
    state: Optional[Literal["active", "paused"]] = Field(
        None, description="paused stops scheduled scans and keeps all history; manual scans still work")


class RunSummary(_Out):
    run_id: str
    radar_id: str
    executed_at: dt.datetime
    finished_at: Optional[dt.datetime] = None
    as_of: dt.date
    status: str
    ranking_version: str
    model_version: Optional[str] = None
    mode: str
    config_hash: str
    n_observed: int
    n_candidates: int
    n_items: int
    coverage: List[Dict[str, Any]] = Field(description="What each source returned for this run, and what it did not")
    notes: List[str] = []
    trigger: Optional[Literal["manual", "scheduled"]] = Field(
        None, description="What started the run; null for runs from the CLI or imported history")


class RunDetail(RunSummary):
    ranking: Dict[str, Any] = Field(description="Weights, active and inactive signals, window, and ranking statistics")


class RadarOut(_Out):
    radar_id: str
    title: str
    description: str
    state: str
    schedule: Optional[str] = None
    owner_id: Optional[str] = None
    created_at: dt.datetime
    updated_at: dt.datetime
    config: Optional[Dict[str, Any]] = None
    n_runs: int
    latest_run: Optional[RunSummary] = None
    next_scan_at: Optional[dt.datetime] = Field(
        None, description="When the scheduler will next scan this radar (now, if due); null if manual or paused")


class ProposalIn(BaseModel):
    """What a user tells a new radar; everything but the topic is optional (progressive disclosure)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    topic: str = Field(min_length=2, max_length=200, description="What to watch, in plain words; also the radar name")
    research_question: str = Field("", max_length=1000)
    description: str = Field("", max_length=2000)
    terms: List[str] = Field([], description="Search terms; default: the topic itself")
    exclude: List[str] = []
    methods: List[str] = Field([], description="Methods, assays, data types to match")
    seed_papers: List[str] = Field([], description="DOIs of papers that define the radar")
    publication_types: List[str] = []
    sources: List[str] = Field(["pubmed", "openalex"])
    mode: Literal["focused", "balanced", "exploratory"] = "balanced"
    window_days: int = Field(90, ge=1, le=3650)


class StrategyStep(_Out):
    source: str
    status: str = Field(description="ready | not_available")
    query: Optional[str] = Field(None, description="The exact query this source will receive")
    filters: Optional[str] = None
    notes: List[str] = []


class ProposalOut(_Out):
    config: Dict[str, Any] = Field(description="The radar config to create (POST /radars) once approved")
    window: Dict[str, Any]
    strategy: List[StrategyStep]
    ranking: Dict[str, Any]
    notes: List[str]


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    top_n: int = Field(25, ge=1, le=200)
    max_results: int = Field(200, ge=1, le=2000)
    mode: Optional[Literal["focused", "balanced", "exploratory"]] = None
    as_of: Optional[dt.date] = Field(None, description="End of the date window (default: today, UTC); for replays")


class JobOut(_Out):
    job_id: str
    radar_id: Optional[str] = None
    status: str = Field(description="queued | started | finished | failed | ...")
    run_id: Optional[str] = Field(None, description="Set when the scan has finished")
    error: Optional[str] = None
    enqueued_at: Optional[dt.datetime] = None
    started_at: Optional[dt.datetime] = None
    ended_at: Optional[dt.datetime] = None
    status_url: str


class RankedItem(_Out):
    rank: int
    paper_id: str
    score: float
    features: Dict[str, Optional[float]] = Field(description="relevance, novelty, method_match (0-1)")
    flags: List[str]
    paper: Dict[str, Any] = Field(description="Title, authors, venue, date, identifiers, URL")
    dismissed: bool = Field(description="Dismissed since this run (feedback applies from the next run)")
    saved: bool = Field(False, description="In the radar's saved list now")
    feedback: List[str] = Field([], description="Feedback actions currently on for this paper in this radar")
    arrival: Optional[Literal["newly_published", "newly_observed", "seen_before"]] = Field(
        None, description="Relative to the radar's earlier runs: published since the previous scan, older but "
                          "seen by this radar for the first time, or seen before. Null on a radar's first run")


class RankedPapersOut(_Out):
    run: RunSummary
    items: List[RankedItem]
    previous_run_id: Optional[str] = Field(None, description="The run before this one, which arrivals compare with")
    arrivals: Optional[Dict[str, int]] = Field(None, description="Count of items per arrival; null on the first run")


class Moved(_Out):
    paper_id: str
    rank: int
    previous_rank: int
    change: int


class DeltaOut(_Out):
    run_id: str
    previous_run_id: Optional[str] = None
    entered: List[str]
    left: List[str]
    moved: List[Moved]
    newly_published: List[str] = Field([], description="Recommended papers published after the previous run's as_of")
    newly_observed: List[str] = Field([], description="Recommended papers older than that, new to this radar")


class PaperOut(_Out):
    paper_id: str
    requested_id: str = Field(description="The ID asked for; differs from paper_id when that paper was merged")
    title: Optional[str] = None
    abstract: Optional[str] = None
    venue: Optional[str] = None
    language: Optional[str] = None
    url: Optional[str] = None
    publication_types: List[str]
    is_retracted: bool
    dates: Dict[str, Any]
    first_seen: Optional[dt.datetime] = None
    last_verified: Optional[dt.datetime] = None
    identifiers: Dict[str, str]
    authors: List[Dict[str, Optional[str]]]
    sources: List[Dict[str, Any]]
    provenance: Dict[str, Any] = Field(description="Which source record each field came from")


class SearchHit(_Out):
    paper_id: str
    title: Optional[str] = None
    venue: Optional[str] = None
    earliest_public_date: Optional[dt.date] = None
    rank: float


class ExplanationOut(_Out):
    paper_id: str
    run_id: str
    radar_id: str
    rank: int
    score: float
    features: Dict[str, Optional[float]]
    details: Dict[str, Any] = Field(description="How it was found, matched terms and methods, similarity, novelty, sources")
    flags: List[str]
    ranking_version: str
    model_version: Optional[str] = None
    mode: str
    weights: Dict[str, float]
    inactive_signals: Dict[str, str]
    source_coverage: List[Dict[str, Any]]
    executed_at: dt.datetime


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    radar_id: str
    paper_id: str
    action: Literal["save", "dismiss", "relevant", "irrelevant"]
    remove: bool = Field(False, description="Undo this action")


class FeedbackOut(_Out):
    id: int
    radar_id: str
    paper_id: str
    action: str
    change: str
    at: dt.datetime
    state: Dict[str, List[str]] = Field(description="The radar's current feedback, applied on its next scan")


class LibraryItem(_Out):
    radar_id: str
    radar_title: Optional[str] = None
    paper_id: str
    saved_at: dt.datetime
    paper: Dict[str, Any]


class Health(_Out):
    status: str
    version: str
    database: str
    queue: str
