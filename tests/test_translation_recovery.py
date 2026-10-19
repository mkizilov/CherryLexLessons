import contextlib
import fcntl
import importlib.util
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from lesson_factory import core, family, translation_recovery as recovery
from lesson_factory.family_translation import publish
from lesson_factory.translation import TranslationError, digest, encode, prepare
from lesson_factory.translation_payload import build_payload
from tests import test_family_core


class TranslationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_family_core.FamilyCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run = self.fixture.root.resolve(), self.fixture.run
        self.fixture.author()
        for locale in core.learner_locales("de"):
            for stage in family.STAGES:
                core.write_json(family.family_locale_path(self.root, self.run, locale, stage) / "adaptation-notes.json", [])
        for role in ("pedagogical", "assessment", "format"):
            family.submit_family_review(self.root, self.run, role, self.fixture.report(role))
        self.folder = self.root / "work/translations/families/de/000020"
        self.prompt_patch = patch("lesson_factory.translation_payload.build_prompt", return_value="focused synthetic prompt")
        self.prompt_patch.start()
        self.addCleanup(self.prompt_patch.stop)
        self.provider_patch = patch("lesson_factory.translation.open_response", side_effect=AssertionError("No network authorized in recovery tests"))
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def scaffold(self, locale="fr", missing=False):
        folder = family.family_locale_path(self.root, self.run, locale)
        shutil.rmtree(folder)
        if not missing:
            for stage in family.STAGES:
                (folder / f"stage-{stage}").mkdir(parents=True)
                if stage:
                    (folder / f"stage-{stage}/lesson.md").write_bytes(b"")

    def attempted(self, locale="fr", count=1, state="uncertain", scope=None):
        folder = self.root / "work/translations" / scope / locale if scope else self.folder / locale
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("worker.lock", "attempt.lock"):
            (folder / name).touch(exist_ok=True)
        if scope:
            context, evidence = recovery._source(self.root, self.run, 120)
            approval = {key: evidence[key] for key in ("binding", "context_digest", "reviews")}
            body, summary = prepare(build_payload(self.root, self.run, context, locale, approval), locale, "focused synthetic prompt")
        else:
            body, summary = prepare({"study_language": "de", "source_locale": "en"}, locale, "retained original prompt")
        for attempt in range(1, count + 1):
            directory = folder / f"attempt-{attempt}"
            record = {"target": locale, "attempt": attempt, "input_hash": summary["hash"], "request_id": f"synthetic-{attempt}", "state": state, "billing_outcome": "unknown", "retry_eligible": False}
            core.write_json(directory / "summary.json", summary)
            (directory / "request.json").write_bytes(encode(body))
            core.write_json(directory / "journal.json", record)
            (directory / "raw.sse").write_bytes(b"data: {\"type\":\"response.created\"}\n\n")
        core.write_json(folder / "journal.json", record)
        return folder

    def authorize(self):
        return recovery.authorize_translation_recovery(self.root, self.run, reason="Explicit synthetic additional-attempt authorization")

    def prepare_retry(self, count=1):
        self.scaffold()
        return self.attempted(count=count)

    def test_read_only_plan_preserves_every_byte_without_authorization(self):
        self.prepare_retry(4)
        before = recovery._inventory(self.root, ".")
        plan = recovery.plan_translation_recovery(self.root, self.run)
        self.assertFalse(plan["active"])
        self.assertEqual(plan["locales"]["fr"]["new_attempt_limit"], 4)
        self.assertEqual(plan["locales"]["fr"]["prior"]["attempts_consumed"], 4)
        self.assertEqual(plan["locales"]["es"]["disposition"], "preserve")
        self.assertEqual(plan["locales"]["fr"]["request_summary"]["output_cap"], None)
        self.assertEqual(recovery._inventory(self.root, "."), before)
        self.assertIsNone(recovery.recovery_for_locale(self.root, self.run, "fr"))

    def test_one_immutable_grant_idempotent_after_paid_attempts(self):
        old = self.prepare_retry(4)
        original = recovery._inventory(self.root, old.relative_to(self.root).as_posix())
        first = self.authorize()
        self.attempted(count=4, scope=first["scope"])
        second = self.authorize()
        self.assertEqual(first["grant_id"], second["grant_id"])
        self.assertEqual(first["locales"], second["locales"])
        self.assertEqual(len(list((self.folder / "renewals").iterdir())), 1)
        self.assertEqual(recovery._inventory(self.root, old.relative_to(self.root).as_posix()), original)
        result = recovery.recovery_for_locale(self.root, self.run, "fr")
        self.assertEqual(result["disposition"], "retry")
        self.assertEqual(result["scope"], first["scope"])
        self.assertEqual(result["request_summary"], first["locales"]["fr"]["request_summary"])

    def test_authorization_reason_required_and_never_attempted_stays_original(self):
        self.prepare_retry()
        self.scaffold("es", missing=True)
        with self.assertRaisesRegex(TranslationError, "reason"):
            recovery.authorize_translation_recovery(self.root, self.run, reason=" ")
        grant = self.authorize()
        self.assertEqual(grant["locales"]["es"]["disposition"], "untouched")
        self.assertIsNone(recovery.recovery_for_locale(self.root, self.run, "es"))

    def test_no_failed_attempt_means_no_grant(self):
        self.scaffold()
        with self.assertRaisesRegex(TranslationError, "No incomplete attempted"):
            self.authorize()
        self.assertFalse((self.folder / "renewals").exists())

    def test_source_edit_or_changed_approval_rejects_existing_grant(self):
        self.prepare_retry()
        self.authorize()
        path = family.family_review_path(self.root, self.run, "format")
        value = core.read_json(path)
        value["findings"].append("Different genuine review bytes")
        core.write_json(path, value)
        with self.assertRaisesRegex(TranslationError, "evidence drift"):
            self.authorize()
        self.assertEqual(len(list((self.folder / "renewals").iterdir())), 1)

    def test_unapproved_source_edit_is_not_renewable(self):
        self.prepare_retry()
        self.authorize()
        path = family.family_locale_path(self.root, self.run, "en", 0) / "quiz.md"
        path.write_text(path.read_text() + "\nedit")
        with self.assertRaisesRegex(core.AuthoringError, "stale"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_prior_proof_or_request_drift_blocks(self):
        folder = self.prepare_retry()
        self.authorize()
        (folder / "attempt-1/raw.sse").write_bytes(b"changed evidence")
        with self.assertRaisesRegex(TranslationError, "prior evidence drift"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_bad_prior_request_provenance_blocks_initial_authorization(self):
        folder = self.prepare_retry()
        (folder / "attempt-1/request.json").write_bytes(b"{}")
        with self.assertRaisesRegex(TranslationError, "provenance"):
            self.authorize()

    def test_focused_payload_change_invalidates_not_replaces_grant(self):
        self.prepare_retry()
        self.authorize()
        with patch("lesson_factory.translation_payload.build_prompt", return_value="changed prompt"):
            with self.assertRaisesRegex(TranslationError, "payload/provenance"):
                self.authorize()

    def test_partial_edit_rejected_before_and_after_grant(self):
        self.prepare_retry()
        destination = family.family_locale_path(self.root, self.run, "fr")
        (destination / "stage-0/quiz.md").write_text("external partial edit")
        with self.assertRaisesRegex(TranslationError, "Partial"):
            self.authorize()
        (destination / "stage-0/quiz.md").unlink()
        self.authorize()
        (destination / "stage-0/quiz.md").write_text("external partial edit")
        with self.assertRaisesRegex(TranslationError, "destination drift"):
            recovery.recovery_for_locale(self.root, self.run, "fr")
        self.assertEqual((destination / "stage-0/quiz.md").read_text(), "external partial edit")

    def test_preserve_complete_expert_edits_requires_current_deep_contract(self):
        self.prepare_retry()
        self.authorize()
        path = family.family_locale_path(self.root, self.run, "es", 2) / "quiz.md"
        path.write_text("valid external expert edit under synthetic validator")
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "es")["disposition"], "preserve")
        validator = family.validate_family
        def changed(root, run, locale=None, **kwargs):
            report = validator(root, run, locale, **kwargs)
            if locale == "es":
                report["family_contract"] = {"changed": True}
            return report
        with patch.object(family, "validate_family", side_effect=changed):
            with self.assertRaisesRegex(TranslationError, "deep source family contract"):
                recovery.recovery_for_locale(self.root, self.run, "es")
        self.assertEqual(path.read_text(), "valid external expert edit under synthetic validator")

    def test_known_complete_candidate_in_earlier_attempt_never_purchased(self):
        self.prepare_retry()
        self.scaffold("es")
        old = self.attempted("es", count=2)
        (old / "attempt-1/raw.sse").write_bytes(b'data: {"type":"response.failed","response":{"status":"failed"}}\n\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n')
        grant = self.authorize()
        self.assertEqual(grant["locales"]["es"]["disposition"], "offline_recovery")
        self.assertEqual(grant["locales"]["es"]["new_attempt_limit"], 0)
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "retry")
        with self.assertRaisesRegex(TranslationError, "offline recovery"):
            recovery.recovery_for_locale(self.root, self.run, "es")

    def test_complete_response_and_retained_candidate_never_renewed(self):
        old = self.prepare_retry()
        for name, value in (("response.json", {"status": "completed"}), ("candidate.json", {"stages": []})):
            with self.subTest(name=name):
                path = old / "attempt-1" / name
                core.write_json(path, value)
                plan = recovery.plan_translation_recovery(self.root, self.run)
                self.assertEqual(plan["locales"]["fr"]["disposition"], "offline_recovery")
                path.unlink()

    def test_original_worker_attempt_and_mutation_locks_are_nonblocking(self):
        folder = self.prepare_retry()
        for path in (folder / "worker.lock", folder / "attempt.lock", self.root / ".authoring.lock"):
            with self.subTest(path=path.name), path.open("rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, "active|locked"):
                    self.authorize()
                fcntl.flock(handle, fcntl.LOCK_UN)
        self.assertFalse((self.folder / "renewals").exists())

    def test_missing_lock_and_orphan_original_evidence_reject(self):
        folder = self.prepare_retry()
        (folder / "attempt.lock").unlink()
        with self.assertRaisesRegex(TranslationError, "Missing original"):
            self.authorize()
        (folder / "attempt.lock").touch()
        (folder / "journal.json").unlink()
        with self.assertRaisesRegex(TranslationError, "Orphan"):
            self.authorize()

    def test_symlink_destination_old_receipt_and_pointer_rejected(self):
        old = self.prepare_retry()
        outside = self.root / "unrelated.txt"
        outside.write_text("synthetic")
        (old / "linked").symlink_to(outside)
        with self.assertRaisesRegex(TranslationError, "Symlink"):
            self.authorize()
        (old / "linked").unlink()
        self.authorize()
        pointer = self.folder / "renewal-active.json"
        pointer.unlink()
        pointer.symlink_to(outside)
        with self.assertRaisesRegex(core.AuthoringError, "Symlink"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_interrupted_pointer_install_never_mints_second_grant(self):
        self.prepare_retry()
        real = recovery.atomic_write
        def interrupted(path, data):
            if Path(path).name == "renewal-active.json":
                raise OSError("synthetic interruption")
            real(path, data)
        with patch.object(recovery, "atomic_write", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.authorize()
        for action in (self.authorize, lambda: recovery.recovery_for_locale(self.root, self.run, "fr")):
            with self.assertRaisesRegex(TranslationError, "Orphan renewal"):
                action()
        self.assertEqual(len(list((self.folder / "renewals").iterdir())), 1)

    def test_active_pointer_and_local_source_copy_are_digest_bound(self):
        self.prepare_retry()
        grant = self.authorize()
        source = self.root / "work/translations" / grant["scope"] / "source-evidence.json"
        source.write_text("{}")
        with self.assertRaisesRegex(TranslationError, "local source evidence"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_orphan_new_attempt_or_missing_new_journal_never_resets_budget(self):
        self.prepare_retry()
        grant = self.authorize()
        new = self.attempted(scope=grant["scope"])
        (new / "journal.json").unlink()
        with self.assertRaisesRegex(TranslationError, "Orphan renewal attempt"):
            self.authorize()
        self.assertEqual(len(list((self.folder / "renewals").iterdir())), 1)

    def test_new_publication_then_valid_expert_edit_is_preserved(self):
        self.prepare_retry()
        grant = self.authorize()
        logs = self.attempted(scope=grant["scope"])
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson" if stage == 0 else "", "quiz_md": "quiz", "adaptation_notes": []} for stage in family.STAGES]}
        (logs / "candidate.json").write_bytes(encode(candidate))
        record = core.read_json(logs / "journal.json")
        record.update(state="completed", output_hash=digest(encode(candidate)))
        destination = family.family_locale_path(self.root, self.run, "fr")
        initial = recovery._inventory(self.root, f"{self.run}/fr")
        scaffold = family.family_locale_scaffold(self.root, self.run, "fr")
        publish(destination, logs, candidate, record, initial, lambda: None, scaffold=scaffold)
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "preserve")
        (destination / "stage-4/quiz.md").write_text("corrected full expert version")
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "preserve")
        self.assertEqual(self.authorize()["grant_id"], grant["grant_id"])
        self.scaffold()
        with self.assertRaisesRegex(TranslationError, "Partial"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_selected_locale_ignores_other_locale_destination_and_prior_drift(self):
        old = self.prepare_retry()
        self.authorize()
        (old / "attempt-1/raw.sse").write_bytes(b"drift")
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "es")["disposition"], "preserve")
        with self.assertRaisesRegex(TranslationError, "prior evidence drift"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_orphan_non_next_renewal_attempt_cannot_be_skipped(self):
        self.prepare_retry()
        grant = self.authorize()
        logs = self.attempted(scope=grant["scope"])
        core.write_json(logs / "attempt-3/candidate.json", {"stages": []})
        with self.assertRaisesRegex(TranslationError, "orphan renewal attempts"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_complete_output_before_terminal_prevents_original_or_renewal_purchase(self):
        logs = self.prepare_retry()
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson", "quiz_md": "quiz", "adaptation_notes": []} for stage in family.STAGES]}
        text = json.dumps(candidate)
        events = [
            [{"type": "response.output_text.done", "text": text}],
            [{"type": "response.output_item.done", "item": {"type": "message", "content": [{"type": "output_text", "text": text}]}}],
            [{"type": "response.output_text.delta", "delta": text[:30]}, {"type": "response.output_text.delta", "delta": text[30:]}],
        ]
        for values in events:
            wire = b"".join(b"data: " + encode(value) + b"\n\n" for value in values)
            (logs / "attempt-1/raw.sse").write_bytes(wire)
            self.assertEqual(recovery.plan_translation_recovery(self.root, self.run)["locales"]["fr"]["disposition"], "offline_recovery")
        (logs / "attempt-1/raw.sse").write_bytes(b"incomplete")
        grant = self.authorize()
        renewed = self.attempted(scope=grant["scope"])
        (renewed / "attempt-1/raw.sse").write_bytes(wire)
        with self.assertRaisesRegex(TranslationError, "offline recovery"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_pending_publication_recovery_all_rename_boundaries(self):
        self.prepare_retry()
        grant = self.authorize()
        logs = self.attempted(scope=grant["scope"])
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson" if stage == 0 else "", "quiz_md": "quiz", "adaptation_notes": []} for stage in family.STAGES]}
        (logs / "candidate.json").write_bytes(encode(candidate))
        record = core.read_json(logs / "journal.json")
        record.update(state="completed", output_hash=digest(encode(candidate)))
        core.write_json(logs / "journal.json", record)
        destination = family.family_locale_path(self.root, self.run, "fr")
        initial = recovery._inventory(self.root, f"{self.run}/fr")
        scaffold = family.family_locale_scaffold(self.root, self.run, "fr")
        from lesson_factory import family_translation
        original_write = family_translation.atomic_write
        def crash_staging(path, data):
            if Path(path).name == "quiz.md":
                raise OSError("synthetic stage interruption")
            original_write(path, data)
        with patch.object(family_translation, "atomic_write", side_effect=crash_staging):
            with self.assertRaisesRegex(OSError, "stage interruption"):
                publish(destination, logs, candidate, record, initial, lambda: None, scaffold=scaffold)
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "retry")
        real_rename = family_translation.os.rename
        def crash_rename(source, target):
            real_rename(source, target)
            raise OSError("synthetic rename interruption")
        for _ in range(2):
            record = core.read_json(logs / "journal.json")
            with patch.object(family_translation.os, "rename", side_effect=crash_rename):
                with self.assertRaisesRegex(OSError, "rename interruption"):
                    publish(destination, logs, candidate, record, initial, lambda: None, scaffold=scaffold)
            self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "retry")
        record = core.read_json(logs / "journal.json")
        publish(destination, logs, candidate, record, initial, lambda: None, scaffold=scaffold)
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "preserve")

    def test_cli_readonly_default_and_explicit_reason(self):
        path = Path(__file__).resolve().parents[1] / "tools/reauthorize-translations.py"
        specification = importlib.util.spec_from_file_location("reauthorize_translations", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        with patch.object(module, "plan_translation_recovery", return_value={"synthetic": True}) as plan, patch.object(module, "authorize_translation_recovery") as authorize, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(module.main(["--root", str(self.root), "--run", self.run]), 0)
            plan.assert_called_once()
            authorize.assert_not_called()
            self.assertEqual(module.main(["--run", self.run, "--reason", "no flag"]), 1)


if __name__ == "__main__":
    unittest.main()
