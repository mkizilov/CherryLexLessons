import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lesson_factory import core
from lesson_factory.translation import TranslationError, digest, encode, prepare
from lesson_factory.translation_payload import PAYLOAD_VERSION, build_payload, build_prompt


class TranslationPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = "family_runs/de/000020"
        self.context = {
            "schema": 5,
            "study_language": "de",
            "source_locale": "en",
            "base_checkpoint": 20,
            "locales": list(core.learner_locales("de")),
            "words": [{"word": "PRIVATE_VOCABULARY", "rank": 1}],
            "dependencies": {"PRIVATE_DEPENDENCY": "hash"},
            "external_history": [],
            "stages": [],
        }
        self.evidence = {"binding": {"PRIVATE_REVIEW_BINDING": "source"}, "reviews": {"pedagogical": "PRIVATE_REVIEW_DETAILS"}}
        self.briefs = []
        self.sources = []
        for stage in range(5):
            event = core.stage_identity(20, stage)
            self.context["stages"].append({**event, "new_word_range": core.word_range(event), "prior_family_stages": [f"stage:{previous}" for previous in range(stage)], "prior_external_keys": ["PRIVATE_PRIOR_KEY"], "planning_external_keys": ["PRIVATE_PLANNING_KEY"]})
            brief = {
                "objective": f"Preserve the stage {stage} decision",
                "option_text_policies": {f"q{stage}-concept": "learner", f"q{stage}-target": "target"},
                "literacy_policy": {"assumed": "Only displayed spelling", "provided_here": "Readings remain", "not_assessed": "Listening", "extra": "PRIVATE_LITERACY_DETAIL"},
                "localization_risks": ["Retain number and politeness"],
                "family_plan": {"evidence": "PRIVATE_FAMILY_PLAN"},
                "lexical_audit": [{"target_form": "PRIVATE_AUDIT"}],
                "prerequisites": [{"source": "PRIVATE_PREREQUISITE"}],
                "sources": [{"reference": "PRIVATE_RESEARCH_SOURCE"}],
            }
            if stage:
                brief["extension"] = {"mode": "practice" if stage == 2 else "extend", "rationale": "PRIVATE_EXTENSION_RATIONALE"}
            self.briefs.append(brief)
            core.write_json(self.root / self.run / f"stage-{stage}/brief.json", brief)
            folder = self.root / self.run / f"en/stage-{stage}"
            folder.mkdir(parents=True)
            lesson = "\r\n \t" if stage == 2 else f"\ufeffLesson {stage}\r\nÄrger: e\u0301 — 日本語 العربية\r\n`Sie` [external](old-lesson#old-page)\r\n"
            quiz = f"Quiz {stage}\r\nPrompt `Sie` — 学习\r\nAnswer: Ä\n"
            source = {"lesson_md": lesson, "quiz_md": quiz}
            self.sources.append(source)
            (folder / "lesson.md").write_bytes(lesson.encode("utf-8"))
            (folder / "quiz.md").write_bytes(quiz.encode("utf-8"))
            (folder / "adaptation-notes.json").write_text('["PRIVATE_SOURCE_NOTES"]', encoding="utf-8")
        (self.root / "prompts").mkdir()
        (self.root / "prompts/localize-family.md").write_text("Localized prompt — 完整", encoding="utf-8")
        formats = self.root / "reference/formats/grammar"
        formats.mkdir(parents=True)
        for name in ("GRAMMAR_MARKDOWN.md", "QUIZ_MARKDOWN.md", "conventions.json"):
            (formats / name).write_text("Full reference " + name, encoding="utf-8")

    def payload(self, context=None, evidence=None, locale="fr"):
        return build_payload(self.root, self.run, self.context if context is None else context, locale, self.evidence if evidence is None else evidence)

    def write_brief(self, stage, brief):
        core.write_json(self.root / self.run / f"stage-{stage}/brief.json", brief)

    def test_five_complete_source_pairs_preserve_exact_unicode_and_bytes(self):
        before = copy.deepcopy(self.context)
        value = self.payload()
        self.assertEqual(set(value), {"payload_version", "study_language", "source_locale", "source_stages", "provenance_digest"})
        self.assertEqual(value["payload_version"], PAYLOAD_VERSION)
        self.assertEqual(PAYLOAD_VERSION, 2)
        self.assertEqual(value["study_language"], "de")
        self.assertEqual(value["source_locale"], "en")
        self.assertEqual([member["stage"] for member in value["source_stages"]], list(range(5)))
        for stage, member in enumerate(value["source_stages"]):
            self.assertEqual(set(member), {"stage", "base_checkpoint", "checkpoint", "new_word_range", "prior_family_stages", "constraints", "lesson_md", "quiz_md"})
            for field in ("stage", "base_checkpoint", "checkpoint", "new_word_range", "prior_family_stages"):
                self.assertEqual(member[field], self.context["stages"][stage][field])
            for field in ("lesson_md", "quiz_md"):
                self.assertEqual(member[field].encode("utf-8"), self.sources[stage][field].encode("utf-8"))
        self.assertEqual(self.context, before)
        self.assertEqual(value, self.payload())
        value["source_stages"][1]["prior_family_stages"].clear()
        value["source_stages"][1]["constraints"]["option_text_policies"].clear()
        self.assertEqual(self.payload()["source_stages"][1]["prior_family_stages"], ["stage:0"])
        self.assertTrue(self.payload()["source_stages"][1]["constraints"]["option_text_policies"])

    def test_history_growth_and_content_only_change_fixed_size_provenance(self):
        baseline = self.payload()
        expanded = copy.deepcopy(self.context)
        expanded["external_history"] = [{"path": f"missing/past-{index}", "lesson": "PRIVATE_PAST_LESSON" * 2000, "quiz": "PRIVATE_PAST_QUIZ" * 2000} for index in range(25)]
        expanded["localized_external_history"] = [{"lesson_md": "PRIVATE_LOCALIZED_HISTORY" * 2000}]
        expanded["words"] *= 1000
        expanded["dependencies"].update({f"PRIVATE_DEPENDENCY_{index}": "hash" for index in range(1000)})
        for identity in expanded["stages"]:
            identity["prior_external_keys"] *= 100
            identity["planning_external_keys"] *= 100
        changed = self.payload(context=expanded)
        self.assertNotEqual(changed["provenance_digest"], baseline["provenance_digest"])
        self.assertEqual({key: value for key, value in baseline.items() if key != "provenance_digest"}, {key: value for key, value in changed.items() if key != "provenance_digest"})
        self.assertEqual(len(encode(changed)), len(encode(baseline)))
        self.assertNotIn("PRIVATE_", encode(changed).decode("utf-8"))
        expanded["external_history"][0]["lesson"] = "DIFFERENT_HISTORY"
        self.assertNotEqual(self.payload(context=expanded)["provenance_digest"], changed["provenance_digest"])
        self.assertEqual(len(encode(self.payload(context=expanded))), len(encode(baseline)))

    def test_provenance_binds_full_context_briefs_source_and_reviews_locally(self):
        value = self.payload()
        self.assertEqual(value["provenance_digest"], digest({"context": self.context, "source_stages": value["source_stages"], "briefs": self.briefs, "review_evidence": self.evidence}))
        self.assertRegex(value["provenance_digest"], r"^[a-f0-9]{64}$")
        self.assertNotEqual(self.payload(evidence={"changed": True})["provenance_digest"], value["provenance_digest"])
        reordered = dict(reversed(list(self.context.items())))
        self.assertEqual(self.payload(context=reordered), value)
        changed = copy.deepcopy(self.briefs[0])
        changed["family_plan"]["evidence"] = "CHANGED_PRIVATE_PLAN"
        self.write_brief(0, changed)
        after_brief = self.payload()
        self.assertEqual(after_brief["source_stages"], value["source_stages"])
        self.assertNotEqual(after_brief["provenance_digest"], value["provenance_digest"])
        path = self.root / self.run / "en/stage-4/quiz.md"
        path.write_bytes(path.read_bytes() + "完整\r\n".encode("utf-8"))
        after_source = self.payload()
        self.assertTrue(after_source["source_stages"][4]["quiz_md"].endswith("完整\r\n"))
        self.assertNotEqual(after_source["provenance_digest"], after_brief["provenance_digest"])

    def test_only_selected_typed_constraints_are_sent(self):
        value = self.payload()
        for stage, member in enumerate(value["source_stages"]):
            constraints = member["constraints"]
            expected = {"option_text_policies", "objective", "literacy_policy", "localization_risks"} | ({"extension"} if stage else set())
            self.assertEqual(set(constraints), expected)
            self.assertEqual(constraints["option_text_policies"], self.briefs[stage]["option_text_policies"])
            self.assertEqual(set(constraints["literacy_policy"]), {"assumed", "provided_here", "not_assessed"})
            if stage:
                self.assertEqual(constraints["extension"], {"mode": self.briefs[stage]["extension"]["mode"]})
        self.assertNotIn("PRIVATE_", encode(value).decode("utf-8"))
        self.write_brief(0, {})
        self.assertEqual(self.payload()["source_stages"][0]["constraints"], {"option_text_policies": {}})

    def test_malformed_option_policy_is_not_silently_dropped(self):
        for policies in (None, [], "learner", {"q": True}, {"q": ["learner"]}, {"q": {"policy": "learner"}}, {"q": "mixed"}, {"": "target"}):
            with self.subTest(policies=policies):
                self.write_brief(0, {"option_text_policies": policies})
                with self.assertRaisesRegex(TranslationError, "option_text_policies"):
                    self.payload()

    def test_selected_constraints_reject_wrong_types(self):
        for field, value in (("objective", {}), ("literacy_policy", "text"), ("literacy_policy", {"assumed": "script"}), ("localization_risks", "risk"), ("localization_risks", [{}])):
            with self.subTest(field=field, value=value):
                self.write_brief(0, {field: value})
                with self.assertRaisesRegex(TranslationError, field):
                    self.payload()

    def test_unknown_missing_reordered_duplicate_and_boolean_stages_rejected(self):
        identities = self.context["stages"]
        variants = (identities[:-1], identities + [{"stage": 5}], list(reversed(identities)), [identities[0]] * 5, None, {})
        for stages in variants:
            with self.subTest(stages=stages):
                context = {**self.context, "stages": stages}
                with self.assertRaises(TranslationError):
                    self.payload(context=context)
        for field, value in (("stage", False), ("stage", "0"), ("stage", 5), ("checkpoint", 21), ("base_checkpoint", 40), ("new_word_range", [True, 20]), ("prior_family_stages", ["stage:1"])):
            with self.subTest(field=field, value=value):
                context = copy.deepcopy(self.context)
                context["stages"][0][field] = value
                with self.assertRaises(TranslationError):
                    self.payload(context=context)
        for field in ("checkpoint", "base_checkpoint", "new_word_range", "prior_family_stages"):
            context = copy.deepcopy(self.context)
            del context["stages"][3][field]
            with self.assertRaises(TranslationError):
                self.payload(context=context)

    def test_unknown_return_modes_and_incomplete_sources_rejected(self):
        for extension in (None, {}, "practice", {"mode": "unknown"}):
            with self.subTest(extension=extension):
                self.write_brief(1, {"extension": extension})
                with self.assertRaisesRegex(TranslationError, "extension.mode"):
                    self.payload()
        self.write_brief(1, self.briefs[1])
        for stage, name, content in ((0, "lesson", b""), (1, "lesson", b" \n"), (4, "quiz", b""), (2, "lesson", b"new teaching")):
            path = self.root / self.run / f"en/stage-{stage}/{name}.md"
            original = path.read_bytes()
            path.write_bytes(content)
            with self.subTest(stage=stage, name=name), self.assertRaises(TranslationError):
                self.payload()
            path.write_bytes(original)
        path = self.root / self.run / "en/stage-4/quiz.md"
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(TranslationError, "UTF-8"):
            self.payload()
        path.unlink()
        with self.assertRaisesRegex(TranslationError, "complete stage 4"):
            self.payload()

    def test_locale_policy_excludes_study_source_and_unknown_locales(self):
        for locale in ("de", "en", "xx", None):
            with self.subTest(locale=locale), self.assertRaises(TranslationError):
                self.payload(locale=locale)
        for study in core.LOCALES:
            source = core.source_locale(study)
            run = f"family_runs/{study}/000020"
            context = {**self.context, "study_language": study, "source_locale": source}
            with patch("pathlib.Path.read_bytes", return_value=b"quiz"), patch.object(core, "read_json", return_value={"extension": {"mode": "extend"}}):
                locale = core.translation_locales(study)[0]
                value = build_payload(self.root, run, context, locale, self.evidence)
                body, _ = prepare(value, locale, "prompt")
                self.assertEqual(json.loads(body["input"][1]["content"])["learner_language"], locale)
                self.assertEqual(value["source_locale"], source)
                for excluded in (study, source):
                    with self.assertRaises(TranslationError):
                        build_payload(self.root, run, context, excluded, self.evidence)
        for field, value in (("study_language", "en"), ("source_locale", "es"), ("base_checkpoint", 40)):
            with self.subTest(field=field), self.assertRaises(TranslationError):
                self.payload(context={**self.context, field: value})

    def test_prompt_preserves_full_reference_assembly(self):
        expected = "Localized prompt — 完整"
        for name in ("GRAMMAR_MARKDOWN.md", "QUIZ_MARKDOWN.md", "conventions.json"):
            expected += "\n\nFORMAT REFERENCE " + name + "\nFull reference " + name
        self.assertEqual(build_prompt(self.root), expected)
        project = Path(__file__).resolve().parents[1]
        prompt = (project / "prompts/localize-family.md").read_text(encoding="utf-8")
        self.assertNotIn("brief.", prompt)
        self.assertNotIn("external_history", prompt)
        self.assertNotIn("snapshot", prompt)
        self.assertIn("constraints.option_text_policies", prompt)
        self.assertIn("not authoring a new lesson", prompt)
        self.assertIn("do not guess destinations", prompt)
        self.assertIn("Do not claim to have inspected past lessons", prompt)

    def test_request_measures_actual_utf8_bytes_and_never_truncates(self):
        value = self.payload()
        prompt = build_prompt(self.root)
        body, summary = prepare(value, "fr", prompt)
        wire = encode(body)
        self.assertEqual(summary["bytes"], len(wire))
        self.assertGreater(summary["bytes"], len(wire.decode("utf-8")))
        self.assertEqual(summary["hash"], digest(wire))
        self.assertIsNone(summary["output_cap"])
        self.assertNotIn("max_output_tokens", body)
        self.assertEqual(json.loads(body["input"][1]["content"]), {"learner_language": "fr", "context": value})
        exact_body, exact_summary = prepare(value, "fr", prompt, summary["bytes"])
        self.assertEqual((exact_body, exact_summary), (body, summary))
        with self.assertRaisesRegex(TranslationError, "nothing truncated"):
            prepare(value, "fr", prompt, summary["bytes"] - 1)
        self.assertEqual(self.payload(), value)
        for count in (1, 50):
            context = copy.deepcopy(self.context)
            context["external_history"] = [{"lesson_md": "PAST_ONLY" * 1000}] * count
            _, changed_summary = prepare(self.payload(context=context), "fr", prompt)
            self.assertEqual(changed_summary["bytes"], summary["bytes"])
            self.assertNotEqual(changed_summary["hash"], summary["hash"])


if __name__ == "__main__":
    unittest.main()
