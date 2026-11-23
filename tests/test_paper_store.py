import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from carousel.core.identifiers import IdentifierError, normalize
from carousel.core.paper import CanonicalPaper, Identifiers, PaperDates, SourceRecord, partial_date_bounds
from carousel.database import PaperStore, StoreError

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
T1 = dt.datetime(2026, 11, 2, 13, 0, tzinfo=UTC)
T2 = dt.datetime(2026, 11, 3, 13, 0, tzinfo=UTC)


def pubmed(**kw):
    base = dict(
        source="pubmed",
        source_id="39000001",
        identifiers={"doi": "10.1000/ABC.123", "pmid": "39000001", "pmcid": "PMC1111111"},
        title="Immune escape in a novel lineage",
        abstract="PubMed abstract.",
        authors=[{"name": "Ada Lovelace", "orcid": "https://orcid.org/0000-0002-1825-0097"}],
        venue="Nature",
        publication_types=["Journal Article"],
        dates={"published": "2026-11", "first_online": "2026-10-28"},
    )
    base.update(kw)
    return SourceRecord.model_validate(base)


def openalex(**kw):
    base = dict(
        source="openalex",
        source_id="W4400000001",
        identifiers={"doi": "https://doi.org/10.1000/abc.123", "openalex": "https://openalex.org/W4400000001"},
        title="Immune Escape in a Novel Lineage (OpenAlex casing)",
        abstract="OpenAlex reconstructed abstract.",
        venue="Nature",
        publication_types=["article"],
        dates={"published": "2026-11-05", "source_updated": "2026-11-06"},
    )
    base.update(kw)
    return SourceRecord.model_validate(base)


class IdentifierTests(unittest.TestCase):
    def test_canonical_forms(self) -> None:
        cases = {
            ("doi", "https://doi.org/10.1038/S41586-020-2012-7"): "10.1038/s41586-020-2012-7",
            ("doi", "doi: 10.1101/2020.01.01.123456"): "10.1101/2020.01.01.123456",
            ("doi", "https://dx.doi.org/10.1002/%28SICI%291097"): "10.1002/(sici)1097",
            ("pmid", "PMID: 031452104"): "31452104",
            ("pmid", "https://pubmed.ncbi.nlm.nih.gov/31452104/"): "31452104",
            ("pmcid", "pmc6779135"): "PMC6779135",
            ("pmcid", "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6779135/"): "PMC6779135",
            ("openalex", "https://openalex.org/w2741809807"): "W2741809807",
            ("arxiv", "arXiv:2101.00001v2"): "2101.00001",
            ("arxiv", "https://arxiv.org/abs/hep-th/9901001v1"): "hep-th/9901001",
        }
        for (scheme, raw), want in cases.items():
            self.assertEqual(normalize(scheme, raw), want, raw)

    def test_rejects_garbage(self) -> None:
        for scheme, raw in [("doi", "11.1000/x"), ("pmid", "0"), ("pmid", "12a"), ("pmcid", "6779135"),
                            ("openalex", "A123"), ("arxiv", "2101.1"), ("isbn", "x")]:
            with self.assertRaises(IdentifierError, msg=f"{scheme}:{raw}"):
                normalize(scheme, raw)

    def test_identifiers_model_normalizes_and_orders(self) -> None:
        ids = Identifiers(openalex="w1", doi="DOI:10.1234/X", pmid=" ")
        self.assertEqual(list(ids.items()), [("doi", "10.1234/x"), ("openalex", "W1")])
        self.assertFalse(Identifiers())


class ModelTests(unittest.TestCase):
    def test_partial_dates(self) -> None:
        self.assertEqual(PaperDates(published="2026").published, "2026")
        self.assertEqual(partial_date_bounds("2024-02"), (dt.date(2024, 2, 1), dt.date(2024, 2, 29)))
        for bad in ("2026-02-30", "2026-13", "26-01-01", "2026/01/01"):
            with self.assertRaises(ValidationError, msg=bad):
                PaperDates(published=bad)

    def test_naive_times_rejected_and_orcid_normalized(self) -> None:
        with self.assertRaises(ValidationError):
            pubmed(first_seen=dt.datetime(2026, 1, 1))
        self.assertEqual(pubmed().authors[0].orcid, "0000-0002-1825-0097")
        with self.assertRaises(ValidationError):
            pubmed(authors=[{"name": "X", "orcid": "1234"}])

    def test_unknown_fields_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            pubmed(citations=12)

    def test_source_record_json_roundtrip(self) -> None:
        r = pubmed(first_seen=T0, last_verified=T1, raw={"MedlineCitation": {"PMID": "39000001"}})
        back = SourceRecord.model_validate_json(r.model_dump_json())
        self.assertEqual(back, r)
        self.assertEqual(back.content_sha256(), r.content_sha256())
        # Observation times and raw payload are not content.
        self.assertEqual(r.model_copy(update={"last_verified": T2, "raw": None}).content_sha256(), r.content_sha256())

    def test_canonical_json_roundtrip(self) -> None:
        recs = [pubmed(first_seen=T0, last_verified=T0), openalex(first_seen=T1, last_verified=T1)]
        paper = CanonicalPaper.from_records("cp_x", recs)
        self.assertEqual(CanonicalPaper.model_validate_json(paper.model_dump_json()), paper)
        # Order of input records does not matter.
        self.assertEqual(CanonicalPaper.from_records("cp_x", list(reversed(recs))), paper)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "carousel.db"
        self.store = PaperStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_two_sources_one_paper_keeps_both_records(self) -> None:
        a = self.store.observe(pubmed(), T0)
        b = self.store.observe(openalex(), T1)
        self.assertEqual(a.status, "new_paper")
        self.assertEqual(b.status, "new_source")
        self.assertEqual(a.paper.paper_id, b.paper.paper_id)

        paper = b.paper
        self.assertEqual([r.source.value for r in paper.sources], ["openalex", "pubmed"])
        self.assertEqual(paper.source("openalex")[0].abstract, "OpenAlex reconstructed abstract.")
        # Field resolution and per-field provenance.
        self.assertEqual(paper.title, "Immune escape in a novel lineage")
        self.assertEqual(paper.provenance["title"].source.value, "pubmed")
        self.assertEqual(paper.abstract, "PubMed abstract.")
        self.assertEqual(paper.identifiers.openalex, "W4400000001")
        self.assertEqual(paper.provenance["identifiers.openalex"].source.value, "openalex")
        self.assertEqual(paper.identifiers.pmid, "39000001")
        self.assertEqual(paper.dates.published, "2026-11")  # PubMed outranks OpenAlex for the publication date
        self.assertEqual(paper.dates.source_updated, "2026-11-06")
        self.assertEqual(paper.provenance["dates.source_updated"].source.value, "openalex")
        self.assertEqual(paper.provenance["dates.source_updated"].observed_at, T1)
        self.assertEqual((paper.first_seen, paper.last_verified), (T0, T1))
        self.assertEqual(paper.conflicts, [])
        self.assertEqual(self.store.stats()["by_source"], {"openalex": 1, "pubmed": 1})

    def test_store_roundtrip_through_disk(self) -> None:
        self.store.observe(pubmed(raw={"k": [1, 2]}), T0)
        live = self.store.observe(openalex(), T1).paper
        self.store.close()
        with PaperStore(self.path) as reopened:
            self.assertEqual(reopened.get(live.paper_id), live)
            self.assertEqual(reopened.find("doi", "https://doi.org/10.1000/ABC.123"), live)
            self.assertEqual(reopened.find("pmcid", "pmc1111111"), live)
            self.assertEqual(reopened.find_source("openalex", "W4400000001"), live)
            self.assertIsNone(reopened.find("pmid", "1"))
            self.assertEqual(reopened.get(live.paper_id).source("pubmed")[0].raw, {"k": [1, 2]})
        self.store = PaperStore(self.path)

    def test_reobservation_keeps_first_seen(self) -> None:
        self.store.observe(pubmed(), T0)
        obs = self.store.observe(pubmed(), T2)
        self.assertEqual(obs.status, "unchanged")
        rec = obs.paper.source("pubmed")[0]
        self.assertEqual((rec.first_seen, rec.last_verified), (T0, T2))
        self.assertEqual(obs.paper.first_seen, T0)

    def test_content_change_updates_canonical(self) -> None:
        self.store.observe(pubmed(), T0)
        obs = self.store.observe(pubmed(title="Immune escape in a novel lineage (corrected)"), T1)
        self.assertEqual(obs.status, "updated")
        self.assertEqual(obs.paper.title, "Immune escape in a novel lineage (corrected)")
        changed_at = self.store.db.execute("SELECT content_changed_at FROM source_records").fetchone()[0]
        self.assertTrue(changed_at.startswith("2026-11-02"))

    def test_stale_observation_does_not_overwrite(self) -> None:
        self.store.observe(pubmed(), T1)
        obs = self.store.observe(pubmed(title="Older title from a replay"), T0)
        self.assertEqual(obs.status, "stale")
        self.assertEqual(obs.paper.title, "Immune escape in a novel lineage")
        rec = obs.paper.source("pubmed")[0]
        self.assertEqual((rec.first_seen, rec.last_verified), (T0, T1))

    def test_retraction_from_any_source_flags_paper(self) -> None:
        self.store.observe(pubmed(), T0)
        paper = self.store.observe(openalex(is_retracted=True), T1).paper
        self.assertTrue(paper.is_retracted)
        self.assertEqual(paper.provenance["is_retracted"].source.value, "openalex")

    def test_conflicting_identifiers_never_merge_papers(self) -> None:
        a = self.store.observe(pubmed(), T0).paper
        other = pubmed(source_id="39000002", identifiers={"doi": "10.1000/other", "pmid": "39000002"}, title="Other")
        b = self.store.observe(other, T0).paper
        # A record claiming paper A's DOI and paper B's PMID.
        bad = openalex(identifiers={"doi": "10.1000/abc.123", "pmid": "39000002", "openalex": "W4400000001"})
        obs = self.store.observe(bad, T1)
        self.assertEqual(obs.paper.paper_id, a.paper_id)  # DOI has priority
        self.assertEqual(len(obs.conflicts), 1)
        c = obs.conflicts[0]
        self.assertEqual((c.scheme, c.value, c.owner_paper_id, c.claimed_by_paper_id), ("pmid", "39000002", b.paper_id, a.paper_id))
        self.assertEqual(self.store.conflicts(), [c])
        self.assertEqual(len(self.store.get(b.paper_id).sources), 1)
        self.assertEqual(self.store.find("pmid", "39000002").paper_id, b.paper_id)
        # Within paper A, the two records now disagree on the PMID.
        self.assertEqual([x.scheme for x in obs.paper.conflicts], ["pmid"])
        self.assertEqual(obs.paper.identifiers.pmid, "39000001")  # PubMed wins its own scheme

    def test_record_without_identifiers_is_its_own_paper(self) -> None:
        a = self.store.observe(pubmed(), T0).paper
        b = self.store.observe(openalex(identifiers={}), T0).paper
        self.assertNotEqual(a.paper_id, b.paper_id)
        self.assertEqual(self.store.stats()["papers"], 2)

    def test_paper_ids_are_deterministic(self) -> None:
        with PaperStore() as other:
            self.assertEqual(other.observe(pubmed(), T2).paper.paper_id, self.store.observe(pubmed(), T0).paper.paper_id)

    def test_newer_schema_refused(self) -> None:
        self.store.close()
        con = sqlite3.connect(self.path)
        con.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        con.commit()
        con.close()
        with self.assertRaises(StoreError):
            PaperStore(self.path)
        self.store = PaperStore(":memory:")

    def test_seen_since_filter(self) -> None:
        a = self.store.observe(pubmed(), T0).paper
        b = self.store.observe(openalex(identifiers={}), T2).paper
        self.assertEqual(self.store.paper_ids(), [a.paper_id, b.paper_id])
        self.assertEqual([p.paper_id for p in self.store.papers(seen_since=T1)], [b.paper_id])


class NewnessTests(unittest.TestCase):
    def _paper(self, first_seen, **dates):
        rec = pubmed(dates=dates, first_seen=first_seen, last_verified=first_seen)
        return CanonicalPaper.from_records("cp_x", [rec])

    def test_new_means_recently_observed_and_published(self) -> None:
        as_of = dt.datetime(2026, 11, 20, tzinfo=UTC)
        self.assertTrue(self._paper(T0, first_online="2026-11-10").is_new(as_of, 21))
        # Only just indexed, but published years ago: not new.
        self.assertFalse(self._paper(T2, published="2019-03-01").is_new(as_of, 21))
        # Recently published, but first observed long ago: not new.
        self.assertFalse(self._paper(T0 - dt.timedelta(days=60), first_online="2026-11-10").is_new(as_of, 21))
        # A month-precision date gets the benefit of the doubt; a year-only one from last year does not.
        self.assertTrue(self._paper(T2, published="2026-11").is_new(as_of, 21))
        self.assertFalse(self._paper(T2, published="2025").is_new(as_of, 21))
        # No usable date: not new.
        self.assertFalse(self._paper(T2, source_updated="2026-11-15").is_new(as_of, 21))
        # The earliest public date counts (journal date is recent, but it was online last year).
        self.assertFalse(self._paper(T2, published="2026-11-15", first_online="2025-06-01").is_new(as_of, 21))


if __name__ == "__main__":
    unittest.main()
