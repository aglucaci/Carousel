import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from carousel.core.config import load_radar
from carousel.core.paper import SourceRecord
from carousel.core.text import search_tokens, stem
from carousel.database import PaperStore
from carousel.search import (
    BM25Index,
    HashingEmbedder,
    candidates_for_radar,
    cosine,
    ensure_embeddings,
    generate_candidates,
    matched_terms,
)
from carousel.search.embeddings import Embedder, pack, unpack

REPO = Path(__file__).resolve().parents[1]
T0 = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)
T1 = T0 + dt.timedelta(days=1)


def rec(n: int, title: str, abstract: str = "", **kw) -> SourceRecord:
    base = dict(source="pubmed", source_id=str(42000000 + n), identifiers={"pmid": str(42000000 + n)},
                title=title, abstract=abstract or None, dates={"published": "2026-11-10"})
    base.update(kw)
    return SourceRecord.model_validate(base)


CORPUS = [
    rec(1, "Immune escape of SARS-CoV-2 variants", "Neutralizing antibodies fail against emerging variants."),
    rec(2, "Hepatocellular carcinoma metabolism", "Tumour cells show immune escape through lactate."),
    rec(3, "Antigenic drift in influenza H3N2", "Hemagglutinin mutations accumulate each season."),
    rec(4, "Soil microbiome diversity in alpine meadows", "Bacterial communities vary with altitude."),
    rec(5, "Deep learning for protein structure", "Models predict folding from sequence."),
]


class CountingEmbedder(Embedder):
    """Wraps HashingEmbedder and counts every text it is asked to embed."""

    def __init__(self, model_version: str = "test/counting-v1") -> None:
        self.inner = HashingEmbedder(64)
        self.dim = 64
        self.model_version = model_version
        self.texts = []

    def embed(self, texts):
        self.texts.extend(texts)
        return self.inner.embed(texts)


class TextTests(unittest.TestCase):
    def test_light_stemming(self) -> None:
        cases = {"variants": "variant", "viruses": "virus", "studies": "study", "analysis": "analysis",
                 "class": "class", "virus": "virus", "patches": "patch", "cells": "cell", "2026": "2026"}
        for word, want in cases.items():
            self.assertEqual(stem(word), want, word)

    def test_search_tokens(self) -> None:
        self.assertEqual(search_tokens("The Immune-Escape of SARS‑CoV‑2 Variants"),
                         ["immune", "escape", "sars", "cov", "2", "variant"])


class BM25Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PaperStore()
        self.papers = [o.paper for o in self.store.observe_many(CORPUS, T0)]
        self.by_title = {p.title: p.paper_id for p in self.papers}

    def tearDown(self) -> None:
        self.store.close()

    def test_title_match_outranks_abstract_match(self) -> None:
        ranked = BM25Index.from_papers(self.papers).search(["immune escape"])
        self.assertEqual([pid for pid, _ in ranked][:2],
                         [self.by_title["Immune escape of SARS-CoV-2 variants"], self.by_title["Hepatocellular carcinoma metabolism"]])

    def test_scores_sum_over_terms_and_ties_are_deterministic(self) -> None:
        idx = BM25Index.from_papers(self.papers)
        one = dict(idx.search(["immune escape"]))
        both = dict(idx.search(["immune escape", "variants"]))
        pid = self.by_title["Immune escape of SARS-CoV-2 variants"]
        self.assertGreater(both[pid], one[pid])
        self.assertEqual(idx.search(["altitude meadows"]), idx.search(["altitude meadows"]))
        self.assertEqual(BM25Index([]).search(["x"]), [])

    def test_matched_terms_are_whole_phrases(self) -> None:
        p = next(p for p in self.papers if p.title.startswith("Immune escape"))
        self.assertEqual(matched_terms(p, ["immune escape", "variant", "escape mutant", "neutralizing antibody"]),
                         ["immune escape", "variant", "neutralizing antibody"])


class EmbeddingTests(unittest.TestCase):
    def test_hashing_embedder_properties(self) -> None:
        e = HashingEmbedder(128)
        a, b, c = e.embed(["immune escape of viral variants", "viral variants escape immunity", "alpine soil bacteria"])
        self.assertEqual(len(a), 128)
        self.assertAlmostEqual(cosine(a, a), 1.0, places=6)
        self.assertGreater(cosine(a, b), cosine(a, c))
        self.assertEqual(e.embed(["same text"]), e.embed(["same text"]))

    def test_pack_roundtrip(self) -> None:
        v = [0.5, -0.25, 0.125, 1e-3]
        self.assertEqual([round(x, 6) for x in unpack(pack(v))], [round(x, 6) for x in v])

    def test_bad_embedder_output_rejected(self) -> None:
        class Broken(CountingEmbedder):
            def embed(self, texts):
                return [[0.0] * 3 for _ in texts]

        with PaperStore() as s:
            papers = [o.paper for o in s.observe_many(CORPUS[:2], T0)]
            with self.assertRaises(ValueError):
                ensure_embeddings(s, papers, Broken())


class EmbeddingCacheTests(unittest.TestCase):
    """Done-when for plan task 7: the same paper is never re-embedded under the same model."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_never_reembedded_under_same_model(self) -> None:
        emb = CountingEmbedder()
        with PaperStore(self.path) as s:
            papers = [o.paper for o in s.observe_many(CORPUS, T0)]
            first, stats = ensure_embeddings(s, papers, emb)
            self.assertEqual((stats.embedded, stats.reused), (5, 0))
            again, stats = ensure_embeddings(s, papers, emb)
            self.assertEqual((stats.embedded, stats.reused), (0, 5))
            self.assertEqual(len(emb.texts), 5)
            for pid in first:
                self.assertEqual([round(x, 5) for x in first[pid]], [round(x, 5) for x in again[pid]])
        # ... and not after reopening the store either.
        with PaperStore(self.path) as s:
            _, stats = ensure_embeddings(s, list(s.papers()), emb)
            self.assertEqual(stats.embedded, 0)
            self.assertEqual(len(emb.texts), 5)
            self.assertEqual(s.stats()["embeddings"], {"test/counting-v1": 5})

    def test_changed_text_is_embedded_once_more(self) -> None:
        emb = CountingEmbedder()
        with PaperStore(self.path) as s:
            s.observe_many(CORPUS, T0)
            ensure_embeddings(s, list(s.papers()), emb)
            s.observe(rec(3, "Antigenic drift in influenza H3N2", "Corrected abstract from the source."), T1)
            _, stats = ensure_embeddings(s, list(s.papers()), emb)
            self.assertEqual((stats.embedded, stats.reused), (1, 4))
            _, stats = ensure_embeddings(s, list(s.papers()), emb)
            self.assertEqual(stats.embedded, 0)
            self.assertEqual(len(emb.texts), 6)

    def test_models_are_cached_side_by_side(self) -> None:
        a, b = CountingEmbedder("test/model-a"), CountingEmbedder("test/model-b")
        with PaperStore(self.path) as s:
            papers = [o.paper for o in s.observe_many(CORPUS, T0)]
            ensure_embeddings(s, papers, a)
            ensure_embeddings(s, papers, b)
            ensure_embeddings(s, papers, a)
            self.assertEqual((len(a.texts), len(b.texts)), (5, 5))
            self.assertEqual(s.stats()["embeddings"], {"test/model-a": 5, "test/model-b": 5})

    def test_papers_without_text_are_skipped(self) -> None:
        with PaperStore() as s:
            paper = s.observe(SourceRecord(source="openalex", source_id="W1", identifiers={"openalex": "W1"}), T0).paper
            vectors, stats = ensure_embeddings(s, [paper], CountingEmbedder())
            self.assertEqual((vectors, stats.skipped_no_text), ({}, 1))

    def test_merge_drops_absorbed_embeddings(self) -> None:
        emb = CountingEmbedder()
        with PaperStore() as s:
            a = s.observe(CORPUS[0], T0).paper
            b = s.observe(CORPUS[4], T0).paper
            ensure_embeddings(s, [a, b], emb)
            s.merge_papers(a.paper_id, b.paper_id)
            self.assertEqual(s.stats()["embeddings"], {"test/counting-v1": 1})


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PaperStore()
        extra = [
            rec(10, "Immune escape by retracted lineage", "Immune escape claims.", is_retracted=True),
            rec(11, "Immune escape in 2019 cohorts", "Old immune escape study.", dates={"published": "2019-05-01"}),
            rec(12, "Immune escape without a date", "Immune escape."),
            rec(13, "Immune escape case report in a child", "A case report of immune escape."),
            rec(14, "Immune escape in a review of reviews", "Immune escape.", publication_types=["Review"]),
        ]
        extra[2] = extra[2].model_copy(update={"dates": extra[2].dates.model_copy(update={"published": None})})
        self.papers = [o.paper for o in self.store.observe_many(CORPUS + extra, T0)]
        self.id_of = {p.title: p.paper_id for p in self.papers}
        self.window = (dt.date(2026, 11, 1), dt.date(2026, 11, 20))

    def tearDown(self) -> None:
        self.store.close()

    def run_candidates(self, **kw):
        args = dict(profile="Viral immune escape and antigenic drift", exclude=["case report"], window=self.window,
                    store=self.store, embedder=HashingEmbedder())
        args.update(kw)
        return generate_candidates(self.papers, ["immune escape", "antigenic drift"], **args)

    def test_hard_filters_record_reasons(self) -> None:
        cs = self.run_candidates()
        self.assertEqual(cs.filtered, {
            "retracted": [self.id_of["Immune escape by retracted lineage"]],
            "outside_date_window": [self.id_of["Immune escape in 2019 cohorts"]],
            "no_publication_date": [self.id_of["Immune escape without a date"]],
            "excluded_term:case report": [self.id_of["Immune escape case report in a child"]],
        })
        self.assertTrue(set(cs.ids()).isdisjoint(sum(cs.filtered.values(), [])))
        reviews = self.run_candidates(publication_types=["review"])
        self.assertEqual(reviews.ids(), [self.id_of["Immune escape in a review of reviews"]])
        self.assertEqual(self.run_candidates(exclude_retracted=False).filtered.get("retracted"), None)

    def test_retrieval_routes_and_features(self) -> None:
        soil = self.id_of["Soil microbiome diversity in alpine meadows"]
        cs = self.run_candidates(source_hits=[soil])
        by_id = {c.paper_id: c for c in cs.candidates}
        top = by_id[self.id_of["Immune escape of SARS-CoV-2 variants"]]
        self.assertIn("lexical", top.retrieved_by)
        self.assertEqual(top.matched_terms, ("immune escape",))
        self.assertIsNotNone(top.similarity)
        self.assertIn("source_query", by_id[soil].retrieved_by)  # source query hits always count
        drift = by_id[self.id_of["Antigenic drift in influenza H3N2"]]
        self.assertEqual(drift.lexical_rank is not None, True)
        self.assertEqual(cs.stats["model_version"], "carousel/hashing-v1-d256")
        json.dumps([c.features() for c in cs.candidates])  # persistable
        self.assertEqual(cs.ids(), self.run_candidates(source_hits=[soil]).ids())  # deterministic

    def test_lexical_only_without_embedder(self) -> None:
        cs = self.run_candidates(embedder=None)
        self.assertTrue(all(c.retrieved_by == ("lexical",) and c.similarity is None for c in cs.candidates))
        self.assertIsNone(cs.stats["model_version"])
        self.assertNotIn(self.id_of["Soil microbiome diversity in alpine meadows"], cs.ids())

    def test_limits(self) -> None:
        cs = self.run_candidates(lexical_k=1, semantic_k=0)
        self.assertEqual(len(cs.candidates), 1)
        # "antigenic drift" occurs in one paper, "immune escape" in eight: the rarer term carries more weight.
        self.assertEqual(cs.candidates[0].paper.title, "Antigenic drift in influenza H3N2")
        strict = self.run_candidates(embedder=HashingEmbedder(), min_similarity=0.99, lexical_k=0)
        self.assertEqual(strict.candidates, [])

    def test_candidates_for_radar(self) -> None:
        radar = load_radar(REPO / "examples" / "viral_evolution.yml")
        cs = candidates_for_radar(radar, self.papers, as_of=dt.date(2026, 11, 20), store=self.store,
                                  embedder=HashingEmbedder())
        self.assertIn(self.id_of["Immune escape of SARS-CoV-2 variants"], cs.ids())
        self.assertEqual(cs.stats["pool"], len(self.papers))


class OptionalDependencyTests(unittest.TestCase):
    def test_semantic_extra_message(self) -> None:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            from carousel.search import SentenceTransformerEmbedder

            with self.assertRaises(ImportError) as ctx:
                SentenceTransformerEmbedder()
            self.assertIn("carousel-research[semantic]", str(ctx.exception))
        else:  # pragma: no cover
            self.skipTest("sentence-transformers is installed")


if __name__ == "__main__":
    unittest.main()
