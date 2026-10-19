"""V2 worker commands; metadata-only, fail-closed checks before same-SID resume.

The V2 CLI creates a missing --session ID, so existence is not optional.
Official V2 OpenAPI session.get returns {data: Session.Info}; transcript export
has a different shape and is only offered for the operator's inspection.
"""
import json
import os
from pathlib import Path
import re
import subprocess

SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]{1,160}\Z")
WORKER_AGENT = "lesson-coordinator"
WORKER_MODEL = "opencode-omniroute/Antharas#xhigh"
VERSION_TIMEOUT = 10
EXPORT_TIMEOUT = 30  # metadata request, not a transcript export
V1_RESUME_REFUSED = "Saved V1 session requires explicit migration; no automatic V1-to-V2 migration or replacement"


def _absolute_executable(executable):
    if not isinstance(executable, str) or not executable or not Path(executable).is_absolute():
        raise ValueError("Absolute worker path required")
    return Path(executable)


def _require_session_id(session_id):
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ValueError("Valid saved coordinator session ID required; no fresh session will launch")


def _require_root(root):
    if not isinstance(root, (str, Path)) or not Path(root).is_absolute():
        raise ValueError("Project root must be an absolute path")
    return str(root)


def verify_binary(executable):
    """V2 only. Fail before queue claims rather than trying incompatible flags."""
    path = _absolute_executable(executable)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Executable worker binary required")
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Worker version check failed; no coordinator launched") from error
    if result.returncode:
        raise ValueError("Worker version check failed; no coordinator launched")
    match = re.fullmatch(r'(?:opencode v)?(\d+)\.(\d+)\.(\d+)', result.stdout.strip())
    if not match:
        raise ValueError("Unrecognized worker version; no coordinator launched")
    if int(match[1]) != 2:
        raise ValueError("Unsupported worker major version; V2 (2.x) required")
    return '.'.join(match.groups())


def command(executable, prompt, session_id=None):
    _absolute_executable(executable)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Coordinator prompt must be nonempty text")
    parts = [executable, 'run', '--standalone', '--format', 'json', '--agent', WORKER_AGENT, '--model', WORKER_MODEL]
    if session_id is not None:
        _require_session_id(session_id)
        parts += ['--session', session_id]
    return parts + [prompt]


def is_v2_command(value):
    if not isinstance(value, list) or len(value) not in (10, 12) or not all(isinstance(v, str) for v in value):
        return False
    try:
        return value == command(value[0], value[-1], value[10] if len(value) == 12 else None)
    except ValueError:
        return False


def export_command(executable, session_id):
    _absolute_executable(executable)
    _require_session_id(session_id)
    return [executable, 'session', 'export', '--standalone', session_id]


def terminal_command(executable, root, session_id):
    _absolute_executable(executable)
    _require_session_id(session_id)
    # V2 TUI has no --agent or --model. Retain the verified session binding.
    return [executable, _require_root(root), '--standalone', '--session', session_id]


def require_session(executable, session_id, root, environment):
    _absolute_executable(executable)
    _require_session_id(session_id)
    root_text = _require_root(root)
    if not isinstance(environment, dict) or not environment.get('HOME'):
        raise ValueError("Original job environment required")
    launch_env = {**os.environ, **environment}
    if 'OPENCODE_DB' in environment and environment['OPENCODE_DB'] is None:
        launch_env.pop('OPENCODE_DB', None)
    try:
        result = subprocess.run(
            [executable, 'api', '--standalone', 'session.get', '--param', f'sessionID={session_id}'],
            capture_output=True, text=True, timeout=EXPORT_TIMEOUT, cwd=root_text,
            env=launch_env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Session verification failed; no fresh session will launch") from error
    if result.returncode:
        raise ValueError("Saved V2 session unavailable; no fresh session will launch")
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise ValueError("Unrecognized session metadata; no fresh session will launch") from error
    if not isinstance(response, dict):
        raise ValueError("Unrecognized session metadata; no fresh session will launch")
    data = response.get('data')
    if (not isinstance(data, dict) or data.get('id') != session_id or data.get('agent') != WORKER_AGENT
            or not isinstance(data.get('location'), dict)
            or data['location'].get('directory') not in {root_text, str(Path(root_text).resolve())}):
        raise ValueError("Session identity mismatch; no fresh session will launch")
    return response
