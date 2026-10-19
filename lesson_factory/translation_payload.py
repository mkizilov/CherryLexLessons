from . import core, family
from .translation import TranslationError, digest


PAYLOAD_VERSION = 2


def build_prompt(root):
    prompt = core.safe_path(root, "prompts/localize-family.md").read_text(encoding="utf-8")
    for name in ("GRAMMAR_MARKDOWN.md", "QUIZ_MARKDOWN.md", "conventions.json"):
        prompt += "\n\nFORMAT REFERENCE " + name + "\n" + core.safe_path(root, f"reference/formats/grammar/{name}").read_text(encoding="utf-8")
    return prompt


def _constraints(brief, stage):
    if not isinstance(brief, dict):
        raise TranslationError(f"Stage {stage} brief must be an object")
    policies = brief.get("option_text_policies", {})
    if not isinstance(policies, dict) or any(not isinstance(key, str) or not key or not isinstance(value, str) or value not in ("target", "learner") for key, value in policies.items()):
        raise TranslationError(f"Stage {stage} option_text_policies must map question IDs to target or learner")
    result = {"option_text_policies": dict(policies)}
    if stage:
        extension = brief.get("extension")
        if not isinstance(extension, dict) or extension.get("mode") not in ("practice", "extend"):
            raise TranslationError(f"Stage {stage} requires extension.mode practice or extend")
        result["extension"] = {"mode": extension["mode"]}
    if "objective" in brief:
        if not isinstance(brief["objective"], str):
            raise TranslationError(f"Stage {stage} objective must be a string")
        result["objective"] = brief["objective"]
    if "literacy_policy" in brief:
        literacy = brief["literacy_policy"]
        fields = ("assumed", "provided_here", "not_assessed")
        if not isinstance(literacy, dict) or any(not isinstance(literacy.get(key), str) for key in fields):
            raise TranslationError(f"Stage {stage} literacy_policy requires assumed, provided_here and not_assessed strings")
        result["literacy_policy"] = {key: literacy[key] for key in fields}
    if "localization_risks" in brief:
        risks = brief["localization_risks"]
        if not isinstance(risks, list) or any(not isinstance(value, str) for value in risks):
            raise TranslationError(f"Stage {stage} localization_risks must be an array of strings")
        result["localization_risks"] = list(risks)
    return result


def build_payload(root, run, context, locale, evidence):
    study, base = family.family_identity(run)
    if not isinstance(context, dict) or context.get("study_language") != study or context.get("source_locale") != core.source_locale(study) or context.get("base_checkpoint") != base:
        raise TranslationError("Pinned family study/source/base identity is required")
    if locale not in core.translation_locales(study):
        raise TranslationError("Destination must be supported and differ from study and source locale")
    identities = context.get("stages")
    if not isinstance(identities, list) or len(identities) != 5:
        raise TranslationError("Payload requires exactly five ordered stages")
    stages, briefs = [], []
    for stage, identity in enumerate(identities):
        event = core.stage_identity(base, stage)
        expected = {**event, "new_word_range": core.word_range(event), "prior_family_stages": [f"stage:{previous}" for previous in range(stage)]}
        if not isinstance(identity, dict) or any(type(identity.get(key)) is not int for key in event) or any(identity.get(key) != value for key, value in expected.items()):
            raise TranslationError(f"Stage {stage} identity/knowledge window mismatch")
        if any(type(rank) is not int for rank in identity["new_word_range"]):
            raise TranslationError(f"Stage {stage} new_word_range must contain integer ranks")
        brief = core.read_json(core.safe_path(root, f"{run}/stage-{stage}/brief.json"))
        constraints = _constraints(brief, stage)
        texts = {}
        for name in ("lesson", "quiz"):
            path = core.safe_path(root, f"{run}/{context['source_locale']}/stage-{stage}/{name}.md")
            try:
                texts[f"{name}_md"] = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeError) as error:
                raise TranslationError(f"Cannot read complete stage {stage} {name}.md as UTF-8") from error
        practice = constraints.get("extension", {}).get("mode") == "practice"
        if not texts["quiz_md"].strip() or (not practice and not texts["lesson_md"].strip()):
            raise TranslationError(f"Stage {stage} source files are incomplete")
        if practice and texts["lesson_md"].strip():
            raise TranslationError(f"Stage {stage} practice lesson must be empty")
        stages.append({**expected, "constraints": constraints, **texts})
        briefs.append(brief)
    return {
        "payload_version": PAYLOAD_VERSION,
        "study_language": study,
        "source_locale": context["source_locale"],
        "source_stages": stages,
        "provenance_digest": digest({"context": context, "source_stages": stages, "briefs": briefs, "review_evidence": evidence}),
    }
