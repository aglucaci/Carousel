"""REST API v1 and the scan worker (plan task 12).

The contract test needs only the [server] extra. The end-to-end tests also
need CAROUSEL_TEST_DATABASE_URL (a disposable Postgres with pgvector); Redis is
faked with fakeredis, and scans replay the recorded PubMed/OpenAlex fixtures.
"""

import datetime as dt
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
                             ("get", "/api/v1/jobs/{job_id}"), ("post", "/api/v1/radars/proposal"),
                             ("get", "/api/v1/library"), ("patch", "/api/v1/radars/{radar_id}")]:
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
                        mock.patch.dict(os.environ, {"CAROUSEL_EMBEDDER": "hashing", "CAROUSEL_DATABASE_URL": URL,
                                                     "CAROUSEL_STORE": settings.store_path})]
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
        from rq.job import Job

        queued = Job.fetch(job["job_id"], connection=self.redis)
        self.assertNotIn("database_url", queued.kwargs)  # credentials stay out of Redis
        self.assertNotIn("postgresql", repr((queued.args, queued.kwargs)))
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
        now = self.get("/radars/escape/papers").json()["items"]
        self.assertTrue(now[0]["dismissed"])
        self.assertEqual((now[1]["saved"], now[1]["feedback"]), (True, ["save"]))
        lib = self.get("/library").json()
        self.assertEqual([(x["radar_id"], x["radar_title"], x["paper_id"]) for x in lib], [("escape", "Escape radar", saved)])
        self.assertTrue(lib[0]["paper"]["title"])

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
        self.assertEqual([x["paper_id"] for x in self.get("/library", radar_id="escape").json()], [saved])
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
        r = self.post("/radars", {"config": changed, "schedule": "weekly"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["config"]["mode"], r.json()["schedule"]), ("exploratory", "weekly"))
        self.assertEqual(self.post("/radars", {"config": RADAR, "schedule": "0 13 * * *"}).status_code, 422)
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


class ProposalTests(ApiTestCase):
    def test_propose_then_approve(self) -> None:
        r = self.post("/radars/proposal", {"topic": "Immune escape", "exclude": ["case report"], "window_days": 14})
        self.assertEqual(r.status_code, 200, r.text)
        prop = r.json()
        cfg = prop["config"]
        self.assertEqual((cfg["radar_id"], cfg["radar_name"], cfg["queries"]), ("immune-escape", "Immune escape", ["Immune escape"]))
        steps = {s["source"]: s for s in prop["strategy"]}
        self.assertEqual(steps["pubmed"]["query"], '(Immune escape) NOT ("case report"[Title/Abstract])')
        self.assertEqual(steps["openalex"]["query"], '("Immune escape") NOT ("case report")')
        self.assertIn("type:article", steps["openalex"]["filters"])
        self.assertEqual(list(steps), ["pubmed", "openalex", "europepmc", "crossref"])  # the defaults
        self.assertEqual((steps["crossref"]["status"], steps["crossref"]["query"]), ("ready", None))
        self.assertEqual(steps["europepmc"]["query"], '((TITLE_ABS:"Immune escape") NOT (TITLE_ABS:"case report")) AND '
                         f'(FIRST_PDATE:[{prop["window"]["start"]} TO {prop["window"]["end"]}]) AND (SRC:MED OR SRC:PMC OR SRC:PPR)')
        self.assertIn("retractions", steps["crossref"]["notes"][0])
        self.assertEqual(prop["window"]["days"], 14)
        self.assertTrue(any("method match is inactive" in n for n in prop["notes"]))
        self.assertEqual(self.get("/radars").json(), [])  # a proposal saves nothing

        created = self.post("/radars", {"config": cfg})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["config"], cfg)
        again = self.post("/radars/proposal", {"topic": "Immune escape"}).json()
        self.assertEqual(again["config"]["radar_id"], "immune-escape-2")

    def test_options_and_errors(self) -> None:
        r = self.post("/radars/proposal", {"topic": "tumour evolution", "terms": ["clonal evolution", "tumor phylogeny"],
                                           "methods": ["single-cell"], "sources": ["pubmed", "biorxiv"],
                                           "mode": "exploratory", "seed_papers": ["https://doi.org/10.1038/S41586-020-2008-3"]})
        self.assertEqual(r.status_code, 200, r.text)
        prop = r.json()
        self.assertEqual(prop["config"]["seed_papers"], ["10.1038/s41586-020-2008-3"])
        self.assertEqual(prop["strategy"][0]["query"], "((clonal evolution) OR (tumor phylogeny))")
        self.assertEqual(prop["strategy"][1]["status"], "not_available")
        self.assertEqual(prop["ranking"]["mode"], "exploratory")
        bad = self.post("/radars/proposal", {"topic": "x y", "seed_papers": ["not a doi"]})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("not a doi", bad.text)
        self.assertEqual(self.post("/radars/proposal", {"topic": "x y", "sources": ["scopus"]}).status_code, 422)
        self.assertEqual(self.post("/radars/proposal", {"topic": " "}).status_code, 422)
        only = self.post("/radars/proposal", {"topic": "x y", "sources": ["crossref"]})
        self.assertEqual(only.status_code, 422)  # Crossref verifies; something has to search
        self.assertIn("at least one source to search", only.text)


class ReplayTests(ApiTestCase):
    def test_worker_replays_recorded_http(self) -> None:
        import shutil

        from carousel.workers.scan import run_scan

        fixtures = Path(self.tmp.name) / "http"
        fixtures.mkdir()
        shutil.copy(REPO / "tests" / "fixtures" / "http" / "openalex_search.json", fixtures / "openalex.json")
        self.post("/radars", {"config": {**RADAR, "sources": ["openalex"]}})
        with mock.patch.dict(os.environ, {"CAROUSEL_HTTP_REPLAY": str(fixtures)}), \
                mock.patch("carousel.radar.CONNECTORS", {}):  # the default connectors aren't used
            out = run_scan("escape", database_url=URL, store_path=str(Path(self.tmp.name) / "replay.db"),
                           max_results=5, as_of=dt.date(2026, 10, 14))
        self.assertIn(out["status"], ("ok", "partial"))  # partial: max_results truncated the source
        self.assertGreater(out["n_items"], 0)
        cov = self.get("/radars/escape/papers").json()["run"]["coverage"]
        self.assertEqual([(c["source"], c["status"]) for c in cov], [("openalex", "ok")])


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


def iso(value: str) -> dt.datetime:
    """Parse an API timestamp; Python 3.10's fromisoformat doesn't accept a trailing Z."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class ScheduleTests(ApiTestCase):
    """Phase 2 task 15: scheduled scans, pause/resume that keeps history, and arrivals."""

    def patch(self, body):
        return self.client.patch("/api/v1/radars/escape", json=body, headers=self.auth)

    def sessions(self):
        from sqlalchemy.orm import sessionmaker

        return sessionmaker(self.app.state.engine)

    def tick(self, now=None):
        from carousel.workers import schedule
        from carousel.workers.scan import enqueue_scan

        def enqueue(queue, radar_id, **kw):  # the recorded fixtures answer only this date and size
            return enqueue_scan(queue, radar_id, max_results=SCAN["max_results"],
                                as_of=dt.date.fromisoformat(SCAN["as_of"]), **kw)
        return schedule.tick(self.sessions(), self.queue, now=now, enqueue=enqueue)

    def test_schedule_pause_resume(self) -> None:
        self.post("/radars", {"config": RADAR})
        radar = self.get("/radars/escape").json()
        self.assertEqual((radar["schedule"], radar["state"], radar["next_scan_at"]), (None, "active", None))
        self.assertEqual(self.tick(), [])  # manual radars are never scheduled

        r = self.patch({"schedule": "daily"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["schedule"], "daily")
        self.assertIsNotNone(r.json()["next_scan_at"])  # due now: no run yet

        jobs = self.tick()
        self.assertEqual([j.meta["radar_id"] for j in jobs], ["escape"])
        self.assertEqual(jobs[0].kwargs["trigger"], "scheduled")
        self.assertEqual(self.tick(), [])  # already queued
        self.work()
        runs = self.get("/radars/escape/runs").json()
        self.assertEqual([r["trigger"] for r in runs], ["scheduled"])
        self.assertEqual(self.tick(), [])  # this period has its run
        radar = self.get("/radars/escape").json()
        executed = iso(runs[0]["executed_at"])
        self.assertEqual(iso(radar["next_scan_at"]),
                         executed.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                         + dt.timedelta(days=1))

        tomorrow = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        self.assertEqual(self.patch({"state": "paused"}).json()["next_scan_at"], None)
        self.assertEqual(self.tick(tomorrow), [])  # paused: no scheduled scans
        self.assertEqual(self.scan_manual()["trigger"], "manual")  # manual scans still work while paused
        self.assertEqual(len(self.get("/radars/escape/runs").json()), 2)  # history kept

        r = self.patch({"state": "active"})
        self.assertEqual((r.json()["state"], r.json()["schedule"]), ("active", "daily"))  # schedule kept
        self.assertEqual(len(self.tick(tomorrow)), 1)
        self.assertEqual(self.tick(tomorrow), [])
        self.redis.flushdb()  # drop the queued scan (fakeredis has no Lua for queue.empty())

        # Weekly, and back to manual; bad values are rejected.
        self.assertEqual(self.patch({"schedule": "weekly"}).json()["schedule"], "weekly")
        self.assertEqual(self.patch({"schedule": None}).json()["schedule"], None)
        self.assertEqual(self.patch({"schedule": "0 13 * * *"}).status_code, 422)
        self.assertEqual(self.patch({"state": "archived"}).status_code, 422)
        self.assertEqual(self.client.patch("/api/v1/radars/nope", json={"state": "paused"}, headers=self.auth).status_code, 404)

    def scan_manual(self):
        r = self.post("/radars/escape/runs", SCAN)
        self.assertEqual(r.status_code, 202, r.text)
        self.work()
        return self.get("/radars/escape/runs").json()[0]

    def test_lock_stops_a_second_scheduler(self) -> None:
        from carousel.workers import schedule

        self.post("/radars", {"config": RADAR, "schedule": "weekly"})
        enqueued = []
        def fake(queue, radar_id, **kw):  # enqueue nothing, so the queue check can't help
            enqueued.append(radar_id)
            return mock.Mock(id="job_x")
        schedule.tick(self.sessions(), self.queue, enqueue=fake)
        schedule.tick(self.sessions(), self.queue, enqueue=fake)
        self.assertEqual(enqueued, ["escape"])

    def test_arrivals(self) -> None:
        self.post("/radars", {"config": RADAR})
        run1 = self.scan_manual()["run_id"]
        first = self.get("/radars/escape/papers").json()
        self.assertEqual((first["previous_run_id"], first["arrivals"]), (None, None))
        self.assertEqual({i["arrival"] for i in first["items"]}, {None})

        run2 = self.scan_manual()["run_id"]
        second = self.get("/radars/escape/papers").json()
        self.assertEqual(second["previous_run_id"], run1)
        self.assertEqual({i["arrival"] for i in second["items"]}, {"seen_before"})  # same fixtures: nothing new

        # Make two of run 2's papers unknown to run 1: one published after run 1's as_of, one before.
        new, old = second["items"][0]["paper_id"], second["items"][1]["paper_id"]
        as_of = dt.date.fromisoformat(SCAN["as_of"])
        with self.app.state.engine.begin() as c:
            for pid in (new, old):
                c.execute(text("DELETE FROM run_observations WHERE run_id = :r AND paper_id = :p"), {"r": run1, "p": pid})
                c.execute(text("DELETE FROM recommendation_items WHERE run_id = :r AND paper_id = :p"), {"r": run1, "p": pid})
            c.execute(text("UPDATE papers SET earliest_public_date = :d WHERE id = :p"), {"d": as_of + dt.timedelta(days=1), "p": new})
            c.execute(text("UPDATE papers SET earliest_public_date = :d WHERE id = :p"), {"d": as_of - dt.timedelta(days=30), "p": old})
        second = self.get("/radars/escape/papers").json()
        by_id = {i["paper_id"]: i["arrival"] for i in second["items"]}
        self.assertEqual((by_id[new], by_id[old]), ("newly_published", "newly_observed"))
        self.assertEqual(second["arrivals"], {"newly_published": 1, "newly_observed": 1,
                                              "seen_before": len(second["items"]) - 2})
        delta = self.get("/radars/escape/delta").json()
        self.assertEqual((delta["run_id"], delta["newly_published"], delta["newly_observed"]), (run2, [new], [old]))
        # Viewing run 1 later still compares it with nothing before it.
        self.assertIsNone(self.get("/radars/escape/papers", run_id=run1).json()["arrivals"])



class CrossrefUpdatesTests(ApiTestCase):
    """Plan task 16: Crossref's updates and relations reach the paper API through the Postgres import."""

    def test_retraction_and_relations_on_paper_detail(self) -> None:
        import sys as _sys

        from carousel.connectors import CrossrefConnector
        from carousel.core.paper import SourceRecord
        from carousel.database import PaperStore
        from carousel.database.importer import import_store
        from test_connectors import FIXTURES, replay

        _sys.path.insert(0, str(FIXTURES))
        from record_fixtures import KNOWN_DOIS

        store = Path(self.tmp.name) / "engine.db"
        t = dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc)
        with PaperStore(store) as st:
            pid = st.observe(SourceRecord(source="pubmed", source_id="9500106", title="Ileal-lymphoid-nodular hyperplasia",
                                          identifiers={"doi": "10.1016/S0140-6736(97)11096-0"}), t).paper.paper_id
            for rec in CrossrefConnector(replay("crossref_lookup.json"), mailto="").fetch(KNOWN_DOIS):
                st.observe(rec, t)
            article = st.find("doi", "10.1021/acsomega.5c02989").paper_id
        import_store(store, self.app.state.engine)

        paper = self.get(f"/papers/{pid}").json()
        self.assertTrue(paper["is_retracted"])
        self.assertEqual([(u["type"], u["asserted_by"], u["via"]) for u in paper["updates"]],
                         [("correction", "retraction-watch", "crossref"), ("retraction", "retraction-watch", "crossref")])
        self.assertEqual([(r["type"], r["value"]) for r in self.get(f"/papers/{article}").json()["relations"]],
                         [("has-preprint", "10.26434/chemrxiv-2025-8hnzz")])


class EuropePMCTextsTests(ApiTestCase):
    """Plan task 17: texts (with their licence) and annotations reach the paper API through the Postgres import."""

    def test_licence_per_text_and_annotations(self) -> None:
        import sys as _sys

        from carousel.connectors import EuropePMCConnector
        from carousel.core.paper import SourceRecord
        from carousel.database import PaperStore
        from carousel.database.importer import import_store
        from test_connectors import FIXTURES, replay

        _sys.path.insert(0, str(FIXTURES))
        from record_fixtures import EPMC_ANNOTATED, EPMC_FULL_TEXTS

        ep = EuropePMCConnector(replay("europepmc_extras.json"))
        store = Path(self.tmp.name) / "engine.db"
        t = dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc)
        with PaperStore(store) as st:
            pid = st.observe(SourceRecord(source="europepmc", source_id="MED:43054306", title="Within-host dynamics",
                                          identifiers={"pmid": "43054306", "pmcid": "PMC13778503"}, license="cc-by",
                                          open_access=True), t).paper.paper_id
            st.save_text(pid, ep.full_text(EPMC_FULL_TEXTS[0]))
            found = ep.annotations(EPMC_ANNOTATED)
            st.save_annotations(pid, "europepmc", "MED:43054306", found["MED:43054306"], t)
        counts = import_store(store, self.app.state.engine)
        self.assertEqual((counts["paper_texts"], counts["paper_annotations"]), (1, 1))

        paper = self.get(f"/papers/{pid}").json()
        self.assertEqual((paper["license"], paper["open_access"]), ("cc-by", True))
        [text] = paper["texts"]
        self.assertEqual((text["source_id"], text["license"], text["license_source"], text["reuse"]),
                         ("PMC13778503", "cc-by-4.0", "text", "open"))
        self.assertEqual(text["sections"][0], "Introduction")
        self.assertGreater(text["characters"], 10_000)
        self.assertNotIn("text", text)  # the text itself is not served
        self.assertEqual(len(paper["annotations"]), 32)
        self.assertEqual(paper["annotations"][0]["name"], "severe acute respiratory syndrome coronavirus 2")


class VersionLinksTests(ApiTestCase):
    """Plan task 18: a preprint and its publication stay two papers, and each shows the other."""

    def test_linked_versions_show_each_other(self) -> None:
        import sys as _sys

        from carousel.connectors import EuropePMCConnector
        from carousel.database import PaperStore
        from carousel.database.importer import import_store
        from carousel.ingestion.versions import link_versions
        from test_connectors import FIXTURES, replay

        _sys.path.insert(0, str(FIXTURES))
        from record_fixtures import EPMC_ARTICLES, EPMC_PREPRINTS

        store = Path(self.tmp.name) / "engine.db"
        t = dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc)
        with PaperStore(store) as st:
            ids = {r.source_id: st.observe(r, t).paper.paper_id
                   for r in EuropePMCConnector(replay("versions.json")).fetch(EPMC_ARTICLES + EPMC_PREPRINTS)}
            link_versions(st, ids.values(), t)
        counts = import_store(store, self.app.state.engine)
        self.assertEqual(counts["paper_versions"], 4)  # two links, one row per direction
        counts = import_store(store, self.app.state.engine)  # re-import replaces, never duplicates
        self.assertEqual(counts["paper_versions"], 4)

        art, pre = ids["MED:42949957"], ids["PPR:PPR1222416"]
        a, p = self.get(f"/papers/{art}").json(), self.get(f"/papers/{pre}").json()
        self.assertNotEqual(a["paper_id"], p["paper_id"])
        [down], [up] = a["versions"], p["versions"]
        self.assertEqual((down["paper_id"], down["role"], down["linked_by"], down["earliest_public_date"]),
                         (pre, "preprint", ["europepmc"], "2026-02-27"))
        self.assertEqual((up["paper_id"], up["role"], up["doi"]), (art, "published", "10.1073/pnas.2608185123"))
        self.assertEqual(len(down["evidence"]), 2)  # asserted on both records
        self.assertIn("Antigenic landscape", up["title"])
