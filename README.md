# CAROUSEL run snapshots

Data-only branch. Each pipeline run is stored as an immutable, gzipped JSON
envelope (`schema: carousel.snapshot/0`); the dashboard on `main` only ever
shows the latest run.

```text
snapshots/<radar_id>/<YYYY>/<YYYY-MM-DDTHHMMSSZ>.json.gz   # envelope + full payload
snapshots/<radar_id>/index.jsonl                           # one metadata line per run
```

Envelope fields: `radar_id`, `captured_at_utc`, `source` (`workflow` or
`git-backfill`), `provenance` (commit / workflow run), `window_days`,
`ranking_version`, `n_items`, `payload_sha256`, `payload`.

Runs from 2026-01-21 to 2026-09-13 were recovered from the git history of
`docs/latest.json` on `main` (`source: git-backfill`, `ranking_version:
unversioned`). They mix 1-, 7-, 14- and 30-day windows because two workflows
previously overwrote each other; filter on `window_days` when comparing runs.

Written by `scripts/snapshot.py` on `main`. Never edit or delete snapshot files.

Quick look with DuckDB:

```sql
SELECT captured_at_utc, window_days, n_items
FROM read_json_auto('snapshots/lab-default/index.jsonl')
ORDER BY captured_at_utc;
```
