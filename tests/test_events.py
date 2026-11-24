"""First Research Delta events: new publication and publication revision (plan task 21).

Done when: events are reproducible from stored runs and link their evidence.
A scripted source returns chosen records on each scan (no network).
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from carousel import Radar
from carousel import feedback as fb
from carousel.connectors.base import QueryCoverage, SearchResult
from carousel.core.config import RadarConfig
from carousel.core.paper import SourceRecord
from carousel.database import PaperStore
from carousel.intelligence import derive_events, events_for_run

UTC = dt.timezone.utc
CFG = RadarConfig.model_validate({
    "radar_id": "drift", "radar_name": "Drift", "sources": ["pubmed"], "queries": ["antigenic drift"],
    "filters": {"date_range": "last_30_days"}})


def rec(pmid, title, published, **kw):
    return SourceRecord(source="pubmed", source_id=pmid, identifiers={"pmid": pmid, **kw.pop("ids", {})},
                        title=f"Antigenic drift {title}", abstract="Antigenic drift of influenza hemagglutinin.",
                        authors=[{"name": f"Author {pmid}"}], dates={"published": published}, **kw)


class Scripted:
    """Returns the records set for the next scan."""

    def __init__(self):
        self.records = []

    def search(self, query):
        cov = QueryCoverage(source="pubmed", connector_version="test", native_query="antigenic drift",
                            date_field="publication date", start=query.start, end=query.end,
                            total_available=len(self.records), retrieved=len(self.records), truncated=False, pages=1,
                            requests=1, cache_hits=0, cost=1, cost_unit="requests", retrieved_at=dt.datetime.now(UTC))
        return SearchResult(records=list(self.records), coverage=cov)


A = rec("101", "in H3N2 clade 2a", "2026-11-01")
B = rec("102", "and vaccine mismatch", "2026-10-25")
C = rec("103", "in H1N1 after the 2026 season", "2026-11-15")
CORRECTED_A = rec("101", "in H3N2 clade 2a", "2026-11-01",
                  updates=[{"type": "correction", "notice_doi": "10.5555/corr-a", "asserted_by": "publisher"}])
RETRACTED_B = rec("102", "and vaccine mismatch", "2026-10-25", is_retracted=True,
                  updates=[{"type": "retraction", "notice_doi": "10.5555/retr-b", "asserted_by": "retraction-watch"}])
PREPRINT = SourceRecord(source="europepmc", source_id="PPR:PPR9", title="Antigenic drift in H1N1 after the 2026 season (preprint)",
                        dates={"preprint_posted": "2026-08-01"})
C_WITH_PREPRINT = rec("103", "in H1N1 after the 2026 season", "2026-11-15",
                      relations=[{"type": "has-preprint", "scheme": "europepmc", "value": "PPR:PPR9"}])


class EventScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.db = d / "s.db"
        feedback = d / "drift.feedback.yml"
        src = Scripted()
        radar = Radar(CFG, store=cls.db, feedback_path=feedback)

        def scan(day, records, as_of):
            src.records = records
            with PaperStore(cls.db) as st:
                for r in records:  # a record of another source (the preprint) is observed like a lookup would
                    if r.source.value != "pubmed":
                        st.observe(r, dt.datetime(2026, 11, day, 6, tzinfo=UTC))
            return radar.scan(as_of=as_of, observed_at=dt.datetime(2026, 11, day, 6, tzinfo=UTC),
                              connectors={"pubmed": src}, embedder="hashing", top_n=10)

        cls.r1 = scan(10, [A, B], dt.date(2026, 11, 10))                       # baseline
        cls.r2 = scan(20, [CORRECTED_A, B, C], dt.date(2026, 11, 20))           # C is new; A corrected
        fb.save(feedback, {**{a: [] for a in fb.ACTIONS}, "save": [cls.r2.items[0]["paper_id"]]}, "drift")
        cls.r3 = scan(27, [CORRECTED_A, RETRACTED_B, C_WITH_PREPRINT, PREPRINT], dt.date(2026, 11, 27))  # B retracted; C linked
        with PaperStore(cls.db) as st:
            cls.ids = {r.source_id: st.find_source(r.source.value, r.source_id).paper_id for r in (A, B, C, PREPRINT)}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def events(self, run):
        with PaperStore(self.db) as st:
            return st.events(run_id=run.run_id)

    def test_the_first_run_is_the_baseline(self) -> None:
        self.assertEqual(self.events(self.r1), [])

    def test_new_publication_and_correction(self) -> None:
        got = {(e["type"], e["kind"], e["paper_id"]) for e in self.events(self.r2)}
        self.assertEqual(got, {("new_publication", None, self.ids["103"]),
                               ("publication_revision", "correction", self.ids["101"])})
        [new] = [e for e in self.events(self.r2) if e["type"] == "new_publication"]
        ev = new["evidence"]
        self.assertEqual((ev["run_id"], ev["previous_run_id"], ev["earliest_public_date"]),
                         (self.r2.run_id, self.r1.run_id, "2026-11-15"))
        self.assertEqual(ev["window"], {"start": "2026-10-22", "end": "2026-11-20"})
        self.assertEqual(new["date"], "2026-11-20")
        [corr] = [e for e in self.events(self.r2) if e["kind"] == "correction"]
        self.assertEqual(corr["evidence"]["notice"]["notice_doi"], "10.5555/corr-a")

    def test_retraction_of_a_watched_paper_and_a_new_version(self) -> None:
        got = {(e["type"], e["kind"], e["paper_id"]) for e in self.events(self.r3)}
        self.assertEqual(got, {("publication_revision", "retraction", self.ids["102"]),  # not recommended, still watched
                               ("publication_revision", "new_version", self.ids["103"])})
        self.assertNotIn(self.ids["102"], [it["paper_id"] for it in self.r3.items])  # retracted: not recommended
        [retr] = [e for e in self.events(self.r3) if e["kind"] == "retraction"]
        self.assertEqual(retr["evidence"]["notice"]["asserted_by"], "retraction-watch")
        self.assertEqual(retr["evidence"]["retracted"], {"before": False, "now": True})
        [ver] = [e for e in self.events(self.r3) if e["kind"] == "new_version"]
        self.assertEqual((ver["evidence"]["version"]["paper_id"], ver["evidence"]["version"]["role"]),
                         (self.ids["PPR:PPR9"], "preprint"))
        self.assertFalse([e for e in self.events(self.r3) if e["type"] == "new_publication"])  # nothing unseen
        self.assertFalse([e for e in self.events(self.r3) if e["kind"] == "correction"])  # already reported

    def test_events_are_reproducible_from_stored_runs(self) -> None:
        with PaperStore(self.db) as st:
            stored = st.events(radar_id="drift")
            st.db.execute("DELETE FROM research_events")
            again = [e for r in st.list_runs("drift") for e in events_for_run(st, r["run_id"])]
        key = lambda e: e["event_id"]  # noqa: E731
        strip = lambda e: {k: v for k, v in e.items() if k != "title"}  # noqa: E731
        self.assertEqual(sorted(map(strip, stored), key=key),
                         sorted((strip({**e, "kind": e["kind"]}) for e in again), key=key))
        self.assertEqual(len(stored), 4)
        self.assertEqual(len({e["event_id"] for e in stored}), 4)

    def test_digest_reports_the_changes(self) -> None:
        from carousel.cli import _store_digest
        from carousel.notifications import to_markdown

        with PaperStore(self.db) as st:
            md = to_markdown(_store_digest(st, st.get_run(self.r3.run_id), "Drift"))
            md2 = to_markdown(_store_digest(st, st.get_run(self.r2.run_id), "Drift"))
        self.assertIn("## Changes to papers you've seen", md)
        self.assertIn("- Retracted: Antigenic drift and vaccine mismatch (notice https://doi.org/10.5555/retr-b, "
                      "reported by Retraction Watch)", md)
        self.assertIn("New version linked: Antigenic drift in H1N1", md)
        self.assertIn("New publications (never seen by this radar, published in the window): 1.", md2)


try:
    from test_api import ApiTestCase
except Exception:  # pragma: no cover - server extras missing
    ApiTestCase = None

if ApiTestCase is not None:
    class EventApiTests(ApiTestCase):
        def test_events_reach_the_api_with_their_evidence(self) -> None:
            from carousel.database.importer import import_store

            EventScenarioTests.setUpClass()
            try:
                counts = import_store(EventScenarioTests.db, self.app.state.engine)
                self.assertEqual(counts["research_events"], 4)
                self.assertEqual(import_store(EventScenarioTests.db, self.app.state.engine)["research_events"], 4)
                events = self.get("/radars/drift/events").json()
                self.assertEqual([(e["date"], e["type"], e["kind"]) for e in events], [
                    ("2026-11-27", "publication_revision", "new_version"),
                    ("2026-11-27", "publication_revision", "retraction"),
                    ("2026-11-20", "new_publication", None),
                    ("2026-11-20", "publication_revision", "correction")])
                retr = events[1]
                self.assertEqual((retr["title"], retr["is_retracted"]), ("Antigenic drift and vaccine mismatch", True))
                self.assertEqual(retr["evidence"]["notice"]["notice_doi"], "10.5555/retr-b")
                r3 = EventScenarioTests.r3.run_id
                self.assertEqual(len(self.get("/radars/drift/events", run_id=r3).json()), 2)
                self.assertEqual(self.get("/radars/drift/papers", run_id=r3).json()["events"],
                                 {"new_version": 1, "retraction": 1})
                self.assertEqual(self.get("/radars/nope/events").status_code, 404)
            finally:
                EventScenarioTests.tearDownClass()


class DeriveTests(unittest.TestCase):
    RUN0 = {"run_id": "r0", "radar_id": "x", "started_at": "2026-11-01T00:00:00.000000Z", "as_of": "2026-11-01",
            "items": [{"paper_id": "p_old", "rank": 1}], "observations": [("pubmed", "9", "p_seen", "new_paper")]}

    def make_run(self, items):
        return {"run_id": "r1", "radar_id": "x", "started_at": "2026-11-08T00:00:00.000000Z", "as_of": "2026-11-08",
                "ranking": {"window": {"start": "2026-10-10", "end": "2026-11-08"}}, "items": items}

    def item(self, pid, date):
        return {"paper_id": pid, "rank": 1, "details": {"paper": {"earliest_public_date": date}}}

    def test_new_means_newly_observed_and_recently_published(self) -> None:
        run = self.make_run([self.item("p_new", "2026-11-02"), self.item("p_late", "2025-03-01"),
                        self.item("p_seen", "2026-11-03"), self.item("p_month", "2026-10")])
        got = [e["paper_id"] for e in derive_events(run, [self.RUN0], {}, {})]
        self.assertEqual(got, ["p_new", "p_month"])  # indexed late, or seen before: not new publications

    def test_no_baseline_no_events(self) -> None:
        self.assertEqual(derive_events(self.make_run([self.item("p_new", "2026-11-02")]), [], {}, None), [])
        states = {"p": {"notices": [{"type": "correction", "notice_doi": "10.1/x"}], "versions": []}}
        self.assertEqual(derive_events(self.make_run([]), [self.RUN0], states, {}), [])  # not watched before


if __name__ == "__main__":
    unittest.main()
