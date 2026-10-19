import fcntl
import json
import unittest
from unittest.mock import patch

from lesson_factory import core, family, slow_retry_recovery as slow, translation_recovery as recovery
from lesson_factory.translation import RetryPolicy, TranslationError, digest, encode, retry_status
from tests import test_translation_recovery


class SlowRetryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_translation_recovery.TranslationRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run = self.fixture.root, self.fixture.run
        self.old = self.fixture.prepare_retry(4)
        self.grant = self.fixture.authorize()
        self.logs = self.fixture.attempted(count=4, scope=self.grant["scope"])
        self.grant_dir = self.logs.parent
        self.reason = "Explicit synthetic additional attempts with repeated thirty-minute waits"
        self.created = 2000.0
        for number in range(1, 5):
            self.failure(number)

    def failure(self, number, **changes):
        path = self.logs / f"attempt-{number}/journal.json"
        record = core.read_json(path)
        record.update(state="uncertain", error_code="remote_eof", retry_eligible=number < 4, retry_at=1900 if number < 4 else None, retry_after=None, failed_at=1800.0)
        record.update(changes)
        core.write_json(path, record)
        if number == 4:
            core.write_json(self.logs / "journal.json", record)
        return record

    def authorize(self, **kwargs):
        return slow.authorize_slow_retries(self.root, self.run, ["fr"], reason=self.reason, operator=True, clock=lambda: self.created, **kwargs)

    def plan(self):
        return slow.plan_slow_retries(self.root, self.run, ["fr"], reason=self.reason, clock=lambda: self.created)

    def policy(self):
        return slow.load_slow_retry_policy(self.root, self.run, "fr")

    def terminal(self, response, number=4, extra=(), **changes):
        record = self.failure(number, **{"state": "provider_failed", "error_code": "max_output_tokens", **changes})
        folder = self.logs / f"attempt-{number}"
        values = [*extra, {"type": "response." + response["status"], "response": response}]
        (folder / "raw.sse").write_bytes(b"".join(b"data: " + json.dumps(value).encode() + b"\n\n" for value in values))
        core.write_json(folder / "response.json", response)
        return record

    def test_plan_read_only_and_default_requires_explicit_operator(self):
        before = recovery._inventory(self.root, ".")
        plan = self.plan()
        self.assertFalse(plan["active"])
        self.assertEqual(plan["extension_receipts"]["fr"]["policy"]["max_attempts"], 8)
        self.assertEqual(recovery._inventory(self.root, "."), before)
        with self.assertRaisesRegex(TranslationError, "operator=True"):
            slow.authorize_slow_retries(self.root, self.run, ["fr"], reason=self.reason)
        self.assertIsNone(self.policy())

    def test_immutable_receipt_preserves_every_existing_byte_and_restart_due(self):
        before = recovery._inventory(self.root, ".")
        first = self.authorize()
        after = recovery._inventory(self.root, ".")
        self.assertTrue(first["active"])
        for name, value in before.items():
            self.assertEqual(after[name], value, name)
        self.assertEqual(set(after) - set(before), {f"work/translations/{self.grant['scope']}/extended-retries", f"work/translations/{self.grant['scope']}/extended-retries/fr.json"})
        self.assertIsInstance(self.policy(), RetryPolicy)
        due = self.policy()["extension_receipt"]["earliest_due"]
        self.assertEqual(due, 3800.01)
        self.created = 9000
        self.assertEqual(self.authorize(), first)
        self.assertEqual(self.policy()["extension_receipt"]["earliest_due"], due)
        self.assertEqual(retry_status(self.logs / "journal.json", self.policy()), {"retry_eligible": True, "retry_at": due, "max_attempts": 8})
        self.assertEqual(recovery._inventory(self.root, "."), after)

    def test_retry_after_existing_due_and_failure_time_are_not_shortened(self):
        self.failure(4, retry_after=4000, retry_at=11000, failed_at=10000)
        core.write_json(self.logs / "attempt-4/response-headers.json", {"Retry-After": "7000"})
        self.assertEqual(self.authorize()["extension_receipts"]["fr"]["policy"]["extension_receipt"]["earliest_due"], 11800.01)
        self.created = 50000
        self.assertEqual(self.policy()["extension_receipt"]["earliest_due"], 11800.01)

    def test_four_attempts_required_and_never_extends_original_scope(self):
        self.failure(4, attempt=3)
        with self.assertRaisesRegex(TranslationError, "four exhausted"):
            self.authorize()
        self.assertFalse((self.grant_dir / "extended-retries").exists())

    def test_original_and_renewal_worker_attempt_locks_are_nonblocking(self):
        for directory in (self.old, self.logs):
            for name in ("worker.lock", "attempt.lock"):
                with self.subTest(directory=directory, name=name), (directory / name).open("rb") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(TranslationError, "active"):
                        self.authorize()
                    fcntl.flock(handle, fcntl.LOCK_UN)
        self.assertFalse((self.grant_dir / "extended-retries").exists())

    def test_missing_lock_unknown_files_and_symlinks_fail_closed(self):
        for name in ("worker.lock", "attempt.lock"):
            path = self.logs / name
            path.unlink()
            with self.assertRaisesRegex(TranslationError, "Missing renewal"):
                self.authorize()
            path.touch()
        for relative in ("orphan.json", "attempt-3/unknown.json", "attempt-5/unknown.json", "attempt-1/candidate.json"):
            path = self.logs / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"{}")
            with self.subTest(relative=relative), self.assertRaises(TranslationError):
                self.authorize()
            path.unlink()
            if relative.startswith("attempt-5/"):
                path.parent.rmdir()
        path = self.logs / "attempt-3/linked"
        path.symlink_to(self.logs / "attempt-2/request.json")
        with self.assertRaisesRegex(TranslationError, "Symlink"):
            self.authorize()

    def test_source_grant_old_scope_destination_and_request_drift_block(self):
        self.authorize()
        paths = [family.family_review_path(self.root, self.run, "format"), self.grant_dir / "grant.json", self.old / "attempt-1/raw.sse", self.logs / "attempt-2/request.json"]
        for path in paths:
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.policy()
            path.write_bytes(original)
        destination = family.family_locale_path(self.root, self.run, "fr") / "stage-0/quiz.md"
        destination.write_text("External partial edit")
        with self.assertRaisesRegex(TranslationError, "destination drift"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_prefix_hash_remains_valid_when_fifth_attempt_is_appended(self):
        self.authorize()
        policy = self.policy()
        original = recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix())
        folder = self.logs / "attempt-5"
        folder.mkdir()
        for name in ("request.json", "summary.json", "raw.sse"):
            (folder / name).write_bytes((self.logs / "attempt-4" / name).read_bytes())
        record = core.read_json(self.logs / "journal.json")
        record.update(attempt=5, request_id="synthetic-5", retry_policy=dict(policy), retry_eligible=True, retry_at=8000.01, retry_scheduled_at=6000, failed_at=6000)
        core.write_json(folder / "journal.json", record)
        core.write_json(self.logs / "journal.json", record)
        policy.validate()
        self.assertEqual(dict(self.policy()), dict(policy))
        self.assertEqual(retry_status(self.logs / "journal.json", self.policy())["retry_at"], 8000.01)
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "retry")
        self.assertEqual(self.policy()["max_attempts"], 8)
        for name, value in original.items():
            if name != "journal.json":
                self.assertEqual(recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix())[name], value)

    def test_pending_fifth_attempt_keeps_receipt_valid_without_retry_permission(self):
        self.authorize()
        policy = self.policy()
        folder = self.logs / "attempt-5"
        folder.mkdir()
        for name in ("request.json", "summary.json"):
            (folder / name).write_bytes((self.logs / "attempt-4" / name).read_bytes())
        record = core.read_json(self.logs / "journal.json")
        record.update(attempt=5, state="pending", request_id="synthetic-5", retry_policy=dict(policy), retry_eligible=False, retry_at=None)
        core.write_json(folder / "journal.json", record)
        core.write_json(self.logs / "journal.json", record)
        policy.validate()
        (folder / "raw.sse").write_bytes(b"")
        self.assertFalse(retry_status(self.logs / "journal.json", policy)["retry_eligible"])
        (folder / "unexpected.txt").write_bytes(b"unexpected")
        with self.assertRaisesRegex(TranslationError, "Unexpected"):
            self.policy()

    def test_successful_fifth_attempt_preserves_locale_after_empty_completed_anchor(self):
        from lesson_factory.family_translation import publish
        self.terminal({"status": "completed", "output": []}, state="rejected", error_code="validation_rejected")
        core.write_json(self.logs / "attempt-4/raw-candidate.json", [])
        self.authorize()
        policy = self.policy()
        folder = self.logs / "attempt-5"
        folder.mkdir()
        for name in ("request.json", "summary.json"):
            (folder / name).write_bytes((self.logs / "attempt-4" / name).read_bytes())
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson" if stage == 0 else "", "quiz_md": "quiz", "adaptation_notes": []} for stage in family.STAGES]}
        core.write_json(folder / "candidate.json", candidate)
        core.write_json(self.logs / "candidate.json", candidate)
        (folder / "raw.sse").write_bytes(b"data: {\"type\":\"response.completed\"}\n\n")
        record = core.read_json(self.logs / "journal.json")
        record.update(attempt=5, state="completed", request_id="synthetic-5", retry_policy=dict(policy), retry_eligible=False, retry_at=None, output_hash=digest(encode(candidate)))
        core.write_json(folder / "journal.json", record)
        core.write_json(self.logs / "journal.json", record)
        destination = family.family_locale_path(self.root, self.run, "fr")
        initial = recovery._inventory(self.root, f"{self.run}/fr")
        scaffold = family.family_locale_scaffold(self.root, self.run, "fr")
        publish(destination, self.logs, candidate, record, initial, lambda: None, scaffold=scaffold)
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "preserve")
        self.assertFalse(retry_status(self.logs / "journal.json", self.policy())["retry_eligible"])

    def test_receipt_scope_reason_prefix_and_due_cannot_be_changed(self):
        self.authorize()
        self.reason = "different authorization"
        with self.assertRaisesRegex(TranslationError, "exact same scope and reason"):
            self.authorize()
        path = self.grant_dir / "extended-retries/fr.json"
        original = path.read_bytes()
        receipt = core.read_json(path)
        receipt["policy"]["extension_receipt"]["earliest_due"] += 60
        receipt["sha256"] = digest({key: value for key, value in receipt.items() if key != "sha256"})
        core.write_json(path, receipt)
        with self.assertRaisesRegex(TranslationError, "due time"):
            self.policy()
        path.write_bytes(original)
        (self.logs / "attempt-1/raw.sse").write_bytes(b"changed historical evidence")
        with self.assertRaisesRegex(TranslationError, "prefix evidence"):
            self.policy()

    def test_unknown_receipt_files_and_interrupted_writes_are_not_replaced(self):
        self.authorize()
        folder = self.grant_dir / "extended-retries"
        for name in ("fr.json.tmp", "xx.json"):
            path = folder / name
            path.write_bytes(b"{}")
            with self.subTest(name=name), self.assertRaisesRegex(TranslationError, "Unexpected"):
                self.policy()
            path.unlink()
        path = folder / "fr.json"
        path.write_bytes(b'{"policy":')
        with self.assertRaises(ValueError):
            self.authorize()
        self.assertEqual(path.read_bytes(), b'{"policy":')

    def test_interruption_before_first_file_fails_closed(self):
        with patch.object(slow, "_write_receipt", side_effect=OSError("synthetic interruption")):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.authorize()
        with self.assertRaisesRegex(TranslationError, "Interrupted empty"):
            self.authorize()
        self.assertEqual(list((self.grant_dir / "extended-retries").iterdir()), [])

    def test_source_change_between_plan_and_install_refuses_receipt(self):
        original = slow.plan_slow_retries
        calls = 0

        def changing(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 1:
                path = family.family_review_path(self.root, self.run, "format")
                path.write_bytes(path.read_bytes() + b" ")
            return result

        with patch.object(slow, "plan_slow_retries", side_effect=changing):
            with self.assertRaisesRegex(TranslationError, "evidence drift"):
                self.authorize()
        self.assertFalse((self.grant_dir / "extended-retries").exists())

    def test_proven_empty_provider_max_tokens_and_completed_are_eligible(self):
        cases = [({"status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}}, {}), ({"status": "completed", "output": []}, {"state": "rejected", "error_code": "validation_rejected"})]
        for response, changes in cases:
            with self.subTest(status=response["status"]):
                self.terminal(response, **changes)
                self.assertFalse(self.plan()["active"])
        core.write_json(self.logs / "attempt-4/raw-candidate.json", [])
        self.authorize()
        self.assertTrue(retry_status(self.logs / "journal.json", self.policy())["retry_eligible"])
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "retry")

    def test_partial_completed_ambiguous_refusal_and_candidate_evidence_block(self):
        path = self.logs / "attempt-4"
        baseline = {name: (path / name).read_bytes() for name in ("journal.json", "raw.sse")}
        responses = [{"status": "completed"}, {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}]}, {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]}, {"status": "incomplete", "output": [], "incomplete_details": {"reason": "context_length_exceeded"}}]
        for response in responses:
            self.terminal(response)
            with self.subTest(response=response), self.assertRaises(TranslationError):
                self.authorize()
            (path / "response.json").unlink()
            for name, value in baseline.items():
                (path / name).write_bytes(value)
            (self.logs / "journal.json").write_bytes(baseline["journal.json"])
        self.failure(4)
        (path / "raw.sse").write_bytes(b'data: {"type":"response.output_text.delta","delta":"partial retained output"}\n\n')
        with self.assertRaisesRegex(TranslationError, "Retained output"):
            self.authorize()

    def test_authorized_terminal_partial_failure_retains_bytes_and_grants_same_lineage(self):
        partial = '{"stages":[{"stage":0,"lesson_md":"unfinished'
        cases = [
            {"status": "failed", "error": {"code": "upstream_stream_read_error"}},
            {"status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}},
        ]
        self.terminal(cases[0], number=2, error_code="upstream_stream_read_error", extra=[{"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": "synthetic-earlier", "delta": partial}])
        events = [
            {"type": "codex.rate_limits", "additional_rate_limits": [], "code_review_rate_limits": {}, "credits": {}, "plan_type": "synthetic", "promo": None, "rate_limits": {}},
            {"type": "codex.response.metadata", "headers": {}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": "synthetic-earlier", "delta": partial},
            {"type": "error", "code": "upstream_stream_read_error", "message": "synthetic transport failure", "param": None, "sequence_number": 4},
        ]
        (self.logs / "attempt-2/raw.sse").write_bytes(b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events))
        core.write_json(self.logs / "attempt-2/response.json", {"status": "failed", "error": events[-1], "usage": None})
        for response in cases:
            with self.subTest(status=response["status"]):
                code = "max_output_tokens" if response["status"] == "incomplete" else "upstream_stream_read_error"
                self.terminal(response, error_code=code, extra=[{"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": "synthetic-message", "delta": partial}])
                before = recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix())
                plan = self.plan()
                self.assertEqual(plan["extension_receipts"]["fr"]["policy"]["max_attempts"], 8)
                self.assertEqual(recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix()), before)
        before = recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix())
        self.authorize()
        self.assertTrue(retry_status(self.logs / "journal.json", self.policy())["retry_eligible"])
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["scope"], self.grant["scope"])
        self.assertEqual(recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix()), before)

    def test_exact_empty_skeleton_before_terminal_partial_failure_is_not_usable_output(self):
        skeleton = {"stages": [{"stage": stage, "lesson_md": "", "quiz_md": "", "adaptation_notes": []} for stage in family.STAGES]}
        text = json.dumps(skeleton)
        terminal = {"status": "failed", "error": {"code": "upstream_stream_read_error"}}
        events = [
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": "synthetic-skeleton", "delta": text},
            {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "item_id": "synthetic-skeleton", "text": text},
            {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "item_id": "synthetic-partial", "delta": '{"stages":[{"stage":0,"lesson_md":"unfinished'},
        ]
        self.terminal(terminal, number=3, error_code="upstream_stream_read_error", extra=events)
        self.terminal({"status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}}, extra=[events[-1]])
        before = recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix())
        self.authorize()
        self.assertTrue(retry_status(self.logs / "journal.json", self.policy())["retry_eligible"])
        self.assertEqual(recovery._inventory(self.root, self.logs.relative_to(self.root).as_posix()), before)

    def test_terminal_partial_recovery_refuses_recoverable_full_family_and_refusal(self):
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson" if stage == 0 else "", "quiz_md": "quiz", "adaptation_notes": []} for stage in family.STAGES]}
        terminal = {"status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}}
        cases = [
            [{"type": "response.output_text.delta", "delta": json.dumps(candidate)}],
            [{"type": "response.refusal.delta", "delta": "Refusal"}],
            [{"type": "response.output_text.delta", "delta": '{"stages":['}, {"type": "response.completed", "response": {"status": "completed"}}],
        ]
        for events in cases:
            with self.subTest(events=[event["type"] for event in events]):
                self.terminal(terminal, extra=events)
                with self.assertRaisesRegex(TranslationError, "Retained output"):
                    self.authorize()
        self.assertFalse((self.grant_dir / "extended-retries").exists())

    def test_earlier_completed_evidence_and_permanent_failures_never_extend(self):
        for code in ("cancelled", "generation_deadline", "authentication_error", "billing_error", "context_length_exceeded"):
            self.failure(4, error_code=code)
            with self.subTest(code=code), self.assertRaises(TranslationError):
                self.authorize()
        self.failure(4)
        self.terminal({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "real output"}]}]}, number=1)
        with self.assertRaisesRegex(TranslationError, "Retained output"):
            self.authorize()

    def test_nonretry_locale_cannot_be_added_to_selected_scope(self):
        self.fixture.scaffold("es")
        with self.assertRaisesRegex(TranslationError, "Missing renewal|existing eligible renewal"):
            slow.authorize_slow_retries(self.root, self.run, ["fr", "es"], reason=self.reason, operator=True, clock=lambda: self.created)
        self.assertFalse((self.grant_dir / "extended-retries").exists())

    def test_other_authorized_locale_pending_write_does_not_block_selected_policy(self):
        fixture = test_translation_recovery.TranslationRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for locale in ("fr", "es"):
            fixture.scaffold(locale)
            fixture.attempted(locale, count=4)
        grant = fixture.authorize()
        logs = {}
        for locale in ("fr", "es"):
            logs[locale] = fixture.attempted(locale, count=4, scope=grant["scope"])
            for number in range(1, 5):
                path = logs[locale] / f"attempt-{number}/journal.json"
                record = core.read_json(path)
                record.update(state="uncertain", error_code="remote_eof", failed_at=1800, retry_after=None, retry_at=None)
                core.write_json(path, record)
                if number == 4:
                    core.write_json(logs[locale] / "journal.json", record)
        slow.authorize_slow_retries(fixture.root, fixture.run, ["fr", "es"], reason=self.reason, operator=True, clock=lambda: self.created)
        policy = slow.load_slow_retry_policy(fixture.root, fixture.run, "es")
        pending = logs["fr"] / "attempt-5"
        pending.mkdir()
        (pending / "request.json").write_bytes((logs["fr"] / "attempt-4/request.json").read_bytes())
        (logs["fr"] / ".translation-synthetic").write_bytes(b"pending write")
        policy.validate()
        self.assertTrue(retry_status(logs["es"] / "journal.json", policy)["retry_eligible"])
        with self.assertRaisesRegex(TranslationError, "Unexpected"):
            slow.load_slow_retry_policy(fixture.root, fixture.run, "fr")

    def test_incomplete_multi_locale_receipt_set_fails_closed(self):
        self.authorize()
        path = self.grant_dir / "extended-retries/fr.json"
        receipt = core.read_json(path)
        receipt["selected_locales"] = ["es", "fr"]
        receipt["sha256"] = digest({key: value for key, value in receipt.items() if key != "sha256"})
        core.write_json(path, receipt)
        with self.assertRaisesRegex(TranslationError, "Interrupted or overlapping"):
            self.policy()
        with self.assertRaisesRegex(TranslationError, "Interrupted or overlapping"):
            self.authorize()


if __name__ == "__main__":
    unittest.main()
