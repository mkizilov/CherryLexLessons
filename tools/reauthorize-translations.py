import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lesson_factory.translation_recovery import authorize_translation_correction, authorize_translation_recovery, extract_retained_family, offline_recovery_status, plan_translation_recovery
from lesson_factory.slow_retry_recovery import authorize_slow_retries, plan_slow_retries


def parser():
    result = argparse.ArgumentParser(description="Inspect translation recovery evidence; operator authorizations never dispatch requests")
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument("--run", required=True)
    action = result.add_mutually_exclusive_group()
    action.add_argument("--authorize", action="store_true", help="Create one immutable four-attempt renewal grant for incomplete failed locales")
    action.add_argument("--correct-placeholder", nargs="+", metavar="LOCALE", help="Explicit operator correction of proven placeholder-only false positives; preserve the original grant and all evidence")
    action.add_argument("--extend-slow", nargs="+", metavar="LOCALE", help="Operator-only: extend exhausted renewal locales to eight total attempts with at least 30-minute waits")
    action.add_argument("--preview-slow", nargs="+", metavar="LOCALE", help="Read-only preview of an explicitly scoped slow-retry extension")
    action.add_argument("--extract-retained", metavar="LOCALE", help="Offline: write one retained complete provider family as an unapproved reconstruction draft (--output) with an exact-hash provenance manifest; provider evidence and locale trees stay unchanged")
    action.add_argument("--offline-status", action="store_true", help="Read-only: per offline_recovery locale reason, retained families, destination file provenance and next step")
    result.add_argument("--output", type=Path, help="New nonsymlink path under work/content-recovery for --extract-retained; no existing file is overwritten")
    result.add_argument("--item", help="candidate_sha256 prefix selecting one retained family when several exist")
    result.add_argument("--reason", help="Required operator authorization reason for changes or a slow-retry preview")
    result.add_argument("--timeout", type=float, default=120, help="Per-validation timeout in seconds")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.reason and not (args.authorize or args.correct_placeholder or args.extend_slow or args.preview_slow):
            raise ValueError("--reason requires an explicit operator authorization action or slow-retry preview")
        if (args.output or args.item) and not args.extract_retained or args.extract_retained and not args.output:
            raise ValueError("--extract-retained requires --output; --output/--item apply only to --extract-retained")
        if args.offline_status:
            report = offline_recovery_status(args.root, args.run, timeout=args.timeout)
        elif args.extract_retained:
            report = extract_retained_family(args.root, args.run, args.extract_retained, args.output, item=args.item, timeout=args.timeout)
        elif args.extend_slow:
            report = authorize_slow_retries(args.root, args.run, args.extend_slow, reason=args.reason, operator=True, timeout=args.timeout)
        elif args.preview_slow:
            report = plan_slow_retries(args.root, args.run, args.preview_slow, reason=args.reason, timeout=args.timeout)
        elif args.correct_placeholder:
            report = authorize_translation_correction(args.root, args.run, args.correct_placeholder, reason=args.reason, operator=True, timeout=args.timeout)
        elif args.authorize:
            report = authorize_translation_recovery(args.root, args.run, reason=args.reason, timeout=args.timeout)
        else:
            report = plan_translation_recovery(args.root, args.run, timeout=args.timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
