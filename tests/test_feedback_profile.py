"""Feedback-adjusted interest profile and versioned explanations (plan task 19).

Done when: explanations carry a version; relevant/irrelevant feedback measurably changes the next ranking.
Recorded-HTTP fixtures only (the PubMed and OpenAlex search fixtures), hashing embeddings.
"""

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from carousel import Radar
from carousel import feedback as fb
from carousel.core.config import RadarConfig
from carousel.database import PaperStore
from carousel.ranking.explain import EXPLANATION_VERSION, explain
from test_scan import AS_OF, DAY1, connectors

CFG = RadarConfig.model_validate({
    "radar_id": "escape", "radar_name": "Escape radar", "description": "Viral and tumour immune escape",
    "sources": ["pubmed", "openalex"], "queries": ["immune escape", "antigenic drift"], "exclude": ["case report"],
    "filters": {"date_range": "last_14_days"}})
DAY2 = DAY1 + dt.timedelta(days=1)


def scan(d: Path, store: Path, feedback: dict, observed_at=DAY1):
    path = d / "escape.feedback.yml"
    fb.save(path, {**{a: [] for a in fb.ACTIONS}, **feedback}, "escape")
    return Radar(CFG, store=store, feedback_path=path).scan(as_of=AS_OF, observed_at=observed_at, connectors=connectors(),
                                                            embedder="hashing", max_results=5, top_n=10)


class FeedbackChangesTheRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        first = scan(d, d / "first.db", {})
        cls.first = first
        ids = [it["paper_id"] for it in first.items]
        cls.liked, cls.disliked = ids[2], ids[0]
        # The same store, rescanned the next day: once judging two papers, once only hiding the same two.
        for name in ("judged", "hidden"):
            shutil.copy(d / "first.db", d / f"{name}.db")
        cls.judged = scan(d, d / "judged.db", {"relevant": [cls.liked], "irrelevant": [cls.disliked]}, DAY2)
        cls.hidden = scan(d, d / "hidden.db", {"dismiss": [cls.liked, cls.disliked]}, DAY2)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_the_next_ranking_changes_beyond_hiding_the_judged_papers(self) -> None:
        judged = [it["paper_id"] for it in self.judged.items]
        hidden = [it["paper_id"] for it in self.hidden.items]
        self.assertNotIn(self.liked, judged)  # judged papers have been seen: they leave the list either way
        self.assertNotEqual(judged, hidden)   # ...but the order differs only because the profile moved
        stats = self.judged.run["ranking"]["stats"]
        effect = stats["feedback_effect"]
        self.assertGreater(effect["moved_up"] + effect["moved_down"] + len(effect["entered"]), 0)
        self.assertGreater(stats["profile"]["shift"], 0.0)
        self.assertEqual(stats["profile"]["feedback_used"], {"positive": [self.liked], "negative": [self.disliked]})
        self.assertIsNone(self.hidden.run["ranking"]["stats"]["feedback_effect"])  # dismiss only hides

    def test_each_paper_records_what_feedback_did_to_it(self) -> None:
        for it in self.judged.items:
            f = it["details"]["feedback"]
            change = it["components"]["relevance"] - f["relevance_without_feedback"]
            self.assertAlmostEqual(f["relevance_change"], change, places=5)
            # Relevance moves toward the paper marked relevant and away from the one marked irrelevant.
            pos, neg = f["nearest_positive"], f["nearest_negative"]
            self.assertEqual((pos["paper_id"], pos["action"]), (self.liked, "relevant"))
            self.assertEqual((neg["paper_id"], neg["action"]), (self.disliked, "irrelevant"))
        by_change = sorted(self.judged.items, key=lambda it: it["details"]["feedback"]["relevance_change"])
        lo, hi = by_change[0]["details"]["feedback"], by_change[-1]["details"]["feedback"]
        self.assertGreater(hi["nearest_positive"]["similarity"] - hi["nearest_negative"]["similarity"],
                           lo["nearest_positive"]["similarity"] - lo["nearest_negative"]["similarity"])

    def test_explanations_are_versioned_and_stored(self) -> None:
        self.assertEqual(self.judged.run["explanation_version"], EXPLANATION_VERSION)
        for it in self.judged.items:
            e = it["details"]["explanation"]
            self.assertEqual(e["version"], EXPLANATION_VERSION)
            self.assertTrue(e["summary"])
            self.assertEqual(e["reasons"][0]["kind"], "relevance")
            self.assertIn("feedback", {r["kind"] for r in e["reasons"]})
        self.assertNotIn("feedback", {r["kind"] for it in self.first.items for r in it["details"]["explanation"]["reasons"]})
        with PaperStore(Path(self.tmp.name) / "judged.db") as st:
            run = st.get_run(self.judged.run["run_id"])
            self.assertEqual(run["explanation_version"], EXPLANATION_VERSION)
            self.assertEqual(run["items"][0]["details"]["explanation"], self.judged.items[0]["details"]["explanation"])


class ExplainTests(unittest.TestCase):
    ITEM = {
        "rank": 2, "score": 0.5, "components": {"relevance": 0.6, "novelty": 0.8},
        "flags": ["corrected", "has_preprint"],
        "details": {"matched_terms": ["immune escape"], "bm25_norm": 1.0, "similarity": 0.3, "novelty_gate": 1.0,
                    "retrieved_by": ["lexical", "source_query"], "sources": ["crossref", "openalex", "pubmed"],
                    "novelty": {"history_size": 40},
                    "feedback": {"relevance_change": 0.04, "rank_without_feedback": None,
                                 "nearest_positive": {"paper_id": "cp_1", "title": "Liked", "action": "save", "similarity": 0.7},
                                 "nearest_negative": None},
                    "versions": [{"role": "preprint", "linked_by": ["europepmc"]}]},
    }

    def test_reasons(self) -> None:
        e = explain(self.ITEM, {"relevance": 0.5, "novelty": 0.5}, {})
        self.assertEqual(e["version"], EXPLANATION_VERSION)
        text = {r["kind"]: r["text"] for r in e["reasons"]}
        self.assertIn("immune escape", text["relevance"])
        self.assertIn("found by the search of OpenAlex, PubMed.", text["relevance"])  # Crossref only verifies
        self.assertIn("raised its relevance by 0.04 and brought it into this list", text["feedback"])
        self.assertIn("a paper you saved: “Liked”", text["feedback"])
        self.assertIn("40 papers", text["novelty"])
        self.assertEqual(text["integrity"], "A correction or erratum has been published for it.")
        self.assertIn("Europe PMC", text["version"])
        self.assertEqual(e["summary"], "Matches immune escape; raised by your feedback; unlike earlier papers")
        self.assertEqual([r["share"] for r in e["reasons"][:1]], [0.6])

    def test_small_changes_are_not_credited_to_feedback(self) -> None:
        item = {**self.ITEM, "details": {**self.ITEM["details"], "feedback": {"relevance_change": -0.004}}}
        e = explain(item, {"relevance": 1.0}, {"novelty": "lexical-only run"})
        self.assertEqual([r["text"] for r in e["reasons"] if r["kind"] == "feedback"],
                         ["Your feedback barely changed its relevance."])
        self.assertNotIn("feedback", e["summary"])


if __name__ == "__main__":
    unittest.main()
