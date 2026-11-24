"""arXiv connector (https://info.arxiv.org/help/api/): preprints in titles and abstracts, by submission date.

**Search** sends one boolean query over titles and abstracts (``ti:``/``abs:``
phrases, ``ANDNOT`` for exclusions) limited to the window by
``submittedDate`` (the first version's submission), newest first, paged with
``start``/``max_results``. The total comes from ``opensearch:totalResults``.

**Records** are preprints:

- ``source_id`` is the arXiv ID without its version (``2610.01111``); the
  version (``v2``) is kept on the record, and its date as ``dates.version``;
- ``identifiers.arxiv`` is that ID and ``identifiers.doi`` arXiv's own DOI
  (``10.48550/arXiv.<id>``), which OpenAlex and others use for the same
  preprint, so the records merge;
- the journal DOI arXiv lists for a published version (``arxiv:doi``) is
  **not** the record's DOI: that would merge the preprint into its article.
  It becomes an ``is-preprint-of`` relation, which
  :mod:`carousel.ingestion.versions` turns into a version link, so the two
  stay separate papers that point at each other.

arXiv asks for at most one request every three seconds; the client keeps to
that. No key is needed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence

from carousel.connectors.base import (
    Budget,
    BudgetExceeded,
    Connector,
    ConnectorError,
    HttpClient,
    Request,
    SearchQuery,
    SearchResult,
    UrllibTransport,
)
from carousel.core.identifiers import try_normalize
from carousel.core.paper import Author, Relation, SourceRecord

API = "https://export.arxiv.org/api/query"
PAGE_MAX = 200
FETCH_BATCH = 50
DEFAULT_BUDGET_REQUESTS = 30
MIN_INTERVAL = 3.1  # arXiv API terms: no more than one request every three seconds
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom",
      "os": "http://a9.com/-/spec/opensearch/1.1/"}
_ID = re.compile(r"arxiv\.org/abs/(?P<id>.+?)(?:v(?P<v>\d+))?$")
_SPACE = re.compile(r"\s+")


def _text(el: Optional[ET.Element]) -> Optional[str]:
    if el is None or el.text is None:
        return None
    t = _SPACE.sub(" ", el.text).strip()
    return t or None


def _phrase(term: str) -> str:
    t = term.replace('"', " ").strip()
    return f'ti:"{t}" OR abs:"{t}"'


def build_query(query: SearchQuery) -> str:
    """``((ti:"a" OR abs:"a" OR ...) ANDNOT (...)) AND submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]``."""
    core = query.native or " OR ".join(_phrase(t) for t in query.terms)
    core = f"({core})"
    if query.exclude and not query.native:
        core = f"({core} ANDNOT ({' OR '.join(_phrase(t) for t in query.exclude)}))"
    return f"{core} AND submittedDate:[{query.start:%Y%m%d}0000 TO {query.end:%Y%m%d}2359]"


def parse_entry(entry: ET.Element) -> Optional[SourceRecord]:
    """One Atom ``<entry>`` -> SourceRecord (None for an error entry or one without an arXiv ID)."""
    m = _ID.search(_text(entry.find("a:id", NS)) or "")
    if not m:
        return None
    arxiv_id = try_normalize("arxiv", m.group("id"))
    if arxiv_id is None:
        return None
    version = f"v{m.group('v')}" if m.group("v") else None
    published = (_text(entry.find("a:published", NS)) or "")[:10] or None
    updated = (_text(entry.find("a:updated", NS)) or "")[:10] or None
    journal_doi = try_normalize("doi", _text(entry.find("arxiv:doi", NS)))
    primary = entry.find("arxiv:primary_category", NS)
    categories = [c.get("term") for c in entry.findall("a:category", NS) if c.get("term")]
    authors = [Author(name=n) for n in (_text(a.find("a:name", NS)) for a in entry.findall("a:author", NS)) if n]
    return SourceRecord(
        source="arxiv",
        source_id=arxiv_id,
        identifiers={"arxiv": arxiv_id, "doi": try_normalize("doi", f"10.48550/arXiv.{arxiv_id}")},
        title=_text(entry.find("a:title", NS)),
        abstract=_text(entry.find("a:summary", NS)),
        authors=authors,
        venue="arXiv" + (f" ({primary.get('term')})" if primary is not None and primary.get("term") else ""),
        publication_types=["preprint"],
        url=f"https://arxiv.org/abs/{arxiv_id}",
        version=version,
        dates={k: v for k, v in {"preprint_posted": published,
                                 "version": updated if version and version != "v1" else None}.items() if v},
        relations=[Relation(type="is-preprint-of", scheme="doi", value=journal_doi, asserted_by="arxiv")]
        if journal_doi else [],
        raw={"categories": categories, "journal_ref": _text(entry.find("arxiv:journal_ref", NS)),
             "comment": _text(entry.find("arxiv:comment", NS))},
    )


def _feed(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ConnectorError(f"arXiv returned something that is not an Atom feed: {exc}") from None
    for entry in root.findall("a:entry", NS):  # arXiv reports query errors as an entry
        if "api/errors" in (_text(entry.find("a:id", NS)) or ""):
            raise ConnectorError(f"arXiv rejected the query: {_text(entry.find('a:summary', NS))}")
    return root


class ArxivConnector(Connector):
    name = "arxiv"
    version = "0.1.0"
    cost_unit = "requests"

    def __init__(self, client: Optional[HttpClient] = None, budget_requests: Optional[float] = DEFAULT_BUDGET_REQUESTS,
                 page_size: int = PAGE_MAX) -> None:
        self.page_size = max(1, min(page_size, 2000))
        if client is None:
            client = HttpClient(UrllibTransport(timeout=60.0), min_interval=MIN_INTERVAL,
                                budget=Budget(budget_requests, unit="requests"))
        super().__init__(client)

    def search(self, query: SearchQuery) -> SearchResult:
        before = self._snapshot_stats()
        q = build_query(query)
        records: List[SourceRecord] = []
        total: Optional[int] = None
        pages = 0
        errors: List[str] = []
        reason: Optional[str] = None
        while len(records) < query.max_results and (total is None or pages * self.page_size < total):
            params = {"search_query": q, "start": pages * self.page_size,
                      "max_results": min(self.page_size, query.max_results - len(records)),
                      "sortBy": "submittedDate", "sortOrder": "descending"}
            try:
                root = _feed(self.client.get(Request.build(API, params)).text())
            except ConnectorError as exc:
                if pages == 0:
                    raise
                errors.append(str(exc))
                reason = "budget" if isinstance(exc, BudgetExceeded) else "error"
                break
            pages += 1
            found = root.find("os:totalResults", NS)
            if found is not None and found.text and found.text.strip().isdigit():
                total = int(found.text.strip())
            entries = root.findall("a:entry", NS)
            records.extend(r for r in (parse_entry(e) for e in entries) if r is not None)
            if not entries:
                break
        records = records[: query.max_results]
        truncated = reason is not None or (total is not None and len(records) < total)
        if truncated and reason is None:
            reason = "max_results"
        coverage = self._coverage(query, before, native_query=q, date_field="first submission date (submittedDate)",
                                  total_available=total, retrieved=len(records), truncated=truncated,
                                  truncation_reason=reason if truncated else None, pages=pages, errors=errors)
        return SearchResult(records=records, coverage=coverage)

    def fetch(self, ids: Sequence[str]) -> List[SourceRecord]:
        """Records by arXiv ID (any form: ``2610.01111``, ``arXiv:2610.01111v2``, an abs URL), 50 per request."""
        wanted = sorted({i for i in (try_normalize("arxiv", x) for x in ids) if i})  # sorted: one request per set
        out: List[SourceRecord] = []
        for i in range(0, len(wanted), FETCH_BATCH):
            batch = wanted[i : i + FETCH_BATCH]
            params: Dict[str, Any] = {"id_list": ",".join(batch), "max_results": len(batch)}
            root = _feed(self.client.get(Request.build(API, params)).text())
            out.extend(r for r in (parse_entry(e) for e in root.findall("a:entry", NS)) if r is not None)
        return out


__all__ = ["ArxivConnector", "build_query", "parse_entry"]
