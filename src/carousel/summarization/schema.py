"""The grounded extraction schema (plan task 22, spec §7), version :data:`SCHEMA_VERSION`.

An extraction answers a fixed set of questions about one paper. Every answer
is a list of **claims**, and every claim cites the **passages** it rests on
with an exact quote, so a reader can check it. A field the text does not
report is ``not_reported``: nothing is inferred, guessed, or filled in from
outside knowledge. The extraction also says what it read (``coverage``): the
abstract only, a licensed full text, or text the user supplied.

Fields (version 1):

=====================  =========================================================
``objective``          what the study set out to do
``study_design``       e.g. cohort, randomized trial, phylogenetic analysis, in vitro
``population``         organisms, populations, samples, or systems studied
``sample_size``        how many (patients, sequences, isolates, ...)
``methods``            methods, assays, data types, models
``findings``           what the authors report finding
``limitations``        limitations the authors state
``data_availability``  where data or code are available
=====================  =========================================================
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

FIELDS: Dict[str, str] = {
    "objective": "What the study set out to do (aim, question, or hypothesis).",
    "study_design": "The kind of study, e.g. cohort, randomized trial, phylogenetic analysis, in vitro experiment.",
    "population": "Organisms, populations, samples, or systems studied.",
    "sample_size": "How many subjects, samples, sequences, or isolates.",
    "methods": "Methods, assays, data types, and models used.",
    "findings": "What the authors report finding.",
    "limitations": "Limitations the authors themselves state.",
    "data_availability": "Where the data or code are available.",
}
SINGLE = {"objective", "study_design", "data_availability"}
"""Fields that usually have one answer (still a list: a paper can state two)."""
MAX_CLAIMS = 6

TextClass = Literal["public", "private"]
Scope = Literal["abstract_only", "full_text", "user_text"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Passage(_M):
    """A sentence-sized piece of the input text, with a stable ID (``abstract:3``, ``s2.results:14``, ``user:5``)."""

    id: str
    section: str
    text: str
    text_class: TextClass = "public"


class Evidence(_M):
    passage_id: str
    quote: str = Field(description="Copied verbatim from the passage")


class Claim(_M):
    text: str
    evidence: List[Evidence] = Field(min_length=1)


class FieldResult(_M):
    status: Literal["reported", "not_reported"]
    claims: List[Claim] = []


class Rejected(_M):
    field: str
    claim: Dict[str, Any]
    reason: str


class Coverage(_M):
    scope: Scope
    statement: str
    sources: List[Dict[str, Any]] = []
    passages: int


class Extraction(_M):
    paper_id: str
    provider: str
    model_version: str = Field(description="The model (or rule set) that produced it, as served")
    schema_version: str = SCHEMA_VERSION
    input_sha256: str = Field(description="Hash of the passages read; a different text is a different extraction")
    location: Literal["local", "external"]
    text_classes: List[TextClass]
    coverage: Coverage
    fields: Dict[str, FieldResult]
    passages: List[Passage] = Field(description="The cited passages (only those), so every quote can be checked")
    rejected: List[Rejected] = Field([], description="Claims the grounding check dropped, with the reason")
    created_at: Optional[str] = None

    @property
    def private(self) -> bool:
        return "private" in self.text_classes


def input_hash(passages: List[Passage]) -> str:
    blob = json.dumps([[p.id, p.text, p.text_class] for p in passages], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def output_json_schema() -> Dict[str, Any]:
    """The JSON schema a provider's answer must follow (strict: every key required, nothing extra)."""
    claim = {"type": "object", "additionalProperties": False, "required": ["text", "evidence"],
             "properties": {"text": {"type": "string"},
                            "evidence": {"type": "array", "items": {
                                "type": "object", "additionalProperties": False, "required": ["passage_id", "quote"],
                                "properties": {"passage_id": {"type": "string"}, "quote": {"type": "string"}}}}}}
    field = {"type": "object", "additionalProperties": False, "required": ["status", "claims"],
             "properties": {"status": {"type": "string", "enum": ["reported", "not_reported"]},
                            "claims": {"type": "array", "items": claim}}}
    return {"type": "object", "additionalProperties": False, "required": ["fields"],
            "properties": {"fields": {"type": "object", "additionalProperties": False, "required": list(FIELDS),
                                      "properties": {k: field for k in FIELDS}}}}


__all__ = ["Claim", "Coverage", "Evidence", "Extraction", "FIELDS", "FieldResult", "MAX_CLAIMS", "Passage", "Rejected",
           "SCHEMA_VERSION", "SINGLE", "input_hash", "output_json_schema"]
