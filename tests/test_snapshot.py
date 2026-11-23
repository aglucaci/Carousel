import datetime as dt
import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import snapshot


def _payload(generated: str = "2026-09-13 16:43:14", n: int = 2) -> dict:
    return {
        "generated_at_utc": generated,
        "days": 14,
        "window_utc": {"mindate": "2026/08/30", "maxdate": "2026/09/13"},
        "ranking_version": "0.0.1-ether",
        "sections": [{"label": "A", "items": [{"pmid": str(i)} for i in range(n)]}],
    }


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self, payload: dict) -> dict:
        return snapshot.build_envelope(
            payload, "r1", "test", dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), {}
        )

    def test_envelope_uses_payload_time_and_counts_items(self) -> None:
        env = self._env(_payload(n=3))
        self.assertEqual(env["captured_at_utc"], "2026-09-13T16:43:14Z")
        self.assertEqual(env["n_items"], 3)
        self.assertEqual(env["ranking_version"], "0.0.1-ether")
        self.assertEqual(
            snapshot.snapshot_relpath(env),
            Path("snapshots/r1/2026/2026-09-13T164314Z.json.gz"),
        )

    def test_missing_timestamp_falls_back_and_version_defaults(self) -> None:
        p = _payload()
        del p["generated_at_utc"], p["ranking_version"]
        env = self._env(p)
        self.assertEqual(env["captured_at_utc"], "2000-01-01T00:00:00Z")
        self.assertEqual(env["ranking_version"], "unversioned")

    def test_write_is_append_only_and_deterministic(self) -> None:
        env = self._env(_payload())
        path, written = snapshot.write_envelope(self.root, env)
        self.assertTrue(written)
        first = path.read_bytes()
        changed = self._env(_payload(n=5))  # same timestamp, different content
        _, written_again = snapshot.write_envelope(self.root, changed)
        self.assertFalse(written_again)
        self.assertEqual(path.read_bytes(), first)
        with gzip.open(path, "rt") as fh:
            self.assertEqual(json.load(fh)["payload"]["sections"][0]["items"][0]["pmid"], "0")

    def test_reindex_sorted_by_time(self) -> None:
        for ts in ("2026-09-13 16:00:00", "2026-01-02 08:00:00", "2026-05-05 12:00:00"):
            snapshot.write_envelope(self.root, self._env(_payload(ts)))
        index = snapshot.reindex(self.root, "r1")
        rows = [json.loads(line) for line in index.read_text().splitlines()]
        self.assertEqual(
            [r["captured_at_utc"] for r in rows],
            ["2026-01-02T08:00:00Z", "2026-05-05T12:00:00Z", "2026-09-13T16:00:00Z"],
        )
        self.assertTrue(all(r["path"].startswith("snapshots/r1/") for r in rows))

    def test_backfill_from_git_history(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        out = repo / "docs" / "latest.json"
        out.parent.mkdir()
        for ts in ("2026-01-01 13:00:00", "2026-01-02 13:00:00"):
            out.write_text(json.dumps(_payload(ts)))
            git("add", ".")
            git("commit", "-q", "-m", f"run {ts}")
        out.write_text("{not json")
        git("commit", "-qam", "broken")

        snap_root = self.root / "snaps"
        args = ["backfill", "--repo", str(repo), "--root", str(snap_root), "--radar-id", "r1"]
        self.assertEqual(snapshot.main(args), 0)
        rows = (snap_root / "snapshots/r1/index.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0])["source"], "git-backfill")
        # Re-running is idempotent.
        snapshot.main(args)
        self.assertEqual(len((snap_root / "snapshots/r1/index.jsonl").read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
