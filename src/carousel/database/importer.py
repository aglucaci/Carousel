"""Import Phase 1A data into the Phase 1B Postgres schema.

    carousel db upgrade --url postgresql://...
    carousel db import-store carousel.db --url ...
    carousel db import-snapshots path/to/snapshots-checkout --url ...

Both imports are idempotent: re-running them inserts nothing twice. Store
imports update mutable rows (papers, sources, reviews) to the store's
current state and never rewrite immutable ones (runs, items, merge log,
feedback events). Snapshot imports verify that the number of snapshots and
items in Postgres matches each radar's ``index.jsonl`` and raise
:class:`ImportCountMismatch` if not.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from carousel.core.paper import CanonicalPaper, partial_date_bounds
from carousel.database import models as m
from carousel.database.local import PaperStore, _parse
from carousel.search.embeddings import unpack


class ImportCountMismatch(RuntimeError):
    pass


def _ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return _parse(s)
    except ValueError:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _paper_row(p: CanonicalPaper) -> Dict[str, Any]:
    d = p.dates
    public = p.earliest_public_date
    return {
        "id": p.paper_id, "status": "active", "title": p.title, "abstract": p.abstract, "venue": p.venue,
        "language": p.language, "url": p.url, "publication_types": list(p.publication_types),
        "is_retracted": p.is_retracted, "published": d.published, "first_online": d.first_online,
        "preprint_posted": d.preprint_posted, "version_date": d.version, "source_updated": d.source_updated,
        "earliest_public_date": partial_date_bounds(public)[0] if public else None,
        "first_seen": p.first_seen, "last_verified": p.last_verified,
        "canonical": p.model_dump(mode="json", exclude={"sources"}),
    }


def _upsert(conn, table, rows: List[Dict[str, Any]], keys: Sequence[str], update_cols: Optional[Sequence[str]] = None,
            chunk: int = 1000) -> None:
    """INSERT ... ON CONFLICT (keys) DO UPDATE (update_cols) / DO NOTHING when update_cols is empty."""
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        stmt = pg_insert(table).values(batch)
        if update_cols:
            stmt = stmt.on_conflict_do_update(index_elements=list(keys),
                                              set_={c: getattr(stmt.excluded, c) for c in update_cols})
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(keys) or None)
        conn.execute(stmt)


def _count(conn, table, where=None) -> int:
    q = select(func.count()).select_from(table)
    if where is not None:
        q = q.where(where)
    return int(conn.execute(q).scalar_one())


# -------------------------
# 1A store
# -------------------------

def import_store(store: Union[str, Path, PaperStore], engine: Engine) -> Dict[str, int]:
    """Copy a Phase 1A SQLite store into Postgres. Returns the number of source rows per table."""
    own = not isinstance(store, PaperStore)
    st = PaperStore(store) if own else store
    try:
        return _import_store(st, engine)
    finally:
        if own:
            st.close()


def _import_store(st: PaperStore, engine: Engine) -> Dict[str, int]:
    db = st.db
    T = {t.name: t for t in m.Base.metadata.sorted_tables}
    counts: Dict[str, int] = {}
    with engine.begin() as conn:
        # ---- radars and their versions (from runs and feedback) ----
        runs = [dict(r) for r in db.execute("SELECT * FROM recommendation_runs ORDER BY started_at, run_id")]
        radar_names: Dict[str, str] = {}
        for r in runs:
            radar_names.setdefault(r["radar_id"], r["radar_name"])
        for r in db.execute("SELECT DISTINCT radar_id FROM feedback_events"):
            radar_names.setdefault(r["radar_id"], r["radar_id"])
        _upsert(conn, T["radars"], [{"id": k, "title": v} for k, v in sorted(radar_names.items())], ["id"])
        versions: Dict[Tuple[str, str], dt.datetime] = {}
        for r in runs:
            key = (r["radar_id"], r["config_hash"])
            versions[key] = min(versions.get(key, _ts(r["started_at"])), _ts(r["started_at"]))
        _upsert(conn, T["radar_versions"], [{"radar_id": a, "config_hash": b, "effective_at": t}
                                            for (a, b), t in sorted(versions.items())], ["radar_id", "config_hash"])
        version_ids = {(r.radar_id, r.config_hash): r.id for r in conn.execute(select(m.RadarVersion))}

        # ---- papers (merged ones too, so old runs stay joinable) ----
        rows = [dict(r) for r in db.execute("SELECT paper_id, title, merged_into, first_seen, last_verified, canonical FROM papers")]
        paper_rows, author_lists = [], {}
        for r in rows:
            p = st.get(r["paper_id"]) if not r["merged_into"] else None
            if p is not None and p.paper_id == r["paper_id"]:
                paper_rows.append(_paper_row(p))
                author_lists[p.paper_id] = p.authors
            else:
                paper_rows.append({
                    "id": r["paper_id"], "status": "merged", "title": r["title"], "abstract": None, "venue": None,
                    "language": None, "url": None, "publication_types": [], "is_retracted": False, "published": None,
                    "first_online": None, "preprint_posted": None, "version_date": None, "source_updated": None,
                    "earliest_public_date": None, "first_seen": _ts(r["first_seen"]), "last_verified": _ts(r["last_verified"]),
                    "canonical": json.loads(r["canonical"] or "{}"),
                })
        cols = [c for c in paper_rows[0] if c != "id"] if paper_rows else []
        _upsert(conn, T["papers"], paper_rows, ["id"], cols)
        for r in rows:
            if r["merged_into"]:
                conn.execute(update(m.Paper).where(m.Paper.id == r["paper_id"]).values(merged_into=r["merged_into"], status="merged"))
        counts["papers"] = len(paper_rows)

        ident = [{"identifier_type": r["scheme"], "value": r["value"], "paper_id": r["paper_id"]}
                 for r in db.execute("SELECT * FROM identifiers")]
        _upsert(conn, T["paper_identifiers"], ident, ["identifier_type", "value"], ["paper_id"])
        counts["paper_identifiers"] = len(ident)

        sources = [{"provider": r["source"], "provider_id": r["source_id"], "paper_id": r["paper_id"],
                    "first_seen": _ts(r["first_seen"]), "last_checked": _ts(r["last_verified"]),
                    "content_changed_at": _ts(r["content_changed_at"]), "content_sha256": r["content_sha256"],
                    "record": json.loads(r["record"])} for r in db.execute("SELECT * FROM source_records")]
        _upsert(conn, T["paper_sources"], sources, ["provider", "provider_id"],
                ["paper_id", "first_seen", "last_checked", "content_changed_at", "content_sha256", "record"])
        counts["paper_sources"] = len(sources)

        # ---- authors ----
        n_links = 0
        for pid, authors in sorted(author_lists.items()):
            conn.execute(m.PaperAuthor.__table__.delete().where(m.PaperAuthor.paper_id == pid))
            links = []
            for pos, a in enumerate(authors, 1):
                if a.orcid:
                    stmt = pg_insert(m.Author).values(name=a.name, orcid=a.orcid, identity="orcid")
                    stmt = stmt.on_conflict_do_update(index_elements=["orcid"], set_={"name": stmt.excluded.name})
                    aid = conn.execute(stmt.returning(m.Author.id)).scalar_one()
                else:
                    stmt = pg_insert(m.Author).values(name=a.name, identity="name_only").on_conflict_do_nothing(
                        index_elements=["name"], index_where=text("orcid IS NULL"))
                    conn.execute(stmt)
                    aid = conn.execute(select(m.Author.id).where(m.Author.name == a.name, m.Author.orcid.is_(None))).scalar_one()
                links.append({"paper_id": pid, "position": pos, "author_id": aid})
            if links:
                conn.execute(insert(m.PaperAuthor), links)
            n_links += len(links)
        counts["paper_authors"] = n_links

        # ---- embeddings ----
        emb = [{"paper_id": r["paper_id"], "model_version": r["model_version"], "text_sha256": r["text_sha256"],
                "dim": r["dim"], "embedding": unpack(r["vector"]), "created_at": _ts(r["created_at"])}
               for r in db.execute("SELECT * FROM embeddings")]
        _upsert(conn, T["paper_embeddings"], emb, ["paper_id", "model_version"], ["text_sha256", "dim", "embedding"], chunk=200)
        counts["paper_embeddings"] = len(emb)

        # ---- texts and annotations (licence per text) ----
        def owner(pid: str) -> str:
            return st.resolve(pid)

        texts = [{"source": r["source"], "source_id": r["source_id"], "kind": r["kind"], "paper_id": owner(r["paper_id"]),
                  "format": r["format"], "license": r["license"], "license_url": r["license_url"],
                  "license_source": r["license_source"], "reuse": r["reuse"], "url": r["url"], "sha256": r["sha256"],
                  "sections": json.loads(r["sections"]), "retrieved_at": _ts(r["retrieved_at"])}
                 for r in db.execute("SELECT * FROM paper_texts")]
        cols = ["paper_id", "format", "license", "license_url", "license_source", "reuse", "url", "sha256", "sections",
                "retrieved_at"]
        _upsert(conn, T["paper_texts"], texts, ["source", "source_id", "kind"], cols, chunk=50)
        counts["paper_texts"] = len(texts)
        notes = [{"source": r["source"], "article_id": r["article_id"], "paper_id": owner(r["paper_id"]),
                  "annotations": json.loads(r["annotations"]), "retrieved_at": _ts(r["retrieved_at"])}
                 for r in db.execute("SELECT * FROM paper_annotations")]
        _upsert(conn, T["paper_annotations"], notes, ["source", "article_id"], ["paper_id", "annotations", "retrieved_at"])
        counts["paper_annotations"] = len(notes)

        # ---- version links (preprint <-> publication; the store is authoritative, so they are replaced) ----
        conn.execute(delete(m.PaperVersion).where(m.PaperVersion.version_type.in_(("preprint", "published"))))
        version_rows = []
        for pre, pub, evidence, created, updated in st.all_version_links():
            method = evidence[0].get("method", "source") if evidence else "source"
            details = {"evidence": evidence, "linked_at": created, "updated_at": updated}
            for pid, vtype, other in ((pub, "preprint", pre), (pre, "published", pub)):
                related = st.get(other)
                version_rows.append({"paper_id": pid, "version_type": vtype, "related_paper_id": other, "source": method[:16],
                                     "date": related.earliest_public_date if related else None, "details": details})
        if version_rows:
            conn.execute(insert(m.PaperVersion), version_rows)
        counts["paper_versions"] = len(version_rows)

        # ---- dedup audit ----
        reviews = [{"paper_a": r["paper_a"], "paper_b": r["paper_b"], "kind": r["kind"], "score": r["score"],
                    "reasons": json.loads(r["reasons"]), "features": json.loads(r["features"]), "status": r["status"],
                    "created_at": _ts(r["created_at"]), "decided_at": _ts(r["decided_at"]), "note": r["note"]}
                   for r in db.execute("SELECT * FROM review_queue")]
        _upsert(conn, T["dedup_reviews"], reviews, ["paper_a", "paper_b"], ["status", "decided_at", "note", "score", "reasons", "features"])
        counts["dedup_reviews"] = len(reviews)
        log = [{"at": _ts(r["at"]), "action": r["action"], "survivor": r["survivor"], "other": r["other"], "method": r["method"],
                "score": r["score"], "detail": json.loads(r["detail"]), "source_log_id": r["id"]}
               for r in db.execute("SELECT * FROM merge_log ORDER BY id")]
        _upsert(conn, T["merge_log"], log, ["at", "action", "survivor", "other"])
        counts["merge_log"] = len(log)
        conflicts = [{"identifier_type": r["scheme"], "value": r["value"], "claimed_by_paper_id": r["claimed_by_paper_id"],
                      "provider": r["source"], "provider_id": r["source_id"], "owner_paper_id": r["owner_paper_id"],
                      "observed_at": _ts(r["observed_at"])} for r in db.execute("SELECT * FROM identifier_conflicts")]
        _upsert(conn, T["identifier_conflicts"], conflicts,
                ["identifier_type", "value", "claimed_by_paper_id", "provider", "provider_id"])
        counts["identifier_conflicts"] = len(conflicts)

        # ---- runs (immutable) ----
        run_rows = [{
            "id": r["run_id"], "radar_id": r["radar_id"], "radar_version_id": version_ids.get((r["radar_id"], r["config_hash"])),
            "executed_at": _ts(r["started_at"]), "finished_at": _ts(r["finished_at"]), "as_of": dt.date.fromisoformat(r["as_of"]),
            "status": r["status"], "config_hash": r["config_hash"], "ranking_version": r["ranking_version"],
            "explanation_version": r["explanation_version"],
            "candidates_version": r["candidates_version"], "model_version": r["model_version"], "mode": r["mode"],
            "n_observed": r["n_observed"], "n_candidates": r["n_candidates"], "n_items": r["n_items"],
            "coverage": json.loads(r["coverage"]), "ranking": json.loads(r["ranking"]), "notes": json.loads(r["notes"]),
        } for r in runs]
        _upsert(conn, T["recommendation_runs"], run_rows, ["id"])
        counts["recommendation_runs"] = len(run_rows)
        items = [{"run_id": r["run_id"], "rank": r["rank"], "paper_id": r["paper_id"], "score": r["score"], "mmr": r["mmr"],
                  "features": json.loads(r["components"]), "explanation": json.loads(r["details"]),
                  "flags": json.loads(r["flags"])} for r in db.execute("SELECT * FROM recommendation_items")]
        _upsert(conn, T["recommendation_items"], items, ["run_id", "rank"])
        counts["recommendation_items"] = len(items)
        obs = [{"run_id": r["run_id"], "provider": r["source"], "provider_id": r["source_id"], "paper_id": r["paper_id"],
                "status": r["status"]} for r in db.execute("SELECT * FROM run_observations")]
        _upsert(conn, T["run_observations"], obs, ["run_id", "provider", "provider_id"])
        counts["run_observations"] = len(obs)

        # ---- feedback history ----
        fb = [{"radar_id": r["radar_id"], "paper_id": st.resolve(r["paper_id"]), "action": r["action"], "change": r["change"],
               "ref": r["ref"], "origin": "file", "at": _ts(r["at"])} for r in db.execute("SELECT * FROM feedback_events ORDER BY id")]
        _upsert(conn, T["user_feedback"], fb, ["radar_id", "paper_id", "action", "change", "at", "origin"])
        counts["user_feedback"] = len(fb)
    return counts


# -------------------------
# Snapshots
# -------------------------

@dataclasses.dataclass
class SnapshotImport:
    radar_id: str
    expected_snapshots: int
    expected_items: int
    snapshots: int
    items: int
    inserted_snapshots: int
    linked_items: int

    @property
    def ok(self) -> bool:
        return (self.snapshots, self.items) == (self.expected_snapshots, self.expected_items)


def _flatten(payload: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Items of an ETHER-era payload (sections) or a CAROUSEL run export (items), in order."""
    if isinstance(payload.get("items"), list):
        for it in payload["items"]:
            paper = (it.get("details") or {}).get("paper") or {}
            yield {"section": None, "rank": it.get("rank"), "title": paper.get("title"), "pmid": paper.get("pmid"),
                   "doi": paper.get("doi"), "score": it.get("score"), "paper_id": it.get("paper_id"), "raw": it}
        return
    for sec in payload.get("sections") or []:
        for i, it in enumerate(sec.get("items") or [], 1):
            score = it.get("score")
            yield {"section": sec.get("label") or sec.get("name"), "rank": i, "title": it.get("title"),
                   "pmid": str(it["pmid"]) if it.get("pmid") else None, "doi": it.get("doi"),
                   "score": float(score) if isinstance(score, (int, float)) else None, "paper_id": None, "raw": it}


def import_snapshots(root: Union[str, Path], engine: Engine, radar_ids: Optional[Iterable[str]] = None) -> List[SnapshotImport]:
    """Import ``snapshots/<radar_id>/...`` from a checkout of the snapshots branch, then verify counts."""
    base = Path(root) / "snapshots"
    wanted = set(radar_ids) if radar_ids else None
    results = []
    for index in sorted(base.glob("*/index.jsonl")):
        rid = index.parent.name
        if wanted is not None and rid not in wanted:
            continue
        rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
        results.append(_import_radar_snapshots(Path(root), rid, rows, engine))
    bad = [r for r in results if not r.ok]
    if bad:
        raise ImportCountMismatch("; ".join(
            f"{r.radar_id}: {r.snapshots}/{r.expected_snapshots} snapshots, {r.items}/{r.expected_items} items" for r in bad))
    return results


def _import_radar_snapshots(root: Path, rid: str, rows: List[Dict[str, Any]], engine: Engine) -> SnapshotImport:
    inserted = linked = 0
    with engine.begin() as conn:
        known_papers: Set[str] = set(conn.execute(select(m.Paper.id)).scalars())
        by_ident = {(t, v): pid for t, v, pid in conn.execute(
            select(m.PaperIdentifier.identifier_type, m.PaperIdentifier.value, m.PaperIdentifier.paper_id)
            .where(m.PaperIdentifier.identifier_type.in_(["pmid", "doi"])))}
        for row in rows:
            with gzip.open(root / row["path"], "rt", encoding="utf-8") as fh:
                env = json.load(fh)
            captured = dt.datetime.strptime(env["captured_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            stmt = pg_insert(m.RunSnapshot).values(
                radar_id=rid, captured_at=captured, source=env.get("source", ""),
                ranking_version=env.get("ranking_version") or "unversioned", run_id=env.get("run_id"),
                window_days=env.get("window_days"), window=env.get("window_utc"), n_items=env.get("n_items", 0),
                payload_sha256=env["payload_sha256"], path=row["path"], provenance=env.get("provenance") or {},
                payload=env["payload"],
            ).on_conflict_do_nothing(index_elements=["radar_id", "captured_at", "payload_sha256"]).returning(m.RunSnapshot.id)
            sid = conn.execute(stmt).scalar_one_or_none()
            if sid is None:
                continue  # already imported
            inserted += 1
            items = []
            for pos, it in enumerate(_flatten(env["payload"]), 1):
                pid = it["paper_id"] if it["paper_id"] in known_papers else None
                pid = pid or by_ident.get(("pmid", it["pmid"])) or by_ident.get(("doi", (it["doi"] or "").lower()))
                linked += pid is not None
                items.append({**it, "snapshot_id": sid, "position": pos, "paper_id": pid})
            if items:
                conn.execute(insert(m.SnapshotItem), items)
        n_snap = _count(conn, m.RunSnapshot.__table__, m.RunSnapshot.radar_id == rid)
        n_items = int(conn.execute(
            select(func.count()).select_from(m.SnapshotItem).join(m.RunSnapshot)
            .where(m.RunSnapshot.radar_id == rid)).scalar_one())
    return SnapshotImport(rid, len(rows), sum(int(r.get("n_items") or 0) for r in rows), n_snap, n_items, inserted, linked)
