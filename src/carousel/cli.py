"""
carousel command-line interface.

    carousel validate RADAR.yml [RADAR.yml ...]   # check configs; exit 1 if any are invalid
    carousel show RADAR.yml [--format json|yaml]  # print the resolved config (defaults filled in)

    carousel review list STORE.db [--status pending|merged|distinct|all] [--format text|json]
    carousel review merge STORE.db KEEP_ID OTHER_ID [--note TEXT]    # same work
    carousel review distinct STORE.db ID_A ID_B [--note TEXT]        # different works
    carousel review split STORE.db SOURCE SOURCE_ID [--note TEXT]    # undo a wrong merge

    carousel scan RADAR.yml [--store carousel.db] [--as-of DATE] [--top N] [--mode M]
                  [--embedder auto|semantic|hashing|none] [--csv OUT.csv] [--json OUT.json]
    carousel runs [--store carousel.db] [--radar ID]                 # list stored runs
    carousel delta RADAR.yml [--store carousel.db]                   # latest run vs the one before
    carousel export RUN_ID (--csv OUT.csv | --json OUT.json) [--store carousel.db]
    carousel feedback RADAR.yml save|dismiss|relevant|irrelevant REF... [--undo]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import List, Optional

import yaml

from carousel import __version__
from carousel.core.config import ConfigError, RadarConfig, load_radar


def _summary(cfg: RadarConfig) -> str:
    return (
        f"radar_id={cfg.radar_id} sources={','.join(s.value for s in cfg.sources)} "
        f"queries={len(cfg.queries)} signal_categories={len(cfg.signal_profile)} "
        f"window={cfg.filters.days}d hash={cfg.config_hash()}"
    )


def cmd_validate(args: argparse.Namespace) -> int:
    failed = 0
    ids: dict = {}
    for path in args.paths:
        try:
            cfg = load_radar(path)
        except ConfigError as exc:
            failed += 1
            print(f"FAIL {exc}", file=sys.stderr)
            continue
        if cfg.radar_id in ids:
            failed += 1
            print(f"FAIL {path}: radar_id {cfg.radar_id!r} is also used by {ids[cfg.radar_id]}", file=sys.stderr)
            continue
        ids[cfg.radar_id] = path
        print(f"ok   {path}  {_summary(cfg)}")
        if not args.quiet:
            for w in cfg.warnings():
                print(f"     warning: {w}")
    if len(args.paths) > 1:
        print(f"{len(args.paths) - failed}/{len(args.paths)} valid", file=sys.stderr if failed else sys.stdout)
    return 1 if failed else 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        cfg = load_radar(args.path)
    except ConfigError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    data = {"config_hash": cfg.config_hash(), **cfg.model_dump(mode="json")}
    if args.format == "json":
        print(json.dumps(data, indent=2))
    else:
        print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), end="")
    return 0


def _open_store(path: str):
    from pathlib import Path

    from carousel.database import PaperStore

    if not Path(path).exists():
        raise FileNotFoundError(f"{path}: no such store")
    return PaperStore(path)


def cmd_review(args: argparse.Namespace) -> int:
    from carousel.database import StoreError

    try:
        store = _open_store(args.store)
    except (FileNotFoundError, StoreError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    try:
        with store:
            if args.action == "list":
                items = store.review_queue(None if args.status == "all" else args.status)
                if args.format == "json":
                    print(json.dumps([dataclasses.asdict(i) for i in items], indent=2))
                    return 0
                for i in items:
                    score = "  -  " if i.score is None else f"{i.score:.3f}"
                    print(f"[{i.status}] {i.kind} score={score} reasons={','.join(i.reasons)}")
                    print(f"    {i.paper_a}  {i.title_a or ''}")
                    print(f"    {i.paper_b}  {i.title_b or ''}")
                print(f"{len(items)} item(s)")
            elif args.action == "merge":
                paper = store.merge_papers(args.keep, args.other, note=args.note)
                print(f"merged {args.other} into {paper.paper_id} ({len(paper.sources)} source records)")
            elif args.action == "distinct":
                store.mark_distinct(args.a, args.b, note=args.note)
                print(f"marked {args.a} and {args.b} as different works")
            elif args.action == "split":
                left, new = store.split(args.source, args.source_id, note=args.note)
                print(f"split {args.source}:{args.source_id} out of {left.paper_id} into {new.paper_id}")
    except StoreError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


def _load_radar_or_fail(path: str):
    from carousel.radar import Radar

    try:
        return Radar.from_yaml(path)
    except ConfigError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return None


def cmd_scan(args: argparse.Namespace) -> int:
    import datetime as dt

    from carousel.feedback import FeedbackError

    radar = _load_radar_or_fail(args.radar)
    if radar is None:
        return 1
    if args.feedback:
        from pathlib import Path

        radar.feedback_path = Path(args.feedback)
    try:
        res = radar.scan(
            store=args.store, as_of=dt.date.fromisoformat(args.as_of) if args.as_of else None, top_n=args.top,
            max_results=args.max_results, mode=args.mode, embedder=args.embedder,
        )
    except (FeedbackError, ValueError, ImportError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(res.summary(top=args.top))
    if args.csv:
        print(f"wrote {res.to_csv(args.csv)}")
    if args.json:
        res.to_json(args.json)
        print(f"wrote {args.json}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    from carousel.database import StoreError

    try:
        store = _open_store(args.store)
    except (FileNotFoundError, StoreError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    with store:
        runs = store.list_runs(args.radar)
    for r in runs:
        print(f"{r['run_id']}  {r['radar_id']:<20} as_of={r['as_of']}  started={r['started_at'][:19]}  "
              f"{r['status']:<7} items={r['n_items']:<3} ranking={r['ranking_version']} model={r['model_version'] or '-'}")
    print(f"{len(runs)} run(s)")
    return 0


def cmd_delta(args: argparse.Namespace) -> int:
    radar = _load_radar_or_fail(args.radar)
    if radar is None:
        return 1
    res = radar.results(store=args.store)
    if res is None:
        print(f"FAIL no runs of {radar.radar_id} in {args.store}", file=sys.stderr)
        return 1
    print(json.dumps(res.delta(), indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from carousel.database import StoreError
    from carousel.radar import ScanResults

    try:
        store = _open_store(args.store)
    except (FileNotFoundError, StoreError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    with store:
        run = store.get_run(args.run_id)
        if run is None:
            print(f"FAIL unknown run {args.run_id}", file=sys.stderr)
            return 1
        prev_id = store.previous_run_id(run["radar_id"], run["started_at"])
        res = ScanResults(run, store.get_run(prev_id) if prev_id else None)
    if args.csv:
        print(f"wrote {res.to_csv(args.csv)}")
    if args.json:
        res.to_json(args.json)
        print(f"wrote {args.json}")
    if not (args.csv or args.json):
        print(res.to_json())
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    from carousel import feedback as fb

    radar = _load_radar_or_fail(args.radar)
    if radar is None:
        return 1
    path = args.file or radar.feedback_path
    try:
        fb.update(path, args.action, args.refs, remove=args.undo, radar_id=radar.radar_id)
    except fb.FeedbackError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    verb = "removed from" if args.undo else "added to"
    print(f"{len(args.refs)} reference(s) {verb} `{args.action}` in {path}; applied on the next scan")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="carousel", description="CAROUSEL research radar tools.")
    ap.add_argument("--version", action="version", version=f"carousel {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    v = sub.add_parser("validate", help="Validate radar config files")
    v.add_argument("paths", nargs="+", metavar="RADAR.yml")
    v.add_argument("-q", "--quiet", action="store_true", help="Hide warnings")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("show", help="Print a radar's resolved config")
    s.add_argument("path", metavar="RADAR.yml")
    s.add_argument("--format", choices=("yaml", "json"), default="yaml")
    s.set_defaults(func=cmd_show)

    r = sub.add_parser("review", help="Review possible duplicate papers in a store")
    rs = r.add_subparsers(dest="action", required=True, metavar="ACTION")
    rl = rs.add_parser("list", help="List the review queue")
    rl.add_argument("store", metavar="STORE.db")
    rl.add_argument("--status", choices=("pending", "merged", "distinct", "superseded", "all"), default="pending")
    rl.add_argument("--format", choices=("text", "json"), default="text")
    rm = rs.add_parser("merge", help="Same work: move OTHER's records into KEEP")
    rm.add_argument("store", metavar="STORE.db")
    rm.add_argument("keep", metavar="KEEP_ID")
    rm.add_argument("other", metavar="OTHER_ID")
    rd = rs.add_parser("distinct", help="Different works: never merge or re-queue this pair")
    rd.add_argument("store", metavar="STORE.db")
    rd.add_argument("a", metavar="ID_A")
    rd.add_argument("b", metavar="ID_B")
    rp = rs.add_parser("split", help="Undo a wrong merge: give one source record its own paper")
    rp.add_argument("store", metavar="STORE.db")
    rp.add_argument("source")
    rp.add_argument("source_id")
    for x in (rm, rd, rp):
        x.add_argument("--note", help="Why (kept in the queue and merge log)")
    r.set_defaults(func=cmd_review)

    sc = sub.add_parser("scan", help="Run a radar: query sources, dedup, rank, and save the run")
    sc.add_argument("radar", metavar="RADAR.yml")
    sc.add_argument("--store", default="carousel.db", help="Paper store (default: carousel.db)")
    sc.add_argument("--as-of", help="End date of the window, YYYY-MM-DD (default: today, UTC)")
    sc.add_argument("--top", type=int, default=25, help="How many papers to recommend (default 25)")
    sc.add_argument("--max-results", type=int, default=200, help="Per-source result cap (default 200)")
    sc.add_argument("--mode", choices=("focused", "balanced", "exploratory"), help="Override the radar's mode")
    sc.add_argument("--embedder", choices=("auto", "semantic", "hashing", "none"), default="auto")
    sc.add_argument("--feedback", help="Feedback file (default: RADAR.feedback.yml next to the radar)")
    sc.add_argument("--csv", help="Also write the recommendations as CSV")
    sc.add_argument("--json", help="Also write the full run as JSON")
    sc.add_argument("-q", "--quiet", action="store_true")
    sc.set_defaults(func=cmd_scan)

    ru = sub.add_parser("runs", help="List stored runs")
    ru.add_argument("--store", default="carousel.db")
    ru.add_argument("--radar", help="Only this radar_id")
    ru.set_defaults(func=cmd_runs)

    de = sub.add_parser("delta", help="What changed between a radar's latest run and the one before")
    de.add_argument("radar", metavar="RADAR.yml")
    de.add_argument("--store", default="carousel.db")
    de.set_defaults(func=cmd_delta)

    ex = sub.add_parser("export", help="Export a stored run as CSV or JSON")
    ex.add_argument("run_id", metavar="RUN_ID")
    ex.add_argument("--store", default="carousel.db")
    ex.add_argument("--csv")
    ex.add_argument("--json")
    ex.set_defaults(func=cmd_export)

    fe = sub.add_parser("feedback", help="Save, dismiss, or judge papers (edits the radar's feedback file)")
    fe.add_argument("radar", metavar="RADAR.yml")
    fe.add_argument("action", choices=("save", "dismiss", "relevant", "irrelevant"))
    fe.add_argument("refs", nargs="+", metavar="REF", help="paper_id (cp_...) or doi:/pmid:/pmcid:/openalex:/arxiv: ID")
    fe.add_argument("--undo", action="store_true", help="Remove the references instead")
    fe.add_argument("--file", help="Feedback file (default: RADAR.feedback.yml next to the radar)")
    fe.set_defaults(func=cmd_feedback)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
