"""Scholarly identifier normalization.

Every identifier is stored in one canonical spelling so that exact matching
across sources is a string comparison:

=========  ===========================  ==============================
scheme     canonical form               accepted inputs (examples)
=========  ===========================  ==============================
doi        ``10.1038/s41586-020-2012-7``  ``https://doi.org/10.1038/S41586...``, ``doi:10.1038/...``
pmid       ``31452104``                 ``PMID: 031452104``, ``https://pubmed.ncbi.nlm.nih.gov/31452104/``
pmcid      ``PMC6779135``               ``pmc6779135``, ``https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6779135/``
openalex   ``W2741809807``              ``https://openalex.org/W2741809807``, ``w2741809807``
arxiv      ``2101.00001``               ``arXiv:2101.00001v2``, ``https://arxiv.org/abs/hep-th/9901001v1``
=========  ===========================  ==============================

arXiv versions are dropped: a version is a property of a source record, not of
the paper's identity. DOIs are case-insensitive (DOI Handbook §2.4), so they
are lowercased.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Callable, Dict, Optional, Tuple

SCHEMES: Tuple[str, ...] = ("doi", "pmid", "pmcid", "openalex", "arxiv")
"""Identifier schemes in matching priority order (most trusted first)."""


class IdentifierError(ValueError):
    """An identifier could not be parsed."""


_DOI_URL = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.I)
_DOI_PREFIX = re.compile(r"^doi:\s*", re.I)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")


def normalize_doi(value: str) -> str:
    v = value.strip()
    if _DOI_URL.match(v):
        v = urllib.parse.unquote(_DOI_URL.sub("", v))
    v = _DOI_PREFIX.sub("", v).strip().lower()
    if not _DOI.match(v):
        raise IdentifierError(f"not a DOI: {value!r}")
    return v


_PMID_PREFIX = re.compile(r"^(?:pmid:?\s*|https?://(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/)", re.I)


def normalize_pmid(value: str) -> str:
    v = _PMID_PREFIX.sub("", str(value).strip()).rstrip("/")
    if not v.isdigit() or int(v) == 0:
        raise IdentifierError(f"not a PMID: {value!r}")
    return str(int(v))


_PMCID_URL = re.compile(r"^https?://(?:www\.)?(?:ncbi\.nlm\.nih\.gov/pmc/articles|pmc\.ncbi\.nlm\.nih\.gov/articles)/", re.I)
_PMCID = re.compile(r"^PMC(\d+)$", re.I)


def normalize_pmcid(value: str) -> str:
    v = _PMCID_URL.sub("", value.strip()).rstrip("/")
    m = _PMCID.match(v)
    if not m or int(m.group(1)) == 0:
        raise IdentifierError(f"not a PMCID (expected PMC<digits>): {value!r}")
    return f"PMC{int(m.group(1))}"


_OPENALEX_URL = re.compile(r"^https?://(?:api\.)?openalex\.org/(?:works/)?", re.I)
_OPENALEX = re.compile(r"^W(\d+)$", re.I)


def normalize_openalex(value: str) -> str:
    v = _OPENALEX_URL.sub("", value.strip()).rstrip("/")
    m = _OPENALEX.match(v)
    if not m:
        raise IdentifierError(f"not an OpenAlex work ID (expected W<digits>): {value!r}")
    return f"W{m.group(1)}"


_ARXIV_PREFIX = re.compile(r"^(?:arxiv:\s*|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)", re.I)
_ARXIV_NEW = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
_ARXIV_OLD = re.compile(r"^([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$")


def normalize_arxiv(value: str) -> str:
    v = _ARXIV_PREFIX.sub("", value.strip()).rstrip("/")
    if v.lower().endswith(".pdf"):
        v = v[:-4]
    m = _ARXIV_NEW.match(v) or _ARXIV_OLD.match(v)
    if not m:
        raise IdentifierError(f"not an arXiv ID: {value!r}")
    return m.group(1)


NORMALIZERS: Dict[str, Callable[[str], str]] = {
    "doi": normalize_doi,
    "pmid": normalize_pmid,
    "pmcid": normalize_pmcid,
    "openalex": normalize_openalex,
    "arxiv": normalize_arxiv,
}


def normalize(scheme: str, value: str) -> str:
    try:
        fn = NORMALIZERS[scheme]
    except KeyError:
        raise IdentifierError(f"unknown identifier scheme {scheme!r} (known: {', '.join(SCHEMES)})") from None
    return fn(value)


def try_normalize(scheme: str, value: Optional[str]) -> Optional[str]:
    """Normalize, returning None for empty or unparseable input (for lenient connectors)."""
    if value is None or not str(value).strip():
        return None
    try:
        return normalize(scheme, str(value))
    except IdentifierError:
        return None
