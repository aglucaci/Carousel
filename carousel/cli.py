"""
carousel command-line interface.

    carousel validate RADAR.yml [RADAR.yml ...]   # check configs; exit 1 if any are invalid
    carousel show RADAR.yml [--format json|yaml]  # print the resolved config (defaults filled in)

`build`, `delta`, and `export` arrive with the connector, snapshot, and export
work (CAROUSEL_PLAN.md, v0.1 tasks 4, 8, 11).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

import yaml

from carousel import __version__
from carousel.config import ConfigError, RadarConfig, load_radar


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
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
