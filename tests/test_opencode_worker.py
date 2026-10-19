"""Pure/fake tests for lesson_factory.opencode_worker. No model calls, no queue/config edits."""
import json
import subprocess
import unittest
from unittest.mock import patch

from lesson_factory import opencode_worker as worker

EXE = "/opt/homebrew/bin/opencode"
ROOT = "/tmp/fake-lessons-root"
SID = "ses_abcDEF123-x"
PROMPT = "Synthetic coordinator prompt"


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class VersionTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(worker.Path, 'is_file', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(worker.os, 'access', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_accepts_v2_line(self):
        with patch.object(worker.subprocess, "run", return_value=completed(stdout="opencode v2.0.29\n")) as run:
            self.assertEqual(worker.verify_binary(EXE), "2.0.29")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], [EXE, "--version"])
        self.assertEqual(kwargs["timeout"], worker.VERSION_TIMEOUT)
        self.assertTrue(kwargs["capture_output"])

    def test_rejects_v1_line(self):
        with patch.object(worker.subprocess, "run", return_value=completed(stdout="1.18.33\n")):
            with self.assertRaisesRegex(ValueError, "V2"):
                worker.verify_binary(EXE)

    def test_rejects_unrecognized_output(self):
        with patch.object(worker.subprocess, "run", return_value=completed(stdout="no version here\n")):
            with self.assertRaises(ValueError):
                worker.verify_binary(EXE)

    def test_version_lookalikes_and_future_major_fail_closed(self):
        for text in ('warning2.0.29', '1.18.33 with v2.0.29', 'opencode v3.0.0', 'opencode v2.0.29-beta'):
            with patch.object(worker.subprocess, 'run', return_value=completed(stdout=text)), self.assertRaises(ValueError):
                worker.verify_binary(EXE)

    def test_rejects_failed_check(self):
        with patch.object(worker.subprocess, "run", return_value=completed(returncode=1, stdout="opencode v2.0.29\n")):
            with self.assertRaises(ValueError):
                worker.verify_binary(EXE)

    def test_rejects_relative_and_missing(self):
        with self.assertRaises(ValueError):
            worker.verify_binary("opencode")
        with patch.object(worker.Path, 'is_file', return_value=False), self.assertRaises(ValueError):
            worker.verify_binary("/nonexistent/opencode-binary-xyz")


class CommandTests(unittest.TestCase):
    def test_fresh_command_exact(self):
        self.assertEqual(
            worker.command(EXE, PROMPT),
            [EXE, "run", "--standalone", "--format", "json", "--agent",
             "lesson-coordinator", "--model", "opencode-omniroute/Antharas#xhigh", PROMPT],
        )

    def test_resume_command_exact(self):
        self.assertEqual(
            worker.command(EXE, PROMPT, SID),
            [EXE, "run", "--standalone", "--format", "json", "--agent",
             "lesson-coordinator", "--model", "opencode-omniroute/Antharas#xhigh",
             "--session", SID, PROMPT],
        )

    def test_command_has_no_variant_or_server(self):
        for value in (worker.command(EXE, PROMPT), worker.command(EXE, PROMPT, SID)):
            self.assertNotIn("--variant", value)
            self.assertNotIn("--server", value)
            self.assertNotIn("--continue", value)
            self.assertNotIn("--fork", value)

    def test_command_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            worker.command("opencode", PROMPT)
        with self.assertRaises(ValueError):
            worker.command(EXE, "  ")
        with self.assertRaises(ValueError):
            worker.command(EXE, PROMPT, "not-a-session")
        with self.assertRaises(ValueError):
            worker.command(EXE, PROMPT, "ses_bad!chars")


class ShapeTests(unittest.TestCase):
    def test_accepts_builder_output(self):
        self.assertTrue(worker.is_v2_command(worker.command(EXE, PROMPT)))
        self.assertTrue(worker.is_v2_command(worker.command(EXE, PROMPT, SID)))

    def test_rejects_v1_commands(self):
        v1_fresh = [EXE, "run", "--format", "json", "--agent", "lesson-coordinator",
                    "--model", "opencode-omniroute/Antharas", "--variant", "xhigh", PROMPT]
        v1_resume = [EXE, "run", "--format", "json", "--agent", "lesson-coordinator",
                     "--model", "opencode-omniroute/Antharas", "--variant", "xhigh",
                     "--session", SID, PROMPT]
        self.assertFalse(worker.is_v2_command(v1_fresh))
        self.assertFalse(worker.is_v2_command(v1_resume))
        self.assertIn("no automatic V1", worker.V1_RESUME_REFUSED)

    def test_rejects_banned_flags(self):
        base = worker.command(EXE, PROMPT)
        for flag in ("--variant", "--server", "--continue", "--fork"):
            self.assertFalse(worker.is_v2_command([*base[:-1], flag, base[-1]]))
        self.assertFalse(worker.is_v2_command("not-a-list"))
        self.assertFalse(worker.is_v2_command([EXE, "run"]))


class InspectCommandTests(unittest.TestCase):
    def test_export_command_exact(self):
        self.assertEqual(
            worker.export_command(EXE, SID),
            [EXE, "session", "export", "--standalone", SID],
        )

    def test_terminal_command_exact_no_agent_model(self):
        self.assertEqual(
            worker.terminal_command(EXE, ROOT, SID),
            [EXE, ROOT, "--standalone", "--session", SID],
        )

    def test_terminal_rejects_relative_root(self):
        with self.assertRaises(ValueError):
            worker.terminal_command(EXE, "relative/root", SID)


class RequireSessionTests(unittest.TestCase):
    CRASH_SID = "ses_crash_fixture"

    def payload(self, sid=CRASH_SID, root=ROOT, agent="lesson-coordinator"):
        return json.dumps({"data": {"id": sid, "location": {"directory": root}, "agent": agent}})

    def run_export(self, stdout, returncode=0):
        with patch.object(worker.subprocess, "run", return_value=completed(returncode, stdout)) as run:
            return run, worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})

    def test_success_returns_export_and_uses_root_env(self):
        run, exported = self.run_export(self.payload())
        self.assertEqual(exported["data"]["id"], self.CRASH_SID)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [EXE, 'api', '--standalone', 'session.get', '--param', f'sessionID={self.CRASH_SID}'])
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertEqual(kwargs['env']['HOME'], ROOT)
        self.assertEqual(kwargs["timeout"], worker.EXPORT_TIMEOUT)

    def test_saved_database_value_and_unset_override_ambient(self):
        for db in (None, '/saved/db.sqlite'):
            with patch.dict(worker.os.environ, {'OPENCODE_DB': '/wrong/db.sqlite'}), patch.object(worker.subprocess, 'run', return_value=completed(stdout=self.payload())) as run:
                worker.require_session(EXE, self.CRASH_SID, ROOT, {'HOME': ROOT, 'OPENCODE_DB': db})
                actual = run.call_args.kwargs['env']
                self.assertEqual(actual.get('OPENCODE_DB'), db)
                if db is None:
                    self.assertNotIn('OPENCODE_DB', actual)

    def test_export_envelope_is_not_session_get_evidence(self):
        with patch.object(worker.subprocess, 'run', return_value=completed(stdout=json.dumps({'info': {'id': self.CRASH_SID}, 'messages': []}))), self.assertRaisesRegex(ValueError, 'mismatch'):
            worker.require_session(EXE, self.CRASH_SID, ROOT, {'HOME': ROOT})

    def test_missing_session_fails_closed(self):
        with patch.object(worker.subprocess, "run", return_value=completed(1, "", "not found")):
            with self.assertRaisesRegex(ValueError, "no fresh session will launch"):
                worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})

    def test_id_mismatch_fails_closed(self):
        with patch.object(worker.subprocess, "run", return_value=completed(stdout=self.payload(sid="ses_other-session"))):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})

    def test_root_mismatch_fails_closed(self):
        with patch.object(worker.subprocess, "run", return_value=completed(stdout=self.payload(root="/elsewhere"))):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})

    def test_agent_mismatch_fails_closed(self):
        with patch.object(worker.subprocess, "run", return_value=completed(stdout=self.payload(agent="other-agent"))):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})

    def test_flat_legacy_shape_fails_closed(self):
        payload = json.dumps({"id": self.CRASH_SID, "directory": ROOT})
        with patch.object(worker.subprocess, "run", return_value=completed(stdout=payload)):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})

    def test_unrecognized_shape_fails_closed(self):
        with patch.object(worker.subprocess, "run", return_value=completed(stdout="[1,2]")):
            with self.assertRaisesRegex(ValueError, "Unrecognized"):
                worker.require_session(EXE, SID, ROOT, {"HOME": ROOT})
        with patch.object(worker.subprocess, "run", return_value=completed(stdout="not json")):
            with self.assertRaisesRegex(ValueError, "Unrecognized"):
                worker.require_session(EXE, SID, ROOT, {"HOME": ROOT})

    def test_transport_error_fails_closed(self):
        with patch.object(worker.subprocess, "run", side_effect=OSError("down")):
            with self.assertRaisesRegex(ValueError, "no fresh session will launch"):
                worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})

    def test_rejects_bad_sid_before_subprocess(self):
        with patch.object(worker.subprocess, "run", side_effect=AssertionError("must not run")):
            with self.assertRaises(ValueError):
                worker.require_session(EXE, "bad", ROOT, {"HOME": ROOT})

    def test_no_stdout_echoed_in_errors(self):
        secret = "SECRET-TOKEN-ABC123"
        with patch.object(worker.subprocess, "run", return_value=completed(1, secret, secret)):
            with self.assertRaisesRegex(ValueError, "no fresh session will launch") as raised:
                worker.require_session(EXE, self.CRASH_SID, ROOT, {"HOME": ROOT})
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
