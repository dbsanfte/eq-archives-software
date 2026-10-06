#!/usr/bin/env python3
"""Bounded discovery, Wayback staging, paid Luna grading and explicit review."""

import argparse
import math
from pathlib import Path
import sys

from acquisition import sample
from common import CrawlError, Store
from discovery import discover
from grading import grade
from review import batch, decisions, review


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return value


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--work-dir", required=True, help="Private durable directory outside both repositories")
    commands = cli.add_subparsers(dest="command", required=True)
    command = commands.add_parser("discover", help="Read selected local Git objects; cache evidence and coverage")
    command.add_argument("--archive-repo", required=True)
    command.add_argument("--seeds", default=str(Path(__file__).with_name("seeds.json")))
    command.add_argument("--max-candidates", type=positive, default=50)
    command.add_argument("--max-tree-entries", type=positive, default=15000)
    command.add_argument("--max-inventory-entries", type=positive, default=200000)
    command.add_argument("--max-seed-captures", type=positive, default=66)
    command.add_argument("--max-seed-probes", type=positive, default=660)
    command.add_argument("--max-per-seed", type=positive, default=6)
    command.add_argument("--max-seed-bytes", type=positive, default=16777216)
    command.add_argument("--max-page-bytes", type=positive, default=524288)
    command.set_defaults(handler=discover)
    command = commands.add_parser("sample", help="Use serial persistent Wayback Machine Downloader to stage exact samples")
    command.add_argument("--max-candidates", type=positive, default=50)
    command.add_argument("--samples-per-candidate", type=int, choices=[1, 2], default=2)
    command.add_argument("--delay", type=float, default=3)
    command.add_argument("--bytes-per-second", type=positive, default=131072)
    command.add_argument("--max-requests", type=positive, default=240)
    command.add_argument("--max-page-bytes", type=positive, default=1048576)
    command.add_argument("--max-bytes", type=positive, default=25165824)
    command.add_argument("--max-seconds", type=positive, default=900)
    command.add_argument("--retry-unresolved", action="store_true", help="Retry previously unavailable/failed candidates within the same cumulative budgets")
    command.set_defaults(handler=sample)
    command = commands.add_parser("grade", help="Explicit paid source-bound Luna judgments")
    command.add_argument("--api-key-file", required=True)
    command.add_argument("--max-candidates", type=positive, default=50)
    command.add_argument("--max-usd", type=float, required=True)
    command.add_argument("--max-source-characters", type=positive, default=120000)
    command.set_defaults(handler=grade)
    command = commands.add_parser("review", help="Export an offline approval queue")
    command.set_defaults(handler=review)
    command = commands.add_parser("decide", help="Import explicit decisions bound to the staged manifests")
    command.add_argument("--file", required=True)
    command.set_defaults(handler=decisions)
    command = commands.add_parser("batch", help="Export approved captures for a later single publication batch")
    command.set_defaults(handler=batch)
    return cli


def main():
    args = parser().parse_args()
    if (not math.isfinite(getattr(args, "max_usd", 1)) or not math.isfinite(getattr(args, "delay", 0))
            or getattr(args, "max_usd", 1) <= 0 or getattr(args, "delay", 0) < 0):
        raise SystemExit("Budget must be positive and delay nonnegative")
    store = None
    try:
        store = Store(args.work_dir)
        args.handler(args, store)
    except CrawlError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
