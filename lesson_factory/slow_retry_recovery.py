import contextlib
import fcntl
import math
import os
import stat
import time
from pathlib import Path

from . import core, family, translation_recovery as recovery
from .translation import RetryPolicy, TranslationError, digest, encode, retained_retry_eligible, retry_after_seconds, retry_outcome_fingerprint, retry_status


MAX_ATTEMPTS = 8
SLOW_DELAY = 1800
RECEIPT_FIELDS = {"schema", "scope", "locale", "request_hash", "prior_attempt", "next_attempt", "prior_error_code", "prior_outcome_fingerprint", "earliest_due", "operator_reason", "grant_sha256", "created_at"}
ATTEMPT_FILES = {"request.json", "summary.json", "journal.json", "raw.sse", "response.json", "response-headers.json", "http.json", "raw-candidate.json"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _selected(locales, reason):
    if not isinstance(locales, (list, tuple)) or not locales or any(not isinstance(locale, str) for locale in locales) or len(set(locales)) != len(locales):
        raise TranslationError("Slow retry authorization requires explicit unique selected locales")
    if not isinstance(reason, str) or not reason.strip():
        raise TranslationError("Explicit nonempty slow retry operator reason is required")
    return sorted(locales), reason.strip()


def _journal(root, grant, locale):
    return core.safe_path(root, f"work/translations/{grant['scope']}/{locale}/journal.json")


def _prefix(root, grant, locale):
    result = {}
    for number in range(1, recovery.ATTEMPTS_PER_GRANT + 1):
        name = f"attempt-{number}"
        files = recovery._inventory(root, f"work/translations/{grant['scope']}/{locale}/{name}")
        if files is None or set(files) - ATTEMPT_FILES or any(value is None for value in files.values()):
            raise TranslationError("Unknown or missing retained slow retry attempt evidence")
        if not {"request.json", "summary.json", "journal.json", "raw.sse"} <= set(files):
            raise TranslationError("Missing retained slow retry attempt evidence")
        result[name] = None
        result.update({f"{name}/{key}": value for key, value in files.items()})
    return result


def _failure_time(journal, record):
    for key in ("failed_at", "failure_at", "finished_at"):
        if key in record:
            if not _number(record[key]):
                raise TranslationError("Invalid retained slow retry failure time")
            return record[key]
    transport = record.get("transport", {})
    if isinstance(transport, dict) and _number(record.get("dispatched_at")) and _number(transport.get("elapsed_seconds")):
        return record["dispatched_at"] + transport["elapsed_seconds"]
    return journal.stat().st_mtime


def _earliest_due(journal, record, created_at, failure_at):
    headers_path = journal.parent / f"attempt-{record['attempt']}" / "response-headers.json"
    headers = core.read_json(headers_path) if headers_path.exists() else {}
    if not isinstance(headers, dict):
        raise TranslationError("Invalid retained slow retry response headers")
    header = next((value for name, value in headers.items() if name.lower() == "retry-after"), None)
    after = retry_after_seconds(header, created_at) or 0
    recorded_after = record.get("retry_after")
    recorded_due = record.get("retry_at")
    if recorded_after is not None and not _number(recorded_after) or recorded_due is not None and not _number(recorded_due):
        raise TranslationError("Invalid retained slow retry due time")
    return max(created_at + max(SLOW_DELAY, after, recorded_after or 0) + 0.01, failure_at + SLOW_DELAY + 0.01, recorded_due or 0)


def _validate_binding(root, run, grant, locale, timeout):
    study, _, _ = recovery._paths(root, run)
    if locale not in core.translation_locales(study) or set(grant.get("locales", {})) != set(core.translation_locales(study)):
        raise TranslationError("Slow retry locale scope differs from immutable grant")
    entry = recovery._effective_entry(grant, locale)
    if entry.get("disposition") != "retry" or entry.get("new_attempt_limit") != recovery.ATTEMPTS_PER_GRANT:
        raise TranslationError("Slow retries require an existing eligible renewal locale")
    context, evidence = recovery._source(root, run, timeout)
    if evidence != grant.get("source_evidence") or recovery._payload_identity(root) != grant.get("payload"):
        raise TranslationError("Slow retry source/approval or payload evidence drift")
    prior = entry["prior"]
    current = recovery._inventory(root, prior["path"])
    if current != prior.get("inventory") or digest(current) != prior.get("digest"):
        raise TranslationError("Retained prior evidence drift for slow retry locale")
    if locale in grant.get("corrections", {}):
        recovery._false_positive(root, entry)
    if recovery._request_summary(root, run, context, locale, evidence) != entry.get("request_summary"):
        raise TranslationError("Slow retry focused request/provenance drift")
    return entry


def _exhausted(root, run, grant, locale, timeout):
    entry = _validate_binding(root, run, grant, locale, timeout)
    if recovery._inventory(root, f"{run}/{locale}") != entry["destination"]:
        raise TranslationError("Slow retry destination drift; refusing overwrite")
    family.family_locale_scaffold(root, run, locale)
    journal = _journal(root, grant, locale)
    if not journal.is_file():
        raise TranslationError("Slow retry requires four exhausted renewal attempts")
    record = core.read_json(journal)
    if type(record.get("attempt")) is not int or record["attempt"] != recovery.ATTEMPTS_PER_GRANT:
        raise TranslationError("Slow retry requires four exhausted renewal attempts")
    files = recovery._inventory(root, journal.parent.relative_to(root).as_posix())
    prefix = _prefix(root, grant, locale)
    allowed = set(prefix) | {"worker.lock", "attempt.lock", "journal.json"}
    if set(files) != allowed:
        raise TranslationError("Unexpected renewal evidence or missing locks; offline recovery required")
    summary = entry["request_summary"]
    for number in range(1, recovery.ATTEMPTS_PER_GRANT + 1):
        folder = journal.parent / f"attempt-{number}"
        request = (folder / "request.json").read_bytes()
        body = core.read_json(folder / "request.json")
        retained = core.read_json(folder / "journal.json")
        if digest(request) != summary["hash"] or core.read_json(folder / "summary.json") != summary or summary.get("bytes") != len(request) or retained.get("input_hash") != summary["hash"] or retained.get("target") != locale or retained.get("attempt") != number:
            raise TranslationError("Slow retry request or attempt provenance changed")
        if not isinstance(body, dict) or any(name in body for name in ("max_output_tokens", "max_tokens", "max_completion_tokens", "output_token_limit", "truncation")) or summary.get("output_cap") is not None:
            raise TranslationError("Slow retries require the unchanged uncapped request")
        response_path = folder / "response.json"
        response = core.read_json(response_path) if response_path.exists() else None
        if not retained_retry_eligible(retained, response, (folder / "raw.sse").read_bytes()):
            raise TranslationError(f"Retained output or permanent/ambiguous failure in attempt-{number} requires offline recovery")
        raw_candidate = folder / "raw-candidate.json"
        if raw_candidate.exists() and core.read_json(raw_candidate) != []:
            raise TranslationError("Retained candidate output requires offline recovery")
        if number == recovery.ATTEMPTS_PER_GRANT and retained != record:
            raise TranslationError("Slow retry top-level and attempt journals differ")
    return journal, record, prefix


def load_extension_receipts(root, grant, grant_sha256):
    root = Path(root).resolve()
    folder = core.safe_path(root, f"work/translations/{grant['scope']}/extended-retries")
    if not folder.exists():
        return {}
    if not folder.is_dir():
        raise TranslationError("Expected slow retry receipt directory")
    paths = sorted(folder.iterdir())
    if not paths:
        raise TranslationError("Interrupted empty slow retry receipt directory")
    receipts = {}
    for path in paths:
        if not stat.S_ISREG(path.lstat().st_mode) or path.name not in {f"{locale}.json" for locale in grant["locales"]}:
            raise TranslationError("Unexpected or duplicate slow retry receipt evidence")
        receipt = core.read_json(path)
        if not isinstance(receipt, dict) or set(receipt) != {"policy", "evidence", "selected_locales", "sha256"}:
            raise TranslationError("Invalid immutable slow retry receipt")
        signed = {key: value for key, value in receipt.items() if key != "sha256"}
        if receipt["sha256"] != digest(signed):
            raise TranslationError("Immutable slow retry receipt digest changed")
        policy, evidence, locale = receipt["policy"], receipt["evidence"], path.stem
        if not isinstance(policy, dict) or set(policy) != {"max_attempts", "appended_delays_seconds", "extension_receipt"} or type(policy.get("max_attempts")) is not int or policy["max_attempts"] != MAX_ATTEMPTS or policy.get("appended_delays_seconds") != [SLOW_DELAY] * 4 or any(type(delay) is not int for delay in policy["appended_delays_seconds"]):
            raise TranslationError("Invalid fixed slow retry attempt budget")
        body = policy["extension_receipt"]
        if not isinstance(body, dict) or set(body) != RECEIPT_FIELDS or type(body.get("schema")) is not int or body["schema"] != 1 or type(body.get("prior_attempt")) is not int or body["prior_attempt"] != 4 or type(body.get("next_attempt")) is not int or body["next_attempt"] != 5:
            raise TranslationError("Invalid slow retry extension anchor")
        entry = recovery._effective_entry(grant, locale)
        if entry.get("disposition") != "retry" or entry.get("new_attempt_limit") != 4 or body.get("scope") != grant["scope"] or body.get("locale") != locale or body.get("grant_sha256") != grant_sha256 or body.get("request_hash") != entry.get("request_summary", {}).get("hash"):
            raise TranslationError("Slow retry receipt binding differs from immutable grant")
        selected, reason = _selected(receipt["selected_locales"], body.get("operator_reason"))
        if selected != receipt["selected_locales"] or locale not in selected or any(item not in grant["locales"] for item in selected) or reason != body["operator_reason"]:
            raise TranslationError("Invalid slow retry selected scope or reason")
        if not isinstance(evidence, dict) or set(evidence) != {"prefix", "journal_sha256", "failure_at"} or not _number(evidence.get("failure_at")) or evidence["prefix"] != _prefix(root, grant, locale):
            raise TranslationError("Retained slow retry prefix evidence changed")
        if not _number(body.get("created_at")) or not _number(body.get("earliest_due")):
            raise TranslationError("Invalid slow retry receipt time")
        journal = _journal(root, grant, locale)
        anchor_path = journal.parent / "attempt-4/journal.json"
        anchor = core.read_json(anchor_path)
        if body.get("prior_error_code") != anchor.get("error_code") or not isinstance(body.get("prior_error_code"), str) or not body["prior_error_code"].strip() or body.get("prior_outcome_fingerprint") != retry_outcome_fingerprint(journal, 4):
            raise TranslationError("Retained slow retry outcome fingerprint changed")
        if body["earliest_due"] != _earliest_due(journal, anchor, body["created_at"], evidence["failure_at"]):
            raise TranslationError("Immutable slow retry due time differs from authorization")
        receipts[locale] = receipt
    for receipt in receipts.values():
        selected = receipt["selected_locales"]
        if any(locale not in receipts or receipts[locale]["selected_locales"] != selected or receipts[locale]["policy"]["extension_receipt"]["operator_reason"] != receipt["policy"]["extension_receipt"]["operator_reason"] for locale in selected):
            raise TranslationError("Interrupted or overlapping slow retry authorization scope")
    return receipts


def _current_lineage(root, grant, locale, receipt):
    journal = _journal(root, grant, locale)
    current = core.read_json(journal)
    if type(current.get("attempt")) is not int or not 4 <= current["attempt"] <= MAX_ATTEMPTS:
        raise TranslationError("Slow retry current attempt moved outside authorized lineage")
    inventory = recovery._inventory(root, journal.parent.relative_to(root).as_posix())
    allowed = {"journal.json", "worker.lock", "attempt.lock", "candidate.json", "generation.json", "publication-generation", "publication-scaffold", "validation"} | {f"attempt-{number}" for number in range(1, current["attempt"] + 1)}
    if {name.split("/")[0] for name in inventory} - allowed:
        raise TranslationError("Unexpected slow retry locale evidence")
    for number in range(5, current["attempt"] + 1):
        files = recovery._inventory(root, f"work/translations/{grant['scope']}/{locale}/attempt-{number}")
        if files is None or set(files) - (ATTEMPT_FILES | {"candidate.json", "repaired-candidate.json", "repair.json"}) or any(value is None for value in files.values()):
            raise TranslationError("Unexpected or missing appended slow retry attempt evidence")
    if current["attempt"] == 4 and core.digest(journal) != receipt["evidence"]["journal_sha256"]:
        raise TranslationError("Retained exhausted journal bytes changed")
    return current


def load_slow_retry_policy(root, run, locale, *, grant=None, timeout=120):
    root = Path(root).resolve()
    grant = recovery._load_grant(root, run) if grant is None else grant
    if grant is None:
        return None
    grant_path = core.safe_path(root, f"work/translations/{grant['scope']}/grant.json")
    receipts = load_extension_receipts(root, grant, core.digest(grant_path))
    if locale not in receipts:
        return None
    expected = receipts[locale]["policy"]

    def validate():
        active = recovery._load_grant(root, run)
        if active is None or active["scope"] != grant["scope"]:
            raise TranslationError("Slow retry immutable active grant changed")
        current = load_extension_receipts(root, active, core.digest(grant_path))
        if locale not in current:
            raise TranslationError("Slow retry operator receipt missing")
        entry = _validate_binding(root, run, active, locale, timeout)
        record = _current_lineage(root, active, locale, current[locale])
        if record.get("state") != "completed" and record.get("family_publication_pending") is not True and recovery._inventory(root, f"{run}/{locale}") != entry["destination"]:
            raise TranslationError("Slow retry destination drift; refusing overwrite")
        return current[locale]["policy"]

    return RetryPolicy(expected, validate_receipt=validate)


@contextlib.contextmanager
def _locks(root, grant, selected):
    entries = {locale: grant["locales"][locale] for locale in selected}
    with recovery._existing_locks(root, {"locales": entries}), contextlib.ExitStack() as stack:
        for locale in selected:
            for name in ("worker.lock", "attempt.lock"):
                path = core.safe_path(root, f"work/translations/{grant['scope']}/{locale}/{name}")
                if not path.is_file():
                    raise TranslationError(f"Missing renewal {name} for {locale}; cannot prove quiescence")
                handle = stack.enter_context(path.open("rb"))
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise TranslationError(f"Renewal locale worker/attempt active: {locale}") from None
        yield


def plan_slow_retries(root, run, locales, *, reason, timeout=120, clock=time.time):
    selected, reason = _selected(locales, reason)
    root = Path(root).resolve()
    grant = recovery._load_grant(root, run)
    if grant is None:
        raise TranslationError("Slow retries require an existing immutable active renewal grant")
    study, _, _ = recovery._paths(root, run)
    if core.safe_path(root, f"family_packs/{study}/{family.family_identity(run)[1]:06d}").exists():
        raise TranslationError("Finalized families cannot receive slow retry authorization")
    if any(locale not in grant["locales"] for locale in selected):
        raise TranslationError("Slow retry locale outside immutable grant scope")
    grant_path = core.safe_path(root, f"work/translations/{grant['scope']}/grant.json")
    grant_sha256 = core.digest(grant_path)
    existing = load_extension_receipts(root, grant, grant_sha256)
    for receipt in existing.values():
        if set(selected) & set(receipt["selected_locales"]) and (receipt["selected_locales"] != selected or receipt["policy"]["extension_receipt"]["operator_reason"] != reason):
            raise TranslationError("Existing slow retry receipt requires the exact same scope and reason")
    created_at = clock()
    if not _number(created_at):
        raise TranslationError("Invalid slow retry operator authorization time")
    receipts = {}
    for locale in selected:
        if locale in existing:
            load_slow_retry_policy(root, run, locale, grant=grant, timeout=timeout)
            receipts[locale] = existing[locale]
            continue
        journal, record, prefix = _exhausted(root, run, grant, locale, timeout)
        failure_at = _failure_time(journal, record)
        body = {"schema": 1, "scope": grant["scope"], "locale": locale, "request_hash": record["input_hash"], "prior_attempt": 4, "next_attempt": 5, "prior_error_code": record.get("error_code"), "prior_outcome_fingerprint": retry_outcome_fingerprint(journal, 4), "earliest_due": _earliest_due(journal, record, created_at, failure_at), "operator_reason": reason, "grant_sha256": grant_sha256, "created_at": created_at}
        policy = {"max_attempts": MAX_ATTEMPTS, "appended_delays_seconds": [SLOW_DELAY] * 4, "extension_receipt": body}
        proof = RetryPolicy(policy, validate_receipt=lambda value=policy: value)
        if not retry_status(journal, proof)["retry_eligible"]:
            raise TranslationError("Exhausted renewal failure is not eligible for slow retries")
        receipt = {"policy": policy, "evidence": {"prefix": prefix, "journal_sha256": core.digest(journal), "failure_at": failure_at}, "selected_locales": selected}
        receipts[locale] = {**receipt, "sha256": digest(receipt)}
    return {"schema": 1, "run": run, "scope": grant["scope"], "grant_id": grant["grant_id"], "selected_locales": selected, "active": all(locale in existing for locale in selected), "extension_receipts": receipts}


def _write_receipt(path, receipt):
    with path.open("xb") as handle:
        handle.write(encode(receipt))
        handle.flush()
        os.fsync(handle.fileno())


def authorize_slow_retries(root, run, locales, *, reason, operator=False, timeout=120, clock=time.time):
    if operator is not True:
        raise TranslationError("Explicit operator=True is required for additional paid slow retries")
    selected, reason = _selected(locales, reason)
    root = Path(root).resolve()
    with core.mutation_lock(root):
        grant = recovery._load_grant(root, run)
        if grant is None or any(locale not in grant["locales"] for locale in selected):
            raise TranslationError("Slow retries require an existing immutable renewal scope")
        with _locks(root, grant, selected):
            created_at = clock()
            plan = plan_slow_retries(root, run, selected, reason=reason, timeout=timeout, clock=lambda: created_at)
            if plan_slow_retries(root, run, selected, reason=reason, timeout=timeout, clock=lambda: created_at) != plan:
                raise TranslationError("Slow retry evidence changed before authorization")
            if plan["active"]:
                return plan
            folder = core.safe_path(root, f"work/translations/{grant['scope']}/extended-retries")
            folder.mkdir(exist_ok=True)
            recovery._sync(folder.parent)
            for locale, receipt in plan["extension_receipts"].items():
                _write_receipt(folder / f"{locale}.json", receipt)
                recovery._sync(folder)
            recovery._sync(folder.parent)
            loaded = load_extension_receipts(root, grant, core.digest(folder.parent / "grant.json"))
            if any(loaded.get(locale) != receipt for locale, receipt in plan["extension_receipts"].items()):
                raise TranslationError("Slow retry receipt changed during authorization")
            return {**plan, "active": True}
