import datetime as dt
import json
import random
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from carousel.core.config import ConfigError, RadarConfig, load_radar
from carousel.core.paper import SourceRecord
from carousel.database import PaperStore
from carousel.ranking import MODES, HistoryItem, LeakageError, check_no_leakage, effective_weights, rank
from carousel.ranking import ranker as ranker_mod
from carousel.search import HashingEmbedder, generate_candidates
from carousel.search.candidates import CandidateSet

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 1, tzinfo=UTC)
WINDOW = (dt.date(2026, 10, 1), dt.date(2026, 11, 30))


def radar(**kw) -> RadarConfig:
    base = dict(radar_name="Viral escape", description="Viral immune escape and antigenic drift",
                sources=["pubmed"], queries=["immune escape", "antigenic drift"],
                methods_profile=["deep mutational scanning", "phylodynamics", "neutralization assay"])
    base.update(kw)
    return RadarConfig.model_validate(base)


def rec(n: int, title: str, abstract: str = "", **kw) -> SourceRecord:
    base = dict(source="pubmed", source_id=str(43000000 + n), identifiers={"pmid": str(43000000 + n)},
                title=title, abstract=abstract or None, dates={"published": "2026-10-15"})
    base.update(kw)
    return SourceRecord.model_validate(base)


HISTORY = [
    rec(1, "Immune escape of SARS-CoV-2 Omicron sublineages", "Neutralization escape by spike mutations."),
    rec(2, "Antigenic drift of influenza H3N2 hemagglutinin", "Seasonal antigenic drift and vaccine mismatch."),
    rec(3, "Immune escape mutations in HIV envelope", "Antibody escape in chronic infection."),
    rec(4, "Antigenic cartography of dengue serotypes", "Antigenic maps of cross-reactive antibodies."),
]
NEW = [
    rec(10, "Immune escape of SARS-CoV-2 Omicron sublineages in vaccinees", "Neutralization escape by spike mutations.",
        dates={"published": "2026-11-02"}),
    rec(11, "Deep mutational scanning maps immune escape in RSV fusion protein",
        "Deep mutational scanning and neutralization assay of antigenic sites.", dates={"published": "2026-11-03"}),
    rec(12, "Phylodynamics of antigenic drift in avian influenza", "Phylodynamics reveals antigenic drift in wild birds.",
        dates={"published": "2026-11-03"}),
    rec(13, "Hospital catering satisfaction survey", "Food quality ratings among inpatients.", dates={"published": "2026-11-04"}),
]


class Fixture:
    """A store with four papers seen on day 0-3 and four new ones seen on day 10."""

    def __init__(self, path=":memory:") -> None:
        self.store = PaperStore(path)
        for i, r in enumerate(HISTORY):
            self.store.observe(r, T0 + dt.timedelta(days=i))
        self.new = [self.store.observe(r, T0 + dt.timedelta(days=10)).paper for r in NEW]
        self.emb = HashingEmbedder()

    def candidates(self, cfg: RadarConfig, **kw) -> CandidateSet:
        args = dict(profile=f"{cfg.radar_name} {cfg.description}", window=WINDOW, store=self.store, embedder=self.emb,
                    source_hits=[p.paper_id for p in self.new])
        args.update(kw)
        return generate_candidates(self.new, list(cfg.queries), **args)

    def rank(self, cfg: RadarConfig, **kw):
        return rank(self.candidates(cfg), cfg, store=self.store, embedder=self.emb, **kw)


class ConfigV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def load(self, text: str) -> RadarConfig:
        p = Path(self.tmp.name) / "r.yml"
        p.write_text(textwrap.dedent(text))
        return load_radar(p)

    def test_v2_fields(self) -> None:
        cfg = self.load("""\
            schema_version: 2
            radar_name: R
            sources: [pubmed, openalex]
            queries: [immune escape]
            mode: exploratory
            research_question: Which viral lineages escape current vaccines?
            methods_profile: [deep mutational scanning]
            seed_papers: ["https://doi.org/10.1038/S41586-020-2012-7"]
            publication_types: [Journal Article, Preprint]
            source_queries:
              pubmed: ['"immune evasion"[MeSH Terms]']
            """)
        self.assertEqual(cfg.mode.value, "exploratory")
        self.assertEqual(cfg.seed_papers, ["10.1038/s41586-020-2012-7"])
        self.assertEqual(cfg.method_terms(), ["deep mutational scanning"])

    def test_v1_files_still_load_and_default_to_balanced(self) -> None:
        cfg = load_radar(Path(__file__).resolve().parents[1] / "examples" / "viral_evolution.yml")
        self.assertEqual(cfg.mode.value, "balanced")
        self.assertEqual(cfg.method_terms(), ["phylodynamics", "phylogenetics", "molecular clock", "genomic epidemiology"])

    def test_errors(self) -> None:
        cases = {
            "schema_version: 1\nradar_name: R\nsources: [pubmed]\nqueries: [x]\nmode: focused\n": "need schema_version: 2",
            "radar_name: R\nsources: [pubmed]\nqueries: [x]\nseed_papers: [not-a-doi]\n": "not a DOI",
            "radar_name: R\nsources: [pubmed]\nqueries: [x]\nsource_queries:\n  openalex: [y]\n": "sources not in `sources`",
            "radar_name: R\nsources: [pubmed]\nqueries: [x]\nmode: wild\n": "mode",
            "schema_version: 3\nradar_name: R\nsources: [pubmed]\nqueries: [x]\n": "unsupported schema_version 3",
        }
        for text, want in cases.items():
            with self.subTest(want), self.assertRaises(ConfigError) as ctx:
                self.load(text)
            self.assertIn(want, str(ctx.exception))


class LeakageTests(unittest.TestCase):
    """Done-when for plan task 8: a leakage test fails if H_t contains later papers."""

    def test_guard_rejects_later_and_simultaneous_papers(self) -> None:
        t = T0 + dt.timedelta(days=5)
        check_no_leakage([HistoryItem("a", t - dt.timedelta(seconds=1), [1.0])], t)
        for later in (t, t + dt.timedelta(days=1)):
            with self.assertRaises(LeakageError):
                check_no_leakage([HistoryItem("b", later, [1.0])], t)

    def test_leaky_history_is_caught_in_a_real_ranking(self) -> None:
        f = Fixture()
        # Simulate a bug that hands the ranker *all* history, including papers seen after the candidate.
        f.store.observe(rec(20, "Immune escape of SARS-CoV-2 Omicron sublineages in vaccinees (later copy)"),
                        T0 + dt.timedelta(days=20))
        with mock.patch.object(ranker_mod, "_history_before", lambda history, t: list(history)):
            with self.assertRaises(LeakageError):
                f.rank(radar())

    def test_later_papers_never_change_novelty(self) -> None:
        f = Fixture()
        before = {i.paper_id: i.to_dict() for i in f.rank(radar()).items}
        # A near-duplicate of every new paper, observed afterwards.
        for i, r in enumerate(NEW):
            f.store.observe(rec(30 + i, r.title + ".", r.abstract or ""), T0 + dt.timedelta(days=30))
        after = {i.paper_id: i.to_dict() for i in f.rank(radar(), history=list(f.store.papers())).items}
        for pid, item in before.items():
            self.assertEqual(item["details"]["novelty_raw"], after[pid]["details"]["novelty_raw"], pid)
            self.assertEqual(item["details"]["novelty"]["nearest"], after[pid]["details"]["novelty"]["nearest"])

    def test_earlier_near_duplicate_lowers_novelty(self) -> None:
        f = Fixture()
        items = {i.title: i for i in f.rank(radar()).items}
        dup = items["Immune escape of SARS-CoV-2 Omicron sublineages in vaccinees"]
        fresh = items["Hospital catering satisfaction survey"]
        self.assertLess(dup.details["novelty_raw"], fresh.details["novelty_raw"])
        self.assertEqual(dup.details["novelty"]["nearest"][0], f.store.find("pmid", "43000001").paper_id)
        self.assertEqual(dup.details["novelty"]["history_size"], 4)


class DeterminismTests(unittest.TestCase):
    """Done-when for plan task 8: deterministic given a stored run."""

    def test_same_inputs_same_ranking(self) -> None:
        f = Fixture()
        cs = f.candidates(radar())
        a = rank(cs, radar(), store=f.store, embedder=f.emb).to_dict()
        shuffled = list(cs.candidates)
        random.Random(7).shuffle(shuffled)
        b = rank(CandidateSet(shuffled, cs.filtered, cs.stats), radar(), store=f.store, embedder=f.emb).to_dict()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_reopened_store_gives_identical_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(Path(d) / "s.db")
            def ranking_only(r):  # everything except the cache bookkeeping (embedded vs reused counts)
                d = r.to_dict()
                d["stats"] = {k: v for k, v in d["stats"].items() if k != "candidate_stats"}
                return json.dumps(d, sort_keys=True)

            first = ranking_only(f.rank(radar()))
            f.store.close()
            store = PaperStore(Path(d) / "s.db")
            emb = HashingEmbedder()
            new = [store.get(p.paper_id) for p in f.new]
            cs = generate_candidates(new, ["immune escape", "antigenic drift"],
                                     profile="Viral escape Viral immune escape and antigenic drift", window=WINDOW,
                                     store=store, embedder=emb, source_hits=[p.paper_id for p in new])
            again = ranking_only(rank(cs, radar(), store=store, embedder=emb))
            self.assertEqual(first, again)
            self.assertEqual(cs.stats["embedded"], 0)  # vectors came from the stored cache
            store.close()


class SignalAndModeTests(unittest.TestCase):
    def test_output_shape_and_versions(self) -> None:
        r = Fixture().rank(radar())
        self.assertEqual(r.ranking_version, "0.1.0")
        self.assertEqual(r.active_signals, ("relevance", "novelty", "method_match"))
        self.assertEqual(set(r.inactive_signals), {"momentum", "discovery", "evidence"})
        self.assertAlmostEqual(sum(r.weights.values()), 1.0)
        self.assertEqual(r.model_version, "carousel/hashing-v1-d256")
        top = {i.title: i for i in r.items}["Deep mutational scanning maps immune escape in RSV fusion protein"]
        self.assertEqual(top.details["methods_matched"], ["deep mutational scanning", "neutralization assay"])
        self.assertAlmostEqual(top.components["method_match"], 2 / 3)
        self.assertEqual(r.items[-1].title, "Hospital catering satisfaction survey")
        json.dumps(r.to_dict())

    def test_mode_weights(self) -> None:
        cfg = radar()
        active = ["relevance", "novelty", "method_match"]
        focused, exploratory = effective_weights(cfg, "focused", active), effective_weights(cfg, "exploratory", active)
        self.assertGreater(focused["relevance"], exploratory["relevance"])
        self.assertLess(focused["novelty"], exploratory["novelty"])
        self.assertGreater(MODES["focused"]["lambda"], MODES["exploratory"]["lambda"])
        self.assertEqual(Fixture().rank(radar(mode="exploratory")).mmr_lambda, 0.5)
        with self.assertRaises(ValueError):
            Fixture().rank(radar(), mode="wild")
        with self.assertRaises(ValueError):
            effective_weights(radar(ranking={"relevance": 0, "novelty": 0, "method_match": 0, "momentum": 1}),
                              "balanced", active)

    def test_mmr_demotes_near_duplicates(self) -> None:
        f = Fixture()
        twin = f.store.observe(rec(40, NEW[1].title + " (extended)", NEW[1].abstract or "",
                                   dates={"published": "2026-11-03"}), T0 + dt.timedelta(days=10)).paper
        pool = f.new + [twin]
        cfg = radar()
        cs = generate_candidates(pool, list(cfg.queries), profile="Viral escape", window=WINDOW, store=f.store,
                                 embedder=f.emb, source_hits=[p.paper_id for p in pool])
        explore = rank(cs, cfg, store=f.store, embedder=f.emb, mode="exploratory")
        pos = {i.paper_id: i for i in explore.items}
        rsv = next(p.paper_id for p in f.new if p.title.startswith("Deep mutational"))
        first, second = sorted([pos[twin.paper_id], pos[rsv]], key=lambda i: i.rank)
        self.assertGreater(second.redundancy, 0.8)  # measured against what was already selected
        self.assertGreater(second.rank - first.rank, 1)  # something else is placed between the twins

    def test_off_topic_papers_cannot_rank_on_novelty_or_diversity(self) -> None:
        # Found with the real model: an off-topic paper is maximally novel and redundant with nothing.
        f = Fixture()
        for mode in MODES:
            items = f.rank(radar(), mode=mode).items
            last = items[-1]
            self.assertEqual(last.title, "Hospital catering satisfaction survey", mode)
            self.assertIn("low_relevance", last.flags)
            self.assertEqual(last.details["novelty_gate"], 0.0)
            self.assertTrue(all("low_relevance" not in i.flags for i in items[:-1]))

    def test_lexical_only_run_reports_inactive_novelty(self) -> None:
        f = Fixture()
        cs = generate_candidates(f.new, ["immune escape", "antigenic drift"], window=WINDOW)
        r = rank(cs, radar())
        self.assertEqual(r.active_signals, ("relevance", "method_match"))
        self.assertIn("lexical-only", r.inactive_signals["novelty"])
        self.assertEqual(r.stats["relevance_formula"], "bm25_norm")
        self.assertIsNone(r.model_version)

    def test_first_scan_has_no_history(self) -> None:
        with PaperStore() as s:
            new = [s.observe(r, T0).paper for r in NEW]
            emb = HashingEmbedder()
            cs = generate_candidates(new, ["immune escape"], profile="Viral escape", window=WINDOW, store=s, embedder=emb)
            r = rank(cs, radar(methods_profile=[]), store=s, embedder=emb)
            self.assertEqual(r.active_signals, ("relevance",))
            self.assertIn("fewer than 3 earlier papers", r.inactive_signals["novelty"])
            self.assertIn("no methods_profile", r.inactive_signals["method_match"])

    def test_seed_papers_shape_profile_and_are_not_recommended(self) -> None:
        f = Fixture()
        f.store.observe(rec(50, "Antibody escape seed study", "Seed.", identifiers={"pmid": "43000050", "doi": "10.5555/seed"},
                            dates={"published": "2026-11-01"}), T0 + dt.timedelta(days=9))
        seed = f.store.find("doi", "10.5555/seed")
        cfg = radar(seed_papers=["10.5555/seed", "10.5555/absent"])
        cs = generate_candidates(f.new + [seed], list(cfg.queries), profile="x", window=WINDOW, store=f.store,
                                 embedder=f.emb, source_hits=[p.paper_id for p in f.new + [seed]])
        r = rank(cs, cfg, store=f.store, embedder=f.emb)
        self.assertNotIn(seed.paper_id, r.ids())
        self.assertEqual(r.stats["excluded"], {"seed_paper": [seed.paper_id]})
        self.assertEqual(r.stats["profile"]["seeds_used"], ["10.5555/seed"])
        self.assertEqual(r.stats["profile"]["seeds_missing"], ["10.5555/absent"])

    def test_integrity_is_a_flag_not_a_score(self) -> None:
        f = Fixture()
        notice = f.store.observe(rec(60, "Correction: Immune escape mutations in HIV envelope", "Corrected figure.",
                                     dates={"published": "2026-11-05"}), T0 + dt.timedelta(days=10)).paper
        retracted = f.store.observe(rec(61, "Immune escape claims later retracted", "Immune escape.", is_retracted=True,
                                        dates={"published": "2026-11-05"}), T0 + dt.timedelta(days=10)).paper
        pool = f.new + [notice, retracted]
        cs = generate_candidates(pool, ["immune escape"], profile="Viral escape", window=WINDOW, store=f.store,
                                 embedder=f.emb, exclude_retracted=False, source_hits=[p.paper_id for p in pool])
        items = {i.paper_id: i for i in rank(cs, radar(), store=f.store, embedder=f.emb).items}
        self.assertIn("correction_or_notice", items[notice.paper_id].flags)
        self.assertIn("retracted", items[retracted.paper_id].flags)
        self.assertNotIn("integrity", items[notice.paper_id].components)


if __name__ == "__main__":
    unittest.main()
