"""OpenAlex connector (https://api.openalex.org).

OpenAlex bills by usage, in credits (1 credit = US$0.0001; read 2026-11-23 from
help.openalex.org/access/example-costs):

=========================  ==========  =====================================
request                    credits     used for
=========================  ==========  =====================================
search (``search*``)       10          :meth:`OpenAlexConnector.search`
list + filter              1           :meth:`OpenAlexConnector.fetch`
=========================  ==========  =====================================

The free daily allowance is US$1 with an API key (US$0.10 without), so every
run has a credit :class:`~carousel.connectors.base.Budget` (default 500 credits,
i.e. 50 search pages, US$0.05). The connector charges what the
``X-RateLimit-Credits-Used`` header reports when present, and records the
other ``X-RateLimit-*`` headers in the query's coverage.

Rules this connector keeps:

- Searches cover scholarly work types only (:data:`DEFAULT_WORK_TYPES`)
  unless ``work_types`` says otherwise.
- Queries are always bounded by a publication-date window and ``max_results``
  (:class:`~carousel.connectors.base.SearchQuery`); it never pages through
  the whole corpus.
- Cursor paging past the first page needs an API key (``OPENALEX_API_KEY``).
  Without one it returns the first page and marks the coverage truncated
  (``no_api_key``) rather than failing.
- The key travels in an ``Authorization`` header, so it never appears in a
  URL, cache key, fixture, or coverage record.
"""

from __future__ import annotations

import os
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
from carousel.core.paper import Author, SourceRecord

API = "https://api.openalex.org/works"
SEARCH_CREDITS = 10
LIST_CREDITS = 1
DEFAULT_BUDGET_CREDITS = 500
PER_PAGE_MAX = 100

DEFAULT_WORK_TYPES = ("article", "preprint", "review", "letter")
"""Scholarly work types searched by default. OpenAlex also indexes datasets, software, peer reviews,
supplementary material, and paratext, which otherwise crowd out papers."""

SELECT = ",".join((
    "id", "doi", "ids", "display_name", "publication_date", "type", "language", "is_retracted",
    "abstract_inverted_index", "authorships", "primary_location", "locations", "updated_date",
))


def reconstruct_abstract(inverted: Optional[Dict[str, List[int]]]) -> Optional[str]:
    """OpenAlex ships abstracts as {word: [positions]}; rebuild the text."""
    if not inverted:
        return None
    positions = {pos: word for word, poss in inverted.items() for pos in poss}
    return " ".join(positions[i] for i in sorted(positions)) or None


def _pmcid(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    return try_normalize("pmcid", f"PMC{tail}" if tail.isdigit() else tail)


def _arxiv(work: Dict[str, Any]) -> Optional[str]:
    doi = (work.get("doi") or "").lower()
    if "10.48550/arxiv." in doi:
        return try_normalize("arxiv", doi.split("10.48550/arxiv.", 1)[1])
    for loc in work.get("locations") or []:
        url = (loc or {}).get("landing_page_url") or ""
        if "arxiv.org/abs/" in url:
            return try_normalize("arxiv", url)
    return None


def parse_work(work: Dict[str, Any]) -> Optional[SourceRecord]:
    """One OpenAlex work JSON -> SourceRecord (None if it has no usable ID)."""
    wid = try_normalize("openalex", work.get("id"))
    if wid is None:
        return None
    ids = work.get("ids") or {}
    authors: List[Author] = []
    for a in work.get("authorships") or []:
        author = (a or {}).get("author") or {}
        name = (author.get("display_name") or a.get("raw_author_name") or "").strip()
        if not name:
            continue
        try:
            authors.append(Author(name=name, orcid=author.get("orcid")))
        except ValidationError:
            authors.append(Author(name=name))

    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    wtype = work.get("type")
    pub_date = work.get("publication_date")
    updated = (work.get("updated_date") or "")[:10] or None
    dates = {"source_updated": updated}
    dates["preprint_posted" if wtype == "preprint" else "published"] = pub_date

    return SourceRecord(
        source="openalex",
        source_id=wid,
        identifiers={
            "doi": try_normalize("doi", work.get("doi") or ids.get("doi")),
            "pmid": try_normalize("pmid", ids.get("pmid")),
            "pmcid": _pmcid(ids.get("pmcid")),
            "openalex": wid,
            "arxiv": _arxiv(work),
        },
        title=(work.get("display_name") or "").strip() or None,
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        authors=authors,
        venue=(source.get("display_name") or "").strip() or None,
        publication_types=[wtype] if wtype else [],
        language=work.get("language"),
        is_retracted=work.get("is_retracted"),
        url=primary.get("landing_page_url") or work.get("doi"),
        dates=dates,
    )


def _phrase(term: str) -> str:
    term = term.replace('"', "")
    return f'"{term}"' if " " in term else term


def build_search(query: SearchQuery) -> str:
    """Radar terms -> OpenAlex boolean search: ("a b" OR c) NOT ("d e")."""
    require = " AND (" + " OR ".join(_phrase(t) for t in query.require) + ")" if query.require else ""
    if query.native:
        return f"({query.native}){require}" if require else query.native
    s = "(" + " OR ".join(_phrase(t) for t in query.terms) + ")" + require
    if query.exclude:
        s += " NOT (" + " OR ".join(_phrase(t) for t in query.exclude) + ")"
    return s


class OpenAlexConnector(Connector):
    name = "openalex"
    version = "0.1.0"
    cost_unit = "credits"

    def __init__(self, client: Optional[HttpClient] = None, api_key: Optional[str] = None,
                 budget_credits: Optional[float] = DEFAULT_BUDGET_CREDITS,
                 work_types: Optional[Sequence[str]] = DEFAULT_WORK_TYPES) -> None:
        self.work_types = tuple(work_types) if work_types else ()
        self.api_key = (api_key if api_key is not None else os.environ.get("OPENALEX_API_KEY")) or None
        if client is None:
            # Boolean searches with >5 operators are limited to 2/s with a key, 1/s without.
            client = HttpClient(min_interval=0.55 if self.api_key else 1.05,
                                budget=Budget(budget_credits, unit="credits"))
        client.cost_header = client.cost_header or "x-ratelimit-credits-used"
        super().__init__(client)

    def _request(self, params: Dict[str, Any]) -> Request:
        auth = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        return Request.build(API, params, secret_headers=auth)

    def _filter(self, query: SearchQuery) -> str:
        f = f"from_publication_date:{query.start.isoformat()},to_publication_date:{query.end.isoformat()}"
        return f + (f",type:{'|'.join(self.work_types)}" if self.work_types else "")

    def count(self, query: SearchQuery) -> "CountResult":
        before = self._snapshot_stats()
        search = build_search(query)
        params = {"search.title_and_abstract": search, "filter": self._filter(query), "select": "id", "per_page": 1}
        data = self.client.get(self._request(params), cost=SEARCH_CREDITS).json()
        return self._count_result(query, before, f"search.title_and_abstract={search} filter={self._filter(query)}",
                                  "publication date", (data.get("meta") or {}).get("count"))

    def search(self, query: SearchQuery) -> SearchResult:
        before = self._snapshot_stats()
        search = build_search(query)
        records: List[SourceRecord] = []
        total: Optional[int] = None
        cursor: Optional[str] = "*"
        pages = 0
        errors: List[str] = []
        reason: Optional[str] = None
        while cursor and len(records) < query.max_results:
            if pages >= 1 and not self.api_key:
                reason = "no_api_key"
                break
            params = {
                "search.title_and_abstract": search,
                "filter": self._filter(query),
                "select": SELECT,
                "per_page": min(PER_PAGE_MAX, query.max_results - len(records)),
                "cursor": cursor,
            }
            try:
                data = self.client.get(self._request(params), cost=SEARCH_CREDITS).json()
            except ConnectorError as exc:
                if pages == 0:
                    raise
                errors.append(str(exc))
                reason = "budget" if isinstance(exc, BudgetExceeded) else "error"
                break
            pages += 1
            meta = data.get("meta") or {}
            total = meta.get("count", total)
            results = data.get("results") or []
            records.extend(r for r in (parse_work(w) for w in results) if r is not None)
            cursor = meta.get("next_cursor")
            if not results:
                break

        records = records[: query.max_results]
        truncated = reason is not None or (total is not None and len(records) < total)
        if truncated and reason is None:
            reason = "max_results"
        coverage = self._coverage(
            query, before,
            native_query=f"search.title_and_abstract={search} filter={self._filter(query)}",
            date_field="publication date",
            total_available=total,
            retrieved=len(records),
            truncated=truncated,
            truncation_reason=reason if truncated else None,
            pages=pages,
            errors=errors,
        )
        return SearchResult(records=records, coverage=coverage)

    def fetch(self, ids: Sequence[str]) -> List[SourceRecord]:
        wids = [w for w in (try_normalize("openalex", i) for i in ids) if w]
        out: List[SourceRecord] = []
        for i in range(0, len(wids), PER_PAGE_MAX):
            batch = wids[i : i + PER_PAGE_MAX]
            params = {"filter": "ids.openalex:" + "|".join(batch), "select": SELECT, "per_page": len(batch)}
            data = self.client.get(self._request(params), cost=LIST_CREDITS).json()
            out.extend(r for r in (parse_work(w) for w in data.get("results") or []) if r is not None)
        return out
