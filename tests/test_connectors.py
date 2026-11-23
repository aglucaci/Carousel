import datetime as dt
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import ValidationError

from carousel.connectors import (
    Budget,
    BudgetExceeded,
    ConnectorError,
    HttpClient,
    OpenAlexConnector,
    PubMedConnector,
    RecordingTransport,
    ReplayTransport,
    Request,
    Response,
    ResponseCache,
    SearchQuery,
)
from carousel.connectors.base import Transport
from carousel.connectors.openalex import build_search, parse_work, reconstruct_abstract
from carousel.connectors.pubmed import build_term, parse_article
from carousel.core.config import load_radar
from carousel.core.paper import SourceRecord
from carousel.database import PaperStore

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "http"

# Must match tests/fixtures/http/record_fixtures.py
QUERY = SearchQuery(
    terms=["immune escape", "antigenic drift"],
    exclude=["case report"],
    start=dt.date(2026, 10, 1),
    end=dt.date(2026, 10, 14),
    max_results=5,
)


def replay(name: str) -> HttpClient:
    return HttpClient(ReplayTransport(FIXTURES / name), sleep=lambda s: None)


class FakeTransport(Transport):
    """Serves scripted responses in order and keeps every request it was sent."""

    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.sent = []

    def send(self, request: Request) -> Response:
        self.sent.append(request)
        return self.responses.pop(0)


def oa_page(works, next_cursor=None, count=None, credits="10") -> Response:
    body = {"meta": {"count": count if count is not None else len(works), "next_cursor": next_cursor}, "results": works}
    return Response(200, json.dumps(body).encode(), {"x-ratelimit-credits-used": credits, "x-ratelimit-remaining": "9990"})


def work(n: int, **kw) -> dict:
    w = {"id": f"https://openalex.org/W{n}", "display_name": f"Work {n}", "type": "article", "publication_date": "2026-10-05"}
    w.update(kw)
    return w


# -------------------------
# Recorded fixtures
# -------------------------

class PubMedFixtureTests(unittest.TestCase):
    def test_search_records_and_coverage(self) -> None:
        conn = PubMedConnector(replay("pubmed_search.json"), api_key="", page_size=3, batch_size=5)
        res = conn.search(QUERY)
        cov = res.coverage
        self.assertEqual(cov.native_query, '((immune escape) OR (antigenic drift)) NOT ("case report"[Title/Abstract])')
        self.assertEqual((cov.total_available, cov.retrieved, cov.pages, cov.requests), (221, 5, 2, 3))
        self.assertTrue(cov.truncated)
        self.assertEqual(cov.truncation_reason, "max_results")
        self.assertEqual(cov.date_field, "publication date")
        self.assertEqual(cov.cost_unit, "requests")

        first = res.records[0]
        self.assertEqual(first.source_id, "42898639")
        self.assertEqual(first.identifiers.doi, "10.1016/j.bioactmat.2026.10.003")
        self.assertEqual(first.identifiers.pmcid, "PMC13677969")
        self.assertEqual(first.dates.published, "2027-03")  # journal issue date
        self.assertEqual(first.dates.first_online, "2026-10-11")  # electronic date
        self.assertEqual(first.raw["entrez_date"], "2026-10-20")
        self.assertIs(first.is_retracted, False)
        self.assertTrue(all(r.title and r.abstract and r.authors for r in res.records))
        for r in res.records:
            self.assertEqual(SourceRecord.model_validate_json(r.model_dump_json()), r)


class OpenAlexFixtureTests(unittest.TestCase):
    def test_search_records_coverage_and_credits(self) -> None:
        conn = OpenAlexConnector(replay("openalex_search.json"), api_key="")
        res = conn.search(QUERY)
        cov = res.coverage
        self.assertEqual(cov.native_query, 'search.title_and_abstract=("immune escape" OR "antigenic drift") NOT ("case report")')
        self.assertEqual((cov.total_available, cov.retrieved, cov.pages, cov.requests), (120, 5, 1, 1))
        self.assertEqual((cov.cost, cov.cost_unit), (10.0, "credits"))  # from X-RateLimit-Credits-Used
        self.assertIn("x-ratelimit-remaining", cov.rate_limit)
        self.assertEqual(cov.truncation_reason, "max_results")

        by_id = {r.source_id: r for r in res.records}
        preprint = by_id["W7219155415"]
        self.assertEqual(preprint.identifiers.arxiv, "2610.01111")
        self.assertEqual(preprint.dates.preprint_posted, "2026-10-01")
        self.assertIsNone(preprint.dates.published)
        article = by_id["W7221320175"]
        self.assertEqual(article.identifiers.doi, "10.1038/s41698-026-01732-3")
        self.assertEqual(article.dates.published, "2026-10-10")
        self.assertTrue(article.abstract.startswith("Epithelial"))
        for r in res.records:
            self.assertEqual(SourceRecord.model_validate_json(r.model_dump_json()), r)

    def test_fixtures_contain_no_secrets(self) -> None:
        # Response bodies are the source's own text (OpenAlex's notice mentions api_key), so check what we sent.
        for path in FIXTURES.glob("*.json"):
            for item in json.loads(path.read_text())["interactions"]:
                self.assertNotIn("api_key", item["request"], path.name)
                self.assertNotIn("authorization", item["response"]["headers"], path.name)

    def test_unrecorded_request_fails_instead_of_hitting_network(self) -> None:
        conn = OpenAlexConnector(replay("openalex_search.json"), api_key="")
        with self.assertRaises(AssertionError):
            conn.search(QUERY.model_copy(update={"max_results": 6}))


# -------------------------
# Parsers
# -------------------------

PUBMED_XML = """
<PubmedArticle>
  <MedlineCitation Status="MEDLINE">
    <PMID Version="1">111</PMID>
    <DateRevised><Year>2026</Year><Month>11</Month><Day>02</Day></DateRevised>
    <Article>
      <Journal><Title>J Test</Title><JournalIssue><PubDate><MedlineDate>2025 Winter</MedlineDate></PubDate></JournalIssue></Journal>
      <ArticleTitle>Escape of <i>SARS-CoV-2</i> variants</ArticleTitle>
      <ELocationID EIdType="doi">10.9999/Test.1</ELocationID>
      <Abstract><AbstractText Label="BACKGROUND">Why.</AbstractText><AbstractText Label="RESULTS">What.</AbstractText></Abstract>
      <AuthorList>
        <Author><LastName>Curie</LastName><ForeName>Marie</ForeName><Identifier Source="ORCID">0000-0002-1825-0097</Identifier></Author>
        <Author><LastName>Broken</LastName><Identifier Source="ORCID">not-an-orcid</Identifier></Author>
        <Author><CollectiveName>Genomics Consortium</CollectiveName></Author>
      </AuthorList>
      <Language>eng</Language>
      <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
    </Article>
    <CommentsCorrectionsList><CommentsCorrections RefType="RetractionIn"><PMID>222</PMID></CommentsCorrections></CommentsCorrectionsList>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">111</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
"""


class ParserTests(unittest.TestCase):
    def test_pubmed_article_edge_cases(self) -> None:
        r = parse_article(ET.fromstring(PUBMED_XML))
        self.assertEqual(r.title, "Escape of SARS-CoV-2 variants")
        self.assertEqual(r.abstract, "BACKGROUND: Why. RESULTS: What.")
        self.assertEqual(r.identifiers.doi, "10.9999/test.1")  # from ELocationID
        self.assertEqual([a.name for a in r.authors], ["Marie Curie", "Broken", "Genomics Consortium"])
        self.assertEqual(r.authors[0].orcid, "0000-0002-1825-0097")
        self.assertIsNone(r.authors[1].orcid)  # bad ORCID dropped, author kept
        self.assertEqual(r.dates.published, "2025")  # MedlineDate
        self.assertEqual(r.dates.source_updated, "2026-11-02")
        self.assertTrue(r.is_retracted)

    def test_pubmed_preprint_date(self) -> None:
        xml = PUBMED_XML.replace("<PublicationType>Journal Article</PublicationType>", "<PublicationType>Preprint</PublicationType>")
        xml = xml.replace("<ELocationID", '<ArticleDate DateType="Electronic"><Year>2026</Year><Month>10</Month><Day>03</Day></ArticleDate><ELocationID')
        r = parse_article(ET.fromstring(xml))
        self.assertEqual(r.dates.preprint_posted, "2026-10-03")
        self.assertIsNone(r.dates.published)

    def test_openalex_work(self) -> None:
        r = parse_work(work(
            5,
            doi="https://doi.org/10.1/ABC" + "D",
            ids={"pmid": "https://pubmed.ncbi.nlm.nih.gov/123", "pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/456"},
            abstract_inverted_index={"world": [1], "Hello": [0], "again": [3], "hello": [2]},
            authorships=[{"author": {"display_name": "A B", "orcid": "https://orcid.org/0000-0002-1825-0097"}}, {"author": {}}],
            primary_location={"landing_page_url": "https://x.org/p", "source": {"display_name": "Venue"}},
            is_retracted=True,
            updated_date="2026-11-01T10:00:00",
        ))
        self.assertIsNone(r.identifiers.doi)  # "10.1/..." is not a valid DOI prefix: dropped, not invented
        self.assertEqual((r.identifiers.pmid, r.identifiers.pmcid, r.identifiers.openalex), ("123", "PMC456", "W5"))
        self.assertEqual(r.abstract, "Hello world hello again")
        self.assertEqual(len(r.authors), 1)
        self.assertEqual((r.venue, r.url, r.is_retracted), ("Venue", "https://x.org/p", True))
        self.assertEqual(r.dates.source_updated, "2026-11-01")
        self.assertIsNone(reconstruct_abstract(None))
        self.assertIsNone(parse_work({"id": "nonsense"}))

    def test_query_builders(self) -> None:
        q = SearchQuery(terms=["CRISPR", 'base "editing"'], start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 2))
        self.assertEqual(build_search(q), '(CRISPR OR "base editing")')
        self.assertEqual(build_term(q), '((CRISPR) OR (base "editing"))')


# -------------------------
# Query bounds
# -------------------------

class SearchQueryTests(unittest.TestCase):
    def test_no_full_corpus_scans(self) -> None:
        with self.assertRaises(ValidationError):
            SearchQuery(terms=["x"], start=dt.date(2024, 1, 1), end=dt.date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            SearchQuery(terms=[], start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 2))
        with self.assertRaises(ValidationError):
            SearchQuery(terms=["x"], start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 2), max_results=100000)

    def test_from_radar_uses_window(self) -> None:
        radar = load_radar(REPO / "examples" / "viral_evolution.yml")
        q = SearchQuery.from_radar(radar, as_of=dt.date(2026, 11, 23))
        self.assertEqual(q.terms, radar.queries)
        self.assertEqual(q.end, dt.date(2026, 11, 23))
        self.assertEqual((q.end - q.start).days + 1, radar.filters.days)


# -------------------------
# HTTP behaviour (synthetic)
# -------------------------

class OpenAlexPagingTests(unittest.TestCase):
    def query(self, n: int) -> SearchQuery:
        return QUERY.model_copy(update={"max_results": n})

    def test_cursor_paging_with_key_and_key_kept_secret(self) -> None:
        t = FakeTransport(oa_page([work(1), work(2)], "c2", count=3), oa_page([work(3)], None, count=3))
        res = OpenAlexConnector(HttpClient(t), api_key="sekret").search(self.query(10))
        self.assertEqual([r.source_id for r in res.records], ["W1", "W2", "W3"])
        self.assertEqual([dict(r.params)["cursor"] for r in t.sent], ["*", "c2"])
        self.assertFalse(res.coverage.truncated)
        self.assertEqual(res.coverage.cost, 20.0)
        for r in t.sent:
            self.assertEqual(dict(r.secret_headers), {"Authorization": "Bearer sekret"})
            self.assertNotIn("sekret", r.key())
        self.assertNotIn("sekret", res.coverage.model_dump_json())

    def test_without_key_stops_after_first_page(self) -> None:
        t = FakeTransport(oa_page([work(1), work(2)], "c2", count=50))
        res = OpenAlexConnector(HttpClient(t), api_key="").search(self.query(10))
        self.assertEqual(len(t.sent), 1)
        self.assertEqual((res.coverage.truncated, res.coverage.truncation_reason), (True, "no_api_key"))

    def test_budget_stops_paging_and_is_recorded(self) -> None:
        t = FakeTransport(oa_page([work(1)], "c2", count=9), oa_page([work(2)], "c3", count=9))
        client = HttpClient(t, budget=Budget(15, unit="credits"))
        res = OpenAlexConnector(client, api_key="k").search(self.query(10))
        self.assertEqual(len(t.sent), 1)
        self.assertEqual(res.coverage.truncation_reason, "budget")
        self.assertIn("budget of 15 credits", res.coverage.errors[0])
        # A run that cannot afford even the first page fails loudly.
        with self.assertRaises(BudgetExceeded):
            OpenAlexConnector(HttpClient(FakeTransport(), budget=Budget(5)), api_key="k").search(self.query(10))

    def test_fetch_uses_cheap_list_requests(self) -> None:
        t = FakeTransport(oa_page([work(7)], credits="1"))
        client = HttpClient(t)
        recs = OpenAlexConnector(client, api_key="").fetch(["https://openalex.org/W7", "garbage"])
        self.assertEqual([r.source_id for r in recs], ["W7"])
        self.assertEqual(dict(t.sent[0].params)["filter"], "ids.openalex:W7")
        self.assertEqual(client.budget.used, 1.0)


class HttpClientTests(unittest.TestCase):
    REQ = Request.build("https://example.org/x", {"q": "a"}, secret_params={"api_key": "sekret"})

    def test_retries_honour_retry_after(self) -> None:
        sleeps = []
        t = FakeTransport(Response(429, b"slow down", {"retry-after": "2"}), Response(200, b"{}"))
        client = HttpClient(t, sleep=sleeps.append)
        self.assertTrue(client.get(self.REQ).ok)
        self.assertEqual((sleeps, client.stats.retries), ([2.0], 1))

    def test_gives_up_after_max_retries(self) -> None:
        t = FakeTransport(*[Response(503, b"down")] * 3)
        with self.assertRaises(ConnectorError) as ctx:
            HttpClient(t, max_retries=2, sleep=lambda s: None).get(self.REQ)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(len(t.sent), 3)

    def test_client_errors_are_not_retried(self) -> None:
        t = FakeTransport(Response(400, b"bad"))
        with self.assertRaises(ConnectorError):
            HttpClient(t, sleep=lambda s: None).get(self.REQ)
        self.assertEqual(len(t.sent), 1)

    def test_secrets_sent_but_never_part_of_the_key(self) -> None:
        self.assertIn("api_key=sekret", self.REQ.full_url())
        self.assertNotIn("sekret", self.REQ.key())
        self.assertEqual(self.REQ, Request.build("https://example.org/x", {"q": "a"}, secret_params={"api_key": "other"}))

    def test_cache_hits_are_free_and_expire(self) -> None:
        now = [1000.0]
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(d, ttl=dt.timedelta(hours=1), clock=lambda: now[0])
            t = FakeTransport(Response(200, b'{"v": 1}'), Response(200, b'{"v": 2}'))
            client = HttpClient(t, cache=cache)
            self.assertEqual(client.get(self.REQ, cost=10).json(), {"v": 1})
            self.assertEqual(client.get(self.REQ, cost=10).json(), {"v": 1})
            self.assertEqual((client.stats.cache_hits, client.budget.used), (1, 10.0))
            now[0] += 3601
            self.assertEqual(client.get(self.REQ, cost=10).json(), {"v": 2})
            self.assertNotIn("sekret", "".join(p.read_text() for p in Path(d).rglob("*.json")))

    def test_recording_drops_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rec = RecordingTransport(FakeTransport(Response(200, b"ok", {"Set-Cookie": "x", "X-RateLimit-Remaining": "5"})), Path(d) / "f.json")
            HttpClient(rec).get(self.REQ)
            text = rec.save().read_text()
            self.assertNotIn("sekret", text)
            self.assertNotIn("Set-Cookie", text)
            self.assertIn("x-ratelimit-remaining", text)
            self.assertTrue(HttpClient(ReplayTransport(Path(d) / "f.json")).get(self.REQ).ok)

    def test_pacing(self) -> None:
        sleeps, now = [], [0.0]
        t = FakeTransport(Response(200, b""), Response(200, b""))
        client = HttpClient(t, min_interval=0.5, sleep=sleeps.append, clock=lambda: now[0])
        client.get(self.REQ)
        now[0] += 0.2
        client.get(self.REQ)
        self.assertEqual([round(s, 3) for s in sleeps], [0.3])


# -------------------------
# Into the store
# -------------------------

class ConnectorToStoreTests(unittest.TestCase):
    def test_same_paper_from_both_connectors_merges_in_store(self) -> None:
        pm = PubMedConnector(replay("pubmed_search.json"), api_key="", page_size=3, batch_size=5).search(QUERY)
        target = pm.records[0]
        oa = OpenAlexConnector(HttpClient(FakeTransport(oa_page([work(9, doi="https://doi.org/" + target.identifiers.doi)]))),
                               api_key="").search(QUERY)
        with PaperStore() as store:
            at = dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc)
            store.observe_many(pm.records, at)
            obs = store.observe_many(oa.records, at)[0]
            self.assertEqual(obs.status, "new_source")
            self.assertEqual(sorted(r.source.value for r in obs.paper.sources), ["openalex", "pubmed"])
            self.assertEqual(obs.paper.identifiers.openalex, "W9")
            self.assertEqual(store.stats()["papers"], 5)


if __name__ == "__main__":
    unittest.main()
