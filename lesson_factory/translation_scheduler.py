import argparse
import contextlib
import fcntl
import json
import math
import subprocess
import threading
import time
from pathlib import Path

from . import core
from .translation import TranslationError, retry_status


STDOUT_LOCK = threading.Lock()
PUBLICATION_LOCK = threading.Lock()


def positive_workers(value):
    count = int(value)
    if not 1 <= count <= 10:
        raise argparse.ArgumentTypeError("workers must be between 1 and 10")
    return count


def emit(record):
    with STDOUT_LOCK:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


@contextlib.contextmanager
def worker_lock(log_dir):
    log_dir = Path(log_dir)
    core.safe_path(log_dir.parent, log_dir.name)
    log_dir.mkdir(parents=True, exist_ok=True)
    lock_path = core.safe_path(log_dir, "worker.lock")
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TranslationError("Locale worker already running; refusing duplicate dispatch") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def retry_worker(attempt, journal, *, resume=False, stop_event=None, clock=time.time, sleep=None, deadline=3600, retry_policy=None):
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0:
        raise TranslationError("Locale worker deadline must be finite and positive")
    stop_event = stop_event or threading.Event()
    expires = clock() + deadline
    retry = False
    while True:
        if stop_event.is_set():
            raise TranslationError("Localization cancelled; evidence retained")
        remaining = expires - clock()
        if remaining <= 0:
            raise TranslationError("Locale worker deadline exceeded; evidence retained")
        previous = core.read_json(journal) if journal.exists() else {}
        policy = retry_status(journal, retry_policy)
        remaining = expires - clock()
        if stop_event.is_set():
            raise TranslationError("Localization cancelled; evidence retained")
        if remaining <= 0:
            raise TranslationError("Locale worker deadline exceeded; evidence retained")
        if policy["retry_eligible"] and (resume or retry):
            if previous["attempt"] >= policy["max_attempts"]:
                raise TranslationError("Locale retry budget exhausted")
            due = policy["retry_at"]
            if type(due) not in (int, float) or not math.isfinite(due):
                raise TranslationError("Transient retry missing persistent due time")
            delay = max(0, due - clock())
            if delay:
                emit({"target": previous.get("target"), "state": "retry_wait", "attempt": previous["attempt"], "retry_at": due})
                wait = min(delay, remaining)
                if sleep is None:
                    stop_event.wait(wait)
                else:
                    sleep(wait)
                continue
            retry = True
        try:
            return attempt(resume=resume or retry, retry=retry, remaining=remaining, stop_event=stop_event)
        except Exception:
            failed = core.read_json(journal) if journal.exists() else {}
            if failed == previous:
                raise
            if stop_event.is_set():
                raise
            policy = retry_status(journal, retry_policy)
            if not policy["retry_eligible"] or failed["attempt"] >= policy["max_attempts"]:
                raise
            retry = True


@contextlib.contextmanager
def publication_lock(root, gate, stop_event, deadline):
    expires = time.monotonic() + min(30, deadline)
    while not PUBLICATION_LOCK.acquire(timeout=min(0.1, max(0, expires - time.monotonic()))):
        if stop_event.is_set() or time.monotonic() >= expires:
            raise TranslationError("Publication lock wait cancelled or deadline exceeded; evidence retained")
    try:
        while True:
            gate()
            if stop_event.is_set():
                raise TranslationError("Localization cancelled before publication; evidence retained")
            manager = core.mutation_lock(root)
            try:
                manager.__enter__()
            except core.AuthoringError as error:
                if "locked by another process" not in str(error) or time.monotonic() >= expires:
                    raise
                stop_event.wait(min(0.1, max(0, expires - time.monotonic())))
                continue
            break
        try:
            yield
        finally:
            manager.__exit__(None, None, None)
    finally:
        PUBLICATION_LOCK.release()


def validate_process(command, timeout, stop_event=None):
    expires = time.monotonic() + timeout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise TranslationError("Localization cancelled during validation; evidence retained")
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TranslationError("Localization validation deadline exceeded; evidence retained")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
