"""Study C harness (plan task 31): annotated benchmark, time-forward replay, and metrics.

Done when: `carousel eval delta` runs the starter set end to end and writes a report; the leakage tests from task 25
hold during replay. Synthetic episodes with known answers test the harness; the starter set replays recorded PubMed
counts (tests/fixtures/http/benchmark_delta.json).
"""

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from carousel.connectors import CountResult, SearchQuery
from carousel.evaluation.delta import (Benchmark, Episode, count_episode, evaluate_benchmark, load_benchmark, replay,
                                       report_markdown, weeks)
from test_connectors import FIXTURES

REPO = Path(__file__).resolve().parents[1]
STARTER = REPO / "benchmarks" / "delta" / "starter.yml"


class Synthetic:
    """Counts with a known story: the topic's share rises 2.5x from ``rise_from`` (None: never)."""

    def __init__(self, rise_from=None, seed=0, field=400, share=0.05):
        self.rng = np.random.default_rng(seed)
        self.rise_from, self.field, self.share, self.memo = rise_from, field, share, {}

    def count(self, q: SearchQuery) -> CountResult:
        key = (tuple(q.require), q.start)
        if key not in self.memo:
            if not q.require:
                n = self.field
            else:
                rate = self.share * (2.5 if self.rise_from and q.start >= self.rise_from else 1.0)
                rate *= 0.8 if len(q.require) < 3 else 1.0  # fewer key terms: a little less of the topic
                n = int(self.rng.poisson(self.field * rate))
            self.memo[key] = n
        return CountResult(source="pubmed", native_query=str(key), date_field="pdat", start=q.start, end=q.end,
                           count=self.memo[key], requests=1, cost=1, cost_unit="requests")


def episode(**kw):
    base = dict(id="e", description="d", field_queries=["field"], topic_label="t", key_terms=["a", "b", "c"],
                expected=True, expert_date=dt.date(2024, 4, 1), replay_start=dt.date(2023, 10, 2),
                replay_end=dt.date(2024, 9, 29), reference="test")
    base.update(kw)
    return Episode(**base)


class HarnessTests(unittest.TestCase):
    def test_weeks(self) -> None:
        w = weeks(dt.date(2024, 1, 3), dt.date(2024, 1, 28))
        self.assertEqual(w[0], (dt.date(2024, 1, 1), dt.date(2024, 1, 7)))
        self.assertEqual(w[-1], (dt.date(2024, 1, 22), dt.date(2024, 1, 28)))

    def test_starter_benchmark_is_valid_and_marked_draft(self) -> None:
        b = load_benchmark(STARTER)
        self.assertEqual(len(b.episodes), 5)
        self.assertTrue(b.status.startswith("draft"))
        self.assertEqual({e.annotation for e in b.episodes}, {"draft"})
        self.assertEqual(sum(e.expected for e in b.episodes), 3)
        self.assertTrue(all(e.reference for e in b.episodes))


@unittest.skipIf(np is None, 'needs pip install "carousel-research[trends]"')
class ReplayTests(unittest.TestCase):
    def test_known_rise_is_detected_after_the_lag_and_a_control_is_not(self) -> None:
        bench = Benchmark(benchmark="synthetic", version=1, status="test", episodes=[
            episode(id="rise", expert_date=dt.date(2024, 3, 25)),
            episode(id="flat", expected=False, expert_date=None)])
        report = evaluate_benchmark(bench, lambda ep: Synthetic(dt.date(2024, 3, 25) if ep.id == "rise" else None, seed=1))
        by = {e["id"]: e for e in report["episodes"]}
        rise = by["rise"]
        self.assertEqual(rise["label"], "true_positive")
        # Detection needs the risen weeks to settle (21 days) and to persist: about a month after the rise, not before it.
        self.assertGreater(rise["latency_days"], 21)
        self.assertLess(rise["latency_days"], 90)
        self.assertTrue(rise["faithfulness"]["faithful"])
        self.assertTrue(rise["stable_without_last_term"])
        self.assertEqual(by["flat"]["label"], "true_negative")
        m = report["metrics"]
        self.assertEqual((m["precision"], m["recall"], m["true_negatives"], m["faithfulness"]), (1.0, 1.0, 1, 1.0))
        md = report_markdown(report)
        self.assertIn("| Precision | 100% |", md)
        self.assertIn("| rise | yes | 2024-03-25 |", md)

    def test_outcomes(self) -> None:
        early = Benchmark(benchmark="s", version=1, status="t", episodes=[episode(id="early", expert_date=dt.date(2024, 9, 1))])
        report = evaluate_benchmark(early, lambda ep: Synthetic(dt.date(2024, 1, 8), seed=2))
        self.assertEqual(report["episodes"][0]["label"], "false_positive")  # detected, but far before the expert date
        missed = Benchmark(benchmark="s", version=1, status="t", episodes=[episode(id="missed")])
        report = evaluate_benchmark(missed, lambda ep: Synthetic(None, seed=3))
        self.assertEqual(report["episodes"][0]["label"], "false_negative")
        self.assertEqual(report["metrics"]["recall"], 0.0)

    def test_replay_never_reads_the_future(self) -> None:
        """The leakage guard during replay: changing every observation made after t leaves the decision at t alone."""
        ep = episode()
        rows = count_episode(ep, Synthetic(dt.date(2024, 3, 25), seed=4))
        cut = dt.date(2024, 4, 29)
        tampered = [dict(r, count=r["count"] * 10) if r["observed_at"][:10] > cut.isoformat() else r for r in rows]
        a, b = replay(ep, rows)["evaluations"], replay(ep, tampered)["evaluations"]
        for x, y in zip(a, b):
            if x["as_of"] <= cut.isoformat():
                self.assertEqual(x, y)
        self.assertNotEqual(a, b)  # ...while later decisions do see the change


@unittest.skipIf(np is None, 'needs pip install "carousel-research[trends]"')
class StarterTests(unittest.TestCase):
    def test_eval_delta_runs_the_starter_set_end_to_end(self) -> None:
        from carousel import cli

        fixture = FIXTURES / "benchmark_delta.json"
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["eval", "delta", str(STARTER), "--replay", str(fixture), "--out", str(Path(d) / "report")])
            self.assertEqual(code, 0, out.getvalue())
            report = json.loads((Path(d) / "report.json").read_text())
            md = (Path(d) / "report.md").read_text()
        self.assertEqual(report["metrics"]["episodes"], 5)
        self.assertIn("Benchmark status: **draft", md)
        self.assertEqual({e["id"] for e in report["episodes"]}, {e.id for e in load_benchmark(STARTER).episodes})
        for e in report["episodes"]:
            self.assertGreater(e["weeks"], 40)
            self.assertGreater(e["topic_papers"], 0)
        self.assertEqual(report["metrics"], RECORDED_METRICS)  # the recorded counts give exactly this result
        by = {e["id"]: e for e in report["episodes"]}
        self.assertEqual(by["sars-cov-2-jn1-2023"]["detection_date"], "2023-10-30")
        self.assertTrue(by["sars-cov-2-jn1-2023"]["faithfulness"]["faithful"])


# The starter set's result on the recorded counts (PubMed entry dates), pinned so any change to the pipeline that moves
# it shows up here. Recall is low: H5N1 in dairy cattle has too few papers a week for a 4-week window, and mpox clade I
# grew with its whole field (see the review notes in benchmarks/delta/starter.yml).
RECORDED_METRICS = {"episodes": 5, "true_positives": 1, "false_positives": 0, "false_negatives": 2, "true_negatives": 2,
                    "precision": 1.0, "recall": 1 / 3, "median_latency_days": -50, "stability": 1.0, "faithfulness": 1.0}


if __name__ == "__main__":
    unittest.main()
