"""Read-only, paginated catalogue over a family run's pinned external history.

The pinned schema-5 ``family-context.json`` (verified by ``family.load_family``) is the only
database: rows come from its ``external_history`` in stable learner order, hashes come from its
already-verified ``dependencies``. Nothing is written, generated or summarised.
"""

import json
from pathlib import Path

from . import core, family, history_index

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
FIELD_LIMITS = {"topic": 160, "objective": 280}
SUMMARY_FIELDS = ("topic", "objective")
DETAIL_FIELDS = ("topic", "objective", "curriculum", "family_plan", "literacy_policy", "prerequisites", "extension", "sources")
LIMITATIONS = history_index.LIMITATIONS + [
    "Catalogue rows are verbatim (possibly truncated) brief claims from the pinned context; missing fields are reported, never generated.",
]


def _pinned(root, run, context_hash):
    root = Path(root)
    if root.is_symlink():
        raise core.AuthoringError("Symlink project root is forbidden")
    root = root.resolve()
    family.family_identity(run)
    for path in family.family_path(root, run).rglob("*"):
        if path.is_symlink():
            raise core.AuthoringError("Symlink family state is forbidden")
    _, before = history_index._read(root, f"{run}/family-context.json")
    context = family.load_family(root, run)
    if context.get("schema") != 5:
        raise core.AuthoringError("Only schema5 family contexts are supported")
    if context_hash is not None and context_hash != context["context_hash"]:
        raise core.AuthoringError("Pinned context hash does not match the family run")
    if len(context["external_history"]) > history_index.MAX_RECORDS:
        raise core.AuthoringError("History exceeds record limit; no truncation performed")
    return root, context, before


def _recheck(root, run, before):
    _, after = history_index._read(root, f"{run}/family-context.json")
    if after != before:
        raise core.AuthoringError("Pinned family context changed during history read")


def _stage(stage):
    if stage is not None:
        core.stage_identity(10, stage)
    return stage


def _locale(context, locale):
    if locale is None:
        return None
    if locale not in core.learner_locales(context["study_language"]):
        raise core.AuthoringError("Unknown learner locale or same-language pair")
    return locale


def _files(context, record, locale):
    base = f"{record['path']}/{locale}/stage-{record['stage']}"
    files = {"brief": f"{record['path']}/stage-{record['stage']}/brief.json", "lesson": f"{base}/lesson.md", "quiz": f"{base}/quiz.md", "notes": f"{base}/adaptation-notes.json"}
    result = {}
    for name, path in files.items():
        digest = context["dependencies"].get(path)
        result[name] = {"path": path, "sha256": digest, "status": "pinned" if digest else "missing"}
    return result


def _family(context, run):
    """Compact current-family metadata; never the whole context, words or dependency map."""
    code = context["study_language"]
    dependencies = context["dependencies"]
    vocabulary = f"reference/vocabulary/{core.LANGUAGES[code]}/{core.LANGUAGES[code].lower()}_vocabulary.csv"
    references = {path: dependencies.get(path) for path in (vocabulary, "reference/grammar/grammar.sqlite", "reference/manifest.json") if path in dependencies}
    formats = sorted(path for path in dependencies if path.startswith("reference/formats/"))
    stages = []
    for window in context["stages"]:
        prior, planning = window["prior_external_keys"], window["planning_external_keys"]
        stages.append({
            "stage": window["stage"], "base_checkpoint": window["base_checkpoint"], "checkpoint": window["checkpoint"], "unlock": window["checkpoint"],
            "new_word_range": window["new_word_range"], "known_rank_through": window["checkpoint"],
            "prior_count": len(prior), "planning_count": len(planning),
            "last_prior_key": prior[-1] if prior else None, "first_planning_key": planning[0] if planning else None,
            "own_prior_stages": window["prior_family_stages"],
        })
    return {
        "run": run, "study_language": code, "base_checkpoint": context["base_checkpoint"], "family_path": context["family_path"],
        "source_locale": context["source_locale"], "locales": context["locales"], "authoring": context["authoring"],
        "vocabulary_horizon": context["vocabulary_horizon"], "vocabulary_path": vocabulary,
        "references": [{"path": path, "sha256": digest} for path, digest in references.items()],
        "format_references": {"count": len(formats), "paths": formats[:20], "truncated": len(formats) > 20},
        "dependency_count": len(dependencies), "context_path": f"{run}/family-context.json",
        "stages": stages,
    }


def _eligibility(context, key, stage):
    labels = {f"stage:{index}": "prior" if key in window["prior_external_keys"] else "planning" if key in window["planning_external_keys"] else "unrelated" for index, window in enumerate(context["stages"])}
    return labels[f"stage:{stage}"] if stage is not None else labels


def _row(context, index, record, stage, locale):
    brief = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
    chosen = locale or record["source_locale"]
    summary, truncated = {}, []
    for field in SUMMARY_FIELDS:
        value = brief.get(field)
        if isinstance(value, str) and len(value) > FIELD_LIMITS[field]:
            value = value[:FIELD_LIMITS[field]].rstrip() + "…"
            truncated.append(field)
        if value is not None:
            summary[field] = value
    extension = brief.get("extension")
    mode = extension.get("mode") if isinstance(extension, dict) else None
    files = _files(context, record, chosen)
    return {
        "index": index, "key": record["key"],
        **{name: record[name] for name in ("base_checkpoint", "stage", "checkpoint")},
        "unlock": record["checkpoint"], "source_locale": record["source_locale"], "selected_locale": chosen,
        **summary, "extension": mode if mode in ("practice", "extend") else None,
        "missing_fields": [field for field in SUMMARY_FIELDS if not isinstance(brief.get(field), str) or not brief[field].strip()] + ([] if record["stage"] == 0 or mode in ("practice", "extend") else ["extension"]),
        "truncated_fields": truncated, "full_brief_path": files["brief"]["path"] if truncated else None,
        "eligibility": _eligibility(context, record["key"], stage),
        "files": files,
    }


def history_catalog(root, run, *, stage=None, offset=0, limit=DEFAULT_LIMIT, locale=None, context_hash=None):
    """Paginated overview of every external_history record; never drops future/planning rows."""
    stage = _stage(stage)
    for name, value, low, high in (("offset", offset, 0, history_index.MAX_RECORDS), ("limit", limit, 1, MAX_LIMIT)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise core.AuthoringError(f"{name} must be an integer from {low} to {high}")
    root, context, before = _pinned(root, run, context_hash)
    locale = _locale(context, locale)
    records = context["external_history"]
    total = len(records)
    if offset > 0 and offset >= total:
        raise core.AuthoringError(f"offset {offset} is out of bounds for {total} records")
    rows = [_row(context, index, records[index], stage, locale) for index in range(offset, min(total, offset + limit))]
    end = offset + len(rows)
    result = {
        "schema": 1, "mode": "catalog", "run": run, "context_hash": context["context_hash"], "source_locale": context["source_locale"],
        "selected_locale": locale, "stage": stage, "family": _family(context, run), "order": "learner (checkpoint, base_checkpoint, stage)",
        "total": total, "offset": offset, "limit": limit, "returned": len(rows), "next_offset": end if end < total else None,
        "limitations": LIMITATIONS, "rows": rows,
    }
    if stage is not None:
        result["current_stage"] = {"checkpoint": context["stages"][stage]["checkpoint"], "own_prior_stages": [{"marker": f"stage:{previous}", "brief_path": f"{run}/stage-{previous}/brief.json"} for previous in range(stage)], "note": "Own-family drafts are separate from external history; read them with --stage N (snapshot)."}
    _recheck(root, run, before)
    return result


def history_entry(root, run, key, *, stage=None, locale=None, context_hash=None):
    """Full metadata for one pinned external_history key (no lesson/quiz bodies, no full lexical audit)."""
    stage = _stage(stage)
    if not isinstance(key, str):
        raise core.AuthoringError("History key must be a string")
    root, context, before = _pinned(root, run, context_hash)
    locale = _locale(context, locale)
    matches = [(index, record) for index, record in enumerate(context["external_history"]) if record["key"] == key]
    if len(matches) != 1:
        raise core.AuthoringError("Unknown history key; use a key from --catalog")
    index, record = matches[0]
    row = _row(context, index, record, stage, locale)
    brief = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
    audit = brief.get("lexical_audit")
    claims = {field: brief[field] for field in DETAIL_FIELDS if field in brief}
    claims["taught_form_claims"] = [item for item in audit if isinstance(item, dict) and "taught_in" in item] if isinstance(audit, list) else []
    anchors = None
    for name, pinned in row["files"].items():
        if pinned["sha256"] is None:
            continue
        text, observed = history_index._read(root, pinned["path"])
        if observed["sha256"] != pinned["sha256"]:
            raise core.AuthoringError(f"History file differs from pinned dependency: {pinned['path']}")
        if name == "lesson":
            anchors = history_index._headings(text, True)
    for field in SUMMARY_FIELDS:
        row.pop(field, None)
    row.update({"truncated_fields": [], "full_brief_path": row["files"]["brief"]["path"]})
    result = {
        "schema": 1, "mode": "entry", "run": run, "context_hash": context["context_hash"], "stage": stage, "selected_locale": locale,
        "limitations": LIMITATIONS, "entry": {**row, "claims": claims,
        "missing_claim_fields": [field for field in ("topic", "objective", "curriculum", "lexical_audit") if field not in brief],
        "lexical_audit_count": len(audit) if isinstance(audit, list) else 0,
        "page_anchors": anchors, "page_count": None if anchors is None else len(anchors)},
    }
    if len(json.dumps(result, ensure_ascii=False).encode()) > history_index.MAX_BYTES:
        raise core.AuthoringError("History entry exceeds byte limit; no truncation performed")
    _recheck(root, run, before)
    return result
