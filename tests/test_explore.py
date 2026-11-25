"""Explore (plan task 33): a radar's literature graph from stored citation edges and topics only.

Done when: rendered from stored edges and topics only; readable without colour, keyboard-navigable, with a list view
of the same neighbourhood; Playwright on an engine-built fixture checks nodes and edges against the store
(apps/web/e2e/explore.spec.ts). These tests check the graph itself, and that the API returns what the store holds.
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

from carousel.connectors import ReplayTransport
from carousel.database.local import PaperStore
from carousel.intelligence.topics import Topic, TopicModel
from carousel.search.citations import EdgeLeakageError
from carousel.search.explore import build_subgraph, store_subgraph
from test_connectors import FIXTURES

sys.path.insert(0, str(FIXTURES))
from record_fixtures import CITATION_SEEDS, citation_scan  # noqa: E402

UTC = dt.timezone.utc
AS_OF = dt.datetime(2026, 10, 16, tzinfo=UTC)


def node(pid, work, roles=("recommended",), rank=None, topic=None):
    return {"paper_id": pid, "title": pid.upper(), "doi": None, "work": work, "roles": list(roles), "rank": rank,
            "topic_id": topic, "date": "2026-10-01"}


def edge(a, b, at="2026-10-01T00:00:00.000000Z"):
    return {"citing": a, "cited": b, "observed_at": at}


class BuildTests(unittest.TestCase):
    NODES = [node("p1", "W1", rank=1, topic="t1"), node("p2", "W2", rank=2, topic="t2"),
             node("s", "WS", roles=("seed",)), node("x", None, roles=("saved", "recommended"), rank=3)]
    EDGES = [edge("W1", "WS"), edge("W2", "WS"), edge("W1", "W9"), edge("W2", "W9"), edge("W1", "W8"), edge("W2", "W8"),
             edge("W1", "W7"), edge("WS", "W8"), edge("W2", "W1", at="2026-10-20T00:00:00.000000Z")]

    def build(self, at=AS_OF, edges=None):
        return build_subgraph(self.NODES, self.EDGES if edges is None else edges, {"t1": "Alpha", "t2": "Beta"},
                              radar_id="r", run_id="run", as_of=at)

    def test_links_come_from_stored_edges_between_nodes(self) -> None:
        g = self.build()
        cites = [(x["source"], x["target"]) for x in g["links"] if x["kind"] == "citation"]
        self.assertEqual(cites, [("p1", "s"), ("p2", "s")])  # W1->W9 etc. leave the graph; W2->W1 is later
        shared = [(x["source"], x["target"], x["shared"]) for x in g["links"] if x["kind"] == "shared_references"]
        self.assertEqual(shared, [("p1", "p2", 3)])  # WS, W8, W9; p1/s share only W8 (below 2)
        by = {n["paper_id"]: n for n in g["nodes"]}
        self.assertEqual((by["x"]["roles"], by["x"]["citation_data"]), (["saved", "recommended"], False))
        self.assertEqual((by["p1"]["topic_label"], by["p1"]["links"]), ("Alpha", 2))
        self.assertEqual([n["paper_id"] for n in g["nodes"]], ["p1", "p2", "x", "s"])  # by topic, then rank
        self.assertEqual(g["topics"], [{"topic_id": "t1", "label": "Alpha", "papers": 1},
                                       {"topic_id": "t2", "label": "Beta", "papers": 1},
                                       {"topic_id": None, "label": None, "papers": 2}])

    def test_a_later_view_sees_later_edges_and_an_earlier_one_never_does(self) -> None:
        later = self.build(at=dt.datetime(2026, 10, 21, tzinfo=UTC))
        self.assertIn(("p2", "p1"), [(x["source"], x["target"]) for x in later["links"] if x["kind"] == "citation"])
        self.assertEqual(self.build(), build_subgraph(self.NODES, [e for e in self.EDGES if e["observed_at"] <= "2026-10-16"],
                                                      {"t1": "Alpha", "t2": "Beta"}, radar_id="r", run_id="run", as_of=AS_OF))
        with self.assertRaises(EdgeLeakageError):  # the guard itself
            from carousel.search.citations import check_edges_no_leakage
            check_edges_no_leakage(self.EDGES, "2026-10-16T00:00:00.000000Z")


def scanned_store(path: Path) -> PaperStore:
    """The recorded task-32 scan, plus one saved paper and a two-topic model over the run's papers."""
    citation_scan(ReplayTransport(FIXTURES / "citations.json"), path)
    st = PaperStore(path)
    run = st.get_run(st.list_runs("escape")[-1]["run_id"])
    ids = [it["paper_id"] for it in run["items"]]
    st.sync_feedback("escape", {(ids[2], "save"): "test"}, dt.datetime(2026, 10, 15, 7, tzinfo=UTC))
    topics = [Topic("tp_a", "antibody escape", ["antibody"], [0.0] * 4, 5, 0.1),
              Topic("tp_b", "tumour immunity", ["tumour"], [0.0] * 4, 5, 0.1)]
    members = {pid: ("tp_a" if i % 2 else "tp_b") for i, pid in enumerate(ids[:6])}
    st.save_topic_model(TopicModel("topics-1:test", "escape", "hashing", {"k": 2}, topics, members,
                                   "2026-10-15T07:00:00.000000Z"), dt.datetime(2026, 10, 15, 7, tzinfo=UTC))
    return st


class StoreTests(unittest.TestCase):
    def test_the_graph_of_the_recorded_scan(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            st = scanned_store(Path(d) / "s.db")
            g = store_subgraph(st, "escape", CITATION_SEEDS, as_of=AS_OF)
            work = {n["paper_id"]: st.get(n["paper_id"]).identifiers.openalex for n in g["nodes"]}
            stored = {(e["citing"], e["cited"]) for e in st.edges_as_of("2026-10-16T00:00:00.000000Z")}
            st.close()
        roles = {n["paper_id"]: n["roles"] for n in g["nodes"]}
        self.assertEqual(sum("seed" in r for r in roles.values()), 2)
        self.assertEqual(sum("saved" in r for r in roles.values()), 1)
        self.assertEqual(sum("recommended" in r for r in roles.values()), 10)
        cites = [x for x in g["links"] if x["kind"] == "citation"]
        self.assertGreaterEqual(len(cites), 5)  # the five papers found by citation cite a seed
        for x in cites:  # every link is a stored edge
            self.assertIn((work[x["source"]], work[x["target"]]), stored)
        self.assertEqual({t["label"] for t in g["topics"]}, {"antibody escape", "tumour immunity", None})
        self.assertEqual(g["classifier_version"], "topics-1:test")


try:
    from test_api import URL, ApiTestCase, create_app
except ImportError:  # pragma: no cover
    URL = create_app = None
    ApiTestCase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(URL and create_app, "set CAROUSEL_TEST_DATABASE_URL and install the [server] and [dev] extras")
class ApiTests(ApiTestCase):
    def test_the_api_returns_the_graph_the_store_holds(self) -> None:
        from carousel.api import services
        from carousel.core.config import RadarConfig
        from carousel.database import importer, postgres
        from record_fixtures import CITATION_RADAR
        from sqlalchemy.orm import Session

        db = Path(self.tmp.name) / "s.db"
        st = scanned_store(db)
        expected = store_subgraph(st, "escape", CITATION_SEEDS, as_of=AS_OF)
        st.close()
        engine = postgres.engine(URL)
        importer.import_store(db, engine)
        with Session(engine) as s:
            services.upsert_radar(s, RadarConfig.model_validate(CITATION_RADAR))
            s.commit()
        engine.dispose()
        r = self.client.get("/api/v1/radars/escape/graph", params={"as_of": AS_OF.isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()
        strip = lambda g: [{k: v for k, v in n.items() if k != "date"} for n in g["nodes"]]  # noqa: E731 (Postgres keeps full dates)
        self.assertEqual(strip(got), strip(expected))
        self.assertEqual(got["links"], expected["links"])
        self.assertEqual((got["topics"], got["run_id"], got["classifier_version"]),
                         (expected["topics"], expected["run_id"], expected["classifier_version"]))
        self.assertEqual(self.client.get("/api/v1/radars/nope/graph").status_code, 404)


if __name__ == "__main__":
    unittest.main()
