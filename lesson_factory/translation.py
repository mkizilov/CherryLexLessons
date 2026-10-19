import email.utils
import fcntl
import hashlib
import io
import math
import threading
import urllib.parse
import random
import socket
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


LOCALES = ("ar", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "pt", "ru", "zh")
RETRY_DELAYS = (120, 300, 1800)
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
ENDPOINT = "https://api.contactboxtools.me/v1/responses"
MODEL = "gpt-6.1-sol"
MAX_INPUT_BYTES = 524288
STAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "lesson_md": {"type": "string"},
        "quiz_md": {"type": "string"},
        "adaptation_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["lesson_md", "quiz_md", "adaptation_notes"],
    "additionalProperties": False,
}


class TranslationError(ValueError):
    pass


class GenerationDeadline(TimeoutError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise TranslationError("Contactbox redirect refused; permanent redirect")


def open_response(request, timeout, *, controller=None, idle_timeout=None):
    if controller is None:
        return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
    url = urllib.parse.urlsplit(request.full_url)
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=timeout)
    controller["connection"] = connection
    connection.connect()
    controller["check"]()
    connection.request(request.get_method(), urllib.parse.urlunsplit(("", "", url.path or "/", url.query, "")), request.data, dict(request.header_items()))
    response = connection.getresponse()
    controller["stream"] = response
    sock = getattr(connection, "sock", None) or getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None and idle_timeout is not None:
        sock.settimeout(idle_timeout)
    if response.status >= 300:
        raise urllib.error.HTTPError(request.full_url, response.status, response.reason, response.headers, response)
    return response


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encode(value)).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".translation-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def usage_report(usage):
    result = {"usage": usage, "estimated_cost_usd": None, "cost_basis": "unknown; not a provider bill"}
    if not isinstance(usage, dict):
        return result
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    values = (input_tokens, output_tokens, cached)
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values):
        return result
    if cached > input_tokens:
        return result
    result.update(input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached, reasoning_tokens=reasoning)
    result["estimated_cost_usd"] = ((input_tokens - cached) * 2 + cached * 0.1 + output_tokens * 10) / 1000000
    result["cost_basis"] = "estimated Sol USD: input 2/M, cached 0.1/M, output 10/M; not a provider bill"
    return result


FAMILY_SCHEMA = {
    "type": "object",
    "properties": {"stages": {"type": "array", "minItems": 5, "maxItems": 5, "items": {
        "type": "object",
        "properties": {"stage": {"type": "integer", "enum": [0, 1, 2, 3, 4]}, **STAGE_SCHEMA["properties"]},
        "required": ["stage", *STAGE_SCHEMA["required"]],
        "additionalProperties": False,
    }}},
    "required": ["stages"],
    "additionalProperties": False,
}


def candidate_policy(reviewed_practice_stages):
    if not isinstance(reviewed_practice_stages, (tuple, list)) or any(type(stage) is not int or stage not in (1, 2, 3, 4) for stage in reviewed_practice_stages) or len(set(reviewed_practice_stages)) != len(reviewed_practice_stages):
        raise TranslationError("reviewed_practice_stages must contain unique return stage integers")


def prepare(context, locale, prompt, max_input_bytes=MAX_INPUT_BYTES):
    if locale not in LOCALES:
        raise TranslationError("Unsupported learner locale")
    study = context.get("study_language")
    source = context.get("source_locale")
    if study not in LOCALES or source != ("es" if study == "en" else "en"):
        raise TranslationError("Pinned study/source locale policy is required")
    if locale in (study, source):
        raise TranslationError("Destination must differ from study and source locale")
    if not isinstance(max_input_bytes, int) or max_input_bytes <= 0:
        raise TranslationError("max_input_bytes must be positive")
    body = {
        "model": MODEL,
        "reasoning": {"effort": "xhigh"},
        "stream": True,
        "store": False,
        "input": [
            {"role": "developer", "content": prompt},
            {"role": "user", "content": encode({"learner_language": locale, "context": context}).decode("utf-8")},
        ],
        "text": {"format": {"type": "json_schema", "name": "localized_family", "strict": True, "schema": FAMILY_SCHEMA}},
    }
    payload = encode(body)
    if len(payload) > max_input_bytes:
        raise TranslationError(f"Complete input is {len(payload)} UTF-8 bytes, above {max_input_bytes}; nothing truncated; no output cap")
    return body, {"bytes": len(payload), "model": MODEL, "effort": "xhigh", "target": locale, "hash": digest(payload), "output_cap": None}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TranslationError("Duplicate JSON property")
        result[key] = value
    return result


def parse_candidate(response, *, reviewed_practice_stages=()):
    candidate_policy(reviewed_practice_stages)
    if not isinstance(response, dict) or response.get("status") != "completed" or response.get("error"):
        raise TranslationError("Provider response is not completed")
    messages = []
    output = response.get("output")
    if not isinstance(output, list):
        raise TranslationError("Missing response output")
    for item in output:
        if not isinstance(item, dict):
            raise TranslationError("Malformed output item")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role", "assistant") != "assistant" or not isinstance(item.get("content"), list):
            raise TranslationError("Unexpected output item")
        texts = []
        for content in item["content"]:
            if not isinstance(content, dict) or content.get("type") != "output_text" or not isinstance(content.get("text"), str):
                raise TranslationError("Unexpected extracted content")
            texts.append(content["text"])
        text = "".join(texts)
        if text.strip():
            messages.append(text)
    if not messages:
        raise TranslationError("Missing output text")
    # Message items are independent outputs, not JSON chunks. Only content
    # parts within one item are concatenated. A provider may emit an exact
    # empty skeleton before the real family; ignore only that known no-content
    # shape. Any other malformed/competing message requires offline triage.
    candidates = {}
    for text in messages:
        try:
            candidate = json.loads(text, object_pairs_hook=_unique_object)
        except (ValueError, TypeError) as error:
            raise TranslationError("Malformed localization JSON; inspect retained message items offline") from error
        if _empty_family_skeleton(candidate):
            continue
        validate_candidate(candidate, reviewed_practice_stages=reviewed_practice_stages)
        candidates[digest(candidate)] = candidate
    if len(candidates) != 1:
        raise TranslationError("Missing or ambiguous family message items; offline review required")
    return next(iter(candidates.values()))


def validate_candidate(candidate, *, reviewed_practice_stages=()):
    candidate_policy(reviewed_practice_stages)
    if not isinstance(candidate, dict) or set(candidate) != {"stages"} or not isinstance(candidate["stages"], list) or len(candidate["stages"]) != 5:
        raise TranslationError("Family localization must contain exactly five ordered stages")
    for stage, artifact in enumerate(candidate["stages"]):
        if not isinstance(artifact, dict) or set(artifact) != {"stage", *STAGE_SCHEMA["required"]} or type(artifact["stage"]) is not int or artifact["stage"] != stage:
            raise TranslationError("Family stage identity/order/fields mismatch")
        if not isinstance(artifact["lesson_md"], str) or not isinstance(artifact["quiz_md"], str) or not artifact["quiz_md"].strip():
            raise TranslationError("Lesson must be a string and quiz must be a nonempty string")
        if stage in reviewed_practice_stages:
            if artifact["lesson_md"].strip():
                raise TranslationError("Practice-only localization must have an empty lesson")
        elif not artifact["lesson_md"].strip():
            raise TranslationError("Lesson and quiz must be nonempty strings")
        if not isinstance(artifact["adaptation_notes"], list) or not all(isinstance(note, str) for note in artifact["adaptation_notes"]):
            raise TranslationError("adaptation_notes must be an array of strings")
    return candidate


def _safe_headers(headers):
    return {name: "[redacted]" if name.lower() in ("authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key") or "bearer " in str(value).lower() else value for name, value in dict(headers or {}).items()}


def retry_after_seconds(value, now):
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else None
    except (ValueError, TypeError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return max(0, parsed.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return None


def _permanent_provider(error):
    detail = encode(error).decode("utf-8").lower()
    return any(term in detail for term in ("auth", "api_key", "billing", "quota", "credit", "invalid", "context", "content_filter", "refusal", "max_output", "token_limit", "permission_denied"))


def classify_provider(response):
    error = response.get("error") or response.get("incomplete_details") or {}
    code = str(error.get("code") or error.get("type") or error.get("reason") or "provider_incomplete") if isinstance(error, dict) else "provider_incomplete"
    detail = encode(error).decode("utf-8").lower()
    transient = ("server_error", "rate_limit", "overloaded", "temporarily_unavailable", "upstream", "transport", "stream_error", "connection", "timeout")
    eligible = not _permanent_provider(error) and any(term in detail for term in transient)
    return code, eligible


def _failure(record, code, eligible, now, retry_after=None, jitter=None, retry_policy=None):
    attempt = record["attempt"]
    maximum = retry_policy["max_attempts"] if retry_policy is not None else len(RETRY_DELAYS) + 1
    eligible = eligible and attempt < maximum
    delay = (1800 if retry_policy is not None or code == "max_output_tokens" else RETRY_DELAYS[attempt - 1]) if eligible else None
    extra = (jitter or (lambda: random.uniform(0.01, 5)))() if eligible else 0
    if not isinstance(extra, (float, int)) or not math.isfinite(extra) or not 0 < extra <= 5:
        extra = 0.01
    record.update(error_code=code, retry_eligible=eligible, retry_after=retry_after, retry_at=now + max(delay, retry_after or 0) + extra if eligible else None, billing_outcome="unknown")
    if retry_policy is not None:
        record["retry_scheduled_at"] = now if eligible else None


@contextmanager
def _attempt_lock(root, summary, scope):
    directory = Path(root).resolve() / "work" / "translations" / scope / summary["target"]
    if not scope or any(part in ("..", "") for part in scope.split("/")) or Path(scope).is_absolute() or summary["target"] not in LOCALES:
        raise TranslationError("Invalid journal scope or locale")
    boundary = Path(root).resolve()
    for component in (directory, *directory.parents):
        if component == boundary:
            break
        if component.is_symlink():
            raise TranslationError("Symlink journal path refused")
    if directory.exists() and any(path.is_symlink() for path in directory.rglob("*")):
        raise TranslationError("Symlink attempt evidence refused")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "attempt.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TranslationError("Translation attempt is already in flight") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _chunks(stream, check=None):
    reader = getattr(stream, "read1", None)
    if reader is None or (isinstance(stream, io.BytesIO) and "__iter__" in type(stream).__dict__):
        yield from stream
        return
    while True:
        if check is not None:
            check()
        chunk = reader(4096)
        if not chunk:
            if check is not None:
                check()
            return
        yield chunk


def read_stream(stream, capture, *, persist=None, check=None):
    buffer = []
    items = {}
    fragments = {}
    placeholder_ids = set()

    def event(lines):
        if not lines:
            return None
        payload = b"\n".join(lines).decode("utf-8")
        if payload.strip() == "[DONE]":
            return None
        value = json.loads(payload, object_pairs_hook=_unique_object)
        kind = value.get("type")
        if value.get("sub2api_placeholder"):
            placeholder_ids.add(value.get("item_id") or (value.get("item") or {}).get("id"))
            return None
        if kind == "response.output_item.done":
            items[value.get("output_index", len(items))] = value["item"]
        elif kind == "response.output_text.delta":
            key = (value.get("output_index", 0), value.get("content_index", 0), value.get("item_id"))
            fragments.setdefault(key, []).append(value.get("delta", ""))
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            response = value.get("response")
            if not isinstance(response, dict):
                raise TranslationError("Missing terminal response")
            response = dict(response)
            if kind != "response.completed":
                response["status"] = kind.removeprefix("response.")
            if not response.get("output") and response.get("status") == "completed":
                if items:
                    response["output"] = [item for _, item in sorted(items.items()) if item.get("id") not in placeholder_ids]
                else:
                    response["output"] = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "".join(parts)}]} for key, parts in sorted(fragments.items(), key=lambda pair: (pair[0][0], pair[0][1])) if key[2] not in placeholder_ids]
            return response
        elif kind in ("error", "response.error"):
            return {"status": "failed", "error": value.get("error") or value, "usage": value.get("usage")}
        return None

    pending = b""
    for chunk in _chunks(stream, check):
        capture.extend(chunk)
        if persist is not None:
            persist(chunk)
        if check is not None:
            check()
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            stripped = line.rstrip(b"\r")
            if stripped.startswith(b"data:"):
                data = stripped[5:]
                buffer.append(data[1:] if data.startswith(b" ") else data)
            elif not stripped:
                final = event(buffer)
                buffer = []
                if final is not None:
                    return final
        if check is not None:
            check()
    if pending.startswith(b"data:"):
        buffer.append(pending[5:].lstrip(b" "))
    final = event(buffer)
    if final is not None:
        return final
    raise TranslationError("EOF before terminal response; dispatch outcome uncertain")


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _retry_policy_data(value):
    if not isinstance(value, Mapping) or set(value) != {"max_attempts", "appended_delays_seconds", "extension_receipt"}:
        raise TranslationError("Invalid slow retry policy contract")
    value = dict(value)
    if type(value["max_attempts"]) is not int or not 5 <= value["max_attempts"] <= 8 or value["appended_delays_seconds"] != [1800, 1800, 1800, 1800] or any(type(delay) is not int for delay in value["appended_delays_seconds"]):
        raise TranslationError("Slow retry policy permits at most eight attempts with 1800-second delays")
    receipt = value["extension_receipt"]
    fields = {"schema", "scope", "locale", "request_hash", "prior_attempt", "next_attempt", "prior_error_code", "prior_outcome_fingerprint", "earliest_due", "operator_reason", "grant_sha256", "created_at"}
    if not isinstance(receipt, dict) or set(receipt) != fields or type(receipt.get("schema")) is not int or receipt["schema"] != 1:
        raise TranslationError("Invalid slow retry extension receipt")
    if type(receipt["prior_attempt"]) is not int or not 1 <= receipt["prior_attempt"] <= 4 or type(receipt["next_attempt"]) is not int or receipt["next_attempt"] != receipt["prior_attempt"] + 1:
        raise TranslationError("Slow retry receipt must extend the existing four-attempt lineage")
    scope = receipt["scope"]
    if not isinstance(scope, str) or not scope or Path(scope).is_absolute() or any(part in ("", ".", "..") for part in scope.split("/")) or receipt["locale"] not in LOCALES:
        raise TranslationError("Invalid slow retry receipt scope or locale")
    for name in ("request_hash", "prior_outcome_fingerprint", "grant_sha256"):
        token = receipt[name]
        if not isinstance(token, str) or len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
            raise TranslationError("Invalid slow retry receipt evidence digest")
    if any(not isinstance(receipt[name], str) or not receipt[name].strip() for name in ("operator_reason", "prior_error_code")):
        raise TranslationError("Slow retry receipt requires operator reason and prior error")
    if not _finite_number(receipt["created_at"]) or not _finite_number(receipt["earliest_due"]) or receipt["created_at"] < 0 or receipt["earliest_due"] <= receipt["created_at"] + 1800:
        raise TranslationError("Slow retry receipt requires fixed 1800-second minimum plus jitter")
    return value


@dataclass(frozen=True, init=False)
class RetryPolicy(Mapping):
    _encoded: bytes
    _validate_receipt: Callable

    def __init__(self, value, *, validate_receipt):
        if not callable(validate_receipt):
            raise TranslationError("Slow retry policy requires an operator receipt validator")
        object.__setattr__(self, "_encoded", encode(_retry_policy_data(value)))
        object.__setattr__(self, "_validate_receipt", validate_receipt)
        self.validate()

    def __getitem__(self, key):
        return json.loads(self._encoded)[key]

    def __iter__(self):
        return iter(json.loads(self._encoded))

    def __len__(self):
        return len(json.loads(self._encoded))

    def validate(self):
        if encode(_retry_policy_data(self._validate_receipt())) != self._encoded:
            raise TranslationError("Validated slow retry operator receipt changed")


def _empty_output(output):
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, dict):
            return False
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role", "assistant") != "assistant" or not isinstance(item.get("content"), list):
            return False
        for content in item["content"]:
            if not isinstance(content, dict) or content.get("type") != "output_text" or not isinstance(content.get("text"), str) or content["text"].strip():
                return False
    return True


def _empty_event(value):
    if not isinstance(value, dict):
        return False
    kind = value.get("type", "")
    if kind in ("codex.rate_limits", "codex.response.metadata"):
        return _metadata_only(value)
    if "refusal" in str(kind):
        return False
    if "response" in value:
        response = value["response"]
        if not isinstance(response, dict) or not _empty_output(response.get("output", [])) or response.get("status") in ("completed", "incomplete") and "output" not in response or _permanent_provider(response.get("error") or {}):
            return False
        details = response.get("incomplete_details") or {}
        if details != {"reason": "max_output_tokens"} and _permanent_provider(details):
            return False
    if value.get("error") and _permanent_provider(value["error"]):
        return False
    if kind in ("response.output_item.added", "response.output_item.done"):
        return _empty_output([value.get("item")])
    if kind in ("response.content_part.added", "response.content_part.done"):
        return _empty_output([{"type": "message", "content": [value.get("part")]}])
    if kind in ("response.output_text.delta", "response.output_text.done"):
        text = value.get("delta" if kind.endswith("delta") else "text")
        return isinstance(text, str) and not text.strip()
    if isinstance(kind, str) and kind.startswith(("response.reasoning_", "response.reasoning.")):
        return True
    return kind in ("response.created", "response.queued", "response.in_progress", "response.completed", "response.incomplete", "response.failed", "error", "response.error", "ping") and not any(name in value for name in ("text", "delta", "item", "part", "content", "output", "refusal"))


def _empty_capture(raw, *, http_error=False):
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeError:
        return False
    if http_error and not any(line.startswith("data:") for line in text.splitlines()):
        try:
            value = json.loads(text, object_pairs_hook=_unique_object)
        except ValueError:
            return not text.strip()
        return isinstance(value, dict) and set(value) <= {"error", "usage", "status", "id", "request_id", "output", "incomplete_details"} and not _permanent_provider(value.get("error") or value.get("incomplete_details") or {}) and value.get("status") != "completed" and _empty_output(value.get("output", []))
    lines = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
        elif not line:
            if lines:
                payload = "\n".join(lines)
                lines = []
                if payload.strip() == "[DONE]":
                    continue
                try:
                    value = json.loads(payload, object_pairs_hook=_unique_object)
                except ValueError:
                    return False
                if not _empty_event(value):
                    return False
        elif not line.startswith((":", "event:", "id:", "retry:")):
            return False
    return True


def slow_retry_reason(response, raw):
    if not isinstance(response, dict) or response.get("error") or not _empty_output(response.get("output")) or not _empty_capture(raw):
        return None
    if response.get("status") == "completed" and not response.get("incomplete_details"):
        return "provider_completed_empty"
    if response.get("status") == "incomplete" and response.get("incomplete_details") == {"reason": "max_output_tokens"}:
        return "provider_max_output_tokens_empty"
    return None


def _metadata_only(value):
    if isinstance(value, dict):
        return not any(key in {"output", "text", "delta", "content", "item", "part", "refusal", "error", "response", "incomplete_details"} for key in value) and all(_metadata_only(item) for item in value.values())
    if isinstance(value, list):
        return all(_metadata_only(item) for item in value)
    return True


def _empty_family_skeleton(candidate):
    if not isinstance(candidate, dict) or set(candidate) != {"stages"} or not isinstance(candidate["stages"], list) or len(candidate["stages"]) != 5:
        return False
    stages = candidate["stages"]
    return all(isinstance(stage, dict) and set(stage) == {"stage", "lesson_md", "quiz_md", "adaptation_notes"} and type(stage["stage"]) is int and stage["lesson_md"] == "" and stage["quiz_md"] == "" and stage["adaptation_notes"] == [] for stage in stages) and {stage["stage"] for stage in stages} == set(range(5))


def _incomplete_json_text(text):
    if not isinstance(text, str):
        return False
    value = text.strip()
    if not value or value in ('"', "'", "-", "."):
        return True
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].lstrip()
    position = min((value.index(character) for character in ("{", "[") if character in value), default=0)
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    try:
        candidate, end = decoder.raw_decode(value[position:])
    except TranslationError:
        return False
    except ValueError:
        for offset, character in enumerate(value):
            if character != "{" or offset == position:
                continue
            try:
                candidate, _ = decoder.raw_decode(value[offset:])
            except TranslationError:
                return False
            except ValueError:
                continue
            if isinstance(candidate, dict) and isinstance(candidate.get("stages"), list) and len(candidate["stages"]) == 5 and not _empty_family_skeleton(candidate):
                return False
        return True
    return _empty_family_skeleton(candidate) and _incomplete_json_text(value[position + end:])


def _partial_output_texts(output):
    if not isinstance(output, list):
        raise TranslationError("Unknown retained partial output")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise TranslationError("Unknown retained partial output item")
        if item.get("type") == "reasoning":
            if any(key in item for key in ("refusal", "error", "output", "text")) or not isinstance(item.get("content", []), list) or any(not isinstance(part, dict) or part.get("type") != "reasoning_text" or not isinstance(part.get("text"), str) for part in item.get("content", [])):
                raise TranslationError("Ambiguous reasoning output")
            continue
        if item.get("type") != "message" or item.get("role", "assistant") != "assistant" or not isinstance(item.get("content"), list) or any(key in item for key in ("refusal", "error", "output", "text")):
            raise TranslationError("Unknown retained partial message")
        for part in item["content"]:
            if not isinstance(part, dict) or part.get("type") != "output_text" or not _incomplete_json_text(part.get("text")) or any(key in part for key in ("refusal", "error", "output", "content")):
                raise TranslationError("Completed or ambiguous retained partial text")
            texts.append(part["text"])
    if not _incomplete_json_text("".join(texts)):
        raise TranslationError("Completed retained partial output")
    return texts


def _partial_failure_code(response):
    if not isinstance(response, dict):
        return None
    status = response.get("status")
    error = response.get("error") or {}
    details = response.get("incomplete_details") or {}
    if response.get("refusal") or "capacity" in encode({"error": error, "details": details}).decode().lower():
        return None
    if status == "incomplete" and not error and details == {"reason": "max_output_tokens"} and isinstance(response.get("output"), list):
        return "max_output_tokens"
    if status != "failed" or _permanent_provider(error) or _permanent_provider(details) or "capacity" in encode({"error": error, "details": details}).decode().lower():
        return None
    code, eligible = classify_provider(response)
    return code if eligible else None


def _partial_retry_reason(response, raw):
    code = _partial_failure_code(response)
    if code is None:
        return None
    try:
        _partial_output_texts(response.get("output", []))
        events = []
        lines = []
        for line in [*bytes(raw).decode("utf-8").splitlines(), ""]:
            if line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))
            elif not line:
                if lines:
                    payload = "\n".join(lines)
                    lines = []
                    if payload.strip() != "[DONE]":
                        events.append(json.loads(payload, object_pairs_hook=_unique_object))
            elif not line.startswith((":", "event:", "id:", "retry:")):
                return None
        fragments = {}
        final_texts = {}
        identities = {}
        terminal = False
        reasoning_events = {"response.reasoning_summary_part.added", "response.reasoning_summary_part.done", "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done", "response.reasoning_text.delta", "response.reasoning_text.done"}
        for event in events:
            if not isinstance(event, dict):
                return None
            kind = event.get("type")
            if kind in ("codex.rate_limits", "codex.response.metadata", "ping"):
                if not _metadata_only(event):
                    return None
                continue
            if terminal or not isinstance(kind, str) or "refusal" in kind:
                return None
            if kind in ("response.failed", "response.incomplete", "error", "response.error"):
                captured = {"status": "failed", "error": event.get("error") or event, "usage": event.get("usage")} if kind in ("error", "response.error") else event.get("response")
                expected_status = "failed" if kind in ("error", "response.error") else kind.removeprefix("response.")
                if not isinstance(captured, dict) or captured.get("status") != expected_status or _partial_failure_code(captured) != code or encode(captured) != encode(response):
                    return None
                extra_error = {key: value for key, value in event.items() if key in ("error", "code", "message", "incomplete_details")}
                if _permanent_provider(encode(extra_error).decode().replace("max_output_tokens", "") if kind == "response.incomplete" else extra_error):
                    return None
                _partial_output_texts(captured.get("output", []))
                terminal = True
                continue
            if kind in ("response.created", "response.queued", "response.in_progress"):
                envelope = event.get("response")
                if not isinstance(envelope, dict) or envelope.get("status") not in ("queued", "in_progress") or envelope.get("error") or envelope.get("incomplete_details"):
                    return None
                _partial_output_texts(envelope.get("output", []))
                continue
            if kind in reasoning_events:
                if any(name in event for name in ("output", "item", "refusal", "error", "response")):
                    return None
                continue
            index = event.get("output_index", 0)
            content = event.get("content_index", 0)
            if type(index) is not int or index < 0 or type(content) is not int or content < 0 or any(name in event for name in ("response", "refusal", "error", "output")):
                return None
            item_id = event.get("item_id") or (event.get("item") or {}).get("id")
            if item_id is not None:
                if not isinstance(item_id, str) or index in identities and identities[index] != item_id:
                    return None
                identities[index] = item_id
            if kind in ("response.output_text.delta", "response.output_text.done"):
                text = event.get("delta" if kind.endswith("delta") else "text")
                if not isinstance(text, str):
                    return None
                if kind.endswith("delta"):
                    key = (index, content)
                    fragments[key] = fragments.get(key, "") + text
                    if not _incomplete_json_text(fragments[key]) or not _incomplete_json_text("".join(fragments[key] for key in sorted(fragments))):
                        return None
                else:
                    if not _incomplete_json_text(text):
                        return None
                    key = (index, content)
                    if key in final_texts and final_texts[key] != text:
                        return None
                    final_texts[key] = text
            elif kind in ("response.output_item.added", "response.output_item.done"):
                item = event.get("item")
                _partial_output_texts([item])
                if kind.endswith("done") and item.get("type") == "message":
                    for part_index, part in enumerate(item["content"]):
                        key = (index, part_index)
                        if key in final_texts and final_texts[key] != part["text"]:
                            return None
                        final_texts[key] = part["text"]
            elif kind in ("response.content_part.added", "response.content_part.done"):
                part = event.get("part")
                _partial_output_texts([{"type": "message", "content": [part]}])
                if kind.endswith("done"):
                    key = (index, content)
                    if key in final_texts and final_texts[key] != part["text"]:
                        return None
                    final_texts[key] = part["text"]
            else:
                return None
            if any(key in fragments and not text.startswith(fragments[key]) for key, text in final_texts.items()):
                return None
            canonical = {**fragments, **final_texts}
            if not _incomplete_json_text("".join(canonical[key] for key in sorted(canonical))):
                return None
        return code if terminal else None
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return None


def retained_retry_eligible(record, response, raw):
    if record.get("state") in ("completed", "pending", "permanent_error") or record.get("error_code") in ("cancelled", "generation_deadline") or record.get("raw_missing_before_recovery") and response is not None:
        return False
    if slow_retry_reason(response, raw):
        return record.get("state") in ("provider_failed", "rejected", "uncertain")
    if record.get("state") == "provider_failed" and _partial_retry_reason(response, raw):
        return True
    if record.get("state") == "rejected" or not _empty_capture(raw, http_error=record.get("state") == "http_error"):
        return False
    if isinstance(response, dict):
        if response.get("status") == "completed" or not _empty_output(response.get("output", [])) or _permanent_provider(response.get("error") or response.get("incomplete_details") or {}):
            return False
        if record.get("state") == "provider_failed":
            return classify_provider(response)[1]
    if record.get("state") == "http_error":
        return record.get("http_status") in TRANSIENT_HTTP
    return record.get("state") == "uncertain" and record.get("error_code") in ("socket_timeout", "remote_eof", "interrupted_attempt")


def retry_outcome_fingerprint(journal, prior_attempt):
    folder = Path(journal).parent / f"attempt-{prior_attempt}"
    for path in (folder, *folder.parents):
        if path.is_symlink():
            raise TranslationError("Symlink retry evidence refused")
        if path.name == "work":
            break
    if any(path.is_symlink() for path in folder.rglob("*")):
        raise TranslationError("Symlink retry evidence refused")
    record = _load(folder / "journal.json")
    files = {}
    for name in ("request.json", "summary.json", "raw.sse", "response.json", "response-headers.json", "http.json", "raw-candidate.json", "candidate.json", "repaired-candidate.json", "repair.json"):
        path = folder / name
        if name in ("request.json", "summary.json", "raw.sse") and not path.is_file():
            raise TranslationError("Missing retained slow retry evidence")
        files[name] = digest(path.read_bytes()) if path.exists() else None
    return digest({"journal": {key: value for key, value in record.items() if key != "retry_policy"}, "files": files})


def retry_status(journal, retry_policy=None):
    journal = Path(journal)
    previous = _load(journal) if journal.exists() else {}
    if previous.get("state") == "completed":
        return {"retry_eligible": False, "retry_at": None, "max_attempts": 4}
    if retry_policy is None:
        if previous.get("retry_policy") is not None and previous.get("state") != "completed":
            raise TranslationError("Existing slow retry lineage requires its validated operator receipt")
        return {"retry_eligible": bool(previous.get("retry_eligible")), "retry_at": previous.get("retry_at"), "max_attempts": 4}
    if not isinstance(retry_policy, RetryPolicy):
        raise TranslationError("Slow retry policy must carry a live operator receipt validator")
    retry_policy.validate()
    receipt = retry_policy["extension_receipt"]
    scope = receipt["scope"]
    expected_parts = ("work", "translations", *scope.split("/"), receipt["locale"], "journal.json")
    if tuple(journal.parts[-len(expected_parts):]) != expected_parts or journal.is_symlink():
        raise TranslationError("Slow retry receipt journal scope changed")
    count = previous.get("attempt")
    anchor = receipt["prior_attempt"]
    if type(count) is not int or not anchor <= count <= retry_policy["max_attempts"] or previous.get("input_hash") != receipt["request_hash"] or previous.get("target") != receipt["locale"]:
        raise TranslationError("Slow retry receipt request or attempt identity changed")
    if retry_outcome_fingerprint(journal, anchor) != receipt["prior_outcome_fingerprint"]:
        raise TranslationError("Slow retry receipt prior outcome evidence changed")
    anchor_record = _load(journal.parent / f"attempt-{anchor}" / "journal.json")
    if anchor_record.get("error_code") != receipt["prior_error_code"]:
        raise TranslationError("Slow retry receipt prior error changed")
    existing = {path.name for path in journal.parent.glob("attempt-*")}
    if existing != {f"attempt-{number}" for number in range(1, count + 1)}:
        raise TranslationError("Missing or orphan slow retry attempts; refusing repurchase")
    for number in range(1, count + 1):
        folder = journal.parent / f"attempt-{number}"
        if any(path.is_symlink() for path in folder.rglob("*")) or folder.is_symlink():
            raise TranslationError("Symlink retry evidence refused")
        request = (folder / "request.json").read_bytes()
        summary = _load(folder / "summary.json")
        retained = _load(folder / "journal.json")
        if digest(request) != receipt["request_hash"] or summary.get("hash") != receipt["request_hash"] or summary.get("target") != receipt["locale"] or summary.get("bytes") != len(request) or retained.get("input_hash") != receipt["request_hash"] or retained.get("attempt") != number or retained.get("target") != receipt["locale"]:
            raise TranslationError("Slow retry lineage evidence changed")
        if number > anchor and retained.get("retry_policy") != dict(retry_policy):
            raise TranslationError("Persisted slow retry policy changed")
        if number == count and retained != previous:
            raise TranslationError("Slow retry top-level and attempt journals differ")
        raw_path = folder / "raw.sse"
        if not raw_path.is_file() and not (number == count and number > anchor and retained.get("state") == "pending" and not raw_path.exists()):
            raise TranslationError("Missing retained slow retry raw evidence")
        response_path = folder / "response.json"
        response = _load(response_path) if response_path.exists() else None
        raw_candidate = folder / "raw-candidate.json"
        if (folder / "candidate.json").exists() or (folder / "repaired-candidate.json").exists() or raw_candidate.exists() and not _empty_output(_load(raw_candidate)) or number < count and not retained_retry_eligible(retained, response, (folder / "raw.sse").read_bytes()):
            return {"retry_eligible": False, "retry_at": None, "max_attempts": retry_policy["max_attempts"]}
    if previous.get("state") == "pending" and count > anchor:
        return {"retry_eligible": False, "retry_at": None, "max_attempts": retry_policy["max_attempts"]}
    folder = journal.parent / f"attempt-{count}"
    response_path = folder / "response.json"
    response = _load(response_path) if response_path.exists() else None
    eligible = retained_retry_eligible(previous, response, (folder / "raw.sse").read_bytes()) and not (journal.parent / "candidate.json").exists() and count < retry_policy["max_attempts"]
    due = receipt["earliest_due"] if count == anchor else previous.get("retry_at")
    if eligible and (not _finite_number(due) or count > anchor and not previous.get("retry_eligible")):
        raise TranslationError("Slow retry missing persistent eligibility or due time")
    if eligible and count > anchor:
        scheduled = previous.get("retry_scheduled_at")
        after = previous.get("retry_after")
        if not _finite_number(scheduled) or due <= scheduled + max(1800, after if _finite_number(after) else 0):
            raise TranslationError("Slow retry due time shortens the persistent minimum delay")
    if count == anchor:
        headers_path = folder / "response-headers.json"
        headers = _load(headers_path) if headers_path.exists() else {}
        after = retry_after_seconds(next((value for name, value in headers.items() if name.lower() == "retry-after"), None), receipt["created_at"])
        recorded_after = previous.get("retry_after")
        minimum = max(1800, after or 0, recorded_after if _finite_number(recorded_after) else 0)
        recorded_due = previous.get("retry_at")
        if due <= receipt["created_at"] + minimum or _finite_number(recorded_due) and due < recorded_due:
            raise TranslationError("Slow retry receipt shortens Retry-After or persistent due time")
    return {"retry_eligible": eligible, "retry_at": due, "max_attempts": retry_policy["max_attempts"]}


def execute(root, body, summary, validate, *, resume=False, retry=False, repair_candidate=None, opener=None, timeout=180, scope="default", gate=None, clock=time.time, jitter=None, wall_timeout=900, stop_event=None, reviewed_practice_stages=(), retry_policy=None):
    candidate_policy(reviewed_practice_stages)
    if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
        raise TranslationError("Slow retry policy must carry a live operator receipt validator")
    with _attempt_lock(root, summary, scope):
        return _execute(root, body, summary, validate, resume=resume, retry=retry, repair_candidate=repair_candidate, opener=opener, timeout=timeout, scope=scope, gate=gate, clock=clock, jitter=jitter, wall_timeout=wall_timeout, stop_event=stop_event, reviewed_practice_stages=reviewed_practice_stages, retry_policy=retry_policy)


def _execute(root, body, summary, validate, *, resume, retry, repair_candidate, opener, timeout, scope, gate, clock, jitter, wall_timeout, stop_event, reviewed_practice_stages, retry_policy):
    started = time.monotonic()
    root = Path(root)
    if body.get("text", {}).get("format", {}).get("schema") != FAMILY_SCHEMA:
        raise TranslationError("Family execution requires guarded family schema and explicit practice stages")
    locale = summary["target"]
    if not scope or any(part in ("..", "") for part in scope.split("/")) or Path(scope).is_absolute():
        raise TranslationError("Invalid journal scope")
    if digest(encode(body)) != summary.get("hash") or len(encode(body)) != summary.get("bytes"):
        raise TranslationError("Request changed after input guard")
    if any(name in body for name in ("max_output_tokens", "max_tokens", "max_completion_tokens", "output_token_limit", "truncation")):
        raise TranslationError("Output caps and automatic truncation are forbidden")
    if gate is None:
            raise TranslationError("Source approval/context gate is required before credentials/network")
    gate()
    journal_dir = root / "work" / "translations" / scope / locale
    journal_path = journal_dir / "journal.json"
    previous = _load(journal_path) if journal_path.exists() else None
    if previous and previous.get("reviewed_practice_stages") != list(reviewed_practice_stages):
        raise TranslationError("Family reviewed practice policy changed")
    if previous is None and (any(journal_dir.glob("attempt-*")) or (journal_dir / "candidate.json").exists()):
        raise TranslationError("Missing top-level journal with retained attempt evidence; refusing repurchase")
    if previous:
        if previous["input_hash"] != summary["hash"]:
            raise TranslationError("Source/context inputs changed; previous localization is stale")
        saved_dir = journal_dir / f"attempt-{previous['attempt']}"
        if digest((saved_dir / "request.json").read_bytes()) != summary["hash"] or _load(saved_dir / "summary.json") != summary:
            raise TranslationError("Prior request evidence changed; refusing resend or adoption")
    destination = journal_dir / "candidate.json"
    if repair_candidate is not None:
        if resume or retry:
            raise TranslationError("Repair cannot be combined with resume/retry")
        if not previous or previous.get("state") != "rejected" or previous.get("input_hash") != summary["hash"]:
            raise TranslationError("Repair requires a rejected completed response with unchanged inputs")
        log_dir = journal_dir / f"attempt-{previous['attempt']}"
        final = _load(log_dir / "response.json")
        if final.get("status") != "completed" or final.get("error"):
            raise TranslationError("Repair requires a completed provider response without error")
        repaired_bytes = Path(repair_candidate).read_bytes()
        candidate = validate_candidate(json.loads(repaired_bytes, object_pairs_hook=_unique_object), reviewed_practice_stages=reviewed_practice_stages)
        validation = validate(candidate)
        if validation is not True and not (isinstance(validation, dict) and validation.get("ok") is True):
            raise TranslationError("Structural validation rejected localization")
        gate()
        atomic_write(log_dir / "repaired-candidate.json", repaired_bytes)
        provenance = {"source": str(Path(repair_candidate).resolve()), "hash": digest(repaired_bytes), "input_hash": summary["hash"], "response_id": final.get("id"), "repaired_at": time.time()}
        atomic_write(log_dir / "repair.json", encode(provenance))
        atomic_write(destination, encode(candidate))
        previous.update(state="completed", output_hash=digest(destination.read_bytes()), repair=provenance)
        atomic_write(log_dir / "journal.json", encode(previous))
        atomic_write(journal_path, encode(previous))
        return previous
    policy_status = retry_status(journal_path, retry_policy) if retry_policy is not None or previous is not None else None
    if previous:
        if previous["state"] == "rejected" and not (policy_status and policy_status["retry_eligible"]):
            raise TranslationError("Validation rejected candidate; no automatic retry; use offline repair")
        if previous["state"] == "completed" and not destination.exists():
            raise TranslationError("Completed localization missing; refusing regeneration")
        if previous["state"] == "pending":
            saved_response = journal_dir / f"attempt-{previous['attempt']}" / "response.json"
            recovered = _load(saved_response) if saved_response.exists() else None
            evidence_dir = saved_response.parent
            raw_path = evidence_dir / "raw.sse"
            headers_path = evidence_dir / "response-headers.json"
            metadata_path = evidence_dir / "http.json"
            headers = _load(headers_path) if headers_path.exists() else {}
            metadata = _load(metadata_path) if metadata_path.exists() else {}
            status = metadata.get("status") or previous.get("http_status")
            if recovered is None and raw_path.exists():
                wire = raw_path.read_bytes()
                try:
                    recovered = read_stream(io.BytesIO(wire), bytearray())
                except (ValueError, UnicodeError, TypeError, KeyError):
                    try:
                        recovered = json.loads(wire)
                    except (ValueError, UnicodeError):
                        recovered = None
                if isinstance(recovered, dict):
                    atomic_write(saved_response, encode(recovered))
            if isinstance(recovered, dict):
                previous.update(response_id=recovered.get("id"), provider_status=recovered.get("status"), **usage_report(recovered.get("usage")))
            else:
                previous.update(**usage_report(None))
            completed = isinstance(recovered, dict) and recovered.get("status") == "completed" and not recovered.get("error")
            previous["state"] = "rejected" if completed else "provider_failed" if recovered is not None else "uncertain"
            code, eligible = ("completed_before_interruption", False) if completed else classify_provider(recovered) if isinstance(recovered, dict) else ("interrupted_attempt", True)
            if status and not completed:
                status = int(status)
                previous.update(state="http_error", http_status=status)
                code, eligible = f"http_{status}", status in TRANSIENT_HTTP
                if isinstance(recovered, dict) and _permanent_provider(recovered.get("error") or {}):
                    code, eligible = classify_provider(recovered)
            if retry_policy is not None:
                if not raw_path.exists():
                    previous["raw_missing_before_recovery"] = True
                    atomic_write(raw_path, b"")
                raw = raw_path.read_bytes()
                anomaly = slow_retry_reason(recovered, raw)
                if anomaly:
                    previous["state"] = "provider_failed"
                    code, eligible = anomaly, True
                previous["error_code"] = code
                partial = _partial_retry_reason(recovered, raw)
                eligible = (eligible or previous["state"] == "provider_failed" and partial is not None) and retained_retry_eligible(previous, recovered, raw)
            if retry_policy is None and previous["state"] == "provider_failed" and code == "max_output_tokens" and raw_path.exists():
                eligible = _partial_retry_reason(recovered, raw_path.read_bytes()) == "max_output_tokens"
            after = retry_after_seconds(next((value for name, value in headers.items() if name.lower() == "retry-after"), None), clock())
            _failure(previous, code, eligible, clock(), after, jitter, retry_policy)
            atomic_write(journal_dir / f"attempt-{previous['attempt']}" / "journal.json", encode(previous))
            atomic_write(journal_path, encode(previous))
            raise TranslationError("Prior attempt pending; retained as uncertain; fresh resume required")
        if previous["state"] != "completed" and retry:
            if previous["state"] == "uncertain" and not resume:
                raise TranslationError("Uncertain attempt requires fresh resume policy")
            status = policy_status or retry_status(journal_path)
            if not status["retry_eligible"]:
                raise TranslationError("Prior error is permanent or unclassified; no automatic retry")
            if previous["attempt"] >= status["max_attempts"]:
                raise TranslationError("Locale retry budget exhausted")
            if not _finite_number(status["retry_at"]) or clock() < status["retry_at"]:
                raise TranslationError("Retry is not due yet")
        if previous["state"] == "completed" and destination.exists():
            if digest(destination.read_bytes()) != previous["output_hash"]:
                raise TranslationError("Localization edited; refusing overwrite")
            if resume:
                validate_candidate(_load(destination), reviewed_practice_stages=reviewed_practice_stages)
                return previous
            raise TranslationError("Already completed; use --resume; refusing regeneration")
        if previous["state"] != "completed" and not retry:
            raise TranslationError("Prior attempt requires retry policy; no automatic retry inside execute")
        if not retry:
            raise TranslationError("Explicit retry required; it may incur new cost")
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in (timeout, wall_timeout)):
        raise TranslationError("Socket timeout and wall budget must be finite and positive")
    wall_timeout -= time.monotonic() - started
    if wall_timeout <= 0:
        raise TranslationError("Socket timeout and wall budget must be finite and positive")
    def check():
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Translation cancelled")
        if clock() >= deadline or time.monotonic() >= real_deadline:
            raise GenerationDeadline("Translation wall budget exceeded between socket reads")
    deadline = clock() + wall_timeout
    real_deadline = time.monotonic() + wall_timeout
    controller = {}
    finished = threading.Event()
    interruption = []
    def watchdog():
        while not finished.wait(0.01):
            cancelled = stop_event is not None and stop_event.is_set()
            if cancelled or clock() >= deadline or time.monotonic() >= real_deadline:
                if not interruption:
                    interruption.append(InterruptedError("Translation cancelled") if cancelled else GenerationDeadline("Translation wall budget exceeded"))
                connection = controller.get("connection")
                stream = controller.get("stream")
                sock = getattr(connection, "sock", None) or getattr(getattr(getattr(stream, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
    def transport_check():
        if interruption:
            raise interruption[0]
        check()
    controller["check"] = transport_check
    check()
    key_file = Path(os.environ.get("CONTACTBOX_KEY_FILE", str(root / "secrets" / "contactbox_key")))
    key = key_file.read_text(encoding="utf-8").strip()
    if not key or "\n" in key or "\r" in key:
        raise TranslationError("Invalid local credential")
    attempt = previous["attempt"] + 1 if previous else 1
    request_id = str(uuid.uuid4())
    log_dir = journal_dir / f"attempt-{attempt}"
    if log_dir.exists():
        raise TranslationError("Next attempt already has retained evidence; refusing overwrite or repurchase")
    record = {"request_id": request_id, "attempt": attempt, "input_hash": summary["hash"], "target": locale, "state": "pending", "dispatched_at": clock(), "output_cap": None, "reviewed_practice_stages": list(reviewed_practice_stages), "retry_eligible": False, "retry_at": None, "retry_after": None, "error_code": None, "billing_outcome": "unknown"}
    if retry_policy is not None:
        retry_policy.validate()
        record["retry_policy"] = dict(retry_policy)
    transport_started = time.monotonic()
    record["transport"] = {"socket_timeout_seconds": timeout, "wall_timeout_seconds": wall_timeout, "elapsed_seconds": 0, "received_bytes": 0, "termination_reason": None}
    atomic_write(log_dir / "request.json", encode(body))
    atomic_write(log_dir / "summary.json", encode(summary))
    atomic_write(journal_path, encode(record))
    atomic_write(log_dir / "journal.json", encode(record))
    request = urllib.request.Request(ENDPOINT, data=encode(body), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "text/event-stream", "Idempotency-Key": request_id}, method="POST")
    raw = bytearray()
    final = None

    def finish_transport(reason):
        finished.set()
        watcher.join()
        record["transport"].update(elapsed_seconds=time.monotonic() - transport_started, received_bytes=len(raw), termination_reason=reason)

    def transport_failure(error):
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, GenerationDeadline):
            return "generation_deadline", False
        if not isinstance(reason, Exception) or isinstance(reason, InterruptedError):
            return "cancelled", False
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "socket_timeout", True
        if isinstance(reason, (ConnectionError, http.client.IncompleteRead)) or "EOF before terminal" in str(error):
            return "remote_eof", True
        return "parse_contract", False
    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()
    try:
        gate()
        if retry_policy is not None:
            retry_policy.validate()
        check()
        with (log_dir / "raw.sse").open("ab", buffering=0) as evidence:
            def persist(line):
                evidence.write(line)
                os.fsync(evidence.fileno())
            socket_budget = min(timeout, real_deadline - time.monotonic())
            transport_check()
            opened = opener(request, timeout=socket_budget) if opener else open_response(request, timeout=socket_budget, controller=controller, idle_timeout=timeout)
            with opened as stream:
                controller["stream"] = stream
                atomic_write(log_dir / "response-headers.json", encode(_safe_headers(getattr(stream, "headers", {}))))
                final = read_stream(stream, raw, persist=persist, check=transport_check)
                finish_transport("provider_terminal")
                atomic_write(log_dir / "response.json", encode(final))
                record.update(response_id=final.get("id"), provider_status=final.get("status"), **usage_report(final.get("usage")))
                if final.get("status") == "completed" and not final.get("error"):
                    record["state"] = "rejected"
        anomaly = slow_retry_reason(final, raw) if retry_policy is not None else None
        if final.get("status") != "completed" or final.get("error") or anomaly:
            record["state"] = "provider_failed"
            code, eligible = (anomaly, True) if anomaly else classify_provider(final)
            if retry_policy is not None:
                partial = _partial_retry_reason(final, raw)
                eligible = eligible or partial is not None
            elif code == "max_output_tokens":
                # Retry only a proven incomplete, unrecoverable remote response;
                # no budget increase and no reinterpretation of old terminal journals.
                eligible = _partial_retry_reason(final, raw) == "max_output_tokens"
            headers = _load(log_dir / "response-headers.json")
            after = retry_after_seconds(next((value for name, value in headers.items() if name.lower() == "retry-after"), None), clock())
            _failure(record, code, eligible, clock(), after, jitter, retry_policy)
            raise TranslationError("Provider failed/incomplete; evidence retained")
        record["state"] = "rejected"
        atomic_write(log_dir / "journal.json", encode(record))
        atomic_write(journal_path, encode(record))
        atomic_write(log_dir / "raw-candidate.json", encode(final.get("output")))
        candidate = parse_candidate(final, reviewed_practice_stages=reviewed_practice_stages)
        atomic_write(log_dir / "candidate.json", encode(candidate))
        validation = validate(candidate)
        if validation is not True and not (isinstance(validation, dict) and validation.get("ok") is True):
            raise TranslationError("Structural validation rejected localization")
        gate()
        atomic_write(destination, encode(candidate))
        record.update(state="completed", output_hash=digest(destination.read_bytes()))
    except urllib.error.HTTPError as error:
        record.update(state="http_error", http_status=error.code, **usage_report(None))
        controller["stream"] = error.fp
        atomic_write(log_dir / "response-headers.json", encode(_safe_headers(error.headers)))
        atomic_write(log_dir / "http.json", encode({"status": error.code, "received_at": clock()}))
        _failure(record, f"http_{error.code}", error.code in TRANSIENT_HTTP, clock(), retry_after_seconds((error.headers or {}).get("Retry-After"), clock()), jitter, retry_policy)
        atomic_write(log_dir / "journal.json", encode(record))
        atomic_write(journal_path, encode(record))
        try:
            with (log_dir / "raw.sse").open("ab", buffering=0) as evidence:
                for chunk in _chunks(error.fp, transport_check):
                    raw.extend(chunk)
                    evidence.write(chunk)
                    os.fsync(evidence.fileno())
                    transport_check()
            finish_transport("http_error")
            try:
                payload = json.loads(bytes(raw))
            except (ValueError, UnicodeError):
                payload = None
            if isinstance(payload, dict):
                final = payload
                atomic_write(log_dir / "response.json", encode(payload))
                record.update(**usage_report(payload.get("usage")))
                if payload.get("error"):
                    code, eligible = classify_provider(payload)
                    if _permanent_provider(payload["error"]):
                        _failure(record, code, False, clock(), record["retry_after"], jitter, retry_policy)
        except BaseException as read_error:
            if interruption:
                read_error = interruption[0]
            code, eligible = transport_failure(read_error)
            record["state"] = "uncertain"
            finish_transport(code)
            _failure(record, code, eligible, clock(), jitter=jitter, retry_policy=retry_policy)
            raise read_error
        finally:
            error.close()
        raise TranslationError(f"HTTP {error.code}; evidence retained") from None
    except BaseException as error:
        if interruption and final is None:
            error = interruption[0]
        if record["state"] == "pending":
            record.update(state="uncertain", **usage_report(None))
            code, eligible = transport_failure(error)
            if isinstance(error, TranslationError) and "redirect" in str(error):
                record["state"] = "permanent_error"
                code, eligible = "parse_contract", False
            finish_transport(code)
            _failure(record, code, eligible, clock(), jitter=jitter, retry_policy=retry_policy)
        elif record["state"] == "rejected":
            _failure(record, "validation_rejected", False, clock(), jitter=jitter, retry_policy=retry_policy)
        raise error
    finally:
        finished.set()
        watcher.join()
        connection = controller.get("connection")
        if connection is not None:
            connection.close()
        if (retry_policy is not None or record.get("error_code") == "max_output_tokens") and record["retry_eligible"] and not retained_retry_eligible(record, final, raw):
            record.update(retry_eligible=False, retry_at=None)
        atomic_write(log_dir / "raw.sse", bytes(raw))
        atomic_write(log_dir / "journal.json", encode(record))
        atomic_write(journal_path, encode(record))
    return record
