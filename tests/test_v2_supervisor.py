"""V2 supervisor ownership + OPENCODE_DB env scoping.

Bounded, non-mutating-safe real-process tests (separate PGIDs, exact env
identity) plus pure unit coverage for saved_environment/OPENCODE_DB merge.
Runs the managed supervisor (supervise()) against fake CLIs that mimic the
offline-smoke V2 topology: CLI wrapper -> private server (own PGID) ->
foreground tool (own PGID).
"""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import unittest.mock
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lesson_factory import dashboard_supervisor as supervisor


def wait_for(predicate, timeout=20.0, interval=0.05, message="timed out"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(message)


class ManagedAttempt:
    """Run supervise() in-process against a fake CLI; kill leftovers by identity."""

    CLI_POLL_TIMEOUT = 60.0

    def __init__(self, test, cli_code):
        self.test = test
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup = test.addCleanup
        self.root = Path(self.tmp.name).resolve()
        self.job_id, self.attempt_id = uuid.uuid4().hex, uuid.uuid4().hex
        self.directory = supervisor.private_directory(
            self.root / 'work/dashboard/jobs' / self.job_id / 'attempts' / self.attempt_id)
        supervisor.private_directory(self.directory.parent.parent / "home")
        self.cli = self.root / "fake-cli.py"
        self.cli.write_text("#!" + sys.executable + "\n" + cli_code)
        self.cli.chmod(0o700)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._cleanup_owned)

    def _cleanup_owned(self):
        for name in ("process.json", "exit.json"):
            evidence = supervisor.read_json(self.directory / name)
            if not isinstance(evidence, dict):
                continue
            for identity in [*(evidence.get("owned") or []), evidence.get("child"), evidence.get("supervisor")]:
                if isinstance(identity, dict) and supervisor.process_state(identity) == "alive":
                    try:
                        os.kill(identity["pid"], signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass

    def launch(self, code, extra_env=None, mode=None, session_id="ses_owned_fixture"):
        cli_argv = [sys.executable, str(self.cli)]
        self.cli.write_text("#!" + sys.executable + "\n" + code)
        self.cli.chmod(0o700)
        env = supervisor.saved_environment(
            supervisor.isolated_environment(self.directory.parent.parent / "home"),
            self.directory.parent.parent / "home")
        if extra_env is not None:
            env.update(extra_env)
        spec = {"root": str(self.root), "job_id": self.job_id, "attempt_id": self.attempt_id,
                "session_id": session_id, "command": cli_argv, "environment": env}
        if mode is not None:
            spec["mode"] = mode
        supervisor.write_json(self.directory / "spec.json", spec)
        child = subprocess.Popen(
            [sys.executable, supervisor.__file__, "--spec", str(self.directory / "spec.json")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
        return child

    def wait_exit(self, child, timeout=CLI_POLL_TIMEOUT):
        """Bounded wait that always reaps; fails the test instead of hanging."""
        try:
            return child.wait(timeout=timeout)
        finally:
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    def process(self):
        return supervisor.read_json(self.directory / "process.json") or {}

    def receipt(self):
        return supervisor.read_json(self.directory / "exit.json")


# Fake CLI topology mirroring work/v2-migration/smoke/REPORT.md: the wrapper
# forks a "private server" in its OWN process group (setsid), which forks a
# "foreground tool" in yet another group. Markers are inherited through env.
# Sleeps stay bounded so the managed supervisor always terminates by itself.
SERVER_TOOL_CODE = """import json,subprocess,sys,time
server = subprocess.Popen([sys.executable, "-c",
    "import subprocess,sys; tool = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); tool.wait()"],
    start_new_session=True)
print(json.dumps({"type":"text","sessionID":"ses_owned_fixture"}), flush=True)
code = server.wait()
sys.exit(code)
"""

QUICK_EXIT_CODE = """import json,sys
print(json.dumps({"type":"text","sessionID":"ses_owned_fixture"}), flush=True)
"""


class OwnedDiscoveryTests(unittest.TestCase):
    def test_finished_tool_history_does_not_exhaust_live_identity_cap(self):
        history = [{'pid': n, 'create_time': n, 'boot_id': 'old'} for n in range(300)]
        current = {'pid': 999, 'create_time': 999, 'boot_id': 'new'}
        with patch.object(supervisor, 'process_state', return_value='dead'):
            merged, overflow = supervisor.merge_owned(history, [current])
        self.assertEqual(merged, [current])
        self.assertFalse(overflow)
        with patch.object(supervisor, 'process_state', return_value='unknown'):
            _, overflow = supervisor.merge_owned(history, [])
        self.assertTrue(overflow)

    def test_unrelated_env_process_is_never_owned(self):
        # This test process itself must never match a foreign job/attempt.
        owned, uncertain = supervisor.discover_owned("no-such-job", "no-such-attempt", time.time() - 60)
        self.assertEqual(owned, [])
        self.assertFalse(uncertain)

    def test_floor_excludes_preflight_aged_processes(self):
        # A future floor excludes everything born earlier (preflight probes,
        # prior attempts, system processes) without false ownership.
        owned, uncertain = supervisor.discover_owned("job", "attempt", time.time() + 3600)
        self.assertEqual(owned, [])
        self.assertFalse(uncertain)

    def test_invalid_scope_is_uncertain_not_quiet(self):
        quiescent, owned, uncertain = supervisor.ownership_quiescent("", "", time.time())
        self.assertFalse(quiescent)
        self.assertTrue(uncertain)

    def test_eperm_and_predating_processes_never_hold_the_slot(self):
        # Regression for the universal-unknown bug: a foreign process (uids
        # unreadable, signal EPERM — the same skip group_state() applies) and
        # a predating same-UID process (preflight probe / global server) must
        # exclude as False, never None (uncertain).
        floor = time.time()

        class ForeignProcess:
            pid = 424242

            def create_time(self):
                return floor + 10  # post-floor: would be a candidate

            def uids(self):
                raise PermissionError("mock foreign uids")

        with unittest.mock.patch("os.kill", side_effect=PermissionError("mock EPERM")):
            self.assertFalse(supervisor._owned_candidate_usable(ForeignProcess(), floor))

        class PredatingProcess:
            pid = 424243

            def create_time(self):
                return floor - 3600

            def uids(self):
                raise AssertionError("birth gate must exclude first")

        self.assertFalse(supervisor._owned_candidate_usable(PredatingProcess(), floor))

    def test_live_machine_scan_stays_quiet_for_unknown_scope(self):
        # Real smoke on this machine: no unrelated OS process may poison
        # ownership — an unknown job/attempt scans clean with zero uncertainty.
        floor = time.time() - 1
        owned, uncertain = supervisor.discover_owned("no-such-job", "no-such-attempt", floor)
        self.assertEqual(owned, [])
        self.assertFalse(uncertain, "unrelated OS processes must not poison ownership")
        quiescent, _, _ = supervisor.ownership_quiescent("no-such-job", "no-such-attempt", floor)
        self.assertTrue(quiescent)

    def test_cross_pgid_owned_child_found_and_killed_by_identity(self):
        attempt = ManagedAttempt(self, QUICK_EXIT_CODE)
        script = ("import subprocess,sys,time\n"
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
                  "print('READY', flush=True)\n"
                  "child.wait()\n")
        child = attempt.launch(script)
        try:
            wait_for(lambda: attempt.process().get("child"), message="supervisor never spawned wrapper")
            child_identity = attempt.process()["child"]
            floor = child_identity["create_time"]
            wait_for(
                lambda: [i for i in supervisor.discover_owned(attempt.job_id, attempt.attempt_id, floor)[0]
                         if i["pid"] != child_identity["pid"]],
                message="cross-PGID owned descendant never discovered")
            owned, uncertain = supervisor.discover_owned(attempt.job_id, attempt.attempt_id, floor)
            self.assertFalse(uncertain)
            pids = [identity["pid"] for identity in owned]
            self.assertNotIn(os.getpid(), pids)
            self.assertNotIn(1, pids)
            victim = next(identity for identity in owned if identity["pid"] != child_identity["pid"])
            before = supervisor.process_state(victim)
            self.assertEqual(before, "alive")
            self.assertGreater(supervisor.signal_owned([victim], signal.SIGKILL, attempt.job_id, attempt.attempt_id), 0)
            wait_for(lambda: supervisor.process_state(victim) == "dead",
                     message="owned descendant survived exact-identity SIGKILL")
            # PID-reuse safety: a recycled PID with different birth is dead to us.
            stale = dict(victim, create_time=victim["create_time"] - 100)
            self.assertEqual(supervisor.process_state(stale), "dead")
            self.assertEqual(supervisor.signal_owned([stale], signal.SIGKILL, attempt.job_id, attempt.attempt_id), 0)
            me = supervisor.process_identity(os.getpid())
            supervisor.signal_owned([me], signal.SIGKILL, attempt.job_id, attempt.attempt_id)
            self.assertTrue(True)  # survived: self is never signaled
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)

    def test_delayed_descendant_blocks_receipt_until_stopped(self):
        # A detached 20s grandchild outlives the wrapper: the owned gate must
        # hold the slot (no exit receipt yet). Stopping then releases it with
        # an honest group_stopped receipt.
        attempt = ManagedAttempt(self, QUICK_EXIT_CODE)
        script = ("import subprocess,sys\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True,\n"
                  "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)\n"
                  "print('SPAWNED', flush=True)\n")
        child = attempt.launch(script)
        try:
            wait_for(lambda: attempt.process().get("owned"),
                     message="delayed owned descendant never persisted")
            time.sleep(2.0)
            # Slot held: wrapper is done but the receipt must not exist yet.
            self.assertIsNone(attempt.receipt())
            persisted = attempt.process()
            self.assertTrue(persisted.get("owned"))
            self.assertFalse(persisted.get("ownership_uncertain"))
            supervisor.write_json(
                attempt.directory / "stop.json",
                {"job_id": attempt.job_id, "attempt_id": attempt.attempt_id, "action": "stop"})
            attempt.wait_exit(child)
            receipt = attempt.receipt()
            self.assertIsNotNone(receipt)
            floor = attempt.process()["child"]["create_time"]
            quiescent, _, uncertain = supervisor.ownership_quiescent(attempt.job_id, attempt.attempt_id, floor)
            self.assertTrue(quiescent, f"ownership not quiescent (uncertain={uncertain})")
            if quiescent:
                self.assertTrue(receipt["group_stopped"])
        finally:
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    def test_managed_run_persists_owned_and_marks_group_stopped_only_when_quiet(self):
        attempt = ManagedAttempt(self, SERVER_TOOL_CODE)
        child = attempt.launch(SERVER_TOOL_CODE)
        try:
            attempt.wait_exit(child)
            self.assertEqual(child.returncode, 0)
            receipt = attempt.receipt()
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["phase"], "stopped")
            self.assertEqual(receipt["session_id"], "ses_owned_fixture")
            self.assertIn("owned", receipt)
            self.assertIn("ownership_uncertain", receipt)
            # A clean quick run leaves no owned survivors: honest group_stopped.
            floor = attempt.process()["child"]["create_time"]
            quiescent, _, _ = supervisor.ownership_quiescent(attempt.job_id, attempt.attempt_id, floor)
            if quiescent:
                self.assertTrue(receipt["group_stopped"])
            for name in ("exit.json", "process.json", "events.jsonl", "stderr.log"):
                self.assertEqual(stat.S_IMODE((attempt.directory / name).stat().st_mode), 0o600)
        finally:
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    def test_abrupt_cli_kill_reaps_cross_pgid_server_and_tool(self):
        attempt = ManagedAttempt(self, SERVER_TOOL_CODE)
        code = ("import subprocess,sys,time\n"
                "server = subprocess.Popen([sys.executable, '-c',\n"
                "    \"import subprocess,sys; tool = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); tool.wait()\"],\n"
                "    start_new_session=True)\n"
                "print('UP', flush=True)\n"
                "server.wait()\n")
        child = attempt.launch(code)
        try:
            wait_for(lambda: attempt.process().get("child"), message="supervisor never spawned wrapper")
            wrapper = attempt.process()["child"]
            floor = wrapper["create_time"]
            wait_for(lambda: [i for i in supervisor.discover_owned(attempt.job_id, attempt.attempt_id, floor)[0]
                              if i["pid"] != wrapper["pid"]],
                     message="server/tool never appeared")
            # Abrupt kill: SIGKILL the CLI wrapper itself (no lifecycle cleanup).
            os.kill(wrapper["pid"], signal.SIGKILL)
            wait_for(lambda: supervisor.process_state(wrapper) == "dead", message="wrapper survived SIGKILL")
            # The wrapper's own reap path runs only inside coordinator_group;
            # emulate what it does on signal death, then assert the result.
            owned_before, _ = supervisor.discover_owned(attempt.job_id, attempt.attempt_id, floor)
            self.assertTrue(owned_before, "expected cross-PGID survivors after abrupt kill")
            supervisor._reap_owned_after_cli(attempt.job_id, attempt.attempt_id, floor)
            wait_for(
                lambda: not supervisor.classify_owned(
                    supervisor.discover_owned(attempt.job_id, attempt.attempt_id, floor)[0], attempt.job_id, attempt.attempt_id)[0],
                timeout=15.0, message="cross-PGID server/tool survived bounded reap")
            quiescent, _, uncertain = supervisor.ownership_quiescent(attempt.job_id, attempt.attempt_id, floor)
            self.assertTrue(quiescent, f"ownership not quiescent (uncertain={uncertain})")
            self.assertFalse(uncertain)
        finally:
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass


class SavedEnvironmentDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "home"
        self.home.mkdir()

    def _base(self, db=...):
        base = {"HOME": "/orig", "XDG_CONFIG_HOME": "/orig/.config",
                "XDG_DATA_HOME": "/orig/.local/share", "XDG_STATE_HOME": "/orig/.local/state",
                "XDG_CACHE_HOME": "/orig/.cache", "OPENCODE_DATA_DIR": "/orig/.local/share/opencode"}
        if db is not ...:
            base["OPENCODE_DB"] = db
        return base

    def _isolated(self, base):
        return supervisor.isolated_environment(self.home, base)

    def test_old_env_records_stay_six_keys_and_fingerprints_stable(self):
        saved = supervisor.saved_environment(self._isolated(self._base()), self.home)
        self.assertEqual(set(saved), set(supervisor.ENVIRONMENT_PATHS))
        self.assertNotIn("OPENCODE_DB", saved)
        before = supervisor.fingerprint({"environment": saved})
        again = supervisor.saved_environment(dict(saved), self.home)
        self.assertEqual(again, saved)
        self.assertEqual(supervisor.fingerprint({"environment": again}), before)

    def test_new_capture_records_set_value(self):
        isolated = self._isolated(self._base(db="/orig/.local/share/opencode/owned.sqlite"))
        saved = supervisor.saved_job_environment(isolated, self.home)
        self.assertEqual(saved["OPENCODE_DB"], "/orig/.local/share/opencode/owned.sqlite")
        self.assertEqual(set(saved) - set(supervisor.ENVIRONMENT_PATHS), {"OPENCODE_DB"})

    def test_new_capture_records_unset_as_none(self):
        isolated = self._isolated(self._base())
        saved = supervisor.saved_job_environment(isolated, self.home)
        self.assertIn("OPENCODE_DB", saved)
        self.assertIsNone(saved["OPENCODE_DB"])

    def test_changed_ambient_db_does_not_leak_into_cleared_record(self):
        saved = supervisor.saved_job_environment(self._isolated(self._base()), self.home)
        self.assertIsNone(saved["OPENCODE_DB"])
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_DB": "/ambient/changed.sqlite"}):
            base = dict(supervisor.isolated_environment(self.home))
            self.assertEqual(base.get("OPENCODE_DB"), "/ambient/changed.sqlite")
            flag = supervisor.apply_saved_environment(base, saved, self.home)
            self.assertEqual(flag, "cleared")
            self.assertNotIn("OPENCODE_DB", base)
            for value in base.values():
                self.assertIsInstance(value, str)

    def test_set_record_overrides_changed_ambient(self):
        saved = supervisor.saved_job_environment(
            self._isolated(self._base(db="/job/owned.sqlite")), self.home)
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_DB": "/ambient/other.sqlite"}):
            base = dict(supervisor.isolated_environment(self.home))
            flag = supervisor.apply_saved_environment(base, saved, self.home)
            self.assertEqual(flag, "set")
            self.assertEqual(base["OPENCODE_DB"], "/job/owned.sqlite")

    def test_old_record_defers_to_ambient(self):
        saved = supervisor.saved_environment(self._isolated(self._base()), self.home)
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_DB": "/ambient/kept.sqlite"}):
            base = dict(supervisor.isolated_environment(self.home))
            flag = supervisor.apply_saved_environment(base, saved, self.home)
            self.assertEqual(flag, "ambient")
            self.assertEqual(base["OPENCODE_DB"], "/ambient/kept.sqlite")

    def test_invalid_db_rejected_before_mutation(self):
        saved = supervisor.saved_job_environment(self._isolated(self._base()), self.home)
        bad = dict(saved, OPENCODE_DB="relative/path.sqlite")
        base = dict(supervisor.isolated_environment(self.home))
        snapshot = dict(base)
        with self.assertRaises(ValueError):
            supervisor.apply_saved_environment(base, bad, self.home)
        self.assertEqual(base, snapshot)

    def test_apply_never_leaves_none_in_subprocess_env(self):
        saved = supervisor.saved_job_environment(self._isolated(self._base()), self.home)
        base = dict(supervisor.isolated_environment(self.home))
        supervisor.apply_saved_environment(base, saved, self.home)
        json.dumps(base)  # must be JSON-serializable str env
        for value in base.values():
            self.assertIsInstance(value, str)


if __name__ == "__main__":
    unittest.main()
