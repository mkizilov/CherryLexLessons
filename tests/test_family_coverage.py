import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lesson_factory import core, family
from tools import coverage


class FamilyCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.code = "ru"

    def put(self, path, value):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def claim(self, page="page-0", question="q-0"):
        return {"topic_id": "usage", "intent": "practice", "teaching_pages": [page], "question_ids": [question]}

    def bundle(self, base=20, claims=None, external=(), prior=None, page_prefix="page", empty_stages=()):
        path = f"family_packs/{self.code}/{base:06d}"
        source = core.source_locale(self.code)
        stages = []
        history = []
        for key in external:
            history.append({"key": key})
        for stage in range(5):
            identity = core.stage_identity(base, stage)
            stages.append({**identity, "new_word_range": core.word_range(identity), "prior_external_keys": list(prior.get(stage, []) if prior else []), "planning_external_keys": [], "prior_family_stages": [f"stage:{item}" for item in range(stage)]})
            brief = {"curriculum": [self.claim(f"{page_prefix}-{stage}", f"q-{stage}")]}
            if claims and stage in claims:
                brief["curriculum"] = claims[stage]
            self.put(f"{path}/stage-{stage}/brief.json", brief)
            for locale in core.learner_locales(self.code):
                locale_prefix = page_prefix if locale == source else f"localized-{locale}"
                question_prefix = "q" if locale == source else f"localized-{locale}-q"
                self.put(f"{path}/{locale}/stage-{stage}/lesson.md", "" if stage in empty_stages else f"# Teaching {{#{locale_prefix}-{stage}}}\n\nContent.\n")
                self.put(f"{path}/{locale}/stage-{stage}/quiz.md", f"# {question_prefix}-{stage}\n\n```quiz\n{{}}\n```\n")
                self.put(f"{path}/{locale}/stage-{stage}/adaptation-notes.json", [])
        self.put("reference/grammar/grammar.sqlite", "synthetic dependency")
        dependencies = {"reference/grammar/grammar.sqlite": core.digest(self.root / "reference/grammar/grammar.sqlite")}
        context = {"schema": 5, "study_language": self.code, "base_checkpoint": base, "source_locale": source, "locales": list(core.learner_locales(self.code)), "stages": stages, "external_history": history, "dependencies": dependencies, "registry_facts": {"families": []}}
        context["context_hash"] = family._hash(context)
        self.put(f"{path}/family-context.json", context)
        self.put(f"{path}/shared-findings.json", [])
        folder = self.root / path
        binding = {"context_hash": context["context_hash"], "brief_digests": {f"stage:{stage}": core.digest(folder / f"stage-{stage}/brief.json") for stage in range(5)}, "source_digests": family._folder_digests(folder, source)}
        roles = core.review_roles(self.code)
        for role in roles + ["final"]:
            expected = dict(binding)
            if role.startswith("language:"):
                expected["locale_digests"] = family._folder_digests(folder, role.split(":", 1)[1])
            if role == "final":
                expected.update({"locale_digests": {locale: family._folder_digests(folder, locale) for locale in context["locales"]}, "findings_digest": core.digest(folder / "shared-findings.json"), "reviews_digest": family._hash({review: core.digest(folder / "reviews" / (review.replace(":", "-") + ".json")) for review in roles})})
            self.put(f"{path}/reviews/{role.replace(':', '-')}.json", {"role": role, "status": "approved", "binding": expected, "reviewer_session": f"synthetic-{role}", "checklist": ["synthetic fixture"], "findings": ["synthetic fixture"], "stages": [{"stage": stage} for stage in range(5)]})
        manifest = {"schema": 5, "study_language": self.code, "base_checkpoint": base, "source_locale": source, "locales": list(core.learner_locales(self.code)), "return_offsets": list(core.RETURN_OFFSETS), "family_path": path, "context_hash": context["context_hash"], "stages": stages, "registry_facts": context["registry_facts"], "dependencies": dependencies, "locale_digests": {locale: family._folder_digests(folder, locale) for locale in context["locales"]}, "files": family._inventory(self.root, self.root / path)}
        self.put(f"{path}/manifest.json", manifest)
        with family.family_registry(self.root) as db:
            family._register(db, self.code, base, path, manifest)
        return path

    def report(self, through=20):
        roadmap = f"work/curriculum/{self.code}.json"
        self.put(roadmap, {"study_language": self.code, "topics": [{"id": "usage"}]})
        return coverage.report(self.root, self.code, through, roadmap)

    def test_early_rank_excludes_four_future_stages(self):
        path = self.bundle()
        result = self.report()
        self.assertEqual([item["pack"] for item in result["claims"]], [f"{path}#stage:0"])
        self.assertEqual(result["learner_stage_count"], 1)
        self.assertEqual(result["future_finalized_planning_pack_count"], 4)
        self.assertEqual(result["source_unit_count"], 1)
        self.assertEqual(result["family_bundle_count"], 1)

    def test_end_horizon_counts_five_truthfully_labeled_stages(self):
        path = self.bundle()
        result = self.report(245)
        self.assertEqual(result["learner_pack_count"], 5)
        self.assertEqual(result["future_finalized_planning_pack_count"], 0)
        self.assertEqual([item["pack"] for item in result["claims"]], [f"{path}#stage:{stage}" for stage in range(5)])
        self.assertTrue(all(item["label"] == coverage.LABEL and not item["warnings"] for item in result["claims"]))

    def test_own_prior_page_is_eligible(self):
        path = self.bundle(claims={1: [self.claim(question="q-1")]}, empty_stages=[1])
        claim = self.report(45)["claims"][1]
        self.assertEqual(claim["page_provenance"], {"page-0": [f"{path}#stage:0"]})
        self.assertEqual(claim["missing_page_ids"], [])

    def test_own_future_page_is_not_eligible(self):
        self.bundle(claims={0: [self.claim("page-4")]})
        self.assertEqual(self.report()["claims"][0]["missing_page_ids"], ["page-4"])

    def test_external_future_reference_rejected_even_in_original_keys(self):
        future = self.bundle(40, page_prefix="future")
        key = f"{future}#stage:0"
        self.bundle(claims={0: [self.claim("future-0")]}, external=[key], prior={0: [key]})
        result = self.report()
        self.assertTrue(any(f"ineligible or unregistered prior pack {key}" in warning for warning in result["warnings"]))
        self.assertEqual(result["claims"][0]["missing_page_ids"], ["future-0"])
        self.assertEqual(result["claims"][0]["page_provenance"]["future-0"], [])

    def test_later_infill_cannot_supply_posthoc_teaching(self):
        self.bundle(claims={0: [self.claim("infill-0")]})
        infill = self.bundle(10, page_prefix="infill")
        self.assertEqual(self.report()["claims"][1]["missing_page_ids"], ["infill-0"])
        self.assertNotIn(f"{infill}#stage:0", self.report()["claims"][1]["page_provenance"]["infill-0"])

    def test_external_key_absent_from_original_history_is_ineligible(self):
        earlier = self.bundle(10, page_prefix="earlier")
        key = f"{earlier}#stage:0"
        self.bundle(claims={0: [self.claim("earlier-0")]}, prior={0: [key]})
        result = self.report()
        self.assertEqual(result["claims"][1]["missing_page_ids"], ["earlier-0"])
        self.assertTrue(any("absent original external key" in warning for warning in result["warnings"]))

    def test_history_presence_without_prior_permission_is_not_teaching(self):
        earlier = self.bundle(10, page_prefix="earlier")
        key = f"{earlier}#stage:0"
        self.bundle(claims={0: [self.claim("earlier-0")]}, external=[key])
        self.assertEqual(self.report()["claims"][1]["missing_page_ids"], ["earlier-0"])

    def test_empty_registry_report_does_not_create_databases(self):
        result = self.report()
        self.assertEqual(result["source_unit_count"], 0)
        self.assertFalse((self.root / "registry.sqlite").exists())
        self.assertFalse((self.root / "family-registry.sqlite").exists())

    def test_original_external_prior_page_is_eligible(self):
        prior = self.bundle(20)
        key = f"{prior}#stage:0"
        current = self.bundle(40, claims={0: [self.claim("page-0")]}, external=[key], prior={0: [key]})
        claim = next(item for item in self.report(40)["claims"] if item["pack"] == f"{current}#stage:0")
        self.assertEqual(claim["page_provenance"]["page-0"], [f"{current}#stage:0", key])
        self.assertIn("ambiguous page provenance", claim["warnings"][0])

    def test_english_study_uses_spanish_source(self):
        self.code = "en"
        path = self.bundle()
        result = self.report()
        self.assertEqual(result["claims"][0]["missing_page_ids"], [])
        self.assertEqual(result["claims"][0]["missing_question_ids"], [])
        self.assertTrue((self.root / path / "es/stage-0/lesson.md").is_file())
        self.assertFalse((self.root / path / "en").exists())

    def test_corrupt_bundle_fails_closed(self):
        path = self.bundle()
        self.put(f"{path}/stage-4/brief.json", {"curriculum": []})
        with self.assertRaisesRegex(core.AuthoringError, "inventory or bytes changed"):
            self.report()

    def test_family_bounds_fail_without_truncation(self):
        self.bundle()
        with patch.object(coverage, "MAX_PACKS", 4):
            with self.assertRaisesRegex(core.AuthoringError, "no truncation performed"):
                self.report()


if __name__ == "__main__":
    unittest.main()
