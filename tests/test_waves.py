import unittest
from unittest.mock import patch

from lesson_factory import core, family
import test_family_core


class WaveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_family_core.FamilyCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def test_two_complete_blocks_have_only_family_base_waves(self):
        order = family.family_order(400)
        self.assertEqual([item["base_checkpoint"] for item in order], list(range(20, 201, 20)) + list(range(10, 200, 20)) + list(range(220, 401, 20)) + list(range(210, 400, 20)))
        self.assertEqual([item["authoring"]["wave"] for item in order], [1] * 10 + [2] * 10 + [1] * 10 + [2] * 10)
        self.assertEqual([item["authoring"]["order"] for item in order], list(range(1, 41)))

    def test_partial_tail_cannot_skip_first_wave(self):
        self.assertEqual([item["base_checkpoint"] for item in family.family_order(35)], [20, 10, 30])
        result = family.family_plan(self.root, "de", 10)
        self.assertIsNone(result["next_eligible"])
        self.assertFalse(result["families"][0]["eligible"])
        with self.assertRaisesRegex(core.AuthoringError, "earlier"):
            family.prepare_family(self.root, "de", 10)
        for through in (True, 0, -1, 1000000, 10.0):
            with self.assertRaises(core.AuthoringError):
                family.family_order(through)

    def test_infill_future_history_stays_planning_and_rejects_prerequisite(self):
        pack = self.fixture.publish()
        with patch.object(family, "_prefix", return_value=[]):
            self.fixture.run = family.prepare_family(self.root, "de", 10)
            context = family.load_family(self.root, self.fixture.run)
            self.assertEqual(context["stages"][0]["prior_external_keys"], [])
            self.assertEqual(context["stages"][0]["planning_external_keys"], [f"{pack}#stage:{stage}" for stage in range(5)])
            self.assertTrue(all(record["quiz"] for record in context["external_history"]))
            self.fixture.author()
            path = self.root / self.fixture.run / "stage-0/brief.json"
            brief = core.read_json(path)
            brief["prerequisites"] = [{"source": f"{pack}#stage:0", "justification": "Future"}]
            core.write_json(path, brief)
            with self.assertRaisesRegex(core.AuthoringError, "Prerequisites"):
                family.validate_family_briefs(self.root, self.fixture.run)
            brief["prerequisites"] = []
            brief["lexical_audit"][0]["taught_in"] = f"{pack}#stage:0"
            core.write_json(path, brief)
            with self.assertRaisesRegex(core.AuthoringError, "planning"):
                family.validate_family_briefs(self.root, self.fixture.run)

    def test_planning_dependency_tamper_rejected(self):
        pack = self.fixture.publish()
        run = family.prepare_family(self.root, "de", 40)
        context = family.load_family(self.root, run)
        self.assertEqual(context["stages"][0]["prior_external_keys"], [f"{pack}#stage:0"])
        self.assertEqual(context["stages"][0]["planning_external_keys"], [f"{pack}#stage:{stage}" for stage in range(1, 5)])
        (self.root / pack / "en/stage-4/quiz.md").write_text("Changed planning")
        with self.assertRaisesRegex(core.AuthoringError, "dependency"):
            family.load_family(self.root, run)


if __name__ == "__main__":
    unittest.main()
