import contextlib
import fcntl
import json
import os
import signal
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import core, family, translation_scheduler as scheduler
from .translation import TranslationError, atomic_write, digest, encode, execute, prepare, validate_candidate
from .translation_payload import build_payload, build_prompt
from .translation_recovery import recovery_for_locale, validate_preserved_locale
from .slow_retry_recovery import load_slow_retry_policy
from .source_validation import SourceValidation


def inventory(folder):
    folder = Path(folder)
    if folder.is_symlink():
        raise TranslationError("Symlink locale generation refused")
    if not folder.exists():
        return None
    result = {}
    for path in sorted(folder.rglob("*")):
        mode = path.lstat().st_mode
        name = path.relative_to(folder).as_posix()
        if stat.S_ISDIR(mode):
            result[name] = None
        elif stat.S_ISREG(mode):
            result[name] = digest(path.read_bytes())
        else:
            raise TranslationError("Symlink or special locale file refused")
    return result


def sync(folder):
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(destination, log_dir, candidate, record, initial, gate, *, scaffold=None):
    destination, log_dir = Path(destination), Path(log_dir)
    generation = log_dir / "publication-generation"
    backup = log_dir / "publication-scaffold"
    outputs = {}
    for artifact in candidate["stages"]:
        prefix = f"stage-{artifact['stage']}/"
        outputs.update({prefix + "lesson.md": artifact["lesson_md"].encode("utf-8"), prefix + "quiz.md": artifact["quiz_md"].encode("utf-8"), prefix + "adaptation-notes.json": encode(artifact["adaptation_notes"])})
    marker = {"candidate_hash": digest(encode(candidate)), "input_hash": record["input_hash"], "files": {name: digest(data) for name, data in outputs.items()}}
    planned = {f"stage-{stage}": None for stage in range(5)} | {name: digest(data) for name, data in outputs.items()}
    journal = log_dir / "journal.json"
    pending = record.get("family_publication_pending") is True
    current = inventory(destination)
    if pending:
        if record.get("family_publication_hashes") != planned:
            raise TranslationError("Interrupted family publication candidate changed")
        old = record.get("family_publication_previous")
        if current not in (old, None, planned) or inventory(backup) not in (None, old):
            raise TranslationError("Interrupted family publication edited; refusing overwrite")
        if current is None and old is not None and inventory(backup) != old:
            raise TranslationError("Interrupted family scaffold backup missing")
    else:
        if current != initial:
            raise TranslationError("Family destination drift; refusing overwrite")
        if record.get("family_publication_hashes") == current and current == planned:
            gate()
            if core.read_json(log_dir / "generation.json") != marker:
                raise TranslationError("Family generation receipt changed")
            return record
        if current is not None:
            expected = {f"stage-{stage}": None for stage in range(5)} | (scaffold or {})
            if scaffold is None or current != expected:
                raise TranslationError("Existing family destination is not the authoritative empty scaffold")
        if generation.exists() or backup.exists():
            raise TranslationError("Untracked family publication staging exists")
        old = initial
    gate()
    if current != planned:
        if not pending:
            record.update(family_publication_pending=True, family_publication_hashes=planned, family_publication_previous=old)
            atomic_write(journal, encode(record))
            sync(log_dir)
        staged = inventory(generation)
        if staged is not None and any(name not in planned or planned[name] != value for name, value in staged.items()):
            raise TranslationError("Family staged generation edited; refusing overwrite")
        if staged != planned:
            for name, data in outputs.items():
                if not (generation / name).exists():
                    atomic_write(generation / name, data)
            for stage in range(5):
                sync(generation / f"stage-{stage}")
            sync(generation)
        if inventory(generation) != planned:
            raise TranslationError("Family staging bytes differ")
        record.update(family_publication_pending=True, family_publication_hashes=planned, family_publication_previous=old)
        atomic_write(journal, encode(record))
        atomic_write(log_dir / "generation.json", encode(marker))
        sync(log_dir)
        gate()
        if inventory(destination) != current or inventory(generation) != planned:
            raise TranslationError("Family destination/staging changed before rename")
        if current is not None:
            os.rename(destination, backup)
            sync(destination.parent)
            sync(log_dir)
        os.rename(generation, destination)
        sync(destination.parent)
        sync(log_dir)
    if inventory(destination) != planned or inventory(generation) is not None:
        raise TranslationError("Published family generation differs from receipt")
    if core.read_json(log_dir / "generation.json") != marker:
        raise TranslationError("Family generation receipt changed")
    record["family_publication_pending"] = False
    atomic_write(journal, encode(record))
    sync(log_dir)
    return record


def localized_history(root, context, locale):
    result = []
    for record in context["external_history"]:
        path = Path(record["path"]) / locale / f"stage-{record['stage']}"
        folder = core.safe_path(root, str(path))
        result.append({"key": record["key"], "lesson_md": (folder / "lesson.md").read_bytes().decode("utf-8"), "quiz_md": (folder / "quiz.md").read_bytes().decode("utf-8")})
    return result


def context_for_locale(root, run, context, locale, evidence):
    return build_payload(root, run, context, locale, evidence)


def run(args):
    root = args.root.resolve()
    repair = args.repair_candidate
    if repair and (args.all or args.execute or args.resume):
        raise TranslationError("Offline repair requires one --lang without --execute/--resume/--all")
    if not 1 <= args.workers <= 10 or not 0 < args.timeout < float("inf") or not 0 < args.deadline < float("inf"):
        raise TranslationError("Workers must be 1..10; timeout/deadline finite and positive")
    context = family.load_family(root, args.run)
    study, base = family.family_identity(args.run)
    locales = core.translation_locales(study) if args.all else (args.lang,)
    if any(locale not in core.translation_locales(study) for locale in locales):
        raise TranslationError("Destination must exclude study and source locales")
    scope = f"families/{study}/{base:06d}"
    stop = threading.Event()
    proof = SourceValidation(root, args.run, context, stop)
    proof.establish(args.timeout)
    baseline = proof.contract
    original = proof.original
    prompt = build_prompt(root)

    def source_gate(timeout=None):
        proof.check(args.timeout if timeout is None else timeout)

    practice = tuple(stage["stage"] for stage in context["stages"] if stage["stage"] and core.read_json(family.family_path(root, args.run) / f"stage-{stage['stage']}/brief.json")["extension"]["mode"] == "practice")
    jobs, errors = [], []
    for locale in locales:
        try:
            source = context_for_locale(root, args.run, context, locale, original)
            body, summary = prepare(source, locale, prompt, args.max_input_bytes)
            scheduler.emit(summary)
            jobs.append((locale, body, summary))
        except Exception as error:
            errors.append(error)
            scheduler.emit({"target": locale, "state": "blocked_capacity" if "above" in str(error) else "worker_failed", "error": str(error)})
    if not args.execute and not repair:
        if errors:
            raise errors[0]
        return

    def configure(job, selected_scope, retry_policy=None):
        locale, body, summary = job
        destination = family.family_locale_path(root, args.run, locale)
        log_dir = core.safe_path(root, f"work/translations/{selected_scope}/{locale}")
        journal = log_dir / "journal.json"
        previous = core.read_json(journal) if journal.exists() else {}
        initial = inventory(destination)
        recovery = previous.get("family_publication_pending") is True and args.resume
        scaffold = None
        if not recovery and previous.get("family_publication_hashes") != initial:
            scaffold = family.family_locale_scaffold(root, args.run, locale)
        if previous and not repair and not args.resume:
            raise TranslationError("Existing family attempt requires --resume")
        if previous and previous.get("input_hash") != summary["hash"]:
            raise TranslationError("Family inputs drifted from retained request")
        localized_baseline = digest(localized_history(root, context, locale))
        expires = None

        def gate():
            if time.monotonic() >= expires or stop.is_set():
                raise TranslationError("Family worker cancelled/deadline exceeded")
            source_gate(min(args.timeout, expires - time.monotonic()))
            if time.monotonic() >= expires or stop.is_set():
                raise TranslationError("Family worker cancelled/deadline exceeded after source gate")
            if digest(localized_history(root, context, locale)) != localized_baseline:
                raise TranslationError("Localized external history drift")
            if inventory(destination) != initial:
                raise TranslationError("Family destination drift; expert edits preserved")

        def validate(candidate):
            validate_candidate(candidate, reviewed_practice_stages=practice)
            path = log_dir / "validation/candidate.json"
            atomic_write(path, encode(candidate))
            remaining = min(args.timeout, expires - time.monotonic())
            if remaining <= 0:
                raise TranslationError("Family validation deadline exceeded")
            result = scheduler.validate_process([os.environ.get("NODE_BINARY", "node"), str(root / "tools/validate-family.cjs"), "--root", str(root), "--run", args.run, "--locale", locale, "--candidate", str(path)], remaining, stop)
            report = json.loads(result.stdout)
            atomic_write(path.parent / "report.json", encode(report))
            if result.returncode or not report.get("ok") or report.get("family_contract") != baseline:
                raise TranslationError("Family candidate rejected; all five source contracts must match")
            return True

        def attempt(*, resume, retry, remaining, stop_event):
            if selected_scope != scope:
                authorization = recovery_for_locale(root, args.run, locale, timeout=min(args.timeout, remaining))
                if not authorization or authorization["scope"] != selected_scope or authorization.get("request_summary") != summary:
                    raise TranslationError("Renewal authorization changed before attempt")
                remaining = min(remaining, expires - time.monotonic())
                if remaining <= 0 or stop_event.is_set():
                    raise TranslationError("Renewal worker cancelled/deadline exceeded before dispatch")
            return execute(root, body, summary, validate, resume=resume, retry=retry, repair_candidate=repair, timeout=min(args.timeout, remaining), wall_timeout=remaining, scope=selected_scope, gate=gate, stop_event=stop_event, reviewed_practice_stages=practice, **({"retry_policy": retry_policy} if retry_policy is not None else {}))

        def publication_gate():
            if time.monotonic() >= expires or stop.is_set():
                raise TranslationError("Family publication cancelled/deadline exceeded")
            source_gate(min(args.timeout, expires - time.monotonic()))
            if time.monotonic() >= expires or stop.is_set():
                raise TranslationError("Family worker cancelled/deadline exceeded after source gate")
            if digest(localized_history(root, context, locale)) != localized_baseline:
                raise TranslationError("Localized external history drift")

        def worker():
            nonlocal expires
            deadline = max(args.deadline, 21600) if retry_policy is not None else args.deadline
            expires = time.monotonic() + deadline
            record = scheduler.retry_worker(attempt, journal, resume=args.resume, stop_event=stop, deadline=deadline, **({"retry_policy": retry_policy} if retry_policy is not None else {}))
            candidate = core.read_json(log_dir / "candidate.json")
            validate(candidate)
            with scheduler.publication_lock(root, gate, stop, max(0.001, expires - time.monotonic())):
                gate()
                publish(destination, log_dir, candidate, record, initial, publication_gate, scaffold=scaffold)
            scheduler.emit({"target": locale, **{key: record.get(key) for key in ("state", "attempt", "request_id", "estimated_cost_usd", "cost_basis")}})
            return record

        return log_dir, worker

    handlers = {}
    with contextlib.ExitStack() as stack:
        ready = []
        source_gate()
        for job in jobs:
            try:
                original_log_dir = core.safe_path(root, f"work/translations/{scope}/{job[0]}")
                original_locked = core.safe_path(root, f"work/translations/{scope}/{job[0]}/worker.lock").exists()
                if original_locked:
                    stack.enter_context(scheduler.worker_lock(original_log_dir))
                authorization = recovery_for_locale(root, args.run, job[0], timeout=args.timeout)
                if authorization and authorization["disposition"] == "preserve":
                    if repair:
                        raise TranslationError("Published locale is preserved; do not replace it through repair")
                    validate_preserved_locale(root, args.run, job[0], timeout=args.timeout)
                    scheduler.emit({"target": job[0], "state": "preserved", "grant_id": authorization["grant_id"]})
                    continue
                selected_scope = authorization["scope"] if authorization else scope
                if authorization and authorization.get("request_summary") != job[2]:
                    raise TranslationError("Focused request differs from the authorized recovery grant")
                if not original_locked:
                    stack.enter_context(scheduler.worker_lock(original_log_dir))
                log_dir = core.safe_path(root, f"work/translations/{selected_scope}/{job[0]}")
                if log_dir != original_log_dir:
                    attempt_lock = core.safe_path(root, f"work/translations/{scope}/{job[0]}/attempt.lock")
                    if not attempt_lock.exists():
                        raise TranslationError("Original attempt lock missing; refusing renewal dispatch")
                    handle = stack.enter_context(attempt_lock.open("rb"))
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise TranslationError("Original translation attempt is active; refusing renewal dispatch") from None
                    stack.enter_context(scheduler.worker_lock(log_dir))
                retry_policy = load_slow_retry_policy(root, args.run, job[0], timeout=args.timeout) if authorization else None
                log_dir, worker = configure(job, selected_scope, retry_policy)
                ready.append((job[0], worker))
            except Exception as error:
                errors.append(error)
                scheduler.emit({"target": job[0], "state": "worker_failed", "error": str(error)})
        source_gate()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        try:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                pending = {pool.submit(worker): locale for locale, worker in ready}
                for future in as_completed(pending):
                    try:
                        future.result()
                    except Exception as error:
                        errors.append(error)
                        scheduler.emit({"target": pending[future], "state": "worker_failed", "error": str(error)})
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
    scheduler.emit({"state": "batch_summary", "selected": len(locales), "completed": len(locales) - len(errors), "failed": len(errors), "cancelled": stop.is_set()})
    if errors:
        raise TranslationError(f"{len(errors)} family locale workers failed; evidence retained")
