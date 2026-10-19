import fcntl
import json
import unittest
from unittest.mock import patch

from lesson_factory import core, family, translation_recovery as recovery
from lesson_factory.translation import TranslationError, digest, encode
from tests import test_translation_recovery as original


class TranslationCorrectionTests(unittest.TestCase):
    setUp = original.TranslationRecoveryTests.setUp
    scaffold = original.TranslationRecoveryTests.scaffold
    attempted = original.TranslationRecoveryTests.attempted
    authorize = original.TranslationRecoveryTests.authorize
    prepare_retry = original.TranslationRecoveryTests.prepare_retry

    def candidate(self, text="..."):
        return {"stages": [{"stage": stage, "lesson_md": text, "quiz_md": text, "adaptation_notes": []} for stage in family.STAGES]}

    def wire(self, *events):
        return b"".join(b"data: " + encode(value) + b"\n\n" for value in events)

    def skeleton_grant(self, locales=("fr",), text="...", extra=None):
        self.scaffold("it")
        self.attempted("it")
        for locale in locales:
            self.scaffold(locale)
            logs = self.attempted(locale)
            (logs / "attempt-1/raw.sse").write_bytes(self.wire({"type": "response.output_item.done", "item": {"phase": "commentary", "content": [{"text": json.dumps(self.candidate(text))}]}}))
            if extra is not None:
                extra(logs)
        detector = recovery._completed_evidence
        with patch.object(recovery, "_completed_evidence", side_effect=lambda folder: detector(folder, require_content=False)):
            grant = self.authorize()
        for locale in locales:
            self.assertEqual(grant["locales"][locale]["disposition"], "offline_recovery")
            self.assertEqual(grant["locales"][locale]["new_attempt_limit"], 0)
        return grant

    def correct(self, locales=("fr",), **kwargs):
        return recovery.authorize_translation_correction(self.root, self.run, list(locales), reason=kwargs.pop("reason", "Explicit synthetic false-positive correction"), operator=kwargs.pop("operator", True), **kwargs)

    def receipt_path(self, grant, locale="fr"):
        return self.root / "work/translations" / grant["scope"] / "corrections" / f"{locale}.json"

    def test_only_provable_skeletons_lose_candidate_classification(self):
        for text in ("", " ", "...", "…", ". . .\n\t…"):
            with self.subTest(text=text):
                self.assertFalse(recovery._has_candidate(self.candidate(text)))
                self.assertTrue(recovery._has_candidate(self.candidate(text), require_content=False))
        self.assertFalse(recovery._has_candidate({"stages": [{"stage": stage} for stage in family.STAGES]}))
        for text in ("quiz", "Not valid QMD and incorrect language", "测试", "!", "{}"):
            with self.subTest(text=text):
                self.assertTrue(recovery._has_candidate(self.candidate(text)))
        candidate = self.candidate("")
        candidate["stages"][3]["quiz_md"] = "Retained substantial fragment"
        candidate["stages"][0]["adaptation_notes"] = "invalid notes still repairable"
        self.assertTrue(recovery._has_candidate(candidate))

    def test_notes_and_malformed_or_unknown_stage_fields_remain_evidence(self):
        for field, value in (
            ("adaptation_notes", ["Actual retained bilingual adaptation explanation"]),
            ("adaptation_notes", "..."),
            ("adaptation_notes", None),
            ("adaptation_notes", [None]),
            ("adaptation_notes", [{}]),
            ("lesson_md", None),
            ("quiz_md", []),
            ("unexpected", ""),
            ("unexpected", None),
            ("unexpected", {"retained": "actual output"}),
        ):
            with self.subTest(field=field, value=value):
                candidate = self.candidate()
                candidate["stages"][2][field] = value
                self.assertTrue(recovery._has_candidate(candidate))
        for notes in ([], [""], ["...", "…", " . . .\n\t "]):
            with self.subTest(notes=notes):
                candidate = self.candidate()
                candidate["stages"][2]["adaptation_notes"] = notes
                self.assertFalse(recovery._has_candidate(candidate))

    def test_substantive_notes_on_ellipsis_stages_forbid_operator_correction(self):
        def add_notes(logs):
            candidate = self.candidate()
            candidate["stages"][0]["adaptation_notes"] = ["Actual retained bilingual adaptation explanation"]
            (logs / "attempt-1/raw.sse").write_bytes(self.wire({"type": "response.output_item.done", "item": {"phase": "commentary", "content": [{"text": json.dumps(candidate)}]}}))
        grant = self.skeleton_grant(extra=add_notes)
        before = recovery._inventory(self.root, "work/translations")
        self.assertTrue(recovery._completed_evidence(self.folder / "fr"))
        with self.assertRaisesRegex(TranslationError, "known completed"):
            self.correct()
        self.assertEqual(recovery._inventory(self.root, "work/translations"), before)
        self.assertFalse(self.receipt_path(grant).exists())
        with self.assertRaisesRegex(TranslationError, "offline recovery"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_stream_final_partial_and_commentary_skeleton_are_incomplete(self):
        logs = self.prepare_retry()
        skeleton = json.dumps(self.candidate())
        wire = self.wire(
            {"type": "response.output_item.added", "item": {"id": "comment", "phase": "commentary"}},
            {"type": "response.output_text.delta", "item_id": "comment", "output_index": 0, "delta": skeleton[:50]},
            {"type": "response.output_text.delta", "item_id": "comment", "output_index": 0, "delta": skeleton[50:]},
            {"type": "response.output_item.added", "item": {"id": "final", "phase": "final_answer"}},
            {"type": "response.output_text.delta", "item_id": "final", "output_index": 1, "delta": '{"stages":[{"stage":0,"lesson_md":"Substantive unfinished'},
        )
        (logs / "attempt-1/raw.sse").write_bytes(wire)
        self.assertFalse(recovery._completed_evidence(logs))
        self.assertEqual(recovery.plan_translation_recovery(self.root, self.run)["locales"]["fr"]["disposition"], "retry")

    def test_substantive_commentary_output_is_always_offline_recoverable(self):
        logs = self.prepare_retry()
        candidate = self.candidate("invalid but real output")
        candidate["stages"][2]["adaptation_notes"] = 42
        for phase in ("commentary", "final_answer", None):
            with self.subTest(phase=phase):
                (logs / "attempt-1/raw.sse").write_bytes(self.wire({"type": "response.output_item.done", "item": {"phase": phase, "content": [{"text": json.dumps(candidate)}]}}))
                self.assertTrue(recovery._completed_evidence(logs))
                self.assertEqual(recovery.plan_translation_recovery(self.root, self.run)["locales"]["fr"]["disposition"], "offline_recovery")

    def test_terminal_completed_rejected_and_empty_candidate_files_block(self):
        logs = self.prepare_retry()
        cases = [
            ("response.json", encode({"status": "completed"})),
            ("response.json", encode({"response": {"status": "completed"}})),
            ("response.json", encode({"state": "rejected"})),
            ("raw.sse", self.wire({"type": "response.completed"})),
            ("raw.sse", self.wire({"provider_status": "completed"})),
            ("candidate.json", b""),
        ]
        for name, data in cases:
            with self.subTest(name=name, data=data):
                target = logs / "attempt-1" / name
                before = target.read_bytes() if target.exists() else None
                target.write_bytes(data)
                self.assertTrue(recovery._completed_evidence(logs))
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)

    def test_operator_correction_is_explicit_immutable_idempotent_and_effective(self):
        grant = self.skeleton_grant()
        before = recovery._inventory(self.root, "work/translations")
        for operator, reason in ((False, "authorized"), (1, "authorized"), (True, " ")):
            with self.assertRaisesRegex(TranslationError, "operator"):
                self.correct(operator=operator, reason=reason)
        with self.assertRaisesRegex(TranslationError, "offline recovery"):
            recovery.recovery_for_locale(self.root, self.run, "fr")
        self.assertFalse(self.receipt_path(grant).exists())
        result = self.correct()
        after = recovery._inventory(self.root, "work/translations")
        self.assertEqual({name: after[name] for name in before}, before)
        added = set(after) - set(before)
        self.assertEqual(added, {f"families/de/000020/renewals/{grant['grant_id']}/corrections", f"families/de/000020/renewals/{grant['grant_id']}/corrections/fr.json"})
        self.assertEqual(result, self.correct())
        self.assertEqual(recovery._inventory(self.root, "work/translations"), after)
        authorization = recovery.recovery_for_locale(self.root, self.run, "fr")
        self.assertEqual(authorization["scope"], grant["scope"])
        self.assertEqual(authorization["grant_id"], grant["grant_id"])
        self.assertEqual(authorization["disposition"], "retry")
        self.assertEqual(authorization["request_summary"], core.read_json(self.receipt_path(grant))["correction"]["request_summary"])
        plan = recovery.plan_translation_recovery(self.root, self.run)
        self.assertEqual(plan["locales"]["fr"]["new_attempt_limit"], 0)
        self.assertEqual(plan["locales"]["fr"]["disposition"], "offline_recovery")
        self.assertEqual(plan["current_dispositions"]["fr"], "retry")
        self.assertEqual(plan["effective_attempt_limits"]["fr"], 4)
        self.assertEqual(self.authorize()["grant_id"], grant["grant_id"])

    def test_new_and_existing_retry_or_preserved_locales_cannot_be_corrected(self):
        self.scaffold("es")
        with self.assertRaisesRegex(TranslationError, "existing immutable"):
            self.correct()
        grant = self.skeleton_grant()
        for locales in (("it",), ("es",), ("de",), ("fr", "fr"), ()):
            with self.subTest(locales=locales), self.assertRaises(TranslationError):
                self.correct(locales)
        self.assertFalse(self.receipt_path(grant).exists())

    def test_real_complete_invalid_candidate_cannot_be_corrected(self):
        grant = self.skeleton_grant(text="Real output, bad grammar and QMD")
        with self.assertRaisesRegex(TranslationError, "known completed"):
            self.correct()
        self.assertFalse(self.receipt_path(grant).exists())

    def test_terminal_completed_cannot_be_corrected_despite_skeleton(self):
        grant = self.skeleton_grant(extra=lambda logs: core.write_json(logs / "attempt-1/response.json", {"status": "completed"}))
        with self.assertRaisesRegex(TranslationError, "known completed"):
            self.correct()
        self.assertFalse(self.receipt_path(grant).exists())

    def test_candidate_file_cannot_be_corrected(self):
        grant = self.skeleton_grant(extra=lambda logs: (logs / "attempt-1/candidate.json").write_bytes(b""))
        with self.assertRaisesRegex(TranslationError, "known completed"):
            self.correct()
        self.assertFalse(self.receipt_path(grant).exists())

    def test_terminal_rejected_journal_cannot_be_corrected(self):
        def reject(logs):
            path = logs / "journal.json"
            value = core.read_json(path)
            value["state"] = "rejected"
            core.write_json(path, value)
        grant = self.skeleton_grant(extra=reject)
        with self.assertRaisesRegex(TranslationError, "known completed"):
            self.correct()
        self.assertFalse(self.receipt_path(grant).exists())

    def test_all_selected_locks_required_before_any_receipt(self):
        grant = self.skeleton_grant(("fr", "ru"))
        for locale in ("fr", "ru"):
            for name in ("worker.lock", "attempt.lock"):
                with self.subTest(locale=locale, name=name), (self.folder / locale / name).open("rb") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(TranslationError, "active"):
                        self.correct(("fr", "ru"))
                    self.assertFalse(self.receipt_path(grant).parent.exists())
        with (self.root / ".authoring.lock").open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "locked"):
                self.correct(("fr", "ru"))
        (self.folder / "ru/attempt.lock").unlink()
        with self.assertRaisesRegex(TranslationError, "Missing original"):
            self.correct(("fr", "ru"))
        self.assertFalse(self.receipt_path(grant).parent.exists())

    def test_any_existing_renewal_evidence_prevents_reauthorization(self):
        grant = self.skeleton_grant()
        self.correct()
        renewed = self.root / "work/translations" / grant["scope"] / "fr"
        renewed.mkdir()
        for name in (None, "worker.lock", "candidate.json"):
            with self.subTest(name=name):
                if name:
                    (renewed / name).touch()
                with self.assertRaises(TranslationError):
                    self.correct()
                if name:
                    (renewed / name).unlink()
        renewed.rmdir()
        self.attempted(count=4, scope=grant["scope"])
        before = self.receipt_path(grant).read_bytes()
        with self.assertRaisesRegex(TranslationError, "used budgets"):
            self.correct()
        self.assertEqual(self.receipt_path(grant).read_bytes(), before)
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["scope"], grant["scope"])
        self.assertEqual(core.read_json(renewed / "journal.json")["attempt"], 4)

    def test_orphan_renewal_evidence_blocks_first_correction(self):
        grant = self.skeleton_grant()
        self.attempted(scope=grant["scope"])
        with self.assertRaisesRegex(TranslationError, "Unexpected renewal"):
            self.correct()
        self.assertFalse(self.receipt_path(grant).exists())

    def test_changed_source_prompt_history_prior_destination_and_request_are_refused(self):
        grant = self.skeleton_grant()
        changes = [
            (family.family_review_path(self.root, self.run, "format"), lambda value: value + b"\n"),
            (self.root / self.run / "en/stage-0/quiz.md", lambda value: value + b"changed"),
            (self.root / "reference/formats/spec.txt", lambda value: value + b"changed"),
            (self.folder / "fr/attempt-1/raw.sse", lambda value: value + b"changed"),
            (self.folder / "fr/attempt-1/request.json", lambda value: value + b"changed"),
        ]
        for path, change in changes:
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(change(before))
                with self.assertRaises(ValueError):
                    self.correct()
                path.write_bytes(before)
        with patch("lesson_factory.translation_payload.build_prompt", return_value="changed prompt"):
            with self.assertRaisesRegex(TranslationError, "payload/provenance"):
                self.correct()
        destination = self.root / self.run / "fr/stage-0/quiz.md"
        destination.write_text("Unapproved destination edit")
        with self.assertRaises(ValueError):
            self.correct()
        destination.unlink()
        self.assertFalse(self.receipt_path(grant).exists())
        self.correct()
        path = self.folder / "fr/attempt-1/raw.sse"
        path.write_bytes(path.read_bytes() + b"drift")
        with self.assertRaisesRegex(TranslationError, "prior evidence drift"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_receipt_tamper_duplicate_wrong_binding_and_symlink_rejected(self):
        grant = self.skeleton_grant()
        self.correct()
        path = self.receipt_path(grant)
        before = path.read_bytes()
        original_receipt = json.loads(before)
        bad = json.loads(before)
        bad["correction"]["reason"] = "tampered"
        core.write_json(path, bad)
        with self.assertRaisesRegex(TranslationError, "digest"):
            recovery.recovery_for_locale(self.root, self.run, "fr")
        for field, value in (("grant_sha256", "0" * 64), ("prior_sha256", "0" * 64), ("new_attempt_limit", 5), ("operator", False), ("locale", "ru")):
            with self.subTest(field=field):
                bad = json.loads(before)
                bad["correction"][field] = value
                bad["sha256"] = digest(bad["correction"])
                core.write_json(path, bad)
                with self.assertRaisesRegex(TranslationError, "binding"):
                    recovery.recovery_for_locale(self.root, self.run, "fr")
        bad = json.loads(before)
        bad["correction"]["request_summary"]["hash"] = "0" * 64
        bad["sha256"] = digest(bad["correction"])
        core.write_json(path, bad)
        with self.assertRaisesRegex(TranslationError, "request/provenance"):
            recovery.recovery_for_locale(self.root, self.run, "fr")
        path.write_bytes(before)
        duplicate = path.with_name("fr-copy.json")
        core.write_json(duplicate, original_receipt)
        with self.assertRaisesRegex(TranslationError, "duplicate"):
            self.correct()
        duplicate.unlink()
        path.unlink()
        path.symlink_to(self.root / "family_runs/de/000020/family-context.json")
        with self.assertRaisesRegex(ValueError, "Unexpected|Symlink"):
            self.correct()

    def test_changed_reason_and_selected_scope_cannot_replace_correction(self):
        grant = self.skeleton_grant(("fr", "ru"))
        self.correct(("fr", "ru"))
        before = self.receipt_path(grant).read_bytes()
        for locales, reason in ((("fr",), "Explicit synthetic false-positive correction"), (("fr", "ru"), "New authorization")):
            with self.subTest(locales=locales, reason=reason), self.assertRaisesRegex(TranslationError, "exact same selected"):
                self.correct(locales, reason=reason)
        self.assertEqual(self.receipt_path(grant).read_bytes(), before)

    def test_partial_receipt_install_resumes_exact_scope_without_rewrites(self):
        grant = self.skeleton_grant(("fr", "ru"))
        real = recovery.atomic_write
        def interrupt(path, data):
            if path.name == "ru.json":
                raise OSError("synthetic receipt interruption")
            real(path, data)
        with patch.object(recovery, "atomic_write", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "receipt interruption"):
                self.correct(("fr", "ru"))
        first = self.receipt_path(grant).read_bytes()
        self.assertFalse(self.receipt_path(grant, "ru").exists())
        with self.assertRaisesRegex(TranslationError, "exact same selected"):
            self.correct(("ru",))
        self.correct(("ru", "fr"))
        self.assertEqual(self.receipt_path(grant).read_bytes(), first)
        self.assertTrue(self.receipt_path(grant, "ru").exists())

    def test_crash_after_atomic_receipt_is_idempotent(self):
        grant = self.skeleton_grant()
        real = recovery.atomic_write
        def interrupt(path, data):
            real(path, data)
            raise OSError("synthetic after atomic receipt")
        with patch.object(recovery, "atomic_write", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "after atomic receipt"):
                self.correct()
        before = self.receipt_path(grant).read_bytes()
        self.correct()
        self.assertEqual(self.receipt_path(grant).read_bytes(), before)

    def test_pre_rename_temporary_receipt_crash_fails_closed(self):
        grant = self.skeleton_grant()
        def interrupt(path, data):
            path.with_name(".translation-interrupted").write_bytes(data)
            raise OSError("synthetic pre-rename interruption")
        with patch.object(recovery, "atomic_write", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "pre-rename interruption"):
                self.correct()
        orphan = self.receipt_path(grant).with_name(".translation-interrupted")
        before = orphan.read_bytes()
        self.assertFalse(self.receipt_path(grant).exists())
        for action in (self.correct, lambda: recovery.recovery_for_locale(self.root, self.run, "fr")):
            with self.assertRaisesRegex(TranslationError, "Unexpected or duplicate"):
                action()
        self.assertEqual(orphan.read_bytes(), before)
        self.assertFalse(self.receipt_path(grant).exists())

    def test_partial_correction_cannot_resume_after_selected_lineage_used(self):
        grant = self.skeleton_grant(("fr", "ru"))
        real = recovery.atomic_write
        def interrupt(path, data):
            if path.name == "ru.json":
                raise OSError("synthetic receipt interruption")
            real(path, data)
        with patch.object(recovery, "atomic_write", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.correct(("fr", "ru"))
        self.attempted(scope=grant["scope"])
        with self.assertRaisesRegex(TranslationError, "used budgets"):
            self.correct(("fr", "ru"))
        self.assertFalse(self.receipt_path(grant, "ru").exists())

    def test_unselected_consumed_sibling_is_not_bound_or_locked(self):
        grant = self.skeleton_grant()
        self.attempted("it", count=4, scope=grant["scope"])
        for name in ("worker.lock", "attempt.lock"):
            with (self.folder / "it" / name).open("rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.correct()
        before = self.receipt_path(grant).read_bytes()
        (self.folder / "it/attempt-1/raw.sse").write_bytes(b"unrelated sibling drift")
        renewed = self.root / "work/translations" / grant["scope"] / "it"
        (renewed / "attempt-4/raw.sse").write_bytes(b"independent sibling event")
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "retry")
        self.correct()
        self.assertEqual(self.receipt_path(grant).read_bytes(), before)
        with self.assertRaisesRegex(TranslationError, "prior evidence drift"):
            recovery.recovery_for_locale(self.root, self.run, "it")

    def test_future_complete_renewed_output_prevents_repurchase(self):
        grant = self.skeleton_grant()
        self.correct()
        logs = self.attempted(scope=grant["scope"])
        (logs / "attempt-1/raw.sse").write_bytes(self.wire({"type": "response.output_item.done", "item": {"phase": "commentary", "content": [{"text": json.dumps(self.candidate("real wrong content"))}]}}))
        with self.assertRaisesRegex(TranslationError, "offline recovery"):
            recovery.recovery_for_locale(self.root, self.run, "fr")
        with self.assertRaises(TranslationError):
            self.correct()


if __name__ == "__main__":
    unittest.main()
