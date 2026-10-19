import csv
import fcntl
import shutil
import sys
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lesson_factory import core, family, family_cli


class FamilyCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        vocabulary = self.root / "reference/vocabulary/German"
        vocabulary.mkdir(parents=True)
        with (vocabulary / "german_vocabulary.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Rank", "Word"])
            writer.writerows((rank, f"wort{rank}") for rank in range(1, 701))
        (self.root / "reference/formats").mkdir()
        (self.root / "reference/formats/spec.txt").write_text("Synthetic format fixture")
        (self.root / "reference/grammar").mkdir()
        (self.root / "reference/grammar/grammar.sqlite").write_bytes(b"Synthetic grammar fixture")
        self.run = family.prepare_family(self.root, "de", 20)
        self.validation_mock = patch.object(family, "validate_family", self.validator)
        self.validation_mock.start()
        self.addCleanup(self.validation_mock.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def author(self):
        for stage in family.STAGES:
            brief = {"topic": "Synthetic test", "objective": "Synthetic regression only", "sources": [{"reference": "synthetic:test", "justification": "Offline fixture"}], "prerequisites": [], "lexical_audit": [{"target_form": "wort1", "known_rank": 1, "justification": "Synthetic canonical test"}]}
            if stage:
                brief["extension"] = {"mode": "practice", "rationale": "Synthetic retrieval test"}
            core.write_json(self.root / self.run / f"stage-{stage}/brief.json", brief)
            for locale in core.learner_locales("de"):
                folder = family.family_locale_path(self.root, self.run, locale, stage)
                (folder / "lesson.md").write_text("" if stage else "Synthetic teaching wort1")
                (folder / "quiz.md").write_text("\n".join(f"# q-{stage}-{index}\nSynthetic item" for index in range(25)))
                core.write_json(folder / "adaptation-notes.json", {"synthetic_fixture": True})

    def report(self, role):
        stages = []
        for stage in family.STAGES:
            stages.append({"stage": stage, "checklist": ["Synthetic check"], "findings": ["Synthetic test report, not genuine review"], "question_checks": [{"id": f"q-{stage}-{index}", "status": "approved", "rationale": "Synthetic per-item evidence", "answer": "wort1", "alternatives": "Synthetic alternatives", "teaching_page": "synthetic-page"} for index in range(25)]})
        return {"status": "approved", "reviewer_session": f"synthetic:{role}", "checklist": ["Synthetic fixture"], "findings": ["Not genuine content approval"], "stages": stages, "binding": family.family_binding(self.root, self.run, role)}

    def approve(self):
        for role in core.review_roles("de") + ["final"]:
            family.submit_family_review(self.root, self.run, role, self.report(role))

    def validator(self, *args, **kwargs):
        family.validate_family_briefs(args[0], args[1])
        return {"ok": True, "family_contract": [{"stage": stage} for stage in family.STAGES], "stages": [{"stage": stage, "contract": {"questions": [{"id": f"q-{stage}-{index}"} for index in range(25)]}} for stage in family.STAGES], "totals": {"questions": 125, "pages": 1}}

    def publish(self):
        self.author()
        self.approve()
        with patch.object(family, "validate_family", self.validator):
            return family.finalize_family(self.root, self.run)

    def test_order_full_horizon_and_scaffold(self):
        self.assertEqual([item["base_checkpoint"] for item in family.family_order(240)], list(range(20, 201, 20)) + list(range(10, 200, 20)) + [220, 240, 210, 230])
        context = family.load_family(self.root, self.run)
        self.assertEqual([item["checkpoint"] for item in context["stages"]], [20, 45, 95, 145, 245])
        self.assertEqual(len(context["words"]), 245)
        self.assertEqual(context["stages"][4]["prior_family_stages"], ["stage:0", "stage:1", "stage:2", "stage:3"])
        self.assertEqual(len(family.family_locale_scaffold(self.root, self.run, "fr")), 4)
        self.assertEqual(family.prepare_family(self.root, "German", 20), self.run)
        with self.assertRaisesRegex(core.AuthoringError, "earlier"):
            family.prepare_family(self.root, "de", 40)
        with self.assertRaisesRegex(core.AuthoringError, "full vocabulary horizon"):
            family.prepare_family(self.root, "de", 500)
        with self.assertRaises(core.AuthoringError):
            family.load_family(self.root, "family_runs/de/../000020")

    def test_addition_and_context_drift(self):
        (self.root / "reference/formats/new.txt").write_text("addition")
        with self.assertRaisesRegex(core.AuthoringError, "inventory"):
            family.load_family(self.root, self.run)
        (self.root / "reference/formats/new.txt").unlink()
        (self.root / "reference/formats/spec.txt").write_text("drift")
        with self.assertRaisesRegex(core.AuthoringError, "dependency"):
            family.load_family(self.root, self.run)

    def test_registry_history_drift_and_nonprefix(self):
        path = self.root / self.run / "family-context.json"
        context = core.read_json(path)
        context["registry_facts"] = {"families": [{"base_checkpoint": 60}]}
        context["context_hash"] = family._hash({key: value for key, value in context.items() if key != "context_hash"})
        core.write_json(path, context)
        with self.assertRaisesRegex(core.AuthoringError, "registry history"):
            family.load_family(self.root, self.run)
        with self.assertRaisesRegex(core.AuthoringError, "prefix"):
            family._prefix([{"base_checkpoint": 40}], 60)

    def test_immutable_receipt_rejects_extra_registry_namespace(self):
        pack = self.publish()
        folder = self.root / pack
        context = core.read_json(folder / "family-context.json")
        context["registry_facts"]["unexpected"] = []
        context["context_hash"] = family._hash({key: value for key, value in context.items() if key != "context_hash"})
        core.write_json(folder / "family-context.json", context)
        manifest = core.read_json(folder / "manifest.json")
        manifest["context_hash"] = context["context_hash"]
        manifest["registry_facts"] = context["registry_facts"]
        manifest["files"]["family-context.json"] = core.digest(folder / "family-context.json")
        with self.assertRaisesRegex(core.AuthoringError, "families-only"):
            family._verify_manifest(self.root, pack, manifest)

    def test_context_hash_edit_and_excluded_empty_directory(self):
        context_path = self.root / self.run / "family-context.json"
        original = core.read_json(context_path)
        changed = dict(original)
        changed["vocabulary_horizon"] = 244
        core.write_json(context_path, changed)
        with self.assertRaisesRegex(core.AuthoringError, "snapshot"):
            family.load_family(self.root, self.run)
        core.write_json(context_path, original)
        self.author()
        (self.root / self.run / "de").mkdir()
        with self.assertRaisesRegex(core.AuthoringError, "Same-language"):
            family.finalize_family(self.root, self.run)

    def test_registry_snapshot_is_families_only(self):
        context = family.load_family(self.root, self.run)
        self.assertEqual(context["registry_facts"], {"families": []})
        self.assertEqual(context["external_history"], [])
        self.assertFalse((self.root / "family-registry.sqlite").exists())
        path = self.root / self.run / "family-context.json"
        context["registry_facts"]["unexpected"] = []
        context["context_hash"] = family._hash({key: value for key, value in context.items() if key != "context_hash"})
        core.write_json(path, context)
        with self.assertRaisesRegex(core.AuthoringError, "registry history"):
            family.load_family(self.root, self.run)

    def test_stage_future_prerequisite_and_rank(self):
        self.author()
        brief_path = self.root / self.run / "stage-1/brief.json"
        brief = core.read_json(brief_path)
        brief["prerequisites"] = [{"source": "stage:2", "justification": "Forbidden future"}]
        core.write_json(brief_path, brief)
        with self.assertRaisesRegex(core.AuthoringError, "earlier"):
            family.validate_family_briefs(self.root, self.run)
        brief["prerequisites"] = [{"source": "stage:0", "justification": "Earlier own teaching"}]
        brief["lexical_audit"] = [{"target_form": "wort46", "known_rank": 46, "justification": "Future rank"}]
        core.write_json(brief_path, brief)
        with self.assertRaisesRegex(core.AuthoringError, "outside stage"):
            family.validate_family_briefs(self.root, self.run)
        brief["lexical_audit"] = [{"target_form": "wort1", "taught_in": "stage:0", "justification": "Earlier source lesson"}]
        core.write_json(brief_path, brief)
        family.validate_family_briefs(self.root, self.run)

    def test_review_full_coverage_and_source_invalidation(self):
        self.author()
        payload = self.report("assessment")
        payload["stages"][4]["question_checks"].pop()
        with self.assertRaisesRegex(core.AuthoringError, "every actual"):
            family.submit_family_review(self.root, self.run, "assessment", payload)
        family.submit_family_review(self.root, self.run, "language:fr", self.report("language:fr"))
        source = family.family_locale_path(self.root, self.run, "en", 4) / "quiz.md"
        source.write_text(source.read_text() + "\nsource change")
        with self.assertRaisesRegex(core.AuthoringError, "stale"):
            family.require_family_review(self.root, self.run, "language:fr")

    def test_question_evidence_is_required_only_for_item_review_roles(self):
        self.author()
        self.approve()
        for role in ("pedagogical", "format", "final", "assessment", "language:fr"):
            with self.subTest(role=role):
                payload = self.report(role)
                for stage in payload["stages"]:
                    stage["question_checks"] = []
                if role in ("assessment", "language:fr"):
                    with self.assertRaisesRegex(core.AuthoringError, "every actual"):
                        family.submit_family_review(self.root, self.run, role, payload)
                else:
                    family.submit_family_review(self.root, self.run, role, payload)
                    self.assertEqual(family.require_family_review(self.root, self.run, role)["status"], "approved")

    def test_candidate_count_is_not_a_brief_requirement(self):
        self.author()
        for stage in family.STAGES:
            path = self.root / self.run / f"stage-{stage}/brief.json"
            brief = core.read_json(path)
            brief["candidates"] = []
            core.write_json(path, brief)
        family.validate_family_briefs(self.root, self.run)

    def test_missing_locale_nested_and_symlink_inventory(self):
        self.author()
        path = family.family_locale_path(self.root, self.run, "fr", 4) / "quiz.md"
        path.unlink()
        with self.assertRaisesRegex(core.AuthoringError, "inventory"):
            family.finalize_family(self.root, self.run)
        path.write_text("synthetic")
        nested = path.parent / "nested"
        nested.mkdir()
        (nested / "lesson.md").write_text("bad")
        with self.assertRaisesRegex(core.AuthoringError, "inventory"):
            family.finalize_family(self.root, self.run)
        (nested / "lesson.md").unlink()
        nested.rmdir()
        (path.parent / "link").symlink_to(path)
        with self.assertRaisesRegex(core.AuthoringError, "Symlink"):
            family.finalize_family(self.root, self.run)

    def test_worker_lock_blocks_reviews(self):
        self.author()
        path = self.root / "work/translations/families/de/000020/fr/worker.lock"
        path.parent.mkdir(parents=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(core.AuthoringError, "worker"):
                family.submit_family_review(self.root, self.run, "pedagogical", self.report("pedagogical"))

    def test_atomic_publish_repeated_and_next_history(self):
        pack = self.publish()
        self.assertEqual(family.finalize_family(self.root, self.run), pack)
        self.assertFalse((self.root / self.run).exists())
        self.assertFalse((self.root / "registry.sqlite").exists())
        next_run = family.prepare_family(self.root, "de", 40)
        context = family.load_family(self.root, next_run)
        self.assertEqual(len(context["external_history"]), 5)
        self.assertEqual(len(context["stages"][0]["prior_external_keys"]), 1)
        self.assertEqual(len(context["stages"][4]["prior_external_keys"]), 5)
        self.assertEqual(len(context["stages"][0]["planning_external_keys"]), 4)
        facts = [{"base_checkpoint": item["base_checkpoint"]} for item in family.family_order(200)]
        with self.assertRaisesRegex(core.AuthoringError, "earlier"):
            family._prefix(facts, 210)
        family._prefix(facts, 220)
        self.assertIn(f"{pack}#stage:0", context["stages"][0]["prior_external_keys"])

    def test_rename_fault_and_committed_receipt_recovery(self):
        self.author()
        self.approve()
        with patch.object(family, "validate_family", self.validator), patch.object(family.os, "rename", side_effect=OSError("Synthetic rename failure")):
            with self.assertRaises(OSError):
                family.finalize_family(self.root, self.run)
        self.assertTrue((self.root / self.run).exists())
        with patch.object(family, "validate_family", self.validator), patch.object(family, "_register", side_effect=RuntimeError("Synthetic post-rename crash")):
            with self.assertRaises(RuntimeError):
                family.finalize_family(self.root, self.run)
        self.assertFalse((self.root / self.run).exists())
        self.assertEqual(family.family_registry_facts(self.root, "de"), [])
        pack = family.finalize_family(self.root, self.run)
        self.assertEqual(pack, "family_packs/de/000020")
        self.assertEqual(len(family.family_registry_facts(self.root, "de")), 1)

    def test_late_family_destination_conflict_prevents_publication(self):
        self.author()
        self.approve()
        destination = self.root / "family_packs/de/000020"
        destination.mkdir(parents=True)
        with self.assertRaisesRegex(core.AuthoringError, "Recovery conflict"):
            family.finalize_family(self.root, self.run)
        self.assertTrue((self.root / self.run).exists())
        self.assertFalse((destination / "manifest.json").exists())

    def test_recovery_requires_exact_publication_receipt(self):
        self.author()
        self.approve()
        with patch.object(family, "validate_family", self.validator), patch.object(family, "_register", side_effect=RuntimeError("Synthetic crash")):
            with self.assertRaises(RuntimeError):
                family.finalize_family(self.root, self.run)
        receipt = self.root / "work/family-publications/de/000020/receipt.json"
        value = core.read_json(receipt)
        value["manifest_sha256"] = "incorrect"
        core.write_json(receipt, value)
        with self.assertRaisesRegex(core.AuthoringError, "publication receipt"):
            family.finalize_family(self.root, self.run)
        self.assertEqual(family.family_registry_facts(self.root, "de"), [])

    def test_recovery_rejects_modified_bytes(self):
        self.author()
        self.approve()
        with patch.object(family, "validate_family", self.validator), patch.object(family, "_register", side_effect=RuntimeError("Synthetic crash")):
            with self.assertRaises(RuntimeError):
                family.finalize_family(self.root, self.run)
        (self.root / "family_packs/de/000020/fr/stage-4/quiz.md").write_text("tampered")
        with self.assertRaisesRegex(core.AuthoringError, "bytes changed"):
            family.finalize_family(self.root, self.run)
        self.assertEqual(family.family_registry_facts(self.root, "de"), [])

    def test_real_bridge_family_validation(self):
        self.validation_mock.stop()
        from quiz_fixtures import lesson_text, quiz_text

        project = Path(__file__).resolve().parents[1]
        shutil.rmtree(self.root / "reference/formats")
        shutil.copytree(project / "reference/formats", self.root / "reference/formats")
        shutil.copytree(project / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(self.root / self.run)
        self.run = family.prepare_family(self.root, "de", 20)
        self.author()
        for locale in core.learner_locales("de"):
            for stage in family.STAGES:
                folder = family.family_locale_path(self.root, self.run, locale, stage)
                lesson = lesson_text("family-base", ("base-page",)) if stage == 0 else ""
                quiz = quiz_text(f"bank-{stage}", "family-base", "base-page", f"q-{stage}")
                lesson = lesson.replace("explained_in: en", f"explained_in: {locale}")
                quiz = quiz.replace("explained_in: en", f"explained_in: {locale}")
                (folder / "lesson.md").write_text(lesson)
                quiz += '\n```mermaid\nflowchart LR\naccTitle: Synthetic diagram\naccDescr {\n# diagram-heading\n}\nA --> B\n```\n'
                (folder / "quiz.md").write_text(quiz)
        report = family.validate_family(self.root, self.run)
        self.assertEqual(report["totals"], {"pages": 1, "questions": 125})
        self.assertEqual(len(report["family_contract"]), 5)
        self.approve()
        self.assertEqual(family.finalize_family(self.root, self.run), "family_packs/de/000020")

    def test_interruptible_validator_cancel_and_deadline(self):
        self.validation_mock.stop()
        self.author()
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "validate-family.cjs").write_text("import time\ntime.sleep(10)\n")
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        started = time.monotonic()
        with patch.dict("os.environ", {"NODE_BINARY": sys.executable}):
            timer.start()
            try:
                with self.assertRaisesRegex(core.AuthoringError, "cancelled"):
                    family.validate_family(self.root, self.run, timeout=5, stop_event=stop)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 2)
            with self.assertRaisesRegex(core.AuthoringError, "deadline"):
                family.validate_family(self.root, self.run, timeout=0.1)
        self.assertTrue((self.root / self.run).exists())

    def test_plan_base_rank_and_cli(self):
        plan = family.family_plan(self.root, "de", 20)
        self.assertEqual([item["base_checkpoint"] for item in plan["families"]], [20, 10])
        self.assertEqual(plan["next_eligible"]["base_checkpoint"], 20)
        with patch("sys.stdout"):
            self.assertEqual(family_cli.main(["--root", str(self.root), "plan", "--language", "de", "--through", "20"]), 0)


if __name__ == "__main__":
    unittest.main()
