"""arXiv connector (plan task 23). Recorded-HTTP fixtures only (tests/fixtures/http/arxiv_*.json)."""

import datetime as dt
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from carousel import Radar
from carousel.connectors import ArxivConnector, ConnectorError, CrossrefConnector, HttpClient, ReplayTransport, SearchQuery
from carousel.connectors.arxiv import NS, _feed, build_query, parse_entry
from carousel.core.config import RadarConfig
from carousel.database import PaperStore
from carousel.ingestion.versions import fetch_linked
from test_connectors import FIXTURES, replay
from test_scan import AS_OF, DAY1, connectors as pubmed_openalex

sys.path.insert(0, str(FIXTURES))
from record_fixtures import ARXIV_JOURNAL_DOI, ARXIV_QUERY, ARXIV_RECORDS  # noqa: E402

UTC = dt.timezone.utc


def arxiv(name, **kw):
    return ArxivConnector(replay(name), **kw)


class QueryTests(unittest.TestCase):
    def test_query(self) -> None:
        self.assertEqual(build_query(ARXIV_QUERY),
                         '((ti:"immune escape" OR abs:"immune escape" OR ti:"antigenic drift" OR abs:"antigenic drift" OR '
                         'ti:"viral evolution" OR abs:"viral evolution" OR ti:"protein language model" OR '
                         'abs:"protein language model") ANDNOT (ti:"case report" OR abs:"case report")) '
                         'AND submittedDate:[202610010000 TO 202610142359]')
        native = SearchQuery(terms=["x"], native="cat:q-bio.PE AND abs:fitness", start=dt.date(2026, 10, 1),
                             end=dt.date(2026, 10, 2))
        self.assertEqual(build_query(native), "(cat:q-bio.PE AND abs:fitness) AND submittedDate:[202610010000 TO 202610022359]")

    def test_errors_are_reported(self) -> None:
        err = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad</id>'
               '<title>Error</title><summary>incorrect id format</summary></entry></feed>')
        with self.assertRaisesRegex(ConnectorError, "arXiv rejected the query: incorrect id format"):
            _feed(err)
        with self.assertRaisesRegex(ConnectorError, "not an Atom feed"):
            _feed("<html>busy")


class SearchTests(unittest.TestCase):
    def test_paging_coverage_and_records(self) -> None:
        res = arxiv("arxiv_search.json", page_size=2).search(ARXIV_QUERY)
        cov = res.coverage
        self.assertEqual((cov.source, cov.total_available, cov.retrieved, cov.pages, cov.truncated, cov.truncation_reason),
                         ("arxiv", 9, 5, 3, True, "max_results"))
        self.assertEqual(cov.date_field, "first submission date (submittedDate)")
        for r in res.records:
            self.assertEqual(r.source.value, "arxiv")
            self.assertEqual(r.identifiers.arxiv, r.source_id)
            self.assertEqual(r.identifiers.doi, f"10.48550/arxiv.{r.source_id}")  # arXiv's own DOI, as OpenAlex has it
            self.assertEqual(r.publication_types, ["preprint"])
            self.assertTrue(r.venue.startswith("arXiv ("))
            self.assertTrue(r.title and r.abstract and r.authors and r.dates.preprint_posted)
            self.assertTrue("2026-10-01" <= r.dates.preprint_posted <= "2026-10-14")


class RecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        client = HttpClient(ReplayTransport(FIXTURES / "arxiv_extras.json"), sleep=lambda s: None)
        cls.client = client
        cls.records = {r.source_id: r for r in ArxivConnector(client).fetch(ARXIV_RECORDS)}

    def test_versions_and_the_journal_doi(self) -> None:
        old = self.records["1404.4197"]
        self.assertEqual((old.version, old.dates.preprint_posted, old.dates.version), ("v2", "2014-04-16", old.dates.version))
        self.assertGreater(old.dates.version, old.dates.preprint_posted)  # v2 came later
        self.assertEqual(old.identifiers.doi, "10.48550/arxiv.1404.4197")  # not the journal's DOI...
        self.assertEqual([(x.type, x.scheme, x.value, x.asserted_by) for x in old.relations],
                         [("is-preprint-of", "doi", ARXIV_JOURNAL_DOI, "arxiv")])  # ...which becomes a link
        new = self.records["2610.01111"]
        self.assertEqual((new.version, new.dates.version, new.relations), ("v1", None, []))
        self.assertEqual(new.raw["categories"], ["math.DS", "q-bio.PE"])

    def test_fetch_accepts_any_id_form(self) -> None:
        got = ArxivConnector(self.client).fetch(["arXiv:2610.01111v1", "https://arxiv.org/abs/1404.4197v2", "bogus"])
        self.assertEqual(sorted(r.source_id for r in got), ["1404.4197", "2610.01111"])

    def test_preprint_links_to_its_journal_version_without_merging(self) -> None:
        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            at = dt.datetime(2026, 11, 24, tzinfo=UTC)
            pre = st.observe(self.records["1404.4197"], at).paper.paper_id
            stats = fetch_linked(st, {"crossref": CrossrefConnector(self.client, mailto="")}, [pre], at)
            self.assertEqual((stats["looked_up"], stats["linked"]), (1, 1))
            [v] = st.version_links(pre)
            self.assertEqual(v["role"], "published")
            self.assertNotEqual(v["paper_id"], pre)  # two papers
            self.assertEqual(st.get(v["paper_id"]).identifiers.doi, ARXIV_JOURNAL_DOI)

    def test_the_same_preprint_from_openalex_merges(self) -> None:
        from carousel.connectors import OpenAlexConnector

        oa = OpenAlexConnector(replay("openalex_search.json"), api_key="")
        from record_fixtures import QUERY

        oa_rec = next(r for r in oa.search(QUERY).records if r.identifiers.doi == "10.48550/arxiv.2610.01111")
        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            at = dt.datetime(2026, 11, 24, tzinfo=UTC)
            a = st.observe(self.records["2610.01111"], at).paper.paper_id
            b = st.observe(oa_rec, at)
            self.assertEqual((b.paper.paper_id, b.status, b.match), (a, "new_source", "identifier"))
            self.assertEqual(sorted(s.source.value for s in b.paper.sources), ["arxiv", "openalex"])


class ProposalTests(unittest.TestCase):
    def test_the_proposal_shows_arxivs_query(self) -> None:
        try:
            from carousel.api.services import propose_radar
        except ImportError:  # pragma: no cover - server extras missing
            self.skipTest("needs the [server] extra")

        class NoRadars:
            def get(self, *_):
                return None

        prop = propose_radar(NoRadars(), topic="viral evolution", sources=["arxiv"], window_days=14,
                             as_of=dt.date(2026, 10, 14))
        [step] = prop["strategy"]
        self.assertEqual((step["source"], step["status"]), ("arxiv", "ready"))
        self.assertTrue(step["query"].startswith('(ti:"viral evolution" OR abs:"viral evolution")'))
        self.assertTrue(step["query"].endswith("AND submittedDate:[202610010000 TO 202610142359]"))
        self.assertIn("linked, not merged", " ".join(step["notes"]))


class ScanTests(unittest.TestCase):
    def test_arxiv_in_a_scan(self) -> None:
        cfg = RadarConfig.model_validate({
            "radar_id": "evo", "radar_name": "Evolution", "sources": ["arxiv"], "queries": ARXIV_QUERY.terms,
            "exclude": ARXIV_QUERY.exclude, "filters": {"date_range": "last_14_days"}})
        with tempfile.TemporaryDirectory() as d:
            res = Radar(cfg, store=Path(d) / "s.db").scan(
                as_of=AS_OF, observed_at=DAY1, connectors={"arxiv": arxiv("arxiv_search.json", page_size=2)},
                embedder="hashing", max_results=5, top_n=5)
            [cov] = res.run["coverage"]
            self.assertEqual((cov["source"], cov["status"], cov["retrieved"], cov["total_available"]), ("arxiv", "ok", 5, 9))
            self.assertTrue(res.items)
            self.assertTrue(all(it["details"]["sources"] == ["arxiv"] for it in res.items))
            self.assertTrue(all("arXiv" in it["details"]["explanation"]["reasons"][0]["text"] for it in res.items
                                if "source_query" in it["details"]["retrieved_by"]))


if __name__ == "__main__":
    unittest.main()
