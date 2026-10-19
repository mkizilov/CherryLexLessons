import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lesson_factory import core
from lesson_factory.dashboard_store import Store
from lesson_factory.dashboard_supervisor import supervise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Record a dashboard job completion receipt without releasing its coordinator slot")
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument("--job", required=True)
    result.add_argument("--attempt", required=True)
    action = result.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", choices=("complete", "blocked"))
    action.add_argument("--external-resume", action="store_true", help="Open the explicitly reserved same session in a tracked foreground terminal; never submit a prompt automatically")
    result.add_argument("--reason", help="Optional nonsecret single-line blocker explanation")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        store = Store(args.root)
        if args.external_resume:
            job = store.get_job(args.job)
            if job["attempt_id"] != args.attempt or job["launch_metadata"].get("mode") != "external":
                raise ValueError("Explicit external handoff reservation required")
            print("Recover only this assigned family in the same session. Preserve original paid budgets and genuine reviews. Close OpenCode and all reviewers, then use Verify and resolve in the dashboard. The queue slot remains held.", flush=True)
            return supervise(core.safe_path(store.root, f"work/dashboard/jobs/{job['id']}/attempts/{job['attempt_id']}/spec.json"), require_external=True)
        job = store.record_completion(args.job, args.attempt, args.status, args.reason)
    except (core.AuthoringError, OSError, ValueError, sqlite3.DatabaseError):
        print("Dashboard completion rejected; check job identity, attempt, and verified finalized family", file=sys.stderr)
        return 1
    print(json.dumps({"job": job["id"], "attempt": job["attempt_id"], "receipt_status": args.status, "state": job["state"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
