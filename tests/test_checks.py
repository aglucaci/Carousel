"""Artifact checks (plan task 27): each check has a fixture where it fires, and the trend it affects is withheld or
marked, naming the check."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import numpy as np

    from carousel.intelligence.checks import checked_trends, settled_through, settling_days
except ImportError:  # pragma: no cover
    np = None

from carousel.core.config import RadarConfig
from carousel.database import PaperStore
from carousel.intelligence.observations import observe
from test_observations import OBS_RADAR, Scripted

UTC = dt.timezone.utc
TODAY = dt.date(2026, 11, 23)
RISING = {dt.date(2026, 11, 16) - dt.timedelta(days=7 * i) for i in range(4)}
LATE = dt.datetime(2026, 12, 21, tzinfo=UTC)  # long after: every week has settled
RADAR = RadarConfig.model_validate({**OBS_RADAR, "sources": ["pubmed"]})


def model(version="topics-1:t"):
    return SimpleNamespace(classifier_version=version, topics=[
        SimpleNamespace(topic_id="tp_rising", key_terms=["rising"]),
        *[SimpleNamespace(topic_id=f"tp_flat{i}", key_terms=[f"flat{i}"]) for i in range(4)]])


def counts(seed=1, field=lambda start: 300, rate_up=2.5):
    rng, memo = np.random.default_rng(seed), {}

    def f(require, start):
        if (require, start) not in memo:
            rate = 0.05 * (rate_up if require and require[0] == "rising" and start in RISING else 1.0)
            memo[(require, start)] = field(start) if not require else int(rng.poisson(field(start) * rate))
        return memo[(require, start)]
    return f


@unittest.skipIf(np is None, 'needs pip install "carousel-research[trends]"')
class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.st = PaperStore(Path(self.tmp.name) / "s.db")

    def tearDown(self) -> None:
        self.st.close()
        self.tmp.cleanup()

    def obs(self, fn, at=dt.datetime(2026, 11, 23, 12, tzinfo=UTC), m=None, weeks=16, today=TODAY):
        observe(self.st, RADAR, m or model(), {"pubmed": Scripted(fn)}, at=at, weeks=weeks, today=today)

    def report(self, as_of=LATE, version="topics-1:t", **kw):
        rows = self.st.topic_observations("escape", version)
        return checked_trends(rows, [t.topic_id for t in model().topics], "pubmed", as_of, classifier_version=version,
                              all_rows=self.st.topic_observations("escape"), **kw)

    def topic(self, rep, tid="tp_rising"):
        return next(t for t in rep["topics"] if t["topic_id"] == tid)

    def test_clean_rise_passes_every_check(self) -> None:
        self.obs(counts())
        rep = self.report()
        self.assertTrue(self.topic(rep)["flagged"])
        self.assertEqual({c["check"]: c["status"] for c in rep["checks"]},
                         {"indexing_lag": "pass", "denominator_shift": "pass", "classifier_change": "pass"})
        self.assertEqual(rep["provisional_weeks"], [])

    def test_indexing_lag_holds_back_recent_weeks(self) -> None:
        self.obs(counts())
        rep = self.report(as_of=dt.datetime(2026, 11, 24, tzinfo=UTC))  # a day after the counts: not settled yet
        lag = next(c for c in rep["checks"] if c["check"] == "indexing_lag")
        self.assertEqual((lag["settling_days"], lag["basis"]), (7, "default (too few re-observed weeks)"))
        self.assertEqual(rep["evaluated_through"], "2026-11-15")
        self.assertEqual(rep["provisional_weeks"], ["2026-11-16"])
        rising = self.topic(rep)
        self.assertEqual(rising["weekly"][-1]["week"], "2026-11-16")
        self.assertTrue(rising["weekly"][-1]["provisional"])  # shown, but not part of the evaluation

    def test_settling_time_is_measured_from_reobservations(self) -> None:
        def growing(observed_week):  # a week's field count is 60% complete a day after it ends, full a week later
            def f(require, start):
                end = start + dt.timedelta(days=6)
                full = 300 if not require else 15
                return int(full * (0.6 if (observed_week - end).days <= 1 else 1.0))
            return f
        for k in range(4):
            day = TODAY + dt.timedelta(days=7 * k)
            self.obs(growing(day), at=dt.datetime.combine(day, dt.time(12), UTC), weeks=4, today=day)
        days, basis = settling_days(self.st.topic_observations("escape"), "pubmed")
        self.assertEqual((days, basis), (8, "measured from 3 re-observed weeks"))
        self.assertEqual(settled_through(dt.date(2026, 12, 21), days), dt.date(2026, 12, 13))

    def test_denominator_shift_withholds_flags(self) -> None:
        jump = {dt.date(2026, 11, 16) - dt.timedelta(days=7 * i) for i in range(4)}
        self.obs(counts(field=lambda start: 700 if start in jump else 300))
        rep = self.report()
        shift = next(c for c in rep["checks"] if c["check"] == "denominator_shift")
        self.assertEqual(shift["status"], "fired")
        self.assertGreater(shift["ratio"], 1.5)
        t = self.topic(rep)
        self.assertFalse(t["flagged"])
        self.assertIn("denominator_shift", t["failed"])

    def test_classifier_change_is_named(self) -> None:
        self.obs(counts(), m=model("topics-1:old"))
        self.obs(counts(), m=model("topics-1:t"), weeks=3)  # a refit: the new version has three weeks
        rep = self.report()
        change = next(c for c in rep["checks"] if c["check"] == "classifier_change")
        self.assertEqual((change["status"], change["other_versions"]), ("fired", ["topics-1:old"]))
        self.assertEqual(self.topic(rep)["status"], "insufficient_history")  # never mixed with the old version's counts

    def test_duplicate_surge_withholds_a_flag(self) -> None:
        self.obs(counts())

        def papers(topic_id, start, end):  # ten records, but only three works (preprints and their articles)
            return [(f"p{i}", f"work{i % 3}") for i in range(10)] if topic_id == "tp_rising" else []

        rep = self.report(topic_papers=papers)
        t = self.topic(rep)
        self.assertFalse(t["flagged"])
        dup = next(c for c in t["checks"] if c["check"] == "duplicate_surge")
        self.assertEqual((dup["status"], dup["records"], dup["works"]), ("fired", 10, 3))
        clean = self.report(topic_papers=lambda tid, s, e: [(f"p{i}", f"w{i}") for i in range(10)])
        self.assertTrue(self.topic(clean)["flagged"])


class ScheduleTests(unittest.TestCase):
    def test_weeks_to_observe(self) -> None:
        from carousel.workers.observe import weeks_to_observe

        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            self.assertEqual(weeks_to_observe(st, "escape", "v", TODAY), 16)
            observe(st, RADAR, model("v"), {"pubmed": Scripted(lambda r, s: 1)},
                    at=dt.datetime(2026, 11, 23, tzinfo=UTC), weeks=16, today=TODAY)
            self.assertEqual(weeks_to_observe(st, "escape", "v", TODAY), 6)  # nothing new: re-observe the last six
            self.assertEqual(weeks_to_observe(st, "escape", "v", TODAY + dt.timedelta(days=14)), 8)


try:
    from test_api import RADAR as API_RADAR, ApiTestCase
except Exception:  # pragma: no cover - server extras missing
    ApiTestCase = None

if ApiTestCase is not None:
    class ObserveTickTests(ApiTestCase):
        def test_weekly_observation_is_enqueued_once_for_radars_with_topics(self) -> None:
            from sqlalchemy.orm import Session

            from carousel.database.importer import import_store
            from carousel.intelligence.topics import Topic, TopicModel
            from carousel.workers.schedule import tick_observations

            self.post("/radars", {"config": API_RADAR})
            self.post("/radars", {"config": {**API_RADAR, "radar_id": "other", "radar_name": "Other"}})
            store = Path(self.tmp.name) / "engine.db"
            with PaperStore(store) as st:
                run = {"run_id": "run_1", "radar_id": "escape", "radar_name": "Escape radar", "config_hash": "x",
                       "as_of": "2026-11-22", "started_at": "2026-11-23T00:00:00.000000Z",
                       "finished_at": "2026-11-23T00:00:00.000000Z", "status": "ok", "ranking_version": "0",
                       "candidates_version": "0", "model_version": None, "mode": "balanced", "n_observed": 0,
                       "n_candidates": 0, "coverage": [], "ranking": {}}
                st.save_run(run, [], [])
                st.save_topic_model(TopicModel(classifier_version="topics-1:x", radar_id="escape", embedder="e",
                                               params={"k": 1}, members={}, fitted_at="2026-11-20T00:00:00Z",
                                               topics=[Topic("tp_a", "a", ["a"], [1.0], 5, 0.1)]))
            import_store(store, self.app.state.engine)
            sessions = lambda: Session(self.app.state.engine)  # noqa: E731
            enqueued = []

            class Job:
                id = "obs_1"

            def enqueue(queue, radar_id):
                enqueued.append(radar_id)
                return Job()

            now = dt.datetime(2026, 11, 25, 9, tzinfo=UTC)
            tick_observations(sessions, self.queue, now=now, enqueue=enqueue)
            tick_observations(sessions, self.queue, now=now + dt.timedelta(hours=5), enqueue=enqueue)
            self.assertEqual(enqueued, ["escape"])  # once this week, and only the radar with a topic model
            tick_observations(sessions, self.queue, now=now + dt.timedelta(days=7), enqueue=enqueue)
            self.assertEqual(enqueued, ["escape", "escape"])  # the next week again
            self.client.patch("/api/v1/radars/escape", json={"state": "paused"}, headers=self.auth)
            tick_observations(sessions, self.queue, now=now + dt.timedelta(days=14), enqueue=enqueue)
            self.assertEqual(len(enqueued), 2)  # paused: not observed


if __name__ == "__main__":
    unittest.main()
