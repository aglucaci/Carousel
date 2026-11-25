"""Phase 1B Postgres schema (spec §8), SQLAlchemy 2.0 models. Migrations: ``database/migrations``.

Tables follow the spec's minimum production data model, plus the audit
tables the engine already keeps (dedup reviews, merge log, identifier
conflicts, run observations) and an immutable home for the historical run
snapshots. Model-specific payloads (features, explanations, coverage, source
records, radar configs) are JSONB; everything that is queried or joined on is
a column.

IDs are text and carried over from the Phase 1A store unchanged: papers keep
their ``cp_...`` IDs, runs their ``run_...`` IDs, radars their ``radar_id``
slug. Merged papers stay as rows with ``status = 'merged'`` and
``merged_into`` set, so old recommendations stay auditable.

Requires PostgreSQL with the ``vector`` extension (pgvector).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {Dict[str, Any]: JSONB, List[Any]: JSONB, dt.datetime: TS}


def _now() -> Any:
    return mapped_column(TS, server_default=func.now(), nullable=False)


# -------------------------
# Users and radars
# -------------------------

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[Optional[str]] = mapped_column(Text)
    preferences: Mapped[Dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[dt.datetime] = _now()


class Radar(Base):
    __tablename__ = "radars"
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # radar_id slug
    owner_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default="")
    state: Mapped[str] = mapped_column(String(16), server_default="active")
    schedule: Mapped[Optional[str]] = mapped_column(Text)  # 'daily' | 'weekly' (UTC periods); NULL = manual only
    digest_emails: Mapped[List[Any]] = mapped_column(server_default="[]")  # addresses that get each run's digest
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now()
    __table_args__ = (
        CheckConstraint("state IN ('active', 'paused', 'archived')", name="state"),
        CheckConstraint("schedule IN ('daily', 'weekly')", name="schedule"),
    )


class RadarVersion(Base):
    """Every configuration a radar has run with, keyed by config hash."""

    __tablename__ = "radar_versions"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"))
    config_hash: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[Optional[int]] = mapped_column(Integer)
    config: Mapped[Optional[Dict[str, Any]]] = mapped_column()  # full RadarConfig, when known
    effective_at: Mapped[dt.datetime] = _now()
    __table_args__ = (UniqueConstraint("radar_id", "config_hash"),)


# -------------------------
# Papers
# -------------------------

class Paper(Base):
    __tablename__ = "papers"
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # canonical paper_id (cp_...)
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    merged_into: Mapped[Optional[str]] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"))
    title: Mapped[Optional[str]] = mapped_column(Text)
    abstract: Mapped[Optional[str]] = mapped_column(Text)
    venue: Mapped[Optional[str]] = mapped_column(Text)
    language: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    publication_types: Mapped[List[Any]] = mapped_column(server_default="[]")
    is_retracted: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Spec §4.1 dates, as partial dates (YYYY, YYYY-MM, YYYY-MM-DD), plus a sortable lower bound.
    published: Mapped[Optional[str]] = mapped_column(String(10))
    first_online: Mapped[Optional[str]] = mapped_column(String(10))
    preprint_posted: Mapped[Optional[str]] = mapped_column(String(10))
    version_date: Mapped[Optional[str]] = mapped_column(String(10))
    source_updated: Mapped[Optional[str]] = mapped_column(String(10))
    earliest_public_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    first_seen: Mapped[Optional[dt.datetime]] = mapped_column(TS)
    last_verified: Mapped[Optional[dt.datetime]] = mapped_column(TS)
    canonical: Mapped[Dict[str, Any]] = mapped_column(server_default="{}")  # provenance, conflicts, ...
    search: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(abstract, ''))", persisted=True)
    )
    __table_args__ = (
        CheckConstraint("status IN ('active', 'merged')", name="status"),
        Index("ix_papers_search", "search", postgresql_using="gin"),
        Index("ix_papers_earliest_public_date", "earliest_public_date"),
        Index("ix_papers_first_seen", "first_seen"),
        Index("ix_papers_merged_into", "merged_into"),
    )


class PaperIdentifier(Base):
    __tablename__ = "paper_identifiers"
    identifier_type: Mapped[str] = mapped_column(String(16), primary_key=True)  # doi | pmid | pmcid | openalex | arxiv
    value: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)


class PaperSource(Base):
    """What each provider said about a paper (immutable apart from re-verification)."""

    __tablename__ = "paper_sources"
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    provider_id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    first_seen: Mapped[dt.datetime] = mapped_column(TS)
    last_checked: Mapped[dt.datetime] = mapped_column(TS)
    content_changed_at: Mapped[dt.datetime] = mapped_column(TS)
    content_sha256: Mapped[str] = mapped_column(String(64))
    record: Mapped[Dict[str, Any]] = mapped_column()


class PaperVersion(Base):
    """Links from a paper to its other versions; ``version_type`` is the *related* paper's role.

    A preprint and its journal article are two papers, linked both ways (plan task 18): the article has a
    ``preprint`` row pointing at the preprint, the preprint a ``published`` row pointing at the article.
    ``source`` is how the link was made (crossref, europepmc, manual); ``details`` holds every piece of evidence.
    """

    __tablename__ = "paper_versions"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    version_type: Mapped[str] = mapped_column(String(16))
    related_paper_id: Mapped[Optional[str]] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(16))
    date: Mapped[Optional[str]] = mapped_column(String(10))
    details: Mapped[Dict[str, Any]] = mapped_column(server_default="{}")
    __table_args__ = (
        CheckConstraint("version_type IN ('preprint', 'published', 'revision', 'correction', 'retraction')",
                        name="version_type"),
        UniqueConstraint("paper_id", "version_type", "related_paper_id"),
    )


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    orcid: Mapped[Optional[str]] = mapped_column(String(19), unique=True)
    # Without an ORCID, authors are identified by exact display name only: provisional, may conflate.
    identity: Mapped[str] = mapped_column(String(16), server_default="name_only")
    __table_args__ = (
        CheckConstraint("identity IN ('orcid', 'name_only')", name="identity"),
        Index("uq_authors_name_only", "name", unique=True, postgresql_where=text("orcid IS NULL")),
    )


class PaperAuthor(Base):
    __tablename__ = "paper_authors"
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id", ondelete="CASCADE"), index=True)


class PaperEmbedding(Base):
    __tablename__ = "paper_embeddings"
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    model_version: Mapped[str] = mapped_column(Text, primary_key=True)
    text_sha256: Mapped[str] = mapped_column(String(64))
    dim: Mapped[int] = mapped_column(Integer)
    # Untyped dimension so several models can coexist; per-model HNSW indexes are added as models are adopted.
    embedding: Mapped[Any] = mapped_column(Vector())
    created_at: Mapped[dt.datetime] = _now()


class PaperText(Base):
    """A full text kept under a reusable licence (Europe PMC), with that licence. See carousel.core.fulltext."""

    __tablename__ = "paper_texts"
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    format: Mapped[str] = mapped_column(String(16))
    license: Mapped[Optional[str]] = mapped_column(Text)
    license_url: Mapped[Optional[str]] = mapped_column(Text)
    license_source: Mapped[str] = mapped_column(String(16))
    reuse: Mapped[str] = mapped_column(String(16))
    url: Mapped[Optional[str]] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    sections: Mapped[List[Any]] = mapped_column()
    retrieved_at: Mapped[dt.datetime] = mapped_column(TS)
    __table_args__ = (
        CheckConstraint("reuse IN ('open', 'noncommercial', 'no_derivatives', 'restricted', 'unknown')", name="reuse"),
        CheckConstraint("license_source IN ('text', 'metadata')", name="license_source"),
    )


class PaperExtraction(Base):
    """A grounded extraction (carousel.summarization): fields of claims, each quoting a cited passage.

    Cached per paper, provider, requested model, schema version, and input hash. Extractions that read text a
    user supplied are private and never imported here.
    """

    __tablename__ = "paper_extractions"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    requested_model: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(16))
    input_sha256: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(16))
    coverage: Mapped[Dict[str, Any]] = mapped_column()
    fields: Mapped[Dict[str, Any]] = mapped_column()
    passages: Mapped[List[Any]] = mapped_column()
    rejected: Mapped[List[Any]] = mapped_column(server_default="[]")
    created_at: Mapped[dt.datetime] = mapped_column(TS)
    __table_args__ = (
        UniqueConstraint("paper_id", "provider", "requested_model", "schema_version", "input_sha256"),
        CheckConstraint("location IN ('local', 'external')", name="location"),
    )


class PaperAnnotation(Base):
    """Text-mined entities a source attached to one article of a paper (one JSON list per article)."""

    __tablename__ = "paper_annotations"
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    article_id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    annotations: Mapped[List[Any]] = mapped_column()
    retrieved_at: Mapped[dt.datetime] = mapped_column(TS)


# -------------------------
# Topics and research events (Phase 3)
# -------------------------

class TopicModel(Base):
    """A versioned topic classifier for one radar (carousel.intelligence.topics); one is active per radar."""

    __tablename__ = "topic_models"
    classifier_version: Mapped[str] = mapped_column(Text, primary_key=True)
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), index=True)
    embedder: Mapped[str] = mapped_column(Text)
    params: Mapped[Dict[str, Any]] = mapped_column()
    fitted_at: Mapped[dt.datetime] = mapped_column(TS)
    previous_version: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean)


class Topic(Base):
    """A topic of one classifier version. ``topic_key`` is its lineage: the same across refits while the topic is
    the same (``topic_mappings``)."""

    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    classifier_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()
    radar_id: Mapped[Optional[str]] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), index=True)
    topic_key: Mapped[Optional[str]] = mapped_column(Text)
    key_terms: Mapped[List[Any]] = mapped_column(server_default="[]")
    size: Mapped[Optional[int]] = mapped_column(Integer)
    threshold: Mapped[Optional[float]] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("classifier_version", "topic_key"),)


class PaperTopic(Base):
    __tablename__ = "paper_topics"
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True)
    confidence: Mapped[float] = mapped_column(Float)
    origin: Mapped[Optional[str]] = mapped_column(String(16))  # fit | assigned
    assigned_at: Mapped[Optional[dt.datetime]] = mapped_column(TS)


class TopicMapping(Base):
    """How a refit's topics relate to the previous version's: same | split | merge | new | retired."""

    __tablename__ = "topic_mappings"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    from_version: Mapped[Optional[str]] = mapped_column(Text)
    to_version: Mapped[str] = mapped_column(Text, index=True)
    relation: Mapped[str] = mapped_column(String(16))
    from_topic: Mapped[Optional[str]] = mapped_column(Text)
    to_topic: Mapped[Optional[str]] = mapped_column(Text)
    shared_members: Mapped[int] = mapped_column(Integer)
    centroid_similarity: Mapped[Optional[float]] = mapped_column(Float)
    __table_args__ = (
        CheckConstraint("relation IN ('same', 'split', 'merge', 'new', 'retired')", name="relation"),
        UniqueConstraint("to_version", "relation", "from_topic", "to_topic"),
    )


class TopicObservation(Base):
    """One count request's answer (carousel.intelligence.observations): append-only, never updated.

    ``topic_key`` is the topic's lineage ID, or ``__field__`` for the radar's denominator (then ``topic_id`` is
    NULL). ``count`` is NULL when the request failed (``status = 'error'``).
    """

    __tablename__ = "topic_observations"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    topic_id: Mapped[Optional[int]] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    window_start: Mapped[dt.date] = mapped_column(Date)
    window_end: Mapped[dt.date] = mapped_column(Date)
    count: Mapped[Optional[int]] = mapped_column(Integer)
    radar_id: Mapped[Optional[str]] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), index=True)
    topic_key: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(String(16))
    status: Mapped[Optional[str]] = mapped_column(String(8))
    query: Mapped[Optional[str]] = mapped_column(Text)
    cost: Mapped[Optional[float]] = mapped_column(Float)
    error: Mapped[Optional[str]] = mapped_column(Text)
    coverage: Mapped[Dict[str, Any]] = mapped_column(server_default="{}")
    classifier_version: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[dt.datetime] = _now()
    __table_args__ = (
        UniqueConstraint("radar_id", "classifier_version", "topic_key", "source", "window_start", "observed_at"),
        Index("ix_topic_observations_series", "radar_id", "source", "window_start"),
    )


class ResearchEvent(Base):
    """A Research Delta event, derived from stored runs (see carousel.intelligence.events).

    Phase 2: ``new_publication`` and ``publication_revision`` (``kind``: retraction, correction, new_version, ...),
    each about one paper and produced by one run; ``evidence`` names the runs, notices, and links it rests on.
    """

    __tablename__ = "research_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_key: Mapped[Optional[str]] = mapped_column(String(32), unique=True)  # the engine's deterministic event_id
    type: Mapped[str] = mapped_column(String(32))
    kind: Mapped[Optional[str]] = mapped_column(String(32))
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("recommendation_runs.id", ondelete="CASCADE"), index=True)
    paper_id: Mapped[Optional[str]] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    evidence: Mapped[Dict[str, Any]] = mapped_column(server_default="{}")
    events_version: Mapped[Optional[str]] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = _now()


# -------------------------
# Recommendations (immutable once written)
# -------------------------

class RecommendationRun(Base):
    __tablename__ = "recommendation_runs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # run_...
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"))
    radar_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("radar_versions.id", ondelete="SET NULL"))
    executed_at: Mapped[dt.datetime] = mapped_column(TS)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(TS)
    as_of: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    config_hash: Mapped[str] = mapped_column(String(32))
    ranking_version: Mapped[str] = mapped_column(String(32))
    explanation_version: Mapped[Optional[str]] = mapped_column(String(16))  # NULL: recorded before versioning
    candidates_version: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[Optional[str]] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16))
    n_observed: Mapped[int] = mapped_column(Integer)
    n_candidates: Mapped[int] = mapped_column(Integer)
    n_items: Mapped[int] = mapped_column(Integer)
    coverage: Mapped[List[Any]] = mapped_column()
    ranking: Mapped[Dict[str, Any]] = mapped_column()
    notes: Mapped[List[Any]] = mapped_column(server_default="[]")
    # What started the run: 'manual' (API/web), 'scheduled' (carousel scheduler); NULL = CLI or imported.
    trigger: Mapped[Optional[str]] = mapped_column(String(16))
    __table_args__ = (
        Index("ix_recommendation_runs_radar_executed", "radar_id", "executed_at"),
        CheckConstraint("trigger IN ('manual', 'scheduled')", name="trigger"),
    )


class RecommendationItem(Base):
    __tablename__ = "recommendation_items"
    run_id: Mapped[str] = mapped_column(ForeignKey("recommendation_runs.id", ondelete="CASCADE"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    mmr: Mapped[float] = mapped_column(Float)
    features: Mapped[Dict[str, Any]] = mapped_column()      # components (relevance, novelty, method_match)
    explanation: Mapped[Dict[str, Any]] = mapped_column()   # details behind "Why this paper?"
    flags: Mapped[List[Any]] = mapped_column(server_default="[]")


class RunObservation(Base):
    __tablename__ = "run_observations"
    run_id: Mapped[str] = mapped_column(ForeignKey("recommendation_runs.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    provider_id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))


# -------------------------
# Users' feedback and collections
# -------------------------

class UserFeedback(Base):
    """Append-only feedback events. ``user_id`` is NULL for file-based (Phase 1A) feedback."""

    __tablename__ = "user_feedback"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    radar_id: Mapped[Optional[str]] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), index=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    change: Mapped[str] = mapped_column(String(8), server_default="added")
    ref: Mapped[Optional[str]] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16), server_default="app")
    at: Mapped[dt.datetime] = mapped_column(TS)
    __table_args__ = (
        CheckConstraint("action IN ('save', 'dismiss', 'relevant', 'irrelevant')", name="action"),
        CheckConstraint("change IN ('added', 'removed')", name="change"),
        UniqueConstraint("radar_id", "paper_id", "action", "change", "at", "origin"),
    )


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(16), server_default="private")
    created_at: Mapped[dt.datetime] = _now()
    __table_args__ = (CheckConstraint("visibility IN ('private', 'link', 'public')", name="visibility"),)


class CollectionPaper(Base):
    __tablename__ = "collection_papers"
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), primary_key=True)
    notes: Mapped[str] = mapped_column(Text, server_default="")
    reading_status: Mapped[str] = mapped_column(String(16), server_default="unread")
    added_at: Mapped[dt.datetime] = _now()
    __table_args__ = (CheckConstraint("reading_status IN ('unread', 'reading', 'read')", name="reading_status"),)


class DigestRun(Base):
    """One digest delivery: a run's Markdown, the radar's RSS feed after a run, or one email to one recipient.

    ``details`` holds the recipient and the error for email, and the rendered content for Markdown and RSS.
    A failed delivery is kept (``delivery_status = 'failed'``) with the reason.
    """

    __tablename__ = "digest_runs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("recommendation_runs.id", ondelete="SET NULL"), index=True)
    channel: Mapped[str] = mapped_column(String(16))  # email | rss | markdown
    generated_at: Mapped[dt.datetime] = _now()
    delivery_status: Mapped[str] = mapped_column(String(16))  # delivered | failed
    details: Mapped[Dict[str, Any]] = mapped_column(server_default="{}")
    __table_args__ = (
        CheckConstraint("channel IN ('email', 'rss', 'markdown')", name="channel"),
        CheckConstraint("delivery_status IN ('delivered', 'failed')", name="delivery_status"),
    )


# -------------------------
# Deduplication audit (from the engine)
# -------------------------

class DedupReview(Base):
    __tablename__ = "dedup_reviews"
    paper_a: Mapped[str] = mapped_column(ForeignKey("papers.id"), primary_key=True)
    paper_b: Mapped[str] = mapped_column(ForeignKey("papers.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    score: Mapped[Optional[float]] = mapped_column(Float)
    reasons: Mapped[List[Any]] = mapped_column()
    features: Mapped[Dict[str, Any]] = mapped_column()
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = mapped_column(TS)
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(TS)
    decided_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("paper_a < paper_b", name="ordered_pair"),
        CheckConstraint("status IN ('pending', 'merged', 'distinct', 'linked', 'superseded')", name="status"),
        Index("ix_dedup_reviews_status", "status"),
    )


class MergeLog(Base):
    __tablename__ = "merge_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(TS)
    action: Mapped[str] = mapped_column(String(8))
    survivor: Mapped[str] = mapped_column(ForeignKey("papers.id"))
    other: Mapped[str] = mapped_column(ForeignKey("papers.id"))
    method: Mapped[str] = mapped_column(String(16))
    score: Mapped[Optional[float]] = mapped_column(Float)
    detail: Mapped[Dict[str, Any]] = mapped_column()
    source_log_id: Mapped[Optional[int]] = mapped_column(BigInteger)  # id in the imported 1A store
    __table_args__ = (
        CheckConstraint("action IN ('merge', 'split')", name="action"),
        UniqueConstraint("at", "action", "survivor", "other"),
    )


class IdentifierConflict(Base):
    __tablename__ = "identifier_conflicts"
    identifier_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[str] = mapped_column(Text, primary_key=True)
    claimed_by_paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    provider_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"))
    observed_at: Mapped[dt.datetime] = mapped_column(TS)


# -------------------------
# Historical run snapshots (immutable)
# -------------------------

class RunSnapshot(Base):
    """One row per snapshot on the ``snapshots`` branch, payload kept verbatim."""

    __tablename__ = "run_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    radar_id: Mapped[str] = mapped_column(Text, index=True)
    captured_at: Mapped[dt.datetime] = mapped_column(TS)
    source: Mapped[str] = mapped_column(String(32))
    ranking_version: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[Optional[str]] = mapped_column(Text)
    window_days: Mapped[Optional[int]] = mapped_column(Integer)
    window: Mapped[Optional[Dict[str, Any]]] = mapped_column()
    n_items: Mapped[int] = mapped_column(Integer)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    provenance: Mapped[Dict[str, Any]] = mapped_column(server_default="{}")
    payload: Mapped[Dict[str, Any]] = mapped_column()
    __table_args__ = (UniqueConstraint("radar_id", "captured_at", "payload_sha256"),)


class SnapshotItem(Base):
    """The items of a snapshot, flattened (ETHER-era sections or CAROUSEL run items)."""

    __tablename__ = "snapshot_items"
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("run_snapshots.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)  # order within the snapshot, from 1
    section: Mapped[Optional[str]] = mapped_column(Text)              # ETHER theme
    rank: Mapped[Optional[int]] = mapped_column(Integer)
    title: Mapped[Optional[str]] = mapped_column(Text)
    pmid: Mapped[Optional[str]] = mapped_column(Text, index=True)
    doi: Mapped[Optional[str]] = mapped_column(Text, index=True)
    score: Mapped[Optional[float]] = mapped_column(Float)
    paper_id: Mapped[Optional[str]] = mapped_column(ForeignKey("papers.id"), index=True)  # linked when known
    raw: Mapped[Dict[str, Any]] = mapped_column()

# -------------------------
# Citation graph (plan task 32)
# -------------------------

class CitationEdge(Base):
    """One citation (OpenAlex work IDs) as first observed: append-only, never updated (carousel.search.citations)."""

    __tablename__ = "citation_edges"
    __table_args__ = (UniqueConstraint("citing", "cited", "source"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    citing: Mapped[str] = mapped_column(Text)
    cited: Mapped[str] = mapped_column(Text, index=True)
    source: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[dt.datetime] = mapped_column(TS)


class ReferenceList(Base):
    """When a work's whole reference list was read, and how long it was: append-only."""

    __tablename__ = "reference_lists"
    __table_args__ = (UniqueConstraint("work_id", "source", "observed_at"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    work_id: Mapped[str] = mapped_column(Text, index=True)
    source: Mapped[str] = mapped_column(String(16))
    n_refs: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[dt.datetime] = mapped_column(TS)
