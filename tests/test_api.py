"""REST API v1 and the scan worker (plan task 12).

The contract test needs only the [server] extra. The end-to-end tests also
need CAROUSEL_TEST_DATABASE_URL (a disposable Postgres with pgvector); Redis is
faked with fakeredis, and scans replay the recorded PubMed/OpenAlex fixtures.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

URL = os.environ.get("CAROUSEL_TEST_DATABASE_URL")
REPO = Path(__file__).resolve().parents[1]

try:
    import fakeredis
    from fastapi.testclient import TestClient
    from rq import Queue, SimpleWorker
    from sqlalchemy import text

    from carousel.api.app import Settings, create_app, openapi_document
    from carousel.database import models as m
    from carousel.database import postgres
    from carousel.workers.scan import QUEUE
except ImportError:  # the [server] or [dev] extras aren't installed
    create_app = None

RADAR = {
    "radar_id": "escape", "radar_name": "Escape radar", "description": "Viral and tumour immune escape",
    "sources": ["pubmed", "openalex", "biorxiv"], "queries": ["immune escape", "antigenic drift"],
    "exclude": ["case report"], "filters": {"date_range": "last_14_days"},
    "methods_profile": ["single-cell", "protein language model"],
}
SCAN = {"max_results": 5, "as_of": "2026-10-14"}  # matches the recorded fixtures


@unittest.skipUnless(create_app, "needs the [server] and [dev] extras")
class OpenApiContractTests(unittest.TestCase):
    def test_committed_schema_matches_the_app(self) -> None:
        committed = json.loads((REPO / "openapi" / "carousel-v1.json").read_text())
        self.assertEqual(committed, json.loads(json.dumps(openapi_document())),
                         "API changed: run `carousel api openapi --out openapi/carousel-v1.json` and commit it")

    def test_spec_routes_are_present(self) -> None:
        paths = openapi_document()["paths"]
        for method, path in [("post", "/api/v1/radars"), ("get", "/api/v1/radars/{radar_id}"),
                             ("post", "/api/v1/radars/{radar_id}/runs"), ("get", "/api/v1/radars/{radar_id}/papers"),
                             ("get", "/api/v1/radars/{radar_id}/delta"), ("get", "/api/v1/papers/{paper_id}"),
                             ("get", "/api/v1/papers/{paper_id}/explanation"), ("post", "/api/v1/feedback"),
                             ("get", "/api/v1/jobs/{job_id}")]:
            self.assertIn(method, paths.get(path, {}), f"{method.upper()} {path}")
        self.assertIn("202", paths["/api/v1/radars/{radar_id}/runs"]["post"]["responses"])


@unittest.skipUnless(URL and create_app, "set CAROUSEL_TEST_DATABASE_URL and install the [server] and [dev] extras")
class ApiTestCase(unittest.TestCase):
    token = None

    @classmethod
    def setUpClass(cls) -> None:
        postgres.downgrade(URL)
        postgres.upgrade(URL)

    def setUp(self) -> None:
        engine = postgres.engine(URL)
        tables = ", ".join(t.name for t in m.Base.metadata.sorted_tables)
        with engine.begin() as c:
            c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        engine.dispose()
        self.tmp = tempfile.TemporaryDirectory()
        self.redis = fakeredis.FakeRedis()
        self.queue = Queue(QUEUE, connection=self.redis)
        settings = Settings(database_url=URL, api_token=self.token, store_path=str(Path(self.tmp.name) / "engine.db"))
        self.app = create_app(settings, queue=self.queue)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        from test_scan import connectors

        factories = {"pubmed": lambda: connectors()["pubmed"], "openalex": lambda: connectors()["openalex"]}
        self.patches = [mock.patch.dict("carousel.radar.CONNECTORS", factories, clear=True),
                        mock.patch.dict(os.environ, {"CAROUSEL_EMBEDDER": "hashing"})]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.client.close()
        self.app.state.engine.dispose()
        self.tmp.cleanup()

    def work(self) -> None:
        """Run queued jobs in this process, the way `carousel worker` would."""
        SimpleWorker([self.queue], connection=self.redis).work(burst=True)

    def post(self, path, body=None):
        return self.client.post(f"/api/v1{path}", json=body, headers=self.auth)

    def get(self, path, **params):
        return self.client.get(f"/api/v1{path}", params=params)

    def scan(self) -> str:
        r = self.post("/radars/escape/runs", SCAN)
        self.assertEqual(r.status_code, 202, r.text)
        job = r.json()
        self.assertEqual(job["status"], "queued")
        self.work()
        done = self.get(f"/jobs/{job['job_id']}").json()
        self.assertEqual(done["status"], "finished", done)
        return done["run_id"]


class AcceptanceFlowTests(ApiTestCase):
    """Spec Phase 1 acceptance, through the API: define a topic, get a deduplicated ranked list,
    save or dismiss papers, and repeat the scan without losing history."""

    def test_full_flow(self) -> None:
        self.assertEqual(self.get("/health").json()["status"], "ok")

        r = self.post("/radars", {"config": RADAR})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["config"]["queries"], ["immune escape", "antigenic drift"])
        self.assertEqual(self.post("/radars", {"config": RADAR}).status_code, 200)

        run1 = self.scan()
        radar = self.get("/radars/escape").json()
        self.assertEqual((radar["n_runs"], radar["latest_run"]["run_id"]), (1, run1))

        ranked = self.get("/radars/escape/papers").json()
        self.assertEqual(ranked["run"]["run_id"], run1)
        self.assertEqual(len(ranked["items"]), 10)
        top = ranked["items"][0]
        self.assertIn("relevance", top["features"])
        self.assertTrue(top["paper"]["title"])
        cov = {c["source"]: c["status"] for c in ranked["run"]["coverage"]}
        self.assertEqual(cov, {"pubmed": "ok", "openalex": "ok", "biorxiv": "no_connector"})

        paper = self.get(f"/papers/{top['paper_id']}").json()
        self.assertEqual(paper["paper_id"], top["paper_id"])
        self.assertTrue(paper["identifiers"])
        self.assertTrue(paper["sources"])
        why = self.get(f"/papers/{top['paper_id']}/explanation", radar_id="escape").json()
        self.assertEqual((why["run_id"], why["rank"], why["ranking_version"]), (run1, 1, "0.1.0"))
        self.assertIn("retrieved_by", why["details"])
        self.assertAlmostEqual(sum(why["weights"].values()), 1.0, places=5)

        fb = self.post("/feedback", {"radar_id": "escape", "paper_id": top["paper_id"], "action": "dismiss"})
        self.assertEqual(fb.status_code, 201, fb.text)
        self.assertEqual(fb.json()["state"]["dismiss"], [top["paper_id"]])
        saved = ranked["items"][1]["paper_id"]
        self.post("/feedback", {"radar_id": "escape", "paper_id": saved, "action": "save"})
        self.assertTrue(self.get("/radars/escape/papers").json()["items"][0]["dismissed"])

        run2 = self.scan()
        second = self.get("/radars/escape/papers").json()
        ids = [i["paper_id"] for i in second["items"]]
        self.assertEqual(second["run"]["run_id"], run2)
        self.assertNotIn(top["paper_id"], ids)    # dismissed papers stay out
        self.assertNotIn(saved, ids)              # saved papers aren't re-recommended
        delta = self.get("/radars/escape/delta").json()
        self.assertEqual((delta["run_id"], delta["previous_run_id"]), (run2, run1))
        self.assertIn(top["paper_id"], delta["left"])

        runs = self.get("/radars/escape/runs").json()
        self.assertEqual([r["run_id"] for r in runs], [run2, run1])  # history kept
        self.assertEqual(self.get("/radars/escape/papers", run_id=run1).json()["items"][0]["paper_id"], top["paper_id"])
        self.assertEqual(self.get(f"/runs/{run1}").json()["ranking"]["window"]["days"], 14)

        hits = self.get("/papers", q="immune escape").json()
        self.assertTrue(hits)
        self.assertIn("paper_id", hits[0])

        # Undo the dismissal: it is back on the next scan.
        self.post("/feedback", {"radar_id": "escape", "paper_id": top["paper_id"], "action": "dismiss", "remove": True})
        self.scan()
        self.assertIn(top["paper_id"], [i["paper_id"] for i in self.get("/radars/escape/papers").json()["items"]])

    def test_config_versions(self) -> None:
        self.post("/radars", {"config": RADAR})
        changed = {**RADAR, "mode": "exploratory"}
        r = self.post("/radars", {"config": changed, "schedule": "0 13 * * *"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["config"]["mode"], r.json()["schedule"]), ("exploratory", "0 13 * * *"))
        engine = postgres.engine(URL)
        with engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM radar_versions WHERE radar_id = 'escape'")).scalar(), 2)
        engine.dispose()

    def test_merged_paper_redirects(self) -> None:
        self.post("/radars", {"config": RADAR})
        self.scan()
        items = self.get("/radars/escape/papers").json()["items"]
        keep, gone = items[0]["paper_id"], items[1]["paper_id"]
        engine = postgres.engine(URL)
        with engine.begin() as c:
            c.execute(text("UPDATE papers SET status = 'merged', merged_into = :k WHERE id = :g"), {"k": keep, "g": gone})
        engine.dispose()
        p = self.get(f"/papers/{gone}").json()
        self.assertEqual((p["paper_id"], p["requested_id"]), (keep, gone))


class ErrorTests(ApiTestCase):
    def test_not_found_and_validation(self) -> None:
        self.assertEqual(self.get("/radars/nope").status_code, 404)
        self.assertEqual(self.get("/radars/nope/papers").status_code, 404)
        self.assertEqual(self.post("/radars/nope/runs", {}).status_code, 404)
        self.assertEqual(self.get("/papers/cp_nope").status_code, 404)
        self.assertEqual(self.get("/jobs/job_nope").status_code, 404)
        self.assertEqual(self.get("/runs/run_nope").status_code, 404)
        r = self.post("/radars", {"config": {**RADAR, "sources": ["scopus"]}})
        self.assertEqual(r.status_code, 422)
        self.assertIn("scopus", r.text)
        self.assertEqual(self.post("/radars", {"config": {k: v for k, v in RADAR.items() if k != "radar_id"}}).status_code, 422)
        self.post("/radars", {"config": RADAR})
        self.assertEqual(self.get("/radars/escape/papers").status_code, 404)  # no runs yet
        self.assertEqual(self.post("/feedback", {"radar_id": "escape", "paper_id": "cp_nope", "action": "save"}).status_code, 404)
        self.assertEqual(self.post("/feedback", {"radar_id": "escape", "paper_id": "x", "action": "like"}).status_code, 422)
        self.assertEqual(self.post("/radars/escape/runs", {"top_n": 0}).status_code, 422)

    def test_failed_scan_is_reported(self) -> None:
        self.post("/radars", {"config": RADAR})
        with mock.patch("carousel.radar.Radar.scan", side_effect=RuntimeError("source exploded")):
            job = self.post("/radars/escape/runs", SCAN).json()
            self.work()
        out = self.get(f"/jobs/{job['job_id']}").json()
        self.assertEqual(out["status"], "failed")
        self.assertIn("source exploded", out["error"] or "")
        self.assertIsNone(out["run_id"])


class AuthTests(ApiTestCase):
    token = "test-token-for-local-api-tests"

    def test_writes_need_the_token_and_reads_do_not(self) -> None:
        bad = self.client.post("/api/v1/radars", json={"config": RADAR})
        self.assertEqual(bad.status_code, 401)
        wrong = self.client.post("/api/v1/radars", json={"config": RADAR}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(self.post("/radars", {"config": RADAR}).status_code, 201)
        self.assertEqual(self.get("/radars").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/radars/escape/runs", json=SCAN).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/feedback", json={"radar_id": "escape", "paper_id": "x",
                                                                    "action": "save"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
