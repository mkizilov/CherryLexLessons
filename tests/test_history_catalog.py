import contextlib
import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from lesson_factory import core, family, history_catalog
import test_family_core


def _tree(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class HistoryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_family_core.FamilyCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.fixture.publish()
        with patch.object(family, "_prefix", return_value=[]):
            self.run = family.prepare_family(self.root, "de", 30)
        self.prefix = patch.object(family, "_prefix", return_value=[])
        self.prefix.start()
        self.addCleanup(self.prefix.stop)

    def test_catalog_complete_ordered_eligibility_and_hashes(self):
        before = _tree(self.root)
        result = history_catalog.history_catalog(self.root, self.run, stage=0)
        self.assertEqual(_tree(self.root), before)
        self.assertEqual(result["total"], 5)
        self.assertIsNone(result["next_offset"])
        self.assertEqual([row["key"] for row in result["rows"]], [f"family_packs/de/000020#stage:{stage}" for stage in range(5)])
        self.assertEqual([row["eligibility"] for row in result["rows"]], ["prior", "planning", "planning", "planning", "planning"])
        self.assertEqual([row["unlock"] for row in result["rows"]], [20, 45, 95, 145, 245])
        row = result["rows"][0]
        self.assertEqual((row["topic"], row["objective"], row["source_locale"], row["selected_locale"]), ("Synthetic test", "Synthetic regression only", "en", "en"))
        self.assertIsNone(row["extension"])
        self.assertEqual(result["rows"][1]["extension"], "practice")
        self.assertEqual(row["missing_fields"], [])
        context = family.load_family(self.root, self.run)
        self.assertEqual(result["context_hash"], context["context_hash"])
        for name, file in row["files"].items():
            self.assertEqual(file["sha256"], hashlib.sha256((self.root / file["path"]).read_bytes()).hexdigest(), name)
        self.assertEqual(row["files"]["notes"]["path"], "family_packs/de/000020/en/stage-0/adaptation-notes.json")
        self.assertNotIn("lesson", row)
        self.assertNotIn("quiz", str(row.keys()))
        labels = history_catalog.history_catalog(self.root, self.run)["rows"][1]["eligibility"]
        self.assertEqual(labels, {"stage:0": "planning", "stage:1": "prior", "stage:2": "prior", "stage:3": "prior", "stage:4": "prior"})
        meta = result["family"]
        self.assertEqual((meta["study_language"], meta["source_locale"], meta["vocabulary_horizon"]), ("de", "en", 255))
        self.assertEqual(meta["locales"], context["locales"])
        self.assertEqual(meta["vocabulary_path"], "reference/vocabulary/German/german_vocabulary.csv")
        self.assertEqual({item["path"]: item["sha256"] for item in meta["references"]}[meta["vocabulary_path"]], hashlib.sha256((self.root / meta["vocabulary_path"]).read_bytes()).hexdigest())
        self.assertEqual([(item["unlock"], item["prior_count"], item["planning_count"]) for item in meta["stages"]], [(30, 1, 4), (55, 2, 3), (105, 3, 2), (155, 4, 1), (255, 5, 0)])
        self.assertEqual(meta["stages"][2]["own_prior_stages"], ["stage:0", "stage:1"])
        self.assertNotIn("words", meta)
        self.assertNotIn("dependencies", meta)
        current = history_catalog.history_catalog(self.root, self.run, stage=2)
        self.assertEqual(current["total"], 5)
        self.assertEqual([item["marker"] for item in current["current_stage"]["own_prior_stages"]], ["stage:0", "stage:1"])

    def test_pagination_stable_and_bounds(self):
        pages, offset = [], 0
        while offset is not None:
            page = history_catalog.history_catalog(self.root, self.run, offset=offset, limit=2)
            self.assertLessEqual(page["returned"], 2)
            pages.extend(row["key"] for row in page["rows"])
            offset = page["next_offset"]
        self.assertEqual(pages, [row["key"] for row in history_catalog.history_catalog(self.root, self.run, limit=100)["rows"]])
        self.assertEqual(len(set(pages)), 5)
        for kwargs in ({"limit": 0}, {"limit": -1}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": 5}, {"offset": "1"}, {"stage": 5}, {"stage": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(core.AuthoringError):
                history_catalog.history_catalog(self.root, self.run, **kwargs)

    def test_locale_switches_file_pointers_and_rejects_bad_locale(self):
        row = history_catalog.history_catalog(self.root, self.run, locale="fr", limit=1)["rows"][0]
        self.assertEqual(row["selected_locale"], "fr")
        self.assertEqual(row["files"]["quiz"]["path"], "family_packs/de/000020/fr/stage-0/quiz.md")
        self.assertEqual(row["files"]["brief"]["path"], "family_packs/de/000020/stage-0/brief.json")
        for locale in ("de", "xx", "../en"):
            with self.assertRaises(core.AuthoringError):
                history_catalog.history_catalog(self.root, self.run, locale=locale)

    def test_huge_objective_truncated_and_missing_metadata(self):
        context = copy.deepcopy(family.load_family(self.root, self.run))
        context["external_history"][0]["manifest"]["objective"] = "x" * 5000
        del context["external_history"][1]["manifest"]["topic"]
        del context["external_history"][1]["manifest"]["extension"]
        context["dependencies"].pop("family_packs/de/000020/en/stage-1/adaptation-notes.json")
        with patch.object(family, "load_family", return_value=context):
            rows = history_catalog.history_catalog(self.root, self.run)["rows"]
            entry = history_catalog.history_entry(self.root, self.run, rows[0]["key"])["entry"]
        self.assertEqual(rows[0]["truncated_fields"], ["objective"])
        self.assertLess(len(rows[0]["objective"]), 300)
        self.assertEqual(rows[0]["full_brief_path"], "family_packs/de/000020/stage-0/brief.json")
        self.assertEqual(rows[1]["missing_fields"], ["topic", "extension"])
        self.assertNotIn("topic", rows[1])
        self.assertEqual(rows[1]["files"]["notes"], {"path": "family_packs/de/000020/en/stage-1/adaptation-notes.json", "sha256": None, "status": "missing"})
        self.assertEqual(len(entry["claims"]["objective"]), 5000)

    def test_entry_metadata_and_bad_keys(self):
        before = _tree(self.root)
        result = history_catalog.history_entry(self.root, self.run, "family_packs/de/000020#stage:1", stage=0, locale="fr")
        self.assertEqual(_tree(self.root), before)
        entry = result["entry"]
        self.assertEqual(entry["eligibility"], "planning")
        self.assertEqual(entry["claims"]["extension"]["mode"], "practice")
        self.assertEqual(entry["claims"]["taught_form_claims"], [])
        self.assertEqual(entry["lexical_audit_count"], 1)
        self.assertNotIn("lexical_audit", entry["claims"])
        self.assertEqual(entry["missing_claim_fields"], ["curriculum"])
        self.assertEqual(entry["files"]["lesson"]["path"], "family_packs/de/000020/fr/stage-1/lesson.md")
        self.assertNotIn("question_ids", entry)
        for key in ("family_packs/de/000020#stage:9", "family_packs/de/000020/fr/stage-1/lesson.md", "../etc/passwd", "/etc/passwd", 7):
            with self.subTest(key=key), self.assertRaisesRegex(core.AuthoringError, "key"):
                history_catalog.history_entry(self.root, self.run, key)
        with self.assertRaises(core.AuthoringError):
            history_catalog.history_catalog(self.root, "../family_runs/de/000030")

    def test_drift_and_pinned_hash_fail_closed(self):
        with self.assertRaisesRegex(core.AuthoringError, "Pinned context hash"):
            history_catalog.history_catalog(self.root, self.run, context_hash="0" * 64)
        (self.root / "family_packs/de/000020/fr/stage-0/adaptation-notes.json").write_text("{}")
        with self.assertRaises(core.AuthoringError):
            history_catalog.history_catalog(self.root, self.run)
        with self.assertRaises(core.AuthoringError):
            history_catalog.history_entry(self.root, self.run, "family_packs/de/000020#stage:0")

    def test_cli(self):
        path = Path(__file__).resolve().parents[1] / "tools/history.py"
        spec = importlib.util.spec_from_file_location("history_cli_catalog", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "ROOT", self.root), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(["--run", self.run, "--catalog", "--stage", "0", "--offset", "1", "--limit", "2"]), 0)
            self.assertEqual(module.main(["--run", self.run, "--key", "family_packs/de/000020#stage:0"]), 0)
            self.assertEqual(module.main(["--run", self.run, "--catalog", "--key", "family_packs/de/000020#stage:0", "--locale", "fr"]), 0)
        self.assertIn('"selected_locale": "fr"', output.getvalue())
        self.assertIn('"next_offset": 3', output.getvalue())
        self.assertIn('"mode": "entry"', output.getvalue())
        with patch.object(module, "ROOT", self.root), contextlib.redirect_stderr(io.StringIO()):
            for args in (["--run", self.run, "--catalog", "--key", "x", "--limit", "2"], ["--run", self.run, "--stage", "0", "--limit", "2"], ["--language", "de", "--catalog"], ["--run", self.run, "--catalog", "--detail"], ["--run", self.run, "--catalog", "--limit", "0"]):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    module.main(args)


if __name__ == "__main__":
    unittest.main()
