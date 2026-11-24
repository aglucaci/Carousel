"""Topic events (plan task 28): derived from stored observations and versions only.

Done when: a test deletes stored topic events and recomputes them identically; every event links at least one
traceable publication. Scenario: three topics of known papers (dated), a rising one in the counts, a method term
that appears in one topic's recent papers only, and a refit that adds a fourth topic.
"""

import datetime as dt
import random
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from carousel.core.config import RadarConfig
from carousel.core.paper import SourceRecord
from carousel.database import PaperStore
from carousel.intelligence.observations import observe
from carousel.intelligence.topic_events import derive_topic_events
from carousel.intelligence.topics import fit_radar
from carousel.search.embeddings import HashingEmbedder
from test_observations import Scripted
from test_topics import GROUPS

UTC = dt.timezone.utc
EMB = HashingEmbedder()
AS_OF = dt.datetime(2026, 12, 22, 9, tzinfo=UTC)  # a Tuesday, long after the observed weeks
RADAR = RadarConfig.model_validate({
    "radar_id": "lab", "radar_name": "Lab", "queries": ["evolution"], "sources": ["pubmed"],
    "methods_profile": ["cryo-em"], "filters": {"date_range": "last_90_days"}})


def build(d: Path):
    st = PaperStore(d / "s.db")
    rng = random.Random(5)
    t0 = dt.datetime(2026, 9, 1, tzinfo=UTC)
    obs = []
    for g in ("flu", "crispr", "coral"):
        words = GROUPS[g].split()
        for i in range(14):
            recent = g == "crispr" and i >= 9  # five crispr papers 25-29 Nov (in the trend's and the connection's
            # windows as of AS_OF), three of them using cryo-EM
            date = (dt.date(2026, 11, 25) + dt.timedelta(days=i - 9)) if recent else (dt.date(2026, 9, 1) + dt.timedelta(days=3 * i))
            extra = " cryo-em structure" if recent and i < 12 else ""
            rec = SourceRecord(source="pubmed", source_id=f"{g}{i}", title=" ".join(rng.sample(words, 6)),
                               abstract=" ".join(rng.choice(words) for _ in range(30)) + extra,
                               dates={"published": date.isoformat()})
            pid = st.observe(rec, t0).paper.paper_id
            obs.append(("pubmed", rec.source_id, pid, "new_paper"))
    run = {"run_id": "run_1", "radar_id": "lab", "radar_name": "Lab", "config_hash": "x", "as_of": "2026-12-20",
           "started_at": "2026-12-21T00:00:00.000000Z", "finished_at": "2026-12-21T00:00:00.000000Z", "status": "ok",
           "ranking_version": "0", "candidates_version": "0", "model_version": None, "mode": "balanced",
           "n_observed": len(obs), "n_candidates": 0, "coverage": [], "ranking": {}}
    st.save_run(run, [], obs)
    model, _ = fit_radar(st, "lab", EMB, "2026-12-01T00:00:00Z")
    crispr = model.members[st.find_source("pubmed", "crispr0").paper_id]
    first_term = {t.topic_id: t.key_terms[0] for t in model.topics}[crispr]
    if np is not None:
        rising = {dt.date(2026, 11, 16) - dt.timedelta(days=7 * i) for i in range(4)}
        nrng, memo = np.random.default_rng(3), {}

        def counts(require, start):
            if (require, start) not in memo:
                rate = 0.05 * (2.5 if require and require[0] == first_term and start in rising else 1.0)
                memo[(require, start)] = 300 if not require else int(nrng.poisson(300 * rate))
            return memo[(require, start)]
        observe(st, RADAR, model, {"pubmed": Scripted(counts)}, at=dt.datetime(2026, 11, 23, 12, tzinfo=UTC), weeks=16,
                today=dt.date(2026, 11, 23))
    return st, model, crispr


class TopicEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.st, self.model, self.crispr = build(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.st.close()
        self.tmp.cleanup()

    def test_connection_and_acceleration(self) -> None:
        out = derive_topic_events(self.st, RADAR, AS_OF, "run_1")
        kinds = {(e["type"], e["kind"]) for e in out["events"]}
        self.assertIn(("new_methodological_connection", "cryo-em"), kinds)
        conn = next(e for e in out["events"] if e["type"] == "new_methodological_connection")
        self.assertEqual(conn["evidence"]["topic_id"], self.crispr)
        self.assertEqual((conn["evidence"]["recent_papers_with_term"], conn["evidence"]["earlier_papers_without_term"]), (3, 9))
        self.assertEqual(len(conn["evidence"]["papers"]), 3)
        if np is not None:
            [acc] = [e for e in out["events"] if e["type"] == "topic_acceleration"]
            ev = acc["evidence"]
            self.assertEqual((ev["source"], ev["evaluated_through"], ev["papers_in_window"]), ("pubmed", "2026-11-29", 5))
            self.assertGreater(ev["rate_ratio"], 1.5)
            self.assertTrue(all(c["status"] != "fired" for c in ev["checks"]))
            self.assertEqual(acc["title"], next(t.label for t in self.model.topics if t.topic_id == self.crispr))
        for e in out["events"]:
            self.assertTrue(e["evidence"]["papers"])  # every event links at least one traceable publication
            self.assertEqual(e["paper_id"], e["evidence"]["papers"][0]["paper_id"])
            self.assertTrue(all(self.st.get(p["paper_id"]) for p in e["evidence"]["papers"]))

    def test_landscape_shift_after_a_refit(self) -> None:
        rng = random.Random(9)
        words = GROUPS["malaria"].split()
        obs = []
        for i in range(14):
            rec = SourceRecord(source="pubmed", source_id=f"malaria{i}", title=" ".join(rng.sample(words, 6)),
                               abstract=" ".join(rng.choice(words) for _ in range(30)), dates={"published": "2026-10-15"})
            obs.append(("pubmed", rec.source_id, self.st.observe(rec, AS_OF).paper.paper_id, "new_paper"))
        run = {"run_id": "run_2", "radar_id": "lab", "radar_name": "Lab", "config_hash": "x", "as_of": "2026-12-21",
               "started_at": "2026-12-22T00:00:00.000000Z", "finished_at": "2026-12-22T00:00:00.000000Z", "status": "ok",
               "ranking_version": "0", "candidates_version": "0", "model_version": None, "mode": "balanced",
               "n_observed": len(obs), "n_candidates": 0, "coverage": [], "ranking": {}}
        self.st.save_run(run, [], obs)
        new, changed = fit_radar(self.st, "lab", EMB, "2026-12-22T00:00:00Z")
        self.assertTrue(changed)
        out = derive_topic_events(self.st, RADAR, AS_OF, "run_2")
        [shift] = [e for e in out["events"] if e["type"] == "research_landscape_shift"]
        self.assertEqual((shift["kind"], shift["date"]), ("new", "2026-12-22"))
        self.assertEqual(shift["evidence"]["from_version"], self.model.classifier_version)
        self.assertEqual(len(shift["evidence"]["papers"]), 10)  # the new topic's papers (capped)
        self.assertTrue(all(p["paper_id"] in {o[2] for o in obs} for p in shift["evidence"]["papers"]))

    def test_reproducible_from_stored_data(self) -> None:
        first = derive_topic_events(self.st, RADAR, AS_OF, "run_1")["events"]
        self.st.save_events(first)
        stored = [e for e in self.st.events(radar_id="lab") if e["type"] in ("topic_acceleration",
                                                                            "new_methodological_connection")]
        self.st.db.execute("DELETE FROM research_events")
        again = derive_topic_events(self.st, RADAR, AS_OF, "run_1")["events"]
        key = lambda e: e["event_id"]  # noqa: E731
        self.assertEqual(sorted(stored, key=key), sorted(({k: v for k, v in e.items()} for e in again), key=key))
        # Recomputed later, a finding that still holds keeps its ID (so repeated scans don't repeat it); the
        # connection ages out of the 28-day window a day later, and the stored event stays stored.
        later = derive_topic_events(self.st, RADAR, AS_OF + dt.timedelta(days=1), "run_1")["events"]
        self.assertTrue({e["event_id"] for e in later} <= {e["event_id"] for e in first})
        if np is not None:
            self.assertEqual([e["event_id"] for e in later if e["type"] == "topic_acceleration"],
                             [e["event_id"] for e in first if e["type"] == "topic_acceleration"])


try:
    from test_api import ApiTestCase
except Exception:  # pragma: no cover - server extras missing
    ApiTestCase = None

if ApiTestCase is not None:
    class TopicEventApiTests(ApiTestCase):
        def test_topic_events_reach_the_api(self) -> None:
            from carousel.database.importer import import_store

            st, model, crispr = build(Path(self.tmp.name))
            with st:
                events = derive_topic_events(st, RADAR, AS_OF, "")["events"]  # no run: as from the CLI
                st.save_events(events)
            import_store(Path(self.tmp.name) / "s.db", self.app.state.engine)
            got = self.get("/radars/lab/events").json()
            types = {e["type"] for e in got}
            self.assertIn("new_methodological_connection", types)
            conn = next(e for e in got if e["type"] == "new_methodological_connection")
            self.assertEqual((conn["kind"], conn["run_id"]), ("cryo-em", None))
            self.assertTrue(conn["evidence"]["papers"] and conn["paper_id"])


if __name__ == "__main__":
    unittest.main()
