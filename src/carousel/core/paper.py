"""Canonical paper model.

A paper is observed through one or more *source records*: what PubMed,
OpenAlex, bioRxiv, and so on said about it, each with its own
``first_seen`` / ``last_verified`` timestamps. The *canonical paper* is derived
from its source records, never edited by hand:

- Each field is taken from the most trusted source that has a value
  (:data:`FIELD_PRECEDENCE`), and ``provenance`` records which source record it
  came from.
- Every source record is kept, so nothing a source said is lost.
- Retractions are an explicit flag: if *any* source reports a retraction, the
  paper is flagged.
- Dates are kept separately (spec §4.1): publication, first online, preprint
  posted, version, and source updated (all partial dates, because sources
  often give only a year or a month), plus CAROUSEL's own ``first_seen`` and
  ``last_verified`` observation times.

Preprints and their journal versions are separate canonical papers; linking
them is the deduplication step's job (plan task 6).
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
from typing import Annotated, Any, Dict, Iterator, List, Optional, Sequence, Tuple

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from carousel.core.config import Source
from carousel.core.identifiers import SCHEMES, normalize

PAPER_SCHEMA_VERSION = 1


# -------------------------
# Scalar types
# -------------------------

def _check_partial_date(value: str) -> str:
    parts = value.split("-")
    try:
        if len(parts) == 1 and len(parts[0]) == 4:
            dt.date(int(parts[0]), 1, 1)
        elif len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            dt.date(int(parts[0]), int(parts[1]), 1)
        elif len(parts) == 3 and [len(p) for p in parts] == [4, 2, 2]:
            dt.date.fromisoformat(value)
        else:
            raise ValueError
    except ValueError:
        raise ValueError(f"expected YYYY, YYYY-MM, or YYYY-MM-DD; got {value!r}") from None
    return value


PartialDate = Annotated[str, AfterValidator(_check_partial_date)]
"""A calendar date that may be known only to the year or month: ``2026``, ``2026-03``, ``2026-03-14``."""


def partial_date_bounds(value: str) -> Tuple[dt.date, dt.date]:
    """Earliest and latest calendar day a partial date can mean."""
    parts = [int(p) for p in value.split("-")]
    if len(parts) == 3:
        d = dt.date(*parts)
        return d, d
    if len(parts) == 2:
        y, m = parts
        return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])
    return dt.date(parts[0], 1, 1), dt.date(parts[0], 12, 31)


UTCDateTime = Annotated[AwareDatetime, AfterValidator(lambda d: d.astimezone(dt.timezone.utc))]
"""A timezone-aware timestamp, stored in UTC. Naive datetimes are rejected."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# -------------------------
# Components
# -------------------------

class Identifiers(_Strict):
    """Normalized identifiers (see :mod:`carousel.core.identifiers`)."""

    doi: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    openalex: Optional[str] = None
    arxiv: Optional[str] = None

    @field_validator(*SCHEMES, mode="before")
    @classmethod
    def _normalize(cls, v: Any, info: Any) -> Optional[str]:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return normalize(info.field_name, str(v))

    def items(self) -> Iterator[Tuple[str, str]]:
        """(scheme, value) pairs that are set, in matching priority order."""
        for scheme in SCHEMES:
            value = getattr(self, scheme)
            if value is not None:
                yield scheme, value

    def __bool__(self) -> bool:
        return any(True for _ in self.items())


class Author(_Strict):
    name: str = Field(min_length=1)
    orcid: Optional[str] = None

    @field_validator("orcid", mode="before")
    @classmethod
    def _orcid(cls, v: Any) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        s = str(v).strip().rsplit("/", 1)[-1].upper()
        digits = s.replace("-", "")
        if len(digits) != 16 or not digits[:15].isdigit() or not (digits[15].isdigit() or digits[15] == "X"):
            raise ValueError(f"not an ORCID iD: {v!r}")
        return "-".join(digits[i : i + 4] for i in range(0, 16, 4))


class PaperDates(_Strict):
    """Bibliographic dates as reported by sources (spec §4.1)."""

    published: Optional[PartialDate] = None
    """Version-of-record publication date (journal issue / official date)."""
    first_online: Optional[PartialDate] = None
    """First appeared online (epub ahead of print)."""
    preprint_posted: Optional[PartialDate] = None
    """Preprint server posting date of this work."""
    version: Optional[PartialDate] = None
    """Date of the version described (e.g. a preprint's latest revision)."""
    source_updated: Optional[PartialDate] = None
    """When the source last changed its record."""


DATE_FIELDS: Tuple[str, ...] = tuple(PaperDates.model_fields)


RETRACTING_UPDATES = frozenset({"retraction", "removal", "withdrawal"})
"""Update types that take a paper out of the record (any one sets ``is_retracted``)."""

CORRECTING_UPDATES = frozenset({"correction", "corrigendum", "erratum", "partial_retraction", "addendum", "clarification"})


def update_type(value: str) -> str:
    """Normalize an update type: ``Expression of Concern`` / ``expression-of-concern`` -> ``expression_of_concern``."""
    return "_".join(value.strip().lower().replace("-", " ").split())


class UpdateNotice(_Strict):
    """A correction, retraction, expression of concern, ... that updates this paper (Crossref ``updated-by``)."""

    type: str = Field(min_length=1, description="retraction | correction | expression_of_concern | erratum | ...")
    notice_doi: Optional[str] = Field(default=None, description="DOI of the notice itself")
    date: Optional[PartialDate] = None
    asserted_by: Optional[str] = Field(default=None, description="Who reported it to the source: publisher | retraction-watch")
    label: Optional[str] = None
    via: Optional[str] = Field(default=None, description="The source that carried it (set on canonical papers)")

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> str:
        return update_type(str(v))

    @field_validator("notice_doi", mode="before")
    @classmethod
    def _doi(cls, v: Any) -> Optional[str]:
        return normalize("doi", str(v)) if v else None


class Relation(_Strict):
    """A link to another work that a source asserts, e.g. this article ``has-preprint`` 10.1101/... ."""

    type: str = Field(min_length=1, description="has-preprint | is-preprint-of | ... (Crossref relation types)")
    scheme: str = Field(min_length=1, description="Identifier scheme of the related work: doi, arxiv, ...")
    value: str = Field(min_length=1)
    asserted_by: Optional[str] = Field(default=None, description="subject | object (Crossref: which side deposited it)")
    via: Optional[str] = Field(default=None, description="The source that carried it (set on canonical papers)")

    @model_validator(mode="after")
    def _normalize(self) -> "Relation":
        if self.scheme in SCHEMES:
            object.__setattr__(self, "value", normalize(self.scheme, self.value))
        return self


# -------------------------
# Source record
# -------------------------

class SourceRecord(_Strict):
    """What one source said about one paper.

    ``first_seen`` / ``last_verified`` are CAROUSEL's observation times. A
    connector may leave them empty; the store fills them in when the record is
    observed.
    """

    source: Source
    source_id: str = Field(min_length=1, description="The paper's ID at this source (PMID for PubMed, W-ID for OpenAlex, ...)")
    identifiers: Identifiers = Field(default_factory=Identifiers)
    title: Optional[str] = None
    abstract: Optional[str] = None
    authors: List[Author] = Field(default_factory=list)
    venue: Optional[str] = None
    publication_types: List[str] = Field(default_factory=list)
    language: Optional[str] = None
    is_retracted: Optional[bool] = None
    url: Optional[str] = None
    version: Optional[str] = Field(default=None, description="Source version label, e.g. a preprint 'v2'")
    dates: PaperDates = Field(default_factory=PaperDates)
    updates: List[UpdateNotice] = Field(default_factory=list, description="Corrections, retractions, ... of this paper")
    relations: List[Relation] = Field(default_factory=list, description="Links to other works (preprint of, ...)")
    license: Optional[str] = Field(default=None, description="The article's licence as this source states it (normalized)")
    open_access: Optional[bool] = Field(default=None, description="Whether this source says the full text is open access")
    first_seen: Optional[UTCDateTime] = None
    last_verified: Optional[UTCDateTime] = None
    raw: Optional[Dict[str, Any]] = Field(default=None, description="Untouched source payload, for audit")

    @model_validator(mode="after")
    def _times(self) -> "SourceRecord":
        if self.first_seen and self.last_verified and self.last_verified < self.first_seen:
            raise ValueError("last_verified is earlier than first_seen")
        return self

    @property
    def key(self) -> Tuple[str, str]:
        return self.source.value, self.source_id

    def content_sha256(self) -> str:
        """Hash of what the source says, excluding observation times and the raw payload."""
        content = self.model_dump(mode="json", exclude={"first_seen", "last_verified", "raw"})
        for key in ("updates", "relations", "license", "open_access"):  # added later; leaving them out when
            if content[key] in (None, []):                               # empty keeps older hashes stable
                del content[key]
        blob = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -------------------------
# Field resolution
# -------------------------

_ALL = [s.value for s in Source]


def _order(*first: str) -> List[str]:
    return list(first) + [s for s in _ALL if s not in first]


DEFAULT_PRECEDENCE: List[str] = _order("pubmed", "crossref", "europepmc", "openalex", "biorxiv", "medrxiv", "arxiv")

FIELD_PRECEDENCE: Dict[str, List[str]] = {
    # Each identifier scheme is most trusted from the registry that issues it.
    "identifiers.doi": _order("crossref", "pubmed", "europepmc", "biorxiv", "medrxiv", "openalex", "arxiv"),
    "identifiers.pmid": _order("pubmed", "europepmc", "openalex"),
    "identifiers.pmcid": _order("pubmed", "europepmc", "openalex"),
    "identifiers.openalex": _order("openalex"),
    "identifiers.arxiv": _order("arxiv", "openalex"),
    # Crossref often lacks abstracts or carries them as JATS markup.
    "abstract": _order("pubmed", "europepmc", "biorxiv", "medrxiv", "arxiv", "openalex", "crossref"),
    "dates.preprint_posted": _order("biorxiv", "medrxiv", "arxiv", "europepmc", "openalex", "crossref"),
    "dates.version": _order("biorxiv", "medrxiv", "arxiv"),
    "version": _order("biorxiv", "medrxiv", "arxiv"),
    # Europe PMC states licences and open-access status for MEDLINE, PMC, and preprint records.
    "license": _order("europepmc", "crossref", "openalex"),
    "open_access": _order("europepmc", "openalex", "crossref"),
}
"""Per-field source precedence; fields not listed use :data:`DEFAULT_PRECEDENCE`."""

RESOLVED_FIELDS: Tuple[str, ...] = (
    *(f"identifiers.{s}" for s in SCHEMES),
    "title",
    "abstract",
    "authors",
    "venue",
    "publication_types",
    "language",
    "url",
    "version",
    "license",
    "open_access",
    *(f"dates.{d}" for d in DATE_FIELDS),
)


def _get(record: SourceRecord, field: str) -> Any:
    obj: Any = record
    for part in field.split("."):
        obj = getattr(obj, part)
    return obj


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


class FieldProvenance(_Strict):
    source: Source
    source_id: str
    observed_at: UTCDateTime = Field(description="last_verified of the source record the value came from")


class IdentifierConflict(_Strict):
    """Two source records of the same paper disagree on an identifier."""

    scheme: str
    values: Dict[str, str] = Field(description="'source:source_id' -> value")


class CanonicalPaper(_Strict):
    paper_id: str
    identifiers: Identifiers
    title: Optional[str] = None
    abstract: Optional[str] = None
    authors: List[Author] = Field(default_factory=list)
    venue: Optional[str] = None
    publication_types: List[str] = Field(default_factory=list)
    language: Optional[str] = None
    url: Optional[str] = None
    version: Optional[str] = None
    dates: PaperDates
    is_retracted: bool = False
    license: Optional[str] = None
    open_access: Optional[bool] = None
    updates: List[UpdateNotice] = Field(default_factory=list, description="From every source, each with its `via`")
    relations: List[Relation] = Field(default_factory=list, description="From every source, each with its `via`")
    first_seen: UTCDateTime
    last_verified: UTCDateTime
    provenance: Dict[str, FieldProvenance] = Field(default_factory=dict)
    conflicts: List[IdentifierConflict] = Field(default_factory=list)
    sources: List[SourceRecord] = Field(min_length=1)
    schema_version: int = PAPER_SCHEMA_VERSION

    @classmethod
    def from_records(cls, paper_id: str, records: Sequence[SourceRecord]) -> "CanonicalPaper":
        """Derive the canonical paper from its source records. Deterministic in record order."""
        if not records:
            raise ValueError("a canonical paper needs at least one source record")
        for r in records:
            if r.first_seen is None or r.last_verified is None:
                raise ValueError(f"source record {r.key} has no observation times")
        records = sorted(records, key=lambda r: r.key)

        def rank(field: str, r: SourceRecord) -> Tuple[int, float, Tuple[str, str]]:
            order = FIELD_PRECEDENCE.get(field, DEFAULT_PRECEDENCE)
            # Same source twice (rare): prefer the most recently verified record.
            return order.index(r.source.value), -r.last_verified.timestamp(), r.key

        values: Dict[str, Any] = {"identifiers": {}, "dates": {}}
        provenance: Dict[str, FieldProvenance] = {}
        for field in RESOLVED_FIELDS:
            candidates = [r for r in records if not _empty(_get(r, field))]
            if not candidates:
                continue
            best = min(candidates, key=lambda r: rank(field, r))
            value = _get(best, field)
            if "." in field:
                group, name = field.split(".")
                values[group][name] = value
            else:
                values[field] = value
            provenance[field] = FieldProvenance(source=best.source, source_id=best.source_id, observed_at=best.last_verified)

        retracting = [r for r in records if r.is_retracted]
        if retracting:
            r = min(retracting, key=lambda r: rank("is_retracted", r))
            provenance["is_retracted"] = FieldProvenance(source=r.source, source_id=r.source_id, observed_at=r.last_verified)

        updates: Dict[Tuple[str, Optional[str], Optional[str]], UpdateNotice] = {}
        relations: Dict[Tuple[str, str, str], Relation] = {}
        for r in records:  # sorted by source key, so the first source to report one is kept
            for u in r.updates:
                updates.setdefault((u.type, u.notice_doi, u.asserted_by), u.model_copy(update={"via": r.source.value}))
            for rel in r.relations:
                relations.setdefault((rel.type, rel.scheme, rel.value), rel.model_copy(update={"via": r.source.value}))

        conflicts = []
        for scheme in SCHEMES:
            seen = {f"{r.source.value}:{r.source_id}": getattr(r.identifiers, scheme) for r in records}
            seen = {k: v for k, v in seen.items() if v is not None}
            if len(set(seen.values())) > 1:
                conflicts.append(IdentifierConflict(scheme=scheme, values=seen))

        return cls(
            paper_id=paper_id,
            identifiers=Identifiers.model_validate(values.pop("identifiers")),
            dates=PaperDates.model_validate(values.pop("dates")),
            is_retracted=bool(retracting),
            updates=sorted(updates.values(), key=lambda u: (u.date or "", u.type, u.notice_doi or "")),
            relations=sorted(relations.values(), key=lambda x: (x.type, x.scheme, x.value)),
            first_seen=min(r.first_seen for r in records),  # type: ignore[type-var]
            last_verified=max(r.last_verified for r in records),  # type: ignore[type-var]
            provenance=provenance,
            conflicts=conflicts,
            sources=list(records),
            **values,
        )

    # ---- dates ----

    @property
    def earliest_public_date(self) -> Optional[str]:
        """The earliest date this work was publicly available (first online, published, or preprint posted)."""
        dates = [d for d in (self.dates.first_online, self.dates.published, self.dates.preprint_posted) if d]
        return min(dates, key=lambda d: partial_date_bounds(d)[0]) if dates else None

    def is_new(self, as_of: dt.datetime, window_days: int) -> bool:
        """Newly *observed* and recently *published*, relative to ``as_of``.

        An old paper that a database only just indexed is not new. A paper
        with no usable date is not new either. Partial dates get the benefit
        of the doubt (``2026-11`` counts as published on 2026-11-30).
        """
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        cutoff = as_of - dt.timedelta(days=window_days)
        if not (cutoff <= self.first_seen <= as_of):
            return False
        public = self.earliest_public_date
        if public is None:
            return False
        return partial_date_bounds(public)[1] >= cutoff.astimezone(dt.timezone.utc).date()

    def source(self, source: str) -> List[SourceRecord]:
        return [r for r in self.sources if r.source.value == source]


def paper_id_for(record: SourceRecord) -> str:
    """Stable ID for a paper first created from ``record``."""
    return "cp_" + hashlib.sha256(f"{record.source.value}:{record.source_id}".encode()).hexdigest()[:16]
