"""Local paper store (SQLite, stdlib only).

The Phase 1A store behind ``carousel scan`` and the Actions demo. It keeps:

- every source record ever observed, with ``first_seen`` / ``last_verified``
  and ``content_changed_at`` (when the source last said something different);
- an identifier index, where each normalized identifier belongs to exactly one
  paper;
- the canonical paper derived from each paper's source records;
- a **review queue** of possible duplicates, and a **merge log** of every
  merge and split, so deduplication is reviewable and reversible.

How a newly observed record finds its paper (plan task 6):

1. Its own ``(source, source_id)`` if seen before.
2. Exact identifiers, in priority order: DOI, PMID, PMCID, OpenAlex, arXiv.
   A match on a weaker identifier is refused when the DOIs disagree; the
   pair goes to review instead.
3. Cautious title/author/year matching (:mod:`carousel.ingestion.dedup`)
   against papers sharing title words. Only a pair that passes every gate is
   merged automatically; plausible pairs go to review.
4. Otherwise it becomes a new paper.

A record whose identifiers point at two different papers never merges them;
the conflict is logged and queued. In Phase 1B the same entities move to
Postgres.

Usage::

    with PaperStore("carousel.db") as store:
        obs = store.observe(record, observed_at=now)
        store.review_queue()                 # pending possible duplicates
        store.merge_papers(keep, other)      # reviewer says: same work
        store.mark_distinct(a, b)            # reviewer says: different works
        store.split("openalex", "W123")      # undo a wrong merge
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from carousel.core.identifiers import normalize
from carousel.core.paper import CanonicalPaper, SourceRecord, paper_id_for
from carousel.ingestion.dedup import MatchDecision, best_match, title_tokens

STORE_SCHEMA_VERSION = 4

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
    canonical     TEXT NOT NULL,           -- CanonicalPaper JSON (derived; rebuilt on every change)
    merged_into   TEXT                     -- set when this paper was merged into another (a redirect)
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
CREATE TABLE IF NOT EXISTS title_tokens (    -- candidate blocking for fuzzy dedup
    token    TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    PRIMARY KEY (token, paper_id)
);
CREATE INDEX IF NOT EXISTS title_tokens_paper ON title_tokens(paper_id);
CREATE TABLE IF NOT EXISTS review_queue (
    paper_a     TEXT NOT NULL,             -- paper_a < paper_b
    paper_b     TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- fuzzy | identifier_conflict
    score       REAL,
    reasons     TEXT NOT NULL,             -- JSON list
    features    TEXT NOT NULL,             -- JSON object
    status      TEXT NOT NULL,             -- pending | merged | distinct | superseded
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    note        TEXT,
    PRIMARY KEY (paper_a, paper_b)
);
CREATE TABLE IF NOT EXISTS embeddings (      -- one vector per (paper, model_version); see search/embeddings.py
    paper_id      TEXT NOT NULL,
    model_version TEXT NOT NULL,
    text_sha256   TEXT NOT NULL,           -- hash of the embedded text (recipe-versioned)
    dim           INTEGER NOT NULL,
    vector        BLOB NOT NULL,           -- little-endian float32
    created_at    TEXT NOT NULL,
    PRIMARY KEY (paper_id, model_version)
);
CREATE TABLE IF NOT EXISTS recommendation_runs (   -- one row per scan (spec §8 recommendation_runs)
    run_id             TEXT PRIMARY KEY,
    radar_id           TEXT NOT NULL,
    radar_name         TEXT NOT NULL,
    config_hash        TEXT NOT NULL,
    as_of              TEXT NOT NULL,          -- date the window ends
    started_at         TEXT NOT NULL,
    finished_at        TEXT NOT NULL,
    status             TEXT NOT NULL,          -- ok | partial (a source failed or was skipped)
    ranking_version    TEXT NOT NULL,
    candidates_version TEXT NOT NULL,
    model_version      TEXT,
    mode               TEXT NOT NULL,
    n_observed         INTEGER NOT NULL,
    n_candidates       INTEGER NOT NULL,
    n_items            INTEGER NOT NULL,
    coverage           TEXT NOT NULL,          -- JSON list, one entry per source
    ranking            TEXT NOT NULL,          -- JSON: weights, signals, stats
    notes              TEXT NOT NULL           -- JSON list of warnings
);
CREATE INDEX IF NOT EXISTS runs_radar ON recommendation_runs(radar_id, started_at);
CREATE TABLE IF NOT EXISTS recommendation_items (  -- spec §8 recommendation_items
    run_id     TEXT NOT NULL REFERENCES recommendation_runs(run_id),
    rank       INTEGER NOT NULL,
    paper_id   TEXT NOT NULL,
    score      REAL NOT NULL,
    mmr        REAL NOT NULL,
    components TEXT NOT NULL,                  -- JSON
    details    TEXT NOT NULL,                  -- JSON (features behind "Why this paper?")
    flags      TEXT NOT NULL,                  -- JSON list
    PRIMARY KEY (run_id, rank)
);
CREATE INDEX IF NOT EXISTS items_paper ON recommendation_items(paper_id);
CREATE TABLE IF NOT EXISTS run_observations (      -- which source records each run saw
    run_id    TEXT NOT NULL,
    source    TEXT NOT NULL,
    source_id TEXT NOT NULL,
    paper_id  TEXT NOT NULL,
    status    TEXT NOT NULL,                   -- Observation.status at the time
    PRIMARY KEY (run_id, source, source_id)
);
CREATE TABLE IF NOT EXISTS feedback_state (        -- current feedback per radar (mirrors the feedback file)
    radar_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    action   TEXT NOT NULL,                    -- save | dismiss | relevant | irrelevant
    ref      TEXT NOT NULL,                    -- as written in the file (paper_id, doi:..., pmid:...)
    since    TEXT NOT NULL,
    PRIMARY KEY (radar_id, paper_id, action)
);
CREATE TABLE IF NOT EXISTS feedback_events (       -- append-only history of feedback changes
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    radar_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    action   TEXT NOT NULL,
    change   TEXT NOT NULL,                    -- added | removed
    ref      TEXT NOT NULL,
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    action    TEXT NOT NULL,               -- merge | split
    survivor  TEXT NOT NULL,               -- merge: kept paper;  split: paper the record left
    other     TEXT NOT NULL,               -- merge: absorbed paper; split: new paper
    method    TEXT NOT NULL,               -- auto_fuzzy | manual
    score     REAL,
    detail    TEXT NOT NULL                -- JSON
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
class ReviewItem:
    paper_a: str
    paper_b: str
    kind: str
    score: Optional[float]
    reasons: Tuple[str, ...]
    features: Dict[str, Any]
    status: str
    created_at: str
    decided_at: Optional[str]
    note: Optional[str]
    title_a: Optional[str] = None
    title_b: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Observation:
    """Result of :meth:`PaperStore.observe`.

    ``status`` is one of:

    - ``new_paper``: no known paper matched; a new one was created.
    - ``new_source``: an existing paper gained a record from this source
      (``match`` says how: ``identifier`` or ``fuzzy``).
    - ``updated``: a known source record now says something different.
    - ``unchanged``: a known source record was re-verified with the same content.
    - ``stale``: the observation is older than the stored one (e.g. a replay);
      only ``first_seen`` may move earlier, and content is not overwritten.

    ``queued`` lists the paper pairs this observation added to the review queue.
    """

    paper: CanonicalPaper
    status: str
    conflicts: Tuple[Conflict, ...] = ()
    match: Optional[str] = None
    queued: Tuple[Tuple[str, str], ...] = ()


def _iso(t: dt.datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("observation times must be timezone-aware")
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)


def _pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


class PaperStore:
    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.db:
            row = None
            if self.db.execute("SELECT name FROM sqlite_master WHERE name = 'meta'").fetchone():
                row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            version = int(row["value"]) if row else None
            if version is not None and version > STORE_SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path} uses store schema {version}, but this CAROUSEL only knows "
                    f"schema {STORE_SCHEMA_VERSION}; upgrade CAROUSEL"
                )
            if version == 1:
                self._migrate_v1()
            self.db.executescript(_DDL)
            self.db.execute("CREATE INDEX IF NOT EXISTS papers_merged_into ON papers(merged_into)")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(STORE_SCHEMA_VERSION),))

    def _migrate_v1(self) -> None:
        """v1 -> v2: redirects for merged papers, title-token index, review queue, merge log."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(papers)")}
        if "merged_into" not in cols:
            self.db.execute("ALTER TABLE papers ADD COLUMN merged_into TEXT")
        self.db.executescript(_DDL)
        for r in self.db.execute("SELECT paper_id, title FROM papers").fetchall():
            self._index_title(r["paper_id"], r["title"])

    # ---- lifecycle ----

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PaperStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- observing records ----

    def observe(self, record: SourceRecord, observed_at: Optional[dt.datetime] = None) -> Observation:
        """Record that ``record`` was seen at ``observed_at`` (default: now) and update its paper."""
        now = _iso(observed_at or dt.datetime.now(dt.timezone.utc))
        record = record.model_copy(update={"first_seen": None, "last_verified": None})
        source, source_id = record.key
        content = record.content_sha256()
        match: Optional[str] = None
        queued: List[Tuple[str, str]] = []

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
                doi_clash = None
                if owners and owners[0][0] != "doi" and record.identifiers.doi:
                    owner_dois = self._identifier_values(owners[0][2], "doi")
                    if owner_dois and record.identifiers.doi not in owner_dois:
                        doi_clash = owners[0]
                if owners and doi_clash is None:
                    paper_id = owners[0][2]
                    status, match = "new_source", "identifier"
                else:
                    paper_id = self._new_paper(record, now)
                    status = "new_paper"
                self._write_record(record, paper_id, content, now, now, now)
                conflicts = self._index_identifiers(record, paper_id, now)
                if doi_clash is not None:
                    queued.append(self._enqueue(paper_id, doi_clash[2], "identifier_conflict", None,
                                                (f"doi_disagrees_with_matching_{doi_clash[0]}",),
                                                {"scheme": doi_clash[0], "value": doi_clash[1],
                                                 "doi": record.identifiers.doi}, now))
                elif status == "new_paper":
                    paper_id, fuzzy_queued, merged = self._fuzzy(paper_id, now)
                    queued.extend(fuzzy_queued)
                    if merged:
                        status, match = "new_source", "fuzzy"
            for c in conflicts:
                queued.append(self._enqueue(c.owner_paper_id, c.claimed_by_paper_id, "identifier_conflict", None,
                                            ("record_claims_identifier_of_other_paper",),
                                            {"scheme": c.scheme, "value": c.value, "source": c.source,
                                             "source_id": c.source_id}, now))

            paper = self._rebuild(paper_id)
        return Observation(paper=paper, status=status, conflicts=tuple(conflicts), match=match,
                           queued=tuple(q for q in queued if q))

    def observe_many(self, records: Sequence[SourceRecord], observed_at: Optional[dt.datetime] = None) -> List[Observation]:
        at = observed_at or dt.datetime.now(dt.timezone.utc)
        return [self.observe(r, at) for r in records]

    def _new_paper(self, record: SourceRecord, now: str) -> str:
        paper_id = paper_id_for(record)
        if self.db.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)).fetchone():
            paper_id = self._fresh_id(record)
        self.db.execute(
            "INSERT INTO papers (paper_id, first_seen, last_verified, canonical) VALUES (?, ?, ?, '{}')",
            (paper_id, now, now),
        )
        return paper_id

    def _fresh_id(self, record: SourceRecord) -> str:
        n = 1
        while True:
            pid = "cp_" + hashlib.sha256(f"{record.source.value}:{record.source_id}:{n}".encode()).hexdigest()[:16]
            if not self.db.execute("SELECT 1 FROM papers WHERE paper_id = ?", (pid,)).fetchone():
                return pid
            n += 1

    def _fuzzy(self, paper_id: str, now: str) -> Tuple[str, List[Tuple[str, str]], bool]:
        """Compare a new paper against title-similar papers. Returns (paper_id, queued pairs, merged?)."""
        paper = self._rebuild(paper_id)
        decided = self._decided_pairs(paper_id)
        matches = [(c, d) for c, d in best_match(paper, self._candidates(paper_id, paper.title))
                   if _pair(paper_id, c.paper_id) not in decided]
        queued: List[Tuple[str, str]] = []
        if matches and matches[0][1].is_merge:
            target, decision = matches[0]
            self._merge(target.paper_id, paper_id, "auto_fuzzy", decision, now)
            return target.paper_id, queued, True
        for cand, decision in matches:
            queued.append(self._enqueue(paper_id, cand.paper_id, "fuzzy", decision.score, decision.reasons,
                                        decision.features, now))
        return paper_id, queued, False

    def _candidates(self, paper_id: str, title: Optional[str], limit: int = 50) -> List[CanonicalPaper]:
        toks = title_tokens(title)
        if len(toks) < 3:
            return []
        need = max(2, math.ceil(len(toks) * 0.5))
        marks = ",".join("?" * len(toks))
        rows = self.db.execute(
            f"""SELECT t.paper_id, COUNT(*) AS c FROM title_tokens t
                JOIN papers p ON p.paper_id = t.paper_id AND p.merged_into IS NULL
                WHERE t.token IN ({marks}) AND t.paper_id != ?
                GROUP BY t.paper_id HAVING c >= ? ORDER BY c DESC, t.paper_id LIMIT ?""",
            (*toks, paper_id, need, limit),
        ).fetchall()
        return [p for p in (self.get(r["paper_id"]) for r in rows) if p is not None]

    def _owners(self, record: SourceRecord) -> List[Tuple[str, str, str]]:
        """(scheme, value, paper_id) for each of the record's identifiers already in the store, priority order."""
        out = []
        for scheme, value in record.identifiers.items():
            hit = self.db.execute("SELECT paper_id FROM identifiers WHERE scheme = ? AND value = ?", (scheme, value)).fetchone()
            if hit:
                out.append((scheme, value, hit["paper_id"]))
        return out

    def _identifier_values(self, paper_id: str, scheme: str) -> set:
        rows = self.db.execute("SELECT record FROM source_records WHERE paper_id = ?", (paper_id,)).fetchall()
        recs = [SourceRecord.model_validate_json(r["record"]) for r in rows]
        return {v for r in recs for v in [getattr(r.identifiers, scheme)] if v}

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

    def _index_title(self, paper_id: str, title: Optional[str]) -> None:
        self.db.execute("DELETE FROM title_tokens WHERE paper_id = ?", (paper_id,))
        self.db.executemany("INSERT OR IGNORE INTO title_tokens VALUES (?, ?)", [(t, paper_id) for t in title_tokens(title)])

    def _rebuild(self, paper_id: str) -> CanonicalPaper:
        paper = CanonicalPaper.from_records(paper_id, self._records(paper_id))
        self.db.execute(
            "UPDATE papers SET first_seen = ?, last_verified = ?, title = ?, canonical = ? WHERE paper_id = ?",
            (_iso(paper.first_seen), _iso(paper.last_verified), paper.title,
             paper.model_dump_json(exclude={"sources"}, exclude_none=True), paper_id),
        )
        self._index_title(paper_id, paper.title)
        return paper

    # ---- review queue ----

    def _enqueue(self, a: str, b: str, kind: str, score: Optional[float], reasons: Sequence[str],
                 features: Dict[str, Any], now: str) -> Tuple[str, str]:
        """Add a pending pair unless the pair was already queued or decided. Returns the pair."""
        pa, pb = _pair(a, b)
        self.db.execute(
            "INSERT OR IGNORE INTO review_queue VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)",
            (pa, pb, kind, score, json.dumps(list(reasons)), json.dumps(features, sort_keys=True), now),
        )
        return pa, pb

    def _decided_pairs(self, paper_id: str) -> set:
        rows = self.db.execute(
            "SELECT paper_a, paper_b FROM review_queue WHERE status != 'pending' AND (paper_a = ? OR paper_b = ?)",
            (paper_id, paper_id),
        ).fetchall()
        return {(r["paper_a"], r["paper_b"]) for r in rows}

    def review_queue(self, status: Optional[str] = "pending") -> List[ReviewItem]:
        sql = """SELECT q.*, pa.title AS title_a, pb.title AS title_b FROM review_queue q
                 LEFT JOIN papers pa ON pa.paper_id = q.paper_a LEFT JOIN papers pb ON pb.paper_id = q.paper_b"""
        args: Tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE q.status = ?"
            args = (status,)
        sql += " ORDER BY q.created_at, q.score DESC, q.paper_a, q.paper_b"
        return [
            ReviewItem(r["paper_a"], r["paper_b"], r["kind"], r["score"], tuple(json.loads(r["reasons"])),
                       json.loads(r["features"]), r["status"], r["created_at"], r["decided_at"], r["note"],
                       r["title_a"], r["title_b"])
            for r in self.db.execute(sql, args).fetchall()
        ]

    def _decide(self, a: str, b: str, status: str, note: Optional[str], now: str, kind: str = "manual") -> None:
        pa, pb = _pair(a, b)
        self.db.execute(
            "INSERT OR IGNORE INTO review_queue VALUES (?, ?, ?, NULL, '[]', '{}', 'pending', ?, NULL, NULL)",
            (pa, pb, kind, now),
        )
        self.db.execute(
            "UPDATE review_queue SET status = ?, decided_at = ?, note = COALESCE(?, note) WHERE paper_a = ? AND paper_b = ?",
            (status, now, note, pa, pb),
        )

    def mark_distinct(self, a: str, b: str, note: Optional[str] = None, at: Optional[dt.datetime] = None) -> None:
        """Reviewer decision: different works. The pair is never merged automatically or re-queued."""
        a, b = self.resolve(a), self.resolve(b)
        if a == b:
            raise StoreError(f"{a} and {b} are the same paper; use split() to separate a source record")
        with self.db:
            self._decide(a, b, "distinct", note, _iso(at or dt.datetime.now(dt.timezone.utc)))

    # ---- merge / split ----

    def merge_papers(self, keep: str, other: str, note: Optional[str] = None,
                     at: Optional[dt.datetime] = None) -> CanonicalPaper:
        """Reviewer decision: same work. ``other``'s source records move to ``keep``; ``other`` becomes a redirect."""
        keep, other = self.resolve(keep), self.resolve(other)
        now = _iso(at or dt.datetime.now(dt.timezone.utc))
        with self.db:
            self._merge(keep, other, "manual", None, now, note)
            return self._rebuild(keep)

    def _merge(self, keep: str, other: str, method: str, decision: Optional[MatchDecision], now: str,
               note: Optional[str] = None) -> None:
        if keep == other:
            raise StoreError(f"cannot merge {keep} into itself")
        for pid in (keep, other):
            row = self.db.execute("SELECT merged_into FROM papers WHERE paper_id = ?", (pid,)).fetchone()
            if row is None:
                raise StoreError(f"unknown paper {pid}")
            if row["merged_into"]:
                raise StoreError(f"{pid} was already merged into {row['merged_into']}")
        self.db.execute("UPDATE source_records SET paper_id = ? WHERE paper_id = ?", (keep, other))
        self.db.execute("UPDATE identifiers SET paper_id = ? WHERE paper_id = ?", (keep, other))
        self.db.execute("DELETE FROM title_tokens WHERE paper_id = ?", (other,))
        self.db.execute("DELETE FROM embeddings WHERE paper_id = ?", (other,))
        self.db.execute("UPDATE papers SET merged_into = ? WHERE paper_id = ? OR merged_into = ?", (keep, other, other))
        self._decide(keep, other, "merged", note, now, kind="fuzzy" if decision else "manual")
        self.db.execute(
            """UPDATE review_queue SET status = 'superseded', decided_at = ?
               WHERE status = 'pending' AND (paper_a = ? OR paper_b = ?)""",
            (now, other, other),
        )
        detail = {"reasons": list(decision.reasons), "features": decision.features} if decision else {}
        if note:
            detail["note"] = note
        self.db.execute(
            "INSERT INTO merge_log (at, action, survivor, other, method, score, detail) VALUES (?, 'merge', ?, ?, ?, ?, ?)",
            (now, keep, other, method, decision.score if decision else None, json.dumps(detail, sort_keys=True)),
        )
        self._rebuild(keep)

    def split(self, source: str, source_id: str, note: Optional[str] = None,
              at: Optional[dt.datetime] = None) -> Tuple[CanonicalPaper, CanonicalPaper]:
        """Undo a wrong merge: move one source record out to a paper of its own, and mark the pair distinct.

        Returns ``(paper_it_left, new_paper)``.
        """
        now = _iso(at or dt.datetime.now(dt.timezone.utc))
        with self.db:
            row = self.db.execute(
                "SELECT paper_id, record FROM source_records WHERE source = ? AND source_id = ?", (source, source_id)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown source record {source}:{source_id}")
            old = row["paper_id"]
            others = [r for r in self._records(old) if r.key != (source, source_id)]
            if not others:
                raise StoreError(f"{source}:{source_id} is the only record of {old}; nothing to split")
            record = SourceRecord.model_validate_json(row["record"])

            new_id = paper_id_for(record)
            existing = self.db.execute("SELECT merged_into FROM papers WHERE paper_id = ?", (new_id,)).fetchone()
            if existing is not None and existing["merged_into"]:
                self.db.execute("UPDATE papers SET merged_into = NULL WHERE paper_id = ?", (new_id,))
            elif existing is not None:
                new_id = self._fresh_id(record)
                existing = None
            if existing is None:
                self.db.execute(
                    "INSERT INTO papers (paper_id, first_seen, last_verified, canonical) VALUES (?, ?, ?, '{}')",
                    (new_id, now, now),
                )
            self.db.execute("UPDATE source_records SET paper_id = ? WHERE source = ? AND source_id = ?",
                            (new_id, source, source_id))
            kept = {(s, v) for r in others for s, v in r.identifiers.items()}
            for scheme, value in record.identifiers.items():
                if (scheme, value) not in kept:
                    self.db.execute("UPDATE identifiers SET paper_id = ? WHERE scheme = ? AND value = ? AND paper_id = ?",
                                    (new_id, scheme, value, old))
            self._decide(old, new_id, "distinct", note, now)
            self.db.execute(
                "INSERT INTO merge_log (at, action, survivor, other, method, score, detail) VALUES (?, 'split', ?, ?, 'manual', NULL, ?)",
                (now, old, new_id, json.dumps({"source": source, "source_id": source_id, "note": note}, sort_keys=True)),
            )
            return self._rebuild(old), self._rebuild(new_id)

    # ---- embeddings ----

    def get_embeddings(self, paper_ids: Sequence[str], model_version: str) -> Dict[str, Tuple[str, List[float]]]:
        """{paper_id: (text_sha256, vector)} for the papers that have one under ``model_version``."""
        from carousel.search.embeddings import unpack

        out: Dict[str, Tuple[str, List[float]]] = {}
        ids = list(paper_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows = self.db.execute(
                f"SELECT paper_id, text_sha256, vector FROM embeddings WHERE model_version = ? "
                f"AND paper_id IN ({','.join('?' * len(chunk))})",
                (model_version, *chunk),
            ).fetchall()
            out.update({r["paper_id"]: (r["text_sha256"], unpack(r["vector"])) for r in rows})
        return out

    def put_embeddings(self, rows: Sequence[Tuple[str, str, Sequence[float]]], model_version: str,
                       at: Optional[dt.datetime] = None) -> None:
        """Store ``(paper_id, text_sha256, vector)`` rows, replacing any older vector for the same model."""
        from carousel.search.embeddings import pack

        now = _iso(at or dt.datetime.now(dt.timezone.utc))
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?, ?, ?)",
                [(pid, model_version, h, len(v), pack(v), now) for pid, h, v in rows],
            )

    # ---- runs ----

    def save_run(self, run: Dict[str, Any], items: Sequence[Dict[str, Any]],
                 observations: Sequence[Tuple[str, str, str, str]]) -> None:
        """Persist one scan: run metadata, ranked items, and the (source, source_id, paper_id, status) it observed."""
        j = lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False)  # noqa: E731
        with self.db:
            self.db.execute(
                "INSERT INTO recommendation_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run["run_id"], run["radar_id"], run["radar_name"], run["config_hash"], run["as_of"],
                 run["started_at"], run["finished_at"], run["status"], run["ranking_version"],
                 run["candidates_version"], run.get("model_version"), run["mode"], run["n_observed"],
                 run["n_candidates"], len(items), j(run["coverage"]), j(run["ranking"]), j(run.get("notes", []))),
            )
            self.db.executemany(
                "INSERT INTO recommendation_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(run["run_id"], it["rank"], it["paper_id"], it["score"], it["mmr"], j(it["components"]),
                  j(it["details"]), j(it["flags"])) for it in items],
            )
            self.db.executemany("INSERT OR IGNORE INTO run_observations VALUES (?, ?, ?, ?, ?)",
                                [(run["run_id"], *o) for o in observations])

    def _run_row(self, r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for k in ("coverage", "ranking", "notes"):
            d[k] = json.loads(d[k])
        return d

    def list_runs(self, radar_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Runs, oldest first, without items."""
        sql, args = "SELECT * FROM recommendation_runs", ()
        if radar_id:
            sql, args = sql + " WHERE radar_id = ?", (radar_id,)
        return [self._run_row(r) for r in self.db.execute(sql + " ORDER BY started_at, run_id", args)]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM recommendation_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = self._run_row(row)
        run["items"] = [
            {**dict(r), "components": json.loads(r["components"]), "details": json.loads(r["details"]),
             "flags": json.loads(r["flags"])}
            for r in self.db.execute("SELECT * FROM recommendation_items WHERE run_id = ? ORDER BY rank", (run_id,))
        ]
        for it in run["items"]:
            it.pop("run_id", None)
        run["observations"] = [tuple(r) for r in self.db.execute(
            "SELECT source, source_id, paper_id, status FROM run_observations WHERE run_id = ? ORDER BY source, source_id",
            (run_id,))]
        return run

    def previous_run_id(self, radar_id: str, before: str) -> Optional[str]:
        """The latest run of ``radar_id`` that started before ``before`` (ISO timestamp)."""
        row = self.db.execute(
            "SELECT run_id FROM recommendation_runs WHERE radar_id = ? AND started_at < ? ORDER BY started_at DESC, run_id DESC LIMIT 1",
            (radar_id, before),
        ).fetchone()
        return row["run_id"] if row else None

    # ---- feedback ----

    def sync_feedback(self, radar_id: str, state: Dict[Tuple[str, str], str],
                      at: Optional[dt.datetime] = None) -> Tuple[int, int]:
        """Make the stored feedback for ``radar_id`` equal ``state`` ({(paper_id, action): ref}); log the changes.

        Returns (added, removed).
        """
        now = _iso(at or dt.datetime.now(dt.timezone.utc))
        with self.db:
            current = {(r["paper_id"], r["action"]): r["ref"] for r in
                       self.db.execute("SELECT * FROM feedback_state WHERE radar_id = ?", (radar_id,))}
            added = [k for k in state if k not in current]
            removed = [k for k in current if k not in state]
            for pid, action in added:
                self.db.execute("INSERT INTO feedback_state VALUES (?, ?, ?, ?, ?)", (radar_id, pid, action, state[(pid, action)], now))
                self.db.execute("INSERT INTO feedback_events (radar_id, paper_id, action, change, ref, at) VALUES (?, ?, ?, 'added', ?, ?)",
                                (radar_id, pid, action, state[(pid, action)], now))
            for pid, action in removed:
                self.db.execute("DELETE FROM feedback_state WHERE radar_id = ? AND paper_id = ? AND action = ?", (radar_id, pid, action))
                self.db.execute("INSERT INTO feedback_events (radar_id, paper_id, action, change, ref, at) VALUES (?, ?, ?, 'removed', ?, ?)",
                                (radar_id, pid, action, current[(pid, action)], now))
        return len(added), len(removed)

    def feedback_events(self, radar_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM feedback_events WHERE radar_id = ? ORDER BY id", (radar_id,))]

    def merge_log(self) -> List[Dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM merge_log ORDER BY id").fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ---- reads ----

    def resolve(self, paper_id: str) -> str:
        """Follow merge redirects to the paper that now holds this paper's records."""
        seen = set()
        while True:
            row = self.db.execute("SELECT merged_into FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown paper {paper_id}")
            if not row["merged_into"] or paper_id in seen:
                return paper_id
            seen.add(paper_id)
            paper_id = row["merged_into"]

    def get(self, paper_id: str) -> Optional[CanonicalPaper]:
        """The paper, following merge redirects (so an old ID still finds its paper)."""
        try:
            paper_id = self.resolve(paper_id)
        except StoreError:
            return None
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
        """Active (not merged-away) papers, oldest first."""
        sql = "SELECT paper_id FROM papers WHERE merged_into IS NULL"
        args: Tuple[Any, ...] = ()
        if seen_since is not None:
            sql += " AND first_seen >= ?"
            args = (_iso(seen_since),)
        return [r["paper_id"] for r in self.db.execute(sql + " ORDER BY first_seen, paper_id", args)]

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
            "papers": q("SELECT COUNT(*) FROM papers WHERE merged_into IS NULL"),
            "merged_away": q("SELECT COUNT(*) FROM papers WHERE merged_into IS NOT NULL"),
            "source_records": q("SELECT COUNT(*) FROM source_records"),
            "identifiers": q("SELECT COUNT(*) FROM identifiers"),
            "conflicts": q("SELECT COUNT(*) FROM identifier_conflicts"),
            "review_pending": q("SELECT COUNT(*) FROM review_queue WHERE status = 'pending'"),
            "runs": q("SELECT COUNT(*) FROM recommendation_runs"),
            "embeddings": dict(self.db.execute(
                "SELECT model_version, COUNT(*) FROM embeddings GROUP BY model_version ORDER BY model_version").fetchall()),
            "by_source": by_source,
        }


__all__ = ["Conflict", "Observation", "PaperStore", "ReviewItem", "StoreError", "STORE_SCHEMA_VERSION"]
