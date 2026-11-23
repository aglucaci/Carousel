"""Local paper store (SQLite, stdlib only).

The Phase 1A store behind ``carousel scan`` and the Actions demo. It keeps:

- every source record ever observed, with ``first_seen`` / ``last_verified``
  and ``content_changed_at`` (when the source last said something different);
- an identifier index, where each normalized identifier belongs to exactly one
  paper;
- the canonical paper derived from each paper's source records; and
- identifier conflicts: a record whose identifiers point at two different
  papers is attached to one, never used to merge them silently, and the
  conflict is kept for review (plan task 6).

Matching here is **exact identifier matching only**. Fuzzy title/author
matching belongs to the dedup step. In Phase 1B the same entities move to
Postgres.

Usage::

    with PaperStore("carousel.db") as store:
        obs = store.observe(record, observed_at=now)
        obs.paper.provenance["title"]
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple, Union

from carousel.core.identifiers import normalize
from carousel.core.paper import CanonicalPaper, SourceRecord, paper_id_for

STORE_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers (
    paper_id      TEXT PRIMARY KEY,
    first_seen    TEXT NOT NULL,
    last_verified TEXT NOT NULL,
    title         TEXT,
    canonical     TEXT NOT NULL            -- CanonicalPaper JSON (derived; rebuilt on every change)
);
CREATE TABLE IF NOT EXISTS source_records (
    source             TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    paper_id           TEXT NOT NULL REFERENCES papers(paper_id),
    content_sha256     TEXT NOT NULL,
    first_seen         TEXT NOT NULL,
    last_verified      TEXT NOT NULL,
    content_changed_at TEXT NOT NULL,
    record             TEXT NOT NULL,      -- SourceRecord JSON
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS source_records_paper ON source_records(paper_id);
CREATE TABLE IF NOT EXISTS identifiers (
    scheme   TEXT NOT NULL,
    value    TEXT NOT NULL,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    PRIMARY KEY (scheme, value)
);
CREATE INDEX IF NOT EXISTS identifiers_paper ON identifiers(paper_id);
CREATE TABLE IF NOT EXISTS identifier_conflicts (
    scheme              TEXT NOT NULL,
    value               TEXT NOT NULL,
    owner_paper_id      TEXT NOT NULL,     -- paper the identifier already belongs to
    claimed_by_paper_id TEXT NOT NULL,     -- paper the new record was attached to
    source              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    PRIMARY KEY (scheme, value, claimed_by_paper_id, source, source_id)
);
"""


class StoreError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Conflict:
    scheme: str
    value: str
    owner_paper_id: str
    claimed_by_paper_id: str
    source: str
    source_id: str
    observed_at: str


@dataclasses.dataclass(frozen=True)
class Observation:
    """Result of :meth:`PaperStore.observe`.

    ``status`` is one of:

    - ``new_paper``: no known paper matched; a new one was created.
    - ``new_source``: an existing paper gained a record from this source.
    - ``updated``: a known source record now says something different.
    - ``unchanged``: a known source record was re-verified with the same content.
    - ``stale``: the observation is older than the stored one (e.g. a replay);
      only ``first_seen`` may move earlier, and content is not overwritten.
    """

    paper: CanonicalPaper
    status: str
    conflicts: Tuple[Conflict, ...] = ()


def _iso(t: dt.datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("observation times must be timezone-aware")
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)


class PaperStore:
    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.db:
            self.db.executescript(_DDL)
            row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(STORE_SCHEMA_VERSION),))
            elif int(row["value"]) > STORE_SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path} uses store schema {row['value']}, but this CAROUSEL only knows "
                    f"schema {STORE_SCHEMA_VERSION}; upgrade CAROUSEL"
                )

    # ---- lifecycle ----

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PaperStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- writes ----

    def observe(self, record: SourceRecord, observed_at: Optional[dt.datetime] = None) -> Observation:
        """Record that ``record`` was seen at ``observed_at`` (default: now) and update its paper."""
        now = _iso(observed_at or dt.datetime.now(dt.timezone.utc))
        record = record.model_copy(update={"first_seen": None, "last_verified": None})
        source, source_id = record.key
        content = record.content_sha256()

        with self.db:
            row = self.db.execute(
                "SELECT * FROM source_records WHERE source = ? AND source_id = ?", (source, source_id)
            ).fetchone()

            conflicts: List[Conflict] = []
            if row is not None:
                paper_id = row["paper_id"]
                first_seen = min(row["first_seen"], now)
                if now < row["last_verified"]:
                    status = "stale"
                    stored = SourceRecord.model_validate_json(row["record"])
                    self._write_record(stored, paper_id, row["content_sha256"], first_seen, row["last_verified"], row["content_changed_at"])
                else:
                    changed = content != row["content_sha256"]
                    status = "updated" if changed else "unchanged"
                    changed_at = now if changed else row["content_changed_at"]
                    self._write_record(record, paper_id, content, first_seen, now, changed_at)
                    if changed:
                        conflicts = self._index_identifiers(record, paper_id, now)
            else:
                owners = self._owners(record)
                if owners:
                    paper_id = owners[0][2]
                    status = "new_source"
                else:
                    paper_id = paper_id_for(record)
                    status = "new_paper"
                    self.db.execute(
                        "INSERT INTO papers (paper_id, first_seen, last_verified, canonical) VALUES (?, ?, ?, '{}')",
                        (paper_id, now, now),
                    )
                self._write_record(record, paper_id, content, now, now, now)
                conflicts = self._index_identifiers(record, paper_id, now)

            paper = self._rebuild(paper_id)
        return Observation(paper=paper, status=status, conflicts=tuple(conflicts))

    def observe_many(self, records: Sequence[SourceRecord], observed_at: Optional[dt.datetime] = None) -> List[Observation]:
        at = observed_at or dt.datetime.now(dt.timezone.utc)
        return [self.observe(r, at) for r in records]

    def _owners(self, record: SourceRecord) -> List[Tuple[str, str, str]]:
        """(scheme, value, paper_id) for each of the record's identifiers already in the store, priority order."""
        out = []
        for scheme, value in record.identifiers.items():
            hit = self.db.execute("SELECT paper_id FROM identifiers WHERE scheme = ? AND value = ?", (scheme, value)).fetchone()
            if hit:
                out.append((scheme, value, hit["paper_id"]))
        return out

    def _index_identifiers(self, record: SourceRecord, paper_id: str, now: str) -> List[Conflict]:
        conflicts = []
        for scheme, value in record.identifiers.items():
            self.db.execute("INSERT OR IGNORE INTO identifiers VALUES (?, ?, ?)", (scheme, value, paper_id))
            owner = self.db.execute(
                "SELECT paper_id FROM identifiers WHERE scheme = ? AND value = ?", (scheme, value)
            ).fetchone()["paper_id"]
            if owner != paper_id:
                c = Conflict(scheme, value, owner, paper_id, record.source.value, record.source_id, now)
                self.db.execute(
                    "INSERT OR IGNORE INTO identifier_conflicts VALUES (?, ?, ?, ?, ?, ?, ?)", dataclasses.astuple(c)
                )
                conflicts.append(c)
        return conflicts

    def _write_record(
        self, record: SourceRecord, paper_id: str, content: str, first_seen: str, last_verified: str, changed_at: str
    ) -> None:
        stored = record.model_copy(update={"first_seen": _parse(first_seen), "last_verified": _parse(last_verified)})
        self.db.execute(
            "INSERT OR REPLACE INTO source_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record.source.value, record.source_id, paper_id, content, first_seen, last_verified, changed_at,
             stored.model_dump_json(exclude_none=True)),
        )

    def _records(self, paper_id: str) -> List[SourceRecord]:
        rows = self.db.execute(
            "SELECT record FROM source_records WHERE paper_id = ? ORDER BY source, source_id", (paper_id,)
        ).fetchall()
        return [SourceRecord.model_validate_json(r["record"]) for r in rows]

    def _rebuild(self, paper_id: str) -> CanonicalPaper:
        paper = CanonicalPaper.from_records(paper_id, self._records(paper_id))
        self.db.execute(
            "UPDATE papers SET first_seen = ?, last_verified = ?, title = ?, canonical = ? WHERE paper_id = ?",
            (_iso(paper.first_seen), _iso(paper.last_verified), paper.title,
             paper.model_dump_json(exclude={"sources"}, exclude_none=True), paper_id),
        )
        return paper

    # ---- reads ----

    def get(self, paper_id: str) -> Optional[CanonicalPaper]:
        records = self._records(paper_id)
        return CanonicalPaper.from_records(paper_id, records) if records else None

    def find(self, scheme: str, value: str) -> Optional[CanonicalPaper]:
        """Look a paper up by any identifier, in any accepted spelling."""
        row = self.db.execute(
            "SELECT paper_id FROM identifiers WHERE scheme = ? AND value = ?", (scheme, normalize(scheme, value))
        ).fetchone()
        return self.get(row["paper_id"]) if row else None

    def find_source(self, source: str, source_id: str) -> Optional[CanonicalPaper]:
        row = self.db.execute(
            "SELECT paper_id FROM source_records WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        return self.get(row["paper_id"]) if row else None

    def paper_ids(self, seen_since: Optional[dt.datetime] = None) -> List[str]:
        if seen_since is None:
            rows = self.db.execute("SELECT paper_id FROM papers ORDER BY first_seen, paper_id")
        else:
            rows = self.db.execute(
                "SELECT paper_id FROM papers WHERE first_seen >= ? ORDER BY first_seen, paper_id", (_iso(seen_since),)
            )
        return [r["paper_id"] for r in rows]

    def papers(self, seen_since: Optional[dt.datetime] = None) -> Iterator[CanonicalPaper]:
        for pid in self.paper_ids(seen_since):
            paper = self.get(pid)
            if paper is not None:
                yield paper

    def conflicts(self) -> List[Conflict]:
        rows = self.db.execute("SELECT * FROM identifier_conflicts ORDER BY observed_at, scheme, value")
        return [Conflict(*tuple(r)) for r in rows]

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        by_source = dict(self.db.execute("SELECT source, COUNT(*) FROM source_records GROUP BY source ORDER BY source").fetchall())
        return {
            "papers": q("SELECT COUNT(*) FROM papers"),
            "source_records": q("SELECT COUNT(*) FROM source_records"),
            "identifiers": q("SELECT COUNT(*) FROM identifiers"),
            "conflicts": q("SELECT COUNT(*) FROM identifier_conflicts"),
            "by_source": by_source,
        }


__all__ = ["Conflict", "Observation", "PaperStore", "StoreError", "STORE_SCHEMA_VERSION"]
