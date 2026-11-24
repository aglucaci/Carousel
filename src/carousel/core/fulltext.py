"""Texts and annotations attached to papers (Phase 2: Europe PMC), each with its licence.

CAROUSEL keeps a paper's **full text** only when its licence allows reuse
(a Creative Commons licence or a public-domain dedication), and records that
licence on the text itself: which licence, where it was read (the text's own
JATS ``<license>`` or the source's metadata), and what it permits
(:data:`REUSE`). Grounded extraction (plan task 22) reads ``reuse`` before it
uses a text. Nothing else about a text is inferred.

**Annotations** are text-mined entities (organisms, diseases, genes and
proteins, chemicals, ...) that a source attached to a paper, summarized as
one row per entity with its mention count.
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from carousel.core.paper import UTCDateTime

REUSE = ("open", "noncommercial", "no_derivatives", "restricted", "unknown")
"""What a licence allows: ``open`` (CC BY, CC BY-SA, CC0, public domain), ``noncommercial`` (an NC
variant), ``no_derivatives`` (an ND variant), ``restricted`` (any other licence), ``unknown`` (none stated)."""

_CC_URL = re.compile(r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)(?:/(\d+(?:\.\d+)?))?", re.I)
_CC_NAME = re.compile(r"\bcc[\s_-]*(0|zero|by(?:[\s_-]*(?:nc|nd|sa))*)\b(?:[\s_-]*(\d+(?:\.\d+)?))?", re.I)


def normalize_license(value: Optional[str]) -> Optional[str]:
    """``https://creativecommons.org/licenses/by-nc/4.0/`` / ``CC BY-NC 4.0`` / ``cc by-nc`` -> ``cc-by-nc-4.0`` / ``cc-by-nc``.

    Public-domain dedications become ``cc0`` (or ``public-domain``). Anything else is returned
    lower-cased and trimmed, so it is still recorded verbatim enough to check.
    """
    if not value or not value.strip():
        return None
    v = " ".join(value.split())
    m = _CC_URL.search(v)
    if m:
        kind, code, version = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if kind == "publicdomain":
            return "cc0" + (f"-{version}" if version and code == "zero" else "") if code == "zero" else "public-domain"
        return f"cc-{code}" + (f"-{version}" if version else "")
    m = _CC_NAME.search(v)
    if m:
        code = re.sub(r"[\s_-]+", "-", m.group(1).lower())
        code = "0" if code in ("0", "zero") else code
        return (f"cc{code}" if code == "0" else f"cc-{code}") + (f"-{m.group(2)}" if m.group(2) else "")
    if re.search(r"public[\s-]*domain", v, re.I):
        return "public-domain"
    return v.lower()[:200]


def reuse_of(license_id: Optional[str]) -> str:
    """What a normalized licence allows (one of :data:`REUSE`)."""
    if not license_id:
        return "unknown"
    if license_id.startswith("cc0") or license_id == "public-domain":
        return "open"
    if license_id.startswith("cc-by"):
        parts = set(re.sub(r"-\d+(\.\d+)?$", "", license_id).split("-")[2:])
        if "nd" in parts:
            return "no_derivatives"
        if "nc" in parts:
            return "noncommercial"
        return "open"
    return "restricted"


def is_reusable(license_id: Optional[str]) -> bool:
    """Creative Commons or public domain: CAROUSEL may keep a copy of the text (with its licence)."""
    return reuse_of(license_id) in ("open", "noncommercial", "no_derivatives")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Section(_Strict):
    title: str = ""
    text: str


class PaperText(_Strict):
    """One text of one paper, from one source, with the licence it was published under."""

    source: str = Field(description="Where the text came from, e.g. europepmc")
    source_id: str = Field(description="The source's ID for the text, e.g. PMC13778503")
    kind: str = Field(default="full_text", description="full_text")
    format: str = Field(default="jats", description="The format it was parsed from")
    license: Optional[str] = Field(description="Normalized licence, e.g. cc-by-4.0; null if none was stated")
    license_url: Optional[str] = None
    license_source: str = Field(description="Where the licence was read: text (the document's own) | metadata")
    reuse: str = Field(description="What the licence allows: " + " | ".join(REUSE))
    url: Optional[str] = Field(default=None, description="Human-readable location of the text")
    sections: List[Section]
    retrieved_at: UTCDateTime

    @property
    def text(self) -> str:
        return "\n\n".join(f"{s.title}\n{s.text}" if s.title else s.text for s in self.sections)

    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class Annotation(_Strict):
    """A text-mined entity a source attached to a paper (one row per entity)."""

    type: str = Field(description="Organisms | Diseases | Gene_Proteins | Chemicals | Gene Ontology | ...")
    name: str
    uri: Optional[str] = None
    mentions: int = Field(ge=1)
    sections: List[str] = Field(default_factory=list, description="Where it was mentioned (Abstract, Methods, ...)")
    provider: Optional[str] = None


def annotation_key(a: Annotation) -> Tuple[str, str, str]:
    return a.type, (a.uri or "").lower(), a.name.lower()
