#!/usr/bin/env python3
"""
Immutable run snapshots for CAROUSEL.

Every pipeline run is stored as one gzipped JSON envelope so later stages
(Research Delta, Momentum, time-forward evaluation) can compare runs over time.
Snapshots are append-only; an existing snapshot file is never rewritten.

Layout under --root (the checked-out `snapshots` branch):

    snapshots/<radar_id>/<YYYY>/<YYYY-MM-DDTHHMMSSZ>.json.gz
    snapshots/<radar_id>/index.jsonl      # one metadata line per snapshot, sorted by time

Commands:

    # After a pipeline run (used by the GitHub workflow)
    python scripts/snapshot.py write --input docs/latest.json --root ../snapshots --radar-id lab-default

    # One-time: recover past runs from git history of docs/latest.json
    python scripts/snapshot.py backfill --repo . --ref main --path docs/latest.json \
        --root ../snapshots --radar-id lab-default

    # Rebuild index.jsonl from the snapshot files
    python scripts/snapshot.py reindex --root ../snapshots --radar-id lab-default

Stdlib only, so it runs before any package install.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

SCHEMA = "carousel.snapshot/0"


# -------------------------
# Envelope construction
# -------------------------

def payload_sha256(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_generated_at(payload: Dict[str, Any]) -> Optional[dt.datetime]:
    raw = payload.get("generated_at_utc")
    if not isinstance(raw, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _count_items(payload: Dict[str, Any]) -> int:
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return 0
    return sum(len(s.get("items") or []) for s in sections if isinstance(s, dict))


def build_envelope(
    payload: Dict[str, Any],
    radar_id: str,
    source: str,
    fallback_time: dt.datetime,
    provenance: Dict[str, Any],
) -> Dict[str, Any]:
    captured = _parse_generated_at(payload) or fallback_time.astimezone(dt.timezone.utc)
    return {
        "schema": SCHEMA,
        "radar_id": radar_id,
        "captured_at_utc": captured.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "provenance": provenance,
        "window_days": payload.get("days"),
        "window_utc": payload.get("window_utc"),
        "ranking_version": payload.get("ranking_version", "unversioned"),
        "n_sections": len(payload.get("sections") or []),
        "n_items": _count_items(payload),
        "payload_sha256": payload_sha256(payload),
        "payload": payload,
    }


def snapshot_relpath(envelope: Dict[str, Any]) -> Path:
    ts = envelope["captured_at_utc"]  # 2026-09-13T16:43:14Z
    stamp = ts.replace(":", "")  # 2026-09-13T164314Z
    return Path("snapshots") / envelope["radar_id"] / ts[:4] / f"{stamp}.json.gz"


def write_envelope(root: Path, envelope: Dict[str, Any]) -> Tuple[Path, bool]:
    """Write a snapshot. Returns (path, written). Never overwrites an existing file."""
    path = root / snapshot_relpath(envelope)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = path.with_suffix(".tmp")
    # mtime=0 keeps the gzip bytes deterministic for identical content.
    with open(tmp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        gz.write(data)
    os.replace(tmp, path)
    return path, True


# -------------------------
# Index
# -------------------------

INDEX_FIELDS = (
    "captured_at_utc",
    "source",
    "window_days",
    "ranking_version",
    "n_sections",
    "n_items",
    "payload_sha256",
)


def iter_snapshots(root: Path, radar_id: str) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    base = root / "snapshots" / radar_id
    for path in sorted(base.glob("*/*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            yield path, json.load(fh)


def reindex(root: Path, radar_id: str) -> Path:
    rows: List[Dict[str, Any]] = []
    for path, env in iter_snapshots(root, radar_id):
        row = {k: env.get(k) for k in INDEX_FIELDS}
        row["path"] = str(path.relative_to(root))
        row["provenance"] = env.get("provenance", {})
        rows.append(row)
    rows.sort(key=lambda r: (r["captured_at_utc"], r["path"]))
    index = root / "snapshots" / radar_id / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with open(index, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return index


# -------------------------
# Commands
# -------------------------

def cmd_write(args: argparse.Namespace) -> int:
    with open(args.input, encoding="utf-8") as fh:
        payload = json.load(fh)
    provenance = {
        k: v
        for k, v in {
            "git_commit": args.git_commit or os.environ.get("GITHUB_SHA"),
            "workflow_run_id": args.run_id or os.environ.get("GITHUB_RUN_ID"),
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
            "event": os.environ.get("GITHUB_EVENT_NAME"),
        }.items()
        if v
    }
    env = build_envelope(
        payload,
        radar_id=args.radar_id,
        source=args.source,
        fallback_time=dt.datetime.now(dt.timezone.utc),
        provenance=provenance,
    )
    root = Path(args.root)
    path, written = write_envelope(root, env)
    reindex(root, args.radar_id)
    print(("wrote " if written else "exists, skipped ") + str(path))
    return 0


def _git(repo: str, *argv: str) -> bytes:
    return subprocess.run(["git", "-C", repo, *argv], check=True, capture_output=True).stdout


def cmd_backfill(args: argparse.Namespace) -> int:
    log = _git(args.repo, "log", "--format=%H%x09%cI%x09%s", args.ref, "--", args.path).decode()
    commits = [line.split("\t", 2) for line in log.splitlines() if line.strip()]
    root = Path(args.root)
    written = skipped = failed = 0
    for sha, committed_iso, subject in reversed(commits):
        try:
            payload = json.loads(_git(args.repo, "show", f"{sha}:{args.path}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            failed += 1
            print(f"skip {sha[:8]}: {exc.__class__.__name__}", file=sys.stderr)
            continue
        env = build_envelope(
            payload,
            radar_id=args.radar_id,
            source="git-backfill",
            fallback_time=dt.datetime.fromisoformat(committed_iso),
            provenance={"git_commit": sha, "commit_message": subject, "committed_at": committed_iso},
        )
        _, ok = write_envelope(root, env)
        written += ok
        skipped += not ok
    index = reindex(root, args.radar_id)
    print(f"backfill: {written} written, {skipped} already present, {failed} unreadable; index -> {index}")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    print(reindex(Path(args.root), args.radar_id))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="Snapshot one pipeline output")
    w.add_argument("--input", required=True)
    w.add_argument("--root", required=True, help="Checkout of the snapshots branch")
    w.add_argument("--radar-id", required=True)
    w.add_argument("--source", default="workflow")
    w.add_argument("--git-commit")
    w.add_argument("--run-id")
    w.set_defaults(func=cmd_write)

    b = sub.add_parser("backfill", help="Recover snapshots from git history of an output file")
    b.add_argument("--repo", default=".")
    b.add_argument("--ref", default="main")
    b.add_argument("--path", default="docs/latest.json")
    b.add_argument("--root", required=True)
    b.add_argument("--radar-id", required=True)
    b.set_defaults(func=cmd_backfill)

    r = sub.add_parser("reindex", help="Rebuild index.jsonl")
    r.add_argument("--root", required=True)
    r.add_argument("--radar-id", required=True)
    r.set_defaults(func=cmd_reindex)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
