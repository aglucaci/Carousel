import contextlib
import csv
import datetime as dt
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from carousel import Radar, cli
from carousel import feedback as fb
from carousel.connectors import ConnectorError, OpenAlexConnector, PubMedConnector, SearchResult
from carousel.connectors.base import Connector, QueryCoverage
from carousel.database import PaperStore
from carousel.radar import CSV_COLUMNS
from test_connectors import replay

UTC = dt.timezone.utc
AS_OF = dt.date(2026, 10, 14)  # matches the recorded fixtures' window (14 days ending 2026-10-14)
DAY1 = dt.datetime(2026, 10, 15, 8, tzinfo=UTC)

RADAR_YML = """\
radar_id: escape
radar_name: Escape radar
description: Viral and tumour immune escape
sources: [pubmed, openalex, biorxiv]
queries: [immune escape, antigenic drift]
exclude: [case report]
filters:
  date_range: last_14_days
"""


def connectors():
    """Replays the recorded PubMed and OpenAlex responses (no network)."""
    return {
        "pubmed": PubMedConnector(replay("pubmed_search.json"), api_key="", page_size=3, batch_size=5),
        "openalex": OpenAlexConnector(replay("openalex_search.json"), api_key=""),
    }


class ScanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.radar_path = self.dir / "escape.yml"
        self.radar_path.write_text(RADAR_YML)
        self.db = self.dir / "carousel.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def scan(self, day: int = 1, **kw):
        radar = Radar.from_yaml(self.radar_path, store=self.db)
        args = dict(as_of=AS_OF, observed_at=DAY1 + dt.timedelta(days=day - 1), connectors=connectors(),
                    embedder="hashing", max_results=5)
        args.update(kw)
        return radar.scan(**args)


class RescanTests(ScanTestCase):
    def test_scan_end_to_end(self) -> None:
        res = self.scan()
        self.assertEqual(len(res), 10)
        self.assertEqual(res.run["status"], "partial")  # biorxiv has no connector yet, and says so
        by_source = {c["source"]: c for c in res.coverage}
        self.assertEqual(by_source["biorxiv"]["status"], "no_connector")
        self.assertEqual((by_source["pubmed"]["retrieved"], by_source["pubmed"]["total_available"]), (5, 221))
        self.assertEqual(by_source["openalex"]["cost"], 10.0)
        self.assertEqual(res.run["ranking_version"], "0.3.0")
        self.assertEqual(res.run["model_version"], "carousel/hashing-v1-d256")
        self.assertIn("using the non-semantic hashing embedder", res.run["notes"])
        top = res.items[0]
        self.assertIn("relevance", top["components"])
        self.assertTrue(top["details"]["paper"]["title"])
        self.assertEqual(res.delta()["previous_run_id"], None)

    def test_rescan_keeps_history(self) -> None:
        first = self.scan(day=1)
        second = self.scan(day=2)
        with PaperStore(self.db) as s:
            runs = s.list_runs("escape")
            self.assertEqual([r["run_id"] for r in runs], [first.run_id, second.run_id])
            # The first run is unchanged by the second.
            self.assertEqual(s.get_run(first.run_id)["items"], first.items)
            self.assertEqual(len(s.get_run(second.run_id)["observations"]), 10)
            # Papers keep the time CAROUSEL first saw them.
            self.assertTrue(all(p.first_seen == DAY1 for p in s.papers()))
            self.assertEqual(s.stats()["papers"], 10)
        self.assertEqual(second.delta()["previous_run_id"], first.run_id)
        # Novelty on the second scan can only look at papers seen before each candidate: none were.
        self.assertIn("novelty", second.run["ranking"]["inactive_signals"])

    def test_source_failure_is_recorded_and_scan_continues(self) -> None:
        class Down(Connector):
            name, version = "openalex", "test"

            def __init__(self):
                pass

            def search(self, query):
                raise ConnectorError("HTTP 503 from https://api.openalex.org/works: down", status=503)

            def fetch(self, ids):
                return []

        conns = connectors()
        conns["openalex"] = Down()
        res = self.scan(connectors=conns)
        cov = {c["source"]: c for c in res.coverage}
        self.assertEqual(cov["openalex"]["status"], "error")
        self.assertIn("503", cov["openalex"]["error"])
        self.assertEqual(len(res), 5)


class FeedbackTests(ScanTestCase):
    def feedback_path(self) -> Path:
        return fb.default_path(self.radar_path)

    def test_dismissed_papers_stay_dismissed(self) -> None:
        first = self.scan(day=1)
        top, second_best = first.items[0], first.items[1]
        doi = second_best["details"]["paper"]["doi"]
        fb.update(self.feedback_path(), "dismiss", [top["paper_id"], f"doi:{doi}"], radar_id="escape")

        for day in (2, 3):
            res = self.scan(day=day)
            self.assertNotIn(top["paper_id"], res.paper_ids())
            self.assertNotIn(second_best["paper_id"], res.paper_ids())
            self.assertEqual(sorted(res.run["ranking"]["stats"]["excluded"]["dismissed"]),
                             sorted([top["paper_id"], second_best["paper_id"]]))
            self.assertEqual(len(res), 8)
        with PaperStore(self.db) as s:
            events = s.feedback_events("escape")
        self.assertEqual([(e["action"], e["change"]) for e in events], [("dismiss", "added")] * 2)  # logged once

        # Undo by removing the line: the paper comes back, and the removal is logged.
        fb.update(self.feedback_path(), "dismiss", [top["paper_id"]], remove=True)
        res = self.scan(day=4)
        self.assertIn(top["paper_id"], res.paper_ids())
        with PaperStore(self.db) as s:
            self.assertEqual(s.feedback_events("escape")[-1]["change"], "removed")

    def test_judgments_move_the_profile_and_leave_the_list(self) -> None:
        first = self.scan(day=1)
        rel, irr = first.items[0]["paper_id"], first.items[-1]["paper_id"]
        fb.update(self.feedback_path(), "relevant", [rel])
        fb.update(self.feedback_path(), "irrelevant", [irr])
        fb.update(self.feedback_path(), "save", ["pmid:42898639"])
        res = self.scan(day=2)
        stats = res.run["ranking"]["stats"]
        self.assertEqual((stats["profile"]["feedback_positive"], stats["profile"]["feedback_negative"]), (2, 1))
        excluded = stats["excluded"]
        self.assertEqual(excluded["marked_relevant"], [rel])
        self.assertEqual(excluded["marked_irrelevant"], [irr])
        self.assertEqual(len(excluded["saved"]), 1)
        self.assertTrue({rel, irr}.isdisjoint(res.paper_ids()))

    def test_unresolved_and_contradictory_feedback(self) -> None:
        fb.update(self.feedback_path(), "save", ["doi:10.9999/not-yet-seen", "nonsense"])
        res = self.scan()
        self.assertTrue(any("not in the store yet" in n for n in res.run["notes"]))
        pid = res.items[0]["paper_id"]
        fb.update(self.feedback_path(), "relevant", [pid])
        fb.update(self.feedback_path(), "irrelevant", [pid])
        with self.assertRaises(fb.FeedbackError):
            self.scan(day=2)

    def test_feedback_file_validation(self) -> None:
        self.feedback_path().write_text("dismis: [cp_1]\n")
        with self.assertRaises(fb.FeedbackError):
            fb.load(self.feedback_path())
        with self.assertRaises(fb.FeedbackError):
            fb.update(self.feedback_path(), "like", ["cp_1"])


class ExportTests(ScanTestCase):
    def test_to_csv(self) -> None:
        res = self.scan()
        path = res.to_csv(self.dir / "recs.csv")
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        self.assertEqual(tuple(reader.fieldnames), CSV_COLUMNS)
        self.assertEqual(len(rows), len(res))
        self.assertEqual([r["rank"] for r in rows], [str(i) for i in range(1, 11)])
        self.assertTrue(all(r["paper_id"].startswith("cp_") and r["run_id"] == res.run_id for r in rows))
        self.assertTrue(any(r["doi"] for r in rows))

    def test_to_json_and_results(self) -> None:
        self.scan(day=1)
        second = self.scan(day=2)
        data = json.loads(second.to_json(self.dir / "run.json"))
        self.assertEqual(data["run_id"], second.run_id)
        self.assertEqual(data["delta"]["previous_run_id"], second.delta()["previous_run_id"])
        again = Radar.from_yaml(self.radar_path, store=self.db).results()
        self.assertEqual(again.run, second.run)


class RadarApiTests(unittest.TestCase):
    def test_topic_constructor(self) -> None:
        r = Radar(topic="Viral Evolution & Escape", mode="exploratory", days=30, sources=["pubmed"])
        self.assertEqual(r.radar_id, "viral-evolution-escape")
        self.assertEqual(r.config.queries, ["Viral Evolution & Escape"])
        self.assertEqual(r.config.filters.days, 30)
        self.assertIsNone(r.feedback_path)
        with self.assertRaises(ValueError):
            Radar()

    def test_source_queries_are_sent_verbatim(self) -> None:
        seen = {}

        class Capture(Connector):
            name, version = "pubmed", "test"

            def __init__(self):
                pass

            def search(self, query):
                seen["native"] = query.native
                cov = QueryCoverage(source="pubmed", connector_version="test", native_query=query.native or "",
                                    date_field="publication date", start=query.start, end=query.end,
                                    total_available=0, retrieved=0, truncated=False, pages=1, requests=1,
                                    cache_hits=0, cost=1, cost_unit="requests", retrieved_at=dt.datetime.now(UTC))
                return SearchResult(records=[], coverage=cov)

            def fetch(self, ids):
                return []

        from carousel.core.config import RadarConfig

        cfg = RadarConfig.model_validate({"radar_id": "x", "radar_name": "X", "sources": ["pubmed"], "queries": ["a"],
                                          "source_queries": {"pubmed": ['"immune evasion"[MeSH]', "escape[tiab]"]}})
        with tempfile.TemporaryDirectory() as d:
            res = Radar(cfg, store=Path(d) / "s.db").scan(connectors={"pubmed": Capture()}, embedder="none")
        self.assertEqual(seen["native"], '("immune evasion"[MeSH]) OR (escape[tiab])')
        self.assertEqual(len(res), 0)
        self.assertEqual(res.run["status"], "ok")


class CliTests(ScanTestCase):
    def run_cli(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_scan_feedback_runs_export_delta(self) -> None:
        factories = {"pubmed": lambda: connectors()["pubmed"], "openalex": lambda: connectors()["openalex"]}
        with mock.patch.dict("carousel.radar.CONNECTORS", factories, clear=True):
            code, out, err = self.run_cli("scan", str(self.radar_path), "--store", str(self.db), "--as-of", "2026-10-14",
                                          "--max-results", "5", "--embedder", "hashing", "--top", "3",
                                          "--csv", str(self.dir / "out.csv"))
            self.assertEqual(code, 0, err)
            self.assertIn("pubmed    5 of 221 retrieved", out)
            self.assertIn("biorxiv   no_connector", out)
            self.assertIn("observed 10 records -> 10 candidates -> 3 recommended", out)
            self.assertTrue((self.dir / "out.csv").exists())
            top_id = out.split("(cp_", 1)[1].split(")", 1)[0]

            code, out, _ = self.run_cli("feedback", str(self.radar_path), "dismiss", f"cp_{top_id}")
            self.assertEqual(code, 0)
            self.assertIn("applied on the next scan", out)

            code, out, _ = self.run_cli("scan", str(self.radar_path), "--store", str(self.db), "--as-of", "2026-10-14",
                                        "--max-results", "5", "--embedder", "hashing", "--top", "3")
            self.assertNotIn(f"cp_{top_id}", out)
            self.assertIn("vs previous run: 1 entered, 1 left", out)

        code, out, _ = self.run_cli("runs", "--store", str(self.db))
        self.assertIn("2 run(s)", out)
        run_id = out.split()[0]
        code, out, _ = self.run_cli("export", run_id, "--store", str(self.db), "--json", str(self.dir / "r.json"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.dir / "r.json").read_text())["run_id"], run_id)
        code, out, _ = self.run_cli("delta", str(self.radar_path), "--store", str(self.db))
        self.assertEqual(json.loads(out)["left"], [f"cp_{top_id}"])

    def test_cli_errors(self) -> None:
        code, _, err = self.run_cli("runs", "--store", str(self.dir / "missing.db"))
        self.assertEqual(code, 1)
        code, _, err = self.run_cli("export", "run_nope", "--store", str(self.db))
        self.assertEqual(code, 1)
        code, _, err = self.run_cli("feedback", str(self.radar_path), "dismiss", "x", "--file", str(self.dir / "f.yml"))
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
