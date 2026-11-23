"""Shared configuration and schemas used across CAROUSEL subsystems."""

from carousel.core.config import (
    SCHEMA_VERSION,
    ConfigError,
    Filters,
    LegacyScoring,
    Mode,
    OutputFormat,
    RadarConfig,
    RankingWeights,
    SignalCategory,
    Source,
    load_radar,
)
from carousel.core.identifiers import SCHEMES, IdentifierError
from carousel.core.paper import (
    Author,
    CanonicalPaper,
    FieldProvenance,
    Identifiers,
    PaperDates,
    SourceRecord,
)

__all__ = [
    "Author",
    "CanonicalPaper",
    "FieldProvenance",
    "IdentifierError",
    "Identifiers",
    "PaperDates",
    "SCHEMES",
    "SourceRecord",
    "SCHEMA_VERSION",
    "ConfigError",
    "Filters",
    "LegacyScoring",
    "Mode",
    "OutputFormat",
    "RadarConfig",
    "RankingWeights",
    "SignalCategory",
    "Source",
    "load_radar",
]
