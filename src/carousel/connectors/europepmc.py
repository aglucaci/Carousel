"""Europe PMC connector (https://europepmc.org/RestfulWebService): search, licences, full text, annotations.

**Search** covers MEDLINE (``MED``), PubMed Central (``PMC``), and preprints
(``PPR``: bioRxiv, medRxiv, Research Square, ...), in titles and abstracts
only (``TITLE_ABS``, so a passing mention deep in a full text is not a hit),
bounded by first-publication date. Each record carries the article's licence
and open-access status as Europe PMC states them, and corrections and
retractions from its ``commentCorrectionList`` ("Retraction in", "Erratum in",
...) as update notices.

**Full text** (:meth:`EuropePMCConnector.full_text`) is fetched as JATS XML
for open-access PMC articles, and kept only when the document's own
``<license>`` (or, failing that, Europe PMC's metadata) is a Creative Commons
licence or a public-domain dedication. The licence travels with the text
(:class:`~carousel.core.fulltext.PaperText`).

**Annotations** (:meth:`EuropePMCConnector.annotations`) are Europe PMC's
text-mined entities, summarized per paper.

Europe PMC needs no key. Requests are paced well inside its fair-use limits.
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
    UrllibTransport,
)
from carousel.core.fulltext import Annotation, PaperText, Section, is_reusable, normalize_license, reuse_of
from carousel.core.identifiers import try_normalize
from carousel.core.paper import Author, SourceRecord, UpdateNotice

REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ANNOTATIONS = "https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds"
PAGE_MAX = 100
ANNOTATION_BATCH = 8  # the annotations API takes 1-8 article IDs per request
DEFAULT_BUDGET_REQUESTS = 120
SOURCES = ("MED", "PMC", "PPR")

# commentCorrectionList entries on the *updated* paper ("... in" = published in that notice).
UPDATE_TYPES = {
    "retraction in": "retraction",
    "retraction and republication in": "retraction",
    "partial retraction in": "partial_retraction",
    "erratum in": "erratum",
    "expression of concern in": "expression_of_concern",
    "republished in": "new_version",
    "update in": "update",
}
RETRACTING = {"retraction"}

_TAG = re.compile(r"<[^>]+>")
_DOI_IN_REF = re.compile(r"doi:\s*(10\.\S+)", re.I)
XLINK = "{http://www.w3.org/1999/xlink}href"


def _clean(value: Optional[str]) -> Optional[str]:
    """Strip the inline HTML Europe PMC keeps in titles and abstracts (<i>, <sup>, <h4>...)."""
    if not value:
        return None
    return " ".join(_TAG.sub(" ", value).split()) or None


def _date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _journal_date(info: Dict[str, Any]) -> Optional[str]:
    year = info.get("yearOfPublication")
    if not year:
        return None
    month = info.get("monthOfPublication")
    return f"{int(year):04d}-{int(month):02d}" if month else f"{int(year):04d}"


def _authors(result: Dict[str, Any]) -> List[Author]:
    out: List[Author] = []
    for a in (result.get("authorList") or {}).get("author") or []:
        name = " ".join(x for x in (a.get("firstName"), a.get("lastName")) if x) or a.get("fullName") or a.get("collectiveName")
        if not name or not name.strip():
            continue
        orcid = None
        aid = a.get("authorId") or {}
        if (aid.get("type") or "").upper() == "ORCID":
            orcid = aid.get("value")
        try:
            out.append(Author(name=name.strip(), orcid=orcid))
        except ValidationError:
            out.append(Author(name=name.strip()))
    return out


def _updates(result: Dict[str, Any]) -> List[UpdateNotice]:
    out = []
    for c in (result.get("commentCorrectionList") or {}).get("commentCorrection") or []:
        kind = UPDATE_TYPES.get((c.get("type") or "").strip().lower())
        if not kind:
            continue
        doi = _DOI_IN_REF.search(c.get("reference") or "")
        out.append(UpdateNotice(type=kind, label=c.get("type"), asserted_by="publisher",
                                notice_doi=try_normalize("doi", doi.group(1).rstrip(".")) if doi else None))
    return out


def parse_result(r: Dict[str, Any]) -> Optional[SourceRecord]:
    """One Europe PMC ``resultType=core`` result -> SourceRecord (None without an ID)."""
    src, rid = r.get("source"), r.get("id")
    if not src or not rid:
        return None
    types = list((r.get("pubTypeList") or {}).get("pubType") or [])
    is_preprint = src == "PPR" or any(t.lower() == "preprint" for t in types)
    updates = _updates(r)
    journal = r.get("journalInfo") or {}
    venue = ((journal.get("journal") or {}).get("title")
             or ((r.get("bookOrReportDetails") or {}).get("publisher") if is_preprint else None))
    first = _date(r.get("firstPublicationDate"))
    dates: Dict[str, Optional[str]] = {"source_updated": _date(r.get("dateOfRevision"))}
    if is_preprint:
        dates["preprint_posted"] = first
    else:
        dates["published"] = _journal_date(journal) or first
        dates["first_online"] = _date(r.get("electronicPublicationDate"))
    oa = r.get("isOpenAccess")
    return SourceRecord(
        source="europepmc",
        source_id=f"{src}:{rid}",
        identifiers={
            "pmid": try_normalize("pmid", r.get("pmid")),
            "pmcid": try_normalize("pmcid", r.get("pmcid")),
            "doi": try_normalize("doi", r.get("doi")),
        },
        title=_clean(r.get("title")),
        abstract=_clean(r.get("abstractText")),
        authors=_authors(r),
        venue=(venue or "").strip() or None,
        publication_types=types,
        language=r.get("language"),
        is_retracted=any(u.type in RETRACTING for u in updates) or any(t.lower() == "retracted publication" for t in types),
        url=f"https://europepmc.org/article/{src}/{rid}",
        dates={k: v for k, v in dates.items() if v},
        updates=updates,
        license=normalize_license(r.get("license")),
        open_access=None if oa is None else oa == "Y",
    )


def _phrase(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def build_query(query: SearchQuery) -> str:
    """Radar terms -> Europe PMC syntax over titles and abstracts, in the date window, from MED/PMC/PPR."""
    if query.native:
        core = f"({query.native})"
    else:
        core = "(" + " OR ".join(f"TITLE_ABS:{_phrase(t)}" for t in query.terms) + ")"
        if query.exclude:
            core += " NOT (" + " OR ".join(f"TITLE_ABS:{_phrase(t)}" for t in query.exclude) + ")"
    window = f"FIRST_PDATE:[{query.start.isoformat()} TO {query.end.isoformat()}]"
    sources = " OR ".join(f"SRC:{s}" for s in SOURCES)
    return f"({core}) AND ({window}) AND ({sources})"


# -------------------------
# Full text (JATS)
# -------------------------

def jats_license(root: ET.Element) -> Tuple[Optional[str], Optional[str]]:
    """(normalized licence, licence URL) from a JATS ``<permissions><license>``; (None, None) if none."""
    for lic in root.iter():
        if not lic.tag.endswith("license") or lic.tag.endswith("license_ref") or lic.tag.endswith("license-p"):
            continue
        url = lic.get(XLINK)
        for child in lic.iter():
            if child.tag.endswith("license_ref") and (child.text or "").strip():
                url = url or child.text.strip()
        for candidate in (url, lic.get("license-type"), " ".join("".join(lic.itertext()).split())):
            norm = normalize_license(candidate)
            if norm and (norm.startswith("cc") or norm == "public-domain"):
                return norm, url
        return normalize_license(url or " ".join("".join(lic.itertext()).split())), url
    return None, None


def _para_text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split())


def jats_sections(root: ET.Element) -> List[Section]:
    """Body sections as (title, paragraphs) - tables, figures, and references left out."""
    body = root.find(".//body")
    if body is None:
        return []
    out: List[Section] = []

    def walk(sec: ET.Element, prefix: str) -> None:
        title = " ".join("".join(sec.findtext("title") or "").split())
        full = f"{prefix} / {title}" if prefix and title else (title or prefix)
        paras = [_para_text(p) for p in sec.findall("p")]
        paras = [p for p in paras if p]
        if paras:
            out.append(Section(title=full, text="\n".join(paras)))
        for child in sec.findall("sec"):
            walk(child, full)

    loose = [_para_text(p) for p in body.findall("p")]
    if any(loose):
        out.append(Section(title="", text="\n".join(p for p in loose if p)))
    for sec in body.findall("sec"):
        walk(sec, "")
    return out


class EuropePMCConnector(Connector):
    name = "europepmc"
    version = "0.1.0"
    cost_unit = "requests"

    def __init__(self, client: Optional[HttpClient] = None, budget_requests: Optional[float] = DEFAULT_BUDGET_REQUESTS,
                 page_size: int = PAGE_MAX) -> None:
        self.page_size = max(1, min(page_size, 1000))
        if client is None:
            # Full-text XML can take tens of seconds to arrive, so allow longer than the default 30 s.
            client = HttpClient(UrllibTransport(timeout=90.0), min_interval=0.15,
                                budget=Budget(budget_requests, unit="requests"))
        super().__init__(client)

    # ---- search ----

    def search(self, query: SearchQuery) -> SearchResult:
        before = self._snapshot_stats()
        q = build_query(query)
        records: List[SourceRecord] = []
        total: Optional[int] = None
        cursor: Optional[str] = "*"
        pages = 0
        errors: List[str] = []
        reason: Optional[str] = None
        while cursor and len(records) < query.max_results:
            params = {"query": q, "resultType": "core", "format": "json", "cursorMark": cursor,
                      "pageSize": min(self.page_size, query.max_results - len(records))}
            try:
                data = self.client.get(Request.build(f"{REST}/search", params)).json()
            except ConnectorError as exc:
                if pages == 0:
                    raise
                errors.append(str(exc))
                reason = "budget" if isinstance(exc, BudgetExceeded) else "error"
                break
            pages += 1
            total = data.get("hitCount", total)
            results = (data.get("resultList") or {}).get("result") or []
            records.extend(r for r in (parse_result(x) for x in results) if r is not None)
            nxt = data.get("nextCursorMark")
            cursor = nxt if nxt and nxt != cursor and results else None
        records = records[: query.max_results]
        truncated = reason is not None or (total is not None and len(records) < total)
        if truncated and reason is None:
            reason = "max_results"
        coverage = self._coverage(
            query, before, native_query=q, date_field="first publication date (FIRST_PDATE)", total_available=total,
            retrieved=len(records), truncated=truncated, truncation_reason=reason if truncated else None,
            pages=pages, errors=errors,
        )
        return SearchResult(records=records, coverage=coverage)

    def fetch(self, ids: Sequence[str]) -> List[SourceRecord]:
        """Records by Europe PMC ID (``MED:123``, ``PPR:PPR456``, ``PMC:PMC789``)."""
        out: List[SourceRecord] = []
        for ref in ids:
            src, _, rid = ref.partition(":")
            if not src or not rid:
                continue
            params = {"query": f"EXT_ID:{rid} AND SRC:{src}", "resultType": "core", "format": "json", "pageSize": 1}
            data = self.client.get(Request.build(f"{REST}/search", params)).json()
            out.extend(r for r in (parse_result(x) for x in (data.get("resultList") or {}).get("result") or []) if r)
        return out

    # ---- full text ----

    def full_text(self, pmcid: str, metadata_license: Optional[str] = None) -> Optional[PaperText]:
        """The JATS full text of an open-access PMC article, if its licence lets CAROUSEL keep it; else None.

        The text's own licence wins; ``metadata_license`` (Europe PMC's, from search) is the fallback.
        Raises :class:`ConnectorError` on HTTP errors other than 404 (not in the open-access subset).
        """
        pmc = try_normalize("pmcid", pmcid)
        if pmc is None:
            return None
        try:
            resp = self.client.get(Request.build(f"{REST}/{pmc}/fullTextXML"))
        except ConnectorError as exc:
            if exc.status == 404:
                return None
            raise
        try:
            root = ET.fromstring(resp.body)
        except ET.ParseError as exc:
            raise ConnectorError(f"unreadable full text for {pmc}: {exc}") from exc
        lic, url = jats_license(root)
        where = "text"
        if lic is None and metadata_license:
            lic, url, where = normalize_license(metadata_license), None, "metadata"
        if not is_reusable(lic):
            return None
        sections = jats_sections(root)
        if not sections:
            return None
        return PaperText(source="europepmc", source_id=pmc, kind="full_text", format="jats", license=lic,
                         license_url=url, license_source=where, reuse=reuse_of(lic),
                         url=f"https://europepmc.org/article/PMC/{pmc}", sections=sections,
                         retrieved_at=dt.datetime.now(dt.timezone.utc))

    # ---- annotations ----

    def annotations(self, article_ids: Iterable[str]) -> Dict[str, List[Annotation]]:
        """Text-mined entities per article (``MED:123`` / ``PMC:PMC789`` / ``PPR:...``), one row per entity."""
        ids = sorted(set(article_ids))
        out: Dict[str, List[Annotation]] = {}
        for i in range(0, len(ids), ANNOTATION_BATCH):
            batch = [_annotation_id(x) for x in ids[i : i + ANNOTATION_BATCH]]
            params = {"articleIds": ",".join(batch), "format": "JSON"}
            data = self.client.get(Request.build(ANNOTATIONS, params)).json()
            for art in data or []:
                rows = summarize_annotations(art.get("annotations") or [])
                for key in _article_keys(art):  # one article can answer for its MED and PMC IDs
                    out[key] = rows
        return out


def _annotation_id(ref: str) -> str:
    """``PMC:PMC123`` -> ``PMC:123`` (the annotations API wants PMC IDs without the prefix)."""
    src, _, rid = ref.partition(":")
    if src == "PMC" and rid.upper().startswith("PMC"):
        rid = rid[3:]
    return f"{src}:{rid}"


def _article_keys(art: Dict[str, Any]) -> List[str]:
    """The IDs an annotations-API article answers for: its own, plus ``PMC:PMC...`` when it has a PMCID."""
    src, ext = art.get("source"), art.get("extId")
    keys = []
    if src and ext:
        if src == "PMC" and not str(ext).upper().startswith("PMC"):
            ext = f"PMC{ext}"
        keys.append(f"{src}:{ext}")
    pmc = try_normalize("pmcid", art.get("pmcid"))
    if pmc and f"PMC:{pmc}" not in keys:
        keys.append(f"PMC:{pmc}")
    return keys


def summarize_annotations(raw: Sequence[Dict[str, Any]]) -> List[Annotation]:
    """Collapse mentions into one row per (type, entity), most-mentioned first."""
    rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for a in raw:
        tags = a.get("tags") or [{}]
        tag = tags[0] or {}
        name = (tag.get("name") or a.get("exact") or "").strip()
        atype = (a.get("type") or "").strip()
        if not name or not atype:
            continue
        uri = tag.get("uri")
        key = (atype, (uri or "").lower(), name.lower())
        row = rows.setdefault(key, {"type": atype, "name": name, "uri": uri, "mentions": 0, "sections": [],
                                    "provider": a.get("provider")})
        row["mentions"] += 1
        section = (a.get("section") or "").split(" (", 1)[0].strip()
        if section and section not in row["sections"]:
            row["sections"].append(section)
    return sorted((Annotation(**r) for r in rows.values()), key=lambda x: (-x.mentions, x.type, x.name.lower()))
