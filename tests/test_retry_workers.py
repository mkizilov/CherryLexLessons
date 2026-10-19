import contextlib
import io
import tempfile
import threading
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lesson_factory import core, translation
from lesson_factory import translation_scheduler as scheduler


class RetryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = self.root / "journal.json"
        self.now = 1000
        self.waits = []

    def tearDown(self):
        self.temp.cleanup()

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def test_persisted_due_time_and_four_total_attempts(self):
        calls = []
        def attempt(**kwargs):
            calls.append(kwargs)
            number = len(calls)
            core.write_json(self.journal, {"target": "fr", "attempt": number, "retry_eligible": True, "retry_at": self.now + (120, 300, 1800, 0)[number - 1]})
            raise translation.TranslationError("transient")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(translation.TranslationError, "transient"):
            scheduler.retry_worker(attempt, self.journal, clock=self.clock, sleep=self.sleep)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.waits, [120, 300, 1800])
        with self.assertRaisesRegex(translation.TranslationError, "exhausted"):
            scheduler.retry_worker(attempt, self.journal, resume=True, clock=self.clock, sleep=self.sleep)
        self.assertEqual(len(calls), 4)

    def test_family_transport_scheduler_retries_with_one_request_per_attempt(self):
        import urllib.error
        (self.root / "secrets").mkdir()
        (self.root / "secrets/contactbox_key").write_text("offline-fixture")
        body, summary = translation.prepare({"study_language": "de", "source_locale": "en"}, "fr", "complete prompt")
        journal = self.root / "work/translations/families/de/000020/fr/journal.json"
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson", "quiz_md": "quiz", "adaptation_notes": []} for stage in range(5)]}
        final = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": translation.encode(candidate).decode()}]}]}
        calls = []
        def opener(request, timeout):
            calls.append(request)
            if len(calls) < 4:
                raise urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, io.BytesIO(b"unavailable"))
            return io.BytesIO(b"data: " + translation.encode({"type": "response.completed", "response": final}) + b"\n\n")
        def attempt(*, resume, retry, remaining, stop_event):
            return translation.execute(self.root, body, summary, lambda value: value == candidate, scope="families/de/000020", gate=lambda: True, resume=resume, retry=retry, wall_timeout=remaining, stop_event=stop_event, opener=opener, clock=self.clock, jitter=lambda: 1)
        with contextlib.redirect_stdout(io.StringIO()):
            result = scheduler.retry_worker(attempt, journal, clock=self.clock, sleep=self.sleep)
        self.assertEqual(result["attempt"], 4)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.waits, [121, 301, 1801])
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({request.get_header("Idempotency-key") for request in calls}), 4)
        for request in calls:
            self.assertEqual(request.data, translation.encode(body))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.retry_worker(attempt, journal, resume=True, clock=self.clock, sleep=self.sleep), result)
        self.assertEqual(len(calls), 4)

    def test_restart_waits_only_remaining_due_time(self):
        core.write_json(self.journal, {"attempt": 2, "retry_eligible": True, "retry_at": 1025})
        calls = []
        with contextlib.redirect_stdout(io.StringIO()):
            result = scheduler.retry_worker(lambda **kwargs: calls.append(kwargs) or "done", self.journal, resume=True, clock=self.clock, sleep=self.sleep)
        self.assertEqual(result, "done")
        self.assertEqual(self.waits, [25])
        self.assertTrue(calls[0]["retry"])
        self.assertTrue(calls[0]["resume"])

    def test_deadline_retains_future_due_time_without_dispatch(self):
        record = {"attempt": 1, "retry_eligible": True, "retry_at": 2000}
        core.write_json(self.journal, record)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(translation.TranslationError, "deadline"):
            scheduler.retry_worker(lambda **_: self.fail("dispatch"), self.journal, resume=True, clock=self.clock, sleep=self.sleep, deadline=15)
        self.assertEqual(core.read_json(self.journal), record)
        self.assertEqual(self.waits, [15])

    def test_cancel_during_backoff_never_dispatches(self):
        stop = threading.Event()
        core.write_json(self.journal, {"attempt": 1, "retry_eligible": True, "retry_at": 2000})
        def cancel(seconds):
            stop.set()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(translation.TranslationError, "cancelled"):
            scheduler.retry_worker(lambda **_: self.fail("dispatch"), self.journal, resume=True, clock=self.clock, sleep=cancel, stop_event=stop)

    def test_failed_gate_never_retries_existing_eligible_record(self):
        core.write_json(self.journal, {"attempt": 1, "retry_eligible": True, "retry_at": 900})
        calls = []
        def gate_failure(**kwargs):
            calls.append(kwargs)
            raise translation.TranslationError("stale gate")
        with self.assertRaisesRegex(translation.TranslationError, "stale gate"):
            scheduler.retry_worker(gate_failure, self.journal, resume=True, clock=self.clock)
        self.assertEqual(len(calls), 1)

    def test_workers_overlap_and_one_failure_does_not_cancel_sibling(self):
        barrier = threading.Barrier(2)
        completed = []
        def work(locale):
            journal = self.root / locale / "journal.json"
            def attempt(**kwargs):
                barrier.wait(timeout=2)
                if locale == "fr":
                    raise translation.TranslationError("permanent")
                completed.append(locale)
                return "completed"
            with scheduler.worker_lock(journal.parent):
                return scheduler.retry_worker(attempt, journal)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(work, "fr")
            second = pool.submit(work, "ja")
            with self.assertRaisesRegex(translation.TranslationError, "permanent"):
                first.result(timeout=3)
            self.assertEqual(second.result(timeout=3), "completed")
        self.assertEqual(completed, ["ja"])
        with scheduler.worker_lock(self.root / "fr"):
            pass

    def test_duplicate_locale_lifetime_lock_refused(self):
        with scheduler.worker_lock(self.root / "fr"):
            with self.assertRaisesRegex(translation.TranslationError, "already running"):
                with scheduler.worker_lock(self.root / "fr"):
                    self.fail("duplicate")
        with scheduler.worker_lock(self.root / "fr"):
            pass

    def test_validator_deadline_terminates_process(self):
        with self.assertRaisesRegex(translation.TranslationError, "deadline"):
            scheduler.validate_process([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.05)

    def test_validator_cancellation_terminates_process(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(translation.TranslationError, "cancelled"):
            scheduler.validate_process([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60, stop_event=stop)

    def test_worker_lock_rejects_symlink(self):
        external = self.root / "external.lock"
        external.write_bytes(b"retained")
        folder = self.root / "fr"
        folder.mkdir()
        (folder / "worker.lock").symlink_to(external)
        with self.assertRaises(core.AuthoringError):
            with scheduler.worker_lock(folder):
                self.fail("symlink accepted")
        self.assertEqual(external.read_bytes(), b"retained")

    def test_workers_argument_bounds(self):
        import runpy
        parser = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/translate-family.py"))["parser"]
        for value in ("0", "11", "-1"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["--run", "family_runs/de/000020", "--all", "--workers", value])
        self.assertEqual(parser().parse_args(["--run", "family_runs/de/000020", "--all"]).workers, 10)


if __name__ == "__main__":
    unittest.main()
