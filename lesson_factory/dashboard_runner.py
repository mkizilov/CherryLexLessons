import fcntl
import os
from pathlib import Path
import re
import shlex
import signal
import time
import shutil
import subprocess
import sys
import threading
import uuid

import psutil

from .dashboard_supervisor import EventReader, SESSION_ID, boot_identity, checked_path, fingerprint, group_state, isolated_environment, private_directory, private_open, process_identity, process_state, read_json, saved_environment, signal_group, terminal_transient_error, write_json
from . import opencode_worker as worker
from .dashboard_supervisor import saved_job_environment, ownership_quiescent, classify_owned, signal_owned


IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
ACTIVE_STATES = {"launching", "running"}


class Runner:
    def __init__(self, root, store, preflight=None):
        self.preflight = preflight
        self.root = Path(root).resolve()
        self.store = store
        self.owner = str(uuid.uuid4())
        self._mutex = threading.RLock()
        self._children = []
        self._auto_rejected = set()
        self.directory = private_directory(self.root / "work/dashboard")
        private_directory(self.directory / "jobs")
        self._lock_fd = private_open(self.directory / "scheduler.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise RuntimeError("Another dashboard scheduler already owns this project") from None

    def close(self):
        with self._mutex:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def _check_open(self):
        if self._lock_fd is None:
            raise RuntimeError("Dashboard scheduler is closed")

    def _paths(self, job):
        if not IDENTIFIER.fullmatch(str(job["id"])) or not IDENTIFIER.fullmatch(str(job["attempt_id"])):
            raise ValueError("Invalid dashboard attempt identity")
        job_dir = checked_path(self.directory / "jobs" / job["id"])
        return job_dir, checked_path(job_dir / "attempts" / job["attempt_id"])

    def _attempt(self, job):
        return next((attempt for attempt in job.get("attempts", []) if attempt["id"] == job["attempt_id"]), {})

    def _metadata(self, job):
        return dict(self._attempt(job).get("launch_metadata") or job.get("launch_metadata") or {})

    def _job_lock_free(self, job_dir):
        fd = private_open(job_dir / "job.lock", os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        finally:
            os.close(fd)

    def _diagnostic(self, job, reason):
        metadata = self._metadata(job)
        if metadata.get("diagnostic") != reason:
            self.store.pause(job["batch_id"])
            self.store.attach_launch(job["id"], job["attempt_id"], {**metadata, "diagnostic": reason})
        return {"job_id": job["id"], "state": job["state"], "reason": reason}

    def _owned_quiescent(self, job, evidence):
        """V2 private servers/tools use separate PGIDs; verify those before release."""
        if 'ownership_floor' not in evidence:  # historical V1 evidence, unchanged
            return True
        floor = evidence.get('ownership_floor')
        if floor is None:
            return evidence.get('child') is None
        alive, uncertain = classify_owned(evidence.get('owned', []), job['id'], job['attempt_id'])
        quiet, _, scan_uncertain = ownership_quiescent(job['id'], job['attempt_id'], floor)
        return quiet and not alive and not uncertain and not scan_uncertain

    def _finish(self, job, state, reason, exit_code=None):
        result = self.store.finish_attempt(job["id"], job["attempt_id"], state, reason=reason, exit_code=exit_code)
        return {"job_id": job["id"], "state": result["state"], "reason": result.get("reason")}

    def _reconcile(self, job):
        job_dir, directory = self._paths(job)
        if not directory.exists():
            if self._metadata(job):
                return self._diagnostic(job, "Launch evidence missing; process ownership remains unresolved")
            return self._finish(job, "blocked", "Launch journal absent; manual reconciliation required")
        if self._metadata(job).get("mode") == "external":
            status = self._runtime(job)
            process = read_json(directory / "process.json")
            stop = read_json(directory / "stop.json")
            if status["supervisor"] == "dead" and process and stop and stop.get("job_id") == job["id"] and stop.get("attempt_id") == job["attempt_id"] and status["child"] == "alive":
                sent_at = stop.get("signal_at")
                sig = signal.SIGKILL if isinstance(sent_at, (int, float)) and time.time() - sent_at > 8 else signal.SIGTERM
                if signal_group(process["child"], sig) and sent_at is None:
                    write_json(directory / "stop.json", {**stop, "signal_at": time.time()})
            if status["diagnostic"] and status["supervisor"] != "alive":
                return self._diagnostic(job, status["diagnostic"])
            if process:
                metadata = self._metadata(job)
                updated = {**metadata, **{key: process[key] for key in ("supervisor", "child", "phase") if key in process}}
                if updated != metadata:
                    self.store.attach_launch(job["id"], job["attempt_id"], updated)
            return {"job_id": job["id"], "state": job["state"], "reason": "External recovery holds this slot until explicit verified resolution"}
        try:
            spec = read_json(directory / "spec.json")
            process = read_json(directory / "process.json")
            receipt = read_json(directory / "exit.json")
        except (OSError, ValueError):
            return self._diagnostic(job, "Unreadable launch evidence; manual reconciliation required")
        metadata = self._metadata(job)
        if spec is None or fingerprint(spec) != metadata.get("spec_sha256"):
            return self._diagnostic(job, "Launch specification identity mismatch; manual reconciliation required")
        for evidence in (process, receipt):
            if evidence is not None and (evidence.get("job_id") != job["id"] or evidence.get("attempt_id") != job["attempt_id"] or evidence.get("spec_sha256") != metadata["spec_sha256"]):
                return self._diagnostic(job, "Process evidence identity mismatch; manual reconciliation required")
        for key in ("supervisor", "child"):
            recorded = metadata.get(key)
            if process is not None and recorded is not None and process.get(key) != recorded or receipt is not None and receipt.get(key) != (process or metadata).get(key):
                return self._diagnostic(job, "Recorded process identity differs; explicit operator supervisor reconciliation required")
        evidence = process or metadata
        supervisor = evidence.get("supervisor") or metadata.get("supervisor")
        child = evidence.get("child")
        supervisor_status = process_state(supervisor)
        if evidence.get("session_conflict"):
            return self._diagnostic(job, "Conflicting coordinator session evidence; manual reconciliation required")
        session_id = evidence.get("session_id")
        if session_id is not None:
            if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
                return self._diagnostic(job, "Invalid coordinator session evidence; manual reconciliation required")
            if job.get("session_id") not in (None, session_id):
                return self._diagnostic(job, "Coordinator session mismatch; manual reconciliation required")
            if job.get("session_id") is None:
                self.store.set_session(job["id"], job["attempt_id"], session_id)
        if supervisor_status == "alive":
            updated = {**metadata, **{key: evidence[key] for key in ("supervisor", "child", "phase") if key in evidence}}
            if updated != metadata:
                self.store.attach_launch(job["id"], job["attempt_id"], updated)
            return {"job_id": job["id"], "state": "running", "reason": None}
        if supervisor_status == "unknown":
            return self._diagnostic(job, "Supervisor ownership unproven; no replacement will launch")
        stop = read_json(directory / "stop.json")
        if stop and child and stop.get("job_id") == job["id"] and stop.get("attempt_id") == job["attempt_id"]:
            sent_at = stop.get("signal_at")
            sig = signal.SIGKILL if isinstance(sent_at, (int, float)) and time.time() - sent_at > 8 else signal.SIGTERM
            sent = signal_group(child, sig)
            if 'ownership_floor' in evidence:
                sent = signal_owned(evidence.get('owned', []), sig, job['id'], job['attempt_id']) > 0 or sent
            if sent and sent_at is None:
                write_json(directory / "stop.json", {**stop, "signal_at": time.time()})
        if not self._job_lock_free(job_dir):
            return self._diagnostic(job, "Orphaned job process still owns its lock; manual reconciliation required")
        if child is not None:
            if process_state(child) != "dead" or group_state(child) != "dead":
                return self._diagnostic(job, "Orphaned coordinator process group; manual reconciliation required")
        elif process and process.get("phase") not in ("prepared", "stopped_before_launch") and not receipt:
            if supervisor.get("boot_id") == boot_identity():
                return self._diagnostic(job, "Child launch ownership unknown; manual reconciliation required")
        elif process is None:
            return self._diagnostic(job, "Supervisor journal missing; manual reconciliation required")
        if (directory / "events.jsonl").exists():
            reader = EventReader(directory / "events.jsonl", session_id or job.get("session_id"))
            while reader.poll():
                pass
            if reader.conflict:
                return self._diagnostic(job, "Conflicting coordinator stream identity; manual reconciliation required")
            if reader.session_id and not job.get("session_id"):
                self.store.set_session(job["id"], job["attempt_id"], reader.session_id)
        if not self._owned_quiescent(job, evidence):
            return self._diagnostic(job, 'V2 private server/tool ownership is live or uncertain; no replacement will launch')
        if receipt is not None:
            if receipt.get("group_stopped") is not True:
                return self._diagnostic(job, "Coordinator termination unverified; manual reconciliation required")
            job = self.store.get_job(job["id"])
            completion = self._attempt(job).get("completion_status")
            if receipt.get("stopped"):
                state = "interrupted" if job.get("session_id") else "blocked"
                return self._finish(job, state, "Stopped by user; review pending paid requests before explicit resume", receipt.get("exit_code"))
            if completion == "complete":
                if not job.get("session_id"):
                    return self._finish(job, "blocked", "Completion lacks a saved coordinator session; manual reconciliation required", receipt.get("exit_code"))
                return self._finish(job, "succeeded", None, receipt.get("exit_code"))
            if completion == "blocked":
                return self._finish(job, "blocked", "Coordinator reported blocked; explicit reconciliation required", receipt.get("exit_code"))
        job = self.store.get_job(job["id"])
        if receipt is not None and job.get("session_id"):
            scheduled = self._schedule_transient(job)
            if scheduled is not None:
                return scheduled
        state = "interrupted" if job.get("session_id") else "blocked"
        reason = "Coordinator stopped without a completion receipt; inspect paid request evidence before explicit resume" if state == "interrupted" else "Coordinator session unavailable; manual reconciliation required, never start a replacement session"
        return self._finish(job, state, reason, receipt.get("exit_code") if receipt else None)

    def recover(self):
        with self._mutex:
            self._check_open()
            self.store.pause_all()
            return self._recover_active()

    def _safe_reconcile(self, job):
        try:
            return self._reconcile(job)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, psutil.Error):
            return self._diagnostic(job, "Job evidence could not be reconciled; manual inspection required")

    def _recover_active(self):
        self._children = [child for child in self._children if child.poll() is None]
        return [self._safe_reconcile(job) for job in self.store.active_jobs()]

    def _transient_evidence(self, job):
        """Identity-bound proof that the current managed attempt ended only by an allowlisted transient provider error.

        Requires the caller to hold the exclusive job lock. Returns None for anything unproven."""
        attempt = self._attempt(job)
        metadata = self._metadata(job)
        session_id = job.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id) or attempt.get("session_id") != session_id or attempt.get("completion_status") is not None or metadata.get("mode") is not None or metadata.get("aborted_before_launch") is not None or metadata.get("diagnostic") is not None:
            return None
        _, directory = self._paths(job)
        if (directory / "stop.json").exists():
            return None
        spec = self._verified_spec(job)
        if not worker.is_v2_command(spec.get('command')) or spec.get("mode") is not None or spec.get("session_id") not in (None, session_id) or spec.get("environment") != self._original_environment(job):
            return None
        runtime = self._runtime(job, lock_held=True)
        if not runtime["quiescent"] or not runtime["started"] or runtime["diagnostic"] is not None:
            return None
        process = read_json(directory / "process.json")
        receipt = read_json(directory / "exit.json")
        if not isinstance(receipt, dict) or not isinstance(process, dict) or process.get("phase") != "stopped" or receipt.get("phase") != "stopped" or receipt.get("group_stopped") is not True or receipt.get("stopped") is not False or "error" in receipt or type(receipt.get("exit_code")) is not int or receipt["exit_code"] == 0 or receipt.get("session_conflict") is not False or receipt.get("session_id") != session_id:
            return None
        terminal = terminal_transient_error(directory / "events.jsonl", session_id)
        if terminal is None:
            return None
        return {"signature": terminal[0], "evidence_sha256": terminal[1], "spec_sha256": fingerprint(spec), "session_id": session_id, "exit_code": receipt["exit_code"]}

    def _schedule_transient(self, job, adopt=False):
        try:
            fd = self._lock_job(job)
        except (OSError, ValueError):
            return None
        try:
            evidence = self._transient_evidence(job)
            if evidence is None:
                return None
            result = self.store.schedule_auto_resume(job["id"], job["attempt_id"], evidence, adopt=adopt)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration, psutil.Error):
            return None
        finally:
            os.close(fd)
        return {"job_id": job["id"], "state": result["state"], "reason": result.get("reason")}

    def _auto_resume(self):
        """Adopt verified transient stops and authorize due same-session resumes; only for unpaused batches."""
        outcomes = []
        candidates = getattr(self.store, "auto_resume_candidates", None)  # minimal stores without the ledger never auto-resume
        for job in candidates() if candidates else []:
            key = (job["id"], job["attempt_id"], job["state"])
            if key in self._auto_rejected:
                continue
            try:
                status = self.store.auto_resume_status(job["id"])
                if job["state"] == "interrupted":
                    if job["attempt_id"] in status["handled_attempts"]:
                        self._auto_rejected.add(key)
                        continue
                    outcome = self._schedule_transient(job, adopt=True)
                    if outcome is None:
                        self._auto_rejected.add(key)
                    else:
                        outcomes.append(outcome)
                    continue
                pending = status["pending"]
                if pending is None or pending["attempt_id"] != job["attempt_id"]:
                    self._auto_rejected.add(key)
                    continue
                if time.time() < pending["due_at"]:
                    continue
                fd = self._lock_job(job)
                try:
                    try:
                        evidence = self._transient_evidence(job)
                    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration, psutil.Error):
                        evidence = None
                    if evidence is None or any(evidence[name] != pending.get(name) for name in ("signature", "evidence_sha256", "spec_sha256", "session_id")):
                        result = self.store.reject_auto_resume(job["id"], job["attempt_id"])
                    else:
                        result = self.store.authorize_auto_resume(job["id"], job["attempt_id"], pending["event_id"], evidence)
                finally:
                    os.close(fd)
                outcomes.append({"job_id": job["id"], "state": result["state"], "reason": result.get("reason")})
            except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration, psutil.Error):
                continue
        return outcomes

    def tick(self):
        with self._mutex:
            self._check_open()
            outcomes = self._recover_active()
            if self.preflight is not None:
                self.preflight.check()
            outcomes.extend(self._auto_resume())
            while True:
                job = self.store.claim_next(self.owner)
                if job is None:
                    break
                try:
                    outcomes.append(self._launch(job))
                except (OSError, ValueError, KeyError, TypeError, AttributeError, psutil.Error):
                    outcomes.append(self._diagnostic(job, "Launch ownership uncertain; manual inspection required"))
            return outcomes

    def _prompt(self, job, batch):
        completion = shlex.join([sys.executable, str(self.root / "tools/dashboard-job.py"), "--job", job["id"], "--attempt", job["attempt_id"], "--status", "complete"])
        return (
            f"Queued dashboard job {job['id']}, attempt {job['attempt_id']}. Read {self.root / 'AGENTS.md'} and the lesson authoring instructions. "
            f"Author exactly ONE schema-5 family: study {job['language']}, base {job['base_checkpoint']}, run {job['run']}, including all five stages. "
            "If this exact family is already finalized, never reauthor or modify it: independently inspect its registry and immutable pack, reconcile existing review evidence, record completion for this attempt and exit. "
            f"If the pack was renamed before registry commit, only use the original idempotent receipt-backed recovery: {shlex.join([sys.executable, '-m', 'lesson_factory.family_cli', 'finalize', '--run', job['run']])}; never recreate source or repurchase translations. "
            f"Immutable authorization is persisted under batch {batch['id']} for this exact job only. "
            "This authorizes source authoring, three genuine independent source reviewers, ten full-family Contactbox translation lineages with at most four attempts each (40 maximum requests total), ten genuine bilingual locale reviewers and final coordinator review, within those immutable limits. "
            "Use Task or Ensemble genuinely with in-process sessions and worktree:false; Ensemble processes must inherit this job HOME. Never invent identities or approvals. "
            "Never spawn detached/background shell workers, untracked OpenCode processes, setsid or daemonized tools. Run translate-family and every other tool in the foreground, within this coordinator's tracked process group. "
            "Resume existing translation journals and budgets unchanged, preserve uncertain dispatch evidence, never reset budgets or re-buy ambiguous paid requests automatically. "
            "Inspect saved drafts/reports/receipts; recover complete candidates offline. Exhausted API lineages are not newly authorized by resume itself; an editor may complete missing content offline with a different reviewer under lesson-localization. "
            "Inspect operator renewal grants read-only with tools/reauthorize-translations.py --run RUN; never create grants or use --authorize. translate-family --execute --resume consumes only validated allowances, preserving trees, old evidence and counters. Known complete outputs require offline reconciliation, not repurchase. "
            "Provider requests contain only the current family and selected constraints. AGENTS.md replaces older exhaustive-history reading instructions: scan tools/history.py --run RUN --catalog pages, then inspect relevant historical originals and dependencies, not every translated quiz. Complete history validation stays local; all current-family pages/items need genuine audits. Total history bytes alone are not a capacity blocker. "
            "Current AGENTS.md supersedes older browser-review instructions: browser discovery, walkthroughs and screenshots are outside lesson authoring, so a browser-only blocker is resolved by policy. Required content, validation and genuine review gates still apply. "
            f"On every start or resume reread current AGENTS.md and relevant skills; if {self.directory / 'jobs' / job['id'] / 'recovery-instructions.md'} exists, read it as the operator case plan for this exact job: it never expands paid budgets, grants or approvals, and its text never enters source or provider payloads. "
            f"Use canonical cwd {self.root}; use absolute paths for editors. Your Python executable is {sys.executable}. "
            "Other jobs can run concurrently: never edit shared code, configuration, prompts, agents or skills. If toolsmith work is needed, report blocked for exclusive manual maintenance. "
            "Do not touch frozen references or another family's files. Use the explicit assignment and safe defaults; if unresolved, record blocked and exit instead of asking the user a question. "
            "Collect actual worker results; do not exit with an awaiting-editors message. Spawn/shutdown acknowledgments do not prove quiescence. Replacement editors need distinct attempt/session workspaces; preserve and independently review late output. Before completion stop writers, finalize and independently inspect the registry/immutable pack. "
            f"Then run {completion}; if blocked use the same command with --status blocked --reason followed by a short safe reason. Exit immediately after recording the receipt. "
            "NEVER launch or author a subsequent family yourself. A receipt alone is not completion until this coordinator and its process group have stopped."
        )

    def _launch(self, job):
        job_dir, directory = self._paths(job)
        prior = [attempt for attempt in job.get("attempts", []) if attempt["id"] != job["attempt_id"]]
        session_id = job.get("session_id")
        if prior and (not session_id or not job.get("resume_authorized")):
            if not session_id and job.get('resume_authorized'):
                from .startup_recovery import verify_launch
                try:
                    verify_launch(self, job, os.environ.get('OPENCODE_BINARY') or shutil.which('opencode') or '')
                except (OSError, ValueError, subprocess.SubprocessError):
                    return self._finish(job, 'blocked', 'Startup retry evidence invalid; no replacement session will launch')
            else:
                return self._finish(job, "blocked", "Explicit same-session resume authorization required; no replacement session will launch")
        if session_id is not None and not SESSION_ID.fullmatch(session_id):
            return self._finish(job, "blocked", "Invalid saved coordinator session; manual reconciliation required")
        if any((directory / ".git").exists() for directory in (self.root, *self.root.parents)) or any(os.environ.get(key) for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")):
            return self._finish(job, "blocked", "Concurrent Ensemble authoring requires a non-Git lesson project; reconcile repository isolation before launch")
        executable = os.environ.get("OPENCODE_BINARY") or shutil.which("opencode")
        if not executable or not Path(executable).is_absolute() or not os.access(executable, os.X_OK):
            return self._finish(job, "blocked", "OpenCode executable unavailable; no coordinator launched")
        try:
            worker.verify_binary(executable)
            environment = self._original_environment(job) if prior else saved_job_environment(isolated_environment(job_dir / "home"), job_dir / "home")
            if session_id:
                original = self._session_spec(job)
                if not worker.is_v2_command(original['command']):
                    raise ValueError(worker.V1_RESUME_REFUSED)
                worker.require_session(executable, session_id, self.root, environment)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return self._finish(job, 'blocked', str(error))
        batch = self.store.get_batch(job["batch_id"])
        private_directory(job_dir)
        private_directory(job_dir / 'home' / '.config' / 'opencode')
        private_directory(job_dir / "attempts")
        if directory.exists():
            return self._diagnostic(job, "Attempt launch journal already exists; no duplicate coordinator will launch")
        private_directory(directory)
        command = worker.command(executable, self._prompt(job, batch), session_id)
        spec = {"root": str(self.root), "job_id": job["id"], "attempt_id": job["attempt_id"], "session_id": session_id, "command": command, "environment": environment}
        write_json(directory / "spec.json", spec)
        metadata = {"attempt_dir": str(directory.relative_to(self.root)), "spec_sha256": fingerprint(spec), "phase": "prepared"}
        self.store.attach_launch(job["id"], job["attempt_id"], metadata)
        supervisor_command = [sys.executable, str(Path(__file__).with_name("dashboard_supervisor.py")), "--spec", str(directory / "spec.json")]
        supervisor_fd = private_open(directory / "supervisor.log")
        try:
            supervisor = subprocess.Popen(supervisor_command, cwd=self.root, stdin=subprocess.DEVNULL, stdout=supervisor_fd, stderr=supervisor_fd, start_new_session=True)
            self._children.append(supervisor)
        except OSError:
            return self._finish(job, "blocked", "Supervisor did not launch; manual reconciliation required")
        finally:
            os.close(supervisor_fd)
        try:
            metadata["supervisor"] = process_identity(supervisor.pid)
            self.store.attach_launch(job["id"], job["attempt_id"], metadata)
        except (OSError, ValueError, psutil.Error):
            return self._diagnostic(job, "Supervisor launch evidence incomplete; recovery required")
        return {"job_id": job["id"], "state": "launching", "reason": None}

    def _verified_spec(self, job, attempt=None):
        attempt = attempt or self._attempt(job)
        _, directory = self._paths({**job, "attempt_id": attempt["id"]})
        spec = read_json(directory / "spec.json")
        if not isinstance(spec, dict) or fingerprint(spec) != attempt["launch_metadata"].get("spec_sha256") or spec.get("root") != str(self.root) or spec.get("job_id") != job["id"] or spec.get("attempt_id") != attempt["id"]:
            raise ValueError("Launch specification identity mismatch; manual reconciliation required")
        return spec

    def _session_spec(self, job):
        """Use the attempt that actually ran this SID, not a previous pre-CLI failure."""
        for attempt in reversed(job.get('attempts', [])):
            if attempt.get('session_id') == job.get('session_id') and attempt.get('launch_metadata', {}).get('spec_sha256'):
                spec = self._verified_spec(job, attempt)
                if spec.get('mode') != 'external':
                    return spec
        raise ValueError('Saved session runtime evidence unavailable; refusing replacement')

    def _original_environment(self, job):
        job_dir, _ = self._paths(job)
        for attempt in job["attempts"]:
            if attempt.get("launch_metadata", {}).get("spec_sha256"):
                recorded = self._verified_spec(job, attempt).get('environment')
                validate = saved_job_environment if isinstance(recorded, dict) and 'OPENCODE_DB' in recorded else saved_environment
                return validate(recorded, job_dir / 'home')
        raise ValueError("Exact original job HOME/XDG/OpenCode environment unavailable; refusing environment loss")

    def _runtime(self, job, lock_held=False):
        status = {"supervisor": "unknown", "child": "unknown", "group": "unknown", "lock": "unknown", "quiescent": False, "started": False, "diagnostic": None}
        try:
            job_dir, directory = self._paths(job)
            aborted = self._metadata(job).get("aborted_before_launch")
            if aborted is not None:
                spec = self._unlaunched_spec(job)
                if self._attempt(job).get("ended_at") is None or aborted != {"spec_sha256": fingerprint(spec), "session_id": job["session_id"]}:
                    raise ValueError("Invalid unlaunched recovery receipt")
                status["lock"] = "free" if lock_held or self._job_lock_free(job_dir) else "held"
                status.update(supervisor="not_started", child="not_started", group="not_started", quiescent=status["lock"] == "free")
                return status
            spec = self._verified_spec(job)
            process = read_json(directory / "process.json")
            receipt = read_json(directory / "exit.json")
            status["lock"] = "free" if lock_held or self._job_lock_free(job_dir) else "held"
            external = self._metadata(job).get("mode") == "external"
            if process is None:
                if external and receipt is None and self._metadata(job).get("phase") == "awaiting_terminal" and status["lock"] == "free":
                    status.update(supervisor="not_started", child="not_started", group="not_started", quiescent=True)
                    return status
                raise ValueError("Supervisor journal missing; process ownership remains unresolved")
            for evidence in (process, receipt):
                if evidence is not None and (not isinstance(evidence, dict) or evidence.get("job_id") != job["id"] or evidence.get("attempt_id") != job["attempt_id"] or evidence.get("spec_sha256") != fingerprint(spec) or evidence.get("session_conflict") or evidence.get("session_id") != job.get("session_id")):
                    raise ValueError("Process or session evidence identity mismatch; manual reconciliation required")
            for key in ("supervisor", "child"):
                identity = process.get(key)
                recorded = self._metadata(job).get(key)
                if recorded is not None and identity != recorded or receipt is not None and receipt.get(key) != identity:
                    raise ValueError("Recorded process ownership identity cannot change")
            status["started"] = bool(process.get("child")) if external else True
            status["supervisor"] = process_state(process.get("supervisor"))
            child = process.get("child")
            status["child"] = process_state(child)
            status["group"] = group_state(child) if status["supervisor"] != "alive" else "alive"
            if child is None and receipt and receipt.get("group_stopped") is True and process.get("phase") in ("prepared", "stopped_before_launch", "supervisor_error"):
                status.update(child="dead", group="dead")
            if receipt and receipt.get("group_stopped") is not True:
                raise ValueError("Coordinator termination receipt is unverified")
            if not external and status["supervisor"] == "dead" and (directory / "events.jsonl").exists():
                reader = EventReader(directory / "events.jsonl", job.get("session_id"))
                while reader.poll():
                    pass
                if reader.conflict:
                    raise ValueError("Conflicting coordinator stream identity; manual reconciliation required")
            status["quiescent"] = status["lock"] == "free" and all(status[key] == "dead" for key in ("supervisor", "child", "group")) and self._owned_quiescent(job, process)
            if not status["quiescent"]:
                status["diagnostic"] = "Coordinator is still running or ownership is unknown; slot cannot be released"
        except (OSError, ValueError, KeyError, TypeError, AttributeError, psutil.Error):
            status["diagnostic"] = "Runtime evidence is missing, inconsistent, or unreadable; slot cannot be released"
        return status

    def _unlaunched_spec(self, job):
        _, directory = self._paths(job)
        if {path.name for path in directory.iterdir()} != {"spec.json"}:
            raise ValueError("Unlaunched recovery requires an untouched spec-only directory")
        previous = job.get("attempts", [])[:-1]
        if not previous or previous[-1].get("ended_at") is None or previous[-1].get("session_id") != job.get("session_id"):
            raise ValueError("Stopped same-session predecessor required")
        original_spec = self._session_spec({**job, 'attempts': previous})
        if not worker.is_v2_command(original_spec['command']):
            raise ValueError('Unlaunched V1 recovery needs explicit migration')
        executable = original_spec['command'][0]
        expected = {"root": str(self.root), "job_id": job["id"], "attempt_id": job["attempt_id"], "session_id": job["session_id"], "environment": self._original_environment(job), "command": worker.command(executable, self._prompt(job, self.store.get_batch(job["batch_id"])), job['session_id'])}
        spec = read_json(directory / "spec.json")
        if spec != expected:
            raise ValueError("Unlaunched specification differs from the authorized saved-session launch")
        return spec

    def _supervisor_recovery_state(self, identity):
        if not isinstance(identity, dict) or set(identity) != {"pid", "create_time", "boot_id", "command_fingerprint"} or type(identity["pid"]) is not int or identity["pid"] <= 0 or type(identity["create_time"]) not in (int, float) or not 0 < identity["create_time"] < float("inf") or not isinstance(identity["boot_id"], str) or not identity["boot_id"] or not isinstance(identity["command_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", identity["command_fingerprint"]):
            raise ValueError("Complete supervisor or child identity required")
        state = process_state(identity)
        if identity["boot_id"] == boot_identity():
            try:
                if psutil.Process(identity["pid"]).create_time() != identity["create_time"]:
                    raise ValueError("Recorded PID has been reused; manual inspection required")
            except psutil.NoSuchProcess:
                pass
        return state

    def _supervisor_recovery_proof(self, job, request):
        job_dir, directory = self._paths(job)
        metadata = self._metadata(job)
        if job["state"] not in ACTIVE_STATES or self._attempt(job).get("ended_at") is not None or metadata.get("phase") != "prepared" or set(metadata) - {"attempt_dir", "spec_sha256", "phase", "supervisor", "diagnostic"}:
            raise ValueError("Only the original prepared supervisor without any child metadata can be reconciled")
        spec = self._verified_spec(job)
        session_id = job.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id) or self._attempt(job).get("session_id") != session_id or spec.get("session_id") != session_id or spec.get("mode") is not None:
            raise ValueError("Exact saved-session managed attempt required")
        validate = saved_job_environment if 'OPENCODE_DB' in spec.get('environment', {}) else saved_environment
        environment = validate(spec.get("environment"), job_dir / "home")
        if spec["environment"] != environment or environment != self._original_environment(job):
            raise ValueError("Original saved environment cannot change")
        command = spec.get("command")
        v1_tail = ["run", "--format", "json", "--agent", "lesson-coordinator", "--model", "opencode-omniroute/Antharas", "--variant", "xhigh", "--session", session_id]
        if not isinstance(command, list) or not command or not isinstance(command[0], str) or not Path(command[0]).is_absolute() or not isinstance(command[-1], str) or not command[-1]:
            raise ValueError("Exact authorized same-session coordinator command required")
        # Reconcile historical V1 evidence without launching or migrating it.
        if command[1:-1] != v1_tail and command != worker.command(command[0], command[-1], session_id):
            raise ValueError("Exact authorized same-session coordinator command required")
        old = metadata.get("supervisor")
        new = request["new_supervisor"]
        attestation = request["attestation"]
        required = {"kind", "old_supervisor", "job_id", "attempt_id", "session_id", "spec_sha256", "checks", "script_sha256", "report_sha256"}
        checks = {"old_supervisor_dead": True, "job_lock_free": True, "phase": "prepared", "child_metadata_absent": True, "directory_files": ["spec.json", "supervisor.log"], "supervisor_log_empty": True}
        if not isinstance(attestation, dict) or set(attestation) != required or attestation.get("kind") != "operator_attested_prelaunch" or attestation.get("old_supervisor") != old or attestation.get("checks") != checks:
            raise ValueError("Explicit operator attestation of witnessed pre-child checks required; no retrospective receipt is inferred")
        for key, value in (("job_id", job["id"]), ("attempt_id", job["attempt_id"]), ("session_id", session_id), ("spec_sha256", fingerprint(spec))):
            if request.get(key) != value or attestation.get(key) != value:
                raise ValueError("Operator attestation scope does not match the current attempt")
        for key in ("script_sha256", "report_sha256"):
            if not isinstance(attestation[key], str) or not re.fullmatch(r"[0-9a-f]{64}", attestation[key]):
                raise ValueError("Operator script and report digests required")
        if metadata.get("attempt_dir") != str(directory.relative_to(self.root)) or request["old_supervisor"] != old or old == new or self._supervisor_recovery_state(old) != "dead":
            raise ValueError("Recorded old supervisor must be verifiably dead and distinct")
        expected_command = [sys.executable, str(self.root / "lesson_factory/dashboard_supervisor.py"), "--spec", str(directory / "spec.json")]
        new_state = self._supervisor_recovery_state(new)
        if any(identity["command_fingerprint"] != fingerprint(expected_command) for identity in (old, new)):
            raise ValueError("Expected original and replacement supervisor command required")
        if new["pid"] == old["pid"] or new["boot_id"] != boot_identity() or old["boot_id"] == new["boot_id"] and new["create_time"] <= old["create_time"]:
            raise ValueError("Replacement supervisor chronology or boot identity mismatch")
        process = read_json(directory / "process.json")
        receipt = read_json(directory / "exit.json")
        if not isinstance(process, dict):
            raise ValueError("Replacement process journal required")
        for evidence in (process, receipt):
            if evidence is None:
                continue
            if not isinstance(evidence, dict) or any(evidence.get(key) != request[key] for key in ("job_id", "attempt_id", "session_id", "spec_sha256")) or evidence.get("session_conflict") or evidence.get("supervisor") != new:
                raise ValueError("Replacement process or exit journal scope mismatch")
        child = process.get("child")
        child_state = self._supervisor_recovery_state(child)
        if child["pid"] in (old["pid"], new["pid"]) or child["boot_id"] != new["boot_id"] or child["create_time"] < new["create_time"]:
            raise ValueError("Replacement child identity or chronology mismatch")
        if receipt is not None and (receipt.get("child") != child or receipt.get("phase") != process.get("phase") or receipt.get("group_stopped") is not True):
            raise ValueError("Replacement exit journal disagrees with child or phase")
        group = group_state(child)
        if new_state == "alive" and child_state == "alive" and group == "alive" and receipt is None and process.get("phase") == "running":
            parent_process = psutil.Process(new["pid"])
            child_process = psutil.Process(child["pid"])
            child_command = child_process.cmdline()
            prefix = [sys.executable, str(self.root / "lesson_factory/dashboard_supervisor.py"), "--spec", str(directory / "spec.json"), "--child", "--gate-fd"]
            if parent_process.cmdline() != expected_command or parent_process.cwd() != str(self.root) or child_process.ppid() != new["pid"] or child_process.cwd() != str(self.root) or os.getpgid(child["pid"]) != child["pid"] or len(child_command) != 9 or child_command[:6] != prefix or child_command[7] != "--lock-fd" or not child_command[6].isdigit() or not child_command[8].isdigit() or fingerprint(child_command) != child["command_fingerprint"]:
                raise ValueError("Live supervised child command, parent, or process group mismatch")
            actual_environment = child_process.environ()
            expected_environment = {key: value for key, value in environment.items() if value is not None}
            expected_environment.update({"CHERRYLEX_DASHBOARD_JOB": job["id"], "CHERRYLEX_DASHBOARD_ATTEMPT": job["attempt_id"]})
            if 'OPENCODE_DB' in environment and environment['OPENCODE_DB'] is None and 'OPENCODE_DB' in actual_environment:
                raise ValueError('Live child database selection drifted')
            if any(actual_environment.get(key) != value for key, value in expected_environment.items()):
                raise ValueError("Live child environment identity mismatch")
            if not any(item.path == str(job_dir / "job.lock") and item.fd == int(child_command[8]) for item in child_process.open_files()):
                raise ValueError("Live child must retain the expected inherited job-lock descriptor")
            lock = "held"
        elif new_state == "dead" and child_state == "dead" and group == "dead" and receipt is not None and process.get("phase") == "stopped":
            if type(receipt.get("exit_code")) is not int or type(receipt.get("stopped")) is not bool or type(receipt.get("ended_at")) not in (int, float) or not child["create_time"] <= receipt["ended_at"] <= time.time():
                raise ValueError("Complete authentic termination journal required")
            lock = "free"
        else:
            raise ValueError("Replacement must be coherently live or fully stopped with an exit journal")
        if (directory / "events.jsonl").exists():
            reader = EventReader(directory / "events.jsonl", session_id)
            while reader.poll():
                pass
            if reader.conflict:
                raise ValueError("Conflicting coordinator stream identity")
        hashes = {"spec": fingerprint(spec), "environment": fingerprint(environment), "process": fingerprint(process), "exit": fingerprint(receipt) if receipt is not None else None}
        if read_json(directory / "spec.json") != spec or read_json(directory / "process.json") != process or read_json(directory / "exit.json") != receipt:
            raise ValueError("Recovery evidence changed during verification; retry explicit reconciliation")
        if self._supervisor_recovery_state(old) != "dead" or self._supervisor_recovery_state(new) != new_state or self._supervisor_recovery_state(child) != child_state or group_state(child) != group:
            raise ValueError("Process ownership changed during verification; retry explicit reconciliation")
        return {"expected_metadata": metadata, "child": child, "phase": process["phase"], "old_supervisor_state": "dead", "supervisor_state": new_state, "child_state": child_state, "group_state": group, "lock": lock, "hashes": hashes}

    def reconcile_supervisor(self, job_id, attempt_id, new_supervisor, operator_note, attestation):
        with self._mutex:
            self._check_open()
            job = self.store.get_job(job_id)
            if job.get("attempt_id") != attempt_id:
                raise ValueError("Only the exact current attempt can be reconciled")
            if not isinstance(attestation, dict):
                raise ValueError("Explicit operator attestation required")
            request = {"job_id": job_id, "attempt_id": attempt_id, "session_id": attestation.get("session_id"), "spec_sha256": attestation.get("spec_sha256"), "old_supervisor": attestation.get("old_supervisor"), "new_supervisor": new_supervisor, "operator_note": operator_note, "attestation": attestation}
            if self._metadata(job).get("supervisor_reconciliation") is not None:
                return self.store.reconcile_supervisor(job_id, attempt_id, request)
            job_dir, _ = self._paths(job)
            if not checked_path(job_dir / "job.lock").is_file():
                raise ValueError("Existing job lock required")
            proof = self._supervisor_recovery_proof(job, request)
            lock_fd = None
            try:
                if proof["lock"] == "free":
                    lock_fd = self._lock_job(job)
                    proof = self._supervisor_recovery_proof(job, request)
                    if proof["lock"] != "free":
                        raise ValueError("Replacement restarted during verification")
                elif self._job_lock_free(job_dir):
                    raise ValueError("Live replacement must hold the job lock")
                return self.store.reconcile_supervisor(job_id, attempt_id, request, proof)
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)

    def abort_unlaunched(self, job_id):
        with self._mutex:
            self._check_open()
            if not (self.directory / "queue.sqlite").is_file():
                raise ValueError("Existing scheduler database required")
            job = self.store.get_job(job_id)
            fd = self._lock_job(job)
            try:
                spec = self._unlaunched_spec(job)
                return self.store.abort_unlaunched(job_id, job["attempt_id"], job["session_id"], fingerprint(spec))
            finally:
                os.close(fd)

    def recovery_details(self, job_id):
        with self._mutex:
            self._check_open()
            job = self.store.get_job(job_id)
            runtime = self._runtime(job) if job.get("attempt_id") else {"supervisor": "not_started", "child": "not_started", "group": "not_started", "lock": "unknown", "quiescent": False, "started": False, "diagnostic": "No coordinator attempt exists"}
            external = self._metadata(job).get("mode") == "external"
            inspect_command = resume_command = None
            environment_reason = None
            try:
                environment = self._original_environment(job)
                original_spec = self._session_spec(job)
                executable = original_spec['command'][0]
                if not Path(executable).is_absolute() or not job.get("session_id") or not SESSION_ID.fullmatch(job["session_id"]):
                    raise ValueError("Original coordinator executable or session unavailable")
                prefix = ['env', *(['-u', 'OPENCODE_DB'] if 'OPENCODE_DB' in environment and environment['OPENCODE_DB'] is None else []), *[f'{key}={value}' for key, value in environment.items() if value is not None]]
                if not worker.is_v2_command(original_spec['command']):
                    raise ValueError('Saved V1 session requires explicit migration')
                inspect_command = shlex.join([*prefix, *worker.export_command(executable, job['session_id'])])
                if external and runtime["quiescent"] and not runtime["started"] and job["state"] in ACTIVE_STATES:
                    resume_command = shlex.join([*prefix, sys.executable, str(self.root / "tools/dashboard-job.py"), "--root", str(self.root), "--job", job_id, "--attempt", job["attempt_id"], "--external-resume"])
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                environment_reason = "Exact original environment or saved session unavailable; external resume is disabled"
            can_handoff = job["state"] in ("blocked", "interrupted", "retry_wait") and runtime["quiescent"] and environment_reason is None
            can_resolve = bool(job.get("session_id")) and job["state"] not in ("queued", "cancelled", "succeeded") and runtime["quiescent"] and runtime["started"]
            return {"job_id": job_id, "attempt_id": job["attempt_id"], "session_id": job.get("session_id"), "attempts": job.get("attempts", []), "process": runtime, "external": external, "inspect_command": inspect_command, "resume_command": resume_command, "can_handoff": can_handoff, "handoff_reason": None if can_handoff else environment_reason or runtime["diagnostic"] or "A stopped failed attempt is required; existing external holds cannot be resumed twice", "can_resolve": can_resolve}

    def _lock_job(self, job):
        job_dir, _ = self._paths(job)
        fd = private_open(job_dir / "job.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ValueError("Tracked coordinator still owns the job; wait for termination") from None
        return fd

    def handoff_job(self, job_id):
        with self._mutex:
            self._check_open()
            self._recover_active()
            job = self.store.get_job(job_id)
            if not self.recovery_details(job_id)["can_handoff"]:
                raise ValueError("External handoff requires a stopped failed attempt, saved session and exact original environment")
            fd = self._lock_job(job)
            try:
                if not self._runtime(job, lock_held=True)["quiescent"]:
                    raise ValueError("Coordinator ownership is active or ambiguous; no external resume allowed")
                environment = self._original_environment(job)
                executable = self._session_spec(job)['command'][0]
                worker.verify_binary(executable)
                worker.require_session(executable, job['session_id'], self.root, environment)
                job = self.store.reserve_external(job_id)
                _, directory = self._paths(job)
                private_directory(directory)
                command = worker.terminal_command(executable, self.root, job['session_id'])
                spec = {"root": str(self.root), "job_id": job_id, "attempt_id": job["attempt_id"], "session_id": job["session_id"], "mode": "external", "command": command, "environment": environment}
                write_json(directory / "spec.json", spec)
                self.store.attach_launch(job_id, job["attempt_id"], {"attempt_dir": str(directory.relative_to(self.root)), "spec_sha256": fingerprint(spec)})
            finally:
                os.close(fd)
            return self.recovery_details(job_id)

    def reconcile_job(self, job_id, operator_note):
        with self._mutex:
            self._check_open()
            self._recover_active()
            job = self.store.get_job(job_id)
            if not job.get("attempt_id"):
                raise ValueError("No coordinator attempt exists")
            fd = self._lock_job(job)
            try:
                runtime = self._runtime(job, lock_held=True)
                if not runtime["quiescent"] or not runtime["started"]:
                    raise ValueError("Coordinator must have started and be verifiably stopped; slot remains held")
                evidence = {"job_id": job_id, "attempt_id": job["attempt_id"], "spec_sha256": self._metadata(job).get("spec_sha256"), "quiescent": True, "started": True, "checked_at": time.time()}
                return self.store.resolve_verified(job_id, job["attempt_id"], operator_note, evidence)
            finally:
                os.close(fd)

    def request_pause(self, batch_id):
        with self._mutex:
            self._check_open()
            return self.store.pause(batch_id)

    def resume_job(self, job_id):
        with self._mutex:
            self._check_open()
            self._recover_active()
            job = self.store.get_job(job_id)
            if job["state"] in ACTIVE_STATES:
                raise ValueError("Coordinator ownership is active or ambiguous; manual reconciliation required")
            if job.get("attempts") and not job.get("session_id"):
                raise ValueError("Saved session ID unavailable; never start a replacement session, reconcile manually")
            if job.get("attempts"):
                self._original_environment(job)
                if not worker.is_v2_command(self._session_spec(job)['command']):
                    raise ValueError(worker.V1_RESUME_REFUSED)
                if not self._runtime(job)["quiescent"]:
                    raise ValueError("Coordinator ownership is active or ambiguous; no duplicate resume allowed")
            return self.store.resume_job(job_id)

    def stop_job(self, job_id):
        with self._mutex:
            self._check_open()
            job = self.store.get_job(job_id)
            if job["state"] not in ACTIVE_STATES:
                return self.store.cancel_job(job_id)
            if self._metadata(job).get("mode") == "external":
                fd = None
                try:
                    fd = self._lock_job(job)
                except ValueError:
                    pass
                if fd is not None:
                    try:
                        status = self._runtime(job, lock_held=True)
                        if status["quiescent"]:
                            _, directory = self._paths(job)
                            write_json(directory / "stop.json", {"job_id": job_id, "attempt_id": job["attempt_id"], "action": "handoff_cancelled"})
                            self.store.finish_attempt(job_id, job["attempt_id"], "interrupted", "External recovery ended without resolution; original session and budgets preserved")
                            return self.store.get_job(job_id)
                    finally:
                        os.close(fd)
            self.store.pause(job["batch_id"])
            _, directory = self._paths(job)
            if not directory.is_dir():
                self._safe_reconcile(job)
                return self.store.get_job(job_id)
            write_json(directory / "stop.json", {"job_id": job["id"], "attempt_id": job["attempt_id"], "action": "stop"})
            self._safe_reconcile(job)
            return self.store.get_job(job_id)
