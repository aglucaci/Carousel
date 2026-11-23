import contextlib
import io
import tempfile
import textwrap
import unittest
from pathlib import Path

from carousel import cli
from carousel.config import ConfigError, RadarConfig, load_radar

REPO = Path(__file__).resolve().parents[1]

MINIMAL = """\
radar_name: Test Radar
sources: [pubmed]
queries:
  - viral evolution
"""


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, text: str, name: str = "my_radar.yml") -> Path:
        p = self.dir / name
        p.write_text(textwrap.dedent(text))
        return p

    def _error(self, text: str) -> str:
        with self.assertRaises(ConfigError) as ctx:
            load_radar(self._write(text))
        return str(ctx.exception)

    # ---- valid configs ----

    def test_all_examples_are_valid_and_have_unique_ids(self) -> None:
        paths = sorted((REPO / "examples").glob("*.yml"))
        self.assertGreaterEqual(len(paths), 8)
        ids = [load_radar(p).radar_id for p in paths]
        self.assertEqual(len(ids), len(set(ids)))
        for p in paths:
            self.assertEqual(load_radar(p).warnings(), [], p.name)

    def test_minimal_config_fills_defaults(self) -> None:
        cfg = load_radar(self._write(MINIMAL))
        self.assertEqual(cfg.radar_id, "my-radar")
        self.assertEqual(cfg.filters.days, 90)
        self.assertEqual([o.value for o in cfg.outputs], ["dashboard"])
        self.assertAlmostEqual(sum(cfg.ranking.normalized().values()), 1.0)

    def test_explicit_radar_id_wins(self) -> None:
        cfg = load_radar(self._write(MINIMAL + "radar_id: viral-evo\n"))
        self.assertEqual(cfg.radar_id, "viral-evo")

    def test_config_hash_is_stable_and_sensitive(self) -> None:
        a = load_radar(self._write(MINIMAL, "a.yml"))
        b = load_radar(self._write("# comment only differs\n" + MINIMAL, "a.yml"))
        c = load_radar(self._write(MINIMAL + "  - immune escape\n", "a.yml"))
        self.assertEqual(a.config_hash(), b.config_hash())
        self.assertNotEqual(a.config_hash(), c.config_hash())

    def test_ranking_renormalizes_over_active_signals(self) -> None:
        cfg = load_radar(self._write(MINIMAL))
        w = cfg.ranking.normalized(active=["relevance", "discovery", "evidence"])
        self.assertEqual(w["novelty"], 0.0)
        self.assertEqual(w["momentum"], 0.0)
        self.assertAlmostEqual(w["relevance"], 0.40 / 0.65)
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_legacy_scoring_accepted_with_warning(self) -> None:
        cfg = load_radar(self._write(MINIMAL + textwrap.dedent("""\
            scoring:
              topic_relevance_weight: 0.35
              recency_weight: 0.20
              signal_weight: 0.35
              source_weight: 0.10
            """)))
        self.assertTrue(any("legacy" in w for w in cfg.warnings()))

    # ---- invalid configs ----

    def test_unknown_field_reports_key_line(self) -> None:
        msg = self._error("radar_name: T\nsources: [pubmed]\nqueries: [x]\nquerys:\n  - y\n")
        self.assertIn("line 4: querys: unknown field", msg)

    def test_unknown_source_lists_allowed_values(self) -> None:
        msg = self._error("radar_name: T\nsources: [pubmed, scopus]\nqueries: [x]\n")
        self.assertIn("line 2: sources.1", msg)
        self.assertIn("'scopus'", msg)
        self.assertIn("openalex", msg)

    def test_missing_required_fields(self) -> None:
        msg = self._error("description: nothing else\n")
        for field in ("radar_name", "sources", "queries"):
            self.assertIn(f"{field}: Field required", msg)

    def test_duplicate_yaml_key_rejected(self) -> None:
        msg = self._error("radar_name: A\nsources: [pubmed]\nqueries: [x]\nradar_name: B\n")
        self.assertIn(":4:1: YAML syntax error: found duplicate key 'radar_name' (first defined on line 1)", msg)

    def test_yaml_syntax_error_has_location(self) -> None:
        msg = self._error('radar_name: T\nqueries:\n  - "unclosed\n')
        self.assertRegex(msg, r"my_radar\.yml:\d+:\d+: YAML syntax error")

    def test_duplicate_terms_case_insensitive(self) -> None:
        msg = self._error("radar_name: T\nsources: [pubmed]\nqueries: [Immune escape, immune escape]\n")
        self.assertIn("duplicate query: 'immune escape'", msg)

    def test_bad_signal_category(self) -> None:
        msg = self._error(MINIMAL + "signal_profile:\n  methods:\n    terms: []\n    weight: 0\n")
        self.assertIn("signal_profile.methods.terms", msg)
        self.assertIn("signal_profile.methods.weight", msg)

    def test_bad_category_name(self) -> None:
        msg = self._error(MINIMAL + "signal_profile:\n  Clinical Relevance:\n    terms: [relapse]\n")
        self.assertIn("lowercase snake_case", msg)

    def test_bad_date_range(self) -> None:
        msg = self._error(MINIMAL + "filters:\n  date_range: last_ninety_days\n")
        self.assertIn("last_<N>_days", msg)

    def test_exclude_cannot_cancel_query(self) -> None:
        msg = self._error(MINIMAL + "exclude: [Viral Evolution]\n")
        self.assertIn("exclude terms also appear in queries", msg)

    def test_all_zero_ranking_rejected(self) -> None:
        zero = "ranking:\n" + "".join(f"  {k}: 0\n" for k in ("relevance", "novelty", "momentum", "discovery", "evidence"))
        self.assertIn("at least one ranking weight", self._error(MINIMAL + zero))

    def test_non_mapping_and_empty_files(self) -> None:
        self.assertIn("top level must be a mapping", self._error("- a\n- b\n"))
        self.assertIn("file is empty", self._error(""))
        with self.assertRaises(ConfigError) as ctx:
            load_radar(self.dir / "missing.yml")
        self.assertIn("file not found", str(ctx.exception))

    # ---- CLI ----

    def _run(self, *argv: str) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_validate_exit_codes(self) -> None:
        good = self._write(MINIMAL, "good.yml")
        bad = self._write("radar_name: T\n", "bad.yml")
        code, out, _ = self._run("validate", str(good))
        self.assertEqual(code, 0)
        self.assertIn("ok   ", out)
        code, _, err = self._run("validate", str(good), str(bad))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", err)
        self.assertIn("1/2 valid", err)

    def test_cli_rejects_duplicate_radar_ids(self) -> None:
        a = self._write(MINIMAL + "radar_id: same\n", "a.yml")
        b = self._write(MINIMAL + "radar_id: same\n", "b.yml")
        code, _, err = self._run("validate", str(a), str(b))
        self.assertEqual(code, 1)
        self.assertIn("radar_id 'same' is also used by", err)

    def test_cli_show_json_roundtrips(self) -> None:
        import json

        code, out, _ = self._run("show", str(self._write(MINIMAL)), "--format", "json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["radar_id"], "my-radar")
        self.assertEqual(len(data["config_hash"]), 12)
        data.pop("config_hash")
        self.assertIsInstance(RadarConfig.model_validate(data), RadarConfig)


if __name__ == "__main__":
    unittest.main()
