"""Bounded automatic same-session resume after verified transient provider interruptions.

Run: python3 -m unittest tests.test_dashboard_autorecovery -v
"""
import fcntl
import json
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

from lesson_factory import dashboard_supervisor as supervisor
from lesson_factory.dashboard_runner import Runner
from lesson_factory.dashboard_store import AUTO_RESUME_DELAYS, AUTO_RESUME_LIMIT, Store

SESSION = "ses_transient_fixture"
UPSTREAM = {"type": "error", "timestamp": 1, "sessionID": SESSION, "error": {"name": "UnknownError", "data": {"message": json.dumps({"message": "Upstream response stream was interrupted", "type": "upstream_error", "code": "upstream_error"})}}}
DEAD = {"pid": 999999999, "create_time": 1, "boot_id": supervisor.boot_identity(), "command_fingerprint": "dead"}
PRESSURE_BODY = json.dumps({"error": {"message": supervisor.RESOURCE_PRESSURE, "type": "server_error", "code": "resource_pressure"}})


def api_error(**data):
    return {"type": "error", "timestamp": 1, "sessionID": SESSION, "error": {"name": "APIError", "data": data}}


def unknown(message):
    return {"type": "error", "timestamp": 1, "sessionID": SESSION, "error": {"name": "UnknownError", "data": {"message": json.dumps(message)}}}


class SignatureTests(unittest.TestCase):
    def test_observed_transient_shapes_are_allowlisted(self):
        cases = {
            "upstream_stream_interrupted": UPSTREAM,
            "upstream_http2_stream_failed": unknown({"message": "Upstream HTTP/2 stream failed", "type": "upstream_error", "code": "upstream_error"}),
            "sse_read_timeout": api_error(message="SSE read timed out", isRetryable=True, metadata={"code": "ProviderResponseStreamError"}),
            "provider_unreachable": api_error(message="Cannot connect to API: Unable to connect. Is the computer able to access the url?", isRetryable=True, metadata={"url": "http://provider/v1"}),
            "resource_pressure_503": api_error(message=supervisor.RESOURCE_PRESSURE, statusCode=503, isRetryable=True, metadata={"url": "x"}, responseHeaders={}, responseBody=PRESSURE_BODY),
            "resource_pressure": unknown({"message": supervisor.RESOURCE_PRESSURE + " (reset after 0s)"}),
        }
        composite = "; ".join(f"codex/gpt-6.1-sol: provider \u2014 {supervisor.RESOURCE_PRESSURE} (HTTP 503)" for _ in range(3))
        cases["resource_pressure"] = unknown({"message": composite + " (reset after 0s)"})
        for expected, event in cases.items():
            self.assertEqual(supervisor.transient_signature(event), expected)

    def test_permanent_unknown_and_lookalike_errors_need_attention(self):
        rejected = [
            unknown({"message": "Service temporarily unavailable: all targets were skipped by pre-dispatch filters (session 100% conn:63d908c6)", "type": "service_unavailable", "code": "ALL_TARGETS_SKIPPED"}),
            unknown({"message": "Upstream response stream was interrupted", "type": "authentication_error", "code": "upstream_error"}),
            unknown({"message": "Invalid API key", "type": "upstream_error", "code": "upstream_error"}),
            unknown({"message": "Upstream response stream was interrupted", "type": "upstream_error", "code": "upstream_error", "detail": "quota exhausted"}),
            unknown({"message": "Insufficient credits (reset after 0s)"}),
            api_error(message="SSE read timed out", isRetryable=False, metadata={"code": "ProviderResponseStreamError"}),
            api_error(message="Unauthorized", statusCode=401, isRetryable=True, metadata={"url": "x"}, responseHeaders={}, responseBody="{}"),
            api_error(message=supervisor.RESOURCE_PRESSURE, statusCode=429, isRetryable=True, metadata={"url": "x"}, responseHeaders={}, responseBody=PRESSURE_BODY),
            api_error(message=supervisor.RESOURCE_PRESSURE, statusCode=503, isRetryable=True, metadata={"url": "x"}, responseHeaders={}, responseBody=json.dumps({"error": {"message": "credit exhausted", "type": "server_error", "code": "resource_pressure"}})),
            {**UPSTREAM, "error": {**UPSTREAM["error"], "name": "ContentFilterError"}},
            {**UPSTREAM, "extra": "refusal"},
            {"type": "text", "sessionID": SESSION},
            "not an event",
        ]
        for event in rejected:
            self.assertIsNone(supervisor.transient_signature(event), event)


class AutoRecoveryTests(unittest.TestCase):
    def setUp(self):
        from test_dashboard_store import DashboardStoreTests
        self.fixture = DashboardStoreTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.store = self.fixture.store
        self.runner = Runner(self.root, self.store)
        self.addCleanup(lambda: self.runner.close())
        self.batch_id = self.fixture.batch(n=2, parallel=1)
        self.store.resume(self.batch_id)
        self.job = self.store.claim_next("synthetic-owner")
        self.store.set_session(self.job["id"], self.job["attempt_id"], SESSION)
        self.executable = self.root / "fake-opencode"
        self.executable.write_text("#!" + sys.executable + "\n")
        self.executable.chmod(0o700)
        self.addCleanup(patch.stopall)
        patch('lesson_factory.dashboard_runner.worker.verify_binary', return_value='2.0.29').start()
        patch('lesson_factory.dashboard_runner.worker.require_session').start()
        self.environment = None
        self.clock = time.time()

    def now(self):
        return self.clock

    def journal(self, events=(UPSTREAM,), exit_code=1, stopped=False, session_id=SESSION, extra=None):
        """Write a complete stopped managed attempt for the current claim."""
        job = self.store.get_job(self.job["id"])
        job_dir, directory = self.runner._paths(job)
        supervisor.private_directory(directory)
        supervisor.private_directory(job_dir / "home")
        if self.environment is None:
            self.environment = supervisor.saved_environment(supervisor.isolated_environment(job_dir / "home"), job_dir / "home")
        spec = supervisor.read_json(directory / "spec.json")  # written by the real _launch on automatic resume
        if spec is None:
            spec = {"root": str(self.root), "job_id": job["id"], "attempt_id": job["attempt_id"], "session_id": None if len(job["attempts"]) == 1 else SESSION, "command": [str(self.executable), 'run', '--standalone', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas#xhigh', 'fixture'], "environment": self.environment}
            supervisor.write_json(directory / "spec.json", spec)
        self.store.attach_launch(job["id"], job["attempt_id"], {"attempt_dir": str(directory.relative_to(self.root)), "spec_sha256": supervisor.fingerprint(spec), "supervisor": DEAD, "child": DEAD, "phase": "running"})
        evidence = {"job_id": job["id"], "attempt_id": job["attempt_id"], "spec_sha256": supervisor.fingerprint(spec), "supervisor": DEAD, "child": DEAD, "phase": "stopped", "session_id": session_id, "session_conflict": False}
        supervisor.write_json(directory / "process.json", evidence)
        with (directory / "events.jsonl").open("w") as handle:
            if session_id is not None:
                handle.write(json.dumps({"type": "step_start", "sessionID": SESSION}) + "\n")
            for event in events:
                handle.write(json.dumps(event) + "\n")
        supervisor.write_json(directory / "exit.json", {**evidence, "group_stopped": True, "stopped": stopped, "exit_code": exit_code, "ended_at": time.time(), **(extra or {})})
        return directory

    def tick(self, offset=0):
        self.clock += offset
        with patch("time.time", side_effect=self.now):
            return self.runner.tick()

    def launch_due(self):
        """Advance past the due time and let the existing claim/_launch path start the same session (spawn mocked)."""
        delay = AUTO_RESUME_DELAYS[self.store.auto_resume_status(self.job["id"])["count"] - 1]
        spawned = Mock(pid=os.getpid())
        spawned.poll.return_value = None
        with patch.dict(os.environ, {"OPENCODE_BINARY": str(self.executable)}), patch("lesson_factory.dashboard_runner.subprocess.Popen", return_value=spawned) as popen, patch("lesson_factory.dashboard_runner.process_identity", return_value=DEAD):
            self.tick(delay + 1)
        return popen

    def state(self):
        return self.store.get_job(self.job["id"])

    def test_verified_transient_stop_schedules_then_resumes_exact_session_and_environment(self):
        self.journal()
        self.tick()
        job = self.state()
        self.assertEqual(job["state"], "retry_wait")
        self.assertIn(f"1/{AUTO_RESUME_LIMIT}", job["reason"])
        self.assertIn("due", job["reason"])
        self.assertIn("upstream_stream_interrupted", job["reason"])
        self.tick(AUTO_RESUME_DELAYS[0] - 5)
        self.assertEqual(self.state()["state"], "retry_wait")
        popen = self.launch_due()
        job = self.state()
        self.assertEqual(job["state"], "launching")
        self.assertEqual(len(job["attempts"]), 2)
        self.assertTrue(job["resume_authorized"])
        popen.assert_called_once()
        _, directory = self.runner._paths(job)
        spec = supervisor.read_json(directory / "spec.json")
        self.assertEqual(spec["command"][spec["command"].index("--session") + 1], SESSION)
        self.assertEqual(spec["environment"], self.environment)
        kinds = [event["kind"] for event in self.store.events(self.job["id"])]
        self.assertEqual(kinds.count("auto_resume_scheduled"), 1)
        self.assertEqual(kinds.count("auto_resume_authorized"), 1)

    def test_ledger_survives_restart_and_paused_batches_never_auto_resume(self):
        self.journal()
        self.tick()
        self.runner.close()
        store = Store(self.root)
        self.runner = Runner(self.root, store)
        self.store = store
        with patch("time.time", side_effect=self.now):
            self.runner.recover()
        status = store.auto_resume_status(self.job["id"])
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["pending"]["attempt_id"], self.job["attempt_id"])
        self.tick(AUTO_RESUME_DELAYS[-1] * 10)
        self.assertEqual(self.state()["state"], "retry_wait")
        store.resume(self.batch_id)
        self.launch_due()
        self.assertEqual(self.state()["state"], "launching")

    def test_total_limit_is_finite_and_never_reset(self):
        for cycle in range(AUTO_RESUME_LIMIT):
            self.journal()
            self.tick()
            self.assertEqual(self.state()["state"], "retry_wait")
            self.launch_due()
            self.job = self.state()
            self.assertEqual(self.job["state"], "launching")
        self.journal()
        self.tick()
        job = self.state()
        self.assertEqual(job["state"], "interrupted")
        self.assertIn(f"limit {AUTO_RESUME_LIMIT}/{AUTO_RESUME_LIMIT}", job["reason"])
        self.tick(10 ** 6)
        self.assertEqual(self.state()["state"], "interrupted")
        status = self.store.auto_resume_status(self.job["id"])
        self.assertEqual(status["count"], AUTO_RESUME_LIMIT)
        self.assertIsNone(status["pending"])
        self.assertIn(self.job["attempt_id"], status["exhausted_attempts"])

    def test_adversarial_stops_stay_attention_needed(self):
        cases = {
            "user stop": dict(stopped=True),
            "permanent quota": dict(events=[unknown({"message": "Service temporarily unavailable: all targets were skipped by pre-dispatch filters (session 100% conn:x)", "type": "service_unavailable", "code": "ALL_TARGETS_SKIPPED"})]),
            "error not final": dict(events=[UPSTREAM, {"type": "text", "sessionID": SESSION}]),
            "clean exit": dict(exit_code=0),
            "launch failure": dict(extra={"error": "coordinator_launch_failed"}),
        }
        for name, arguments in cases.items():
            with self.subTest(name):
                self.journal(**arguments)
                self.tick()
                job = self.state()
                self.assertEqual(job["state"], "interrupted", name)
                self.tick(10 ** 5)
                self.assertEqual(self.state()["state"], "interrupted", name)
                self.assertEqual(self.store.auto_resume_status(self.job["id"])["count"], 0)
                self.store.resume_job(self.job["id"])
                self.job = self.store.claim_next("synthetic-owner")
                self.assertIsNotNone(self.job)

    def test_conflicting_session_error_keeps_diagnostic_hold(self):
        self.journal(events=[{**UPSTREAM, "sessionID": "ses_other"}])
        self.tick()
        job = self.state()
        self.assertIn(job["state"], ("launching", "running"))
        self.assertIn("Conflicting coordinator stream identity", job["launch_metadata"]["diagnostic"])
        self.assertTrue(self.store.get_batch(self.batch_id)["paused"])
        self.assertEqual(self.store.auto_resume_status(self.job["id"])["count"], 0)

    def test_blocked_receipt_and_cancel_and_stop_json_never_auto_resume(self):
        directory = self.journal()
        self.store.record_completion(self.job["id"], self.job["attempt_id"], "blocked", "Synthetic content blocker")
        self.tick()
        self.assertEqual(self.state()["state"], "blocked")
        self.tick(10 ** 5)
        self.assertEqual(self.state()["state"], "blocked")
        self.store.resume_job(self.job["id"])
        self.job = self.store.claim_next("synthetic-owner")
        directory = self.journal()
        supervisor.write_json(directory / "stop.json", {"job_id": self.job["id"], "attempt_id": self.job["attempt_id"], "action": "stop"})
        self.tick()
        self.assertEqual(self.state()["state"], "interrupted")
        self.store.resume_job(self.job["id"])
        self.job = self.store.claim_next("synthetic-owner")
        self.journal()
        self.tick()
        self.assertEqual(self.state()["state"], "retry_wait")
        self.runner.stop_job(self.job["id"])
        self.assertEqual(self.state()["state"], "cancelled")
        self.tick(10 ** 5)
        self.assertEqual(self.state()["state"], "cancelled")

    def test_missing_session_is_blocked_without_auto_resume(self):
        from test_dashboard_store import DashboardStoreTests
        fixture = DashboardStoreTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = Runner(fixture.root, fixture.store)
        self.addCleanup(runner.close)
        self.root, self.store, self.runner = fixture.root, fixture.store, runner
        self.batch_id = fixture.batch(n=1, parallel=1)
        self.store.resume(self.batch_id)
        self.job = self.store.claim_next("synthetic-owner")
        self.journal(session_id=None, events=[{key: value for key, value in UPSTREAM.items() if key != "sessionID"}])
        self.tick()
        self.assertEqual(self.state()["state"], "blocked")
        self.assertEqual(self.store.auto_resume_status(self.job["id"])["count"], 0)

    def test_held_job_lock_never_schedules_or_adopts(self):
        self.journal()
        job_dir, _ = self.runner._paths(self.job)
        fd = os.open(job_dir / "job.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.tick()
            self.assertIn(self.state()["state"], ("launching", "running"))
            self.assertEqual(self.store.auto_resume_status(self.job["id"])["count"], 0)
        finally:
            os.close(fd)

    def test_existing_interrupted_stop_is_adopted_only_when_unpaused(self):
        self.journal()
        self.store.pause(self.batch_id)
        self.store.finish_attempt(self.job["id"], self.job["attempt_id"], "interrupted", "Coordinator stopped without a completion receipt")
        self.tick(10 ** 4)
        self.assertEqual(self.state()["state"], "interrupted")
        self.store.resume(self.batch_id)
        self.tick()
        job = self.state()
        self.assertEqual(job["state"], "retry_wait")
        self.assertTrue(any(event["kind"] == "auto_resume_scheduled" and event["details"]["adopted"] for event in self.store.events(self.job["id"])))

    def test_evidence_tampering_before_due_returns_to_review(self):
        directory = self.journal()
        self.tick()
        with (directory / "events.jsonl").open("a") as handle:
            handle.write(json.dumps({"type": "text", "sessionID": SESSION}) + "\n")
        self.tick(AUTO_RESUME_DELAYS[0] + 1)
        job = self.state()
        self.assertEqual(job["state"], "interrupted")
        self.assertIn("re-verified", job["reason"])
        self.tick(10 ** 5)
        self.assertEqual(self.state()["state"], "interrupted")

    def test_prompt_references_optional_recovery_instructions_without_budget_expansion(self):
        prompt = self.runner._prompt(self.job, self.store.get_batch(self.batch_id))
        self.assertIn(f"jobs/{self.job['id']}/recovery-instructions.md", prompt)
        self.assertIn("never expands paid budgets", prompt)
        self.assertLess(len(prompt), 5000)


if __name__ == "__main__":
    unittest.main()
