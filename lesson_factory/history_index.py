import hashlib
import json
import re
from pathlib import Path

from . import core, family

MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 5000
LIMITATIONS = [
    "Derived discovery index; scan the complete catalogue, then inspect relevant originals and dependencies under AGENTS.md. Summaries alone are not teaching evidence.",
    "Brief fields are author claims, not verified teaching, learner knowledge or mastery.",
    "Anchors and question IDs are strict headings outside fences, not full semantic validation.",
    "Own-prior drafts are mutable observations, not finalized prerequisites or proof of teaching.",
]


def _read(root, relative, optional=False):
    path = core.safe_path(root, relative)
    if optional and not path.exists():
        return None, {"path": relative, "status": "not-authored", "sha256": None}
    if not path.is_file():
        raise core.AuthoringError(f"Missing regular history file: {relative}")
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise core.AuthoringError("History input exceeds byte limit; no truncation performed")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise core.AuthoringError(f"Invalid UTF-8 history file: {relative}") from exc
    return text, {"path": relative, "status": "present" if text.strip() else "empty", "sha256": hashlib.sha256(data).hexdigest()}


def _headings(text, pages=False):
    result = []
    fence = None
    pattern = r"^# [^\r\n]+ \{#([A-Za-z0-9][A-Za-z0-9_-]*)\}\s*$" if pages else r"^# ([A-Za-z0-9][A-Za-z0-9_-]*)\s*$"
    for line in (text or "").splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            match = re.fullmatch(pattern, line)
            if match:
                result.append(match[1])
    return result


def _claims(brief, detail):
    result = {key: brief[key] for key in ("topic", "objective", "curriculum") if key in brief}
    audit = brief.get("lexical_audit", [])
    result["taught_form_claims"] = [item for item in audit if isinstance(item, dict) and "taught_in" in item] if isinstance(audit, list) else []
    if detail:
        result.update({key: brief[key] for key in ("family_plan", "literacy_policy", "prerequisites", "extension", "lexical_audit") if key in brief})
    return result


def snapshot_index(root, run, stage, locale=None, *, detail=False):
    core.stage_identity(10, stage)
    code, _ = family.family_identity(run)
    if locale is not None and locale not in core.learner_locales(code):
        raise core.AuthoringError("Unknown learner locale or same-language pair")
    root = Path(root)
    if root.is_symlink():
        raise core.AuthoringError("Symlink project root is forbidden")
    root = root.resolve()
    _read(root, f"{run}/family-context.json")
    folder = family.family_path(root, run)
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise core.AuthoringError("Symlink family state is forbidden")
    context = family.load_family(root, run)
    if len(context["external_history"]) > MAX_RECORDS:
        raise core.AuthoringError("History exceeds record limit; no truncation performed")
    selected = locale or context["source_locale"]
    window = context["stages"][stage]
    records = {record["key"]: record for record in context["external_history"]}
    observations = []

    def entry(record, relation, own=False):
        source = record["source_locale"]
        chosen = locale or source
        path = record["path"]
        if not own and not path.startswith("family_packs/"):
            raise core.AuthoringError("History must reference a finalized family")
        member = f"/stage-{record['stage']}"
        brief_relative = f"{path}/stage-{record['stage']}/brief.json"
        brief_text, brief_file = _read(root, brief_relative, optional=own)
        if brief_text is None:
            brief = {}
        else:
            try:
                brief = json.loads(brief_text)
            except ValueError as exc:
                raise core.AuthoringError(f"Invalid history JSON: {brief_relative}") from exc
            if not isinstance(brief, dict):
                raise core.AuthoringError("History brief must be an object")
        lesson, lesson_file = _read(root, f"{path}/{chosen}{member}/lesson.md", optional=own)
        quiz, quiz_file = _read(root, f"{path}/{chosen}{member}/quiz.md", optional=own)
        files = {"brief": brief_file, "lesson": lesson_file, "quiz": quiz_file}
        observations.extend(files.values())
        anchors, ids = _headings(lesson, True), _headings(quiz)
        return {
            "key": record["key"], "relation": relation,
            **{key: record[key] for key in ("checkpoint", "base_checkpoint", "stage")},
            "source_locale": source, "selected_locale": chosen,
            "authorship": "not-authored" if own and (brief_text is None or quiz is None or not quiz.strip() or lesson is None) else "draft-observed" if own else "finalized-history",
            "files": files, "claims": _claims(brief, detail),
            "missing_claim_fields": [key for key in ("topic", "objective", "curriculum", "lexical_audit") if key not in brief],
            "page_anchors": anchors, "page_count": len(anchors),
            "question_ids": ids, "question_count": len(ids),
        }

    prior = [entry(records[key], "prior") for key in window["prior_external_keys"]]
    planning = [entry(records[key], "planning") for key in window["planning_external_keys"]]
    own = [entry({**context["stages"][previous], "key": f"{run}#stage:{previous}", "path": run, "source_locale": context["source_locale"]}, "own-prior", True) for previous in range(stage)]
    if family.load_family(root, run)["context_hash"] != context["context_hash"]:
        raise core.AuthoringError("Pinned family context changed during history read")
    for observed in observations:
        _, current = _read(root, observed["path"], optional=observed["status"] == "not-authored")
        if current != observed:
            raise core.AuthoringError(f"History file changed during read: {observed['path']}")
    return {"schema": 1, "run": run, "stage": stage, "checkpoint": window["checkpoint"], "context_hash": context["context_hash"], "source_locale": context["source_locale"], "selected_locale": selected, "limitations": LIMITATIONS, "prior": prior, "planning": planning, "own_prior": own}


def horizon_report(root, language):
    if Path(root).is_symlink():
        raise core.AuthoringError("Symlink project root is forbidden")
    code = core.language_code(language)
    count = len(core.vocabulary(root, code))
    if not 1 <= count <= 999999:
        raise core.AuthoringError("Vocabulary count must be from 1 to 999999")
    expanded = min(999999, ((count + 199) // 200) * 200)
    order = family.family_order(expanded)
    prefix = []
    blocked = None
    for item in order:
        if item["base_checkpoint"] + 225 > count:
            blocked = item
            break
        prefix.append(item)
    supported = {item["base_checkpoint"] for item in prefix}
    tail = [item["base_checkpoint"] for item in order if item["base_checkpoint"] + 225 <= count and item["base_checkpoint"] not in supported]
    last = max(supported, default=None)
    return {
        "study_language": code, "vocabulary_count": count, "expanded_complete_block_through": expanded,
        "supported_prefix_families": len(prefix), "supported_prefix_stages": len(prefix) * 5,
        "last_authored_base": prefix[-1]["base_checkpoint"] if prefix else None,
        "highest_supported_base": last,
        "first_blocked_base": blocked["base_checkpoint"] if blocked else None,
        "first_blocked_required_rank": blocked["base_checkpoint"] + 225 if blocked else None,
        "theoretical_last_eligible_base": max(0, (count - 225) // 10 * 10) or None,
        "eligible_but_blocked_tail": tail,
        "last_learner_unlock": last + 225 if last is not None else None,
        "limitations": ["Read-only vocabulary ceiling, not actual authored progress or an assessment/coverage target.", "Strict two-wave prefix; eligible later families remain blocked. No skipping, clamping, schedule changes or mastery inference."]
    }
