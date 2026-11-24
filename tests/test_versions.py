"""Preprint <-> publication linking without collapsing the records (plan task 18).

Fixture pairs (tests/fixtures/http/versions.json, recorded by record_fixtures.py):

- two Europe PMC pairs, linked by Europe PMC from both sides ("Preprint in" on the article, "Preprint of" on
  the preprint);
- one Crossref pair (``has-preprint`` / ``is-preprint-of``, deposited by each side).
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

from carousel.connectors import CrossrefConnector, EuropePMCConnector
from carousel.core.paper import SourceRecord
from carousel.database import PaperStore, StoreError
from carousel.ingestion.versions import fetch_linked, link_versions
from test_connectors import FIXTURES, replay

sys.path.insert(0, str(FIXTURES))
from record_fixtures import CROSSREF_ARTICLE, CROSSREF_PREPRINT, EPMC_ARTICLES, EPMC_PREPRINTS  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 24, 9, tzinfo=UTC)


def connectors():
    client = replay("versions.json")
    return {"europepmc": EuropePMCConnector(client), "crossref": CrossrefConnector(client, mailto="")}


class Store:
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.st = PaperStore(Path(self.tmp.name) / "s.db")
        self.conns = connectors()

    def tearDown(self) -> None:
        self.st.close()
        self.tmp.cleanup()

    def observe(self, source: str, ids):
        return {r.source_id: self.st.observe(r, T0).paper.paper_id for r in self.conns[source].fetch(ids)}


class SourceRelationTests(Store, unittest.TestCase):
    def test_europepmc_links_each_side_to_the_other(self) -> None:
        recs = {r.source_id: r for r in self.conns["europepmc"].fetch(EPMC_ARTICLES + EPMC_PREPRINTS)}
        rels = {k: [(x.type, x.scheme, x.value, x.asserted_by) for x in r.relations] for k, r in recs.items()}
        self.assertEqual(rels["MED:42949957"], [("has-preprint", "europepmc", "PPR:PPR1222416", "title_author_match")])
        self.assertEqual(rels["PPR:PPR1222416"], [("is-preprint-of", "europepmc", "MED:42949957", "title_author_match")])

    def test_relations_dont_change_older_hashes(self) -> None:
        r = SourceRecord(source="pubmed", source_id="1", title="T")
        self.assertEqual(r.content_sha256(), r.model_copy(update={"relations": []}).content_sha256())


class LinkTests(Store, unittest.TestCase):
    """Done-when: linked records stay separate and show each other."""

    def test_europepmc_pairs_link_without_merging(self) -> None:
        ids = self.observe("europepmc", EPMC_ARTICLES + EPMC_PREPRINTS)
        self.assertEqual(len(set(ids.values())), 4)  # dedup never merges a preprint with its article...
        queued = {(i.paper_a, i.paper_b): i for i in self.st.review_queue()}
        art, pre = ids["MED:42949957"], ids["PPR:PPR1222416"]
        self.assertIn("possible_preprint_version", queued[tuple(sorted((art, pre)))].reasons)  # ...it asks a reviewer

        stats = link_versions(self.st, ids.values(), T0)
        self.assertEqual((stats["linked"], stats["confirmed"], stats["unresolved"]), (2, 2, []))  # each pair from both sides
        self.assertEqual(self.st.review_queue(), [])  # the source settled what the reviewer was asked
        self.assertEqual({i.status for i in self.st.review_queue("linked")}, {"linked"})

        [up] = self.st.version_links(pre)
        [down] = self.st.version_links(art)
        self.assertEqual((up["paper_id"], up["role"]), (art, "published"))
        self.assertEqual((down["paper_id"], down["role"]), (pre, "preprint"))
        self.assertEqual(sorted((e["method"], e["relation"], e["paper_id"]) for e in down["evidence"]),
                         sorted([("europepmc", "has-preprint", art), ("europepmc", "is-preprint-of", pre)]))
        # Both papers keep their own record: different dates, sources, and identifiers.
        a, p = self.st.get(art), self.st.get(pre)
        self.assertEqual((a.dates.preprint_posted, p.dates.preprint_posted), (None, "2026-02-27"))
        self.assertEqual(([s.source_id for s in a.sources], [s.source_id for s in p.sources]),
                         (["MED:42949957"], ["PPR:PPR1222416"]))
        self.assertEqual(self.st.stats()["version_links"], 2)

        again = link_versions(self.st, ids.values(), T0 + dt.timedelta(days=1))
        self.assertEqual((again["linked"], again["confirmed"]), (0, 4))

    def test_crossref_pair(self) -> None:
        ids = {**self.observe("crossref", [CROSSREF_ARTICLE]), **self.observe("crossref", [CROSSREF_PREPRINT])}
        link_versions(self.st, ids.values(), T0)
        [v] = self.st.version_links(ids[CROSSREF_ARTICLE])
        self.assertEqual((v["paper_id"], v["role"]), (ids[CROSSREF_PREPRINT], "preprint"))
        self.assertEqual(sorted((e["method"], e["asserted_by"]) for e in v["evidence"]),
                         [("crossref", "object"), ("crossref", "subject")])

    def test_missing_version_is_fetched_and_linked(self) -> None:
        ids = self.observe("europepmc", EPMC_ARTICLES)
        self.assertEqual(sorted(link_versions(self.st, ids.values(), T0)["unresolved"]),
                         [("europepmc", p) for p in EPMC_PREPRINTS])
        stats = fetch_linked(self.st, self.conns, ids.values(), T0)
        self.assertEqual((stats["looked_up"], stats["found"], stats["linked"], stats["unresolved"], stats["errors"]),
                         (2, 2, 2, 0, []))
        for art in ids.values():
            [v] = self.st.version_links(art)
            self.assertEqual(v["role"], "preprint")
            self.assertTrue(self.st.get(v["paper_id"]).sources[0].source_id.startswith("PPR:"))

        [rec] = self.conns["crossref"].fetch([CROSSREF_ARTICLE])
        pid = self.st.observe(rec, T0).paper.paper_id
        self.assertEqual(fetch_linked(self.st, {"crossref": self.conns["crossref"]}, [pid], T0)["linked"], 1)
        self.assertEqual(self.st.get(self.st.version_links(pid)[0]["paper_id"]).identifiers.doi, CROSSREF_PREPRINT)

    def test_a_version_not_found_is_not_asked_for_again_for_30_days(self) -> None:
        calls = []

        class Empty:
            def fetch(self, ids):
                calls.append(list(ids))
                return []

        rec = SourceRecord(source="crossref", source_id="10.5555/carousel-a", title="Article", identifiers={"doi": "10.5555/carousel-a"},
                           relations=[{"type": "has-preprint", "scheme": "doi", "value": "10.5555/carousel-p", "asserted_by": "object"}])
        pid = self.st.observe(rec, T0).paper.paper_id
        for days in (0, 1, 29, 31):
            stats = fetch_linked(self.st, {"crossref": Empty()}, [pid], T0 + dt.timedelta(days=days))
        self.assertEqual(calls, [["10.5555/carousel-p"], ["10.5555/carousel-p"]])  # day 0 and day 31
        self.assertEqual((stats["found"], stats["unresolved"]), (0, 1))
        self.assertEqual(self.st.link_checked_at("doi", "10.5555/carousel-p"), T0 + dt.timedelta(days=31))
        # Only connectors given are asked; an unknown scheme is left alone.
        self.assertEqual(fetch_linked(self.st, {}, [pid], T0 + dt.timedelta(days=90))["looked_up"], 0)

    def test_reviewer_decisions(self) -> None:
        ids = self.observe("europepmc", EPMC_ARTICLES + EPMC_PREPRINTS)
        art, pre = ids["MED:42949957"], ids["PPR:PPR1222416"]
        link_versions(self.st, ids.values(), T0)
        self.assertEqual(self.st.unlink_versions(art, pre, note="different studies"), 1)
        self.assertEqual(self.st.version_links(art), [])
        self.assertEqual(link_versions(self.st, [art, pre], T0)["refused"], 2)  # a source never overrides a reviewer
        self.assertEqual(self.st.link_versions(pre, art, note="checked: same study"), "linked")
        self.assertEqual(self.st.version_links(art)[0]["evidence"], [{"method": "manual", "note": "checked: same study"}])
        with self.assertRaises(StoreError):
            self.st.link_versions(art, art)

    def test_links_follow_merges(self) -> None:
        ids = self.observe("europepmc", EPMC_ARTICLES + EPMC_PREPRINTS)
        art, pre = ids["MED:42949957"], ids["PPR:PPR1222416"]
        link_versions(self.st, ids.values(), T0)
        other = self.st.observe(SourceRecord(source="openalex", source_id="W1", title="Something else entirely here",
                                             identifiers={"openalex": "W1"}), T0).paper.paper_id
        self.st.merge_papers(other, art)  # a reviewer folds the article into another paper (test only)
        [v] = self.st.version_links(pre)
        self.assertEqual(v["paper_id"], other)
        self.assertEqual(self.st.version_links(art)[0]["paper_id"], pre)  # the old ID still finds its versions

    def test_unlinked_lookalikes_stay_in_review(self) -> None:
        """A preprint and an article with near-identical titles, but no source linking them: review, no link."""
        title = "Antigenic cartography of influenza B viruses across five seasons"
        a = self.st.observe(SourceRecord(source="pubmed", source_id="1", title=title, authors=[{"name": "Ana Lee"}],
                                         identifiers={"pmid": "1"}, dates={"published": "2026-10-01"}), T0).paper.paper_id
        p = self.st.observe(SourceRecord(source="europepmc", source_id="PPR:PPR1", title=title, authors=[{"name": "Ana Lee"}],
                                         dates={"preprint_posted": "2026-05-01"}), T0).paper.paper_id
        self.assertNotEqual(a, p)
        self.assertEqual(link_versions(self.st, [a, p], T0)["linked"], 0)
        self.assertEqual([i.reasons for i in self.st.review_queue()], [("possible_preprint_version",)])
        self.assertEqual(self.st.version_links(a), [])


class ScanTests(unittest.TestCase):
    def test_a_scan_links_what_it_recommends(self) -> None:
        """The scan finds two journal articles; their preprints are looked up, linked, and shown on each item."""
        from carousel import Radar
        from carousel.connectors.base import QueryCoverage, SearchResult
        from carousel.core.config import RadarConfig

        ep = connectors()["europepmc"]
        articles = ep.fetch(EPMC_ARTICLES)

        class Search:  # Europe PMC as a scan uses it: search, then record lookups (no full texts here)
            def search(self, query):
                cov = QueryCoverage(source="europepmc", connector_version="test", native_query="fixture",
                                    date_field="first publication date", start=query.start, end=query.end,
                                    total_available=len(articles), retrieved=len(articles), truncated=False, pages=1,
                                    requests=1, cache_hits=0, cost=1, cost_unit="requests", retrieved_at=T0)
                return SearchResult(records=articles, coverage=cov)

            def fetch(self, ids):
                return ep.fetch(ids)

        cfg = RadarConfig.model_validate({
            "radar_id": "versions", "radar_name": "Versions", "sources": ["europepmc"],
            "queries": ["SARS-CoV-2 spike", "respiratory syncytial virus"], "filters": {"date_range": "last_60_days"}})
        with tempfile.TemporaryDirectory() as d:
            res = Radar(cfg, store=Path(d) / "s.db").scan(as_of=dt.date(2026, 11, 24), observed_at=T0,
                                                          connectors={"europepmc": Search()}, embedder="hashing")
            self.assertEqual(res.run["ranking"]["versions"],
                             {"linked": 2, "refused": 0, "looked_up": 2, "found": 2, "unresolved": 0, "errors": []})
            for item in res.items:
                self.assertIn("has_preprint", item["flags"])
                [v] = item["details"]["versions"]
                self.assertEqual((v["role"], v["linked_by"]), ("preprint", ["europepmc"]))
                self.assertNotEqual(v["paper_id"], item["paper_id"])
            # The preprints were looked up, not found by the search: they are papers but not recommendations.
            self.assertEqual(len(res.items), 2)
            with PaperStore(Path(d) / "s.db") as st:
                self.assertEqual(st.stats()["papers"], 4)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str):
        import contextlib
        import io

        from carousel import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_link_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "s.db")
            with PaperStore(db) as st:
                ids = {r.source_id: st.observe(r, T0).paper.paper_id
                       for r in connectors()["europepmc"].fetch(EPMC_ARTICLES + EPMC_PREPRINTS)}
            art, pre = ids["MED:42949957"], ids["PPR:PPR1222416"]
            code, out, _ = self.run_cli("review", "link", db, pre, art, "--note", "read both")
            self.assertEqual(code, 0, out)
            code, out, _ = self.run_cli("review", "list", db, "--status", "linked")
            self.assertIn(pre, out)
            code, out, _ = self.run_cli("review", "unlink", db, art, pre)
            self.assertIn("1 link(s) removed", out)
            code, _, err = self.run_cli("review", "link", db, art, art)
            self.assertEqual(code, 1)
            self.assertIn("same paper", err)
            with PaperStore(db) as st:
                self.assertEqual(st.version_links(art), [])


if __name__ == "__main__":
    unittest.main()
