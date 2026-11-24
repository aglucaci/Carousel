"""Grounded extraction (plan task 22, spec §7): structured facts about a paper, each quoting its source passage.

- :mod:`.schema`: the fields, claims, evidence, and coverage (``SCHEMA_VERSION``)
- :mod:`.passages`: citable sentence-sized passages with stable IDs
- :mod:`.providers`: the provider interface, a local rule-based provider, and Claude
- :mod:`.grounding`: the deterministic check every claim must pass
- :mod:`.extract`: consent, caching, and running it for a stored paper
"""

from carousel.summarization.extract import ConsentRequired, extract_paper, gather
from carousel.summarization.grounding import ground
from carousel.summarization.providers import (PROVIDERS, AnthropicProvider, ExtractionError, RuleBasedProvider,
                                              make_provider)
from carousel.summarization.schema import FIELDS, SCHEMA_VERSION, Extraction, Passage, output_json_schema

__all__ = ["AnthropicProvider", "ConsentRequired", "Extraction", "ExtractionError", "FIELDS", "PROVIDERS", "Passage",
           "RuleBasedProvider", "SCHEMA_VERSION", "extract_paper", "gather", "ground", "make_provider",
           "output_json_schema"]
