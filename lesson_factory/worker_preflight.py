"""Non-model V2 worker preflight: prove inheritance once, then stat-gate claims.

Private `opencode serve` on 127.0.0.1 only. Proves: one active global
Ensemble (package.json name+version 0.19.0, source path matching a GLOBAL
config plugin entry), six project roles (exact mode + Antharas/xhigh +
explicit question deny under last-match-wins), no plugin registration in ANY
project doc, non-mutating Ensemble RPC. No session/model launch, no
queue/config/global mutation. prepare() hashes fully once; guard.check()
compares stat identity (size/mtime/ctime/dev/ino/mode) only. Hashes/counts
only, never raw secrets or entry URLs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import select
import shutil
import signal
import socket
import stat as statmod
import subprocess
import time
import uuid
from fnmatch import fnmatchcase
from pathlib import Path

from .dashboard_supervisor import isolated_environment

ENSEMBLE_ID = "ensemble"
ENSEMBLE_VERSION = "0.19.0"
ENSEMBLE_PACKAGE = "@hueyexe/opencode-ensemble"
EXPECTED_ROLES = {
    "lesson-coordinator": "primary",
    "lesson-assessment-reviewer": "subagent",
    "lesson-format-reviewer": "subagent",
    "lesson-language-expert": "subagent",
    "lesson-pedagogy-reviewer": "subagent",
    "lesson-toolsmith": "subagent",
}
EXPECTED_PROVIDER = "opencode-omniroute"
EXPECTED_MODEL = "Antharas"
EXPECTED_VARIANT = "xhigh"
PREFLIGHT_TIMEOUT_S = 60.0
SERVE_START_TIMEOUT_S = 20.0
API_TIMEOUT_S = 15
MAX_PLUGIN_FILES = 300
MAX_CONFIG_BYTES = 4_000_000
EVIDENCE_DIR = Path("work/v2-migration/preflight")
PROJECT_CONFIGS = ("opencode.json", "opencode.jsonc", ".opencode/opencode.json", ".opencode/opencode.jsonc")
GLOBAL_CONFIGS = ("opencode.json", "opencode.jsonc")

__all__ = ["prepare", "WorkerPreflightGuard"]

_FAIL = "no coordinator launched"


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _must_be_regular(path: Path):
    try:
        st = os.lstat(path)
    except OSError as error:
        raise ValueError(f"Preflight binding unavailable; {_FAIL}") from error
    if statmod.S_ISLNK(st.st_mode):
        raise ValueError(f"Preflight binding is a symlink; {_FAIL}")
    if statmod.S_ISFIFO(st.st_mode):
        raise ValueError(f"Preflight binding is a FIFO; {_FAIL}")
    if not statmod.S_ISREG(st.st_mode):
        raise ValueError(f"Preflight binding is not a file; {_FAIL}")
    return st


def _full_hash(path: Path) -> str:
    _must_be_regular(path)
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _stat_id(path: Path) -> list:
    st = _must_be_regular(path)
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino, st.st_mode]


def _jsonc(root: Path, text: str) -> dict:
    boot = root / "tools" / "bootstrap.py"
    _must_be_regular(boot)
    spec = importlib.util.spec_from_file_location("preflight_bootstrap_jsonc", str(boot))
    if spec is None or spec.loader is None:
        raise ValueError(f"Preflight config bindings unavailable; {_FAIL}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.jsonc(text)


def _load_json(path: Path, root: Path) -> dict:
    if _must_be_regular(path).st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"Preflight config oversize; {_FAIL}")
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = _jsonc(root, text)
        except ValueError as error:
            raise ValueError(f"Preflight config unreadable; {_FAIL}") from error
    if not isinstance(data, dict):
        raise ValueError(f"Preflight config unreadable; {_FAIL}")
    return data


def _parse_agent(path: Path) -> dict:
    _must_be_regular(path)
    parts = Path(path).read_text(encoding="utf-8").split("---")
    if len(parts) < 3:
        raise ValueError(f"Agent frontmatter unavailable; {_FAIL}")
    data: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
    return data


def _deny(perms) -> bool:
    """Ordered last-match-wins evaluation for (action=question, resource=*)."""
    if isinstance(perms, dict):
        items = list(perms.items())
    elif isinstance(perms, list):
        items = []
        for rule in perms:
            if not isinstance(rule, dict):
                continue
            effect = rule.get("effect")
            if effect not in ("allow", "deny", "ask"):
                continue
            items.append(((rule.get("action"), rule.get("resource", "*")), effect))
        items = [((a, r), e) for (a, r), e in items]
    else:
        return False
    effective = None
    for key, effect in items:
        if isinstance(perms, dict):
            try:
                matched = fnmatchcase("question", str(key))
            except Exception:
                continue
            if matched:
                effective = effect
        else:
            action_pat, resource_pat = key
            try:
                if fnmatchcase("question", str(action_pat)) and fnmatchcase("*", str(resource_pat)):
                    effective = effect
            except Exception:
                continue
    return effective == "deny"


def _model_id(model) -> str | None:
    if isinstance(model, str):
        return model.split("/", 1)[1] if "/" in model else model
    if isinstance(model, dict):
        return model.get("model") or model.get("id")
    return None


def _plugin_registrations(info) -> list:
    if not isinstance(info, dict):
        return []
    out: list = []
    for key in ("plugins", "plugin"):
        if key in info and info[key]:
            value = info[key]
            out.extend(value if isinstance(value, list) else [value])
    return out


def _reject_plugin_reg(info) -> None:
    if _plugin_registrations(info):
        raise ValueError(f"Preflight duplicate plugin registration; {_FAIL}")


def _normalize_plugin_entry(entry, base_dir: str) -> str | None:
    """Local-path plugin entry -> resolved absolute path. None for registry/URL/other."""
    if isinstance(entry, dict):
        pkg = entry.get("package")
        if not isinstance(pkg, str):
            return None
        entry = pkg
    if not isinstance(entry, str):
        return None
    text = entry.strip()
    if not text or text.startswith("-") or "://" in text:
        return None
    if text.startswith("file://"):
        text = text[len("file://"):]
    if text.startswith("~"):
        try:
            text = os.path.expanduser(text)
        except Exception:
            return None
    if not (text.startswith("/") or text.startswith(".")):
        return None
    path = Path(text)
    if not path.is_absolute():
        path = Path(base_dir) / text
    try:
        return str(path.resolve())
    except OSError:
        return None


def _check_project_doc(info: dict) -> None:
    if not isinstance(info, dict):
        raise ValueError(f"Preflight project binding drift; {_FAIL}")
    _reject_plugin_reg(info)
    if _model_id(info.get("model")) != EXPECTED_MODEL:
        raise ValueError(f"Preflight project binding drift; {_FAIL}")
    if info.get("default_agent") != "lesson-coordinator":
        raise ValueError(f"Preflight project binding drift; {_FAIL}")
    if not _deny(info.get("permissions", info.get("permission"))):
        raise ValueError(f"Preflight question gate drift; {_FAIL}")


def _require_roles(entries: list) -> None:
    by_id = {str(a.get("id")): a for a in entries if isinstance(a, dict)}
    for name, mode in EXPECTED_ROLES.items():
        entry = by_id.get(name)
        if entry is None:
            raise ValueError(f"Preflight project roles incomplete; {_FAIL}")
        if entry.get("mode") != mode:
            raise ValueError(f"Preflight role binding drift; {_FAIL}")
        model = entry.get("model")
        if (not isinstance(model, dict) or model.get("providerID") != EXPECTED_PROVIDER
                or model.get("id") != EXPECTED_MODEL or model.get("variant") != EXPECTED_VARIANT):
            raise ValueError(f"Preflight role binding drift; {_FAIL}")
        if not _deny(entry.get("permissions")):
            raise ValueError(f"Preflight question gate drift; {_FAIL}")


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _call_api(exe: str, port: int, password: str, op: list[str], data: str | None = None,
              timeout: int = API_TIMEOUT_S, cwd: str | None = None):
    cmd = [exe, "api", "--server", f"http://127.0.0.1:{port}", *op]
    if data is not None:
        cmd += ["-d", data]
    env = {**os.environ, "OPENCODE_PASSWORD": password}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Preflight API unreachable; {_FAIL}") from error
    if result.returncode:
        raise ValueError(f"Preflight API call failed; {_FAIL}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ValueError(f"Unrecognized preflight response; {_FAIL}") from error


def _start_server(exe: str, root: str, env: dict, port: int):
    try:
        return subprocess.Popen(
            [exe, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
    except OSError as error:
        raise ValueError(f"Preflight server did not start; {_FAIL}") from error


def _read_password(proc, deadline: float) -> str:
    out = proc.stdout
    if out is None:
        raise ValueError(f"Preflight server output unavailable; {_FAIL}")
    try:
        fd = out.fileno()
    except (OSError, ValueError, AttributeError):
        fd = None
    nonblock = False
    if fd is not None:
        try:
            os.set_blocking(fd, False)
            nonblock = True
        except OSError:
            nonblock = False
    buf = b""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ValueError(f"Preflight server exited before readiness; {_FAIL}")
        if nonblock:
            assert fd is not None
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as error:
                raise ValueError(f"Preflight server output unreadable; {_FAIL}") from error
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
        else:  # StringIO-style fakes in unit tests
            chunk = out.readline()
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk.encode() if isinstance(chunk, str) else chunk
        for line in buf.decode("utf-8", "replace").splitlines():
            if line.startswith("server password ") and line[16:].strip():
                return line[16:].strip()
    raise ValueError(f"Preflight startup verification timed out; {_FAIL}")


def _stop_server(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        if proc.stdout:
            proc.stdout.close()
    except OSError:
        pass


class WorkerPreflightGuard:
    """Stat-bound admission guard. check() raises ValueError on any drift."""

    def __init__(self, records: dict, db_present: bool, db_hash: str | None, evidence: dict):
        self._records = {k: dict(v) for k, v in records.items()}
        self._db_present = bool(db_present)
        self._db_hash = db_hash
        self._evidence = dict(evidence)

    @property
    def evidence(self) -> dict:
        return dict(self._evidence)

    def check(self) -> None:
        for rec in self._records.values():
            path = Path(rec["path"])
            if not rec["present"]:
                if path.exists() or path.is_symlink():
                    raise ValueError("Preflight shadow binding appeared; manual reconciliation required")
                continue
            try:
                st = os.lstat(path)
            except OSError as error:
                raise ValueError("Preflight binding drift; manual reconciliation required") from error
            if (statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode)
                    or [st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                        st.st_dev, st.st_ino, st.st_mode] != rec["stat"]):
                raise ValueError("Preflight binding drift; manual reconciliation required")
        cur = os.environ.get("OPENCODE_DB")
        cur_present = "OPENCODE_DB" in os.environ and cur is not None
        if bool(cur_present) != self._db_present:
            raise ValueError("Preflight environment drift; manual reconciliation required")
        if cur_present and _sha_text(str(cur)) != self._db_hash:
            raise ValueError("Preflight environment drift; manual reconciliation required")


def prepare(root, exe, environment=None) -> WorkerPreflightGuard:
    """Verify once against an isolated short-lived V2 server; return a hash guard."""
    if not isinstance(root, (str, Path)) or not Path(root).is_absolute():
        raise ValueError("Project root must be an absolute path")
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise ValueError(f"Project root unavailable; {_FAIL}")
    for name in ("lesson_factory", "tools", "opencode.json"):
        if not (root_path / name).exists():
            raise ValueError(f"Project root is not a CherryLexLessons checkout; {_FAIL}")
    agents_dir = root_path / ".opencode" / "agents"
    if not agents_dir.is_dir():
        raise ValueError(f"Project roles unavailable; {_FAIL}")
    if not isinstance(exe, str) or not exe or not Path(exe).is_absolute():
        raise ValueError("Absolute worker path required")
    exe_path = Path(exe)
    if not exe_path.is_file() or not os.access(exe_path, os.X_OK):
        raise ValueError("Executable worker binary required")

    from . import opencode_worker as worker
    base = dict(environment) if isinstance(environment, dict) else dict(os.environ)
    version = worker.verify_binary(str(exe_path))
    exe_real = exe_path.resolve()  # symlink (e.g. ~/.local/bin) resolves to the target
    exe_sha, exe_stat, exe_display = _full_hash(exe_real), _stat_id(exe_real), str(exe_real)

    project_cfg = root_path / "opencode.json"
    _check_project_doc(_load_json(project_cfg, root_path))
    agent_paths = [agents_dir / f"{name}.md" for name in EXPECTED_ROLES]
    for path, (name, mode) in zip(agent_paths, EXPECTED_ROLES.items()):
        front = _parse_agent(path)
        if front.get("mode") != mode:
            raise ValueError(f"Preflight role binding drift; {_FAIL}")
        if front.get("model") != f"{EXPECTED_PROVIDER}/{EXPECTED_MODEL}" or front.get("variant") != EXPECTED_VARIANT:
            raise ValueError(f"Preflight role binding drift; {_FAIL}")
        try:
            deny = _deny(json.loads(front.get("permission", "")))
        except ValueError:
            deny = False
        if not deny:
            raise ValueError(f"Preflight question gate drift; {_FAIL}")
    ensemble_settings = root_path / ".opencode" / "ensemble.json"
    if ensemble_settings.is_file() and _load_json(ensemble_settings, root_path).get("dashboardPort") != 0:
        raise ValueError(f"Preflight dashboard binding drift; {_FAIL}")

    tracked: list[tuple[str, Path]] = [(f"project:{n}", root_path / n) for n in PROJECT_CONFIGS]
    xdg_config = base.get("XDG_CONFIG_HOME")
    if not xdg_config or not Path(xdg_config).is_absolute():
        home = base.get("HOME") or str(Path.home())
        xdg_config = str(Path(home).expanduser().resolve() / ".config")
    tracked += [(f"global:{n}", Path(xdg_config) / "opencode" / n) for n in GLOBAL_CONFIGS]
    tracked.append(("ensemble-settings", ensemble_settings))
    records: dict[str, dict] = {}
    for key, path in tracked:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Preflight binding is not a file; {_FAIL}")
        records[key] = ({"path": str(path), "sha": _full_hash(path), "stat": _stat_id(path), "present": True}
                        if path.is_file() else {"path": str(path), "sha": None, "stat": None, "present": False})
    # Local fail-fast: no project config may register plugins (shadow or primary).
    for key, path in tracked:
        if key.startswith("project:") and records[key]["present"]:
            _reject_plugin_reg(_load_json(Path(records[key]["path"]), root_path))
    records.update({f"agent:{p.stem}": {"path": str(p), "sha": _full_hash(p), "stat": _stat_id(p), "present": True}
                    for p in agent_paths})
    records["exe"] = {"path": exe_display, "sha": exe_sha, "stat": exe_stat, "present": True}
    before = {k: v["sha"] for k, v in records.items() if v["present"]}
    db_present = "OPENCODE_DB" in base and base.get("OPENCODE_DB") is not None
    db_hash = _sha_text(str(base.get("OPENCODE_DB"))) if db_present else None

    evidence_dir = root_path / EVIDENCE_DIR
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        evidence_dir.chmod(0o700)
    except OSError:
        pass
    probe_home = evidence_dir / f"probe-{uuid.uuid4().hex[:12]}-home"
    (probe_home / ".config" / "opencode").mkdir(mode=0o700, parents=True, exist_ok=True)
    probe_env = isolated_environment(str(probe_home.resolve()), dict(base))
    if db_present:
        probe_env["OPENCODE_DB"] = base.get("OPENCODE_DB")
    else:
        probe_env.pop("OPENCODE_DB", None)
    probe_env.pop("OPENCODE_PASSWORD", None)

    # Single total budget covering readiness + every API call + final proof.
    # Cleanup (_stop_server) carries its own bounds outside this deadline.
    deadline = time.monotonic() + PREFLIGHT_TIMEOUT_S
    port = _free_port()
    proc = _start_server(exe_display, str(root_path), probe_env, port)
    try:
        password = _read_password(proc, min(deadline, time.monotonic() + SERVE_START_TIMEOUT_S))
        root_text = str(root_path)
        project_paths = {str(root_path / n) for n in PROJECT_CONFIGS}
        global_paths = {str(Path(xdg_config) / "opencode" / n) for n in GLOBAL_CONFIGS}

        def api(op, data=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"Preflight startup verification timed out; {_FAIL}")
            return _call_api(exe_display, port, password, op, data=data,
                             timeout=min(API_TIMEOUT_S, remaining), cwd=root_text)

        verified: dict = {}
        while True:
            if time.monotonic() >= deadline:
                raise ValueError(f"Preflight startup verification timed out; {_FAIL}")
            try:
                loc = api(["location.get", "--param", f"location[directory]={root_text}"])
                if not isinstance(loc, dict) or loc.get("directory") != root_text:
                    raise ValueError("location")
                plugins = api(["plugin.list", "--param", f"location[directory]={root_text}"])
                data = plugins.get("data", []) if isinstance(plugins, dict) else []
                ensembles = [p for p in data if isinstance(p, dict) and p.get("id") == ENSEMBLE_ID]
                if len(ensembles) != 1:
                    raise ValueError("count")
                ens = ensembles[0]
                if (ens.get("state") or {}).get("status") != "active":
                    raise ValueError("status")
                entry = Path(str((ens.get("source") or {}).get("path", "")))
                if not entry.is_file():
                    raise ValueError("source")
                pkg_data = _load_json(entry.parent / "package.json", root_path)
                if pkg_data.get("name") != ENSEMBLE_PACKAGE or str(pkg_data.get("version")) != ENSEMBLE_VERSION:
                    raise ValueError("version")
                agents = api(["agent.list", "--param", f"location[directory]={root_text}"])
                adata = agents.get("data", []) if isinstance(agents, dict) else []
                _require_roles([a for a in adata if isinstance(a, dict) and str(a.get("id")) in EXPECTED_ROLES])
                cfgs = api(["config.get", "--param", f"location[directory]={root_text}"])
                docs = cfgs if isinstance(cfgs, list) else []  # actual shape: list of {type,path,info}
                for doc in docs:
                    if (isinstance(doc, dict) and str(doc.get("path", "")) in project_paths
                            and isinstance(doc.get("info"), dict)):
                        _reject_plugin_reg(doc["info"])
                proj = [d for d in docs if isinstance(d, dict) and d.get("path") == str(project_cfg)
                        and isinstance(d.get("info"), dict)]
                if len(proj) != 1:
                    raise ValueError("projectdoc")
                _check_project_doc(proj[0]["info"])
                # Loaded ensemble must resolve to a GLOBAL registration (path or
                # {"package": path} object); project shadows were rejected above.
                try:
                    src_resolved = str(entry.resolve())
                except OSError:
                    raise ValueError("source")
                matched = None
                for doc in docs:
                    if not isinstance(doc, dict) or str(doc.get("path", "")) not in global_paths:
                        continue
                    info = doc.get("info")
                    if not isinstance(info, dict):
                        continue
                    base_dir = str(Path(str(doc["path"])).parent)
                    for reg in _plugin_registrations(info):
                        candidate = _normalize_plugin_entry(reg, base_dir)
                        if candidate and (src_resolved == candidate
                                          or str(Path(src_resolved).parent) == candidate):
                            matched = candidate
                            break
                    if matched:
                        break
                if matched is None:
                    raise ValueError(f"Preflight plugin source not globally registered; {_FAIL}")
                rpc = api(["POST", "/api/rpc/ensemble/summary"], data=json.dumps({"input": {"team": "nonexistent"}}))
                if not isinstance(rpc, dict) or (rpc.get("output") or {}).get("text") != 'No team "nonexistent".':
                    raise ValueError("rpc")
                verified = {"total": len(data), "entry": str(entry), "agents": len(adata), "matched": matched}
                break
            except ValueError as error:
                if str(error) not in {"location", "count", "status", "source", "version", "projectdoc", "rpc"}:
                    raise
                if time.monotonic() >= deadline:
                    raise ValueError(f"Preflight startup verification timed out; {_FAIL}") from error
                time.sleep(min(2.0, max(0.2, (deadline - time.monotonic()) / 10)))
        if time.monotonic() >= deadline:
            raise ValueError(f"Preflight startup verification timed out; {_FAIL}")
        for key, sha in before.items():  # drift while the probe ran invalidates the proof
            if _full_hash(Path(records[key]["path"])) != sha:
                raise ValueError(f"Preflight bindings changed during probe; {_FAIL}")
        base_dir = Path(verified["entry"]).parent
        targets = [base_dir / "index.js", base_dir / "package.json"]
        src = base_dir / "src"
        if src.is_dir():
            targets += [p for p in sorted(src.rglob("*")) if p.is_file()]
        if len(targets) > MAX_PLUGIN_FILES:
            raise ValueError(f"Preflight plugin inventory overflow; {_FAIL}")
        for target in targets:
            _must_be_regular(target)
        records.update({f"plugin:{p}": {"path": str(p), "sha": _full_hash(p), "stat": _stat_id(p), "present": True}
                        for p in targets})
        evidence = {
            "root": root_text, "exe_version": version, "exe_sha256": exe_sha,
            "plugin_total": verified["total"], "plugin_version": ENSEMBLE_VERSION,
            "plugin_entry_sha256": _sha_text(verified["matched"]),
            "plugin_files": len(targets), "agent_count": verified["agents"],
            "file_count": len(records), "file_hashes": {k: v["sha"] for k, v in records.items() if v["present"]},
            "db_present": bool(db_present), "db_hash": db_hash, "checked_at": time.time(),
        }
        try:
            out = evidence_dir / f"preflight-{uuid.uuid4().hex[:12]}.json"
            fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(evidence, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise ValueError(f"Preflight startup verification timed out; {_FAIL}")
        return WorkerPreflightGuard(records, bool(db_present), db_hash, evidence)
    finally:
        _stop_server(proc)
        shutil.rmtree(probe_home, ignore_errors=True)
