"""Crossref connector (plan task 16): DOI lookups, corrections and retractions, preprint relations.

Recorded-HTTP fixtures only (tests/fixtures/http/crossref_*.json, recorded by
record_fixtures.py); nothing here touches the network.
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

from carousel import Radar
from carousel.connectors import (Budget, ConnectorError, CrossrefConnector, HttpClient, ReplayTransport, Request,
                                 SearchQuery)
from carousel.connectors.crossref import jats_to_text, parse_work
from carousel.core.config import RadarConfig
from carousel.core.paper import SourceRecord
from carousel.database import PaperStore
from carousel.search.candidates import generate_candidates
from test_connectors import FIXTURES, replay
from test_scan import AS_OF, DAY1, connectors

sys.path.insert(0, str(FIXTURES))
from record_fixtures import KNOWN_DOIS  # noqa: E402

UTC = dt.timezone.utc
WAKEFIELD = "10.1016/s0140-6736(97)11096-0"
NOTICE = "10.1016/s0140-6736(10)60175-4"
ARTICLE, PREPRINT = "10.1021/acsomega.5c02989", "10.26434/chemrxiv-2025-8hnzz"


def crossref(name="crossref_lookup.json", **kw) -> CrossrefConnector:
    return CrossrefConnector(replay(name), mailto="", **kw)


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = {r.source_id: r for r in crossref().fetch(KNOWN_DOIS)}

    def test_unregistered_dois_are_skipped(self) -> None:
        self.assertEqual(sorted(self.records), sorted([ARTICLE, PREPRINT, NOTICE, WAKEFIELD, "10.1038/nature12373"]))

    def test_retraction_and_correction_from_retraction_watch(self) -> None:
        r = self.records[WAKEFIELD]
        self.assertTrue(r.is_retracted)
        self.assertEqual([(u.type, u.notice_doi, u.date, u.asserted_by) for u in r.updates], [
            ("correction", "10.1016/s0140-6736(04)15715-2", "2004-03-06", "retraction-watch"),
            ("retraction", NOTICE, "2010-02-06", "retraction-watch"),
        ])
        self.assertEqual((r.identifiers.doi, r.venue, r.dates.published), (WAKEFIELD, "The Lancet", "1998-02"))
        self.assertEqual(len(r.authors), 13)

    def test_a_notice_is_typed_as_one(self) -> None:
        r = self.records[NOTICE]
        self.assertEqual(r.publication_types, ["journal-article", "retraction"])
        self.assertFalse(r.is_retracted)  # the notice is not itself retracted

    def test_preprint_relations_both_ways(self) -> None:
        art, pre = self.records[ARTICLE], self.records[PREPRINT]
        self.assertEqual([(x.type, x.value) for x in art.relations], [("has-preprint", PREPRINT)])
        self.assertEqual([(x.type, x.value) for x in pre.relations], [("is-preprint-of", ARTICLE)])
        self.assertEqual(pre.publication_types, ["posted-content"])
        self.assertEqual(pre.dates.preprint_posted, "2025-02-28")
        self.assertIsNone(pre.dates.published)
        self.assertEqual((art.dates.published, art.dates.first_online), ("2025-06-10", "2025-06-02"))
        self.assertTrue(pre.abstract and "<" not in pre.abstract)  # JATS markup removed

    def test_no_updates(self) -> None:
        r = self.records["10.1038/nature12373"]
        self.assertEqual((r.updates, r.relations, r.is_retracted), ([], [], False))

    def test_jats_and_partial_dates(self) -> None:
        self.assertEqual(jats_to_text("<jats:title>Abstract</jats:title><jats:p>Viral <jats:italic>escape</jats:italic>.</jats:p>"),
                         "Viral escape .")
        rec = parse_work({"DOI": "10.1234/x", "type": "journal-article", "issued": {"date-parts": [[2026]]},
                          "updated-by": [{"type": "Expression of Concern", "DOI": "10.1234/eoc"}, {"label": "no type"}]})
        self.assertEqual((rec.dates.published, [u.type for u in rec.updates]), ("2026", ["expression_of_concern"]))
        self.assertIsNone(parse_work({"DOI": "not a doi"}))

    def test_requests_and_secrets(self) -> None:
        with self.assertRaises(ConnectorError):
            crossref().search(SearchQuery(terms=["x"], start=AS_OF, end=AS_OF))
        req = CrossrefConnector(HttpClient(), mailto="someone@example.org")._request({"filter": "doi:10.1/x"})
        self.assertNotIn("mailto", req.key())  # sent, but never part of a cache key or fixture
        self.assertIn("mailto", req.full_url())
        self.assertNotIn("mailto", Request.build("https://api.crossref.org/works").key())

    def test_unchanged_hash_for_records_without_updates(self) -> None:
        r = SourceRecord(source="pubmed", source_id="1", title="T")
        import hashlib
        import json

        content = r.model_dump(mode="json", exclude={"first_seen", "last_verified", "raw", "updates", "relations", "license", "open_access"})
        blob = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(r.content_sha256(), hashlib.sha256(blob.encode()).hexdigest())


class RetractionFlagsThePaperTests(unittest.TestCase):
    """Done-when: a retraction Crossref reports flags the paper (and keeps it out of candidates)."""

    def test_flag_and_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            t = dt.datetime(2026, 11, 24, tzinfo=UTC)
            pubmed = SourceRecord(source="pubmed", source_id="9500106", identifiers={"doi": WAKEFIELD, "pmid": "9500106"},
                                  title="Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and pervasive "
                                        "developmental disorder in children", dates={"published": "1998-02"})
            pid = st.observe(pubmed, t).paper.paper_id
            self.assertFalse(st.get(pid).is_retracted)

            for rec in crossref().fetch(KNOWN_DOIS):  # the recorded batch
                if rec.source_id == WAKEFIELD:
                    st.observe(rec, t)
            paper = st.get(pid)
            self.assertEqual(sorted(s.source.value for s in paper.sources), ["crossref", "pubmed"])  # same paper, by DOI
            self.assertTrue(paper.is_retracted)
            self.assertEqual(paper.provenance["is_retracted"].source.value, "crossref")
            self.assertEqual([(u.type, u.via) for u in paper.updates], [("correction", "crossref"), ("retraction", "crossref")])

            cands = generate_candidates([paper], ["colitis"])
            self.assertEqual((cands.ids(), cands.filtered.get("retracted")), ([], [pid]))


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "s.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def scan(self, cr: CrossrefConnector):
        cfg = RadarConfig.model_validate({
            "radar_id": "escape", "radar_name": "Escape radar", "sources": ["pubmed", "openalex", "crossref"],
            "queries": ["immune escape", "antigenic drift"], "exclude": ["case report"],
            "filters": {"date_range": "last_14_days"}})
        return Radar(cfg, store=self.db).scan(as_of=AS_OF, observed_at=DAY1, connectors={**connectors(), "crossref": cr},
                                              embedder="hashing", max_results=5)

    def test_scan_verifies_found_dois(self) -> None:
        res = self.scan(crossref("crossref_scan.json"))
        cov = {c["source"]: c for c in res.run["coverage"]}
        self.assertEqual(list(cov), ["pubmed", "openalex", "crossref"])  # verification runs after the searches
        c = cov["crossref"]
        self.assertEqual((c["status"], c["role"], c["total_available"], c["retrieved"], c["truncated"]),
                         ("ok", "verification", 10, 9, False))
        self.assertIn("1 of 10 DOIs are not registered with Crossref", c["errors"][0])
        self.assertEqual(res.run["status"], "ok")
        with PaperStore(self.db) as st:
            verified = [p for p in st.papers() if p.source("crossref")]
            self.assertEqual(len(verified), 9)
            self.assertTrue(all(p.identifiers.doi for p in verified))
            obs = st.get_run(res.run_id)["observations"]
            self.assertEqual(sum(1 for o in obs if o[0] == "crossref"), 9)  # (source, source_id, paper_id, status)

    def test_budget_truncates_with_reason(self) -> None:
        res = self.scan(CrossrefConnector(HttpClient(ReplayTransport(FIXTURES / "crossref_scan.json"),
                                                     budget=Budget(0, unit="requests")), mailto=""))
        c = [c for c in res.run["coverage"] if c["source"] == "crossref"][0]
        self.assertEqual((c["status"], c["truncated"], c["truncation_reason"], c["retrieved"]), ("ok", True, "budget", 0))


if __name__ == "__main__":
    unittest.main()
