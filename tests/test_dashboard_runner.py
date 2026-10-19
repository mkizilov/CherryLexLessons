import copy
import csv
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from lesson_factory.dashboard_runner import Runner
from lesson_factory import dashboard_supervisor as supervisor


class MemoryStore:
    def __init__(self):
        self.batch = {"id": "batch", "paused": True, "authorization": {"translations": 10, "attempts": 4}}
        self.job = {"id": "job", "batch_id": "batch", "language": "de", "base_checkpoint": 20, "run": "family_runs/de/000020", "state": "queued", "attempt_id": None, "session_id": None, "resume_authorized": False, "attempts": [], "reason": None}
        self.finished = []

    def snapshot(self):
        return copy.deepcopy({"jobs": [self.job], "batches": [self.batch]})

    def active_jobs(self):
        return [self.get_job("job")] if self.job["state"] in ("launching", "running") else []

    def get_batch(self, batch_id):
        assert batch_id == "batch"
        return copy.deepcopy(self.batch)

    def get_job(self, job_id):
        assert job_id == self.job["id"]
        return copy.deepcopy(self.job)

    def claim_next(self, owner):
        if self.batch["paused"] or self.job["state"] != "queued":
            return None
        attempt_id = f"attempt{len(self.job['attempts']) + 1}"
        self.job.update(state="launching", attempt_id=attempt_id)
        self.job["attempts"].append({"id": attempt_id, "owner": owner, "launch_metadata": {}, "completion_status": None, "ended_at": None})
        return self.get_job("job")

    def attach_launch(self, job_id, attempt_id, metadata):
        assert attempt_id == self.job["attempt_id"]
        self.job["attempts"][-1]["launch_metadata"].update(metadata)
        if metadata.get("child"):
            self.job["state"] = "running"
        return self.get_job(job_id)

    def set_session(self, job_id, attempt_id, session_id):
        assert attempt_id == self.job["attempt_id"]
        assert self.job["session_id"] in (None, session_id)
        self.job["session_id"] = session_id
        self.job['attempts'][-1]['session_id'] = session_id

    def finish_attempt(self, job_id, attempt_id, state, reason=None, exit_code=None):
        self.finished.append((attempt_id, state, exit_code))
        self.job.update(state=state, reason=reason)
        self.job["attempts"][-1]["ended_at"] = time.time()
        return self.get_job(job_id)

    def pause_all(self):
        self.batch["paused"] = True

    def pause(self, batch_id):
        assert batch_id == "batch"
        self.batch["paused"] = True

    def resume_job(self, job_id):
        self.job.update(state="queued", resume_authorized=True)
        return self.get_job(job_id)

    def cancel_job(self, job_id):
        self.job["state"] = "cancelled"
        return self.get_job(job_id)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = MemoryStore()
        self.addCleanup(patch.stopall)
        patch('lesson_factory.dashboard_runner.worker.verify_binary', return_value='2.0.29').start()
        patch('lesson_factory.dashboard_runner.worker.require_session').start()
        self.runner = Runner(self.root, self.store)

    def tearDown(self):
        self.runner.close()
        self.temp.cleanup()

    def journal(self, session_id="ses_saved", phase="running", receipt=False):
        self.store.batch["paused"] = False
        job = self.store.claim_next("test")
        self.store.batch["paused"] = True
        job_dir, directory = self.runner._paths(job)
        supervisor.private_directory(directory)
        supervisor.private_directory(job_dir / "home")
        spec = {"root": str(self.root), "job_id": "job", "attempt_id": job["attempt_id"], "session_id": None, "command": [sys.executable, 'run', '--standalone', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas#xhigh', 'fixture'], "environment": supervisor.saved_environment(supervisor.isolated_environment(job_dir / "home"), job_dir / "home")}
        supervisor.write_json(directory / "spec.json", spec)
        dead = {"pid": 999999999, "create_time": 1, "boot_id": supervisor.boot_identity(), "command_fingerprint": "dead"}
        evidence = {"job_id": "job", "attempt_id": job["attempt_id"], "spec_sha256": supervisor.fingerprint(spec), "supervisor": dead, "child": dead, "phase": phase, "session_id": session_id}
        self.store.attach_launch("job", job["attempt_id"], {"attempt_dir": str(directory.relative_to(self.root)), "spec_sha256": supervisor.fingerprint(spec)})
        supervisor.write_json(directory / "process.json", evidence)
        if receipt:
            supervisor.write_json(directory / "exit.json", {**evidence, "group_stopped": True, "stopped": False, "exit_code": 0})
        return directory, evidence

    def test_resume_prompt_excludes_browser_work_and_preserves_budgets(self):
        text = self.runner._prompt(self.store.job, self.store.batch)
        self.assertIn("browser-only blocker is resolved by policy", text)
        self.assertIn("Exhausted API lineages are not newly authorized by resume itself", text)
        self.assertIn("recover complete candidates offline", text.lower())
        self.assertIn("Required content, validation and genuine review gates still apply", text)

    def test_prompt_uses_catalogue_and_collects_actual_worker_results(self):
        text = self.runner._prompt(self.store.job, self.store.batch)
        self.assertIn("replaces older exhaustive-history reading instructions", text)
        self.assertIn("--catalog", text)
        self.assertIn("relevant historical originals and dependencies", text)
        self.assertIn("all current-family pages/items", text)
        self.assertIn("do not exit with an awaiting-editors message", text)
        self.assertIn("distinct attempt/session workspaces", text)
        self.assertIn("at most four attempts", text)

    def test_failed_admission_never_claims_or_authorizes_resume(self):
        self.runner.preflight = Mock()
        self.runner.preflight.check.side_effect = ValueError('Configuration verification stale')
        with patch.object(self.store, 'claim_next') as claim, patch.object(self.runner, '_auto_resume') as resume, self.assertRaisesRegex(ValueError, 'stale'):
            self.runner.tick()
        claim.assert_not_called()
        resume.assert_not_called()

    def test_scheduler_lock_prevents_second_dashboard(self):
        with self.assertRaisesRegex(RuntimeError, "Another dashboard"):
            Runner(self.root, self.store)
        self.runner.close()
        other = Runner(self.root, self.store)
        other.close()

    def test_recovery_pauses_batches_but_reconnects_live_supervisor(self):
        directory, evidence = self.journal()
        evidence["supervisor"] = supervisor.process_identity(os.getpid())
        supervisor.write_json(directory / "process.json", evidence)
        self.store.batch["paused"] = False
        result = self.runner.recover()
        self.assertTrue(self.store.batch["paused"])
        self.assertEqual(result[0]["state"], "running")
        self.assertEqual(self.store.job["session_id"], "ses_saved")
        self.assertFalse(self.store.finished)

    def test_receipt_does_not_complete_while_supervisor_alive(self):
        directory, evidence = self.journal(receipt=True)
        evidence["supervisor"] = supervisor.process_identity(os.getpid())
        supervisor.write_json(directory / "process.json", evidence)
        self.store.job["attempts"][-1]["completion_status"] = "complete"
        self.runner.recover()
        self.assertFalse(self.store.finished)

    def test_complete_receipt_and_dead_process_required(self):
        self.journal(receipt=True)
        self.store.job["attempts"][-1]["completion_status"] = "complete"
        self.runner.recover()
        self.assertEqual(self.store.job["state"], "succeeded")

    def test_completion_without_session_stays_blocked(self):
        self.journal(session_id=None, receipt=True)
        self.store.job["attempts"][-1]["completion_status"] = "complete"
        self.runner.recover()
        self.assertEqual(self.store.job["state"], "blocked")
        self.assertIn("saved coordinator session", self.store.job["reason"])

    def test_session_captured_from_private_stream_after_supervisor_dies(self):
        directory, _ = self.journal(session_id=None)
        (directory / "events.jsonl").write_text('{"type":"text","sessionID":"ses_recovered"}\n')
        self.runner.recover()
        self.assertEqual(self.store.job["state"], "interrupted")
        self.assertEqual(self.store.job["session_id"], "ses_recovered")

    def test_exit_zero_without_completion_is_interrupted(self):
        self.journal(receipt=True)
        self.runner.recover()
        self.assertEqual(self.store.job["state"], "interrupted")
        self.assertEqual(self.store.job["session_id"], "ses_saved")

    def test_known_dead_child_recovers_exact_session(self):
        self.journal()
        self.runner.recover()
        self.assertEqual(self.store.job["state"], "interrupted")
        resumed = self.runner.resume_job("job")
        self.assertEqual(resumed["session_id"], "ses_saved")
        self.assertTrue(resumed["resume_authorized"])

    def test_reboot_known_dead_process_keeps_session(self):
        directory, evidence = self.journal()
        for key in ("supervisor", "child"):
            evidence[key] = {**evidence[key], "boot_id": "previous-boot"}
        supervisor.write_json(directory / "process.json", evidence)
        self.runner.recover()
        self.assertEqual(self.store.job["state"], "interrupted")
        self.assertEqual(self.store.job["session_id"], "ses_saved")

    def test_missing_session_is_blocked_and_resume_refuses(self):
        self.journal(session_id=None)
        self.runner.recover()
        self.assertEqual(self.store.job["state"], "blocked")
        with self.assertRaisesRegex(ValueError, "Saved session ID unavailable"):
            self.runner.resume_job("job")

    def test_unattempted_blocked_job_can_resume(self):
        self.store.job["state"] = "blocked"
        self.assertEqual(self.runner.resume_job("job")["state"], "queued")

    def test_orphan_group_holds_slot(self):
        directory, evidence = self.journal()
        evidence["child"] = supervisor.process_identity(os.getpid())
        supervisor.write_json(directory / "process.json", evidence)
        result = self.runner.recover()
        self.assertIn("Orphaned", result[0]["reason"])
        self.assertFalse(self.store.finished)
        with self.assertRaisesRegex(ValueError, "active or ambiguous"):
            self.runner.resume_job("job")

    def test_inherited_job_lock_holds_slot_even_after_dead_pid(self):
        directory, _ = self.journal()
        fd = supervisor.private_open(directory.parent.parent / "job.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = self.runner.recover()
            self.assertIn("still owns its lock", result[0]["reason"])
            self.assertFalse(self.store.finished)
        finally:
            os.close(fd)

    def test_supervisor_missing_journal_blocks_without_spawning(self):
        directory, evidence = self.journal()
        (directory / "process.json").unlink()
        self.store.attach_launch("job", "attempt1", {"supervisor": evidence["supervisor"]})
        with patch("lesson_factory.dashboard_runner.subprocess.Popen") as spawn:
            self.runner.recover()
            self.runner.tick()
        spawn.assert_not_called()
        self.assertFalse(self.store.finished)

    def test_cross_group_v2_server_prevents_false_completion(self):
        directory, evidence = self.journal(receipt=True)
        evidence.update(ownership_floor=1000, owned=[])
        supervisor.write_json(directory / 'process.json', evidence)
        supervisor.write_json(directory / 'exit.json', {**evidence, 'group_stopped': True, 'stopped': False, 'exit_code': 0})
        with patch('lesson_factory.dashboard_runner.ownership_quiescent', return_value=(False, [], True)):
            result = self.runner.recover()
        self.assertIn('ownership', result[0]['reason'])
        self.assertFalse(self.store.finished)

    def test_mismatched_attempt_evidence_keeps_slot(self):
        directory, evidence = self.journal(receipt=True)
        evidence["attempt_id"] = "wrong"
        supervisor.write_json(directory / "process.json", evidence)
        result = self.runner.recover()
        self.assertIn("identity mismatch", result[0]["reason"])
        self.assertFalse(self.store.finished)

    def test_launch_claim_and_spec_persist_before_spawn(self):
        self.store.batch["paused"] = False
        process = Mock(pid=os.getpid())
        def spawn(*args, **kwargs):
            job = self.store.get_job("job")
            self.assertIsNotNone(job["attempt_id"])
            _, directory = self.runner._paths(job)
            spec = supervisor.read_json(directory / "spec.json")
            self.assertEqual(job["attempts"][-1]["launch_metadata"]["spec_sha256"], supervisor.fingerprint(spec))
            self.assertEqual(kwargs["cwd"], self.root)
            self.assertTrue(kwargs["start_new_session"])
            self.assertIn('OPENCODE_DB', spec['environment'])
            self.assertTrue((self.root / 'work/dashboard/jobs/job/home/.config/opencode').is_dir())
            return process
        with patch.dict(os.environ, {"OPENCODE_BINARY": sys.executable}), patch("lesson_factory.dashboard_runner.subprocess.Popen", side_effect=spawn):
            self.runner.tick()
        self.assertEqual(len(self.store.job["attempts"]), 1)

    def test_resume_command_uses_only_explicit_saved_session(self):
        self.journal()
        self.runner.recover()
        self.runner.resume_job("job")
        self.store.batch["paused"] = False
        process = Mock(pid=os.getpid())
        with patch.dict(os.environ, {"OPENCODE_BINARY": sys.executable}), patch("lesson_factory.dashboard_runner.subprocess.Popen", return_value=process):
            self.runner.tick()
        _, directory = self.runner._paths(self.store.get_job("job"))
        spec = supervisor.read_json(directory / "spec.json")
        self.assertEqual(spec["command"][spec["command"].index("--session") + 1], "ses_saved")
        self.assertNotIn("--continue", spec["command"])
        self.assertNotIn("--fork", spec["command"])
        self.assertIn('--standalone', spec['command'])
        self.assertNotIn('--variant', spec['command'])
        self.assertIn('opencode-omniroute/Antharas#xhigh', spec['command'])

    def test_v1_session_is_not_silently_migrated_or_replaced(self):
        directory, evidence = self.journal()
        spec = supervisor.read_json(directory / 'spec.json')
        spec['command'] = [sys.executable, 'run', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas', '--variant', 'xhigh', 'fixture']
        supervisor.write_json(directory / 'spec.json', spec)
        self.store.attach_launch('job', 'attempt1', {'spec_sha256': supervisor.fingerprint(spec)})
        supervisor.write_json(directory / 'process.json', {**evidence, 'spec_sha256': supervisor.fingerprint(spec)})
        self.runner.recover()
        with self.assertRaisesRegex(ValueError, 'V1 session'):
            self.runner.resume_job('job')
        self.assertEqual(self.store.get_job('job')['session_id'], 'ses_saved')

    def test_missing_v2_session_blocks_before_coordinator_spawn(self):
        self.journal()
        self.runner.recover()
        self.runner.resume_job('job')
        self.store.batch['paused'] = False
        with patch.dict(os.environ, {'OPENCODE_BINARY': sys.executable}), patch('lesson_factory.dashboard_runner.worker.require_session', side_effect=ValueError('Saved V2 session missing')), patch('lesson_factory.dashboard_runner.subprocess.Popen') as spawn:
            self.runner.tick()
        spawn.assert_not_called()
        self.assertEqual(self.store.get_job('job')['state'], 'blocked')

    def test_resume_authorization_missing_never_spawns(self):
        self.journal()
        self.runner.recover()
        self.store.job["state"] = "queued"
        self.store.batch["paused"] = False
        with patch("lesson_factory.dashboard_runner.subprocess.Popen") as spawn:
            self.runner.tick()
        spawn.assert_not_called()
        self.assertEqual(self.store.job["state"], "blocked")

    def test_stop_writes_control_receipt_without_arbitrary_signal(self):
        self.journal()
        with patch("lesson_factory.dashboard_runner.signal_group", return_value=False) as kill:
            self.runner.stop_job("job")
        self.assertEqual(kill.call_args.args[0]["pid"], 999999999)
        self.assertTrue(self.store.batch["paused"])

    def test_pause_and_close_do_not_signal_processes(self):
        with patch("os.killpg") as kill:
            self.runner.request_pause("batch")
            self.runner.close()
        kill.assert_not_called()

    def test_git_repository_refuses_parallel_plugin_startup(self):
        (self.root / ".git").mkdir()
        self.store.batch["paused"] = False
        with patch("lesson_factory.dashboard_runner.subprocess.Popen") as spawn:
            self.runner.tick()
        spawn.assert_not_called()
        self.assertEqual(self.store.job["state"], "blocked")
        self.assertIn("non-Git", self.store.job["reason"])

    def test_missing_directory_with_launch_evidence_retains_slot(self):
        directory, _ = self.journal()
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
        result = self.runner.recover()
        self.assertIn("ownership remains unresolved", result[0]["reason"])
        self.assertFalse(self.store.finished)

    def test_malformed_stop_does_not_escape_tick(self):
        directory, _ = self.journal()
        (directory / "stop.json").write_text("{broken")
        result = self.runner.tick()
        self.assertIn("manual inspection", result[0]["reason"])
        self.assertFalse(self.store.finished)

    def test_nonobject_process_journal_keeps_slot(self):
        directory, _ = self.journal()
        supervisor.write_json(directory / "process.json", ["invalid"])
        result = self.runner.recover()
        self.assertIn("manual inspection", result[0]["reason"])
        self.assertFalse(self.store.finished)

    def test_prompt_is_bounded_to_single_job_and_preserves_safety(self):
        self.store.batch["authorization"] = {"jobs": ["other-batch-sentinel"] * 100000}
        prompt = self.runner._prompt({**self.store.job, "attempt_id": "attempt1"}, self.store.batch)
        self.assertLess(len(prompt), 5000)
        self.assertIn("exactly ONE", prompt)
        self.assertIn("NEVER launch", prompt)
        self.assertIn("never reset budgets", prompt)
        self.assertIn("--attempt attempt1", prompt)
        self.assertNotIn("other-batch-sentinel", prompt)
        self.assertNotIn("Russian-20", prompt)
        self.assertIn("instead of asking the user", prompt)


class ExternalRecoveryTests(unittest.TestCase):
    def setUp(self):
        from test_dashboard_store import DashboardStoreTests
        self.fixture = DashboardStoreTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.store = self.fixture.store
        self.addCleanup(patch.stopall)
        patch('lesson_factory.dashboard_runner.worker.verify_binary', return_value='2.0.29').start()
        patch('lesson_factory.dashboard_runner.worker.require_session').start()
        self.runner = Runner(self.root, self.store)
        self.addCleanup(self.runner.close)
        self.job = self.fixture.claim(n=2, parallel=1)
        self.store.set_session(self.job["id"], self.job["attempt_id"], "ses_original")
        self.executable = self.root / "fake opencode"
        self.executable.write_text("#!" + sys.executable + "\nimport json,os,sys,time\nfrom pathlib import Path\nif '--version' in sys.argv: print('opencode v2.0.29');sys.exit(0)\nif 'api' in sys.argv: print(json.dumps({'data':{'id':'ses_original','location':{'directory':os.getcwd()},'agent':'lesson-coordinator'}}));sys.exit(0)\nroot=Path(sys.argv[1])\n(root/'invocation.json').write_text(json.dumps({'argv':sys.argv,'environment':{key:os.environ[key] for key in " + repr(supervisor.ENVIRONMENT_PATHS) + "}}))\nwhile not (root/'release').exists(): time.sleep(.02)\n")
        self.executable.chmod(0o700)
        job_dir, directory = self.runner._paths(self.job)
        supervisor.private_directory(directory)
        supervisor.private_directory(job_dir / "home")
        self.environment = supervisor.saved_environment(supervisor.isolated_environment(job_dir / "home", {"HOME": "/original home", "XDG_CONFIG_HOME": "/original config", "OPENCODE_DATA_DIR": "/original provider"}), job_dir / "home")
        spec = {"root": str(self.root), "job_id": self.job["id"], "attempt_id": self.job["attempt_id"], "session_id": "ses_original", "command": [str(self.executable), 'run', '--standalone', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas#xhigh', 'fixture'], "environment": self.environment}
        supervisor.write_json(directory / "spec.json", spec)
        self.store.attach_launch(self.job["id"], self.job["attempt_id"], {"spec_sha256": supervisor.fingerprint(spec)})
        dead = {"pid": 999999999, "create_time": 1, "boot_id": supervisor.boot_identity(), "command_fingerprint": "dead"}
        self.evidence = {"job_id": self.job["id"], "attempt_id": self.job["attempt_id"], "spec_sha256": supervisor.fingerprint(spec), "supervisor": dead, "child": dead, "phase": "stopped", "session_id": "ses_original"}
        supervisor.write_json(directory / "process.json", self.evidence)
        supervisor.write_json(directory / "exit.json", {**self.evidence, "group_stopped": True, "stopped": False, "exit_code": 1})
        self.store.record_completion(self.job["id"], self.job["attempt_id"], "blocked", "Synthetic coordinator failure")
        self.runner.tick()
        self.directory = directory

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail("Timed out waiting for offline recovery fixture")

    def test_reserved_not_started_holds_slot_and_cannot_resolve_resume_or_cancel(self):
        with patch("lesson_factory.dashboard_runner.subprocess.Popen") as spawn:
            details = self.runner.handoff_job(self.job["id"])
            self.runner.tick()
        spawn.assert_not_called()
        self.assertTrue(details["external"])
        self.assertEqual(details["session_id"], "ses_original")
        self.assertFalse(details["can_resolve"])
        self.assertIn("--external-resume", details["resume_command"])
        import shlex
        tokens = shlex.split(details["resume_command"])
        for name, value in self.environment.items():
            self.assertIn(f"{name}={value}", tokens)
        self.assertIn('session export --standalone ses_original', details['inspect_command'])
        self.assertIsNone(self.store.claim_next("cannot-release"))
        for action in (lambda: self.runner.resume_job(self.job["id"]), lambda: self.runner.handoff_job(self.job["id"]), lambda: self.runner.reconcile_job(self.job["id"], "Verified"), lambda: self.store.cancel_job(self.job["id"])):
            with self.assertRaises((ValueError, RuntimeError)):
                action()
        cancelled = self.runner.stop_job(self.job["id"])
        self.assertEqual(cancelled["state"], "interrupted")
        self.assertIsNotNone(cancelled["attempts"][-1]["ended_at"])
        self.assertIsNone(self.store.claim_next("same-language-still-blocked"))
        self.assertTrue(self.runner.recovery_details(self.job["id"])["can_handoff"])
        self.assertFalse(self.runner.recovery_details(self.job["id"])["can_resolve"])
        replacement = self.runner.handoff_job(self.job["id"])
        self.assertNotEqual(replacement["attempt_id"], details["attempt_id"])

    def test_live_and_unknown_ownership_cannot_handoff_or_resolve(self):
        for state in ("alive", "unknown"):
            with self.subTest(state=state), patch("lesson_factory.dashboard_runner.process_state", return_value=state):
                details = self.runner.recovery_details(self.job["id"])
                self.assertFalse(details["can_handoff"])
                self.assertFalse(details["can_resolve"])
                with self.assertRaises(ValueError):
                    self.runner.handoff_job(self.job["id"])
                with self.assertRaises(ValueError):
                    self.runner.reconcile_job(self.job["id"], "Verified")
        self.assertEqual(self.store.get_job(self.job["id"])["state"], "blocked")

    def test_missing_original_environment_fails_closed(self):
        spec = supervisor.read_json(self.directory / "spec.json")
        del spec["environment"]
        supervisor.write_json(self.directory / "spec.json", spec)
        with self.store._transaction() as db:
            db.execute("UPDATE attempts SET launch_metadata=? WHERE id=?", (json.dumps({"spec_sha256": supervisor.fingerprint(spec)}), self.job["attempt_id"]))
        evidence = {**self.evidence, "spec_sha256": supervisor.fingerprint(spec)}
        supervisor.write_json(self.directory / "process.json", evidence)
        supervisor.write_json(self.directory / "exit.json", {**evidence, "group_stopped": True})
        details = self.runner.recovery_details(self.job["id"])
        self.assertTrue(details["process"]["quiescent"])
        self.assertFalse(details["can_handoff"])
        self.assertIsNone(details["inspect_command"])
        with self.assertRaises(ValueError):
            self.runner.resume_job(self.job["id"])

    def test_external_same_session_double_launch_then_verified_successor(self):
        details = self.runner.handoff_job(self.job["id"])
        job = self.store.get_job(self.job["id"])
        _, directory = self.runner._paths(job)
        master, slave = os.openpty()
        tool = Path(__file__).resolve().parents[1] / "tools/dashboard-job.py"
        command = [sys.executable, str(tool), "--root", str(self.root), "--job", job["id"], "--attempt", job["attempt_id"], "--external-resume"]
        helper = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave)
        try:
            self.wait_for(lambda: (self.root / "invocation.json").exists())
            invocation = json.loads((self.root / "invocation.json").read_text())
            self.assertEqual(invocation["environment"], self.environment)
            self.assertEqual(invocation["argv"][invocation["argv"].index("--session") + 1], "ses_original")
            self.assertNotIn("--prompt", invocation["argv"])
            second = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertEqual(second.returncode, 2)
            self.assertFalse(self.runner.recovery_details(job["id"])["can_resolve"])
            with self.assertRaises(ValueError):
                self.runner.reconcile_job(job["id"], "Not stopped")
            self.runner.tick()
            self.assertIsNone(self.store.claim_next("held-while-running"))
            (self.root / "release").touch()
            helper.wait(timeout=10)
            self.assertEqual(helper.returncode, 0)
            self.assertTrue(supervisor.read_json(directory / "exit.json")["group_stopped"])
            self.runner.tick()
            details = self.runner.recovery_details(job["id"])
            self.assertTrue(details["can_resolve"])
            self.assertIsNone(details["resume_command"])
            self.assertIsNone(self.store.claim_next("held-after-helper-exit"))
            with self.assertRaises(Exception):
                self.runner.reconcile_job(job["id"], "Pack not ready")
            self.fixture.publish()
            result = self.runner.reconcile_job(job["id"], "Inspected immutable family and original reviews after same-session repair")
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(result["session_id"], "ses_original")
            self.assertEqual(result["attempts"][0]["completion_status"], "blocked")
            self.assertIsNone(result["attempts"][-1]["completion_status"])
            self.assertEqual(result["launch_metadata"]["operator_verification"]["kind"], "operator_verification")
            self.runner.reconcile_job(job["id"], "Second click")
            self.assertEqual(len([event for event in self.store.events(job["id"]) if event["kind"] == "operator_verified"]), 1)
            self.assertEqual(self.store.claim_next("verified-successor")["base_checkpoint"], 40)
        finally:
            (self.root / "release").touch()
            evidence = supervisor.read_json(directory / "process.json") or {}
            if helper.poll() is None:
                import psutil
                child_status = [(child.pid, child.status(), child.cmdline()) for child in psutil.Process(helper.pid).children(recursive=True)]
                evidence["test_children"] = child_status
                if evidence.get("child"):
                    supervisor.signal_group(evidence["child"], signal.SIGKILL)
                helper.kill()
                helper.wait(timeout=5)
            import select
            output = os.read(master, 65536) if select.select([master], [], [], .1)[0] else b""
            os.close(master)
            os.close(slave)
            self.assertTrue((self.root / "invocation.json").exists(), (output.decode(errors="replace"), evidence))

    def test_stale_command_cannot_bypass_reservation_by_changing_mode(self):
        self.runner.handoff_job(self.job["id"])
        job = self.store.get_job(self.job["id"])
        _, directory = self.runner._paths(job)
        self.runner.stop_job(job["id"])
        spec = supervisor.read_json(directory / "spec.json")
        spec.pop("mode")
        spec["command"] = [sys.executable, "-c", "pass"]
        supervisor.write_json(directory / "spec.json", spec)
        tool = Path(__file__).resolve().parents[1] / "tools/dashboard-job.py"
        result = subprocess.run([sys.executable, str(tool), "--root", str(self.root), "--job", job["id"], "--attempt", job["attempt_id"], "--external-resume"], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        direct = subprocess.run([sys.executable, supervisor.__file__, "--spec", str(directory / "spec.json")], capture_output=True, timeout=5)
        self.assertEqual(direct.returncode, 1)
        self.assertFalse((directory / "process.json").exists())
        self.assertEqual(self.store.get_job(job["id"])["state"], "interrupted")

    def test_stopped_unresolved_external_can_explicitly_end_and_handoff_again(self):
        self.runner.handoff_job(self.job["id"])
        job = self.store.get_job(self.job["id"])
        _, directory = self.runner._paths(job)
        spec = supervisor.read_json(directory / "spec.json")
        evidence = {**self.evidence, "attempt_id": job["attempt_id"], "spec_sha256": supervisor.fingerprint(spec)}
        supervisor.write_json(directory / "process.json", evidence)
        supervisor.write_json(directory / "exit.json", {**evidence, "group_stopped": True})
        self.runner.tick()
        with self.assertRaises(Exception):
            self.runner.reconcile_job(job["id"], "No pack yet")
        self.assertIsNone(self.store.get_job(job["id"])["attempts"][-1]["ended_at"])
        self.runner.stop_job(job["id"])
        next_attempt = self.runner.handoff_job(job["id"])
        self.assertEqual(next_attempt["session_id"], "ses_original")
        self.assertNotEqual(next_attempt["attempt_id"], job["attempt_id"])
        self.assertEqual(len(next_attempt["attempts"]), 3)

    def test_changed_process_identity_cannot_release(self):
        self.runner.handoff_job(self.job["id"])
        job = self.store.get_job(self.job["id"])
        _, directory = self.runner._paths(job)
        dead = self.evidence["supervisor"]
        alive = supervisor.process_identity(os.getpid())
        self.store.attach_launch(job["id"], job["attempt_id"], {"supervisor": dead, "child": alive, "phase": "running"})
        spec = supervisor.read_json(directory / "spec.json")
        process = {"job_id": job["id"], "attempt_id": job["attempt_id"], "spec_sha256": supervisor.fingerprint(spec), "session_id": "ses_original", "supervisor": dead, "child": dead, "phase": "stopped"}
        supervisor.write_json(directory / "process.json", process)
        self.assertFalse(self.runner.recovery_details(job["id"])["can_resolve"])
        with self.assertRaises(ValueError):
            self.runner.reconcile_job(job["id"], "Changed identity must fail")
        self.assertIsNone(self.store.claim_next("held"))

    def test_orphan_external_stop_signals_only_verified_child(self):
        self.runner.handoff_job(self.job["id"])
        job = self.store.get_job(self.job["id"])
        _, directory = self.runner._paths(job)
        dead = self.evidence["supervisor"]
        alive = supervisor.process_identity(os.getpid())
        self.store.attach_launch(job["id"], job["attempt_id"], {"supervisor": dead, "child": alive, "phase": "running"})
        spec = supervisor.read_json(directory / "spec.json")
        process = {"job_id": job["id"], "attempt_id": job["attempt_id"], "spec_sha256": supervisor.fingerprint(spec), "session_id": "ses_original", "supervisor": dead, "child": alive, "phase": "running"}
        supervisor.write_json(directory / "process.json", process)
        with patch("lesson_factory.dashboard_runner.signal_group", return_value=True) as stop:
            self.runner.stop_job(job["id"])
        stop.assert_called_once_with(alive, signal.SIGTERM)
        self.assertIsNone(self.store.get_job(job["id"])["attempts"][-1]["ended_at"])

    def test_scheduler_does_not_snapshot_inactive_history(self):
        with patch.object(self.store, "snapshot", side_effect=AssertionError("full history queried")):
            self.runner.tick()


class SupervisorTests(unittest.TestCase):
    def test_pid_reuse_and_command_mismatch_never_signal(self):
        identity = supervisor.process_identity(os.getpid())
        identity["create_time"] -= 1
        self.assertEqual(supervisor.process_state(identity), "dead")
        with patch("os.killpg") as kill:
            self.assertFalse(supervisor.signal_group(identity, signal.SIGTERM))
        kill.assert_not_called()
        identity = supervisor.process_identity(os.getpid())
        identity["command_fingerprint"] = "different"
        self.assertEqual(supervisor.process_state(identity), "unknown")

    def test_home_isolated_but_xdg_auth_provider_and_sessions_shared(self):
        inherited = {"HOME": "/original/home", "PATH": "/node24/bin:/bin", "XDG_DATA_HOME": "/custom/data"}
        first = supervisor.isolated_environment("/jobs/one/home", inherited)
        second = supervisor.isolated_environment("/jobs/two/home", inherited)
        self.assertNotEqual(Path(first["HOME"]) / ".config/opencode/ensemble.db", Path(second["HOME"]) / ".config/opencode/ensemble.db")
        self.assertEqual(first["XDG_CONFIG_HOME"], "/original/home/.config")
        self.assertEqual(first["XDG_DATA_HOME"], "/custom/data")
        self.assertEqual(first["XDG_STATE_HOME"], "/original/home/.local/state")
        self.assertEqual(first["XDG_CACHE_HOME"], "/original/home/.cache")
        self.assertEqual(first["PATH"], "/node24/bin:/bin")
        self.assertEqual(first["XDG_DATA_HOME"], second["XDG_DATA_HOME"])
        self.assertEqual(first["OPENCODE_DATA_DIR"], "/original/home/.local/share/opencode")
        self.assertEqual(supervisor.isolated_environment("/jobs/one/home", {**inherited, "OPENCODE_DATA_DIR": "/explicit/auth"})["OPENCODE_DATA_DIR"], "/explicit/auth")
        with self.assertRaises(ValueError):
            supervisor.isolated_environment("/jobs/one/home", {**inherited, "OPENCODE_DATA_DIR": "relative"})
        self.assertEqual(inherited["HOME"], "/original/home")
        with self.assertRaises(ValueError):
            supervisor.isolated_environment("/jobs/one/home", {**inherited, "XDG_DATA_HOME": "relative"})

    def test_event_reader_only_first_top_level_session(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "events.jsonl"
            events = [{"type": "text", "part": {"sessionID": "ses_nested"}}, {"type": "step_start", "sessionID": "ses_coordinator"}, {"type": "tool_use", "sessionID": "ses_coordinator", "part": {"output": {"sessionID": "ses_review"}}}]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            reader = supervisor.EventReader(path)
            while reader.poll():
                pass
            self.assertEqual(reader.session_id, "ses_coordinator")
            self.assertFalse(reader.conflict)
            with path.open("a") as handle:
                handle.write(json.dumps({"type": "text", "sessionID": "ses_other"}) + "\n")
            reader.poll()
            self.assertTrue(reader.conflict)
            self.assertEqual(reader.session_id, "ses_coordinator")

    def test_oversized_and_partial_events_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "events.jsonl"
            path.write_bytes(b"x" * 3000000 + b"\n" + b'{"type":"text","sessionID":"ses_good"}\n')
            reader = supervisor.EventReader(path)
            while reader.poll():
                self.assertLessEqual(len(reader.partial), 2097152)
            self.assertEqual(reader.session_id, "ses_good")

    def test_symlink_parent_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            outside = root / "outside"
            outside.mkdir(mode=0o755)
            (root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                supervisor.private_directory(root / "link/secret")
            self.assertFalse((outside / "secret").exists())
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
            for operation in (supervisor.private_open, supervisor.read_json):
                with self.assertRaises(ValueError):
                    operation(root / "link/secret")
            with self.assertRaises(ValueError):
                supervisor.write_json(root / "link/secret", {})

    def test_child_gate_eof_never_executes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "spec.json"
            marker = Path(temp).resolve() / "unexpected"
            supervisor.write_json(path, {"root": temp, "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]})
            read_fd, write_fd = os.pipe()
            os.close(write_fd)
            try:
                self.assertEqual(supervisor.coordinator_group(path, read_fd, 0), 125)
            finally:
                os.close(read_fd)
            self.assertFalse(marker.exists())

    def test_real_fake_process_captures_streams_session_and_private_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directory = supervisor.private_directory(root / "work/dashboard/jobs/job/attempts/attempt")
            supervisor.private_directory(directory.parent.parent / "home")
            code = "import json,os,sys; print(json.dumps({'type':'text','sessionID':'ses_fake','part':{'sessionID':'ses_nested'}}),flush=True); sys.stderr.write('private token=example\\n'); print(os.environ['HOME'],flush=True)"
            spec = {"root": str(root), "job_id": "job", "attempt_id": "attempt", "session_id": None, "command": [sys.executable, "-c", code]}
            supervisor.write_json(directory / "spec.json", spec)
            process = subprocess.Popen([sys.executable, supervisor.__file__, "--spec", str(directory / "spec.json")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                process.wait(timeout=15)
                self.assertEqual(process.returncode, 0)
                receipt = supervisor.read_json(directory / "exit.json")
                self.assertEqual(receipt["session_id"], "ses_fake")
                self.assertEqual(receipt["exit_code"], 0)
                self.assertTrue(receipt["group_stopped"])
                self.assertNotIn("token", json.dumps(receipt))
                self.assertIn(str(directory.parent.parent / "home"), (directory / "events.jsonl").read_text())
                for name in ("exit.json", "process.json", "events.jsonl", "stderr.log"):
                    self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
            finally:
                if process.poll() is None:
                    evidence = supervisor.read_json(directory / "process.json")
                    if evidence and evidence.get("child"):
                        supervisor.signal_group(evidence["child"], signal.SIGKILL)
                    process.kill()
                    process.wait(timeout=5)


class DurableProcessTests(unittest.TestCase):
    def setUp(self):
        from lesson_factory.dashboard_store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        folder = self.root / "reference/vocabulary/German"
        folder.mkdir(parents=True)
        with (folder / "german_vocabulary.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Rank", "Word"])
            writer.writerows((rank, f"word{rank}") for rank in range(1, 301))
        self.store = Store(self.root)
        authorization = {"paid_scope_acknowledged": True, "max_translation_attempts": 4, "families": [{"language": "de", "base_checkpoint": 20}]}
        self.batch = self.store.create_batch(["de"], 1, 1, authorization)
        self.runner = None
        self.executable = self.root / "fake-coordinator"

    def tearDown(self):
        for path in self.root.glob("work/dashboard/jobs/*/attempts/*/process.json"):
            evidence = supervisor.read_json(path)
            if evidence.get("child"):
                supervisor.signal_group(evidence["child"], signal.SIGKILL)
            identity = evidence.get("supervisor")
            if supervisor.process_state(identity) == "alive":
                os.kill(identity["pid"], signal.SIGTERM)
        if self.runner:
            for process in self.runner._children:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.runner.close()
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("Timed out waiting for synthetic process evidence")

    def fake(self, code):
        preflight = "import sys,json,os\nif '--version' in sys.argv: print('opencode v2.0.29');sys.exit(0)\nif 'api' in sys.argv: print(json.dumps({'data':{'id':'ses_crash_fixture','location':{'directory':os.getcwd()},'agent':'lesson-coordinator'}}));sys.exit(0)\n"
        self.executable.write_text("#!" + sys.executable + "\n" + preflight + code)
        self.executable.chmod(0o700)

    def active_directory(self):
        job = self.store.snapshot()["jobs"][0]
        return job, self.root / "work/dashboard/jobs" / job["id"] / "attempts" / job["attempt_id"]

    def test_dashboard_crash_reconnect_stop_and_exact_session_resume(self):
        self.fake("import json,time\nprint(json.dumps({'type':'step_start','sessionID':'ses_crash_fixture'}),flush=True)\ntime.sleep(60)\n")
        project = Path(__file__).resolve().parents[1]
        code = "import os,sys;sys.path.insert(0,sys.argv[1]);from lesson_factory.dashboard_store import Store;from lesson_factory.dashboard_runner import Runner;s=Store(sys.argv[2]);r=Runner(sys.argv[2],s);r.recover();s.resume(sys.argv[3]);r.tick();os._exit(0)"
        crashed = subprocess.run([sys.executable, "-c", code, str(project), str(self.root), self.batch], env={**os.environ, "OPENCODE_BINARY": str(self.executable)}, capture_output=True, timeout=10)
        self.assertEqual(crashed.returncode, 0)
        original, directory = self.active_directory()
        self.wait_for(lambda: (supervisor.read_json(directory / "process.json") or {}).get("session_id"))
        self.runner = Runner(self.root, self.store)
        self.runner.recover()
        saved = self.store.get_job(original["id"])
        self.assertEqual(saved["session_id"], "ses_crash_fixture")
        self.assertTrue(self.store.snapshot()["batches"][0]["paused"])
        self.assertEqual(supervisor.process_state(saved["launch_metadata"]["supervisor"]), "alive")
        self.runner.stop_job(saved["id"])
        self.wait_for(lambda: (self.runner.tick(), self.store.get_job(saved["id"])["state"] == "interrupted")[1])
        self.assertEqual(supervisor.group_state(saved["launch_metadata"]["child"]), "dead")
        self.runner.resume_job(saved["id"])
        self.store.resume(self.batch)
        with patch.dict(os.environ, {"OPENCODE_BINARY": str(self.executable)}):
            self.runner.tick()
        resumed, directory = self.active_directory()
        self.assertNotEqual(original["attempt_id"], resumed["attempt_id"])
        spec = supervisor.read_json(directory / "spec.json")
        self.assertEqual(spec["command"][spec["command"].index("--session") + 1], "ses_crash_fixture")
        self.wait_for(lambda: (supervisor.read_json(directory / "process.json") or {}).get("session_id"))
        self.runner.stop_job(saved["id"])
        self.wait_for(lambda: (self.runner.tick(), self.store.get_job(saved["id"])["state"] == "interrupted")[1])

    def test_descendant_holds_group_and_slot_after_coordinator_exits(self):
        self.fake("import json,subprocess,sys\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\nprint(json.dumps({'type':'text','sessionID':'ses_descendant'}),flush=True)\n")
        self.runner = Runner(self.root, self.store)
        self.store.resume(self.batch)
        with patch.dict(os.environ, {"OPENCODE_BINARY": str(self.executable)}):
            self.runner.tick()
        job, directory = self.active_directory()
        self.wait_for(lambda: (supervisor.read_json(directory / "process.json") or {}).get("session_id"))
        self.runner.tick()
        job = self.store.get_job(job["id"])
        self.assertEqual(job["state"], "running")
        self.assertFalse((directory / "exit.json").exists())
        self.assertFalse(self.runner._job_lock_free(directory.parent.parent))
        self.assertEqual(supervisor.group_state(job["launch_metadata"]["child"]), "alive")
        self.runner.stop_job(job["id"])
        self.wait_for(lambda: (self.runner.tick(), self.store.get_job(job["id"])["state"] == "interrupted")[1])
        self.assertEqual(supervisor.group_state(job["launch_metadata"]["child"]), "dead")


if __name__ == "__main__":
    unittest.main()
