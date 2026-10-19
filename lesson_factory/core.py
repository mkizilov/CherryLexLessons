import csv
import fcntl
import hashlib
import json
import math
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path

LOCALES = ("en", "ar", "de", "es", "fr", "hi", "it", "ja", "ko", "pt", "ru", "zh")
LANGUAGES = dict(zip(("ar", "en", "fr", "de", "hi", "it", "ja", "ko", "zh", "pt", "ru", "es"), ("Arabic", "English", "French", "German", "Hindi", "Italian", "Japanese", "Korean", "Mandarin", "Portuguese", "Russian", "Spanish")))
RETURN_OFFSETS = (25, 75, 125, 225)


class AuthoringError(ValueError):
    pass


class MutationLockBusy(AuthoringError):
    """Temporary shared-writer contention; the requested mutation has not begun."""


def language_code(language):
    aliases = {name.casefold(): code for code, name in LANGUAGES.items()}
    aliases.update({code: code for code in LANGUAGES})
    aliases.update({"chinese": "zh", "mandarin chinese": "zh"})
    try:
        return aliases[language.casefold()]
    except (KeyError, AttributeError) as exc:
        raise AuthoringError("Unknown study language") from exc


def source_locale(study):
    return "es" if language_code(study) == "en" else "en"


def learner_locales(study):
    code = language_code(study)
    return tuple(locale for locale in LOCALES if locale != code)


def translation_locales(study):
    source = source_locale(study)
    return tuple(locale for locale in learner_locales(study) if locale != source)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical(value) + b"\n")
    os.replace(temporary, path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthoringError(f"Cannot read JSON {path}: {exc}") from exc


def safe_path(root, relative):
    root = Path(root).resolve()
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise AuthoringError("Paths must be root-relative without traversal")
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise AuthoringError("Symlink paths are not allowed in authoring state")
    result = (root / raw).resolve()
    if not result.is_relative_to(root):
        raise AuthoringError("Path escapes project root")
    return result


def stage_identity(checkpoint, stage=0):
    if isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 10 or checkpoint % 10 or checkpoint > 999999:
        raise AuthoringError("Base checkpoint must be a positive multiple of 10 below 1000000")
    if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= len(RETURN_OFFSETS):
        raise AuthoringError("Stage must be an integer from 0 to 4")
    unlock = checkpoint + (RETURN_OFFSETS[stage - 1] if stage else 0)
    return {"checkpoint": unlock, "base_checkpoint": checkpoint, "stage": stage}


def event_key(event):
    return event["checkpoint"], event["base_checkpoint"], event["stage"]


@contextmanager
def mutation_lock(root, *, timeout=0, stop_event=None):
    """Wait only for acquisition, never rerun a mutation or remove a live lock.

    Direct callers remain nonblocking by default. Family CLI commands opt into
    bounded waiting; publication/authorization callers retain their own policy.
    The PID text is diagnostic only: the kernel flock owns synchronization.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Mutation lock timeout must be finite and nonnegative")
    expires = time.monotonic() + timeout
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = safe_path(root, ".authoring.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise AuthoringError("Authoring lock must be a regular file")
        contended = False
        while True:
            if stop_event is not None and stop_event.is_set():
                raise AuthoringError("Authoring lock wait cancelled; mutation not started")
            if contended and time.monotonic() >= expires:
                raise MutationLockBusy("Authoring mutation is locked by another process; temporary contention, mutation not started")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                contended = True
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise MutationLockBusy("Authoring mutation is locked by another process; temporary contention, mutation not started") from exc
                wait = min(0.1, remaining)
                if stop_event is None:
                    time.sleep(wait)
                else:
                    stop_event.wait(wait)
        try:
            if stop_event is not None and stop_event.is_set():
                raise AuthoringError("Authoring lock wait cancelled; mutation not started")
            if contended and time.monotonic() >= expires:
                raise MutationLockBusy("Authoring mutation lock wait expired before admission; mutation not started")
            current, opened = path.stat(), os.fstat(handle.fileno())
            if path.is_symlink() or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise AuthoringError("Authoring lock was replaced while waiting")
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode())
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def vocabulary(root, code):
    wordlist = safe_path(root, f"reference/vocabulary/{LANGUAGES[code]}/{LANGUAGES[code].lower()}_vocabulary.csv")
    with wordlist.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        words = list(reader)
    rank_key = next((key for key in (reader.fieldnames or []) if key.casefold() == "rank"), None)
    if rank_key is None:
        raise AuthoringError("Vocabulary CSV requires rank column")
    try:
        ranks = [int(word[rank_key]) for word in words]
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthoringError("Invalid vocabulary ranks") from exc
    if ranks != list(range(1, len(words) + 1)):
        raise AuthoringError("Canonical vocabulary must have contiguous ordered ranks")
    return words


def word_range(event):
    base, stage = event["base_checkpoint"], event["stage"]
    previous = base + RETURN_OFFSETS[stage - 2] if stage > 1 else base
    return [previous + 1, event["checkpoint"]] if stage else [base - (19 if base % 20 == 0 else 9), base]


def review_roles(study):
    return ["pedagogical", "assessment", "format"] + [f"language:{locale}" for locale in translation_locales(study)]
