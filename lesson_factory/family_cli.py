import argparse
import json
import sys
from pathlib import Path

from . import core, family


DEFAULT_LOCK_WAIT = 300
TEMPORARY_BUSY_EXIT = 75


def lock_wait(value):
    seconds = int(value)
    if not 0 <= seconds <= 1800:
        raise argparse.ArgumentTypeError("lock wait must be from 0 to 1800 seconds")
    return seconds


def parser():
    result = argparse.ArgumentParser(prog="lesson_factory.family_cli")
    result.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("prepare", "plan"):
        command = commands.add_parser(name)
        command.add_argument("--language", required=True)
        command.add_argument("--checkpoint" if name == "prepare" else "--through", type=int, required=True)
        if name == "prepare":
            command.add_argument("--lock-wait", type=lock_wait, default=DEFAULT_LOCK_WAIT, help="Wait for the shared authoring lock (seconds, default 300); timeout exits 75 before mutation")
    for name in ("status", "check", "validate", "binding", "review", "finalize"):
        command = commands.add_parser(name)
        command.add_argument("--run", required=True)
        if name in ("check", "validate"):
            command.add_argument("--locale")
        if name in ("binding", "review"):
            command.add_argument("--role", required=True)
        if name == "review":
            command.add_argument("--file", required=True)
        if name in ("review", "finalize"):
            command.add_argument("--lock-wait", type=lock_wait, default=DEFAULT_LOCK_WAIT, help="Wait for the shared authoring lock (seconds, default 300); timeout exits 75 before mutation")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    root = Path(arguments.root).resolve()
    try:
        if arguments.command == "prepare":
            result = {"run": family.prepare_family(root, arguments.language, arguments.checkpoint, lock_timeout=arguments.lock_wait)}
        elif arguments.command == "plan":
            result = family.family_plan(root, arguments.language, arguments.through)
        elif arguments.command in ("check", "validate"):
            result = family.validate_family(root, arguments.run, arguments.locale)
        elif arguments.command == "binding":
            result = family.family_binding(root, arguments.run, arguments.role)
        elif arguments.command == "review":
            result = family.submit_family_review(root, arguments.run, arguments.role, core.read_json(core.safe_path(root, arguments.file)), lock_timeout=arguments.lock_wait)
        elif arguments.command == "finalize":
            result = {"path": family.finalize_family(root, arguments.run, lock_timeout=arguments.lock_wait)}
        else:
            context = family.load_family(root, arguments.run)
            reviews = {}
            for role in core.review_roles(context["study_language"]) + ["final"]:
                try:
                    family.require_family_review(root, arguments.run, role)
                    reviews[role] = "approved"
                except (core.AuthoringError, OSError) as exc:
                    reviews[role] = str(exc)
            result = {"run": arguments.run, "context": context, "reviews": reviews}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except core.MutationLockBusy as exc:
        print(json.dumps({"state": "waiting_for_lock", "retryable": True, "mutation_started": False, "wait_seconds": arguments.lock_wait, "message": str(exc), "next_action": "Retry the identical local command. Do not delete locks, kill the owner or restart provider requests."}), file=sys.stderr)
        return TEMPORARY_BUSY_EXIT
    except (core.AuthoringError, OSError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
