"""Citation graph v1 (plan task 32): OpenAlex citations, append-only edges, citation expansion, the graph term.

Done when: recorded-HTTP fixture tests; a leakage test fails if a replay at t uses an edge observed after t; the
OpenAlex budget is never exceeded; a radar with no seeds ranks exactly as under 0.1.0.
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

from carousel.connectors import HttpClient, OpenAlexConnector, ReplayTransport
from carousel.connectors.base import Budget
from carousel.core.paper import SourceRecord
from carousel.database.local import PaperStore
from carousel.ranking.explain import explain
from carousel.search.citations import EdgeLeakageError, check_edges_no_leakage, graph_features
from test_connectors import FIXTURES

sys.path.insert(0, str(FIXTURES))
from record_fixtures import CITATION_RADAR, CITATION_SEEDS, QUERY, citation_scan  # noqa: E402

UTC = dt.timezone.utc
T1 = dt.datetime(2026, 10, 1, tzinfo=UTC)
T2 = dt.datetime(2026, 10, 8, tzinfo=UTC)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def replay(budget=None) -> HttpClient:
    return HttpClient(ReplayTransport(FIXTURES / "citations.json"), sleep=lambda s: None,
                      budget=Budget(budget, unit="credits") if budget is not None else None)


def paper(st: PaperStore, n: int, openalex: bool = True, title: str = "") -> str:
    rec = SourceRecord(source="openalex" if openalex else "pubmed", source_id=f"W{n}" if openalex else str(n),
                       identifiers={"openalex": f"W{n}"} if openalex else {"pmid": str(n)},
                       title=title or f"Paper {n}", dates={"published": "2026-01-01"})
    return st.observe(rec, T1).paper.paper_id


class ConnectorTests(unittest.TestCase):
    def test_references_and_citing_from_the_recording(self) -> None:
        conn = OpenAlexConnector(replay(), api_key="")
        seeds = conn.references(dois=CITATION_SEEDS)
        self.assertEqual(len(seeds.records), 2)  # the unregistered DOI returns nothing
        self.assertEqual(seeds.cost, 1.0)  # one list request, 1 credit
        works = sorted(seeds.references)
        self.assertTrue(all(len(seeds.references[w]) > 10 for w in works))
        citing = conn.citing(works, QUERY.start, QUERY.end, max_results=QUERY.max_results)
        self.assertEqual(len(citing.records), 5)
        self.assertEqual((citing.truncated, citing.truncation_reason), (True, "max_results"))
        self.assertGreater(citing.total_available, 5)
        for rec in citing.records:  # every result cites a seed and falls in the window
            self.assertTrue(set(citing.references[rec.identifiers.openalex]) & set(works))


class StoreTests(unittest.TestCase):
    def test_edges_are_append_only_and_read_as_of(self) -> None:
        st = PaperStore(":memory:")
        self.assertEqual(st.save_citations({"W1": ["W2", "W3"]}, iso(T1)), 2)
        self.assertEqual(st.save_citations({"W1": ["W2", "W4"]}, iso(T2)), 1)  # W1->W2 keeps its first time
        self.assertEqual([(e["cited"], e["observed_at"]) for e in st.edges_as_of(iso(T2))],
                         [("W2", iso(T1)), ("W3", iso(T1)), ("W4", iso(T2))])
        self.assertEqual([e["cited"] for e in st.edges_as_of(iso(T1))], ["W2", "W3"])
        self.assertEqual(st.reference_lists_as_of(iso(T1)), {"W1": iso(T1)})
        self.assertEqual(st.reference_lists_as_of(iso(T1 - dt.timedelta(days=1))), {})


class GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.st = st = PaperStore(":memory:")
        self.seed, self.saved = paper(st, 1, title="Seed"), paper(st, 2, title="Saved")
        self.cites, self.coupled, self.none = paper(st, 10), paper(st, 11), paper(st, 12)
        self.unread, self.pubmed_only = paper(st, 13), paper(st, 14, openalex=False)
        st.save_citations({"W1": ["W100", "W101", "W102"], "W2": ["W103"], "W10": ["W1"],
                           "W11": ["W100", "W101", "W103", "W999"], "W12": ["W500"]}, iso(T1))
        self.anchors = {self.seed: "seed", self.saved: "feedback"}

    def features(self, at=T1):
        return graph_features(self.st, self.anchors, [self.cites, self.coupled, self.none, self.unread, self.pubmed_only], at)

    def test_values(self) -> None:
        g = self.features()
        self.assertEqual((g[self.cites]["value"], [x["title"] for x in g[self.cites]["cites"]]), (1.0, ["Seed"]))
        self.assertEqual(g[self.coupled]["shared_references"], 3)  # W100, W101 (seed) and W103 (saved)
        self.assertEqual((g[self.coupled]["value"], g[self.coupled]["value_seeds"]), (0.3, 0.2))
        self.assertEqual((g[self.none]["status"], g[self.none]["value"]), ("ok", 0.0))
        self.assertEqual((g[self.unread]["status"], g[self.unread]["value"]), ("references_not_read", 0.0))
        self.assertEqual(g[self.pubmed_only]["status"], "no_openalex_id")

    def test_an_anchor_citing_a_paper_links_it(self) -> None:
        self.st.save_citations({}, iso(T1), edges=[("W2", "W13")])
        g = self.features()
        self.assertEqual((g[self.unread]["status"], g[self.unread]["value"], g[self.unread]["value_seeds"]), ("ok", 1.0, 0.0))
        self.assertEqual(g[self.unread]["cited_by"][0]["anchor"], "feedback")

    def test_an_edge_observed_later_is_not_used(self) -> None:
        """The leakage guard: a decision at T1 is unchanged by edges observed after T1."""
        before = self.features(T1)
        self.st.save_citations({"W12": ["W1"]}, iso(T2))  # observed a week later: W12 now cites the seed
        self.assertEqual(self.features(T1), before)
        self.assertEqual(self.features(T2)[self.none]["value"], 1.0)
        with self.assertRaises(EdgeLeakageError):
            check_edges_no_leakage(self.st.edges_as_of(iso(T2)), iso(T1))

    def test_explanation_names_the_linked_seed(self) -> None:
        g = self.features()
        item = {"score": 0.5, "components": {"relevance": 0.5},
                "details": {"retrieved_by": ["citation"], "graph": g[self.cites], "matched_terms": []}}
        e = explain(item, {"relevance": 1.0}, {})
        self.assertIn("cites seed paper “Seed”", e["reasons"][0]["text"])
        self.assertIn("Linked by citation to a seed paper", e["summary"])
        coupled = explain({**item, "details": {"retrieved_by": ["lexical"], "graph": g[self.coupled]}}, {"relevance": 1.0}, {})
        self.assertIn("shares 3 references with your seed and saved papers", coupled["reasons"][0]["text"])


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_citation_expansion_and_graph_term(self) -> None:
        res = citation_scan(ReplayTransport(FIXTURES / "citations.json"), self.dir / "s.db")
        run = res.run
        self.assertEqual((run["status"], run["ranking_version"], run["candidates_version"]), ("ok", "0.3.0", "0.3.0"))
        cov = [c for c in run["coverage"] if c.get("role") == "citations"][0]
        self.assertEqual(cov["anchors"]["seed"], 2)
        self.assertEqual(cov["anchors"]["seeds_not_found"], ["10.9999/carousel-not-registered"])
        self.assertEqual((cov["retrieved"], cov["truncation_reason"]), (5, "max_results"))
        self.assertEqual(cov["cost"], cov["requests"])  # list requests only: 1 credit each
        stats = run["ranking"]["stats"]
        self.assertEqual(stats["relevance_formula"], "(0.6*sim + 0.4*bm25_norm + 0.25*graph) / 1.25")
        self.assertEqual(stats["candidate_stats"]["citation_hits"], 5)
        self.assertEqual(stats["profile"]["seeds_used"], sorted(CITATION_SEEDS[:2]))  # seeds fetched by DOI
        by_citation = [it for it in res.items if "citation" in it["details"]["retrieved_by"]]
        self.assertEqual(len(by_citation), 5)
        for it in by_citation:  # every paper found through citations cites a seed, and says so
            g = it["details"]["graph"]
            self.assertEqual((g["status"], g["value"]), ("ok", 1.0))
            self.assertTrue(all(x["anchor"] == "seed" for x in g["cites"]))
            self.assertIn("cites seed paper", it["details"]["explanation"]["reasons"][0]["text"])
        for it in res.items:
            self.assertEqual(it["details"]["graph"]["status"], "ok")  # every candidate's references were read
        again = citation_scan(ReplayTransport(FIXTURES / "citations.json"), self.dir / "t.db")
        self.assertEqual([i["paper_id"] for i in again.items], [i["paper_id"] for i in res.items])

    def test_the_budget_is_never_exceeded(self) -> None:
        from carousel import Radar
        from carousel.core.config import RadarConfig

        client = replay(budget=11)  # the search (10 credits) and one lookup (1 credit), no more
        res = Radar(RadarConfig.model_validate(CITATION_RADAR)).scan(
            as_of=QUERY.end, observed_at=dt.datetime(2026, 10, 15, 6, tzinfo=UTC), store=self.dir / "s.db",
            connectors={"openalex": OpenAlexConnector(client, api_key="")}, embedder="hashing", max_results=QUERY.max_results)
        self.assertLessEqual(client.stats.cost, 11)
        cov = [c for c in res.run["coverage"] if c.get("role") == "citations"][0]
        self.assertEqual(cov["status"], "error")
        self.assertIn("budget", cov["errors"][0])
        self.assertEqual(res.run["status"], "partial")
        self.assertGreater(len(res.items), 0)  # the scan still ranks what it has

    def test_a_radar_without_seeds_makes_no_citation_requests_and_ranks_as_before(self) -> None:
        from carousel import Radar
        from carousel.core.config import RadarConfig

        cfg = RadarConfig.model_validate({k: v for k, v in CITATION_RADAR.items() if k != "seed_papers"})
        client = replay()
        res = Radar(cfg).scan(as_of=QUERY.end, observed_at=dt.datetime(2026, 10, 15, 6, tzinfo=UTC), store=self.dir / "s.db",
                              connectors={"openalex": OpenAlexConnector(client, api_key="")}, embedder="hashing",
                              max_results=QUERY.max_results)
        self.assertEqual(client.stats.requests, 1)  # the search only
        self.assertFalse(any(c.get("role") == "citations" for c in res.run["coverage"]))
        stats = res.run["ranking"]["stats"]
        self.assertEqual((stats["relevance_formula"], stats["graph"]), ("0.6*sim + 0.4*bm25_norm", None))
        self.assertTrue(all("graph" not in it["details"] for it in res.items))


try:
    from test_postgres import URL, PostgresTestCase, importer
except ImportError:  # pragma: no cover
    URL = importer = None
    PostgresTestCase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(URL and importer, "set CAROUSEL_TEST_DATABASE_URL and install the [server] extra")
class PostgresImportTests(PostgresTestCase):
    def test_citations_import_and_reimport(self) -> None:
        db = self.dir / "s.db"
        citation_scan(ReplayTransport(FIXTURES / "citations.json"), db)
        with PaperStore(db) as st:
            n_edges = st.db.execute("SELECT count(*) FROM citation_edges").fetchone()[0]
            n_refs = st.db.execute("SELECT count(*) FROM reference_lists").fetchone()[0]
        counts = importer.import_store(db, self.engine)
        self.assertEqual((counts["citation_edges"], self.count("citation_edges")), (n_edges, n_edges))
        self.assertEqual((counts["reference_lists"], self.count("reference_lists")), (n_refs, n_refs))
        importer.import_store(db, self.engine)
        self.assertEqual((self.count("citation_edges"), self.count("reference_lists")), (n_edges, n_refs))


if __name__ == "__main__":
    unittest.main()
