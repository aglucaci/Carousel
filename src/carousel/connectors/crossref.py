"""Crossref connector (https://api.crossref.org): DOI metadata, corrections and retractions, preprint links.

CAROUSEL uses Crossref to **verify** the DOIs that its search sources found,
not to search: Crossref's free-text search has no boolean operators, so it
cannot express a radar's queries and exclusions. A radar opts in by listing
``crossref`` in its ``sources``; each scan then looks up the DOIs of the papers
it observed (:meth:`CrossrefConnector.fetch`) and records:

- **updates** from ``updated-by``: corrections, retractions, expressions of
  concern, ... with who reported them (``publisher`` or ``retraction-watch``;
  Crossref has carried the Retraction Watch database since 2023). A
  retraction, removal, or withdrawal sets ``is_retracted``, which flags the
  paper and keeps it out of recommendations;
- **notices**: a record that *is* a correction or retraction (``update-to``)
  gets that update type among its ``publication_types``;
- **relations**: ``has-preprint`` / ``is-preprint-of`` links (stored for
  preprint-publication linking; the records are never collapsed);
- the version-of-record dates, venue, authors (with ORCID iDs), and type.

Lookups are batched (``filter=doi:a,doi:b,...``) and paced for Crossref's
public pool. No contact email is sent; set ``CROSSREF_MAILTO`` to use the
"polite" pool. It is sent as a parameter but never stored in caches or fixtures.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence

from pydantic import ValidationError

from carousel.connectors.base import (
    Budget,
    BudgetExceeded,
    Connector,
    ConnectorError,
    HttpClient,
    Request,
    SearchQuery,
    SearchResult,
)
from carousel.core.identifiers import try_normalize
from carousel.core.paper import RETRACTING_UPDATES, Author, Relation, SourceRecord, UpdateNotice, update_type

API = "https://api.crossref.org/works"
BATCH = 20  # DOIs per request; keeps URLs well under common limits
DEFAULT_BUDGET_REQUESTS = 60  # per scan: up to 1,200 DOIs
SELECT = ",".join((
    "DOI", "URL", "type", "title", "abstract", "author", "container-title", "published",
    "published-online", "published-print", "posted", "issued", "deposited", "updated-by", "update-to", "relation",
))
PREPRINT_RELATIONS = frozenset({"has-preprint", "is-preprint-of"})

_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"^\s*(abstract|summary)\s*", re.I)


def _date(part: Optional[Dict[str, Any]]) -> Optional[str]:
    """Crossref ``{"date-parts": [[2026, 3, 14]]}`` -> ``2026-03-14`` (or ``2026-03`` / ``2026``)."""
    try:
        parts = [int(x) for x in ((part or {}).get("date-parts") or [[]])[0] if x is not None]
    except (TypeError, ValueError):
        return None
    if not parts or not 1000 <= parts[0] <= 9999:
        return None
    return "-".join([f"{parts[0]:04d}", *(f"{p:02d}" for p in parts[1:3])])


def _text(value: Any) -> Optional[str]:
    if isinstance(value, list):
        value = value[0] if value else None
    return " ".join(str(value).split()) or None if value else None


def jats_to_text(abstract: Optional[str]) -> Optional[str]:
    """Crossref abstracts are JATS XML (``<jats:p>...``); keep the words, drop the markup and a leading 'Abstract'."""
    if not abstract:
        return None
    text = " ".join(_TAG.sub(" ", abstract).split())
    text = _TITLE.sub("", text, count=1)
    return text or None


def _updates(items: Optional[List[Dict[str, Any]]]) -> List[UpdateNotice]:
    out = []
    for u in items or []:
        if not u.get("type"):
            continue
        try:
            out.append(UpdateNotice(type=u["type"], notice_doi=u.get("DOI"), date=_date(u.get("updated")),
                                    asserted_by=u.get("source"), label=u.get("label")))
        except (ValidationError, ValueError):
            continue
    return out


def _relations(relation: Optional[Dict[str, Any]]) -> List[Relation]:
    out = []
    for rtype, targets in (relation or {}).items():
        if rtype not in PREPRINT_RELATIONS:
            continue
        for t in targets or []:
            scheme = (t.get("id-type") or "").lower()
            value = t.get("id")
            if scheme != "doi" or not value or try_normalize("doi", value) is None:
                continue
            out.append(Relation(type=rtype, scheme="doi", value=value, asserted_by=t.get("asserted-by")))
    return out


def parse_work(work: Dict[str, Any]) -> Optional[SourceRecord]:
    """One Crossref work JSON -> SourceRecord (None without a valid DOI)."""
    doi = try_normalize("doi", work.get("DOI"))
    if doi is None:
        return None
    authors: List[Author] = []
    for a in work.get("author") or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name") or ""
        if not name.strip():
            continue
        try:
            authors.append(Author(name=name.strip(), orcid=a.get("ORCID")))
        except ValidationError:
            authors.append(Author(name=name.strip()))

    wtype = work.get("type")
    updates = _updates(work.get("updated-by"))
    notices = [update_type(u["type"]) for u in work.get("update-to") or [] if u.get("type")]
    types = [wtype] if wtype else []
    types += [t for t in dict.fromkeys(notices) if t not in types]

    posted = _date(work.get("posted"))
    is_preprint = wtype == "posted-content"
    dates: Dict[str, Optional[str]] = {"source_updated": _date(work.get("deposited"))}
    if is_preprint:
        dates["preprint_posted"] = posted or _date(work.get("issued"))
    else:
        dates["published"] = _date(work.get("published-print")) or _date(work.get("published")) or _date(work.get("issued"))
        dates["first_online"] = _date(work.get("published-online"))

    return SourceRecord(
        source="crossref",
        source_id=doi,
        identifiers={"doi": doi},
        title=_text(work.get("title")),
        abstract=jats_to_text(work.get("abstract")),
        authors=authors,
        venue=_text(work.get("container-title")),
        publication_types=types,
        is_retracted=any(u.type in RETRACTING_UPDATES for u in updates),
        url=work.get("URL"),
        dates={k: v for k, v in dates.items() if v},
        updates=updates,
        relations=_relations(work.get("relation")),
    )


class CrossrefConnector(Connector):
    name = "crossref"
    version = "0.1.0"
    cost_unit = "requests"

    def __init__(self, client: Optional[HttpClient] = None, mailto: Optional[str] = None,
                 budget_requests: Optional[float] = DEFAULT_BUDGET_REQUESTS) -> None:
        self.mailto = (mailto if mailto is not None else os.environ.get("CROSSREF_MAILTO")) or None
        if client is None:
            # Public pool: 5 requests/s, one at a time (polite pool: 10/s).
            client = HttpClient(min_interval=0.12 if self.mailto else 0.25,
                                budget=Budget(budget_requests, unit="requests"))
        super().__init__(client)

    def _request(self, params: Dict[str, Any]) -> Request:
        return Request.build(API, params, secret_params={"mailto": self.mailto} if self.mailto else None)

    def search(self, query: SearchQuery) -> SearchResult:
        raise ConnectorError("Crossref verifies DOIs found by the other sources; it is not searched "
                             "(its free-text search cannot express a radar's boolean queries)")

    def lookup(self, query: SearchQuery, ids: Sequence[str]) -> SearchResult:
        """Verify the DOIs a scan found. Stops early (truncated, with the reason) on the budget or an error."""
        before = self._snapshot_stats()
        dois = sorted({d for d in (try_normalize("doi", i) for i in ids) if d})
        records: List[SourceRecord] = []
        errors: List[str] = []
        reason: Optional[str] = None
        pages = 0
        for i in range(0, len(dois), BATCH):
            try:
                records.extend(self._batch(dois[i : i + BATCH]))
            except ConnectorError as exc:
                errors.append(str(exc))
                reason = "budget" if isinstance(exc, BudgetExceeded) else "error"
                break
            pages += 1
        looked_up = min(len(dois), pages * BATCH)
        coverage = self._coverage(
            query, before,
            native_query=f"DOI lookup: {len(dois)} DOIs from this scan's papers (filter=doi:..., {BATCH} per request)",
            date_field="none (looks up DOIs; no date window)",
            total_available=len(dois),
            retrieved=len(records),
            truncated=reason is not None,
            truncation_reason=reason,
            pages=pages,
            errors=errors,
        )
        if looked_up > len(records):
            coverage.errors.append(f"{looked_up - len(records)} of {looked_up} DOIs are not registered with Crossref "
                                   "(other agencies such as DataCite, or not deposited)")
        return SearchResult(records=records, coverage=coverage)

    def _batch(self, dois: Sequence[str]) -> List[SourceRecord]:
        params = {"filter": ",".join(f"doi:{d}" for d in dois), "rows": len(dois), "select": SELECT}
        items = (self.client.get(self._request(params)).json().get("message") or {}).get("items") or []
        return [r for r in (parse_work(w) for w in items) if r is not None]

    def fetch(self, ids: Sequence[str]) -> List[SourceRecord]:
        """Look up DOIs (any form: ``10.x/y``, ``doi:``, ``https://doi.org/``). Unknown DOIs are skipped."""
        dois = sorted({d for d in (try_normalize("doi", i) for i in ids) if d})
        out: List[SourceRecord] = []
        for i in range(0, len(dois), BATCH):
            out.extend(self._batch(dois[i : i + BATCH]))
        return out
