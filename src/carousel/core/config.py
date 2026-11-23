"""
Radar configuration: the YAML contract for a CAROUSEL radar.

A radar is a saved literature monitor. Its config states what to search
(sources, queries, exclusions, filters), what to prioritize (signal profile),
and how to rank (ranking weights). `load_radar` parses a YAML file, validates it
strictly, and raises `ConfigError` with file and line locations on failure.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
V2_FIELDS = ("mode", "methods_profile", "seed_papers", "publication_types", "research_question", "source_queries")

_SLUG = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_DATE_RANGE = re.compile(r"^last_(\d+)_days$")


class ConfigError(Exception):
    """A radar config could not be read or is invalid. `str()` is user-facing."""


# -------------------------
# Models
# -------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Source(str, Enum):
    pubmed = "pubmed"
    europepmc = "europepmc"
    biorxiv = "biorxiv"
    medrxiv = "medrxiv"
    crossref = "crossref"
    openalex = "openalex"
    arxiv = "arxiv"


class OutputFormat(str, Enum):
    dashboard = "dashboard"
    json = "json"
    csv = "csv"
    bibtex = "bibtex"
    ris = "ris"
    markdown = "markdown"
    rss = "rss"


def _clean_terms(values: List[str], what: str, one: str) -> List[str]:
    """Strip, drop empties, and reject case-insensitive duplicates."""
    out: List[str] = []
    seen: Dict[str, str] = {}
    for v in values:
        v = v.strip()
        if not v:
            raise ValueError(f"{what} must not contain empty strings")
        key = v.casefold()
        if key in seen:
            raise ValueError(f"duplicate {one}: {v!r}")
        seen[key] = v
        out.append(v)
    return out


class Filters(_Strict):
    date_range: str = Field("last_90_days", description="Candidate window, as last_<N>_days.")
    language: str = "english"

    @field_validator("date_range")
    @classmethod
    def _check_range(cls, v: str) -> str:
        m = _DATE_RANGE.match(v)
        if not m or int(m.group(1)) < 1:
            raise ValueError("must look like 'last_<N>_days' with N >= 1, e.g. 'last_90_days'")
        return v

    @property
    def days(self) -> int:
        return int(_DATE_RANGE.match(self.date_range).group(1))  # type: ignore[union-attr]


class SignalCategory(_Strict):
    description: str = ""
    terms: List[str] = Field(min_length=1)
    weight: float = Field(1.0, gt=0, le=10)

    @field_validator("terms")
    @classmethod
    def _terms(cls, v: List[str]) -> List[str]:
        return _clean_terms(v, "terms", "term")


class LegacyScoring(_Strict):
    """ETHER-era weights. Accepted so existing configs load; not used by the v0.1 ranker."""

    topic_relevance_weight: float = Field(ge=0)
    recency_weight: float = Field(ge=0)
    signal_weight: float = Field(ge=0)
    source_weight: float = Field(ge=0)


SIGNALS = ("relevance", "novelty", "method_match", "momentum", "discovery", "evidence")


class Mode(str, Enum):
    """Precision/discovery trade-off for selection (see carousel.ranking.MODES)."""

    focused = "focused"
    balanced = "balanced"
    exploratory = "exploratory"


class RankingWeights(_Strict):
    """Weights for the CAROUSEL signals. Defaults are the blueprint's starting values.

    ``method_match`` is new in schema v2. Signals a run can't compute yet
    (momentum, discovery, evidence in Phase 1) are reported inactive and the
    others are rescaled.
    """

    relevance: float = Field(0.40, ge=0)
    novelty: float = Field(0.20, ge=0)
    method_match: float = Field(0.10, ge=0)
    momentum: float = Field(0.15, ge=0)
    discovery: float = Field(0.10, ge=0)
    evidence: float = Field(0.15, ge=0)

    @model_validator(mode="after")
    def _not_all_zero(self) -> "RankingWeights":
        if sum(self.as_dict().values()) <= 0:
            raise ValueError("at least one ranking weight must be > 0")
        return self

    def as_dict(self) -> Dict[str, float]:
        return {k: float(getattr(self, k)) for k in SIGNALS}

    def normalized(self, active: Optional[Sequence[str]] = None) -> Dict[str, float]:
        """Weights rescaled to sum to 1 over `active` signals (all by default).

        Inactive signals get 0 rather than dragging every score down.
        """
        w = self.as_dict()
        keys = list(w) if active is None else [k for k in w if k in set(active)]
        total = sum(w[k] for k in keys)
        if total <= 0:
            raise ValueError(f"no positive weight among active signals {sorted(keys)}")
        return {k: (w[k] / total if k in keys else 0.0) for k in w}


class RadarConfig(_Strict):
    schema_version: int = SCHEMA_VERSION
    radar_id: Optional[str] = Field(None, description="Stable slug; defaults to the file name.")
    radar_name: str = Field(min_length=1)
    description: str = ""
    sources: List[Source] = Field(min_length=1)
    queries: List[str] = Field(min_length=1)
    exclude: List[str] = Field(default_factory=list, description="Terms that remove a candidate.")
    filters: Filters = Field(default_factory=Filters)
    signal_profile: Dict[str, SignalCategory] = Field(default_factory=dict)
    ranking: RankingWeights = Field(default_factory=RankingWeights)
    scoring: Optional[LegacyScoring] = None
    outputs: List[OutputFormat] = Field(default_factory=lambda: [OutputFormat.dashboard])
    # ---- schema v2 ----
    mode: Mode = Field(Mode.balanced, description="focused | balanced | exploratory")
    research_question: str = Field("", description="Optional; joins the interest profile for relevance.")
    methods_profile: List[str] = Field(
        default_factory=list,
        description="Methods, assays, data types to match (falls back to signal_profile.methods terms).",
    )
    seed_papers: List[str] = Field(default_factory=list, description="DOIs of papers that define the radar.")
    publication_types: List[str] = Field(default_factory=list, description="Keep only these types (empty = all).")
    source_queries: Dict[Source, List[str]] = Field(
        default_factory=dict, description="Per-source query overrides, e.g. native PubMed syntax."
    )

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, v: int) -> int:
        if v not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported schema_version {v}; this CAROUSEL reads versions "
                f"{', '.join(map(str, SUPPORTED_SCHEMA_VERSIONS))}"
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _v2_fields_need_v2(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("schema_version") == 1:
            used = [f for f in V2_FIELDS if f in data]
            if used:
                raise ValueError(f"{', '.join(used)} need schema_version: 2 (or remove schema_version: 1)")
        return data

    @field_validator("methods_profile")
    @classmethod
    def _methods(cls, v: List[str]) -> List[str]:
        return _clean_terms(v, "methods_profile", "method")

    @field_validator("publication_types")
    @classmethod
    def _types(cls, v: List[str]) -> List[str]:
        return _clean_terms(v, "publication_types", "publication type")

    @field_validator("seed_papers")
    @classmethod
    def _seeds(cls, v: List[str]) -> List[str]:
        from carousel.core.identifiers import IdentifierError, normalize_doi

        out = []
        for doi in v:
            try:
                out.append(normalize_doi(doi))
            except IdentifierError as exc:
                raise ValueError(str(exc)) from None
        dupes = sorted({d for d in out if out.count(d) > 1})
        if dupes:
            raise ValueError(f"duplicate seed papers: {', '.join(dupes)}")
        return out

    @field_validator("source_queries")
    @classmethod
    def _source_queries(cls, v: Dict[Source, List[str]]) -> Dict[Source, List[str]]:
        return {k: _clean_terms(q, f"source_queries.{k.value}", "query") for k, q in v.items()}

    @field_validator("radar_id")
    @classmethod
    def _radar_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _SLUG.match(v):
            raise ValueError("must be lowercase letters/digits separated by '-' or '_', e.g. 'viral-evolution'")
        return v

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, v: List[Source]) -> List[Source]:
        dupes = sorted({s.value for s in v if v.count(s) > 1})
        if dupes:
            raise ValueError(f"duplicate sources: {', '.join(dupes)}")
        return v

    @field_validator("queries")
    @classmethod
    def _queries(cls, v: List[str]) -> List[str]:
        return _clean_terms(v, "queries", "query")

    @field_validator("exclude")
    @classmethod
    def _exclude(cls, v: List[str]) -> List[str]:
        return _clean_terms(v, "exclude terms", "exclude term")

    @field_validator("signal_profile")
    @classmethod
    def _profile_keys(cls, v: Dict[str, SignalCategory]) -> Dict[str, SignalCategory]:
        bad = [k for k in v if not _SLUG.match(k)]
        if bad:
            raise ValueError(f"category names must be lowercase snake_case, got: {', '.join(map(repr, bad))}")
        return v

    @model_validator(mode="after")
    def _source_queries_use_listed_sources(self) -> "RadarConfig":
        extra = sorted(k.value for k in self.source_queries if k not in self.sources)
        if extra:
            raise ValueError(f"source_queries for sources not in `sources`: {', '.join(extra)}")
        return self

    @model_validator(mode="after")
    def _exclusions_do_not_cancel_queries(self) -> "RadarConfig":
        q = {s.casefold() for s in self.queries}
        clash = [e for e in self.exclude if e.casefold() in q]
        if clash:
            raise ValueError(f"exclude terms also appear in queries: {', '.join(map(repr, clash))}")
        return self

    # ---- derived ----

    def method_terms(self) -> List[str]:
        """``methods_profile``, or the terms of a ``signal_profile`` category named ``methods``."""
        if self.methods_profile:
            return list(self.methods_profile)
        methods = self.signal_profile.get("methods")
        return list(methods.terms) if methods else []

    def config_hash(self) -> str:
        """Short, stable hash of the resolved config (defaults included)."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    def warnings(self) -> List[str]:
        out: List[str] = []
        if self.scoring is not None:
            out.append(
                "scoring: legacy ETHER weights are accepted but not used by the CAROUSEL ranker; "
                "set `ranking:` (relevance, novelty, momentum, discovery, evidence) instead"
            )
        if not self.signal_profile:
            out.append("signal_profile: empty; ranking will rely on queries alone")
        return out


# -------------------------
# Loading
# -------------------------

class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last one."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Dict[Any, Any]:
    seen: Dict[Any, yaml.Node] = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r} (first defined on line {seen[key].start_mark.line + 1})",
                key_node.start_mark,
            )
        seen[key] = key_node
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _node_at(
    root: Optional[yaml.Node], loc: Tuple[Union[str, int], ...], key: bool = False
) -> Optional[yaml.Node]:
    """Walk a composed YAML node tree along a pydantic error location.

    With key=True, return the mapping key node for the last step (for unknown-field errors).
    """
    node = root
    for i, part in enumerate(loc):
        if isinstance(node, yaml.MappingNode):
            want_key = key and i == len(loc) - 1
            node = next(
                (k if want_key else v for k, v in node.value if getattr(k, "value", None) == str(part)), None
            )
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value):
            node = node.value[part]
        else:
            return node
        if node is None:
            return None
    return node


def _format_validation_error(path: str, exc: ValidationError, root: Optional[yaml.Node]) -> str:
    lines = [f"{path}: invalid radar config ({exc.error_count()} problem{'s' if exc.error_count() != 1 else ''})"]
    for err in exc.errors():
        loc = tuple(p for p in err["loc"] if not (isinstance(p, str) and p.startswith("function-")))
        dotted = ".".join(str(p) for p in loc) or "<root>"
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        if err["type"] == "extra_forbidden":
            msg = "unknown field (check spelling)"
        elif err["type"] == "enum":
            msg = f"{msg} (got {err.get('input')!r})"
        node = _node_at(root, loc, key=err["type"] == "extra_forbidden")
        where = f"line {node.start_mark.line + 1}: " if node is not None else ""
        lines.append(f"  - {where}{dotted}: {msg}")
    return "\n".join(lines)


def default_radar_id(path: Union[str, Path]) -> str:
    stem = Path(path).name
    for suffix in (".yaml", ".yml"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "radar"


def load_radar(path: Union[str, Path]) -> RadarConfig:
    """Read and validate a radar YAML file. Raises ConfigError with a readable message."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{p}: file not found") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{p}: cannot read file ({exc})") from None

    try:
        root = yaml.compose(text, Loader=_UniqueKeyLoader)
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f":{mark.line + 1}:{mark.column + 1}" if mark else ""
        problem = exc.problem or str(exc)
        raise ConfigError(f"{p}{where}: YAML syntax error: {problem}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: YAML error: {exc}") from None

    if data is None:
        raise ConfigError(f"{p}: file is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be a mapping of fields (radar_name:, sources:, ...)")

    try:
        cfg = RadarConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(str(p), exc, root)) from None

    if cfg.radar_id is None:
        cfg = cfg.model_copy(update={"radar_id": default_radar_id(p)})
    return cfg

