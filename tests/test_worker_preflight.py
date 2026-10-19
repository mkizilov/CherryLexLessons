"""Unit tests for lesson_factory.worker_preflight. Fake servers/files only.

No real queue, config, model call, or full-suite dependency.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lesson_factory import worker_preflight as preflight

GLOBAL_PLUGIN_PATH = "/fake/global/opencode-ensemble-v0.19.0/index.js"


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


FAKE_HOME = "/fake/orig-home"
FAKE_ENV = {
    "HOME": FAKE_HOME,
    "XDG_CONFIG_HOME": f"{FAKE_HOME}/.config",
    "XDG_DATA_HOME": f"{FAKE_HOME}/.local/share",
    "XDG_STATE_HOME": f"{FAKE_HOME}/.local/state",
    "XDG_CACHE_HOME": f"{FAKE_HOME}/.cache",
    "OPENCODE_DATA_DIR": f"{FAKE_HOME}/.local/share/opencode",
}


def make_root():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name).resolve()
    (root / "lesson_factory").mkdir()
    (root / "tools").mkdir()
    (root / "tools" / "bootstrap.py").write_text("def jsonc(text):\n    import json\n    return json.loads(text)\n")
    (root / "opencode.json").write_text(json.dumps({
        "model": "opencode-omniroute/Antharas",
        "default_agent": "lesson-coordinator",
        "permission": {"*": "allow", "question": "deny"},
    }))
    (root / ".opencode" / "agents").mkdir(parents=True)
    fronts = {
        "lesson-coordinator": "primary",
        "lesson-assessment-reviewer": "subagent",
        "lesson-format-reviewer": "subagent",
        "lesson-language-expert": "subagent",
        "lesson-pedagogy-reviewer": "subagent",
        "lesson-toolsmith": "subagent",
    }
    for name, mode in fronts.items():
        (root / ".opencode" / "agents" / f"{name}.md").write_text(
            "---\n"
            "description: fixture role\n"
            f"mode: {mode}\n"
            "model: opencode-omniroute/Antharas\n"
            "variant: xhigh\n"
            'permission: {"*": "allow", "question": "deny"}\n'
            "---\n\nBody.\n"
        )
    exe = root / "fake-opencode"
    exe.write_bytes(b"fake-binary-bytes")
    exe.chmod(0o700)
    return temp, root, exe


def api_payload(kind, root, plugin_src):
    root = str(root)
    if kind == "location":
        return {"directory": root, "project": {"directory": root, "canonical": root}}
    if kind == "plugin":
        return {"location": {"directory": root}, "data": [{
            "id": "ensemble",
            "source": {"type": "local", "path": str(plugin_src / "index.js")},
            "state": {"status": "active"},
        }]}
    if kind == "agents":
        data = [{"id": name, "mode": mode,
                 "model": {"providerID": "opencode-omniroute", "id": "Antharas", "variant": "xhigh"},
                 "permissions": [{"action": "*", "resource": "*", "effect": "allow"},
                                 {"action": "question", "resource": "*", "effect": "deny"}]}
                for name, mode in preflight.EXPECTED_ROLES.items()]
        return {"location": {"directory": root}, "data": data}
    if kind == "config":
        return [
            {"type": "document", "path": f"{FAKE_HOME}/.config/opencode/opencode.jsonc", "info": {
                "plugins": [str(plugin_src)],
            }},
            {"type": "document", "path": str(Path(root) / "opencode.json"), "info": {
                "model": {"providerID": "opencode-omniroute", "model": "Antharas"},
                "default_agent": "lesson-coordinator",
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"},
                                {"action": "question", "resource": "*", "effect": "deny"}],
            }},
            {"type": "directory", "path": str(Path(root) / ".opencode")},
        ]
    if kind == "rpc":
        return {"output": {"text": 'No team "nonexistent".'}}
    raise AssertionError(kind)


class FakeStdout:
    def __init__(self):
        self._lines = ["server listening\n", "server password pw-for-test\n"]

    def fileno(self):
        raise OSError(9, "Bad file descriptor")

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def close(self):
        pass


class FakeProc:
    def __init__(self):
        self.pid = 424242
        self.stdout = FakeStdout()

    def poll(self):
        return None

    def wait(self, timeout=None):  # noqa: ARG002
        return 0


def fake_serve(exe_path, payload_by_kind):
    plugin_src = Path(exe_path).parent / "opencode-ensemble-v0.19.0"
    src = plugin_src / "src"
    src.mkdir(parents=True, exist_ok=True)
    (plugin_src / "index.js").write_text("entry")
    (plugin_src / "package.json").write_text(json.dumps({"name": "@hueyexe/opencode-ensemble", "version": "0.19.0"}))
    (src / "index.ts").write_text("ensemble")
    calls = {"api": [], "env": []}

    def popen(cmd, **kwargs):  # noqa: ARG001
        return FakeProc()

    def run(cmd, **kwargs):
        calls["api"].append(cmd)
        calls["env"].append(kwargs.get("env"))
        text = " ".join(str(part) for part in cmd)
        if "location.get" in text:
            kind = "location"
        elif "plugin.list" in text:
            kind = "plugin"
        elif "agent.list" in text:
            kind = "agents"
        elif "config.get" in text:
            kind = "config"
        elif "/api/rpc/ensemble/summary" in text:
            kind = "rpc"
        else:
            raise AssertionError(f"unexpected API call: {cmd}")
        if kind not in payload_by_kind:
            raise AssertionError(f"unexpected API kind: {kind}: {cmd}")
        return completed(stdout=json.dumps(payload_by_kind[kind]))

    return popen, run, calls


def ok_responses(root, plugin_src):
    return {k: api_payload(k, root, plugin_src) for k in ("location", "plugin", "agents", "config", "rpc")}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root, self.exe = make_root()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch("lesson_factory.worker_preflight._free_port", return_value=45999).start()
        patch("lesson_factory.worker_preflight._stop_server").start()
        patch.object(preflight, "PREFLIGHT_TIMEOUT_S", 6.0).start()
        patch.object(preflight, "SERVE_START_TIMEOUT_S", 6.0).start()
        patch.object(preflight, "API_TIMEOUT_S", 5).start()

    def run_prepare(self, responses, env=None):
        popen, run, calls = fake_serve(self.exe, responses)
        patch("lesson_factory.worker_preflight._start_server", return_value=FakeProc()).start()
        patch("lesson_factory.worker_preflight.subprocess.Popen", side_effect=popen).start()
        patch("lesson_factory.worker_preflight.subprocess.run", side_effect=run).start()
        with patch("lesson_factory.opencode_worker.verify_binary", return_value="2.0.29"), \
                patch("os.read", side_effect=[b"server listening\n", b"server password pw-for-test\n"]):
            guard = preflight.prepare(str(self.root), str(self.exe), dict(env or FAKE_ENV))
        return guard, calls

    def plugin_src(self):
        return self.root / "opencode-ensemble-v0.19.0"

    def test_success_returns_guard_and_hash_only_evidence(self):
        guard, calls = self.run_prepare(ok_responses(self.root, self.plugin_src()))
        guard.check()
        blob = json.dumps(guard.evidence)
        self.assertNotIn("pw-for-test", blob)
        self.assertEqual(guard.evidence["plugin_version"], "0.19.0")
        self.assertEqual(guard.evidence["plugin_total"], 1)
        self.assertIn("db_present", blob)
        evidence_dir = self.root / "work/v2-migration/preflight"
        self.assertEqual(len(list(evidence_dir.glob("preflight-*.json"))), 1)
        self.assertTrue(all(e and e.get("OPENCODE_PASSWORD") == "pw-for-test" for e in calls["env"]))
        ops = [" ".join(str(c) for c in cmd) for cmd in calls["api"]]
        self.assertTrue(any("location.get" in op for op in ops))
        self.assertTrue(any("plugin.list" in op for op in ops))

    def test_deny_then_allow_rejected_allow_then_deny_passes(self):
        deny = {"action": "question", "resource": "*", "effect": "deny"}
        allow = {"action": "question", "resource": "*", "effect": "allow"}
        self.assertFalse(preflight._deny([deny, allow]))
        self.assertTrue(preflight._deny([allow, deny]))
        self.assertTrue(preflight._deny({"*": "allow", "question": "deny"}))
        self.assertFalse(preflight._deny({"question": "deny", "*": "allow"}))
        # end-to-end: later allow must fail the live check
        payloads = ok_responses(self.root, self.plugin_src())
        entry = dict(payloads["agents"]["data"][0])
        entry["permissions"] = [{"action": "*", "resource": "*", "effect": "allow"}, deny, allow]
        payloads["agents"] = {**payloads["agents"], "data": [entry, *payloads["agents"]["data"][1:]]}
        with self.assertRaisesRegex(ValueError, "question gate"):
            self.run_prepare(payloads)

    def test_duplicate_or_missing_ensemble_fails_closed(self):
        for count in (0, 2):
            payloads = ok_responses(self.root, self.plugin_src())
            payloads["plugin"] = {**payloads["plugin"], "data": list(payloads["plugin"]["data"]) * count}
            with self.assertRaises(ValueError):
                self.run_prepare(payloads)
        with self.assertRaises(ValueError):
            payloads = ok_responses(self.root, self.plugin_src())
            entry = dict(payloads["plugin"]["data"][0])
            entry["state"] = {"status": "failed"}
            payloads["plugin"] = {**payloads["plugin"], "data": [entry]}
            self.run_prepare(payloads)

    def test_role_mode_model_permission_strict(self):
        with self.assertRaises(ValueError):
            payloads = ok_responses(self.root, self.plugin_src())
            payloads["agents"] = {**payloads["agents"], "data": [a for a in payloads["agents"]["data"] if a["id"] != "lesson-toolsmith"]}
            self.run_prepare(payloads)
        with self.assertRaises(ValueError):
            payloads = ok_responses(self.root, self.plugin_src())
            entry = dict(payloads["agents"]["data"][0])
            entry["model"] = {"providerID": "opencode-omniroute", "id": "Antharas"}
            payloads["agents"] = {**payloads["agents"], "data": [entry, *payloads["agents"]["data"][1:]]}
            self.run_prepare(payloads)
        with self.assertRaises(ValueError):
            payloads = ok_responses(self.root, self.plugin_src())
            info = dict(payloads["config"][1]["info"])
            info["permissions"] = [{"action": "*", "resource": "*", "effect": "allow"}]
            payloads["config"] = [payloads["config"][0], {**payloads["config"][1], "info": info}, payloads["config"][2]]
            self.run_prepare(payloads)

    def test_project_plugin_and_shadow_config_refused(self):
        (self.root / "opencode.json").write_text(json.dumps({
            "model": "opencode-omniroute/Antharas",
            "plugin": ["./plugins/local"],
            "permission": {"*": "allow", "question": "deny"},
        }))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_prepare(ok_responses(self.root, self.plugin_src()))
        (self.root / "opencode.json").write_text(json.dumps({
            "model": "opencode-omniroute/Antharas",
            "default_agent": "lesson-coordinator",
            "permission": {"*": "allow", "question": "deny"},
        }))
        guard, _ = self.run_prepare(ok_responses(self.root, self.plugin_src()))
        (self.root / ".opencode" / "opencode.json").write_text(json.dumps({"plugins": ["./shadow"]}))
        with self.assertRaisesRegex(ValueError, "shadow"):
            guard.check()

    def test_live_shadow_doc_rejected_even_when_primary_clean(self):
        payloads = ok_responses(self.root, self.plugin_src())
        payloads["config"] = [*payloads["config"],
                              {"type": "document", "path": str(self.root / ".opencode" / "opencode.json"),
                               "info": {"plugins": ["./shadow-ensemble"]}}]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_prepare(payloads)

    def test_global_moved_to_project_shadow_rejected(self):
        payloads = ok_responses(self.root, self.plugin_src())
        global_doc = dict(payloads["config"][0])
        global_doc["info"] = {}
        shadow = {"type": "document", "path": str(self.root / ".opencode" / "opencode.json"),
                  "info": {"plugins": [str(self.plugin_src())]}}
        payloads["config"] = [global_doc, payloads["config"][1], payloads["config"][2], shadow]
        with self.assertRaisesRegex(ValueError, "globally registered|duplicate"):
            self.run_prepare(payloads)

    def test_total_deadline_bounds_slow_readiness(self):
        # Never-ready plugin forces the poll loop to burn budget: 20s slow
        # readiness + slow API calls must fail inside ONE 60s total deadline,
        # not 20 + 5x14 = 90s. Fake clock only advances via readiness/calls.
        payloads = ok_responses(self.root, self.plugin_src())
        entry = dict(payloads["plugin"]["data"][0])
        entry["state"] = {"status": "starting"}
        payloads["plugin"] = {**payloads["plugin"], "data": [entry]}
        popen, run, calls = fake_serve(self.exe, payloads)
        now = [1000.0]
        timeouts: list = []
        pw_deadlines: list = []

        def slow_run(cmd, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            now[0] += kwargs.get("timeout", 14)  # call consumes its allotment
            return run(cmd, **kwargs)

        def slow_password(proc, deadline):
            pw_deadlines.append(deadline)
            now[0] += 20.0  # slow readiness burns budget too
            return "pw"

        patch("lesson_factory.worker_preflight._start_server", return_value=FakeProc()).start()
        patch("lesson_factory.worker_preflight.subprocess.run", side_effect=slow_run).start()
        with patch("lesson_factory.opencode_worker.verify_binary", return_value="2.0.29"), \
                patch.object(preflight.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(preflight.time, "sleep", side_effect=lambda s: now.__setitem__(0, now[0] + s)), \
                patch("lesson_factory.worker_preflight._read_password", side_effect=slow_password), \
                patch.object(preflight, "PREFLIGHT_TIMEOUT_S", 60.0), \
                patch.object(preflight, "API_TIMEOUT_S", 14):
            start = now[0]
            with self.assertRaisesRegex(ValueError, "timed out"):
                preflight.prepare(str(self.root), str(self.exe), dict(FAKE_ENV))
            elapsed = now[0] - start
        # Deadline was fixed before server start, not after 20s readiness.
        self.assertLessEqual(pw_deadlines[0] - start, 60.0)
        # Total bounded by the single deadline, not 90s of stacked timeouts.
        self.assertLessEqual(elapsed, 70.0, f"single deadline exceeded: {elapsed}")
        self.assertTrue(timeouts)
        self.assertTrue(all(t is not None and t <= 14 for t in timeouts))

    def test_final_hashing_cannot_return_success_after_deadline(self):
        now = [1000.0]
        final = [False]
        original = preflight._full_hash
        def hashing(path):
            if final[0]:
                now[0] += 1.0
            return original(path)
        responses = ok_responses(self.root, self.plugin_src())
        _, run, _ = fake_serve(self.exe, responses)
        def api_run(cmd, **kwargs):
            if '/api/rpc/ensemble/summary' in cmd:
                now[0] = 1059.5
                final[0] = True
            return run(cmd, **kwargs)
        with patch.object(preflight.time, 'monotonic', side_effect=lambda: now[0]), patch.object(preflight, '_full_hash', side_effect=hashing), patch.object(preflight, '_start_server', return_value=FakeProc()), patch.object(preflight, '_read_password', return_value='pw'), patch.object(preflight.subprocess, 'run', side_effect=api_run), patch('lesson_factory.opencode_worker.verify_binary', return_value='2.0.29'), patch.object(preflight, 'PREFLIGHT_TIMEOUT_S', 60.0):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                preflight.prepare(str(self.root), str(self.exe), dict(FAKE_ENV))

    def test_stat_identity_covers_atomic_replace(self):
        fields = preflight._stat_id(self.exe)
        self.assertEqual(len(fields), 6)  # size, mtime, ctime, dev, ino, mode

    def test_stat_identity_cheap_and_binary_no_rehash(self):
        guard, _ = self.run_prepare(ok_responses(self.root, self.plugin_src()))
        with patch("lesson_factory.worker_preflight._full_hash", side_effect=AssertionError("must not rehash")):
            guard.check()
        self.exe.write_bytes(b"changed-binary-bytes")
        with self.assertRaisesRegex(ValueError, "drift"):
            guard.check()

    def test_env_db_presence_change_breaks_guard(self):
        env = dict(FAKE_ENV)
        env["OPENCODE_DB"] = "/fake/db.sqlite"
        guard, _ = self.run_prepare(ok_responses(self.root, self.plugin_src()), env=env)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "environment drift"):
                guard.check()
        with patch.dict(os.environ, {"OPENCODE_DB": "/fake/db.sqlite"}, clear=True):
            guard.check()

    def test_no_secret_in_errors(self):
        secret = "SECRET-PW-ABC123"
        env = dict(FAKE_ENV)
        env["OPENCODE_DB"] = secret
        try:
            self.run_prepare(ok_responses(self.root, self.plugin_src()), env=env)
        except ValueError as error:
            self.fail(f"unexpected failure: {error}")
        with patch.dict(os.environ, {"OPENCODE_DB": "other-value"}):
            with self.assertRaises(ValueError) as raised:
                preflight.WorkerPreflightGuard(
                    {}, True, preflight._sha_text(secret), {},
                ).check()
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
