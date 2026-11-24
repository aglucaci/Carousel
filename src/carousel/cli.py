"""
carousel command-line interface.

    carousel validate RADAR.yml [RADAR.yml ...]   # check configs; exit 1 if any are invalid
    carousel show RADAR.yml [--format json|yaml]  # print the resolved config (defaults filled in)

    carousel review list STORE.db [--status pending|merged|distinct|all] [--format text|json]
    carousel review merge STORE.db KEEP_ID OTHER_ID [--note TEXT]    # same work
    carousel review distinct STORE.db ID_A ID_B [--note TEXT]        # different works
    carousel review split STORE.db SOURCE SOURCE_ID [--note TEXT]    # undo a wrong merge
    carousel review link STORE.db PREPRINT_ID PUBLISHED_ID [--note TEXT]   # versions of one work, kept apart
    carousel review unlink STORE.db ID_A ID_B [--note TEXT]          # not versions of one work

    carousel scan RADAR.yml [--store carousel.db] [--as-of DATE] [--top N] [--mode M]
                  [--embedder auto|semantic|hashing|none] [--csv OUT.csv] [--json OUT.json]
    carousel runs [--store carousel.db] [--radar ID]                 # list stored runs
    carousel delta RADAR.yml [--store carousel.db]                   # latest run vs the one before
    carousel export RUN_ID (--csv OUT.csv | --json OUT.json) [--store carousel.db]
    carousel feedback RADAR.yml save|dismiss|relevant|irrelevant REF... [--undo]
    carousel site RUN.json... [--out docs]                           # static dashboard for GitHub Pages
    carousel db upgrade|current|downgrade [--url URL]                # Postgres migrations ([server] extra)
    carousel db import-store STORE.db | import-snapshots CHECKOUT    # copy Phase 1A data into Postgres
    carousel api serve [--host H --port P] | api openapi [--out F]   # REST API v1
    carousel worker [--burst]                                        # scan worker (RQ on Redis)
    carousel scheduler [--once] [--interval SECONDS]                 # enqueue daily/weekly radar scans
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
            elif args.action == "link":
                store.link_versions(args.preprint, args.published, note=args.note)
                print(f"linked preprint {args.preprint} to its publication {args.published} (both kept)")
            elif args.action == "unlink":
                n = store.unlink_versions(args.a, args.b, note=args.note)
                print(f"unlinked {args.a} and {args.b} ({n} link(s) removed); sources won't relink them")
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


def cmd_site(args: argparse.Namespace) -> int:
    from carousel import site

    try:
        written = site.build(args.runs, args.out)
    except (ValueError, OSError, KeyError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(written)} files to {args.out}")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    try:
        from carousel.database import importer, postgres
    except ImportError as exc:
        print(f'FAIL Postgres support needs the server extra: pip install "carousel-research[server]" ({exc})',
              file=sys.stderr)
        return 1
    try:
        if args.action == "upgrade":
            postgres.upgrade(args.url, args.revision)
            print(f"database at revision {postgres.current(args.url)}")
        elif args.action == "downgrade":
            postgres.downgrade(args.url, args.revision)
            print(f"database at revision {postgres.current(args.url) or 'base (empty)'}")
        elif args.action == "current":
            print(postgres.current(args.url) or "base (empty)")
        elif args.action == "import-store":
            counts = importer.import_store(args.store, postgres.engine(args.url))
            for table, n in counts.items():
                print(f"  {table:<22} {n}")
            print(f"imported {args.store}")
        elif args.action == "import-snapshots":
            results = importer.import_snapshots(args.root, postgres.engine(args.url), args.radar or None)
            for r in results:
                print(f"  {r.radar_id}: {r.snapshots}/{r.expected_snapshots} snapshots, {r.items}/{r.expected_items} items "
                      f"(+{r.inserted_snapshots} new, {r.linked_items} items linked to papers)")
            print("counts match")
    except (ValueError, importer.ImportCountMismatch) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    try:
        from carousel.api.app import openapi_document
    except ImportError as exc:
        print(f'FAIL the API needs the server extra: pip install "carousel-research[server]" ({exc})', file=sys.stderr)
        return 1
    if args.action == "openapi":
        text = json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"wrote {args.out}")
        else:
            print(text, end="")
        return 0
    import uvicorn

    from carousel.api.app import create_app

    try:
        app = create_app()
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if not app.state.settings.api_token and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: CAROUSEL_API_TOKEN is not set, so anyone who can reach this server can write to it",
              file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    try:
        from redis import Redis
        from rq import Queue, SimpleWorker, Worker

        from carousel.workers.scan import QUEUE
    except ImportError as exc:
        print(f'FAIL the worker needs the server extra: pip install "carousel-research[server]" ({exc})', file=sys.stderr)
        return 1
    import os

    conn = Redis.from_url(os.environ.get("CAROUSEL_REDIS_URL", "redis://localhost:6379/0"))
    # Forking after PyTorch is loaded is unreliable on macOS; run jobs in-process there.
    cls = SimpleWorker if (args.simple or sys.platform == "darwin") else Worker
    worker = cls([Queue(QUEUE, connection=conn)], connection=conn)
    worker.work(burst=args.burst)
    return 0


def cmd_scheduler(args: argparse.Namespace) -> int:
    try:
        from redis import Redis
        from rq import Queue
        from sqlalchemy.orm import sessionmaker

        from carousel.database import postgres
        from carousel.workers import schedule
        from carousel.workers.scan import QUEUE
    except ImportError as exc:
        print(f'FAIL the scheduler needs the server extra: pip install "carousel-research[server]" ({exc})', file=sys.stderr)
        return 1
    import logging
    import os

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        sessions = sessionmaker(postgres.engine())
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    queue = Queue(QUEUE, connection=Redis.from_url(os.environ.get("CAROUSEL_REDIS_URL", "redis://localhost:6379/0")))
    if args.once:
        jobs = schedule.tick(sessions, queue)
        print(f"enqueued {len(jobs)} scheduled scan(s)" + "".join(f"\n  {j.meta.get('radar_id')}  {j.id}" for j in jobs))
        return 0
    schedule.run_forever(sessions, queue, interval=args.interval)
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
    rl.add_argument("--status", choices=("pending", "merged", "distinct", "linked", "superseded", "all"), default="pending")
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
    rk = rs.add_parser("link", help="A preprint and its publication: link them, keeping both papers")
    rk.add_argument("store", metavar="STORE.db")
    rk.add_argument("preprint", metavar="PREPRINT_ID")
    rk.add_argument("published", metavar="PUBLISHED_ID")
    ru = rs.add_parser("unlink", help="Not versions of one work: remove the link; sources won't relink the pair")
    ru.add_argument("store", metavar="STORE.db")
    ru.add_argument("a", metavar="ID_A")
    ru.add_argument("b", metavar="ID_B")
    for x in (rm, rd, rp, rk, ru):
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

    si = sub.add_parser("site", help="Build the static dashboard (GitHub Pages) from run exports")
    si.add_argument("runs", nargs="+", metavar="RUN.json", help="Exports from `carousel scan --json`")
    si.add_argument("--out", default="docs", help="Output directory, replaced entirely (default: docs)")
    si.set_defaults(func=cmd_site)

    db = sub.add_parser("db", help="Postgres (Phase 1B): migrations and imports; needs the [server] extra")
    dbs = db.add_subparsers(dest="action", required=True, metavar="ACTION")
    for name, help_ in (("upgrade", "Apply migrations (default: to head)"), ("downgrade", "Revert migrations (default: to base)"),
                        ("current", "Show the current migration revision")):
        x = dbs.add_parser(name, help=help_)
        x.add_argument("--url", help="Database URL (default: $CAROUSEL_DATABASE_URL)")
        if name != "current":
            x.add_argument("--revision", default="head" if name == "upgrade" else "base")
    x = dbs.add_parser("import-store", help="Copy a Phase 1A SQLite store into Postgres (idempotent)")
    x.add_argument("store", metavar="STORE.db")
    x.add_argument("--url")
    x = dbs.add_parser("import-snapshots", help="Import run snapshots from a checkout of the snapshots branch; verifies counts")
    x.add_argument("root", metavar="SNAPSHOTS_CHECKOUT")
    x.add_argument("--radar", action="append", help="Only this radar_id (repeatable)")
    x.add_argument("--url")
    db.set_defaults(func=cmd_db)

    api = sub.add_parser("api", help="REST API v1 (Phase 1B); needs the [server] extra")
    apis = api.add_subparsers(dest="action", required=True, metavar="ACTION")
    sv = apis.add_parser("serve", help="Run the API (uvicorn); configure with CAROUSEL_* environment variables")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    op = apis.add_parser("openapi", help="Print or write the OpenAPI schema")
    op.add_argument("--out", help="Write to this file instead of stdout")
    api.set_defaults(func=cmd_api)

    wk = sub.add_parser("worker", help="Run the scan worker (RQ); run exactly one")
    wk.add_argument("--burst", action="store_true", help="Exit when the queue is empty")
    wk.add_argument("--simple", action="store_true", help="Run jobs in-process (no fork); default on macOS")
    wk.set_defaults(func=cmd_worker)

    sc = sub.add_parser("scheduler", help="Enqueue scheduled (daily/weekly) radar scans; reads CAROUSEL_DATABASE_URL and CAROUSEL_REDIS_URL")
    sc.add_argument("--once", action="store_true", help="Check once, enqueue what is due, and exit (for cron)")
    sc.add_argument("--interval", type=float, default=60.0, help="Seconds between checks (default 60)")
    sc.set_defaults(func=cmd_scheduler)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
