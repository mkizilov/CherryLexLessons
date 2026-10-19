import fcntl
import shutil
import unittest
from unittest.mock import patch

from lesson_factory import core, family
import test_family_core


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_family_core.FamilyCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.run = self.fixture.run

    def brief(self, stage, change):
        path = self.root / self.run / f"stage-{stage}/brief.json"
        value = core.read_json(path)
        change(value)
        core.write_json(path, value)

    def test_four_returns_windows_and_same_family_prior_only(self):
        context = family.load_family(self.root, self.run)
        self.assertEqual([stage["checkpoint"] for stage in context["stages"]], [20, 45, 95, 145, 245])
        self.assertEqual([stage["new_word_range"] for stage in context["stages"]], [[1, 20], [21, 45], [46, 95], [96, 145], [146, 245]])
        self.assertEqual(context["stages"][4]["prior_family_stages"], ["stage:0", "stage:1", "stage:2", "stage:3"])
        self.assertEqual(context["vocabulary_horizon"], 245)
        self.assertEqual(len(context["words"]), 245)

    def test_practice_requires_empty_lesson_and_no_current_teaching(self):
        self.fixture.author()
        self.brief(1, lambda brief: brief["lexical_audit"][0].update(taught_in="this"))
        with self.assertRaisesRegex(core.AuthoringError, "practice-only this"):
            family.validate_family_briefs(self.root, self.run)
        self.brief(1, lambda brief: brief["lexical_audit"][0].pop("taught_in"))
        (self.root / self.run / "en/stage-1/lesson.md").write_text("Teaching")
        with self.assertRaisesRegex(core.AuthoringError, "Extension mode"):
            family.validate_family_briefs(self.root, self.run)

    def test_audited_rank_requires_exact_lexical_form(self):
        self.fixture.author()
        self.brief(0, lambda brief: brief["lexical_audit"][0].update(target_form="1"))
        with self.assertRaisesRegex(core.AuthoringError, "canonical ranked"):
            family.validate_family_briefs(self.root, self.run)

    def test_target_translation_contract_blocks_publication(self):
        self.fixture.author()
        self.fixture.approve()
        def changed(root, run, locale=None, **kwargs):
            result = self.fixture.validator(root, run, locale, **kwargs)
            if locale == "fr":
                result["family_contract"] = ["changed-target"]
            return result
        with patch.object(family, "validate_family", side_effect=changed), self.assertRaisesRegex(core.AuthoringError, "meaning contract"):
            family.finalize_family(self.root, self.run)
        self.assertTrue((self.root / self.run).exists())
        self.assertFalse((self.root / "family-registry.sqlite").exists())

    def test_final_review_binds_other_reviews(self):
        self.fixture.author()
        self.fixture.approve()
        path = family.family_review_path(self.root, self.run, "pedagogical")
        report = core.read_json(path)
        report["findings"] = ["Changed assessment"]
        core.write_json(path, report)
        with self.assertRaisesRegex(core.AuthoringError, "stale"):
            family.require_family_review(self.root, self.run, "final")

    def test_language_review_binds_source(self):
        self.fixture.author()
        self.fixture.approve()
        path = self.root / self.run / "en/stage-4/quiz.md"
        path.write_text(path.read_text() + "Source revision")
        with self.assertRaisesRegex(core.AuthoringError, "stale"):
            family.require_family_review(self.root, self.run, "language:fr")

    def test_shared_findings_acknowledgments_required(self):
        self.fixture.author()
        self.fixture.approve()
        path = self.root / self.run / "shared-findings.json"
        findings = [{"status": "resolved", "resolution": "Correction inspected"}]
        core.write_json(path, findings)
        with self.assertRaisesRegex(core.AuthoringError, "acknowledgments"):
            family._findings(self.root, self.run)
        findings[0]["locale_acknowledgments"] = {locale: "Inspected" for locale in core.translation_locales("de")}
        core.write_json(path, findings)
        self.assertEqual(family._findings(self.root, self.run), findings)

    def test_review_and_publication_refuse_translation_worker(self):
        self.fixture.author()
        path = self.root / "work/translations/families/de/000020/fr/worker.lock"
        path.parent.mkdir(parents=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(core.AuthoringError, "worker is active"):
                family.finalize_family(self.root, self.run)
            binding = family.family_binding(self.root, self.run, "pedagogical")
            with self.assertRaisesRegex(core.AuthoringError, "worker is active"):
                family.submit_family_review(self.root, self.run, "pedagogical", {"status": "approved", "reviewer_session": "synthetic", "checklist": ["test"], "findings": ["test"], "binding": binding})

    def test_finalized_extra_files_and_same_language_directory_rejected(self):
        pack = self.fixture.publish()
        folder = self.root / pack
        registry_before = core.digest(self.root / "family-registry.sqlite")
        extra = folder / "extra.txt"
        extra.write_text("unexpected")
        with self.assertRaisesRegex(core.AuthoringError, "bytes changed"):
            family.verify_family_pack(self.root, family.family_registry_facts(self.root, "de")[0])
        extra.unlink()
        (folder / "de").mkdir()
        with self.assertRaisesRegex(core.AuthoringError, "Same-language"):
            family.verify_family_pack(self.root, family.family_registry_facts(self.root, "de")[0])
        self.assertEqual(core.digest(self.root / "family-registry.sqlite"), registry_before)

    def test_english_source_and_source_binding_are_spanish(self):
        folder = self.root / "reference/vocabulary/English"
        folder.mkdir()
        shutil.copyfile(self.root / "reference/vocabulary/German/german_vocabulary.csv", folder / "english_vocabulary.csv")
        run = family.prepare_family(self.root, "en", 20)
        self.assertEqual(family.load_family(self.root, run)["source_locale"], "es")
        self.assertNotIn("en", family.load_family(self.root, run)["locales"])
        for stage in family.STAGES:
            core.write_json(self.root / run / f"stage-{stage}/brief.json", {"synthetic": True})
            for locale in ("es", "fr"):
                stage_folder = family.family_locale_path(self.root, run, locale, stage)
                (stage_folder / "lesson.md").write_text("Synthetic source" if stage == 0 else "")
                (stage_folder / "quiz.md").write_text("Synthetic quiz")
                core.write_json(stage_folder / "adaptation-notes.json", [])
        source = family.family_pair_digests(self.root, run, "es")
        self.assertEqual(family.family_binding(self.root, run, "pedagogical")["source_digests"], source)
        self.assertEqual(family.family_binding(self.root, run, "language:fr")["source_digests"], source)
        with self.assertRaises(core.AuthoringError):
            family.family_locale_path(self.root, run, "en", 0)


if __name__ == "__main__":
    unittest.main()
