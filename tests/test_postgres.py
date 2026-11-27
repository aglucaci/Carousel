"""Phase 1B Postgres schema, migrations, and imports.

Runs only when CAROUSEL_TEST_DATABASE_URL points at a disposable PostgreSQL
database with pgvector (CI starts one; locally e.g.
`docker run -p 127.0.0.1:55432:5432 -e POSTGRES_USER=carousel -e POSTGRES_PASSWORD=carousel
-e POSTGRES_DB=carousel_test pgvector/pgvector:pg16`). The tests drop and recreate every table.
"""

import contextlib
import datetime as dt
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

URL = os.environ.get("CAROUSEL_TEST_DATABASE_URL")
try:
    from sqlalchemy import func, inspect, select, text

    from carousel.database import importer, postgres
    from carousel.database import models as m
except ImportError:  # the [server] extra isn't installed
    importer = None

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

UTC = dt.timezone.utc


@unittest.skipUnless(URL and importer, "set CAROUSEL_TEST_DATABASE_URL and install the [server] extra")
class PostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        postgres.downgrade(URL)
        postgres.upgrade(URL)
        cls.engine = postgres.engine(URL)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        tables = ", ".join(t.name for t in m.Base.metadata.sorted_tables)
        with self.engine.begin() as c:
            c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def count(self, table: str) -> int:
        with self.engine.connect() as c:
            return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


class MigrationTests(PostgresTestCase):
    def test_up_down_up_and_no_drift(self) -> None:
        self.assertEqual(postgres.current(URL), "0011")
        postgres.downgrade(URL)
        self.assertIsNone(postgres.current(URL))
        self.assertEqual(inspect(postgres.engine(URL)).get_table_names(), ["alembic_version"])
        postgres.upgrade(URL)
        self.assertEqual(postgres.current(URL), "0011")

        from alembic.autogenerate import compare_metadata
        from alembic.runtime.migration import MigrationContext

        with self.engine.connect() as c:
            diff = compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), m.Base.metadata)
        self.assertEqual(diff, [])

    def test_spec_tables_exist(self) -> None:
        names = set(inspect(self.engine).get_table_names())
        spec = {"users", "radars", "radar_versions", "papers", "paper_identifiers", "paper_versions", "paper_sources",
                "paper_authors", "paper_embeddings", "topics", "paper_topics", "topic_observations", "recommendation_runs",
                "recommendation_items", "research_events", "user_feedback", "collections", "collection_papers", "digest_runs"}
        self.assertEqual(spec - names, set())

    def test_constraints(self) -> None:
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError), self.engine.begin() as c:
            c.execute(text("INSERT INTO papers (id, status) VALUES ('cp_x', 'deleted')"))
        with self.assertRaises(IntegrityError), self.engine.begin() as c:
            c.execute(text("INSERT INTO papers (id) VALUES ('cp_b'), ('cp_a')"))
            c.execute(text("INSERT INTO dedup_reviews (paper_a, paper_b, kind, reasons, features, status, created_at) "
                           "VALUES ('cp_b', 'cp_a', 'fuzzy', '[]', '{}', 'pending', now())"))


class StoreImportTests(PostgresTestCase):
    def build_store(self) -> Path:
        """A 1A store with runs, a fuzzy merge, a split, a review, feedback, embeddings."""
        from carousel import Radar
        from carousel import feedback as fb
        from carousel.core.paper import SourceRecord
        from carousel.database import PaperStore
        from test_scan import AS_OF, DAY1, RADAR_YML, connectors

        (self.dir / "escape.yml").write_text(RADAR_YML)
        db = self.dir / "c.db"
        radar = Radar.from_yaml(self.dir / "escape.yml", store=db)
        first = radar.scan(as_of=AS_OF, observed_at=DAY1, connectors=connectors(), embedder="hashing", max_results=5)
        fb.update(fb.default_path(self.dir / "escape.yml"), "dismiss", [first.items[0]["paper_id"]])
        radar.scan(as_of=AS_OF, observed_at=DAY1 + dt.timedelta(days=1), connectors=connectors(), embedder="hashing",
                   max_results=5)
        with PaperStore(db) as s:
            t = DAY1 + dt.timedelta(days=2)
            title = "Host and microbe interactions shape antibiotic tolerance in biofilms"
            s.observe(SourceRecord(source="pubmed", source_id="49000001", identifiers={"pmid": "49000001"}, title=title,
                                   authors=[{"name": "Sara Lindqvist", "orcid": "0000-0002-1825-0097"}],
                                   dates={"published": "2026-10-03"}), t)
            obs = s.observe(SourceRecord(source="openalex", source_id="W49000001", identifiers={"openalex": "W49000001"},
                                         title=title + ".", authors=[{"name": "Lindqvist S"}],
                                         dates={"published": "2026-10-03"}), t)
            self.assertEqual(obs.match, "fuzzy")             # auto merge (merge log 1)
            s.split("openalex", "W49000001", at=t)           # undone (merge log 2, a 'distinct' review row)
            keep, other = first.items[2]["paper_id"], first.items[3]["paper_id"]
            s.merge_papers(keep, other, at=t)                # reviewer merge of two recommended papers (merge log 3)
            self.merged_from = keep
        return db

    def test_import_counts_match_and_reimport_is_a_no_op(self) -> None:
        import sqlite3

        db = self.build_store()
        counts = importer.import_store(db, self.engine)
        lite = sqlite3.connect(db)
        q = lambda sql: lite.execute(sql).fetchone()[0]  # noqa: E731
        expected = {
            "papers": q("SELECT count(*) FROM papers"),
            "paper_identifiers": q("SELECT count(*) FROM identifiers"),
            "paper_sources": q("SELECT count(*) FROM source_records"),
            "paper_embeddings": q("SELECT count(*) FROM embeddings"),
            "dedup_reviews": q("SELECT count(*) FROM review_queue"),
            "merge_log": q("SELECT count(*) FROM merge_log"),
            "recommendation_runs": q("SELECT count(*) FROM recommendation_runs"),
            "recommendation_items": q("SELECT count(*) FROM recommendation_items"),
            "run_observations": q("SELECT count(*) FROM run_observations"),
            "user_feedback": q("SELECT count(*) FROM feedback_events"),
        }
        lite.close()
        for table, n in expected.items():
            self.assertEqual((counts[table], self.count(table)), (n, n), table)
        self.assertEqual(self.count("merge_log"), 3)  # auto merge, split, manual merge
        self.assertEqual(self.count("radars"), 1)
        self.assertEqual(self.count("radar_versions"), 1)

        before = {t: self.count(t) for t in expected}
        importer.import_store(db, self.engine)
        self.assertEqual({t: self.count(t) for t in expected}, before)

    def test_imported_rows(self) -> None:
        db = self.build_store()
        importer.import_store(db, self.engine)
        from sqlalchemy.orm import Session

        with Session(self.engine) as c:
            merged = c.execute(select(m.Paper.status, m.Paper.merged_into).where(m.Paper.merged_into.isnot(None))).all()
            self.assertEqual([tuple(r) for r in merged], [("merged", self.merged_from)])
            orcid = c.execute(select(m.Author).where(m.Author.orcid == "0000-0002-1825-0097")).scalar_one()
            self.assertEqual((orcid.name, orcid.identity), ("Sara Lindqvist", "orcid"))
            run = c.execute(select(m.RecommendationRun).order_by(m.RecommendationRun.executed_at)).scalars().first()
            self.assertEqual((run.radar_id, run.ranking_version), ("escape", "0.3.0"))
            self.assertIsNotNone(run.radar_version_id)
            item = c.execute(select(m.RecommendationItem).where(m.RecommendationItem.run_id == run.id,
                                                                m.RecommendationItem.rank == 1)).scalar_one()
            self.assertIn("relevance", item.features)
            self.assertIn("retrieved_by", item.explanation)
            fb = c.execute(select(m.UserFeedback)).scalar_one()
            self.assertEqual((fb.action, fb.change, fb.origin, fb.user_id), ("dismiss", "added", "file", None))
            hit = c.execute(text("SELECT count(*) FROM papers WHERE search @@ plainto_tsquery('english', 'immune escape')")).scalar()
            self.assertGreater(hit, 0)
            near = c.execute(text("SELECT count(*) FROM paper_embeddings e, paper_embeddings f "
                                  "WHERE e.paper_id <> f.paper_id AND e.embedding <=> f.embedding < 2")).scalar()
            self.assertGreater(near, 0)


class SnapshotImportTests(PostgresTestCase):
    def make_snapshots(self) -> Path:
        from scripts import snapshot

        root = self.dir / "snapshots-checkout"
        legacy = {"generated_at_utc": "2026-09-01 13:00:00", "days": 14, "window_utc": "x",
                  "sections": [{"label": "Selection", "items": [{"pmid": "41000001", "title": "A", "score": 3.2},
                                                                 {"pmid": "41000002", "title": "B", "score": 2.0}]},
                               {"label": "Phylodynamics", "items": [{"pmid": "41000003", "title": "C"}]}]}
        run = {"run_id": "run_x", "started_at": "2026-11-23T23:27:08.000000Z", "ranking_version": "0.1.0",
               "ranking": {"window": {"start": "2026-11-10", "end": "2026-11-23", "days": 14}},
               "items": [{"rank": 1, "paper_id": "cp_unknown", "score": 0.5,
                          "details": {"paper": {"title": "D", "pmid": "41000001", "doi": "10.1/d"}}}]}
        for payload in (legacy, run):
            env = snapshot.build_envelope(payload, "lab-default", "test", dt.datetime(2000, 1, 1, tzinfo=UTC), {})
            snapshot.write_envelope(root, env)
        snapshot.reindex(root, "lab-default")
        return root

    def test_counts_match_and_reimport(self) -> None:
        root = self.make_snapshots()
        [r] = importer.import_snapshots(root, self.engine)
        self.assertEqual((r.snapshots, r.expected_snapshots, r.items, r.expected_items), (2, 2, 4, 4))
        self.assertTrue(r.ok)
        with self.engine.connect() as c:
            rows = c.execute(select(m.SnapshotItem.section, m.SnapshotItem.rank, m.SnapshotItem.pmid)
                             .order_by(m.SnapshotItem.snapshot_id, m.SnapshotItem.position)).all()
            versions = c.execute(select(m.RunSnapshot.ranking_version).order_by(m.RunSnapshot.captured_at)).scalars().all()
        self.assertEqual([tuple(x) for x in rows][:3], [("Selection", 1, "41000001"), ("Selection", 2, "41000002"),
                                                        ("Phylodynamics", 1, "41000003")])
        self.assertEqual(versions, ["unversioned", "0.1.0"])
        [again] = importer.import_snapshots(root, self.engine)
        self.assertEqual((again.inserted_snapshots, again.snapshots, again.items), (0, 2, 4))

    def test_items_link_to_known_papers(self) -> None:
        root = self.make_snapshots()
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO papers (id) VALUES ('cp_known')"))
            c.execute(text("INSERT INTO paper_identifiers VALUES ('pmid', '41000001', 'cp_known')"))
        [r] = importer.import_snapshots(root, self.engine)
        self.assertEqual(r.linked_items, 2)  # the legacy item and the run item, both by PMID

    def test_mismatch_is_an_error(self) -> None:
        root = self.make_snapshots()
        index = root / "snapshots" / "lab-default" / "index.jsonl"
        rows = [json.loads(x) for x in index.read_text().splitlines()]
        rows[0]["n_items"] += 1
        index.write_text("".join(json.dumps(x) + "\n" for x in rows))
        with self.assertRaises(importer.ImportCountMismatch):
            importer.import_snapshots(root, self.engine)


class CliTests(PostgresTestCase):
    def run_cli(self, *argv):
        from carousel import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_db_commands(self) -> None:
        code, out, _ = self.run_cli("db", "current", "--url", URL)
        self.assertEqual((code, out.strip()), (0, "0011"))
        root = SnapshotImportTests.make_snapshots(self)
        code, out, err = self.run_cli("db", "import-snapshots", str(root), "--url", URL)
        self.assertEqual(code, 0, err)
        self.assertIn("2/2 snapshots, 4/4 items", out)
        code, _, err = self.run_cli("db", "current")
        self.assertEqual(code, 1 if not os.environ.get("CAROUSEL_DATABASE_URL") else 0)


if __name__ == "__main__":
    unittest.main()
