import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lesson_factory import core
from lesson_factory.history_catalog import DEFAULT_LIMIT, history_catalog, history_entry
from lesson_factory.history_index import horizon_report, snapshot_index


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only lesson catalogue and pinned history metadata; verify teaching claims in relevant original content.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", help="family_runs/<study>/<six-digit-base>")
    mode.add_argument("--language", help="Study language code for the finite vocabulary ceiling report")
    parser.add_argument("--stage", type=int, choices=range(5))
    parser.add_argument("--locale", help="Learner locale; defaults to each historical record's pinned source")
    parser.add_argument("--horizon", action="store_true", help="Report strict-prefix vocabulary ceiling independently of any run")
    parser.add_argument("--detail", action="store_true", help="Include additional verbatim planning/audit claims; no full content")
    parser.add_argument("--catalog", action="store_true", help="Paginated catalogue of every external history record plus compact family metadata; --stage only labels eligibility")
    parser.add_argument("--key", help="One external history key from the catalogue: full brief metadata, file paths/hashes, no bodies")
    parser.add_argument("--offset", type=int, help="Catalogue start row (default 0)")
    parser.add_argument("--limit", type=int, help=f"Catalogue rows per page (default {DEFAULT_LIMIT}, max 100)")
    parser.add_argument("--context-hash", help="Require this pinned family context hash")
    args = parser.parse_args(argv)
    catalog = args.catalog or args.key is not None
    if (args.offset is not None or args.limit is not None) and (not args.catalog or args.key is not None):
        parser.error("--offset/--limit require --catalog without --key")
    if args.context_hash and not catalog:
        parser.error("--context-hash requires --catalog or --key")
    if catalog and (not args.run or args.horizon or args.detail):
        parser.error("--catalog/--key require --run and exclude --horizon and --detail")
    if args.run and not catalog and (args.stage is None or args.horizon):
        parser.error("--run requires --stage 0..4 and excludes --horizon")
    if args.language and (not args.horizon or args.stage is not None or args.locale or args.detail):
        parser.error("--language requires --horizon and excludes --stage, --locale and --detail")
    try:
        if args.key is not None:
            result = history_entry(ROOT, args.run, args.key, stage=args.stage, locale=args.locale, context_hash=args.context_hash)
        elif args.catalog:
            result = history_catalog(ROOT, args.run, stage=args.stage, offset=args.offset or 0, limit=DEFAULT_LIMIT if args.limit is None else args.limit, locale=args.locale, context_hash=args.context_hash)
        else:
            result = horizon_report(ROOT, args.language) if args.horizon else snapshot_index(ROOT, args.run, args.stage, args.locale, detail=args.detail)
    except (core.AuthoringError, OSError) as exc:
        parser.exit(1, f"History report failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
