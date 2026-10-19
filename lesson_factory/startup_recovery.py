"""Operator-only recovery of one exact CLI-parse rejection, never a generic missing-SID retry."""
import hashlib
import os
from pathlib import Path
import stat

from . import core
from .dashboard_supervisor import fingerprint

HELP_SHA256 = '7a138276d9e67c37aa13875e2879a997858c1baff45e676e82e4b2bff8ff4b7d'
ERROR = b'\nERROR\n  Unrecognized flag: --variant in command opencode run\n'


def worker_identity(executable):
    path = Path(executable)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('Compatible absolute worker path required')
    from .opencode_worker import verify_binary
    version = verify_binary(str(path))
    with path.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {'path': str(path.resolve()), 'sha256': digest, 'version': version}


def bounded_bytes(root, path):
    checked = core.safe_path(root, str(path.relative_to(root)))
    fd = os.open(checked, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError('Invalid startup evidence file')
        data = os.read(fd, 65537)
        if len(data) > 65536:
            raise ValueError('Oversized startup evidence')
        return data
    finally:
        os.close(fd)


def proof(runner, job, attempt, *, lock_held):
    root = runner.root
    if not lock_held or job.get('session_id') is not None or attempt.get('session_id') is not None or attempt.get('ended_at') is None or attempt.get('state') != 'blocked' or attempt.get('exit_code') != 1 or attempt.get('completion_status') is not None:
        raise ValueError('Only a stopped first CLI rejection without a session can be recovered')
    selected = {**job, 'attempt_id': attempt['id'], 'launch_metadata': attempt['launch_metadata']}
    _, directory = runner._paths(selected)
    required = {'spec.json', 'process.json', 'exit.json', 'events.jsonl', 'stderr.log', 'supervisor.log'}
    if {p.name for p in directory.iterdir()} != required:
        raise ValueError('Unexpected startup artifacts or stop request')
    # Preflight BEFORE existing JSON/runtime helpers open any evidence paths.
    values = {name: bounded_bytes(root, directory / name) for name in sorted(required)}
    spec = runner._verified_spec(selected, attempt)
    metadata = attempt['launch_metadata']
    if metadata.get('mode') is not None or spec.get('mode') is not None or spec.get('session_id') is not None:
        raise ValueError('Managed initial launch required')
    command = spec.get('command', [])
    if len(command) != 11 or command[1:-1] != ['run', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas', '--variant', 'xhigh'] or not isinstance(command[-1], str) or not command[-1]:
        raise ValueError('Unexpected rejected command')
    runtime = runner._runtime(selected, lock_held=True)
    if runtime.get('quiescent') is not True or any(runtime.get(key) != 'dead' for key in ('supervisor', 'child', 'group')):
        raise ValueError('Rejected process group is not proven stopped')
    if values != {name: bounded_bytes(root, directory / name) for name in sorted(required)}:
        raise ValueError('Startup evidence changed during verification')
    if values['stderr.log'] != ERROR or hashlib.sha256(values['events.jsonl']).hexdigest() != HELP_SHA256 or values['supervisor.log']:
        raise ValueError('Not the exact pre-session CLI parse failure')
    import json
    receipt = json.loads(values['exit.json'])
    if receipt.get('exit_code') != 1 or receipt.get('stopped') is not False or receipt.get('group_stopped') is not True or receipt.get('session_conflict') is not False or receipt.get('phase') != 'stopped':
        raise ValueError('Stopped non-cancelled CLI rejection receipt required')
    run_paths = [job['run'], job['path'], f"work/translations/families/{job['language']}/{job['base_checkpoint']:06d}", f"work/family-publications/{job['language']}/{job['base_checkpoint']:06d}"]
    if any(core.safe_path(root, relative).exists() for relative in run_paths):
        raise ValueError('Existing authoring or paid evidence prevents a no-session restart')
    runner._original_environment(selected)
    return {'kind': 'exact-cli-variant-rejection-v1', 'job_id': job['id'], 'batch_id': job['batch_id'], 'attempt_id': attempt['id'], 'spec_sha256': fingerprint(spec), 'evidence_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in values.items()}}


def authorize(runner, job_id, executable):
    """Explicit operator entry; all old records untouched, authorization consumed by one claim."""
    with runner._mutex:
        runner._check_open()
        job = runner.store.get_job(job_id)
        if len(job['attempts']) != 1:
            raise ValueError('Exactly one rejected initial attempt is required')
        fd = runner._lock_job(job)
        try:
            evidence = proof(runner, job, job['attempts'][0], lock_held=True)
            worker = worker_identity(executable)
            return runner.store.authorize_cli_startup_retry(job_id, {'proof': evidence, 'worker': worker})
        finally:
            os.close(fd)


def verify_launch(runner, job, executable):
    """Recheck exact old evidence immediately before the one authorized replacement launch."""
    if not job.get('resume_authorized') or job.get('session_id') is not None or len(job['attempts']) != 2:
        raise ValueError('One explicit startup recovery required')
    receipt = runner.store.cli_startup_retry(job['id'])
    if receipt is None:
        raise ValueError('Missing startup retry receipt')
    fd = runner._lock_job(job)
    try:
        evidence = proof(runner, job, job['attempts'][0], lock_held=True)
        if receipt != {'proof': evidence, 'worker': worker_identity(executable)}:
            raise ValueError('Startup retry evidence or selected worker drifted')
        return receipt
    finally:
        os.close(fd)
