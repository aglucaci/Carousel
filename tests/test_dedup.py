import contextlib
import datetime as dt
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from carousel import cli
from carousel.core.paper import CanonicalPaper, SourceRecord
from carousel.database import PaperStore, StoreError
from carousel.ingestion.dedup import AUTO_MERGE_TITLE, compare, family_name, normalize_title

REPO = Path(__file__).resolve().parents[1]
PAIRS = yaml.safe_load((REPO / "tests" / "fixtures" / "dedup" / "pairs.yml").read_text())
T0 = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)
T1 = T0 + dt.timedelta(hours=1)


def record(spec: dict) -> SourceRecord:
    spec = dict(spec)
    spec["authors"] = [{"name": n} for n in spec.get("authors", [])]
    return SourceRecord.model_validate(spec)


def paper(spec: dict, pid: str) -> CanonicalPaper:
    r = record(spec).model_copy(update={"first_seen": T0, "last_verified": T0})
    return CanonicalPaper.from_records(pid, [r])


class FixtureSuiteTests(unittest.TestCase):
    def test_suite_covers_all_three_outcomes(self) -> None:
        counts = {e: sum(p["expect"] == e for p in PAIRS) for e in ("merge", "review", "distinct")}
        self.assertTrue(all(n >= 6 for n in counts.values()), counts)
        self.assertEqual(len({p["id"] for p in PAIRS}), len(PAIRS))

    def test_pairs_have_no_shared_identifiers(self) -> None:
        for p in PAIRS:
            a, b = record(p["a"]), record(p["b"])
            self.assertFalse(set(a.identifiers.items()) & set(b.identifiers.items()), p["id"])

    def test_decisions_and_reasons(self) -> None:
        for p in PAIRS:
            with self.subTest(p["id"]):
                d = compare(paper(p["a"], "cp_a"), paper(p["b"], "cp_b"))
                self.assertEqual((d.decision, d.reasons[0]), (p["expect"], p["reason"]), d.features)

    def test_decisions_are_symmetric(self) -> None:
        for p in PAIRS:
            with self.subTest(p["id"]):
                ab = compare(paper(p["a"], "cp_a"), paper(p["b"], "cp_b"))
                ba = compare(paper(p["b"], "cp_b"), paper(p["a"], "cp_a"))
                self.assertEqual((ab.decision, ab.reasons, ab.score), (ba.decision, ba.reasons, ba.score))

    def test_no_automatic_merge_below_threshold(self) -> None:
        for p in PAIRS:
            d = compare(paper(p["a"], "cp_a"), paper(p["b"], "cp_b"))
            f = d.features
            gates = (
                f["title_similarity"] >= AUTO_MERGE_TITLE,
                f["first_author_match"] is True,
                f["year_gap"] is not None and f["year_gap"] <= 1,
                not f["identifier_conflicts"],
                f["preprint"][0] == f["preprint"][1],
                f["notice"] == [False, False],
                not f["shared_sources"],
                f["numbers_match"],
                not f["title_generic"],
            )
            self.assertEqual(d.is_merge, all(gates), (p["id"], f))

    def test_store_outcome_matches_label(self) -> None:
        for p in PAIRS:
            with self.subTest(p["id"]), PaperStore() as store:
                a = store.observe(record(p["a"]), T0)
                b = store.observe(record(p["b"]), T1)
                if p["expect"] == "merge":
                    self.assertEqual((b.status, b.match), ("new_source", "fuzzy"))
                    self.assertEqual(b.paper.paper_id, a.paper.paper_id)
                    self.assertEqual(len(b.paper.sources), 2)
                    self.assertEqual(store.stats()["review_pending"], 0)
                elif p["expect"] == "review":
                    self.assertEqual(b.status, "new_paper")
                    self.assertEqual(len(b.queued), 1)
                    self.assertEqual(store.review_queue()[0].reasons[0], p["reason"])
                else:
                    self.assertEqual(b.status, "new_paper")
                    self.assertEqual(store.stats()["review_pending"], 0)
                    self.assertEqual(store.stats()["papers"], 2)


class NormalizationTests(unittest.TestCase):
    def test_title_normalization(self) -> None:
        self.assertEqual(normalize_title("Escape of <i>SARS‑CoV‑2</i> β-variants: café &amp; co."),
                         "escape of sars cov 2 beta variants cafe co")
        self.assertEqual(normalize_title(None), "")

    def test_family_names(self) -> None:
        for name in ("Ada Lovelace", "Lovelace A", "Lovelace, Ada", "Lovelace AB", "A. Lovelace"):
            self.assertEqual(family_name(name), "lovelace", name)
        self.assertEqual({family_name(n) for n in ("van der Berg J", "Jan van der Berg")}, {"berg"})
        self.assertIsNone(family_name("COVID-19 Genomics UK Consortium"))


def by_id(pid: str) -> dict:
    return next(p for p in PAIRS if p["id"] == pid)


class StoreDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PaperStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_auto_merge_is_logged_and_reversible(self) -> None:
        p = by_id("case_and_trailing_period")
        a = self.store.observe(record(p["a"]), T0).paper
        self.store.observe(record(p["b"]), T1)
        log = self.store.merge_log()
        self.assertEqual([(e["action"], e["method"], e["survivor"]) for e in log], [("merge", "auto_fuzzy", a.paper_id)])
        self.assertGreaterEqual(log[0]["detail"]["features"]["title_similarity"], AUTO_MERGE_TITLE)

        left, new = self.store.split("openalex", "W5000000001", note="different cohorts", at=T1)
        self.assertEqual([r.source.value for r in left.sources], ["pubmed"])
        self.assertEqual([r.source.value for r in new.sources], ["openalex"])
        self.assertEqual(self.store.stats()["papers"], 2)
        self.assertEqual(self.store.review_queue("distinct")[0].note, "different cohorts")
        self.assertEqual(self.store.merge_log()[-1]["action"], "split")
        # The absorbed paper's old ID is a live paper again, not a redirect.
        self.assertEqual(self.store.resolve(new.paper_id), new.paper_id)

    def test_reviewer_merge_and_redirect(self) -> None:
        p = by_id("preprint_and_published_version")
        pre = self.store.observe(record(p["a"]), T0).paper
        pub = self.store.observe(record(p["b"]), T1).paper
        [item] = self.store.review_queue()
        self.assertEqual(item.kind, "fuzzy")
        self.assertEqual({item.title_a, item.title_b}, {pre.title})

        merged = self.store.merge_papers(pub.paper_id, pre.paper_id, note="same study", at=T1)
        self.assertEqual(sorted(r.source.value for r in merged.sources), ["biorxiv", "pubmed"])
        self.assertEqual(self.store.get(pre.paper_id), merged)  # old ID redirects
        self.assertEqual(self.store.find("doi", "10.1101/2026.03.02.123456").paper_id, pub.paper_id)
        self.assertEqual(self.store.review_queue(), [])
        self.assertEqual(self.store.review_queue("merged")[0].note, "same study")
        self.assertEqual(self.store.stats()["merged_away"], 1)
        self.assertEqual(self.store.paper_ids(), [pub.paper_id])
        with self.assertRaises(StoreError):
            self.store.merge_papers(pub.paper_id, pre.paper_id)  # resolves to the same paper

    def test_distinct_pairs_are_not_requeued(self) -> None:
        p = by_id("author_order_changed")
        a = self.store.observe(record(p["a"]), T0).paper
        b = self.store.observe(record(p["b"]), T1).paper
        self.store.mark_distinct(a.paper_id, b.paper_id, note="separate preprints")
        self.assertEqual(self.store.review_queue(), [])
        # A re-observation (even with a changed title) does not reopen a decided pair.
        self.store.observe(record({**p["b"], "title": p["b"]["title"] + "."}), T1 + dt.timedelta(hours=1))
        self.assertEqual(self.store.review_queue(), [])

    def test_weaker_identifier_match_refused_when_dois_disagree(self) -> None:
        a = self.store.observe(SourceRecord(source="pubmed", source_id="41009999", title="Paper A",
                                            identifiers={"pmid": "41009999", "doi": "10.1000/a"}), T0).paper
        bad = SourceRecord(source="openalex", source_id="W7", title="Paper B",
                           identifiers={"pmid": "41009999", "doi": "10.1000/b", "openalex": "W7"})
        obs = self.store.observe(bad, T1)
        self.assertEqual(obs.status, "new_paper")
        self.assertNotEqual(obs.paper.paper_id, a.paper_id)
        kinds = {(i.kind, i.reasons[0]) for i in self.store.review_queue()}
        self.assertIn(("identifier_conflict", "doi_disagrees_with_matching_pmid"), kinds)
        self.assertEqual(len(self.store.get(a.paper_id).sources), 1)

    def test_identifier_match_still_wins_without_doi_clash(self) -> None:
        a = self.store.observe(SourceRecord(source="pubmed", source_id="41008888", title="Paper A",
                                            identifiers={"pmid": "41008888", "doi": "10.1000/a"}), T0).paper
        obs = self.store.observe(SourceRecord(source="openalex", source_id="W8", title="Completely different title",
                                              identifiers={"pmid": "41008888", "openalex": "W8"}), T1)
        self.assertEqual((obs.status, obs.match, obs.paper.paper_id), ("new_source", "identifier", a.paper_id))

    def test_split_refuses_only_record(self) -> None:
        self.store.observe(record(by_id("year_missing")["a"]), T0)
        with self.assertRaises(StoreError):
            self.store.split("pubmed", "41000209")


class MigrationTests(unittest.TestCase):
    def test_v1_store_is_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v1.db"
            with PaperStore(path) as s:
                s.observe(record(by_id("subtitle_dropped")["a"]), T0)
            con = sqlite3.connect(path)
            con.executescript("""
                DROP TABLE title_tokens; DROP TABLE review_queue; DROP TABLE merge_log;
                DROP INDEX papers_merged_into; ALTER TABLE papers DROP COLUMN merged_into;
                UPDATE meta SET value = '1' WHERE key = 'schema_version';
            """)
            con.commit()
            con.close()
            with PaperStore(path) as s:
                self.assertEqual(s.db.execute("SELECT value FROM meta").fetchone()[0], "2")
                obs = s.observe(record(by_id("subtitle_dropped")["b"]), T1)  # needs the backfilled title index
                self.assertEqual(len(obs.queued), 1)


class ReviewCliTests(unittest.TestCase):
    def run_cli(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_merge_distinct_split(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "store.db")
            with PaperStore(db) as s:
                p = by_id("reworded_title")
                a = s.observe(record(p["a"]), T0).paper.paper_id
                b = s.observe(record(p["b"]), T1).paper.paper_id

            code, out, _ = self.run_cli("review", "list", db)
            self.assertEqual(code, 0)
            self.assertIn("below_auto_merge_threshold", out)
            self.assertIn("1 item(s)", out)
            code, out, _ = self.run_cli("review", "list", db, "--format", "json")
            self.assertEqual(json.loads(out)[0]["kind"], "fuzzy")

            code, out, _ = self.run_cli("review", "merge", db, a, b, "--note", "same paper")
            self.assertEqual(code, 0)
            self.assertIn("2 source records", out)
            code, out, _ = self.run_cli("review", "split", db, "openalex", "W5000000203")
            self.assertEqual(code, 0)
            code, _, err = self.run_cli("review", "distinct", db, a, a)
            self.assertEqual(code, 1)
            self.assertIn("FAIL", err)
            code, _, err = self.run_cli("review", "list", str(Path(d) / "missing.db"))
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
