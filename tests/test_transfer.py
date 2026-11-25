"""Method Transfer v1 (plan task 35): the discovery signal, as a hypothesis with a grounded quote.

Done when: a fixture with known transfers recovers them and ranks near-field method papers below them; every
explanation has a quote that the grounding check verifies; the UI and digests label it a hypothesis, never a finding;
integrity flags still exclude.

The fixture: radar "escape" studies influenza antibody escape and lists deep mutational scanning (DMS) as a method.
Radar "soil" scans the soil microbiome. Two soil papers use DMS: the **known transfers**. Three influenza papers use DMS
too: **near-field** method papers. One more soil DMS paper has a published correction (an integrity flag). Both radars'
papers are in one store, and the shared topic map is fitted over them before the scan.
"""

import datetime as dt
import random
import tempfile
import unittest
from pathlib import Path

from carousel import Radar
from carousel.core.config import RadarConfig
from carousel.core.paper import SourceRecord, UpdateNotice
from carousel.database.local import PaperStore
from carousel.intelligence.shared_topics import fit_shared
from carousel.notifications.digest import digest_from_run, to_markdown
from carousel.ranking.transfer import transfer_context
from carousel.summarization.grounding import check_claim
from carousel.summarization.passages import from_abstract
from test_shared_topics import add_radar
from test_topics import EMB, GROUPS

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 24, tzinfo=UTC)
SCAN = dt.datetime(2026, 11, 26, 6, tzinfo=UTC)
DMS = "deep mutational scanning"
QUESTION = "Which hemagglutinin mutations let influenza escape neutralizing antibodies?"
RADAR = {"radar_id": "escape", "radar_name": "Flu escape", "sources": ["pubmed"], "queries": ["influenza", "hemagglutinin"],
         "methods_profile": [DMS], "research_question": QUESTION, "filters": {"date_range": "last_7_days"}}


def text(group: str, seed: str, method_sentence: str = "") -> tuple:
    rng = random.Random(seed)
    words = GROUPS[group].split()
    title = " ".join(rng.sample(words, 6))
    abstract = " ".join(rng.choice(words) for _ in range(30)) + "."
    return title, (f"{method_sentence} {abstract}" if method_sentence else abstract)


def observe(st, pid, group, method_sentence="", updates=()):
    title, abstract = text(group, pid, method_sentence)
    rec = SourceRecord(source="pubmed", source_id=pid, title=title, abstract=abstract, dates={"published": "2026-11-20"},
                       updates=list(updates))
    return st.observe(rec, T0 + dt.timedelta(days=1)).paper.paper_id


def build(path: Path) -> dict:
    """The store: both radars' earlier papers, the shared map, then this week's papers (with the known answers)."""
    st = PaperStore(path)
    add_radar(st, "escape", [("flu", 0)], n=20)
    add_radar(st, "soil", [("soil", 0), ("coral", 0)], n=20)
    fit_shared(st, EMB, "2026-11-24T12:00:00Z")
    ids = {
        "transfers": [observe(st, "soil_dms_1", "soil", "We applied deep mutational scanning to a rhizosphere nitrogenase."),
                      observe(st, "soil_dms_2", "soil", "Deep mutational scanning of a fungal network enzyme mapped tolerance.")],
        "near": [observe(st, f"flu_dms_{i}", "flu", "We used deep mutational scanning of hemagglutinin.") for i in range(3)],
        "corrected": observe(st, "soil_dms_corr", "soil", "Deep mutational scanning of root exudate transporters.",
                             updates=[UpdateNotice(type="correction", notice_doi="10.1000/carousel.correction")]),
        "flu": [observe(st, f"flu_new_{i}", "flu") for i in range(6)],
        "soil": [observe(st, f"soil_new_{i}", "soil") for i in range(4)],
    }
    st.close()
    return ids


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "s.db"
        cls.ids = build(cls.db)
        cls.res = Radar(RadarConfig.model_validate(RADAR)).scan(as_of=SCAN.date(), observed_at=SCAN, store=cls.db,
                                                              connectors={}, embedder=EMB, top_n=25)
        cls.items = {it["paper_id"]: it for it in cls.res.items}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_known_transfers_are_recovered_and_near_field_method_papers_rank_below(self) -> None:
        run = self.res.run
        self.assertEqual((run["ranking_version"], run["explanation_version"]), ("0.3.0", "3"))
        self.assertIn("discovery", run["ranking"]["active_signals"])
        hyp = [pid for pid, it in self.items.items() if "method_transfer_hypothesis" in it["flags"]]
        self.assertEqual(sorted(hyp), sorted(self.ids["transfers"]))  # exactly the known transfers
        for pid in self.ids["transfers"]:
            t = self.items[pid]["details"]["transfer"]
            self.assertEqual((t["status"], t["method"]), ("transfer", DMS))
            self.assertGreater(self.items[pid]["components"]["discovery"], 0)
            self.assertIn("method", self.items[pid]["details"]["retrieved_by"])  # found through the method channel
        for pid in self.ids["near"]:  # the same method in the radar's own field: no transfer, and lower discovery
            it = self.items[pid]
            self.assertEqual((it["details"]["transfer"]["status"], it["components"]["discovery"]), ("near_field", 0.0))
            self.assertNotIn("method_transfer_hypothesis", it["flags"])
        by_discovery = sorted(self.items.values(), key=lambda it: -(it["components"].get("discovery") or 0))
        self.assertEqual({it["paper_id"] for it in by_discovery[:2]}, set(self.ids["transfers"]))
        others = [it for it in self.items.values() if it["paper_id"] in self.ids["soil"]]
        self.assertTrue(all(it["details"]["transfer"]["status"] == "no_radar_method" for it in others))

    def test_every_transfer_cites_a_quote_the_grounding_check_verifies(self) -> None:
        with PaperStore(self.db) as st:
            for pid in self.ids["transfers"]:
                t = self.items[pid]["details"]["transfer"]
                p = st.get(pid)
                passages = from_abstract(p.abstract, p.title)
                claim = {"text": f"uses {t['method']}", "evidence": [{"passage_id": t["passage_id"], "quote": t["quote"]}]}
                self.assertEqual(check_claim(claim, passages), "")
                self.assertIn("deep mutational scanning", t["quote"].lower())
                bad = {"text": "x", "evidence": [{"passage_id": t["passage_id"], "quote": "invented words"}]}
                self.assertIn("not found verbatim", check_claim(bad, passages))

    def test_explanations_and_digests_say_hypothesis(self) -> None:
        for pid in self.ids["transfers"]:
            e = self.items[pid]["details"]["explanation"]
            self.assertTrue(e["summary"].startswith("Method transfer (a hypothesis): deep mutational scanning from"))
            r = [x for x in e["reasons"] if x["kind"] == "method_transfer"][0]
            self.assertTrue(r["hypothesis"])
            self.assertTrue(r["text"].startswith("Hypothesis, not a finding: it uses deep mutational scanning"))
            self.assertIn(QUESTION, r["text"])
            self.assertIn(r["quote"]["text"], r["text"])
        md = to_markdown(digest_from_run(self.res.run, self.res.items, radar_name="Flu escape", arrivals=None, executed_at=SCAN))
        self.assertEqual(md.count("**Method transfer: a hypothesis**"), 2)
        self.assertNotIn("finding", md.split("Method transfer")[0].lower())

    def test_integrity_flags_still_exclude(self) -> None:
        it = self.items.get(self.ids["corrected"])
        self.assertIsNotNone(it)
        self.assertIn("corrected", it["flags"])
        self.assertEqual((it["details"]["transfer"]["status"], it["components"]["discovery"]), ("integrity_flag", 0.0))
        self.assertNotIn("method_transfer_hypothesis", it["flags"])

    def test_no_shared_map_means_no_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            st = PaperStore(Path(d) / "s.db")
            add_radar(st, "escape", [("flu", 0)], n=20)
            st.close()
            res = Radar(RadarConfig.model_validate(RADAR)).scan(as_of=SCAN.date(), observed_at=SCAN, store=Path(d) / "s.db",
                                                                connectors={}, embedder=EMB)
        self.assertNotIn("discovery", res.run["ranking"]["active_signals"])
        self.assertIn("shared topic map", res.run["ranking"]["inactive_signals"]["discovery"])
        self.assertTrue(all("transfer" not in it["details"] for it in res.items))

    def test_a_map_fitted_after_the_scan_is_not_used(self) -> None:
        with PaperStore(self.db) as st:
            ctx, why = transfer_context(st, "escape", QUESTION, dt.datetime(2026, 11, 24, 6, tzinfo=UTC))
        self.assertIsNone(ctx)
        self.assertEqual(why, "the shared topic map was fitted after this scan")


if __name__ == "__main__":
    unittest.main()
