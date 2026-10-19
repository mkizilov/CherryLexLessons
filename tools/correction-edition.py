#!/usr/bin/env python3
"""Explicit local correction preview; never selects/updates a live consumer."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "build", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--spec", help="Exact root-relative lead-authored JSON spec")
    parser.add_argument("--spec-sha256", help="Exact raw spec SHA256 (never inferred)")
    parser.add_argument("--edition-id")
    parser.add_argument("--draft", action="store_true", help="Explicit unapproved preview; never production approval")
    args = parser.parse_args()
    if args.command == "verify":
        if not args.edition_id or args.spec or args.spec_sha256 or args.draft:
            parser.error("verify requires only --root and --edition-id")
    elif not args.spec or not args.spec_sha256 or args.edition_id:
        parser.error("preview/build require --spec and --spec-sha256; no implicit latest")
    sys.path.insert(0, str(args.root.resolve()))
    # Works both staged and after the lead explicitly adopts the two new files.
    module_spec = importlib.util.spec_from_file_location("lesson_factory.correction_edition", Path(__file__).resolve().parents[1] / "lesson_factory/correction_edition.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    try:
        result = module.verify(args.root, args.edition_id) if args.command == "verify" else getattr(module, args.command)(args.root, {"path": args.spec, "sha256": args.spec_sha256}, draft=args.draft)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
