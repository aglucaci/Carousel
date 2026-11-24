"""The trend model (plan task 26, spec §6).

Done when: simulation tests show injected accelerations are detected at the target power, and null series stay
within the nominal false-discovery rate; results are deterministic for the same observations.

Design point (stated so the target is checkable): a topic with about 15 papers a week in a field of about 300,
overdispersed (negative binomial, alpha 0.1), 16 weekly observations. Target: at least 80% power for a 2.5-fold
rise over the last 4 weeks; under no rise, BH at q = 0.10 (runs with any false flag at most 10%, with sampling slack).
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np

    from carousel.intelligence.trends import (FDR, benjamini_hochberg, evaluate, fit_series,
                                              trends_from_observations)
except ImportError:  # pragma: no cover - the [trends] extra is not installed
    np = None

UTC = dt.timezone.utc


def sim(rng, rr, weeks=16, p0=0.05, alpha=0.1, field=300, spike_only_last=False):
    out = []
    for w in range(weeks):
        n_field = int(rng.poisson(field))
        boost = rr if (w == weeks - 1 if spike_only_last else w >= weeks - 4) else 1.0
        lam = rng.gamma(1 / alpha, n_field * p0 * boost * alpha)
        out.append({"week": f"2026-{w + 1:02d}", "count": int(rng.poisson(lam)), "denominator": n_field})
    return out


@unittest.skipIf(np is None, 'needs pip install "carousel-research[trends]"')
class SimulationTests(unittest.TestCase):
    def test_power_at_the_design_point(self) -> None:
        rng = np.random.default_rng(7)
        report = evaluate({**{f"up{i}": sim(rng, 2.5) for i in range(60)}, **{f"null{i}": sim(rng, 1.0) for i in range(60)}})
        flagged = [t["topic_id"] for t in report["topics"] if t["flagged"]]
        power = sum(f.startswith("up") for f in flagged) / 60
        false = sum(f.startswith("null") for f in flagged)
        self.assertGreaterEqual(power, 0.8)
        self.assertLessEqual(false / max(1, len(flagged)), FDR)  # false-discovery proportion within q

    def test_null_series_stay_within_the_false_discovery_rate(self) -> None:
        runs_with_flags, flags = 0, 0
        for seed in range(40):
            rng = np.random.default_rng(1000 + seed)
            report = evaluate({f"null{i}": sim(rng, 1.0) for i in range(30)})
            k = sum(t["flagged"] for t in report["topics"])
            flags, runs_with_flags = flags + k, runs_with_flags + (k > 0)
        # Under a global null, BH's FDR equals the chance of any false flag: nominal 0.10 (0.15 allows sampling noise).
        self.assertLessEqual(runs_with_flags / 40, 0.15)
        self.assertLessEqual(flags / (40 * 30), 0.01)

    def test_a_one_week_spike_is_not_persistent(self) -> None:
        rng = np.random.default_rng(3)
        report = evaluate({"spike": sim(rng, 6.0, spike_only_last=True), "flat": sim(rng, 1.0)})
        spike = next(t for t in report["topics"] if t["topic_id"] == "spike")
        self.assertFalse(spike["flagged"])
        self.assertTrue({"persistence", "interval"} & set(spike["failed"]))  # one week can't make a trend

    def test_a_large_effect_on_few_papers_is_not_flagged(self) -> None:
        rng = np.random.default_rng(4)
        report = evaluate({"tiny": sim(rng, 3.0, p0=0.002), **{f"n{i}": sim(rng, 1.0) for i in range(5)}})
        tiny = next(t for t in report["topics"] if t["topic_id"] == "tiny")
        self.assertFalse(tiny["flagged"])
        self.assertIn("min_count", tiny["failed"])

    def test_deterministic(self) -> None:
        data = {f"t{i}": sim(np.random.default_rng(i), 1.0 + i / 3) for i in range(6)}
        self.assertEqual(evaluate(data), evaluate(data))


@unittest.skipIf(np is None, 'needs pip install "carousel-research[trends]"')
class FitTests(unittest.TestCase):
    def test_fit_reports_what_it_used(self) -> None:
        f = fit_series(sim(np.random.default_rng(5), 2.5))
        self.assertEqual((f.status, f.method, f.baseline, f.weeks), ("ok", "negbin", "level", 16))
        self.assertTrue(f.ci_low < f.rate_ratio < f.ci_high)
        self.assertGreater(f.recent_count, f.expected_recent)
        long = fit_series(sim(np.random.default_rng(5), 1.0, weeks=34))
        self.assertEqual(long.baseline, "linear")  # enough baseline weeks to estimate a drift

    def test_too_little_data(self) -> None:
        self.assertEqual(fit_series(sim(np.random.default_rng(1), 1.0, weeks=10)).status, "insufficient_history")
        zeros = [{"week": f"w{i}", "count": 0, "denominator": 100} for i in range(16)]
        self.assertEqual(fit_series(zeros).status, "no_counts")
        sparse = [{"week": f"w{i}", "count": 1 if i in (3, 14) else 0, "denominator": 100} for i in range(16)]
        f = fit_series(sparse)
        self.assertIn(f.status, ("ok", "failed"))  # whatever happens, it says what it did
        if f.status == "ok":
            self.assertIn(f.method, ("negbin", "poisson"))

    def test_a_topic_that_did_not_exist_gets_an_exact_test(self) -> None:
        """No papers in any baseline week (a new lineage, a new host): the GLM can't estimate the excess."""
        weeks = [{"week": f"w{i:02d}", "count": 0 if i < 12 else c, "denominator": 1000}
                 for i, c in zip(range(16), [0] * 12 + [3, 8, 15, 30])]
        f = fit_series(weeks)
        self.assertEqual((f.status, f.method, f.ci_high, f.recent_count), ("ok", "exact (emerging topic)", None, 56))
        self.assertLess(f.p_value, 1e-20)  # (4000/16000)**56
        self.assertGreater(f.ci_low, 1.5)
        report = evaluate({"new": weeks, **{f"flat{i}": sim(np.random.default_rng(i), 1.0) for i in range(3)}})
        new = next(t for t in report["topics"] if t["topic_id"] == "new")
        self.assertTrue(new["flagged"])
        one = [dict(w, count=0) for w in weeks[:-1]] + [dict(weeks[-1], count=2)]
        self.assertFalse(evaluate({"blip": one})["topics"][0]["flagged"])  # two papers: below the minimum count

    def test_benjamini_hochberg(self) -> None:
        q = benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5})
        self.assertAlmostEqual(q["a"], 0.04)
        self.assertAlmostEqual(q["c"], 0.0533333, places=6)
        self.assertAlmostEqual(q["b"], 0.0533333, places=6)  # monotone: never below a smaller p's q
        self.assertAlmostEqual(q["d"], 0.5)


@unittest.skipIf(np is None, 'needs pip install "carousel-research[trends]"')
class ObservationTests(unittest.TestCase):
    def test_from_stored_observations_as_known_at_a_time(self) -> None:
        from types import SimpleNamespace

        from carousel.core.config import RadarConfig
        from carousel.database import PaperStore
        from carousel.intelligence.observations import observe
        from test_observations import OBS_RADAR, Scripted

        weeks_rising = {dt.date(2026, 11, 16) - dt.timedelta(days=7 * i) for i in range(4)}
        rng = np.random.default_rng(11)
        noise = {}

        def counts(require, start):
            key = (require, start)
            if key not in noise:
                rate = 0.05 * (2.5 if require and require[0] == "rising" and start in weeks_rising else 1.0)
                noise[key] = 300 if not require else int(rng.poisson(300 * rate))
            return noise[key]

        model = SimpleNamespace(classifier_version="topics-1:t", topics=[
            SimpleNamespace(topic_id="tp_rising", key_terms=["rising"]),
            *[SimpleNamespace(topic_id=f"tp_flat{i}", key_terms=[f"flat{i}"]) for i in range(4)]])
        radar = RadarConfig.model_validate({**OBS_RADAR, "sources": ["pubmed"]})
        at = dt.datetime(2026, 11, 23, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            observe(st, radar, model, {"pubmed": Scripted(counts)}, at=at, weeks=16, today=dt.date(2026, 11, 23))
            rows = st.topic_observations("escape")
        report = trends_from_observations(rows, [t.topic_id for t in model.topics], "pubmed", at)
        flagged = [t["topic_id"] for t in report["topics"] if t["flagged"]]
        self.assertEqual(flagged, ["tp_rising"])
        self.assertEqual(report["trend_model_version"], "trend-nb-1")
        before = trends_from_observations(rows, ["tp_rising"], "pubmed", at - dt.timedelta(days=1))
        self.assertEqual(before["topics"][0]["status"], "insufficient_history")  # nothing was observed yet
        self.assertEqual(report, trends_from_observations(list(reversed(rows)), [t.topic_id for t in model.topics],
                                                          "pubmed", at))  # deterministic, whatever the row order


@unittest.skipIf(np is None, 'needs pip install "carousel-research[trends]"')
class CliTests(unittest.TestCase):
    def test_topics_trends(self) -> None:
        import contextlib
        import io

        from carousel import cli
        from carousel.core.config import RadarConfig
        from carousel.database import PaperStore
        from carousel.intelligence.observations import observe
        from carousel.intelligence.topics import Topic, TopicModel
        from test_observations import OBS_RADAR, Scripted

        rising = {dt.date(2026, 11, 16) - dt.timedelta(days=7 * i) for i in range(4)}
        rng, memo = np.random.default_rng(21), {}

        def counts(require, start):
            if (require, start) not in memo:
                rate = 0.05 * (2.5 if require and require[0] == "rising" and start in rising else 1.0)
                memo[(require, start)] = 300 if not require else int(rng.poisson(300 * rate))
            return memo[(require, start)]

        topics = [Topic(topic_id=f"tp_{n}", label=f"{n} label", key_terms=[n], centroid=[1.0], size=5, threshold=0.1)
                  for n in ("rising", "flat0", "flat1", "flat2")]
        model = TopicModel(classifier_version="topics-1:cli", radar_id="escape", embedder="e", params={"k": 4},
                           topics=topics, members={}, fitted_at="2026-11-20T00:00:00Z")
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "s.db")
            with PaperStore(db) as st:
                st.save_topic_model(model)
                observe(st, RadarConfig.model_validate({**OBS_RADAR, "sources": ["pubmed"]}), model,
                        {"pubmed": Scripted(counts)}, at=dt.datetime(2026, 11, 23, 12, tzinfo=UTC), weeks=16,
                        today=dt.date(2026, 11, 23))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["topics", "trends", "escape", "--store", db, "--as-of", "2026-12-14T00:00:00Z"]), 0)
            lines = out.getvalue().splitlines()
            self.assertTrue(lines[0].startswith("pubmed (trend-nb-1"))
            self.assertTrue(lines[1].lstrip().startswith("FLAGGED rising label"))
            self.assertEqual(sum("FLAGGED" in line for line in lines), 1)


try:
    from test_api import ApiTestCase
except Exception:  # pragma: no cover - server extras missing
    ApiTestCase = None

if ApiTestCase is not None and np is not None:
    class TrendsApiTests(ApiTestCase):
        def test_trends_endpoint(self) -> None:
            from carousel.core.config import RadarConfig
            from carousel.database import PaperStore
            from carousel.database.importer import import_store
            from carousel.intelligence.observations import observe
            from carousel.intelligence.topics import Topic, TopicModel
            from test_observations import OBS_RADAR, Scripted

            rising = {dt.date(2026, 11, 16) - dt.timedelta(days=7 * i) for i in range(4)}
            rng = np.random.default_rng(12)
            memo = {}

            def counts(require, start):
                if (require, start) not in memo:
                    rate = 0.05 * (2.5 if require and require[0] == "rising" and start in rising else 1.0)
                    memo[(require, start)] = 300 if not require else int(rng.poisson(300 * rate))
                return memo[(require, start)]

            topics = [Topic(topic_id="tp_rising", label="rising", key_terms=["rising"], centroid=[1.0, 0.0], size=5,
                            threshold=0.1)] + [Topic(topic_id=f"tp_flat{i}", label=f"flat {i}", key_terms=[f"flat{i}"],
                                                     centroid=[0.0, 1.0], size=5, threshold=0.1) for i in range(4)]
            model = TopicModel(classifier_version="topics-1:api", radar_id="escape", embedder="e", params={"k": 5},
                               topics=topics, members={}, fitted_at="2026-11-20T00:00:00Z")
            store = Path(self.tmp.name) / "engine.db"
            at = dt.datetime(2026, 11, 23, 12, tzinfo=UTC)
            with PaperStore(store) as st:
                run = {"run_id": "run_1", "radar_id": "escape", "radar_name": "Escape radar", "config_hash": "x",
                       "as_of": "2026-11-22", "started_at": "2026-11-23T00:00:00.000000Z",
                       "finished_at": "2026-11-23T00:00:00.000000Z", "status": "ok", "ranking_version": "0",
                       "candidates_version": "0", "model_version": None, "mode": "balanced", "n_observed": 0,
                       "n_candidates": 0, "coverage": [], "ranking": {}}
                st.save_run(run, [], [])
                st.save_topic_model(model)
                observe(st, RadarConfig.model_validate({**OBS_RADAR, "sources": ["pubmed"]}), model,
                        {"pubmed": Scripted(counts)}, at=at, weeks=16, today=dt.date(2026, 11, 23))
            import_store(store, self.app.state.engine)
            body = self.get("/radars/escape/topics/trends", as_of="2026-12-14T00:00:00Z").json()
            [rep] = body["reports"]
            self.assertEqual((rep["source"], rep["trend_model_version"]), ("pubmed", "trend-nb-1"))
            self.assertEqual([t["topic_id"] for t in rep["topics"] if t["flagged"]], ["tp_rising"])
            self.assertEqual(next(t for t in rep["topics"] if t["flagged"])["label"], "rising")
            rising = next(t for t in rep["topics"] if t["flagged"])
            self.assertEqual(len(rising["weekly"]), 16)  # every observed week, for the chart and table
            self.assertEqual(len(rising["baseline_share"]), 16)
            self.assertFalse(any(w["provisional"] for w in rising["weekly"]))  # long after: all settled
            self.assertEqual((body["fitted_at"][:10], body["previous_version"]), ("2026-11-20", None))
            early = self.get("/radars/escape/topics/trends", as_of="2026-11-01T00:00:00Z").json()
            self.assertEqual(early["reports"], [])  # nothing had been observed then


if __name__ == "__main__":
    unittest.main()
