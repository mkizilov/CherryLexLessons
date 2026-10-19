import contextlib
import csv
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from lesson_factory import core, family, history_index
import test_family_core


class HistoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_family_core.FamilyCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.run = self.fixture.run

    def test_rank30_exact_prior_and_planning_window(self):
        self.fixture.publish()
        with patch.object(family, "_prefix", return_value=[]):
            run = family.prepare_family(self.root, "de", 30)
            result = history_index.snapshot_index(self.root, run, 0)
        self.assertEqual([row["key"] for row in result["prior"]], ["family_packs/de/000020#stage:0"])
        self.assertEqual([row["checkpoint"] for row in result["planning"]], [45, 95, 145, 245])
        self.assertEqual([row["key"] for row in result["planning"]], [f"family_packs/de/000020#stage:{stage}" for stage in range(1, 5)])
        self.assertEqual(result["own_prior"], [])
        self.assertEqual(result["prior"][0]["claims"]["topic"], "Synthetic test")
        self.assertEqual(result["prior"][0]["question_count"], 25)

    def test_stage4_only_own_prior_absent_drafts_and_missing_claims(self):
        result = history_index.snapshot_index(self.root, self.run, 4)
        self.assertEqual([row["key"] for row in result["own_prior"]], [f"{self.run}#stage:{stage}" for stage in range(4)])
        self.assertTrue(all(row["authorship"] == "not-authored" for row in result["own_prior"]))
        self.assertEqual(result["planning"], [])
        self.assertEqual(result["own_prior"][0]["files"]["brief"]["status"], "not-authored")
        self.fixture.author()
        brief = self.root / self.run / "stage-0/brief.json"
        core.write_json(brief, {})
        result = history_index.snapshot_index(self.root, self.run, 4)
        self.assertEqual(result["own_prior"][0]["missing_claim_fields"], ["topic", "objective", "curriculum", "lexical_audit"])
        self.assertEqual(result["context_hash"], family.load_family(self.root, self.run)["context_hash"])
        self.assertFalse((self.root / "family-registry.sqlite").exists())
        self.assertFalse((self.root / "registry.sqlite").exists())

    def test_english_source_spanish_and_explicit_locale(self):
        folder = self.root / "reference/vocabulary/English"
        folder.mkdir()
        with (folder / "english_vocabulary.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Rank", "Word"])
            writer.writerows((rank, f"word{rank}") for rank in range(1, 246))
        run = family.prepare_family(self.root, "en", 20)
        result = history_index.snapshot_index(self.root, run, 1)
        self.assertEqual(result["source_locale"], "es")
        self.assertEqual(result["own_prior"][0]["selected_locale"], "es")
        result = history_index.snapshot_index(self.root, run, 1, "fr")
        self.assertEqual(result["own_prior"][0]["selected_locale"], "fr")
        self.assertEqual(result["own_prior"][0]["source_locale"], "es")
        with self.assertRaises(core.AuthoringError):
            history_index.snapshot_index(self.root, run, 0, "en")
        context = family.load_family(self.root, run)
        record = {**core.stage_identity(10), "key": "packs/en/000010", "path": "packs/en/000010", "source_locale": "en"}
        context["external_history"] = [record]
        context["stages"][0]["prior_external_keys"] = [record["key"]]
        with patch.object(family, "load_family", return_value=context), self.assertRaisesRegex(core.AuthoringError, "finalized family"):
            history_index.snapshot_index(self.root, run, 0)

    def test_drift_symlink_and_invalid_inputs(self):
        for stage in (-1, 5, True, "0"):
            with self.assertRaises(core.AuthoringError):
                history_index.snapshot_index(self.root, self.run, stage)
        for locale in ("unknown", "de", "../en"):
            with self.assertRaises(core.AuthoringError):
                history_index.snapshot_index(self.root, self.run, 0, locale)
        link = self.root / self.run / "link"
        link.symlink_to(self.root / "reference/formats/spec.txt")
        with self.assertRaisesRegex(core.AuthoringError, "Symlink"):
            history_index.snapshot_index(self.root, self.run, 0)
        link.unlink()
        (self.root / "reference/formats/spec.txt").write_text("tamper")
        with self.assertRaisesRegex(core.AuthoringError, "dependency"):
            history_index.snapshot_index(self.root, self.run, 0)

    def test_context_tamper_and_bounded_read(self):
        path = self.root / self.run / "family-context.json"
        context = core.read_json(path)
        context["knowledge_policy"] = "tampered"
        core.write_json(path, context)
        with self.assertRaisesRegex(core.AuthoringError, "snapshot"):
            history_index.snapshot_index(self.root, self.run, 0)
        with patch.object(history_index, "MAX_BYTES", 8):
            with self.assertRaisesRegex(core.AuthoringError, "byte limit"):
                history_index.snapshot_index(self.root, self.run, 0)

    def test_strict_headings_order_duplicates_and_fences(self):
        text = "# q-2\n```quiz\n# fake\n```\n# q-1\n# q-1\n~~~\n# hidden {#hidden}\n~~~\n# Title {#real-page}\n"
        self.assertEqual(history_index._headings(text), ["q-2", "q-1", "q-1"])
        self.assertEqual(history_index._headings(text, True), ["real-page"])

    def test_horizon_finite_prefix(self):
        for count, total, last, blocked, required in ((8000, 768, 7760, 7780, 8005), (10000, 968, 9760, 9780, 10005), (245, 1, 20, 40, 265)):
            with self.subTest(count=count), patch.object(core, "vocabulary", return_value=[{}] * count):
                result = history_index.horizon_report(self.root, "de")
                self.assertEqual(result["supported_prefix_families"], total)
                self.assertEqual(result["last_authored_base"], last)
                self.assertEqual(result["first_blocked_base"], blocked)
                self.assertEqual(result["first_blocked_required_rank"], required)
                self.assertEqual(result["last_learner_unlock"], last + 225)
                self.assertEqual(result["theoretical_last_eligible_base"], (count - 225) // 10 * 10)
                self.assertEqual(result["eligible_but_blocked_tail"], [10] if count == 245 else list(range((last // 200) * 200 + 10, (count - 225) // 10 * 10 + 1, 20)))
        for count in (0, 1000000):
            with patch.object(core, "vocabulary", return_value=[{}] * count), self.assertRaises(core.AuthoringError):
                history_index.horizon_report(self.root, "de")
        self.assertFalse((self.root / "family-registry.sqlite").exists())

    def test_cli_modes(self):
        path = Path(__file__).resolve().parents[1] / "tools/history.py"
        spec = importlib.util.spec_from_file_location("history_cli", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "ROOT", self.root), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(["--run", self.run, "--stage", "4"]), 0)
        self.assertIn('"own_prior"', output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            for args in (["--run", self.run], ["--language", "de"], ["--language", "de", "--horizon", "--stage", "0"]):
                with self.assertRaises(SystemExit):
                    module.main(args)


if __name__ == "__main__":
    unittest.main()
