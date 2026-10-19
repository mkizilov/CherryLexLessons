import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lesson_factory import core
from lesson_factory.family_translation import run
from lesson_factory.translation_scheduler import positive_workers
from lesson_factory.translation import MAX_INPUT_BYTES, TranslationError


def parser():
    result = argparse.ArgumentParser(description="One complete five-stage family request per locale; dry-run by default")
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument("--run", required=True)
    locales = result.add_mutually_exclusive_group(required=True)
    locales.add_argument("--all", action="store_true")
    locales.add_argument("--lang", choices=core.LOCALES)
    result.add_argument("--execute", action="store_true", help="Execute within the original four-attempt limit or a separately validated operator renewal/extension")
    result.add_argument("--resume", action="store_true")
    result.add_argument("--workers", type=positive_workers, default=10)
    result.add_argument("--timeout", type=float, default=180, help="Connection/socket idle timeout and separate validator timeout; not a total generation limit")
    result.add_argument("--deadline", type=float, default=3600, help="Total seconds per worker including generation, validation and retry waits; validated slow-retry extensions use at least 21600 seconds")
    result.add_argument("--max-input-bytes", type=int, default=MAX_INPUT_BYTES)
    result.add_argument("--repair-candidate", type=Path)
    return result


def main():
    try:
        run(parser().parse_args())
    except (core.AuthoringError, TranslationError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
