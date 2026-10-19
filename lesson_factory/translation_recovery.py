import contextlib
import fcntl
import json
import os
import re
import stat
import uuid
from pathlib import Path

from . import core, family
from .translation import TranslationError, _unique_object, atomic_write, digest, encode, prepare, validate_candidate


PAYLOAD_VERSION = 2
ATTEMPTS_PER_GRANT = 4


def _inventory(root, relative):
    folder = core.safe_path(root, relative)
    if not folder.exists():
        return None
    if not folder.is_dir():
        raise TranslationError("Expected a directory in translation recovery evidence")
    result = {}
    for path in sorted(folder.rglob("*")):
        mode = path.lstat().st_mode
        name = path.relative_to(folder).as_posix()
        if stat.S_ISDIR(mode):
            result[name] = None
        elif stat.S_ISREG(mode):
            result[name] = core.digest(path)
        else:
            raise TranslationError("Symlink or special recovery evidence refused")
    return result


def _paths(root, run):
    study, base = family.family_identity(run)
    scope = f"families/{study}/{base:06d}"
    folder = core.safe_path(root, f"work/translations/{scope}")
    return study, scope, folder


def _source(root, run, timeout):
    context = family.load_family(root, run)
    family.require_family_source_reviews(root, run, timeout=timeout)
    paths = [f"{run}/family-context.json"]
    for stage in family.STAGES:
        paths.append(f"{run}/stage-{stage}/brief.json")
        paths.extend(f"{run}/{context['source_locale']}/stage-{stage}/{name}" for name in ("lesson.md", "quiz.md", "adaptation-notes.json"))
    paths.extend(family.family_review_path(root, run, role).relative_to(root).as_posix() for role in ("pedagogical", "assessment", "format"))
    files = {name: core.digest(core.safe_path(root, name)) for name in paths}
    evidence = {
        "binding": family.family_binding(root, run, "pedagogical"),
        "context_digest": files[f"{run}/family-context.json"],
        "reviews": {role: core.digest(family.family_review_path(root, run, role)) for role in ("pedagogical", "assessment", "format")},
        "files": files,
    }
    return context, evidence


def validate_preserved_locale(root, run, locale, *, timeout=120):
    root = Path(root).resolve()
    context = family.load_family(root, run)
    if locale not in core.translation_locales(context["study_language"]):
        raise TranslationError("Preserved locale must exclude study and source")
    before = _inventory(root, f"{run}/{locale}")
    expected = {f"stage-{stage}" for stage in family.STAGES} | {f"stage-{stage}/{name}" for stage in family.STAGES for name in ("lesson.md", "quiz.md", "adaptation-notes.json")}
    if before is None or set(before) != expected:
        raise TranslationError("Partial or edited destination cannot be regenerated; complete locale required")
    baseline = family.validate_family(root, run, context["source_locale"], timeout=timeout)
    report = family.validate_family(root, run, locale, timeout=timeout)
    if not report.get("ok") or "family_contract" not in report or report["family_contract"] != baseline.get("family_contract"):
        raise TranslationError("Preserved locale must match the deep source family contract")
    if _inventory(root, f"{run}/{locale}") != before:
        raise TranslationError("Destination changed during preservation validation")
    return report


def _placeholder_stage(item):
    if set(item) - {"stage", "lesson_md", "quiz_md", "adaptation_notes"}:
        return False
    for field in ("lesson_md", "quiz_md"):
        if field in item and (not isinstance(item[field], str) or item[field].strip(" .…\t\r\n")):
            return False
    if "adaptation_notes" in item:
        notes = item["adaptation_notes"]
        if not isinstance(notes, list) or any(not isinstance(note, str) or note.strip(" .…\t\r\n") for note in notes):
            return False
    return True


def _has_candidate(value, *, require_content=True):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            return False
    if isinstance(value, dict):
        stages = value.get("stages")
        if isinstance(stages, list) and len(stages) == 5 and {item.get("stage") for item in stages if isinstance(item, dict) and type(item.get("stage")) is int} == set(family.STAGES):
            if not require_content or any(not _placeholder_stage(item) for item in stages):
                return True
        return any(_has_candidate(item, require_content=require_content) for key, item in value.items() if key in ("output", "content", "text", "item", "response"))
    if isinstance(value, list):
        return any(_has_candidate(item, require_content=require_content) for item in value)
    return False


_FAMILY_START = re.compile(r'\{\s*"stages"\s*:')
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _family_objects(text):
    """Every complete five-stage object in one output text, including JSON surrounded by commentary/fences."""
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    found = []
    for match in _FAMILY_START.finditer(text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        stages = value.get("stages") if isinstance(value, dict) else None
        if isinstance(stages, list) and len(stages) == 5 and {item.get("stage") for item in stages if isinstance(item, dict) and type(item.get("stage")) is int} == set(family.STAGES):
            found.append(value)
    return found


def _sse_events(data):
    for block in re.split(rb"\r?\n\r?\n", data):
        payload = b"\n".join(line[5:].lstrip(b" ") for line in block.splitlines() if line.startswith(b"data:"))
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict):
            yield value


def _message_texts(output, origin):
    for index, item in enumerate(output if isinstance(output, list) else ()):
        if isinstance(item, dict) and item.get("type", "message") == "message":
            for part in item.get("content") or ():
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield {"origin": origin, "output_index": index, "item_id": item.get("id")}, part["text"]


def _attempt_texts(attempt):
    """Per-item output texts; separate items are never concatenated."""
    if (attempt / "raw.sse").is_file():
        deltas = {}
        for event in _sse_events((attempt / "raw.sse").read_bytes()):
            kind = event.get("type")
            if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
                deltas.setdefault((event.get("output_index"), event.get("content_index"), event.get("item_id")), []).append(event["delta"])
            elif kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                for where, text in _message_texts([event["item"]], "sse.output_item.done"):
                    yield {**where, "output_index": event.get("output_index")}, text
            elif kind == "response.output_text.done" and isinstance(event.get("text"), str):
                yield {"origin": "sse.output_text.done", "output_index": event.get("output_index"), "item_id": event.get("item_id")}, event["text"]
            elif isinstance(event.get("response"), dict):
                yield from _message_texts(event["response"].get("output"), f"sse.{kind}.output")
        for (index, _, item_id), parts in deltas.items():
            yield {"origin": "sse.output_text.delta", "output_index": index, "item_id": item_id}, "".join(parts)
    for name, origin in (("response.json", "response.output"), ("raw-candidate.json", "raw-candidate")):
        if (attempt / name).is_file():
            try:
                value = json.loads((attempt / name).read_bytes())
            except (ValueError, UnicodeError):
                continue
            yield from _message_texts(value.get("output") if isinstance(value, dict) else value, origin)


def _anomalies(candidate):
    found = set()
    for item in candidate["stages"]:
        for key, value in item.items():
            for text in value if isinstance(value, list) else (value,):
                if isinstance(text, str) and _CONTROL.search(text):
                    found.add(f"stage-{item.get('stage')}.{key}: control characters (possible transport/escape corruption)")
    return sorted(found)


def retained_family_items(folder):
    """Read-only inventory of complete five-stage objects retained under a locale or attempt folder."""
    folder = Path(folder)
    attempts = [folder] if (folder / "request.json").is_file() else sorted((path for path in folder.glob("attempt-*") if path.is_dir()), key=lambda path: int(path.name.split("-")[1]))
    items = {}
    for attempt in attempts:
        record = core.read_json(attempt / "journal.json") if (attempt / "journal.json").is_file() else {}
        for where, text in _attempt_texts(attempt):
            for candidate in _family_objects(text):
                key = digest(encode(candidate))
                entry = items.setdefault(key, {
                    "candidate_sha256": key,
                    "candidate": candidate,
                    "placeholder": all(_placeholder_stage(item) for item in candidate["stages"]),
                    "anomalies": _anomalies(candidate),
                    "attempt": int(attempt.name.split("-")[1]) if attempt.name.startswith("attempt-") else None,
                    "attempt_path": attempt.as_posix(),
                    "request_sha256": record.get("input_hash"),
                    "request_id": record.get("request_id"),
                    "response_id": record.get("response_id"),
                    "journal_state": record.get("state"),
                    "provider_status": record.get("provider_status"),
                    "error_code": record.get("error_code"),
                    "sources": [],
                })
                source = {**where, "text_sha256": digest(text.encode("utf-8"))}
                if source not in entry["sources"]:
                    entry["sources"].append(source)
    return list(items.values())


def _terminal_completion(value):
    return isinstance(value, dict) and (value.get("type") == "response.completed" or value.get("status") == "completed" or value.get("state") in ("completed", "rejected") or value.get("provider_status") == "completed" or isinstance(value.get("response"), dict) and _terminal_completion(value["response"]))


def _completed_evidence(folder, *, require_content=True):
    if any(not require_content or not item["placeholder"] for item in retained_family_items(folder)):
        return True
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if "candidate" in path.name:
            return True
        if path.name in ("response.json", "journal.json"):
            value = core.read_json(path)
            if _has_candidate(value, require_content=require_content) or _terminal_completion(value):
                return True
        if path.name == "raw.sse":
            data = path.read_bytes()
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError):
                value = None
            if _has_candidate(value, require_content=require_content) or _terminal_completion(value):
                return True
            fragments = {}
            for event in re.split(rb"\r?\n\r?\n", data):
                payload = b"\n".join(line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:"))
                try:
                    value = json.loads(payload)
                except (ValueError, UnicodeError):
                    continue
                if _has_candidate(value, require_content=require_content) or _terminal_completion(value):
                    return True
                if isinstance(value, dict) and value.get("type") == "response.output_text.delta" and isinstance(value.get("delta"), str):
                    key = str((value.get("output_index"), value.get("content_index"), value.get("item_id")))
                    fragments.setdefault(key, []).append(value["delta"])
            if any(_has_candidate("".join(parts), require_content=require_content) for parts in fragments.values()):
                return True
    return False


def _prior(root, scope, locale):
    relative = f"work/translations/{scope}/{locale}"
    files = _inventory(root, relative)
    folder = core.safe_path(root, relative)
    journal = folder / "journal.json"
    if not journal.exists():
        if files is not None and set(files) - {"worker.lock", "attempt.lock"}:
            raise TranslationError(f"Orphan prior attempt evidence for {locale}; offline recovery required")
        return {"path": relative, "inventory": files, "digest": digest(files), "attempts_consumed": 0, "billing_outcome": "no retained dispatch evidence", "completed_evidence": False}
    previous = core.read_json(journal)
    count = previous.get("attempt")
    if type(count) is not int or not 1 <= count <= ATTEMPTS_PER_GRANT:
        raise TranslationError("Invalid retained attempt count; refusing budget replacement")
    attempts = {name for name, value in files.items() if "/" not in name and value is None and name.startswith("attempt-")}
    if attempts != {f"attempt-{number}" for number in range(1, count + 1)}:
        raise TranslationError("Missing or orphan prior attempts; offline recovery required")
    for number in range(1, count + 1):
        attempt = folder / f"attempt-{number}"
        request_hash = core.digest(attempt / "request.json")
        summary = core.read_json(attempt / "summary.json")
        record = core.read_json(attempt / "journal.json")
        if summary.get("hash") != request_hash or summary.get("target") != locale or summary.get("bytes") != (attempt / "request.json").stat().st_size or record.get("input_hash") != request_hash or record.get("attempt") != number or record.get("target") != locale or previous.get("input_hash") != request_hash:
            raise TranslationError("Prior request provenance changed; refusing renewal")
        if number == count and (previous.get("request_id") != record.get("request_id") or previous.get("target") != locale):
            raise TranslationError("Retained journal identity differs from last attempt")
    return {"path": relative, "inventory": files, "digest": digest(files), "attempts_consumed": count, "billing_outcome": "unknown; prior failures may have been billed", "completed_evidence": _completed_evidence(folder)}


def _payload_identity(root):
    from .translation_payload import PAYLOAD_VERSION as current_version, build_prompt
    if current_version != PAYLOAD_VERSION:
        raise TranslationError("Renewal requires focused payload version 2")
    return {"version": PAYLOAD_VERSION, "prompt_sha256": digest(build_prompt(root).encode("utf-8"))}


def _request_summary(root, run, context, locale, evidence):
    from .translation_payload import build_payload, build_prompt
    approval = {key: evidence[key] for key in ("binding", "context_digest", "reviews")}
    _, summary = prepare(build_payload(root, run, context, locale, approval), locale, build_prompt(root))
    return summary


def _correction_body(grant, grant_sha256, locale, selected, reason, summary):
    entry = grant["locales"][locale]
    return {
        "schema": 1,
        "run": grant["run"],
        "grant_id": grant["grant_id"],
        "grant_sha256": grant_sha256,
        "locale": locale,
        "selected_locales": selected,
        "reason": reason,
        "operator": True,
        "from_disposition": "offline_recovery",
        "disposition": "retry",
        "new_attempt_limit": ATTEMPTS_PER_GRANT,
        "proof": "five-stage-placeholder-only-v1",
        "source_evidence_sha256": digest(grant["source_evidence"]),
        "payload_sha256": digest(grant["payload"]),
        "prior_sha256": entry["prior"]["digest"],
        "destination_sha256": digest(entry["destination"]),
        "request_summary": summary,
    }


def _load_corrections(root, grant, grant_sha256):
    folder = core.safe_path(root, f"work/translations/{grant['scope']}/corrections")
    if not folder.exists():
        return {}
    if not folder.is_dir():
        raise TranslationError("Expected correction receipt directory")
    receipts = {}
    for path in sorted(folder.iterdir()):
        if not stat.S_ISREG(path.lstat().st_mode) or path.name not in {f"{locale}.json" for locale in grant["locales"]}:
            raise TranslationError("Unexpected or duplicate correction receipt evidence")
        receipt = core.read_json(path)
        if not isinstance(receipt, dict) or set(receipt) != {"correction", "sha256"} or not isinstance(receipt.get("correction"), dict) or receipt.get("sha256") != digest(receipt["correction"]):
            raise TranslationError("Immutable correction receipt digest changed")
        body = receipt["correction"]
        locale = path.stem
        selected, reason = body.get("selected_locales"), body.get("reason")
        if not isinstance(selected, list) or not selected or any(not isinstance(item, str) for item in selected) or selected != sorted(set(selected)) or locale not in selected or not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
            raise TranslationError("Invalid correction operator scope/reason")
        if any(item not in grant["locales"] or grant["locales"][item].get("disposition") != "offline_recovery" or grant["locales"][item].get("new_attempt_limit") != 0 or grant["locales"][item].get("prior", {}).get("completed_evidence") is not True for item in selected):
            raise TranslationError("Correction must retain the original offline recovery scope")
        expected = _correction_body(grant, grant_sha256, locale, selected, reason, body.get("request_summary"))
        if encode(body) != encode(expected) or not isinstance(body.get("request_summary"), dict):
            raise TranslationError("Correction receipt binding differs from immutable grant")
        for prior in receipts.values():
            other = prior["correction"]
            if set(other["selected_locales"]) & set(selected) and (other["selected_locales"] != selected or other["reason"] != reason):
                raise TranslationError("Overlapping correction authorization differs")
        receipts[locale] = receipt
    return receipts


def _effective_entry(grant, locale):
    entry = grant["locales"][locale]
    receipt = grant.get("corrections", {}).get(locale)
    if receipt is None:
        return entry
    return {**entry, "disposition": "retry", "new_attempt_limit": ATTEMPTS_PER_GRANT, "request_summary": receipt["correction"]["request_summary"]}


def _load_grant(root, run):
    _, scope, folder = _paths(root, run)
    renewals = core.safe_path(root, f"work/translations/{scope}/renewals")
    pointer = core.safe_path(root, f"work/translations/{scope}/renewal-active.json")
    if not pointer.exists():
        if renewals.exists():
            raise TranslationError("Orphan renewal evidence without active pointer; explicit offline reconciliation required")
        return None
    active = core.read_json(pointer)
    grant_id = active.get("grant_id")
    if not isinstance(grant_id, str) or not re.fullmatch(r"[0-9a-f]{32}", grant_id):
        raise TranslationError("Invalid active renewal grant identity")
    if not renewals.is_dir() or {path.name for path in renewals.iterdir()} != {grant_id}:
        raise TranslationError("Orphan or additional renewal grant; refusing duplicate authorization")
    grant_dir = core.safe_path(root, f"work/translations/{scope}/renewals/{grant_id}")
    grant_path = core.safe_path(root, f"work/translations/{scope}/renewals/{grant_id}/grant.json")
    if set(active) != {"grant_id", "sha256"} or core.digest(grant_path) != active.get("sha256"):
        raise TranslationError("Immutable renewal grant digest changed")
    grant = core.read_json(grant_path)
    if grant.get("schema") != 1 or grant.get("run") != run or grant.get("grant_id") != grant_id or grant.get("scope") != f"{scope}/renewals/{grant_id}" or grant.get("attempts_per_retry_locale") != ATTEMPTS_PER_GRANT:
        raise TranslationError("Invalid renewal grant contract")
    corrections = _load_corrections(root, grant, active["sha256"])
    from .slow_retry_recovery import load_extension_receipts
    extensions = load_extension_receipts(root, {**grant, "corrections": corrections}, active["sha256"])
    allowed = {"grant.json", "source-evidence.json", "corrections", "extended-retries"} | {locale for locale, entry in grant["locales"].items() if entry["disposition"] == "retry"} | set(corrections)
    if {path.name for path in grant_dir.iterdir()} - allowed:
        raise TranslationError("Unexpected renewal grant evidence")
    for path in grant_dir.iterdir():
        if path.is_symlink():
            raise TranslationError("Symlink renewal evidence refused")
    source_path = grant_dir / "source-evidence.json"
    if core.digest(source_path) != grant.get("source_copy_sha256"):
        raise TranslationError("Retained local source evidence changed")
    return {**grant, **({"corrections": corrections} if corrections else {}), **({"extensions": extensions} if extensions else {})}


def _candidate_inventory(candidate):
    return {f"stage-{stage}": None for stage in family.STAGES} | {
        f"stage-{item['stage']}/{name}": digest(data)
        for item in candidate["stages"]
        for name, data in (("lesson.md", item["lesson_md"].encode("utf-8")), ("quiz.md", item["quiz_md"].encode("utf-8")), ("adaptation-notes.json", encode(item["adaptation_notes"])))
    }


def _practice(root, run):
    context = family.load_family(root, run)
    return tuple(item["stage"] for item in context["stages"] if item["stage"] and core.read_json(family.family_path(root, run) / f"stage-{item['stage']}/brief.json")["extension"]["mode"] == "practice")


RETAINED_OUTPUT = "Known completed renewal candidate requires offline recovery; no new purchase"
UNPUBLISHED_EDIT = "Destination edited without tool publication and not a complete deep-valid locale; finish all fifteen files offline, deep-validate and review; no overwrite or new purchase"


def _unpublished_destination(root, run, locale, current, timeout, reasons):
    expected = {f"stage-{stage}" for stage in family.STAGES} | {f"stage-{stage}/{name}" for stage in family.STAGES for name in ("lesson.md", "quiz.md", "adaptation-notes.json")}
    if current is not None and set(current) == expected:
        try:
            validate_preserved_locale(root, run, locale, timeout=timeout)
            return "preserve"
        except core.AuthoringError as error:
            if "changed during" in str(error):
                raise
    reasons[locale] = UNPUBLISHED_EDIT
    return "offline_recovery"


def _retry_destination(root, run, locale, grant, entry, timeout, retry_policy=None, reasons=None):
    reasons = {} if reasons is None else reasons
    current = _inventory(root, f"{run}/{locale}")
    relative = f"work/translations/{grant['scope']}/{locale}"
    files = _inventory(root, relative)
    folder = core.safe_path(root, relative)
    journal_path = folder / "journal.json"
    if not journal_path.exists():
        if files is not None and set(files) - {"worker.lock", "attempt.lock"}:
            raise TranslationError("Orphan renewal attempt evidence; refusing budget reset")
        if current != entry["destination"]:
            raise TranslationError("Renewal destination drift; refusing overwrite")
        return "retry"
    journal = core.read_json(journal_path)
    count = journal.get("attempt")
    limit = retry_policy["max_attempts"] if retry_policy is not None else ATTEMPTS_PER_GRANT
    if type(count) is not int or not 1 <= count <= limit:
        raise TranslationError("Invalid renewal attempt count")
    if journal.get("input_hash") != entry["request_summary"]["hash"] or journal.get("target") != locale:
        raise TranslationError("Renewal request identity differs from operator grant")
    attempts = {name for name, value in files.items() if "/" not in name and value is None and name.startswith("attempt-")}
    if attempts != {f"attempt-{number}" for number in range(1, count + 1)}:
        raise TranslationError("Missing or orphan renewal attempts; refusing additional purchase")
    if journal.get("state") not in ("completed", "rejected") and (folder / "candidate.json").exists():
        raise TranslationError("Retained renewal candidate requires offline recovery; no new purchase")
    known_output = False
    for number in range(1, count + 1):
        attempt = folder / f"attempt-{number}"
        if core.digest(attempt / "request.json") != journal["input_hash"] or core.read_json(attempt / "summary.json") != entry["request_summary"]:
            raise TranslationError("Renewal request evidence changed")
        proven_retryable = False
        if retry_policy is not None and (attempt / "raw.sse").exists():
            from .translation import retained_retry_eligible, slow_retry_reason, read_stream
            import io
            raw = (attempt / "raw.sse").read_bytes()
            response = core.read_json(attempt / "response.json") if (attempt / "response.json").exists() else None
            if response is None:
                try:
                    response = read_stream(io.BytesIO(raw), bytearray())
                except (ValueError, UnicodeError, TypeError, KeyError):
                    response = None
            retained = core.read_json(attempt / "journal.json")
            proven_retryable = retained_retry_eligible(retained, response, raw) or (number == count and journal.get("state") == "pending" and slow_retry_reason(response, raw) is not None)
        if _completed_evidence(attempt) and (number < count or journal.get("state") not in ("completed", "rejected")) and not proven_retryable:
            known_output = True
    if "family_publication_pending" not in journal and current != entry["destination"]:
        return _unpublished_destination(root, run, locale, current, timeout, reasons)
    if known_output:
        reasons[locale] = RETAINED_OUTPUT
        return "offline_recovery"
    if current == entry["destination"] and journal.get("family_publication_pending") is not False:
        return "retry"
    candidate = core.read_json(folder / "candidate.json")
    validate_candidate(candidate, reviewed_practice_stages=_practice(root, run))
    expected = _candidate_inventory(candidate)
    receipt = {"candidate_hash": digest(encode(candidate)), "input_hash": journal.get("input_hash"), "files": {name: value for name, value in expected.items() if value is not None}}
    if journal.get("output_hash") != digest(encode(candidate)) or journal.get("family_publication_hashes") != expected or core.read_json(folder / "generation.json") != receipt:
        raise TranslationError("Renewal publication provenance differs from candidate")
    if journal.get("family_publication_pending") is True:
        backup = _inventory(root, f"{relative}/publication-scaffold")
        if journal.get("family_publication_previous") != entry["destination"] or current not in (None, expected) or backup != entry["destination"]:
            raise TranslationError("Interrupted renewal publication destination drift")
        return "retry"
    if journal.get("family_publication_pending") is not False:
        raise TranslationError("Renewal publication was not completed")
    validate_preserved_locale(root, run, locale, timeout=timeout)
    return "preserve"


def _false_positive(root, entry):
    folder = core.safe_path(root, entry["prior"]["path"])
    if entry["prior"].get("completed_evidence") is not True or _completed_evidence(folder) or not _completed_evidence(folder, require_content=False):
        raise TranslationError("False-positive placeholder proof required; known completed output remains offline recovery only")


def _validate_grant(root, run, grant, timeout, selected=None, reasons=None):
    reasons = {} if reasons is None else reasons
    study, _, _ = _paths(root, run)
    context, evidence = _source(root, run, timeout)
    if evidence != grant.get("source_evidence"):
        raise TranslationError("Renewal source/approval evidence drift; grant cannot be replaced")
    if _payload_identity(root) != grant.get("payload"):
        raise TranslationError("Renewal payload/provenance drift; grant cannot be replaced")
    if set(grant.get("locales", {})) != set(core.translation_locales(study)):
        raise TranslationError("Renewal locale scope changed")
    dispositions = {}
    for locale in grant["locales"]:
        entry = _effective_entry(grant, locale)
        disposition = entry.get("disposition")
        if disposition == "untouched":
            continue
        if disposition not in ("retry", "preserve", "offline_recovery"):
            raise TranslationError("Unknown renewal locale disposition")
        if selected is not None and locale != selected:
            continue
        old = entry["prior"]
        current = _inventory(root, old["path"])
        if current != old["inventory"] or digest(current) != old["digest"]:
            raise TranslationError(f"Retained prior evidence drift for {locale}")
        if locale in grant.get("corrections", {}):
            _false_positive(root, entry)
        if disposition == "retry" and _request_summary(root, run, context, locale, evidence) != entry.get("request_summary"):
            raise TranslationError("Renewal focused request/provenance drift")
        if disposition == "retry":
            from .slow_retry_recovery import load_slow_retry_policy
            policy = load_slow_retry_policy(root, run, locale, grant=grant, timeout=timeout) if locale in grant.get("extensions", {}) else None
            disposition = _retry_destination(root, run, locale, grant, entry, timeout, retry_policy=policy, reasons=reasons)
        elif disposition == "preserve":
            validate_preserved_locale(root, run, locale, timeout=timeout)
        elif (current := _inventory(root, f"{run}/{locale}")) != entry["destination"]:
            disposition = _unpublished_destination(root, run, locale, current, timeout, reasons)
        else:
            reasons[locale] = RETAINED_OUTPUT
        dispositions[locale] = disposition
    return dispositions


def plan_translation_recovery(root, run, *, timeout=120):
    root = Path(root).resolve()
    study, scope, folder = _paths(root, run)
    active = _load_grant(root, run)
    if active is not None:
        reasons = {}
        dispositions = _validate_grant(root, run, active, timeout, reasons=reasons)
        limits = {locale: 0 if dispositions.get(locale) in ("offline_recovery", "preserve") else active["extensions"][locale]["policy"]["max_attempts"] if locale in active.get("extensions", {}) else _effective_entry(active, locale)["new_attempt_limit"] for locale in active["locales"]}
        return {**active, "active": True, "current_dispositions": dispositions, "current_reasons": reasons, "effective_attempt_limits": limits}
    context, evidence = _source(root, run, timeout)
    if core.safe_path(root, f"family_packs/{study}/{family.family_identity(run)[1]:06d}").exists():
        raise TranslationError("Finalized families cannot receive translation renewal")
    if folder.exists() and {path.name for path in folder.iterdir()} - set(core.translation_locales(study)):
        raise TranslationError("Unknown original translation evidence; refusing renewal")
    locales = {}
    for locale in core.translation_locales(study):
        prior = _prior(root, scope, locale)
        destination = _inventory(root, f"{run}/{locale}")
        try:
            family.family_locale_scaffold(root, run, locale)
            empty = True
        except core.AuthoringError:
            empty = False
        if not empty:
            validate_preserved_locale(root, run, locale, timeout=timeout)
            disposition = "preserve"
        elif not prior["attempts_consumed"]:
            disposition = "untouched"
        elif prior["completed_evidence"]:
            disposition = "offline_recovery"
        else:
            disposition = "retry"
        locales[locale] = {"disposition": disposition, "destination": destination, "prior": prior, "new_attempt_limit": ATTEMPTS_PER_GRANT if disposition == "retry" else 0}
        if disposition == "retry":
            locales[locale]["request_summary"] = _request_summary(root, run, context, locale, evidence)
    return {"schema": 1, "run": run, "active": False, "source_evidence": evidence, "payload": _payload_identity(root), "attempts_per_retry_locale": ATTEMPTS_PER_GRANT, "locales": locales}


@contextlib.contextmanager
def _existing_locks(root, plan):
    with contextlib.ExitStack() as stack:
        for locale, entry in sorted(plan["locales"].items()):
            prior = entry["prior"]
            for name in ("worker.lock", "attempt.lock"):
                path = core.safe_path(root, f"{prior['path']}/{name}")
                if not path.exists():
                    if prior["attempts_consumed"]:
                        raise TranslationError(f"Missing original {name} for {locale}; cannot prove quiescence")
                    continue
                handle = stack.enter_context(path.open("rb"))
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise TranslationError(f"Original locale worker/attempt active: {locale}") from None
        yield


def _sync(folder):
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def authorize_translation_recovery(root, run, *, reason, timeout=120):
    if not isinstance(reason, str) or not reason.strip():
        raise TranslationError("Explicit nonempty operator authorization reason is required")
    root = Path(root).resolve()
    with core.mutation_lock(root):
        plan = plan_translation_recovery(root, run, timeout=timeout)
        if plan["active"]:
            return plan
        if not any(entry["disposition"] == "retry" for entry in plan["locales"].values()):
            raise TranslationError("No incomplete attempted locale requires a new paid grant")
        with _existing_locks(root, plan):
            if plan_translation_recovery(root, run, timeout=timeout) != plan:
                raise TranslationError("Recovery plan changed before authorization")
            _, scope, folder = _paths(root, run)
            grant_id = uuid.uuid4().hex
            grant_dir = core.safe_path(root, f"work/translations/{scope}/renewals/{grant_id}")
            source_copy = {name: core.safe_path(root, name).read_bytes().decode("utf-8") for name in plan["source_evidence"]["files"]}
            source_bytes = encode(source_copy)
            for name, text in source_copy.items():
                if digest(text.encode("utf-8")) != plan["source_evidence"]["files"][name]:
                    raise TranslationError("Source changed while retaining authorization evidence")
            grant = {key: value for key, value in plan.items() if key != "active"}
            grant.update(grant_id=grant_id, scope=f"{scope}/renewals/{grant_id}", reason=reason.strip(), source_copy_sha256=digest(source_bytes))
            grant_dir.mkdir(parents=True, exist_ok=False)
            atomic_write(grant_dir / "source-evidence.json", source_bytes)
            atomic_write(grant_dir / "grant.json", encode(grant))
            _sync(grant_dir)
            _sync(grant_dir.parent)
            atomic_write(folder / "renewal-active.json", encode({"grant_id": grant_id, "sha256": digest(encode(grant))}))
            _sync(folder)
            return {**grant, "active": True}


def authorize_translation_correction(root, run, locales, *, reason, operator=False, timeout=120):
    if operator is not True or not isinstance(reason, str) or not reason.strip():
        raise TranslationError("Explicit operator=True and nonempty correction reason are required")
    if not isinstance(locales, (list, tuple)) or not locales or any(not isinstance(locale, str) for locale in locales) or len(set(locales)) != len(locales):
        raise TranslationError("Correction requires explicit unique selected locales")
    selected = sorted(locales)
    reason = reason.strip()
    root = Path(root).resolve()
    with core.mutation_lock(root):
        grant = _load_grant(root, run)
        if grant is None:
            raise TranslationError("Correction requires an existing immutable renewal grant")
        study, _, _ = _paths(root, run)
        if core.safe_path(root, f"family_packs/{study}/{family.family_identity(run)[1]:06d}").exists():
            raise TranslationError("Finalized families cannot receive translation correction")
        for locale in selected:
            entry = grant["locales"].get(locale, {})
            if entry.get("disposition") != "offline_recovery" or entry.get("new_attempt_limit") != 0:
                raise TranslationError("Correction only permits original offline_recovery locales")
        for receipt in grant.get("corrections", {}).values():
            body = receipt["correction"]
            if set(body["selected_locales"]) & set(selected) and (body["selected_locales"] != selected or body["reason"] != reason):
                raise TranslationError("Existing correction requires the exact same selected scope and reason")
        with _existing_locks(root, {"locales": {locale: grant["locales"][locale] for locale in selected}}):
            if _load_grant(root, run) != grant:
                raise TranslationError("Correction grant changed before authorization")
            context, evidence = _source(root, run, timeout)
            grant_dir = core.safe_path(root, f"work/translations/{grant['scope']}")
            grant_sha256 = core.digest(grant_dir / "grant.json")
            receipts = {}
            for locale in selected:
                entry = grant["locales"][locale]
                _validate_grant(root, run, grant, timeout, selected=locale)
                _false_positive(root, entry)
                if _inventory(root, f"{run}/{locale}") != entry["destination"]:
                    raise TranslationError("Correction destination drift; refusing overwrite")
                family.family_locale_scaffold(root, run, locale)
                if _inventory(root, f"work/translations/{grant['scope']}/{locale}") is not None:
                    raise TranslationError("Renewal locale evidence already exists; correction cannot reset used budgets")
                body = _correction_body(grant, grant_sha256, locale, selected, reason, _request_summary(root, run, context, locale, evidence))
                receipt = {"correction": body, "sha256": digest(body)}
                previous = grant.get("corrections", {}).get(locale)
                if previous is not None and previous != receipt:
                    raise TranslationError("Immutable correction receipt differs; no replacement allowed")
                receipts[locale] = receipt
            if _source(root, run, timeout)[1] != evidence or _payload_identity(root) != grant["payload"]:
                raise TranslationError("Correction source/payload evidence changed before receipt")
            for locale in selected:
                entry = grant["locales"][locale]
                if _inventory(root, entry["prior"]["path"]) != entry["prior"]["inventory"] or _inventory(root, f"{run}/{locale}") != entry["destination"]:
                    raise TranslationError("Correction selected evidence changed before receipt")
            corrections = grant_dir / "corrections"
            corrections.mkdir(exist_ok=True)
            for locale, receipt in receipts.items():
                if locale not in grant.get("corrections", {}):
                    atomic_write(corrections / f"{locale}.json", encode(receipt))
                    _sync(corrections)
                    _sync(grant_dir)
            return {"schema": 1, "run": run, "active": True, "grant_id": grant["grant_id"], "scope": grant["scope"], "corrected_locales": selected, "correction_receipts": {locale: {"path": f"work/translations/{grant['scope']}/corrections/{locale}.json", "sha256": receipt["sha256"], "new_attempt_limit": ATTEMPTS_PER_GRANT} for locale, receipt in receipts.items()}}


def recovery_for_locale(root, run, locale, *, timeout=120):
    root = Path(root).resolve()
    study, _, _ = _paths(root, run)
    if locale not in core.translation_locales(study):
        raise TranslationError("Recovery locale must exclude study and source")
    grant = _load_grant(root, run)
    if grant is None:
        return None
    reasons = {}
    dispositions = _validate_grant(root, run, grant, timeout, selected=locale, reasons=reasons)
    disposition = dispositions.get(locale)
    if disposition is None:
        return None
    if disposition == "offline_recovery":
        raise TranslationError(f"{reasons.get(locale, RETAINED_OUTPUT)} [{locale}]")
    return {"scope": grant["scope"], "grant_id": grant["grant_id"], "disposition": disposition, "source_evidence": grant["source_evidence"], "request_summary": _effective_entry(grant, locale).get("request_summary")}


def _contract_differences(actual, expected, path="", limit=40):
    if len(path) > 400:
        return [path]
    if isinstance(actual, dict) and isinstance(expected, dict):
        keys = sorted(set(actual) | set(expected))
        return [found for key in keys for found in _contract_differences(actual.get(key), expected.get(key), f"{path}.{key}", limit)][:limit]
    if isinstance(actual, list) and isinstance(expected, list) and len(actual) == len(expected):
        return [found for index, pair in enumerate(zip(actual, expected)) for found in _contract_differences(*pair, f"{path}[{index}]", limit)][:limit]
    return [] if actual == expected else [path or "."]


def _draft_path(root, path):
    raw = Path(path)
    try:
        relative = raw.relative_to(root) if raw.is_absolute() else raw
    except ValueError as error:
        raise TranslationError("Draft output must be under work/content-recovery") from error
    if relative.parts[:2] != ("work", "content-recovery") or len(relative.parts) < 3:
        raise TranslationError("Draft output must be under work/content-recovery, outside provider evidence and family trees")
    return core.safe_path(root, relative)


def _new_draft(root, path, data):
    path = _draft_path(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _draft_path(root, path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


FAMILY_FILES = tuple(f"stage-{stage}/{name}" for stage in family.STAGES for name in ("lesson.md", "quiz.md", "adaptation-notes.json"))


def _alternatives(items):
    """Usable retained families bound to the current pinned request; never auto-ranked ("last wins" forbidden)."""
    usable = [entry for entry in items if not entry["placeholder"]]
    current = [entry for entry in usable if entry.get("request_current") is True]
    skipped = [{"candidate_sha256": entry["candidate_sha256"], "attempt_path": entry["attempt_path"], "reason": "request differs from current pinned focused request; not a reconstruction basis"} for entry in usable if entry.get("request_current") is not True]
    return current, skipped


def destination_reconstruction(root, run, locale, candidate):
    """Per-file provenance of the current locale tree relative to a retained provider draft (read-only)."""
    planned = {name: value for name, value in _candidate_inventory(candidate).items() if value is not None}
    current = _inventory(root, f"{run}/{locale}") or {}
    empty = digest(b"")
    files = {}
    for name in FAMILY_FILES:
        actual = current.get(name)
        status = "missing" if actual is None else "matches_retained_draft" if actual == planned[name] else "empty_scaffold" if actual == empty and name.endswith("lesson.md") else "editor_or_other"
        files[name] = {"status": status, "sha256": actual, "draft_sha256": planned[name]}
    counts = {}
    for entry in files.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    try:
        family.family_locale_scaffold(root, run, locale)
        state = "pristine_scaffold"
    except core.AuthoringError:
        state = "complete" if set(current) - {f"stage-{stage}" for stage in family.STAGES} == set(FAMILY_FILES) else "partial"
    return {"destination_state": state, "counts": counts, "files": files, "unexpected": sorted(set(current) - set(FAMILY_FILES) - {f"stage-{stage}" for stage in family.STAGES})}


def _locale_items(root, run, locale, grant, timeout=120):
    _, scope, _ = _paths(root, run)
    context, evidence = _source(root, run, timeout)
    current_request = _request_summary(root, run, context, locale, evidence)["hash"]
    items = []
    lineages = ([(f"work/translations/{grant['scope']}/{locale}", grant["grant_id"])] if grant else []) + [(f"work/translations/{scope}/{locale}", None)]
    for relative, grant_id in lineages:
        path = core.safe_path(root, relative)
        if path.is_dir():
            before = _inventory(root, relative)
            for entry in retained_family_items(path):
                entry.update(lineage=relative, grant_id=grant_id, request_current=entry["request_sha256"] == current_request, attempt_path=Path(entry["attempt_path"]).resolve().relative_to(root).as_posix())
                items.append(entry)
            if _inventory(root, relative) != before:
                raise TranslationError("Provider evidence changed during extraction")
    return items, current_request, evidence


def offline_recovery_status(root, run, *, timeout=120):
    """Read-only per-locale route for offline_recovery locales. Per-locale problems are reported, never raised for the batch."""
    root = Path(root).resolve()
    try:
        plan = plan_translation_recovery(root, run, timeout=timeout)
    except (TranslationError, core.AuthoringError, OSError, KeyError, TypeError) as error:
        return {"run": run, "error": str(error), "offline_locales": {}}
    grant = _load_grant(root, run) if plan.get("active") else None
    dispositions = plan.get("current_dispositions") or {locale: entry["disposition"] for locale, entry in plan["locales"].items()}
    result = {}
    for locale, disposition in sorted(dispositions.items()):
        if disposition != "offline_recovery":
            continue
        try:
            items, _, _ = _locale_items(root, run, locale, grant, timeout)
            current, skipped = _alternatives(items)
            alternatives = [{**{key: value for key, value in entry.items() if key != "candidate"}, "reconstruction": destination_reconstruction(root, run, locale, entry["candidate"])} for entry in current]
            if len(current) == 1:
                step = "Extract the single current-request draft with --extract-retained; a bilingual editor completes all fifteen files (keeping existing editor files), then family_cli check and an independent locale review. No paid request."
            elif current:
                step = f"{len(current)} distinct retained versions: choose by validated target contract and linguistic review, then --extract-retained --item SHA_PREFIX; no automatic choice. No paid request."
            else:
                step = "No usable current-request retained family: an editor authors the complete locale offline from the source, then family_cli check and an independent locale review. No paid request."
            result[locale] = {"reason": (plan.get("current_reasons") or {}).get(locale, RETAINED_OUTPUT), "alternative_count": len(current), "alternatives": alternatives, "placeholders": sum(entry["placeholder"] for entry in items), "skipped": skipped, "next_step": step}
        except (TranslationError, core.AuthoringError, OSError, KeyError, TypeError, ValueError) as error:
            result[locale] = {"error": str(error), "next_step": "Inspect manually; no paid request."}
    return {"run": run, "grant_id": plan.get("grant_id"), "offline_locales": result}


def extract_retained_family(root, run, locale, output, *, item=None, timeout=120):
    """Write one retained provider family as an offline reconstruction draft plus an exact-hash provenance manifest.

    Read-only for provider evidence and locale trees. The draft is never adopted, approved or published here."""
    root = Path(root).resolve()
    study, scope, folder = _paths(root, run)
    if locale not in core.translation_locales(study):
        raise TranslationError("Recovery locale must exclude study and source")
    output = _draft_path(root, output)
    manifest_path = _draft_path(root, output.with_name(output.name + ".provenance.json"))
    if output.exists() or manifest_path.exists():
        raise TranslationError("Draft output already exists; refusing overwrite")
    context, _ = _source(root, run, timeout)
    grant = _load_grant(root, run)
    items, current_request, evidence = _locale_items(root, run, locale, grant, timeout)
    current, skipped = _alternatives(items)
    selected = [entry for entry in current if item is not None and entry["candidate_sha256"].startswith(item)] if item is not None else current
    if len(selected) != 1:
        raise TranslationError(f"{len(current)} current-request retained alternatives; " + ("--item must match exactly one" if item is not None else "select one with --item after contract/linguistic comparison (no automatic choice)") + f": {json.dumps([entry['candidate_sha256'] for entry in current])}")
    chosen = selected[0]
    draft = encode(chosen["candidate"])
    _new_draft(root, output, draft)
    try:
        return _write_manifest(root, run, locale, output, manifest_path, draft, chosen, items, current, skipped, grant, current_request, evidence, context, timeout)
    except BaseException:
        if not manifest_path.exists():
            output.unlink(missing_ok=True)
        raise


def _write_manifest(root, run, locale, output, manifest_path, draft, chosen, items, current, skipped, grant, current_request, evidence, context, timeout):
    summary = [{key: value for key, value in entry.items() if key != "candidate"} for entry in items]
    baseline = family.validate_family(root, run, context["source_locale"], timeout=timeout)["family_contract"]
    from . import translation_scheduler as scheduler
    process = scheduler.validate_process([os.environ.get("NODE_BINARY", "node"), str(root / "tools/validate-family.cjs"), "--root", str(root), "--run", run, "--locale", locale, "--candidate", str(output.resolve())], timeout)
    try:
        report = json.loads(process.stdout)
    except ValueError:
        report = {"ok": False, "issues": [{"msg": (process.stderr or "validator produced no JSON").strip()[-2000:]}]}
    try:
        validate_candidate(chosen["candidate"], reviewed_practice_stages=_practice(root, run))
        schema_error = None
    except TranslationError as error:
        schema_error = str(error)
    contract_equal = report.get("ok") is True and report.get("family_contract") == baseline
    manifest = {
        "schema": 1,
        "kind": "retained-provider-family-draft",
        "run": run,
        "locale": locale,
        "draft": {"path": str(output.resolve()), "sha256": digest(draft)},
        "selected": {key: value for key, value in chosen.items() if key != "candidate"},
        "retained_items": summary,
        "alternative_count": len(current),
        "skipped": skipped,
        "grant": {"grant_id": grant["grant_id"], "scope": grant["scope"]} if grant else None,
        "current_request_sha256": current_request,
        "source_evidence_sha256": digest(evidence),
        "source_evidence_current": grant is None or evidence == grant.get("source_evidence"),
        "destination": _inventory(root, f"{run}/{locale}"),
        "reconstruction": destination_reconstruction(root, run, locale, chosen["candidate"]),
        "validation": {"schema_error": schema_error, "validator_ok": report.get("ok") is True, "family_contract_equal": contract_equal, "contract_differences": [] if contract_equal or not report.get("ok") else _contract_differences(report.get("family_contract"), baseline), "issues": report.get("issues") or []},
        "provider_authored_text": True,
        "approved": False,
        "adopted": False,
        "use": "Offline reconstruction basis only: a bilingual editor must produce the complete locale tree, then deep validation and an independent locale review. Provider journals and status are unchanged; no paid request is implied.",
    }
    if _source(root, run, timeout)[1] != evidence:
        raise TranslationError("Source evidence changed during draft validation")
    current_items, request_now, _ = _locale_items(root, run, locale, grant, timeout)
    if request_now != current_request or not any(entry == chosen for entry in current_items):
        raise TranslationError("Selected provider evidence changed during draft validation")
    if _inventory(root, f"{run}/{locale}") != manifest["destination"] or core.digest(output) != digest(draft):
        raise TranslationError("Recovery destination or draft changed during validation")
    _new_draft(root, manifest_path, encode(manifest))
    return {**manifest, "manifest_path": str(manifest_path.resolve())}
