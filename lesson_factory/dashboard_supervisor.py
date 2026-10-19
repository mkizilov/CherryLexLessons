import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import termios
import tty
import subprocess
import sys
import tempfile
import time

import psutil


SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]{1,160}\Z")
EVENT_TYPES = {"step_start", "step_finish", "text", "tool_use", "error"}


def checked_path(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Unsafe dashboard path")
    return path


def private_directory(path):
    path = checked_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Unsafe dashboard directory")
    path.chmod(0o700)
    return path


def private_open(path, flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND):
    fd = os.open(checked_path(path), flags | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def write_json(path, value):
    path = checked_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    path = checked_path(path)
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def fingerprint(command):
    return hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def boot_identity():
    if sys.platform.startswith("linux"):
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            pass
    return f"{sys.platform}:{psutil.boot_time():.6f}"


def process_identity(pid):
    process = psutil.Process(pid)
    with process.oneshot():
        return {"pid": pid, "create_time": process.create_time(), "boot_id": boot_identity(), "command_fingerprint": fingerprint(process.cmdline())}


def process_state(identity):
    if not isinstance(identity, dict) or not all(key in identity for key in ("pid", "create_time", "boot_id", "command_fingerprint")):
        return "unknown"
    if identity["boot_id"] != boot_identity():
        return "dead"
    try:
        process = psutil.Process(identity["pid"])
        with process.oneshot():
            if process.create_time() != identity["create_time"]:
                return "dead"
            if process.status() == psutil.STATUS_ZOMBIE:
                return "dead"
            if fingerprint(process.cmdline()) != identity["command_fingerprint"]:
                return "unknown"
        return "alive"
    except psutil.NoSuchProcess:
        return "dead"
    except (psutil.AccessDenied, OSError, ValueError, TypeError):
        return "unknown"


def group_state(identity, exclude_pid=None):
    if not isinstance(identity, dict) or not isinstance(identity.get("pid"), int):
        return "unknown"
    if identity.get("boot_id") != boot_identity():
        return "dead"
    denied = False
    for process in psutil.process_iter():
        if process.pid == exclude_pid:
            continue
        try:
            os.kill(process.pid, 0)
        except PermissionError:
            continue
        except ProcessLookupError:
            continue
        try:
            if process.uids().real != os.getuid() or process.create_time() < identity.get("create_time", 0):
                continue
            if os.getpgid(process.pid) == identity["pid"] and process.status() != psutil.STATUS_ZOMBIE:
                return "alive"
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied):
            denied = True
    return "unknown" if denied else "dead"


def signal_group(identity, sig):
    if process_state(identity) != "alive":
        return False
    try:
        if os.getpgid(identity["pid"]) != identity["pid"]:
            return False
        if process_state(identity) != "alive":
            return False
        os.killpg(identity["pid"], sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# V2 ownership tracking (env-inherited, cross-PGID).
#
# Offline smoke (work/v2-migration/smoke/REPORT.md) proved `run --standalone`
# cleanup containment by wrapper PGID is unsafe: the CLI, its private server
# (`opencode serve --stdio --port 0`) and its foreground tool each establish a
# DIFFERENT process group. Graceful SIGTERM happened to clean them; an abrupt
# CLI kill leaves the server/tool orphaned outside the wrapper PGID while
# group_state() reports "dead" (false group_stopped).
#
# These helpers track the unique inherited CHERRYLEX_DASHBOARD_JOB +
# CHERRYLEX_DASHBOARD_ATTEMPT processes across groups and PID reuse: same UID,
# born at/after the CLI spawn floor, exact marker match. Only the two marker
# values are compared from the OS environment snapshot; unrelated environment
# values, secret config and printenv output are never logged or persisted. Termination signals exact verified PIDs
# only (never killpg/kill-by-name, never PID 1/self, never a global server).
# Discovered identities are persisted to process/exit metadata so the caller
# (Runner._runtime, lead-owned) can gate quiescence on them:
#   quiescent <=> wrapper done + wrapper PGID dead + ownership_quiescent()[0]
# where ownership_quiescent() is True only when every owned process is dead
# AND ownership is fully proven (no scan/liveness uncertainty).

OWNED_JOB_ENV = "CHERRYLEX_DASHBOARD_JOB"
OWNED_ATTEMPT_ENV = "CHERRYLEX_DASHBOARD_ATTEMPT"
OWNED_SCAN_CAP = 256
OWNED_RESCAN_INTERVAL = 1.0
WRAPPER_OWNED_TERM_WAIT = 3.0
WRAPPER_OWNED_KILL_WAIT = 2.0


def _owned_markers_match(process, job_id, attempt_id):
    """Compare only the two ownership markers. True/False, or None if vanished."""
    try:
        environment = process.environ()
    except (psutil.NoSuchProcess, ProcessLookupError):
        return None
    if not isinstance(environment, dict):
        return None
    if environment.get(OWNED_JOB_ENV) != job_id or environment.get(OWNED_ATTEMPT_ENV) != attempt_id:
        return False
    return True


def _owned_candidate_usable(process, floor):
    """Birth + UID gate, checked before any signal probe.

    Returns True only for a proven same-UID process born at/after floor.
    Returns False for proven-foreign or proven-predating processes (cheap
    exclude: preflight probes, prior attempts, system daemons, the global
    server). Returns None when ownership cannot be proven either way.
    A signal EPERM (PermissionError) means foreign: it cannot have inherited
    our markers, so it excludes exactly like group_state() does — never
    uncertainty, never a blind record for preexisting/system processes.
    """
    try:
        birth = process.create_time()
    except (psutil.NoSuchProcess, ProcessLookupError):
        return False
    except (psutil.AccessDenied, PermissionError, OSError, ValueError, TypeError):
        birth = None
    if birth is not None and birth < floor:
        return False  # pre-CLI birth: preflight probe, prior attempt, system
    try:
        same_user = process.uids().real == os.getuid()
    except (psutil.NoSuchProcess, ProcessLookupError):
        return False
    except (psutil.AccessDenied, PermissionError, OSError, ValueError, TypeError):
        same_user = None
    if same_user is False:
        return False
    if same_user is True and birth is not None:
        return True
    # Birth or UID unreadable: fall back to the signal discriminator.
    try:
        os.kill(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # EPERM: foreign UID, consistent with group_state()
    except OSError:
        return None
    return None if birth is None or same_user is None else True


def discover_owned(job_id, attempt_id, floor_time, exclude_pids=()):
    """Bounded scan for live processes that inherited this attempt's markers.

    Returns (owned, uncertain): owned is a capped list of process identities,
    uncertain is True only when a proven same-UID post-floor candidate could
    not be fully verified (unreadable status/environ/identity) or the
    persistence cap was hit. Birth time and UID are proven BEFORE any signal
    probe: pre-CLI births (preflight probes, prior attempts, system daemons,
    the global server) and foreign UIDs (signal EPERM, consistent with
    group_state()) exclude cheaply — they never hold the slot and never
    count as uncertainty. Only the two marker values are compared; full
    environment values other than the two markers are never persisted or logged.
    """
    if not isinstance(job_id, str) or not job_id or not isinstance(attempt_id, str) or not attempt_id:
        return [], True
    try:
        floor = float(floor_time)
    except (TypeError, ValueError):
        return [], True
    excluded = {os.getpid(), 1}
    for pid in exclude_pids or ():
        try:
            excluded.add(int(pid))
        except (TypeError, ValueError):
            continue
    owned = []
    uncertain = False
    try:
        iterator = psutil.process_iter()
    except Exception:
        return [], True
    for process in iterator:
        if process.pid in excluded:
            continue
        usable = _owned_candidate_usable(process, floor)
        if usable is not True:
            if usable is None:
                uncertain = True
            continue
        try:
            zombie = process.status() == psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError, OSError, ValueError, TypeError):
            uncertain = True
            continue
        if zombie:
            continue
        try:
            match = _owned_markers_match(process, job_id, attempt_id)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError, OSError, ValueError, TypeError):
            uncertain = True
            continue
        if match is not True:
            continue  # False: unrelated same-UID process; None: vanished race
        try:
            owned.append(process_identity(process.pid))
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError, OSError, ValueError, TypeError):
            uncertain = True
            continue
        if len(owned) >= OWNED_SCAN_CAP:
            uncertain = True
            break
    return owned, uncertain


def classify_owned(owned, job_id=None, attempt_id=None):
    """Split persisted owned identities into (alive, uncertain).

    Re-verifies exact identity (PID-reuse safe via create_time/boot/cmd) and,
    when job/attempt are given, re-checks the live environ markers. Never
    signals. A live PID whose occupant no longer carries our markers is not
    ours: it is left alone and reported as uncertainty, never as owned-alive.
    """
    alive = []
    uncertain = False
    for identity in owned or ():
        try:
            state = process_state(identity)
        except Exception:
            uncertain = True
            continue
        if state == "dead":
            continue
        if state != "alive":
            uncertain = True
            continue
        if job_id is not None and attempt_id is not None:
            try:
                marker = _owned_markers_match(psutil.Process(identity["pid"]), job_id, attempt_id)
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
            except (psutil.AccessDenied, PermissionError, OSError, ValueError, TypeError):
                uncertain = True
                continue
            if marker is not True:
                uncertain = True
                continue
        alive.append(identity)
    return alive, uncertain


def signal_owned(owned, sig, job_id=None, attempt_id=None):
    """Signal only verifiably owned PIDs by exact identity. Returns count.

    Re-verifies liveness plus markers immediately before each kill
    (PID-reuse guard). Never uses killpg, never signals PID 1 or self, never
    kills on command-name match, never touches the global service.
    """
    signaled = 0
    for identity in owned or ():
        if process_state(identity) != "alive":
            continue
        pid = identity.get("pid")
        if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
            continue
        if job_id is not None and attempt_id is not None:
            try:
                if _owned_markers_match(psutil.Process(pid), job_id, attempt_id) is not True:
                    continue
            except (psutil.NoSuchProcess, ProcessLookupError, psutil.AccessDenied, PermissionError, OSError, ValueError, TypeError):
                continue
        try:
            os.kill(pid, sig)
            signaled += 1
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return signaled


def merge_owned(previous, discovered):
    """Bound live/uncertain identities, not the lifetime number of tool processes."""
    retained = [identity for identity in previous if process_state(identity) != 'dead']
    by_identity = {(item['pid'], item.get('create_time'), item.get('boot_id')): item for item in [*retained, *discovered]}
    merged = sorted(by_identity.values(), key=lambda item: (item['pid'], item.get('create_time', 0)))
    return merged[:OWNED_SCAN_CAP], len(merged) > OWNED_SCAN_CAP


def ownership_quiescent(job_id, attempt_id, floor_time, exclude_pids=()):
    """Authoritative stop gate. True only if every owned process is dead and
    ownership is fully proven. Returns (quiescent, owned, uncertain)."""
    owned, scan_uncertain = discover_owned(job_id, attempt_id, floor_time, exclude_pids)
    alive, live_uncertain = classify_owned(owned, job_id, attempt_id)
    uncertain = bool(scan_uncertain or live_uncertain)
    return (not alive and not uncertain, owned, uncertain)


def _reap_owned_after_cli(job_id, attempt_id, floor_time):
    """Best-effort bounded cleanup of env-tracked survivors after the CLI
    exited (e.g. a private --standalone server or foreground tool in its own
    PGID orphaned by an abrupt CLI kill). The supervisor ownership gate stays
    authoritative; this only avoids leaving known-owned processes behind."""
    owned, _ = discover_owned(job_id, attempt_id, floor_time)
    if not owned:
        return
    signal_owned(owned, signal.SIGTERM, job_id, attempt_id)
    deadline = time.monotonic() + WRAPPER_OWNED_TERM_WAIT
    while time.monotonic() < deadline:
        owned, _ = discover_owned(job_id, attempt_id, floor_time)
        alive, _ = classify_owned(owned, job_id, attempt_id)
        if not alive:
            return
        time.sleep(0.1)
    owned, _ = discover_owned(job_id, attempt_id, floor_time)
    signal_owned(owned, signal.SIGKILL, job_id, attempt_id)
    deadline = time.monotonic() + WRAPPER_OWNED_KILL_WAIT
    while time.monotonic() < deadline:
        owned, _ = discover_owned(job_id, attempt_id, floor_time)
        alive, _ = classify_owned(owned, job_id, attempt_id)
        if not alive:
            return
        time.sleep(0.1)


def isolated_environment(job_home, inherited=None):
    environment = dict(os.environ if inherited is None else inherited)
    original_home = Path(environment.get("HOME") or Path.home()).expanduser().resolve()
    for name, suffix in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        value = environment.get(name)
        if value:
            path = Path(value)
            if not path.is_absolute():
                raise ValueError("XDG paths must be absolute before isolation")
            environment[name] = str(path)
        else:
            environment[name] = str(original_home / suffix)
    provider_data = environment.get("OPENCODE_DATA_DIR")
    if provider_data and not Path(provider_data).is_absolute():
        raise ValueError("OpenCode provider data path must be absolute before isolation")
    environment["OPENCODE_DATA_DIR"] = provider_data or str(original_home / ".local/share/opencode")
    environment["HOME"] = str(Path(job_home).resolve())
    return environment


ENVIRONMENT_PATHS = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "OPENCODE_DATA_DIR")

OPENCODE_DB_ENV = "OPENCODE_DB"


def _validated_db_value(value):
    """Shared OPENCODE_DB shape check: absolute secret-opaque path string."""
    if not isinstance(value, str) or not value or not Path(value).is_absolute() or any(ord(char) < 32 for char in value):
        raise ValueError("Saved OpenCode DB identity invalid; refusing environment loss")
    return value


def saved_environment(environment, job_home):
    if not isinstance(environment, dict) or any(not isinstance(environment.get(key), str) or not Path(environment[key]).is_absolute() or any(ord(char) < 32 for char in environment[key]) for key in ENVIRONMENT_PATHS):
        raise ValueError("Exact original job HOME/XDG/OpenCode environment unavailable; refusing environment loss")
    if environment["HOME"] != str(Path(job_home).resolve()):
        raise ValueError("Saved job HOME identity mismatch")
    return {key: environment[key] for key in ENVIRONMENT_PATHS}


def saved_job_environment(environment, job_home):
    """New-job capture: required 6 paths plus explicit OPENCODE_DB scoping.

    Returns {**saved_environment(...), "OPENCODE_DB": str | None} where None
    records that the variable was unset at capture time (unset vs value is
    preserved). Old 6-key records are never rewritten here: use
    saved_environment() to revalidate those, which keeps their shape and
    fingerprints byte-identical. No old job env is force-upgraded.
    """
    saved = saved_environment(environment, job_home)
    db_value = environment.get(OPENCODE_DB_ENV)
    if db_value is not None:
        _validated_db_value(db_value)
    return {**saved, OPENCODE_DB_ENV: db_value}


def apply_saved_environment(base, saved, job_home):
    """Merge a spec-saved env into a launch environ. Returns a meaningful flag.

    - "set": new record carried an OPENCODE_DB string; it overrides ambient.
    - "cleared": new record carried None; ambient OPENCODE_DB is popped so a
      changed ambient DB can never leak into the job (require_session and
      probe spawns must honor this too: never pass None into subprocess env).
    - "ambient": old 6-key record without the DB key; ambient is left exactly
      as before (no forced upgrade, old behavior preserved).

    New records (key present, even None) take priority over ambient; old
    records (key absent) defer to ambient. Raises ValueError before mutating
    base when the saved record is invalid, so a failed apply never leaves a
    half-merged or None-valued environ (subprocess env values must be str).
    """
    if not isinstance(base, dict) or not isinstance(saved, dict):
        raise ValueError("Exact original job environment unavailable; refusing environment loss")
    if OPENCODE_DB_ENV in saved and saved[OPENCODE_DB_ENV] is not None:
        _validated_db_value(saved[OPENCODE_DB_ENV])
    validated = saved_environment(saved, job_home)
    base.update(validated)
    if OPENCODE_DB_ENV not in saved:
        return "ambient"
    if saved[OPENCODE_DB_ENV] is None:
        base.pop(OPENCODE_DB_ENV, None)
        return "cleared"
    base[OPENCODE_DB_ENV] = saved[OPENCODE_DB_ENV]
    return "set"


class EventReader:
    def __init__(self, path, session_id=None):
        self.path = path
        self.offset = 0
        self.partial = b""
        self.dropping = False
        self.session_id = session_id
        self.conflict = False

    def poll(self):
        with checked_path(self.path).open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read(262144)
            self.offset = handle.tell()
        if not chunk:
            return False
        for part in chunk.splitlines(keepends=True):
            complete = part.endswith(b"\n")
            if not self.dropping:
                self.partial += part
                if len(self.partial) > 2097152:
                    self.partial = b""
                    self.dropping = True
            if not complete:
                continue
            if not self.dropping:
                self._event(self.partial)
            self.partial = b""
            self.dropping = False
        return True

    def _event(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict) or event.get("type") not in EVENT_TYPES:
            return
        value = event.get("sessionID")
        if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
            return
        if self.session_id is None:
            self.session_id = value
        elif self.session_id != value:
            self.conflict = True


RESOURCE_PRESSURE = "Service temporarily unavailable due to resource pressure. Retry shortly."
UPSTREAM_INTERRUPTIONS = {"Upstream response stream was interrupted": "upstream_stream_interrupted", "Upstream HTTP/2 stream failed": "upstream_http2_stream_failed"}
PRESSURE_TARGET = r"[A-Za-z0-9_.:/-]{1,120}: provider \u2014 " + re.escape(RESOURCE_PRESSURE) + r" \(HTTP 503\)"
PRESSURE_SUMMARY = re.compile(r"(?:" + re.escape(RESOURCE_PRESSURE) + r"|" + PRESSURE_TARGET + r"(?:; " + PRESSURE_TARGET + r"){0,15}) \(reset after \d{1,4}s\)\Z")


def transient_signature(event):
    """Exact allowlist of observed transient transport errors; anything else needs attention."""
    if not isinstance(event, dict) or set(event) != {"type", "timestamp", "sessionID", "error"} or event["type"] != "error":
        return None
    error = event["error"]
    if not isinstance(error, dict) or set(error) != {"name", "data"} or not isinstance(error["data"], dict):
        return None
    data = error["data"]
    if error["name"] == "UnknownError":
        if set(data) != {"message"} or not isinstance(data["message"], str):
            return None
        try:
            body = json.loads(data["message"])
        except ValueError:
            return None
        if body == {"message": body.get("message"), "type": "upstream_error", "code": "upstream_error"}:
            return UPSTREAM_INTERRUPTIONS.get(body["message"])
        if isinstance(body, dict) and set(body) == {"message"} and isinstance(body["message"], str) and PRESSURE_SUMMARY.fullmatch(body["message"]):
            return "resource_pressure"
        return None
    if error["name"] != "APIError" or data.get("isRetryable") is not True:
        return None
    message, metadata = data.get("message"), data.get("metadata")
    if set(data) == {"message", "isRetryable", "metadata"} and message == "SSE read timed out" and metadata == {"code": "ProviderResponseStreamError"}:
        return "sse_read_timeout"
    if set(data) == {"message", "isRetryable", "metadata"} and message == "Cannot connect to API: Unable to connect. Is the computer able to access the url?" and isinstance(metadata, dict) and set(metadata) == {"url"}:
        return "provider_unreachable"
    if set(data) == {"message", "isRetryable", "metadata", "statusCode", "responseHeaders", "responseBody"} and message == RESOURCE_PRESSURE and data["statusCode"] == 503 and isinstance(data["responseBody"], str):
        try:
            body = json.loads(data["responseBody"])
        except ValueError:
            return None
        if body == {"error": {"message": RESOURCE_PRESSURE, "type": "server_error", "code": "resource_pressure"}}:
            return "resource_pressure_503"
    return None


def terminal_transient_error(path, session_id):
    """Return (signature, sha256 of the final event line) when the stream ends with an allowlisted error of the saved session."""
    path = checked_path(path)
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - 262144))
        tail = handle.read()
    if not tail.endswith(b"\n"):
        return None
    lines = tail[:-1].rsplit(b"\n", 1)
    if len(lines) == 1 and size > len(tail):
        return None
    line = lines[-1]
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        return None
    signature = transient_signature(event)
    if signature is None or not isinstance(session_id, str) or event.get("sessionID") != session_id:
        return None
    return signature, hashlib.sha256(line).hexdigest()


def coordinator_group(spec_path, gate_fd, lock_fd):
    if os.read(gate_fd, 1) != b"1":
        return 125
    os.close(gate_fd)
    spec_path = Path(spec_path)
    if (spec_path.parent / "stop.json").exists():
        return 125
    spec = read_json(spec_path)
    external = spec.get("mode") == "external"
    if external:
        # Claim the foreground terminal BEFORE any preflight subprocess runs:
        # a child spawned first could otherwise acquire the controlling
        # terminal instead (observed as the external fake exiting 1 silent).
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    signal.signal(signal.SIGTERM, lambda _number, _frame: None)
    if external:
        signal.signal(signal.SIGINT, lambda _number, _frame: None)
    if '--standalone' in spec.get('command', []):
        # Recheck just before execution, including delayed manual terminal handoffs.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from lesson_factory import opencode_worker as worker
        worker.verify_binary(spec['command'][0])
        if spec.get('session_id'):
            # os.environ is os._Environ, not a dict; require_session rejects
            # non-dict environments, so pass a real snapshot here.
            worker.require_session(spec['command'][0], spec['session_id'], Path(spec['root']), dict(os.environ))
        if (spec_path.parent / 'stop.json').exists():
            return 125
    identity = process_identity(os.getpid())
    # Ownership floor: preflight probe children (verify/require above) are
    # already reaped here, and anything born before this instant cannot be a
    # CLI descendant. Cross-group owned survivors are birth-dated against it.
    floor_time = time.time()
    child = subprocess.Popen(spec["command"], cwd=spec["root"], stdin=None if external else subprocess.DEVNULL, pass_fds=(lock_fd,))
    code = child.wait()
    if code is not None and code < 0:
        # Abrupt signal death skips CLI lifecycle cleanup: bound the
        # cross-group owned survivors (private --standalone server, tools).
        # Normal exits keep descendant-hold semantics for the caller to stop.
        _reap_owned_after_cli(spec.get("job_id"), spec.get("attempt_id"), floor_time)
    while group_state(identity, exclude_pid=os.getpid()) != "dead":
        time.sleep(0.1)
    return code


def supervise(spec_path, require_external=False):
    spec_path = checked_path(spec_path)
    spec = read_json(spec_path)
    attempt_dir = spec_path.parent
    job_dir = attempt_dir.parent.parent
    expected = Path(spec["root"]) / "work/dashboard/jobs" / spec["job_id"] / "attempts" / spec["attempt_id"]
    if attempt_dir != expected.resolve() or spec_path.name != "spec.json":
        raise ValueError("Supervisor journal identity mismatch")
    lock_fd = private_open(job_dir / "job.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return 2
    if (attempt_dir / "process.json").exists() or (attempt_dir / "exit.json").exists():
        os.close(lock_fd)
        return 2
    external = spec.get("mode") == "external"
    if checked_path(Path(spec["root"]) / "work/dashboard/queue.sqlite").exists():
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from lesson_factory.dashboard_store import Store
            job = Store(spec["root"]).get_job(spec["job_id"])
            if job["attempt_id"] != spec["attempt_id"] or job["state"] not in ("launching", "running") or job["launch_metadata"].get("spec_sha256") != fingerprint(spec) or (job["launch_metadata"].get("mode") == "external") != external:
                raise ValueError("Supervisor reservation or specification identity mismatch")
        except BaseException:
            os.close(lock_fd)
            raise
    if require_external and not external:
        os.close(lock_fd)
        raise ValueError("External reservation cannot change execution mode")
    if external:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from lesson_factory.dashboard_store import Store
            job = Store(spec["root"]).get_job(spec["job_id"])
            if job["attempt_id"] != spec["attempt_id"] or job["state"] not in ("launching", "running") or job["launch_metadata"].get("mode") != "external" or job["launch_metadata"].get("spec_sha256") != fingerprint(spec) or spec.get("session_id") != job["session_id"] or not SESSION_ID.fullmatch(spec.get("session_id", "")):
                raise ValueError("External handoff identity is no longer reserved")
            saved_environment(spec.get("environment"), job_dir / "home")
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError("External recovery requires a foreground terminal")
        except BaseException:
            os.close(lock_fd)
            raise
    child = None
    master_fd = slave_fd = None
    terminal_state = None
    state = {"job_id": spec["job_id"], "attempt_id": spec["attempt_id"], "spec_sha256": fingerprint(spec), "supervisor": process_identity(os.getpid()), "phase": "prepared", "session_id": spec.get("session_id"), "owned": [], "ownership_uncertain": False, "ownership_floor": None}
    journal = attempt_dir / "process.json"
    write_json(journal, state)
    stdout_fd = private_open(attempt_dir / "events.jsonl")
    stderr_fd = private_open(attempt_dir / "stderr.log")
    try:
        if (attempt_dir / "stop.json").exists():
            state["phase"] = "stopped_before_launch"
            write_json(journal, state)
            write_json(attempt_dir / "exit.json", {**state, "exit_code": None, "stopped": True, "group_stopped": True, "ended_at": time.time()})
            return 0
        state["phase"] = "spawning"
        write_json(journal, state)
        environment = isolated_environment(job_dir / "home")
        if "environment" in spec:
            # None-valued DB means recorded-unset: never env.update(None)
            # (invalid subprocess env); pop ambient instead. Old 6-key
            # records keep old ambient-deferring behavior.
            apply_saved_environment(environment, spec["environment"], job_dir / "home")
        environment.update({"CHERRYLEX_DASHBOARD_JOB": spec["job_id"], "CHERRYLEX_DASHBOARD_ATTEMPT": spec["attempt_id"]})
        if external:
            master_fd, slave_fd = os.openpty()
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\x00" * 8))
            terminal_state = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno(), termios.TCSANOW)
        gate_read, gate_write = os.pipe()
        try:
            command = [sys.executable, str(Path(__file__).resolve()), "--spec", str(spec_path), "--child", "--gate-fd", str(gate_read), "--lock-fd", str(lock_fd)]
            child = subprocess.Popen(command, cwd=spec["root"], env=environment, stdin=slave_fd if external else subprocess.DEVNULL, stdout=slave_fd if external else stdout_fd, stderr=slave_fd if external else stderr_fd, start_new_session=True, pass_fds=(lock_fd, gate_read))
            if slave_fd is not None:
                os.close(slave_fd)
                slave_fd = None
            state["child"] = process_identity(child.pid)
            state["phase"] = "running"
            state["ownership_floor"] = state["child"]["create_time"]
            write_json(journal, state)
            os.write(gate_write, b"1")
        finally:
            os.close(gate_read)
            os.close(gate_write)
        reader = EventReader(attempt_dir / "events.jsonl", spec.get("session_id"))
        stopped = False
        stop_at = None
        last_owned_scan = 0.0
        while True:
            if external and master_fd is not None:
                ready, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [], 0.1)
                for descriptor in ready:
                    try:
                        data = os.read(descriptor, 65536)
                    except OSError:
                        data = b""
                    if descriptor == master_fd:
                        if data:
                            os.write(sys.stdout.fileno(), data)
                            os.write(stdout_fd, data)
                        else:
                            os.close(master_fd)
                            master_fd = None
                    elif data and master_fd is not None:
                        os.write(master_fd, data)
                    elif not data:
                        write_json(attempt_dir / "stop.json", {"job_id": spec["job_id"], "attempt_id": spec["attempt_id"], "action": "terminal_closed"})
            if not external:
                reader.poll()
            changed = False
            if time.monotonic() - last_owned_scan >= OWNED_RESCAN_INTERVAL:
                last_owned_scan = time.monotonic()
                owned_now, owned_uncertain = discover_owned(spec["job_id"], spec["attempt_id"], state["ownership_floor"], exclude_pids=(os.getpid(), state["child"]["pid"]))
                discovered, overflow = merge_owned(state.get('owned', []), owned_now)
                owned_uncertain = owned_uncertain or overflow
                if discovered != state.get('owned', []):
                    state['owned'] = discovered
                    changed = True
                _, retained_uncertain = classify_owned(state.get('owned', []), spec['job_id'], spec['attempt_id'])
                uncertain_now = bool(owned_uncertain or retained_uncertain)
                if state.get('ownership_uncertain') != uncertain_now:
                    state['ownership_uncertain'] = uncertain_now
                    changed = True
            if state.get("session_id") != reader.session_id:
                state["session_id"] = reader.session_id
                changed = True
            if reader.conflict and not state.get("session_conflict"):
                state["session_conflict"] = True
                changed = True
            code = child.poll()
            if changed:
                write_json(journal, state)
            if (attempt_dir / "stop.json").exists():
                stopped = True
                if stop_at is None:
                    wrapper_signaled = signal_group(state['child'], signal.SIGTERM)
                    owned_signaled = signal_owned(state.get('owned', []), signal.SIGTERM, spec['job_id'], spec['attempt_id'])
                    if wrapper_signaled or owned_signaled:
                        stop_at = time.monotonic()
                elif time.monotonic() - stop_at > 8:
                    signal_group(state["child"], signal.SIGKILL)
                    signal_owned(state.get("owned", []), signal.SIGKILL, spec["job_id"], spec["attempt_id"])
            child_done = code is not None and group_state(state["child"]) == "dead"
            owned_done = True
            if child_done and state.get("ownership_floor") is not None:
                owned_now, owned_uncertain = discover_owned(spec["job_id"], spec["attempt_id"], state["ownership_floor"], exclude_pids=(os.getpid(), state["child"]["pid"]))
                discovered, overflow = merge_owned(state.get('owned', []), owned_now)
                owned_uncertain = owned_uncertain or overflow
                if discovered != state.get('owned', []):
                    state['owned'] = discovered
                alive, live_uncertain = classify_owned(state.get("owned", []), spec["job_id"], spec["attempt_id"])
                # Uncertainty describes this scan, not a permanent latch: a
                # vanished inaccessible process must not block the job forever.
                state['ownership_uncertain'] = bool(owned_uncertain or live_uncertain)
                if alive or state.get("ownership_uncertain"):
                    # A live owned descendant (or unprovable ownership) holds
                    # the slot: persist the discovery but keep waiting for it
                    # (or a stop signal) instead of exiting without a receipt.
                    write_json(journal, state)
                owned_done = not alive and not state.get("ownership_uncertain")
            if child_done and owned_done:
                if not external:
                    while reader.poll():
                        pass
                final_owned, final_uncertain = discover_owned(spec["job_id"], spec["attempt_id"], state["ownership_floor"], exclude_pids=(os.getpid(), state["child"]["pid"]))
                state['owned'], overflow = merge_owned(state.get('owned', []), final_owned)
                alive_now, live_now_uncertain = classify_owned(state.get("owned", []), spec["job_id"], spec["attempt_id"])
                state['ownership_uncertain'] = bool(final_uncertain or live_now_uncertain or overflow)
                group_dead_now = group_state(state["child"]) == "dead"
                if alive_now or state['ownership_uncertain'] or not group_dead_now:
                    write_json(journal, state)
                    continue
                state["session_id"] = reader.session_id
                state["session_conflict"] = reader.conflict
                os.fsync(stdout_fd)
                os.fsync(stderr_fd)
                state["phase"] = "stopped"
                write_json(journal, state)
                # Termination is marked group_stopped only when the wrapper
                # group is dead AND every discovered owned descendant is dead
                # AND ownership was fully proven. Otherwise the receipt stays
                # honest ("Coordinator termination unverified") and the caller
                # (Runner._runtime) must keep the slot held.
                owned_quiet = not alive_now and not state.get("ownership_uncertain")
                write_json(attempt_dir / "exit.json", {**state, "exit_code": code, "stopped": stopped, "group_stopped": bool(group_dead_now and owned_quiet), "ended_at": time.time()})
                return 0
            if changed and not child_done:
                pass  # journal already flushed above for the running phase
            time.sleep(0.1)
    except Exception:
        state["phase"] = "supervisor_error"
        write_json(journal, state)
        if child is None:
            write_json(attempt_dir / "exit.json", {**state, "exit_code": None, "stopped": False, "group_stopped": True, "ended_at": time.time(), "error": "coordinator_launch_failed"})
        return 1
    finally:
        if terminal_state is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, terminal_state)
        if master_fd is not None:
            os.close(master_fd)
        if slave_fd is not None:
            os.close(slave_fd)
        os.close(stdout_fd)
        os.close(stderr_fd)
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--gate-fd", type=int)
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    try:
        if args.child:
            return coordinator_group(args.spec, args.gate_fd, args.lock_fd)
        return supervise(args.spec)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
