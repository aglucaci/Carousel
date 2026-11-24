"""Europe PMC connector (plan task 17): search, licences, full text kept only under a reusable licence, annotations.

Recorded-HTTP fixtures only (tests/fixtures/http/europepmc_*.json, recorded by record_fixtures.py).
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

from carousel import Radar
from carousel.connectors import EuropePMCConnector, HttpClient, ReplayTransport
from carousel.connectors.europepmc import build_query, jats_license, parse_result, summarize_annotations
from carousel.core.config import RadarConfig
from carousel.core.fulltext import is_reusable, normalize_license, reuse_of
from carousel.database import PaperStore
from test_connectors import FIXTURES, replay
from test_scan import AS_OF, DAY1, connectors

sys.path.insert(0, str(FIXTURES))
from record_fixtures import EPMC_ANNOTATED, EPMC_FULL_TEXTS, EPMC_RECORDS, QUERY  # noqa: E402

UTC = dt.timezone.utc


def epmc(name: str, **kw) -> EuropePMCConnector:
    return EuropePMCConnector(replay(name), **kw)


class LicenceTests(unittest.TestCase):
    def test_normalize_and_reuse(self) -> None:
        cases = {
            "https://creativecommons.org/licenses/by/4.0/": ("cc-by-4.0", "open"),
            "cc by": ("cc-by", "open"),
            "CC BY-NC 4.0": ("cc-by-nc-4.0", "noncommercial"),
            "cc by-nc-nd": ("cc-by-nc-nd", "no_derivatives"),
            "https://creativecommons.org/publicdomain/zero/1.0/": ("cc0-1.0", "open"),
            "Elsevier user license": ("elsevier user license", "restricted"),
            None: (None, "unknown"),
        }
        for raw, (norm, reuse) in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual((normalize_license(raw), reuse_of(normalize_license(raw))), (norm, reuse))
        self.assertTrue(is_reusable("cc-by-nc-nd"))
        self.assertFalse(is_reusable("elsevier user license"))
        self.assertFalse(is_reusable(None))

    def test_jats_license_prefers_the_documents_own(self) -> None:
        import xml.etree.ElementTree as ET

        doc = ET.fromstring('<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><permissions>'
                            '<license xlink:href="https://creativecommons.org/licenses/by-nc/4.0/"><license-p>Open</license-p>'
                            '</license></permissions></front></article>')
        self.assertEqual(jats_license(doc), ("cc-by-nc-4.0", "https://creativecommons.org/licenses/by-nc/4.0/"))
        self.assertEqual(jats_license(ET.fromstring("<article/>")), (None, None))


class SearchTests(unittest.TestCase):
    def test_query_paging_and_records(self) -> None:
        q = build_query(QUERY)
        self.assertEqual(q, '((TITLE_ABS:"immune escape" OR TITLE_ABS:"antigenic drift") NOT (TITLE_ABS:"case report")) '
                            'AND (FIRST_PDATE:[2026-10-01 TO 2026-10-14]) AND (SRC:MED OR SRC:PMC OR SRC:PPR)')
        res = epmc("europepmc_search.json", page_size=3).search(QUERY)
        cov = res.coverage
        self.assertEqual((cov.total_available, cov.retrieved, cov.pages, cov.truncated, cov.truncation_reason),
                         (61, 5, 2, True, "max_results"))
        self.assertEqual(cov.native_query, q)
        by_id = {r.source_id: r for r in res.records}
        pre = by_id["PPR:PPR1338286"]
        self.assertEqual((pre.dates.preprint_posted, pre.dates.published, pre.license), ("2026-10-08", None, "cc-by"))
        art = by_id["MED:42831628"]
        self.assertEqual((art.identifiers.pmid, art.identifiers.pmcid, art.identifiers.doi),
                         ("42831628", "PMC13776638", "10.1128/jvi.01316-26"))
        self.assertEqual((art.license, art.open_access, art.dates.published, art.dates.first_online),
                         ("cc-by", True, "2026-11", "2026-10-05"))
        self.assertTrue(all(r.title and "<" not in r.title for r in res.records))
        self.assertEqual(art.url, "https://europepmc.org/article/MED/42831628")

    def test_retraction_from_comment_corrections(self) -> None:
        [r] = epmc("europepmc_extras.json").fetch(EPMC_RECORDS)
        self.assertTrue(r.is_retracted)
        self.assertEqual([(u.type, u.notice_doi) for u in r.updates], [
            ("retraction", "10.1016/s0140-6736(04)15715-2"), ("retraction", "10.1016/s0140-6736(10)60175-4"),
            ("expression_of_concern", "10.1097/meg.0b013e328349d184")])
        self.assertEqual(r.identifiers.doi, "10.1016/s0140-6736(97)11096-0")
        self.assertIsNone(parse_result({"source": "MED"}))


class FullTextTests(unittest.TestCase):
    """Done-when: a licence is recorded per text."""

    def test_licensed_text_kept_with_its_licence(self) -> None:
        ep = epmc("europepmc_extras.json")
        text = ep.full_text(EPMC_FULL_TEXTS[0])
        self.assertEqual((text.source, text.source_id, text.kind, text.format), ("europepmc", "PMC13778503", "full_text", "jats"))
        self.assertEqual((text.license, text.license_url, text.license_source, text.reuse),
                         ("cc-by-4.0", "https://creativecommons.org/licenses/by/4.0/", "text", "open"))
        self.assertEqual(text.sections[0].title, "Introduction")
        self.assertTrue(any(s.title.startswith("Methods / ") for s in text.sections))  # nested sections keep their path
        self.assertGreater(len(text.text), 10_000)
        self.assertIsNone(ep.full_text(EPMC_FULL_TEXTS[1]))  # not kept: not open access, or no reusable licence
        self.assertIsNone(ep.full_text("not a pmcid"))

    def test_store_round_trip(self) -> None:
        text = epmc("europepmc_extras.json").full_text(EPMC_FULL_TEXTS[0])
        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            from carousel.core.paper import SourceRecord

            pid = st.observe(SourceRecord(source="pubmed", source_id="43054306", identifiers={"pmcid": "PMC13778503"},
                                          title="T"), dt.datetime(2026, 11, 24, tzinfo=UTC)).paper.paper_id
            st.save_text(pid, text)
            [back] = st.texts(pid)
            self.assertEqual((back.license, back.reuse, back.sha256()), (text.license, text.reuse, text.sha256()))
            self.assertTrue(st.has_text("europepmc", "PMC13778503"))


class AnnotationTests(unittest.TestCase):
    def test_summarized_per_entity_under_both_ids(self) -> None:
        found = epmc("europepmc_extras.json").annotations(EPMC_ANNOTATED)
        self.assertEqual(sorted(found), ["MED:43054306", "PMC:PMC13778503"])  # one article answers for both IDs
        rows = found["MED:43054306"]
        self.assertEqual(len(rows), 32)
        top = rows[0]
        self.assertEqual((top.type, top.name, top.mentions), ("Organisms", "severe acute respiratory syndrome coronavirus 2", 27))
        self.assertTrue(top.uri and top.sections)
        self.assertEqual([r.mentions for r in rows], sorted((r.mentions for r in rows), reverse=True))

    def test_summary_skips_unusable_mentions(self) -> None:
        rows = summarize_annotations([
            {"type": "Diseases", "exact": "flu", "tags": [{"name": "influenza", "uri": "u1"}], "section": "Abstract (x)"},
            {"type": "Diseases", "exact": "influenza", "tags": [{"name": "influenza", "uri": "u1"}], "section": "Results (y)"},
            {"type": "", "exact": "x"},
        ])
        self.assertEqual([(r.name, r.mentions, r.sections) for r in rows], [("influenza", 2, ["Abstract", "Results"])])


class ScanTests(unittest.TestCase):
    def test_search_and_enrichment_in_a_scan(self) -> None:
        cfg = RadarConfig.model_validate({
            "radar_id": "escape", "radar_name": "Escape radar", "sources": ["pubmed", "openalex", "europepmc"],
            "queries": ["immune escape", "antigenic drift"], "exclude": ["case report"],
            "filters": {"date_range": "last_14_days"}})
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "s.db"
            conns = {**connectors(), "europepmc": epmc("europepmc_scan.json", page_size=3)}
            res = Radar(cfg, store=db).scan(as_of=AS_OF, observed_at=DAY1, connectors=conns, embedder="hashing",
                                            max_results=5, top_n=5)
            cov = {c["source"]: c for c in res.run["coverage"]}
            self.assertEqual((cov["europepmc"]["status"], cov["europepmc"]["retrieved"]), ("ok", 5))
            self.assertEqual(cov["europepmc"]["enrichment"],
                             {"full_texts": 1, "full_texts_skipped_license": 1, "annotated": 3, "errors": []})
            with PaperStore(db) as st:
                [text] = [t for pid in st.paper_ids() for t in st.texts(pid)]
                self.assertTrue(text.license.startswith("cc"))  # kept only under a reusable licence...
                self.assertEqual((text.license_source, text.reuse in ("open", "noncommercial", "no_derivatives")),
                                 ("text", True))                  # ...read from the document itself
                annotated = [pid for pid in st.paper_ids() if st.annotations(pid)]
                self.assertEqual(len(annotated), 3)
                self.assertIn("europepmc", {s.source.value for p in st.papers() for s in p.sources})

            # A rescan within 30 days doesn't fetch the same full text or annotations again.
            from carousel.radar import enrich_from_europepmc

            class Refuse:
                def full_text(self, *a, **k):
                    raise AssertionError("full text fetched again")

                def annotations(self, ids):
                    raise AssertionError(f"annotations fetched again: {ids}")

            with PaperStore(db) as st:
                ids = [it["paper_id"] for it in res.items]
                stats = enrich_from_europepmc(Refuse(), st, ids, DAY1 + dt.timedelta(days=1))
                self.assertEqual((stats["full_texts"], stats["annotated"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
