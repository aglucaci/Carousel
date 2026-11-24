"""PubMed connector (NCBI E-utilities), ported from the ETHER-era daily script (retired in plan task 10).

``search`` runs ESearch (JSON) for the PMIDs in the date window, paging with
``retstart``, then EFetch (XML) in batches for the records. Changes from the
legacy script:

- returns :class:`~carousel.core.paper.SourceRecord` with all identifiers
  (PMID, PMCID, DOI), every author with ORCID, publication types, language,
  and the separate dates (journal issue date, electronic date, last revision);
- PubMed-indexed preprints put their date in ``preprint_posted``;
- retractions are an explicit flag (publication type or ``RetractionIn``);
- paging, an optional API key (``NCBI_API_KEY``), pacing within NCBI's limits
  (3 requests/s, or 10/s with a key), retries, and coverage reporting.
"""

from __future__ import annotations

import datetime as dt
import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence

from pydantic import ValidationError

from carousel.connectors.base import BudgetExceeded, Connector, ConnectorError, HttpClient, Request, SearchQuery, SearchResult
from carousel.core.identifiers import try_normalize
from carousel.core.paper import Author, SourceRecord

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
MAX_GET_URL = 2000
"""Longer requests (long native queries, big ID batches) are sent as POST."""

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _text(node: Optional[ET.Element]) -> str:
    """All nested text (keeps italicized taxa, sub/superscripts), whitespace-collapsed."""
    return "" if node is None else " ".join("".join(node.itertext()).split())


def _partial_date(year: Optional[str], month: Optional[str] = None, day: Optional[str] = None) -> Optional[str]:
    """PubMed date parts -> ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``; None if unusable."""
    if not year or not year.strip().isdigit() or len(year.strip()) != 4:
        return None
    out = year.strip()
    if not month:
        return out
    m = month.strip()
    mi = int(m) if m.isdigit() else _MONTHS.get(m[:3].lower())
    if not mi or not 1 <= mi <= 12:
        return out
    out += f"-{mi:02d}"
    if day and day.strip().isdigit():
        try:
            dt.date(int(out[:4]), mi, int(day.strip()))
        except ValueError:
            return out
        return f"{out}-{int(day.strip()):02d}"
    return out


def _date_of(node: Optional[ET.Element]) -> Optional[str]:
    if node is None:
        return None
    return _partial_date(node.findtext("Year"), node.findtext("Month"), node.findtext("Day"))


def _pub_date(journal_issue: Optional[ET.Element]) -> Optional[str]:
    if journal_issue is None:
        return None
    pd = journal_issue.find("PubDate")
    if pd is None:
        return None
    if pd.findtext("Year"):
        return _date_of(pd)
    medline = (pd.findtext("MedlineDate") or "").strip()  # e.g. "2024 Jan-Feb", "2023 Winter"
    parts = medline.split()
    if parts and parts[0].isdigit() and len(parts[0]) == 4:
        return parts[0]
    return None


def parse_article(article: ET.Element) -> Optional[SourceRecord]:
    """One ``<PubmedArticle>`` -> SourceRecord (None if it has no PMID)."""
    mc = article.find("MedlineCitation")
    if mc is None:
        return None
    pmid = (mc.findtext("PMID") or "").strip()
    if not pmid:
        return None
    art = mc.find("Article")
    if art is None:
        return None

    ids: Dict[str, Optional[str]] = {"pmid": try_normalize("pmid", pmid)}
    for aid in article.findall("PubmedData/ArticleIdList/ArticleId"):
        kind = aid.get("IdType")
        if kind == "doi" and not ids.get("doi"):
            ids["doi"] = try_normalize("doi", aid.text)
        elif kind == "pmc" and not ids.get("pmcid"):
            ids["pmcid"] = try_normalize("pmcid", aid.text)
    if not ids.get("doi"):
        for eloc in art.findall("ELocationID"):
            if eloc.get("EIdType") == "doi":
                ids["doi"] = try_normalize("doi", eloc.text)
                break

    abstract_parts = []
    for node in art.findall("Abstract/AbstractText"):
        txt = _text(node)
        if txt:
            label = node.get("Label")
            abstract_parts.append(f"{label}: {txt}" if label else txt)

    authors: List[Author] = []
    for a in art.findall("AuthorList/Author"):
        if a.get("ValidYN") == "N":
            continue
        name = " ".join(p for p in ((a.findtext("ForeName") or "").strip(), (a.findtext("LastName") or "").strip()) if p)
        name = name or _text(a.find("CollectiveName"))
        if not name:
            continue
        orcid = next((i.text for i in a.findall("Identifier") if i.get("Source") == "ORCID" and i.text), None)
        try:
            authors.append(Author(name=name, orcid=orcid))
        except ValidationError:
            authors.append(Author(name=name))

    pub_types = [t.text.strip() for t in art.findall("PublicationTypeList/PublicationType") if t.text]
    retracted = "Retracted Publication" in pub_types or any(
        c.get("RefType") == "RetractionIn" for c in mc.findall("CommentsCorrectionsList/CommentsCorrections")
    )

    issue_date = _pub_date(art.find("Journal/JournalIssue"))
    electronic = next((_date_of(d) for d in art.findall("ArticleDate") if d.get("DateType") == "Electronic"), None)
    dates: Dict[str, Optional[str]] = {
        "first_online": electronic,
        "source_updated": _date_of(mc.find("DateRevised")),
    }
    if "Preprint" in pub_types:
        dates["preprint_posted"] = electronic or issue_date
    else:
        dates["published"] = issue_date

    history = {d.get("PubStatus"): _date_of(d) for d in article.findall("PubmedData/History/PubMedPubDate")}
    raw = {k: v for k, v in {
        "entrez_date": history.get("entrez") or history.get("pubmed"),
        "medline_status": mc.get("Status"),
        "publication_status": article.findtext("PubmedData/PublicationStatus"),
    }.items() if v}

    return SourceRecord(
        source="pubmed",
        source_id=ids["pmid"] or pmid,
        identifiers=ids,
        title=_text(art.find("ArticleTitle")) or _text(art.find("VernacularTitle")) or None,
        abstract=" ".join(abstract_parts) or None,
        authors=authors,
        venue=(art.findtext("Journal/Title") or "").strip() or None,
        publication_types=pub_types,
        language=(art.findtext("Language") or "").strip() or None,
        is_retracted=retracted,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        dates=dates,
        raw=raw or None,
    )


def parse_efetch(xml_bytes: bytes) -> List[SourceRecord]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ConnectorError(f"PubMed EFetch returned unparseable XML: {exc}") from exc
    return [r for r in (parse_article(a) for a in root.iter("PubmedArticle")) if r is not None]


def build_term(query: SearchQuery) -> str:
    """Radar terms -> a PubMed query.

    Terms are left unquoted so PubMed's automatic term mapping (MeSH) still
    applies; exclusions are conservative exact phrases in title/abstract.
    """
    require = " AND (" + " OR ".join(f"({t})" for t in query.require) + ")" if query.require else ""
    if query.native:
        return f"({query.native}){require}" if require else query.native
    term = " OR ".join(f"({t})" for t in query.terms)
    if len(query.terms) > 1:
        term = f"({term})"
    term += require
    if query.exclude:
        term += " NOT (" + " OR ".join(f'"{t}"[Title/Abstract]' for t in query.exclude) + ")"
    return term


class PubMedConnector(Connector):
    name = "pubmed"
    version = "0.1.0"
    cost_unit = "requests"

    def __init__(
        self,
        client: Optional[HttpClient] = None,
        api_key: Optional[str] = None,
        date_field: str = "pdat",
        page_size: int = 500,
        batch_size: int = 200,
        tool: str = "carousel",
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.environ.get("NCBI_API_KEY")) or None
        super().__init__(client or HttpClient(min_interval=0.11 if self.api_key else 0.34))
        if date_field not in ("pdat", "edat", "mdat"):
            raise ValueError("date_field must be pdat (publication), edat (added to PubMed), or mdat (modified)")
        self.date_field = date_field
        self.page_size = page_size
        self.batch_size = batch_size
        self.tool = tool

    def _request(self, endpoint: str, params: Dict[str, Any]) -> Request:
        req = Request.build(EUTILS + endpoint, {**params, "tool": self.tool}, secret_params={"api_key": self.api_key})
        if len(req.full_url()) > MAX_GET_URL:  # NCBI answers 414 for long URLs; E-utilities accept POST
            req = Request.build(EUTILS + endpoint, {**params, "tool": self.tool},
                                secret_params={"api_key": self.api_key}, method="POST")
        return req

    def count(self, query: SearchQuery) -> "CountResult":
        before = self._snapshot_stats()
        term = build_term(query)
        params = {"db": "pubmed", "term": term, "retmode": "json", "rettype": "count", "datetype": self.date_field,
                  "mindate": query.start.strftime("%Y/%m/%d"), "maxdate": query.end.strftime("%Y/%m/%d")}
        data = self.client.get(self._request("esearch.fcgi", params)).json().get("esearchresult") or {}
        return self._count_result(query, before, term, f"{self.date_field} (PubMed date)", data.get("count"))

    def search(self, query: SearchQuery) -> SearchResult:
        before = self._snapshot_stats()
        term = build_term(query)
        pmids: List[str] = []
        total: Optional[int] = None
        pages = 0
        errors: List[str] = []
        reason: Optional[str] = None
        while len(pmids) < query.max_results:
            params = {
                "db": "pubmed",
                "term": term,
                "retmode": "json",
                "sort": "pub_date",
                "datetype": self.date_field,
                "mindate": query.start.strftime("%Y/%m/%d"),
                "maxdate": query.end.strftime("%Y/%m/%d"),
                "retstart": len(pmids),
                "retmax": min(self.page_size, query.max_results - len(pmids)),
            }
            try:
                data = self.client.get(self._request("esearch.fcgi", params)).json()["esearchresult"]
            except ConnectorError as exc:
                if pages == 0:
                    raise
                errors.append(str(exc))
                reason = "budget" if isinstance(exc, BudgetExceeded) else "error"
                break
            if "ERROR" in data:
                raise ConnectorError(f"PubMed ESearch error: {data['ERROR']}")
            pages += 1
            total = int(data.get("count", 0))
            page = data.get("idlist", [])
            pmids.extend(page)
            if not page or len(pmids) >= total:
                break

        records: List[SourceRecord] = []
        for i in range(0, len(pmids), self.batch_size):
            try:
                records.extend(self.fetch(pmids[i : i + self.batch_size]))
            except ConnectorError as exc:
                errors.append(str(exc))
                reason = reason or ("budget" if isinstance(exc, BudgetExceeded) else "error")
                break

        truncated = reason is not None or (total is not None and len(records) < total)
        if truncated and reason is None:
            reason = "max_results" if len(pmids) < (total or 0) else "error"
        coverage = self._coverage(
            query, before,
            native_query=term,
            date_field={"pdat": "publication date", "edat": "Entrez date (added to PubMed)", "mdat": "modification date"}[self.date_field],
            total_available=total,
            retrieved=len(records),
            truncated=truncated,
            truncation_reason=reason if truncated else None,
            pages=pages,
            errors=errors,
        )
        return SearchResult(records=records, coverage=coverage)

    def fetch(self, ids: Sequence[str]) -> List[SourceRecord]:
        pmids = [p for p in (try_normalize("pmid", i) for i in ids) if p]
        out: List[SourceRecord] = []
        for i in range(0, len(pmids), self.batch_size):
            batch = pmids[i : i + self.batch_size]
            resp = self.client.get(self._request("efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}))
            out.extend(parse_efetch(resp.body))
        return out
