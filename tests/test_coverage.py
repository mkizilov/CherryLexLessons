import unittest
from unittest.mock import patch

from lesson_factory import core
from tools import coverage
import test_family_coverage


class CoverageTests(unittest.TestCase):
    setUp = test_family_coverage.FamilyCoverageTests.setUp
    put = test_family_coverage.FamilyCoverageTests.put
    bundle = test_family_coverage.FamilyCoverageTests.bundle
    claim = test_family_coverage.FamilyCoverageTests.claim

    def report(self, through=20):
        roadmap = f"work/curriculum/{self.code}.json"
        self.put(roadmap, {"study_language": self.code, "topics": [{"id": "usage", "priority": "essential", "review_by_rank": 20}]})
        return coverage.report(self.root, self.code, through, roadmap)

    def test_missing_ids_and_unknown_claims_are_warnings(self):
        claim = {**self.claim(), "topic_id": "unknown", "intent": "mastered", "teaching_pages": ["absent"], "question_ids": ["absent"]}
        self.bundle(claims={0: [claim, "unknown"]})
        result = self.report()
        claim = result["claims"][0]
        self.assertEqual(claim["missing_page_ids"], ["absent"])
        self.assertEqual(claim["missing_question_ids"], ["absent"])
        self.assertEqual(len(claim["warnings"]), 4)
        self.assertIn("unknown claim shape", result["warnings"][0])

    def test_optional_missing_claims_do_not_assert_missing_teaching(self):
        self.bundle(claims={0: []})
        result = self.report()
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["topics"][0]["label"], "no-finalized-learner-claim")
        self.assertTrue(result["topics"][0]["review_due"])
        self.assertIn("not evidence", result["warnings"][0])

    def test_missing_roadmap_is_error_without_creating_registry(self):
        with self.assertRaisesRegex(core.AuthoringError, "Missing curriculum roadmap"):
            coverage.report(self.root, "ru", 20, "absent.json")
        self.assertFalse((self.root / "family-registry.sqlite").exists())

    def test_stable_labels_do_not_assert_coverage_or_mastery(self):
        self.bundle()
        result = self.report()
        self.assertEqual(result["claims"][0]["label"], coverage.LABEL)
        self.assertEqual(result["topics"][0]["label"], coverage.LABEL)
        self.assertEqual(result["claims"][0]["warnings"], [])
        self.assertNotIn("covered", result["claims"][0])
        self.assertIn("not linguistic coverage or learner mastery", result["scope"])
        self.assertEqual(result, self.report())

    def test_report_preserves_all_existing_bytes(self):
        self.bundle()
        self.report()
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        coverage.report(self.root, "ru", 20, "work/curriculum/ru.json")
        self.assertEqual(before, {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_fenced_headings_are_not_references(self):
        text = "```markdown\n# fake\n# Fake {#fake-page}\n````\n~~~markdown\n# false\n# False {#false-page}\n~~~\n# real\n# Real {#real-page}\n"
        self.assertEqual(coverage.heading_ids(text), {"real"})
        self.assertEqual(coverage.heading_ids(text, pages=True), {"real-page"})
        self.assertEqual(coverage.heading_ids("```\n# unclosed\n"), set())

    def test_reference_and_input_bounds_fail_without_truncation(self):
        self.bundle(claims={0: [{**self.claim(), "question_ids": ["q-0"] * 101}]})
        with self.assertRaisesRegex(core.AuthoringError, "no truncation performed"):
            self.report()
        with patch.object(coverage, "MAX_BYTES", 8):
            with self.assertRaisesRegex(core.AuthoringError, "no truncation performed"):
                self.report()

    def test_topic_and_claim_bounds_fail_closed(self):
        self.bundle(claims={0: [self.claim()] * 101})
        with self.assertRaisesRegex(core.AuthoringError, "excessive curriculum"):
            self.report()
        with patch.object(coverage, "MAX_TOPICS", 0):
            with self.assertRaisesRegex(core.AuthoringError, "no truncation performed"):
                self.report()

    def test_missing_topic_id_is_warning(self):
        self.bundle(claims={0: [{**self.claim(), "topic_id": None}]})
        self.assertIn("missing or unknown roadmap topic_id", self.report()["claims"][0]["warnings"])

    def test_invalid_reference_shapes_are_warnings(self):
        self.bundle(claims={0: [{**self.claim(), "teaching_pages": None, "question_ids": [False]}]})
        warnings = self.report()["claims"][0]["warnings"]
        self.assertIn("teaching_pages requires nonempty string IDs", warnings)
        self.assertIn("question_ids requires nonempty string IDs", warnings)

    def test_roadmap_paths_and_invalid_ranks_fail_read_only(self):
        for roadmap in ("../roadmap.json", str(self.root / "roadmap.json")):
            with self.assertRaises(core.AuthoringError):
                coverage.report(self.root, "ru", 20, roadmap)
        self.put("work/curriculum/ru.json", {"study_language": "ru", "topics": []})
        for rank in (True, 0, -1, 20.0):
            with self.assertRaises(core.AuthoringError):
                coverage.report(self.root, "ru", rank, "work/curriculum/ru.json")
        self.assertFalse((self.root / "family-registry.sqlite").exists())

    def test_roadmap_identity_and_duplicate_topics_rejected(self):
        for value in ({"study_language": "de", "topics": []}, {"study_language": "ru", "topics": [{"id": "usage"}, {"id": "usage"}]}):
            self.put("work/curriculum/ru.json", value)
            with self.assertRaises(core.AuthoringError):
                coverage.report(self.root, "ru", 20, "work/curriculum/ru.json")


if __name__ == "__main__":
    unittest.main()
