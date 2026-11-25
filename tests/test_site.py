import contextlib
import csv
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from carousel import Radar, cli, site
from carousel.core.config import load_radar
from test_scan import AS_OF, DAY1, RADAR_YML, connectors

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import snapshot  # noqa: E402

THEMED_RADAR = RADAR_YML + """\
signal_profile:
  immune_escape:
    description: Immune escape
    terms: [immune escape, immune evasion]
  tumour:
    description: Tumour biology
    terms: [tumor, cancer, carcinoma]
"""


class SiteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "escape.yml").write_text(THEMED_RADAR)
        self.radar = Radar.from_yaml(self.dir / "escape.yml", store=self.dir / "c.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def export(self, day: int = 1, name: str = "run.json") -> Path:
        res = self.radar.scan(as_of=AS_OF, observed_at=DAY1 + dt.timedelta(days=day - 1), connectors=connectors(),
                              embedder="hashing", max_results=5)
        path = self.dir / name
        res.to_json(path)
        return path


class SiteTests(SiteTestCase):
    def test_builds_index_radar_page_and_data(self) -> None:
        self.export(day=1, name="first.json")
        run_path = self.export(day=2)
        out = self.dir / "docs"
        (out / "explore").mkdir(parents=True)
        (out / "explore" / "index.html").write_text("old ETHER page")
        site.build([run_path], out)

        self.assertFalse((out / "explore").exists())  # the output directory is replaced
        for rel in ("index.html", ".nojekyll", "latest.json", "assets/carousel-logo.svg", "assets/favicon.png",
                    "radars/escape/index.html", "radars/escape/latest.json", "radars/escape/latest.csv"):
            self.assertTrue((out / rel).exists(), rel)

        run = json.loads(run_path.read_text())
        self.assertEqual(json.loads((out / "radars/escape/latest.json").read_text()), run)
        index = json.loads((out / "latest.json").read_text())
        self.assertEqual(index["radars"][0]["run_id"], run["run_id"])

        page = (out / "radars/escape/index.html").read_text()
        first_title = run["items"][0]["details"]["paper"]["title"]
        self.assertIn(first_title.replace("&", "&amp;")[:40], page)
        self.assertIn("Why this paper?", page)
        self.assertIn("no_connector", page)                   # coverage is shown, gaps included
        self.assertIn('data-theme="immune_escape"', page)     # theme filter from signal_profile
        self.assertIn("Since the previous run", page)
        self.assertIn("not a measure of a paper", page)
        with open(out / "radars/escape/latest.csv", newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), len(run["items"]))
        home = (out / "index.html").read_text()
        self.assertIn('href="radars/escape/index.html"', home)
        self.assertIn("Viral and tumour immune escape", home)

    def test_text_is_escaped(self) -> None:
        run = json.loads(self.export().read_text())
        run["items"][0]["details"]["paper"]["title"] = '<script>alert("x")</script>'
        run["radar"]["description"] = "<img src=x onerror=alert(1)>"
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps(run))
        out = self.dir / "docs"
        site.build([bad], out)
        page = (out / "radars/escape/index.html").read_text()
        self.assertNotIn('<script>alert("x")</script>', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<img src=x", page)

    def test_rejects_duplicate_or_missing_runs(self) -> None:
        a = self.export(day=1, name="a.json")
        b = self.export(day=2, name="b.json")
        with self.assertRaises(ValueError):
            site.build([a, b], self.dir / "docs")
        with self.assertRaises(ValueError):
            site.build([], self.dir / "docs")

    def test_cli(self) -> None:
        run_path = self.export()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["site", str(run_path), "--out", str(self.dir / "site")])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("wrote", out.getvalue())


class SnapshotOfRunTests(SiteTestCase):
    def test_run_export_becomes_a_versioned_snapshot(self) -> None:
        run = json.loads(self.export().read_text())
        env = snapshot.build_envelope(run, radar_id="escape", source="workflow",
                                      fallback_time=dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), provenance={})
        self.assertEqual(env["captured_at_utc"], "2026-10-15T08:00:00Z")  # from the run, not the fallback
        self.assertEqual(env["ranking_version"], "0.3.0")
        self.assertEqual(env["window_days"], 14)
        self.assertEqual(env["window_utc"], {"start": "2026-10-01", "end": "2026-10-14"})
        self.assertEqual(env["n_items"], len(run["items"]))
        self.assertEqual(env["run_id"], run["run_id"])
        root = self.dir / "snaps"
        path, written = snapshot.write_envelope(root, env)
        self.assertTrue(written)
        index = snapshot.reindex(root, "escape")
        row = json.loads(index.read_text().splitlines()[0])
        self.assertEqual((row["run_id"], row["ranking_version"]), (run["run_id"], "0.3.0"))


class LabRadarTests(unittest.TestCase):
    def test_lab_default_radar(self) -> None:
        cfg = load_radar(REPO / "radars" / "lab_default.yml")
        self.assertEqual(cfg.radar_id, "lab-default")  # continues the existing snapshot history
        self.assertEqual([s.value for s in cfg.sources], ["pubmed", "openalex"])
        native = cfg.source_queries[cfg.sources[0]]
        self.assertEqual(len(native), 10)                # one legacy query per theme
        self.assertTrue(all("NOT (case report OR randomized OR psychology)" in q for q in native))
        self.assertEqual(len(cfg.signal_profile), 10)
        self.assertEqual(cfg.signal_profile["selection_codon_models"].weight, 1.35)
        self.assertEqual(cfg.filters.days, 14)
        self.assertLess(len(" OR ".join(f"({q})" for q in native)), 4500)  # fits a PubMed GET


if __name__ == "__main__":
    unittest.main()
