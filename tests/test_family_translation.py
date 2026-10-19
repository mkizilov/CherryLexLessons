import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lesson_factory import core, family, family_translation
from lesson_factory.family_translation import inventory, publish
from lesson_factory.translation import TranslationError, digest, encode, execute, parse_candidate, prepare, validate_candidate


def candidate():
    return {"stages": [{"stage": stage, "lesson_md": "lesson" if stage != 2 else "", "quiz_md": "quiz", "adaptation_notes": []} for stage in range(5)]}


def response(value):
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}]}


class FamilyCandidateTests(unittest.TestCase):
    def test_strict_family_policy(self):
        value = candidate()
        self.assertEqual(validate_candidate(value, reviewed_practice_stages=(2,)), value)
        self.assertEqual(parse_candidate(response(value), reviewed_practice_stages=(2,)), value)
        cases = []
        missing = copy.deepcopy(value)
        missing["stages"].pop()
        cases.append(missing)
        reordered = copy.deepcopy(value)
        reordered["stages"].reverse()
        cases.append(reordered)
        extra = copy.deepcopy(value)
        extra["other"] = 1
        cases.append(extra)
        boolean = copy.deepcopy(value)
        boolean["stages"][0]["stage"] = False
        cases.append(boolean)
        empty = copy.deepcopy(value)
        empty["stages"][1]["lesson_md"] = ""
        cases.append(empty)
        quiz = copy.deepcopy(value)
        quiz["stages"][4]["quiz_md"] = ""
        cases.append(quiz)
        for bad in cases:
            with self.assertRaises(TranslationError):
                validate_candidate(bad, reviewed_practice_stages=(2,))
        with self.assertRaises(TranslationError):
            validate_candidate(value)
        with self.assertRaises(TranslationError):
            validate_candidate(value, reviewed_practice_stages=(2, 2))

    def test_single_pair_candidate_and_request_schema_are_rejected(self):
        pair = {key: value for key, value in candidate()["stages"][0].items() if key != "stage"}
        with self.assertRaisesRegex(TranslationError, "five ordered stages"):
            validate_candidate(pair)
        with self.assertRaisesRegex(TranslationError, "five ordered stages"):
            parse_candidate(response(pair))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body, summary = prepare({"study_language": "de", "source_locale": "en"}, "fr", "prompt")
            body["text"]["format"]["schema"] = {"type": "object", "properties": {name: {"type": "string"} for name in pair}}
            summary.update(hash=digest(body), bytes=len(encode(body)))
            with self.assertRaisesRegex(TranslationError, "guarded family schema"):
                execute(root, body, summary, lambda _: True, gate=lambda: True, opener=lambda *_: self.fail("dispatch"))
            self.assertFalse((root / "work/translations/default/fr/journal.json").exists())

    def test_family_body_and_whole_body_guard(self):
        context = {"study_language": "de", "source_locale": "en"}
        body, summary = prepare(context, "fr", "prompt")
        self.assertEqual(body["text"]["format"]["name"], "localized_family")
        self.assertEqual(summary["bytes"], len(encode(body)))
        self.assertNotIn("max_output_tokens", body)
        self.assertEqual(body["text"]["format"]["schema"]["properties"]["stages"]["minItems"], 5)
        with self.assertRaisesRegex(TranslationError, "nothing truncated"):
            prepare(context, "fr", "prompt", summary["bytes"] - 1)

    def test_family_transport_budget_includes_initial_source_gate(self):
        import time
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            (root / "secrets/contactbox_key").write_text("offline-fixture")
            body, summary = prepare({"study_language": "de", "source_locale": "en"}, "fr", "prompt")
            calls = []
            gates = []
            def gate():
                gates.append(True)
                if len(gates) in (1, 2):
                    time.sleep(0.1)
            wire = b"data: " + encode({"type": "response.completed", "response": response(candidate())}) + b"\n\n"
            def opener(request, timeout):
                calls.append(timeout)
                return io.BytesIO(wire)
            record = execute(root, body, summary, lambda _: True, gate=gate, opener=opener, wall_timeout=0.5, reviewed_practice_stages=(2,))
            self.assertEqual(len(calls), 1)
            self.assertGreater(calls[0], 0)
            self.assertLess(calls[0], 0.31)
            self.assertEqual(record["transport"]["socket_timeout_seconds"], 180)
            self.assertGreater(record["transport"]["wall_timeout_seconds"], 0)
            self.assertLess(record["transport"]["wall_timeout_seconds"], 0.41)
            self.assertEqual(record["transport"]["termination_reason"], "provider_terminal")

    def test_ten_initial_requests_not_fifty_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            (root / "secrets/contactbox_key").write_text("offline-fixture")
            calls = []
            value = candidate()
            wire = b"data: " + encode({"type": "response.completed", "response": response(value)}) + b"\n\n"
            def opener(request, timeout):
                calls.append(request)
                return io.BytesIO(wire)
            for locale in ("ar", "es", "fr", "hi", "it", "ja", "ko", "pt", "ru", "zh"):
                body, summary = prepare({"study_language": "de", "source_locale": "en"}, locale, "prompt")
                options = dict(scope="families/de/000020", gate=lambda: True, opener=opener, reviewed_practice_stages=(2,))
                record = execute(root, body, summary, lambda _: True, **options)
                self.assertEqual(record["attempt"], 1)
                execute(root, body, summary, lambda _: True, resume=True, **options)
            self.assertEqual(len(calls), 10)

    def test_rejected_complete_offline_repair_never_repurchase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            (root / "secrets/contactbox_key").write_text("offline-fixture")
            body, summary = prepare({"study_language": "de", "source_locale": "en"}, "fr", "prompt")
            wire = b"data: " + encode({"type": "response.completed", "response": response(candidate())}) + b"\n\n"
            options = dict(scope="families/de/000020", gate=lambda: True, reviewed_practice_stages=(2,))
            with self.assertRaisesRegex(TranslationError, "Structural"):
                execute(root, body, summary, lambda _: False, opener=lambda *a, **k: io.BytesIO(wire), **options)
            retained = root / "work/translations/families/de/000020/fr/attempt-1/candidate.json"
            self.assertTrue(retained.exists())
            with self.assertRaisesRegex(TranslationError, "offline repair"):
                execute(root, body, summary, lambda _: True, resume=True, **options)
            repair = root / "repair.json"
            repair.write_bytes(encode(candidate()))
            record = execute(root, body, summary, lambda _: True, repair_candidate=repair, **options)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["attempt"], 1)


class FamilyPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.destination = self.root / "family_runs/de/000020/fr"
        self.destination.parent.mkdir(parents=True)
        self.logs = self.root / "work/translations/families/de/000020/fr"
        self.logs.mkdir(parents=True)
        self.record = {"input_hash": "input"}

    def tearDown(self):
        self.temp.cleanup()

    def test_single_rename_all_five_and_receipt(self):
        import os
        original = os.rename
        calls = []
        def rename(source, target):
            calls.append((source, target))
            original(source, target)
        with patch("lesson_factory.family_translation.os.rename", side_effect=rename):
            publish(self.destination, self.logs, candidate(), self.record, None, lambda: True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([name for name in inventory(self.destination) if name.endswith("quiz.md")]), 5)
        self.assertFalse(self.record["family_publication_pending"])
        self.assertTrue((self.logs / "generation.json").exists())

    def test_authoritative_scaffold_and_crash_rename_recovery(self):
        for stage in range(5):
            folder = self.destination / f"stage-{stage}"
            folder.mkdir(parents=True)
            if stage:
                (folder / "lesson.md").write_bytes(b"")
        initial = inventory(self.destination)
        scaffold = {f"stage-{stage}/lesson.md": digest(b"") for stage in range(1, 5)}
        import os
        original = os.rename
        def crash(source, target):
            original(source, target)
            if Path(target) == self.destination:
                raise OSError("crash after rename")
        with patch("lesson_factory.family_translation.os.rename", side_effect=crash):
            with self.assertRaisesRegex(OSError, "crash"):
                publish(self.destination, self.logs, candidate(), self.record, initial, lambda: True, scaffold=scaffold)
        retained = json.loads((self.logs / "journal.json").read_bytes())
        self.assertTrue(retained["family_publication_pending"])
        publish(self.destination, self.logs, candidate(), retained, inventory(self.destination), lambda: True)
        self.assertFalse(retained["family_publication_pending"])

    def test_partial_generation_crash_recovers_and_edited_receipt_blocks(self):
        from lesson_factory import family_translation
        original = family_translation.atomic_write
        def crash(path, data):
            original(path, data)
            if Path(path).name == "quiz.md":
                raise OSError("staging crash")
        with patch.object(family_translation, "atomic_write", side_effect=crash):
            with self.assertRaisesRegex(OSError, "staging crash"):
                publish(self.destination, self.logs, candidate(), self.record, None, lambda: True)
        retained = core.read_json(self.logs / "journal.json")
        self.assertTrue(retained["family_publication_pending"])
        publish(self.destination, self.logs, candidate(), retained, None, lambda: True)
        (self.logs / "generation.json").write_text("{}")
        with self.assertRaisesRegex(TranslationError, "receipt changed"):
            publish(self.destination, self.logs, candidate(), retained, inventory(self.destination), lambda: True)

    def test_source_and_destination_drift_publish_none(self):
        def drift():
            raise TranslationError("source drift")
        with self.assertRaisesRegex(TranslationError, "source drift"):
            publish(self.destination, self.logs, candidate(), self.record, None, drift)
        self.assertFalse(self.destination.exists())
        self.destination.mkdir()
        (self.destination / "expert.md").write_text("expert edit")
        with self.assertRaisesRegex(TranslationError, "drift"):
            publish(self.destination, self.logs, candidate(), self.record, None, lambda: True)
        self.assertEqual((self.destination / "expert.md").read_text(), "expert edit")

    def test_resume_never_overwrites_expert_edit(self):
        publish(self.destination, self.logs, candidate(), self.record, None, lambda: True)
        (self.destination / "stage-3/quiz.md").write_text("expert")
        with self.assertRaisesRegex(TranslationError, "authoritative"):
            publish(self.destination, self.logs, candidate(), self.record, inventory(self.destination), lambda: True)
        self.assertEqual((self.destination / "stage-3/quiz.md").read_text(), "expert")


class FamilyWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_family_core import FamilyCoreTests
        self.fixture = FamilyCoreTests()
        self.fixture.setUp()
        self.root, self.run = self.fixture.root, self.fixture.run
        self.actual_context = family.load_family(self.root, self.run)
        for stage in range(5):
            folder = family.family_locale_path(self.root, self.run, "en", stage)
            (folder / "lesson.md").write_text("source" if stage == 0 else "")
            (folder / "quiz.md").write_text("source quiz")
            core.write_json(folder / "adaptation-notes.json", [])
            core.write_json(self.root / self.run / f"stage-{stage}/brief.json", {"extension": {"mode": "practice"}})
        (self.root / "prompts").mkdir()
        (self.root / "prompts/localize-family.md").write_text("synthetic prompt")
        formats = self.root / "reference/formats/grammar"
        formats.mkdir()
        for name in ("GRAMMAR_MARKDOWN.md", "QUIZ_MARKDOWN.md", "conventions.json"):
            (formats / name).write_text("synthetic format")
        (self.root / "secrets").mkdir()
        (self.root / "secrets/contactbox_key").write_text("offline-fixture")
        from lesson_factory.source_validation import VALIDATOR_FILES
        for name in VALIDATOR_FILES:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('synthetic validator code')
        import argparse
        self.args = argparse.Namespace(root=self.root, run=self.run, all=True, lang=None, execute=True, resume=False, repair_candidate=None, workers=10, timeout=30, deadline=60, max_input_bytes=524288)
        self.context = self.actual_context
        self.contract = [{"stage": stage} for stage in range(5)]
        for role in ("pedagogical", "assessment", "format"):
            core.write_json(family.family_review_path(self.root, self.run, role), {"synthetic": True})
        self.calls = []

    def tearDown(self):
        self.fixture.tearDown()

    def execute(self, bad_locale=None, source_drift=False, barrier=None):
        def opener(request, timeout, **kwargs):
            body = json.loads(request.data)
            locale = json.loads(body["input"][1]["content"])["learner_language"]
            self.calls.append(locale)
            if barrier is not None:
                barrier.wait(timeout=10)
            if source_drift:
                (self.root / self.run / "family-context.json").write_text("source drift")
            value = candidate()
            for artifact in value["stages"][1:]:
                artifact["lesson_md"] = ""
            if locale == bad_locale:
                value["stages"][4]["quiz_md"] = ""
            return io.BytesIO(b"data: " + encode({"type": "response.completed", "response": response(value)}) + b"\n\n")
        import subprocess
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(family, "load_family", return_value=self.context))
            stack.enter_context(patch.object(family, "require_family_source_reviews", return_value=[]))
            stack.enter_context(patch.object(family, "validate_family", return_value={"ok": True, "family_contract": self.contract}))
            stack.enter_context(patch.object(family, "family_binding", return_value={"synthetic": True}))
            stack.enter_context(patch.object(family_translation.scheduler, "validate_process", return_value=subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "family_contract": self.contract}), "")))
            stack.enter_context(patch.object(family_translation.scheduler, "emit"))
            stack.enter_context(patch("lesson_factory.translation.open_response", side_effect=opener))
            family_translation.run(self.args)

    def test_actual_context_synthetic_minimum_and_maximum_request_sizes(self):
        from tests.quiz_fixtures import lesson_text, quiz_text
        project = Path(__file__).resolve().parents[1]
        prompt = (project / "prompts/localize-family.md").read_text()
        for name in ("GRAMMAR_MARKDOWN.md", "QUIZ_MARKDOWN.md", "conventions.json"):
            prompt += "\n\nFORMAT REFERENCE " + name + "\n" + (project / "reference/formats/grammar" / name).read_text()
        measurements = []
        for count in (25, 50):
            for stage in range(5):
                folder = family.family_locale_path(self.root, self.run, "en", stage)
                (folder / "lesson.md").write_text(lesson_text("base", ("base-page",)) if stage == 0 else "")
                (folder / "quiz.md").write_text(quiz_text(f"bank-{stage}", "base", "base-page", f"q-{stage}", count))
            envelope = family_translation.context_for_locale(self.root, self.run, self.actual_context, "fr", {"synthetic": True})
            _, summary = prepare(envelope, "fr", prompt)
            measurements.append(summary["bytes"])
            self.assertLessEqual(summary["bytes"], 524288)
        self.assertLess(measurements[0], measurements[1])
        print(json.dumps({"synthetic_question_counts": [125, 250], "request_bytes": measurements, "guard": 524288, "provider_capacity": "not measured"}))

    def test_all_ten_workers_dispatch_in_parallel(self):
        import threading
        self.execute(barrier=threading.Barrier(10))
        self.assertEqual(len(self.calls), 10)

    def test_worker_wall_budget_is_not_the_socket_timeout(self):
        self.args.all = False
        self.args.lang = "fr"
        with patch.object(family_translation, "execute", wraps=execute) as transport:
            self.execute()
        self.assertEqual(transport.call_count, 1)
        options = transport.call_args.kwargs
        self.assertEqual(options["timeout"], self.args.timeout)
        self.assertGreater(options["wall_timeout"], self.args.timeout)
        self.assertLessEqual(options["wall_timeout"], self.args.deadline)

    def test_timeout_change_preserves_request_identity_and_resume(self):
        self.args.all = False
        self.args.lang = "fr"
        self.execute()
        journal_path = self.root / "work/translations/families/de/000020/fr/journal.json"
        previous = core.read_json(journal_path)
        request = (journal_path.parent / "attempt-1/request.json").read_bytes()
        self.args.timeout = 15
        self.args.deadline = 120
        self.args.resume = True
        self.execute()
        current = core.read_json(journal_path)
        self.assertEqual(self.calls, ["fr"])
        self.assertEqual(current["input_hash"], previous["input_hash"])
        self.assertEqual(current["attempt"], previous["attempt"])
        self.assertEqual((journal_path.parent / "attempt-1/request.json").read_bytes(), request)

    def test_exact_source_drift_cancels_all_publication(self):
        with self.assertRaises(TranslationError):
            self.execute(source_drift=True)
        for locale in core.translation_locales("de"):
            self.assertFalse(any(family.family_locale_path(self.root, self.run, locale).rglob("quiz.md")))

    def test_offline_cli_repair_publishes_all_stages_without_request(self):
        self.args.all = False
        self.args.lang = "fr"
        with self.assertRaises(TranslationError):
            self.execute("fr")
        self.assertEqual(self.calls, ["fr"])
        value = candidate()
        for artifact in value["stages"][1:]:
            artifact["lesson_md"] = ""
        repaired = self.root / "repair.json"
        repaired.write_bytes(encode(value))
        self.args.execute = False
        self.args.repair_candidate = repaired
        self.execute()
        self.assertEqual(self.calls, ["fr"])
        self.assertEqual(len(list(family.family_locale_path(self.root, self.run, "fr").rglob("quiz.md"))), 5)

    def test_workers_publish_ten_whole_families_and_resume_without_calls(self):
        self.execute()
        self.assertEqual(len(self.calls), 10)
        for locale in core.translation_locales("de"):
            self.assertEqual(len([name for name in inventory(family.family_locale_path(self.root, self.run, locale)) if name.endswith("quiz.md")]), 5)
        self.args.resume = True
        self.execute()
        self.assertEqual(len(self.calls), 10)

    def test_one_bad_stage_isolates_locale_and_publishes_none(self):
        with self.assertRaisesRegex(TranslationError, "1 family locale"):
            self.execute("fr")
        self.assertEqual(len(self.calls), 10)
        folder = family.family_locale_path(self.root, self.run, "fr")
        self.assertFalse(any(folder.rglob("quiz.md")))
        journal = core.read_json(self.root / "work/translations/families/de/000020/fr/journal.json")
        self.assertEqual(journal["state"], "rejected")
        self.assertEqual(len(list(family.family_locale_path(self.root, self.run, "es").rglob("quiz.md"))), 5)


class FamilyRenewalCallerTests(unittest.TestCase):
    def setUp(self):
        from tests.test_translation_recovery import TranslationRecoveryTests
        import argparse
        import subprocess
        self.fixture = TranslationRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run = self.fixture.root, self.fixture.run
        preserved_log = self.fixture.folder / "es"
        self.assertEqual(set(inventory(preserved_log)), {"worker.lock"})
        (preserved_log / "worker.lock").unlink()
        preserved_log.rmdir()
        (self.root / "secrets").mkdir()
        (self.root / "secrets/contactbox_key").write_text("offline-fixture")
        from lesson_factory.source_validation import VALIDATOR_FILES
        for name in VALIDATOR_FILES:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('synthetic validator code')
        self.old = self.fixture.prepare_retry()
        self.grant = self.fixture.authorize()
        self.original = inventory(self.old)
        self.original_journal = (self.old / "journal.json").read_bytes()
        self.selected = self.root / "work/translations" / self.grant["scope"] / "fr"
        self.args = argparse.Namespace(root=self.root, run=self.run, all=False, lang="fr", execute=True, resume=False, repair_candidate=None, workers=1, timeout=30, deadline=600, max_input_bytes=524288)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(family_translation, "build_prompt", return_value="focused synthetic prompt"))
        self.events = self.stack.enter_context(patch.object(family_translation.scheduler, "emit"))
        report = {"ok": True, "family_contract": [{"stage": stage} for stage in family.STAGES]}
        self.stack.enter_context(patch.object(family_translation.scheduler, "validate_process", return_value=subprocess.CompletedProcess([], 0, json.dumps(report), "")))

    def assert_original_unchanged(self):
        self.assertEqual((self.old / "journal.json").read_bytes(), self.original_journal)
        self.assertEqual(inventory(self.old), self.original)

    def emitted_errors(self):
        return [call.args[0].get("error", "") for call in self.events.call_args_list]

    def test_preserve_without_original_directory_creates_no_locks_or_request(self):
        self.args.lang = "es"
        original = self.fixture.folder / "es"
        selected = self.root / "work/translations" / self.grant["scope"] / "es"
        destination = family.family_locale_path(self.root, self.run, "es")
        before = inventory(destination)
        self.assertFalse(original.exists())
        with patch.object(family_translation.scheduler, "worker_lock", side_effect=AssertionError("Preservation must not create a lock")) as locks, patch.object(family_translation, "execute", side_effect=AssertionError("Preservation must not dispatch")) as transport, patch.object(family_translation, "validate_preserved_locale", wraps=family_translation.validate_preserved_locale) as validate:
            family_translation.run(self.args)
        locks.assert_not_called()
        transport.assert_not_called()
        validate.assert_called_once_with(self.root, self.run, "es", timeout=30)
        self.assertFalse(original.exists())
        self.assertFalse(selected.exists())
        self.assertEqual(inventory(destination), before)
        self.assertTrue(any(call.args[0].get("state") == "preserved" for call in self.events.call_args_list))
        self.assert_original_unchanged()

    def test_renewal_holds_both_workers_and_readonly_original_attempt_lock(self):
        import fcntl
        original_open = Path.open
        attempt_opens = []
        def tracked_open(path, *args, **kwargs):
            if path == self.old / "attempt.lock":
                attempt_opens.append(args[0] if args else kwargs.get("mode", "r"))
            return original_open(path, *args, **kwargs)
        def opener(request, timeout, **kwargs):
            self.assertEqual((self.old / "journal.json").read_bytes(), self.original_journal)
            for path in (self.old / "worker.lock", self.old / "attempt.lock", self.selected / "worker.lock"):
                with path.open("rb") as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            value = candidate()
            for artifact in value["stages"][1:]:
                artifact["lesson_md"] = ""
            return io.BytesIO(b"data: " + encode({"type": "response.completed", "response": response(value)}) + b"\n\n")
        with patch.object(Path, "open", tracked_open), patch("lesson_factory.translation.open_response", side_effect=opener) as transport:
            try:
                family_translation.run(self.args)
            except TranslationError:
                self.fail(str(self.emitted_errors()))
        self.assertEqual(transport.call_count, 1)
        self.assertGreaterEqual(len(attempt_opens), 2)
        self.assertEqual(set(attempt_opens), {"rb"})
        self.assertEqual(core.read_json(self.selected / "journal.json")["state"], "completed")
        self.assertTrue((self.selected / "attempt-1/request.json").is_file())
        self.assert_original_unchanged()
        for path in (self.old / "worker.lock", self.old / "attempt.lock", self.selected / "worker.lock"):
            with path.open("rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)

    def test_active_original_attempt_refuses_renewal_dispatch(self):
        import fcntl
        with (self.old / "attempt.lock").open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(family_translation, "execute", side_effect=AssertionError("Conflicting original attempt must block")) as transport:
                with self.assertRaisesRegex(TranslationError, "1 family locale workers failed"):
                    family_translation.run(self.args)
            fcntl.flock(handle, fcntl.LOCK_UN)
        transport.assert_not_called()
        self.assertTrue(any("Original translation attempt is active" in error for error in self.emitted_errors()))
        self.assertFalse(self.selected.exists())
        self.assert_original_unchanged()

    def test_current_focused_summary_must_match_grant_before_scope_creation(self):
        original_prepare = family_translation.prepare
        def changed_prepare(*args, **kwargs):
            body, summary = original_prepare(*args, **kwargs)
            return body, {**summary, "hash": "0" * 64}
        with patch.object(family_translation, "prepare", side_effect=changed_prepare), patch.object(family_translation, "execute", side_effect=AssertionError("Mismatched request must block")) as transport:
            with self.assertRaisesRegex(TranslationError, "1 family locale workers failed"):
                family_translation.run(self.args)
        transport.assert_not_called()
        self.assertTrue(any("Focused request differs from the authorized recovery grant" in error for error in self.emitted_errors()))
        self.assertFalse(self.selected.exists())
        self.assert_original_unchanged()

    def test_grant_summary_is_rechecked_immediately_before_attempt(self):
        original_recovery = family_translation.recovery_for_locale
        checks = []
        def changed_grant(*args, **kwargs):
            authorization = original_recovery(*args, **kwargs)
            checks.append(authorization)
            if len(checks) == 2:
                return {**authorization, "request_summary": {**authorization["request_summary"], "hash": "0" * 64}}
            return authorization
        with patch.object(family_translation, "recovery_for_locale", side_effect=changed_grant), patch.object(family_translation, "execute", side_effect=AssertionError("Changed grant must block")) as transport:
            with self.assertRaisesRegex(TranslationError, "1 family locale workers failed"):
                family_translation.run(self.args)
        transport.assert_not_called()
        self.assertEqual(len(checks), 2)
        self.assertTrue(any("Renewal authorization changed before attempt" in error for error in self.emitted_errors()))
        self.assertFalse((self.selected / "attempt-1").exists())
        self.assert_original_unchanged()

    def test_completed_candidate_before_eof_is_rechecked_before_retry_purchase(self):
        import time
        retry_worker = family_translation.scheduler.retry_worker
        now = [time.time()]
        waits = []
        def advance(seconds):
            waits.append(seconds)
            now[0] += seconds
        def retry_without_wait(attempt, journal, **kwargs):
            return retry_worker(attempt, journal, clock=lambda: now[0], sleep=advance, **kwargs)
        value = candidate()
        for artifact in value["stages"][1:]:
            artifact["lesson_md"] = ""
        wire = b"data: " + encode({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": "synthetic-complete", "delta": json.dumps(value)}) + b"\n\n"
        with patch.object(family_translation, "recovery_for_locale", wraps=family_translation.recovery_for_locale) as recovery, patch.object(family_translation.scheduler, "retry_worker", side_effect=retry_without_wait), patch("lesson_factory.translation.open_response", side_effect=lambda *args, **kwargs: io.BytesIO(wire)) as transport:
            with self.assertRaisesRegex(TranslationError, "1 family locale workers failed"):
                family_translation.run(self.args)
        self.assertEqual(recovery.call_count, 3, self.emitted_errors())
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 0)
        self.assertTrue(any("Known completed renewal candidate" in error and "no new purchase" in error for error in self.emitted_errors()))
        self.assertEqual(core.read_json(self.selected / "journal.json")["attempt"], 1)
        self.assertEqual((self.selected / "attempt-1/raw.sse").read_bytes(), wire)
        self.assertFalse((self.selected / "attempt-2").exists())
        self.assertFalse((self.selected / "generation.json").exists())
        self.assert_original_unchanged()


if __name__ == "__main__":
    unittest.main()
