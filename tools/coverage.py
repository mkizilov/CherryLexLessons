import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lesson_factory import core, family


LABEL = "referenced-not-linguistically-verified"
MAX_BYTES = 64 * 1024 * 1024
MAX_CLAIMS = 100
MAX_PACKS = 5000
MAX_TOPICS = 2000
MAX_REFERENCE_IDS = 100


def read_text(path):
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise core.AuthoringError(f"Coverage input exceeds {MAX_BYTES} bytes: {path.name}; no truncation performed")
    return data.decode("utf-8")


def read_json(path):
    return json.loads(read_text(path))


def heading_ids(text, pages=False):
    result = set()
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        pattern = r"^# [^\r\n]+ \{#([A-Za-z0-9][A-Za-z0-9_-]*)\}\s*$" if pages else r"^# ([A-Za-z0-9][A-Za-z0-9_-]*)\s*$"
        match = re.fullmatch(pattern, line)
        if match:
            result.add(match.group(1))
    return result


def report(root, language, through, roadmap):
    root = Path(root).resolve()
    code = core.language_code(language)
    if type(through) is not int or through < 1:
        raise core.AuthoringError("Through must be a positive integer vocabulary rank")
    if Path(roadmap).is_absolute():
        raise core.AuthoringError("Roadmap must be root-relative")
    roadmap_path = core.safe_path(root, roadmap)
    if not roadmap_path.is_file():
        raise core.AuthoringError(f"Missing curriculum roadmap: {roadmap}; create an author planning file such as work/curriculum/{code}.json")
    plan = read_json(roadmap_path)
    if not isinstance(plan, dict) or plan.get("study_language") != code or not isinstance(plan.get("topics"), list):
        raise core.AuthoringError("Roadmap requires matching study_language and topics list")
    if len(plan["topics"]) > MAX_TOPICS:
        raise core.AuthoringError(f"Roadmap exceeds {MAX_TOPICS} topics; no truncation performed")
    topics = {}
    for topic in plan["topics"]:
        if not isinstance(topic, dict) or not isinstance(topic.get("id"), str) or not topic["id"] or topic["id"] in topics:
            raise core.AuthoringError("Roadmap topics require unique nonempty IDs")
        topics[topic["id"]] = topic
    families = family.family_registry_facts(root, code)
    if 5 * len(families) > MAX_PACKS:
        raise core.AuthoringError(f"Registry exceeds {MAX_PACKS} learner stages; no truncation performed")
    manifests = {}
    family_contexts = {}
    stages = []
    for row in families:
        read_json(core.safe_path(root, f"{row['path']}/manifest.json"))
        context = read_json(core.safe_path(root, f"{row['path']}/family-context.json"))
        receipt = family.verify_family_pack(root, row)
        family_contexts[row["path"]] = context
        if not isinstance(receipt.get("stages"), list) or len(receipt["stages"]) != 5:
            raise core.AuthoringError("Family receipt requires exactly five stages")
        for stage in range(5):
            identity = core.stage_identity(row["base_checkpoint"], stage)
            window = receipt["stages"][stage]
            if not isinstance(window, dict) or any(type(window.get(key)) is not type(value) or window.get(key) != value for key, value in identity.items()):
                raise core.AuthoringError("Family stage identity mismatch")
            key = f"{row['path']}#stage:{stage}"
            stages.append({**identity, "path": key, "family_path": row["path"], "source_locale": receipt["source_locale"], "window": window})
            manifests[key] = read_json(core.safe_path(root, f"{row['path']}/stage-{stage}/brief.json"))
    facts = sorted(stages, key=core.event_key)
    by_path = {row["path"]: row for row in facts}
    page_cache = {}
    warnings = []
    claims = []
    learner_facts = [row for row in facts if row["checkpoint"] <= through]

    def source_path(row, name):
        return f"{row['family_path']}/{row['source_locale']}/stage-{row['stage']}/{name}"

    def pages(row):
        if row["path"] not in page_cache:
            page_cache[row["path"]] = heading_ids(read_text(core.safe_path(root, source_path(row, "lesson.md"))), pages=True)
        return page_cache[row["path"]]

    for row in learner_facts:
        pack_manifest = manifests[row["path"]]
        raw_claims = pack_manifest.get("curriculum", [])
        if isinstance(raw_claims, dict):
            raw_claims = raw_claims.get("claims", [])
        if not isinstance(raw_claims, list) or len(raw_claims) > MAX_CLAIMS:
            raise core.AuthoringError(f"Invalid or excessive curriculum claims: {row['path']}")
        if not raw_claims:
            warnings.append(f"{row['path']}: no curriculum claims; absence is not evidence of missing teaching")
            continue
        question_ids = heading_ids(read_text(core.safe_path(root, source_path(row, "quiz.md"))))
        eligible = {row["path"]: pages(row)}
        context = family_contexts[row["family_path"]]
        window = row["window"]
        external = window.get("prior_external_keys", [])
        own = window.get("prior_family_stages", [])
        history = context.get("external_history", [])
        if not isinstance(external, list) or not isinstance(own, list) or not isinstance(history, list) or len(history) > MAX_PACKS or len(external) > MAX_PACKS or len(own) > 4:
            raise core.AuthoringError("Invalid or excessive family original history; no truncation performed")
        if any(not isinstance(entry, dict) or not isinstance(entry.get("key"), str) for entry in history):
            raise core.AuthoringError("Invalid family original history keys")
        original_keys = {entry["key"] for entry in history}
        prior = [key for key in external if isinstance(key, str) and key in original_keys]
        for key in external:
            if not isinstance(key, str) or key not in original_keys:
                warnings.append(f"{row['path']}: ineligible or absent original external key {key}")
        for key in own:
            match = re.fullmatch(r"stage:([0-4])", key) if isinstance(key, str) else None
            if match and int(match[1]) < row["stage"]:
                prior.append(f"{row['family_path']}#{key}")
            else:
                warnings.append(f"{row['path']}: ineligible original family stage {key}")
        for path in prior:
            original = by_path.get(path)
            if original is None or core.event_key(original) >= core.event_key(row):
                warnings.append(f"{row['path']}: ineligible or unregistered prior pack {path}")
                continue
            eligible[path] = pages(original)
        for index, claim in enumerate(raw_claims):
            claim_warnings = []
            if not isinstance(claim, dict):
                warnings.append(f"{row['path']}: unknown claim shape at {index}")
                continue
            topic_id = claim.get("topic_id")
            if not isinstance(topic_id, str) or topic_id not in topics:
                claim_warnings.append("missing or unknown roadmap topic_id")
            if claim.get("intent") not in ("introduce", "practice", "extend", "review"):
                claim_warnings.append("unknown claim intent")
            references = {}
            for field in ("teaching_pages", "question_ids"):
                values = claim.get(field)
                if isinstance(values, list) and len(values) > MAX_REFERENCE_IDS:
                    raise core.AuthoringError(f"Claim exceeds {MAX_REFERENCE_IDS} {field}: {row['path']}; no truncation performed")
                if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value for value in values):
                    claim_warnings.append(f"{field} requires nonempty string IDs")
                    values = []
                references[field] = values
            missing_questions = [value for value in references["question_ids"] if value not in question_ids]
            provenance = {value: [path for path, ids in eligible.items() if value in ids] for value in references["teaching_pages"]}
            missing_pages = [value for value, paths in provenance.items() if not paths]
            if missing_questions:
                claim_warnings.append("missing current question IDs: " + ", ".join(missing_questions))
            if missing_pages:
                claim_warnings.append("missing current/original eligible prior page IDs: " + ", ".join(missing_pages))
            if any(len(paths) > 1 for paths in provenance.values()):
                claim_warnings.append("ambiguous page provenance; bare ID occurs in multiple eligible packs")
            claims.append({"pack": row["path"], "unlock_rank": row["checkpoint"], "topic_id": topic_id, "intent": claim.get("intent"), "label": LABEL, **references, "page_provenance": provenance, "missing_question_ids": missing_questions, "missing_page_ids": missing_pages, "warnings": claim_warnings})
    topic_reports = []
    for topic_id, topic in topics.items():
        associated = [claim for claim in claims if claim["topic_id"] == topic_id]
        deadline = topic.get("review_by_rank")
        topic_reports.append({**topic, "label": LABEL if associated else "no-finalized-learner-claim", "learner_claim_count": len(associated), "review_due": type(deadline) is int and deadline <= through})
    return {"study_language": code, "through": through, "roadmap": roadmap, "scope": "Claim/reference inventory, not linguistic coverage or learner mastery", "reference_check": "Strict anchored Markdown headings outside fences; not full GMD/QMD or linguistic validation", "learner_pack_count": len(learner_facts), "family_bundle_count": len(families), "source_unit_count": len(families), "learner_stage_count": len(learner_facts), "future_finalized_planning_pack_count": len(facts) - len(learner_facts), "target_rank": plan.get("target_rank"), "overview_review_range": plan.get("overview_review_range"), "domain_audit": plan.get("domain_audit", []), "topics": topic_reports, "claims": claims, "warnings": warnings, "limitations": plan.get("limitations", [])}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only curriculum claims and reference inventory; never proof of mastery")
    parser.add_argument("--language", required=True)
    parser.add_argument("--through", required=True, type=int)
    parser.add_argument("--roadmap", required=True, help="Root-relative author planning JSON, e.g. work/curriculum/ru.json")
    args = parser.parse_args(argv)
    try:
        result = report(ROOT, args.language, args.through, args.roadmap)
    except (core.AuthoringError, OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"coverage: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
