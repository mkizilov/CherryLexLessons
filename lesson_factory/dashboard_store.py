import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import core, family


class DashboardError(core.AuthoringError):
    pass


STATES = ("queued", "launching", "running", "retry_wait", "interrupted", "blocked", "succeeded", "cancelled")
ACTIVE_STATES = ("launching", "running")
# Lifetime per-job budget for unattended same-session resumes after verified transient transport failures.
# Never reset: useful progress cannot be proven from coordinator streams.
AUTO_RESUME_DELAYS = (120, 300, 1800)
AUTO_RESUME_LIMIT = len(AUTO_RESUME_DELAYS)


def _positive(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise DashboardError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _text(value: str | None, name: str, maximum: int = 1000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise DashboardError(f"{name} must be nonempty single-line text of at most {maximum} characters")
    if re.search(r"(?i)(?:bearer\s+|(?:api[_-]?key|password|token|secret)\s*[:=]\s*\S+|sk-[a-z0-9]{16,})", value):
        raise DashboardError(f"{name} must not contain credentials")
    return value


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve(strict=True)
        self.directory = core.safe_path(self.root, "work/dashboard")
        core.safe_path(self.root, "work").mkdir(mode=0o700, exist_ok=True)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self._private_directory()
        self.path = core.safe_path(self.root, "work/dashboard/queue.sqlite")
        self._private_file(self.path, create=True)
        with self._connection() as db:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise DashboardError("Dashboard queue requires SQLite WAL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, languages TEXT NOT NULL, families_per_language INTEGER NOT NULL,
                    max_parallel INTEGER NOT NULL CHECK(max_parallel BETWEEN 1 AND 12),
                    paused INTEGER NOT NULL CHECK(paused IN (0,1)), authorization TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    language TEXT NOT NULL, base_checkpoint INTEGER NOT NULL, position INTEGER NOT NULL,
                    run TEXT NOT NULL, path TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','launching','running','retry_wait','interrupted','blocked','succeeded','cancelled')),
                    reason TEXT, blocked_by TEXT REFERENCES jobs(id), attempt_id TEXT,
                    session_id TEXT, resume_authorized INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(language,base_checkpoint), UNIQUE(batch_id,language,position)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), owner TEXT NOT NULL,
                    state TEXT NOT NULL, launch_metadata TEXT NOT NULL DEFAULT '{}', session_id TEXT,
                    started_at REAL NOT NULL, ended_at REAL, exit_code INTEGER,
                    completion_status TEXT CHECK(completion_status IN ('complete','blocked')),
                    completion_reason TEXT, completion_proof TEXT, completion_at REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES batches(id),
                    job_id TEXT REFERENCES jobs(id), attempt_id TEXT REFERENCES attempts(id),
                    kind TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_batch_authorization
                BEFORE UPDATE OF authorization,languages,families_per_language ON batches
                BEGIN SELECT RAISE(ABORT,'Batch authorization is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_job_scope
                BEFORE UPDATE OF batch_id,language,base_checkpoint,position,run,path ON jobs
                BEGIN SELECT RAISE(ABORT,'Job scope is immutable'); END;
                COMMIT;
            """)

    def _private_directory(self) -> None:
        path = core.safe_path(self.root, "work/dashboard")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(descriptor).st_uid != os.getuid():
                raise DashboardError("Dashboard directory must belong to the current user")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)

    def _private_file(self, path: Path, *, create: bool = False) -> None:
        path = core.safe_path(self.root, path.relative_to(self.root).as_posix())
        if not path.exists() and not create:
            return
        if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
            raise DashboardError("Dashboard queue files must be private regular files")
        flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileNotFoundError:
            if create:
                raise
            return
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise DashboardError("Dashboard queue files must be private regular files")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._private_directory()
        for suffix in ("", "-wal", "-shm", "-journal"):
            self._private_file(self.path.with_name(self.path.name + suffix))
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=30000")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _languages(self, languages: list[str]) -> list[str]:
        if not isinstance(languages, (list, tuple)) or not languages:
            raise DashboardError("Select at least one study language")
        codes = [core.language_code(item) for item in languages]
        if len(set(codes)) != len(codes):
            raise DashboardError("Study languages must not be duplicated")
        return codes

    def _facts(self, language: str, *, verify: bool = True) -> list[dict[str, Any]]:
        facts = family.family_registry_facts(self.root, language)
        horizon = max([200] + [row["base_checkpoint"] for row in facts])
        order = family.family_order(min(999999, ((horizon + 199) // 200) * 200))
        if [row["base_checkpoint"] for row in facts] != [item["base_checkpoint"] for item in order[:len(facts)]]:
            raise DashboardError("Family registry is not a finalized authoring prefix")
        if verify:
            for row in facts:
                family.verify_family_pack(self.root, row)
        return facts

    def _available(self, language: str, base: int, vocabulary_count: int, *, allow_run: bool) -> None:
        if vocabulary_count < base + 225:
            raise DashboardError(f"Full vocabulary horizon {base + 225} required; only {vocabulary_count} available")
        if core.safe_path(self.root, f"family_packs/{language}/{base:06d}").exists():
            raise DashboardError("Existing family pack requires explicit registry reconciliation")
        if not allow_run and core.safe_path(self.root, f"family_runs/{language}/{base:06d}").exists():
            raise DashboardError("Existing mutable family requires explicit ownership reconciliation")

    def plan(self, languages: list[str], families_per_language: int) -> dict[str, Any]:
        codes = self._languages(languages)
        count = _positive(families_per_language, "Families per language", 99999)
        results = []
        all_entries = []
        for code in codes:
            reason = None
            facts = []
            vocabulary_count = 0
            try:
                vocabulary_count = len(core.vocabulary(self.root, code))
                facts = self._facts(code, verify=False)
                for row in facts:
                    family.verify_family_pack(self.root, row)
            except (core.AuthoringError, OSError, ValueError, sqlite3.DatabaseError):
                reason = "Cannot verify vocabulary or finalized family registry; explicit reconciliation required"
            length = len(facts) + count
            if length > 99999:
                raise DashboardError("Requested family scope exceeds the supported checkpoint range")
            horizon = min(999999, max(200, ((length + 19) // 20) * 200))
            selected = family.family_order(horizon)[len(facts):length]
            if len(selected) != count:
                raise DashboardError("Requested family prefix is unavailable")
            entries = []
            for item in selected:
                base = item["base_checkpoint"]
                if reason is None:
                    try:
                        self._available(code, base, vocabulary_count, allow_run=False)
                    except (core.AuthoringError, OSError) as exc:
                        reason = str(exc) if isinstance(exc, core.AuthoringError) else "Family filesystem is unavailable"
                entry = {"language": code, "base_checkpoint": base, "run": f"family_runs/{code}/{base:06d}", "path": f"family_packs/{code}/{base:06d}", "state": "blocked" if reason else "queued", "reason": reason}
                entries.append(entry)
                all_entries.append(entry)
            results.append({"language": code, "vocabulary_count": vocabulary_count, "finalized_count": len(facts), "families": entries, "blocked_reason": reason})
        return {"languages": results, "families": all_entries}

    def create_batch(self, languages: list[str], families_per_language: int, max_parallel: int, authorization: dict[str, Any]) -> str:
        codes = self._languages(languages)
        _positive(max_parallel, "Parallel coordinators", 12)
        with self._transaction() as db:
            plan = self.plan(codes, families_per_language)
            scope = [{"language": row["language"], "base_checkpoint": row["base_checkpoint"]} for row in plan["families"]]
            expected = {"paid_scope_acknowledged": True, "families": scope, "max_translation_attempts": 4}
            if not isinstance(authorization, dict) or authorization != expected or authorization.get("paid_scope_acknowledged") is not True or type(authorization.get("max_translation_attempts")) is not int:
                raise DashboardError("Explicit paid scope acknowledgment for the exact families and four total translation attempts is required")
            for row in scope:
                if db.execute("SELECT id FROM jobs WHERE language=? AND base_checkpoint=?", (row["language"], row["base_checkpoint"])).fetchone():
                    raise DashboardError("Family already has a dashboard job; reconcile or resume that exact job")
            batch_id = uuid.uuid4().hex
            now = time.time()
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)", (batch_id, json.dumps(codes), families_per_language, max_parallel, 1, core.canonical(expected).decode(), now, now))
            for position, row in enumerate(plan["families"]):
                job_id = uuid.uuid4().hex
                db.execute("INSERT INTO jobs(id,batch_id,language,base_checkpoint,position,run,path,state,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (job_id, batch_id, row["language"], row["base_checkpoint"], position, row["run"], row["path"], row["state"], row["reason"], now, now))
            self._event(db, batch_id, None, None, "batch_created", {"authorized_jobs": len(scope)})
            return batch_id

    def _event(self, db: sqlite3.Connection, batch_id: str, job_id: str | None, attempt_id: str | None, kind: str, details: dict[str, Any]) -> None:
        db.execute("INSERT INTO events(batch_id,job_id,attempt_id,kind,details,created_at) VALUES (?,?,?,?,?,?)", (batch_id, job_id, attempt_id, kind, core.canonical(details).decode(), time.time()))

    def _batch(self, db: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise DashboardError("Unknown dashboard batch")
        result = dict(row)
        result["languages"] = json.loads(result["languages"])
        result["authorization"] = json.loads(result["authorization"])
        result["paused"] = bool(result["paused"])
        counts = {state: 0 for state in STATES}
        for item in db.execute("SELECT state,COUNT(*) AS count FROM jobs WHERE batch_id=? GROUP BY state", (batch_id,)):
            counts[item["state"]] = item["count"]
        result["state_counts"] = counts
        result["state"] = "succeeded" if counts["succeeded"] == sum(counts.values()) else "paused" if result["paused"] else "running" if counts["running"] + counts["launching"] else "blocked" if counts["blocked"] + counts["interrupted"] + counts["cancelled"] else "queued"
        return result

    def _job(self, db: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise DashboardError("Unknown dashboard job")
        result = dict(row)
        result["resume_authorized"] = bool(result["resume_authorized"])
        result["attempts"] = []
        for attempt in db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY rowid", (job_id,)):
            item = dict(attempt)
            item["launch_metadata"] = json.loads(item["launch_metadata"])
            item["completion_proof"] = json.loads(item["completion_proof"]) if item["completion_proof"] else None
            result["attempts"].append(item)
        result["launch_metadata"] = result["attempts"][-1]["launch_metadata"] if result["attempts"] else {}
        result["authorization"] = json.loads(db.execute("SELECT authorization FROM batches WHERE id=?", (result["batch_id"],)).fetchone()[0])
        return result

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connection() as db:
            db.execute("BEGIN")
            return self._job(db, job_id)

    def snapshot(self) -> dict[str, Any]:
        with self._connection() as db:
            db.execute("BEGIN")
            batches = [self._batch(db, row[0]) for row in db.execute("SELECT id FROM batches ORDER BY created_at,id")]
            jobs = [self._job(db, row[0]) for row in db.execute("SELECT id FROM jobs ORDER BY created_at,position,id")]
            counts = {state: sum(job["state"] == state for job in jobs) for state in STATES}
            return {"batches": batches, "jobs": jobs, "state_counts": counts}

    def active_jobs(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            db.execute("BEGIN")
            return [self._job(db, row[0]) for row in db.execute("SELECT id FROM jobs WHERE state IN ('launching','running') ORDER BY created_at,position,id")]

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self._connection() as db:
            return self._batch(db, batch_id)

    def reserve_external(self, job_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            job = self._job(db, job_id)
            if job["state"] not in ("blocked", "interrupted", "retry_wait") or not job["attempts"] or job["attempts"][-1]["ended_at"] is None:
                raise DashboardError("Only a stopped failed attempt can be handed off")
            if not job["session_id"] or job["attempts"][-1]["session_id"] != job["session_id"]:
                raise DashboardError("The original saved coordinator session is required")
            self._preflight(db, job)
            limit = min(self._batch(db, job["batch_id"])["max_parallel"], db.execute("SELECT MIN(max_parallel) FROM batches WHERE paused=0").fetchone()[0] or 12)
            if db.execute("SELECT COUNT(*) FROM attempts WHERE ended_at IS NULL").fetchone()[0] >= limit:
                raise DashboardError("No coordinator slot available for external recovery; pause and wait for another coordinator to stop")
            now = time.time()
            attempt_id = uuid.uuid4().hex
            metadata = {"mode": "external", "phase": "awaiting_terminal", "previous_attempt_id": job["attempt_id"]}
            db.execute("INSERT INTO attempts(id,job_id,owner,state,session_id,started_at,launch_metadata) VALUES (?,?,?,'launching',?,?,?)", (attempt_id, job_id, "operator", job["session_id"], now, core.canonical(metadata).decode()))
            self._block(db, job, "launching", "External recovery reserved; awaiting tracked terminal and explicit verification")
            db.execute("UPDATE jobs SET attempt_id=?,resume_authorized=0 WHERE id=?", (attempt_id, job_id))
            self._event(db, job["batch_id"], job_id, attempt_id, "external_handoff_reserved", {"session_id": job["session_id"], "previous_attempt_id": job["attempt_id"], "preserve_paid_journals": True})
            return self._job(db, job_id)

    def resolve_verified(self, job_id: str, attempt_id: str, operator_note: str, runtime: dict[str, Any]) -> dict[str, Any]:
        _text(operator_note, "Operator verification note")
        if operator_note is None:
            raise DashboardError("Operator verification note is required")
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id, active=False)
            metadata = attempt["launch_metadata"]
            if not job["session_id"] or attempt["session_id"] != job["session_id"]:
                raise DashboardError("Verified saved coordinator session identity is required")
            if runtime.get("job_id") != job_id or runtime.get("attempt_id") != attempt_id or runtime.get("spec_sha256") != metadata.get("spec_sha256") or not metadata.get("spec_sha256") or runtime.get("quiescent") is not True or runtime.get("started") is not True:
                raise DashboardError("Identity-bound runtime termination verification is required")
            proof = self._proof(job)
            if job["state"] == "succeeded":
                return job
            if job["state"] not in ("blocked", "interrupted", "retry_wait", "launching", "running"):
                raise DashboardError("This job cannot be resolved")
            if attempt["ended_at"] is None and metadata.get("mode") != "external":
                raise DashboardError("Coordinator termination must be confirmed before reconciliation")
            verification = {"kind": "operator_verification", "note": operator_note, "verified_at": time.time(), "proof": proof, "runtime": runtime}
            db.execute("UPDATE attempts SET state='succeeded',ended_at=COALESCE(ended_at,?),launch_metadata=? WHERE id=?", (time.time(), core.canonical({**metadata, "operator_verification": verification}).decode(), attempt_id))
            self._succeed(db, job)
            self._event(db, job["batch_id"], job_id, attempt_id, "operator_verified", verification)
            return self._job(db, job_id)

    def pause(self, batch_id: str) -> None:
        self._pause(batch_id, True)

    def resume(self, batch_id: str) -> None:
        self._pause(batch_id, False)

    def _pause(self, batch_id: str, paused: bool) -> None:
        with self._transaction() as db:
            self._batch(db, batch_id)
            db.execute("UPDATE batches SET paused=?,updated_at=? WHERE id=?", (int(paused), time.time(), batch_id))
            self._event(db, batch_id, None, None, "batch_paused" if paused else "batch_resumed", {})

    def pause_all(self) -> None:
        with self._transaction() as db:
            for row in db.execute("SELECT id FROM batches WHERE paused=0").fetchall():
                db.execute("UPDATE batches SET paused=1,updated_at=? WHERE id=?", (time.time(), row[0]))
                self._event(db, row[0], None, None, "restart_paused", {})

    def set_parallel(self, batch_id: str, n: int) -> None:
        _positive(n, "Parallel coordinators", 12)
        with self._transaction() as db:
            self._batch(db, batch_id)
            db.execute("UPDATE batches SET max_parallel=?,updated_at=? WHERE id=?", (n, time.time(), batch_id))
            self._event(db, batch_id, None, None, "parallel_changed", {"max_parallel": n})

    def _order(self, base: int) -> int:
        return (base - 1) // 200 * 20 + (base - 1) % 200 // 20 + 1 + (10 if base % 20 else 0)

    def _earlier_jobs(self, db: sqlite3.Connection, job: dict[str, Any]) -> list[sqlite3.Row]:
        rows = db.execute("SELECT id,state,base_checkpoint FROM jobs WHERE language=?", (job["language"],))
        return [row for row in rows if self._order(row["base_checkpoint"]) < self._order(job["base_checkpoint"])]

    def _preflight(self, db: sqlite3.Connection, job: dict[str, Any]) -> None:
        if any(row["state"] != "succeeded" for row in self._earlier_jobs(db, job)):
            raise DashboardError("Earlier job must succeed before this family can proceed")
        facts = self._facts(job["language"])
        if any(row["base_checkpoint"] == job["base_checkpoint"] for row in facts):
            if not job["attempts"] or not job["session_id"]:
                raise DashboardError("Finalized family recovery requires the original saved coordinator session")
            self._proof(job)
            return
        family._prefix(facts, job["base_checkpoint"])
        if job["attempts"] and job["session_id"] and core.safe_path(self.root, job["path"]).exists():
            self._publication_recovery(job)
            return
        self._available(job["language"], job["base_checkpoint"], len(core.vocabulary(self.root, job["language"])), allow_run=bool(job["attempts"]))

    def _publication_recovery(self, job: dict[str, Any]) -> None:
        if core.safe_path(self.root, job["run"]).exists():
            raise DashboardError("Both mutable run and unregistered family pack exist; explicit reconciliation required")
        manifest = core.read_json(core.safe_path(self.root, f"{job['path']}/manifest.json"))
        receipt = core.read_json(core.safe_path(self.root, f"work/family-publications/{job['language']}/{job['base_checkpoint']:06d}/receipt.json"))
        expected = {"schema": 5, "run": job["run"], "path": job["path"], "manifest_sha256": family._hash(manifest), "context_hash": manifest.get("context_hash"), "files": manifest.get("files"), "dependencies": manifest.get("dependencies"), "registry_facts": manifest.get("registry_facts")}
        if receipt != expected:
            raise DashboardError("Unregistered family pack does not match its original publication receipt")
        family._verify_manifest(self.root, job["path"], manifest)
        family._load_family(self.root, job["run"], core.safe_path(self.root, job["path"]))

    def _block(self, db: sqlite3.Connection, job: dict[str, Any], state: str, reason: str | None) -> None:
        now = time.time()
        db.execute("UPDATE jobs SET state=?,reason=?,blocked_by=NULL,updated_at=? WHERE id=?", (state, reason, now, job["id"]))
        later = db.execute("SELECT id,base_checkpoint FROM jobs WHERE language=? AND state IN ('queued','blocked','retry_wait')", (job["language"],)).fetchall()
        for row in later:
            if self._order(row["base_checkpoint"]) > self._order(job["base_checkpoint"]):
                db.execute("UPDATE jobs SET state='blocked',reason='Earlier family job requires reconciliation',blocked_by=?,updated_at=? WHERE id=?", (job["id"], now, row["id"]))

    def claim_next(self, owner: str) -> dict[str, Any] | None:
        _text(owner, "Owner", 200)
        if owner is None:
            raise DashboardError("Owner is required")
        with self._transaction() as db:
            limit = db.execute("SELECT MIN(max_parallel) FROM batches b WHERE paused=0 AND EXISTS (SELECT 1 FROM jobs j WHERE j.batch_id=b.id AND j.state NOT IN ('succeeded','cancelled'))").fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM attempts WHERE ended_at IS NULL").fetchone()[0]
            if limit is None or active >= limit:
                return None
            candidates = db.execute("SELECT j.id FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.paused=0 AND j.state='queued' ORDER BY b.created_at,j.position,j.id").fetchall()
            for candidate in candidates:
                job = self._job(db, candidate[0])
                if db.execute("SELECT 1 FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.language=? AND a.ended_at IS NULL", (job["language"],)).fetchone():
                    continue
                if any(row["state"] != "succeeded" for row in self._earlier_jobs(db, job)):
                    continue
                try:
                    self._preflight(db, job)
                except (core.AuthoringError, OSError, ValueError, sqlite3.DatabaseError):
                    self._block(db, job, "blocked", "Family preflight failed; explicit reconciliation required")
                    self._event(db, job["batch_id"], job["id"], None, "preflight_blocked", {})
                    continue
                attempt_id = uuid.uuid4().hex
                now = time.time()
                db.execute("INSERT INTO attempts(id,job_id,owner,state,session_id,started_at) VALUES (?,?,?,'launching',?,?)", (attempt_id, job["id"], owner, job["session_id"], now))
                db.execute("UPDATE jobs SET state='launching',attempt_id=?,reason=NULL,updated_at=? WHERE id=?", (attempt_id, now, job["id"]))
                self._event(db, job["batch_id"], job["id"], attempt_id, "attempt_claimed", {"owner": owner})
                return self._job(db, job["id"])
            return None

    def _attempt(self, db: sqlite3.Connection, job_id: str, attempt_id: str, *, active: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        job = self._job(db, job_id)
        if job["attempt_id"] != attempt_id or not job["attempts"] or job["attempts"][-1]["id"] != attempt_id:
            raise DashboardError("Job and current attempt identity do not match")
        attempt = job["attempts"][-1]
        if active and (attempt["ended_at"] is not None or job["state"] not in ACTIVE_STATES):
            raise DashboardError("Attempt is not active")
        return job, attempt

    def attach_launch(self, job_id: str, attempt_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(metadata, dict) or len(core.canonical(metadata)) > 65536:
            raise DashboardError("Launch metadata must be a bounded JSON object")
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id)
            merged = dict(attempt["launch_metadata"])
            for key in ("supervisor", "child", "attempt_dir", "spec_sha256", "supervisor_reconciliation"):
                if key in merged and key in metadata and merged[key] != metadata[key]:
                    raise DashboardError("Recorded launch identity cannot be replaced")
            merged.update(metadata)
            state = "running" if merged.get("child") else job["state"]
            db.execute("UPDATE attempts SET launch_metadata=?,state=? WHERE id=?", (core.canonical(merged).decode(), state, attempt_id))
            db.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?", (state, time.time(), job_id))
            self._event(db, job["batch_id"], job_id, attempt_id, "launch_attached", {})
            return self._job(db, job_id)

    def reconcile_supervisor(self, job_id: str, attempt_id: str, request: dict[str, Any], proof: dict[str, Any] | None = None) -> dict[str, Any]:
        from .dashboard_supervisor import fingerprint

        if not isinstance(request, dict) or len(core.canonical(request)) > 16384 or set(request) != {"job_id", "attempt_id", "session_id", "spec_sha256", "old_supervisor", "new_supervisor", "operator_note", "attestation"}:
            raise DashboardError("Bounded explicit supervisor reconciliation request required")
        if _text(request.get("operator_note"), "Operator reconciliation note") is None:
            raise DashboardError("Operator reconciliation note is required")
        if request.get("job_id") != job_id or request.get("attempt_id") != attempt_id:
            raise DashboardError("Reconciliation request scope mismatch")
        request_hash = fingerprint(request)
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id, active=False)
            metadata = attempt["launch_metadata"]
            previous = db.execute("SELECT details FROM events WHERE job_id=? AND attempt_id=? AND kind='supervisor_reconciled'", (job_id, attempt_id)).fetchall()
            if previous:
                record = json.loads(previous[0]["details"])
                if len(previous) != 1 or record.get("request_sha256") != request_hash or metadata.get("supervisor") != request["new_supervisor"] or metadata.get("child") != record["proof"]["child"] or metadata.get("supervisor_reconciliation") != request_hash:
                    raise DashboardError("Supervisor reconciliation already exists with different identity or evidence")
                return job
            if not isinstance(proof, dict) or len(core.canonical(proof)) > 32768:
                raise DashboardError("Runner-verified supervisor reconciliation proof required")
            if job["state"] not in ACTIVE_STATES or attempt["ended_at"] is not None or metadata != proof.get("expected_metadata") or metadata.get("phase") != "prepared" or set(metadata) - {"attempt_dir", "spec_sha256", "phase", "supervisor", "diagnostic"}:
                raise DashboardError("Prepared launch metadata changed; supervisor cannot be reconciled")
            if not request.get("session_id") or request["session_id"] != job["session_id"] or request["session_id"] != attempt["session_id"] or request.get("spec_sha256") != metadata.get("spec_sha256") or not metadata.get("spec_sha256") or request.get("old_supervisor") != metadata.get("supervisor") or not request.get("old_supervisor") or request.get("new_supervisor") == request["old_supervisor"] or not request.get("new_supervisor"):
                raise DashboardError("Supervisor reconciliation identity mismatch")
            alive = all(proof.get(key) == "alive" for key in ("supervisor_state", "child_state", "group_state")) and proof.get("lock") == "held" and proof.get("phase") == "running"
            stopped = all(proof.get(key) == "dead" for key in ("supervisor_state", "child_state", "group_state")) and proof.get("lock") == "free" and proof.get("phase") == "stopped"
            hashes = proof.get("hashes")
            if proof.get("old_supervisor_state") != "dead" or not proof.get("child") or not (alive or stopped) or not isinstance(hashes, dict) or hashes.get("spec") != metadata["spec_sha256"] or any(not isinstance(hashes.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", hashes[key]) for key in ("spec", "environment", "process")) or (alive and hashes.get("exit") is not None) or (stopped and (not isinstance(hashes.get("exit"), str) or not re.fullmatch(r"[0-9a-f]{64}", hashes["exit"]))):
                raise DashboardError("Coherent identity-bound runtime and evidence hashes required")
            attestation = request.get("attestation")
            if not isinstance(attestation, dict) or attestation.get("kind") != "operator_attested_prelaunch" or any(attestation.get(key) != request[key] for key in ("job_id", "attempt_id", "session_id", "spec_sha256", "old_supervisor")):
                raise DashboardError("Identity-bound operator attestation required")
            now = time.time()
            record = {"kind": "operator_supervisor_reconciliation", "recorded_at": now, "request_sha256": request_hash, "request": request, "proof": proof}
            merged = {**metadata, "supervisor": request["new_supervisor"], "child": proof["child"], "phase": proof["phase"], "supervisor_reconciliation": request_hash}
            merged.pop("diagnostic", None)
            db.execute("UPDATE attempts SET launch_metadata=?,state='running' WHERE id=? AND launch_metadata=?", (core.canonical(merged).decode(), attempt_id, core.canonical(metadata).decode()))
            if db.execute("SELECT changes()").fetchone()[0] != 1:
                raise DashboardError("Launch metadata changed before adoption")
            db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (now, job_id))
            self._event(db, job["batch_id"], job_id, attempt_id, "supervisor_reconciled", record)
            return self._job(db, job_id)

    def set_session(self, job_id: str, attempt_id: str, session_id: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
            raise DashboardError("Invalid coordinator session identity")
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id)
            if job["session_id"] is not None and job["session_id"] != session_id:
                raise DashboardError("Coordinator session cannot change across attempts")
            db.execute("UPDATE jobs SET session_id=?,state='running',updated_at=? WHERE id=?", (session_id, time.time(), job_id))
            db.execute("UPDATE attempts SET session_id=?,state='running' WHERE id=?", (session_id, attempt["id"]))
            self._event(db, job["batch_id"], job_id, attempt_id, "session_attached", {})
            return self._job(db, job_id)

    def _proof(self, job: dict[str, Any]) -> dict[str, Any]:
        facts = self._facts(job["language"])
        row = next((item for item in facts if item["base_checkpoint"] == job["base_checkpoint"]), None)
        if row is None or row["path"] != job["path"]:
            raise DashboardError("Matching finalized family registry receipt is required")
        if core.safe_path(self.root, job["run"]).exists():
            raise DashboardError("Mutable family still exists beside finalized family")
        manifest = family.verify_family_pack(self.root, row)
        if manifest.get("schema") != 5 or manifest.get("locales") != list(core.learner_locales(job["language"])):
            raise DashboardError("Complete five-stage eleven-locale family is required")
        receipt = core.read_json(core.safe_path(self.root, f"work/family-publications/{job['language']}/{job['base_checkpoint']:06d}/receipt.json"))
        expected = {"schema": 5, "run": job["run"], "path": job["path"], "manifest_sha256": family._hash(manifest), "context_hash": manifest.get("context_hash"), "files": manifest.get("files"), "dependencies": manifest.get("dependencies"), "registry_facts": manifest.get("registry_facts")}
        if receipt != expected:
            raise DashboardError("Finalized family differs from its original publication receipt")
        return {"language": job["language"], "base_checkpoint": job["base_checkpoint"], "path": row["path"], "context_hash": row["context_hash"], "manifest_sha256": row["manifest_sha256"]}

    def record_completion(self, job_id: str, attempt_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
        if status not in ("complete", "blocked"):
            raise DashboardError("Completion status must be complete or blocked")
        _text(reason, "Reason")
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id)
            proof = self._proof(job) if status == "complete" else None
            if attempt["completion_status"] is not None:
                if attempt["completion_status"] != status or attempt["completion_reason"] != reason or attempt["completion_proof"] != proof:
                    raise DashboardError("Attempt completion receipt is immutable")
                return job
            db.execute("UPDATE attempts SET completion_status=?,completion_reason=?,completion_proof=?,completion_at=? WHERE id=?", (status, reason, core.canonical(proof).decode() if proof else None, time.time(), attempt_id))
            self._event(db, job["batch_id"], job_id, attempt_id, "completion_recorded", {"status": status, "reason": reason})
            return self._job(db, job_id)

    def _succeed(self, db: sqlite3.Connection, job: dict[str, Any]) -> None:
        db.execute("UPDATE jobs SET state='succeeded',reason=NULL,blocked_by=NULL,updated_at=? WHERE id=?", (time.time(), job["id"]))
        db.execute("UPDATE jobs SET state='queued',reason=NULL,blocked_by=NULL,updated_at=? WHERE blocked_by=? AND state='blocked'", (time.time(), job["id"]))

    def abort_unlaunched(self, job_id: str, attempt_id: str, session_id: str, spec_hash: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", spec_hash):
            raise DashboardError("Verified unlaunched specification hash required")
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id)
            if job["state"] != "launching" or set(attempt["launch_metadata"]) - {"diagnostic"} or attempt["completion_status"] is not None or not session_id or job["session_id"] != session_id or attempt["session_id"] != session_id:
                raise DashboardError("Attempt no longer satisfies unlaunched recovery conditions")
            previous = job["attempts"][:-1]
            if not previous or previous[-1]["ended_at"] is None or previous[-1]["session_id"] != session_id:
                raise DashboardError("Stopped predecessor with the same session is required")
            metadata = {**attempt["launch_metadata"], "aborted_before_launch": {"spec_sha256": spec_hash, "session_id": session_id}}
            reason = "Aborted before supervisor launch; original session and paid budgets preserved"
            db.execute("UPDATE attempts SET state='interrupted',ended_at=?,launch_metadata=? WHERE id=?", (time.time(), core.canonical(metadata).decode(), attempt_id))
            self._block(db, job, "interrupted", reason)
            self._event(db, job["batch_id"], job_id, attempt_id, "unlaunched_aborted", metadata)
            return self._job(db, job_id)

    def finish_attempt(self, job_id: str, attempt_id: str, state: str, reason: str | None = None, exit_code: int | None = None) -> dict[str, Any]:
        if state not in ("succeeded", "blocked", "interrupted", "cancelled", "retry_wait"):
            raise DashboardError("Invalid finished attempt state")
        _text(reason, "Reason")
        if exit_code is not None and type(exit_code) is not int:
            raise DashboardError("Exit code must be an integer or null")
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id)
            if state == "succeeded":
                try:
                    if not job["session_id"] or attempt["session_id"] != job["session_id"]:
                        raise DashboardError("Verified saved coordinator session identity is required")
                    if attempt["completion_status"] != "complete" or attempt["completion_proof"] != self._proof(job):
                        raise DashboardError("Verified identity-bound completion receipt is required")
                except (core.AuthoringError, OSError, ValueError, sqlite3.DatabaseError):
                    state = "blocked"
                    reason = "Verified complete receipt and unchanged finalized family are required"
            db.execute("UPDATE attempts SET state=?,ended_at=?,exit_code=? WHERE id=?", (state, time.time(), exit_code, attempt_id))
            if state == "succeeded":
                self._succeed(db, job)
            else:
                self._block(db, job, state, reason)
            self._event(db, job["batch_id"], job_id, attempt_id, "attempt_finished", {"state": state, "reason": reason, "exit_code": exit_code})
            return self._job(db, job_id)

    def reconcile(self, job_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            job = self._job(db, job_id)
            if job["attempts"] and job["attempts"][-1]["ended_at"] is None:
                raise DashboardError("Coordinator termination must be confirmed before reconciliation")
            if not job["attempts"]:
                raise DashboardError("Job has no attempt to reconcile")
            attempt = job["attempts"][-1]
            if not job["session_id"] or attempt["session_id"] != job["session_id"]:
                raise DashboardError("Verified saved coordinator session identity is required")
            if attempt["completion_status"] != "complete" or attempt["completion_proof"] != self._proof(job):
                raise DashboardError("Verified identity-bound completion receipt is required")
            self._succeed(db, job)
            self._event(db, job["batch_id"], job_id, attempt["id"], "job_reconciled", {})
            return self._job(db, job_id)

    def cli_startup_retry(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            rows = db.execute("SELECT details FROM events WHERE job_id=? AND kind='cli_startup_retry_authorized' ORDER BY id", (job_id,)).fetchall()
            if len(rows) > 1:
                raise DashboardError('Conflicting startup retry authorizations')
            return json.loads(rows[0][0]) if rows else None

    def authorize_cli_startup_retry(self, job_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        """Internal operator bridge; runtime proof must be obtained under the job lock."""
        with self._transaction() as db:
            job = self._job(db, job_id)
            existing = db.execute("SELECT details FROM events WHERE job_id=? AND kind='cli_startup_retry_authorized'", (job_id,)).fetchall()
            if existing:
                if len(existing) == 1 and json.loads(existing[0][0]) == receipt:
                    return job
                raise DashboardError('Different startup retry authorization already exists')
            if job['state'] != 'blocked' or job['session_id'] is not None or len(job['attempts']) != 1 or job['attempts'][0]['ended_at'] is None or job['attempts'][0]['completion_status'] is not None:
                raise DashboardError('Only a stopped first no-session CLI failure qualifies')
            proof = receipt.get('proof', {})
            worker = receipt.get('worker', {})
            if (set(receipt) != {'proof', 'worker'} or proof.get('kind') != 'exact-cli-variant-rejection-v1' or
                    any(proof.get(key) != job[key] for key in ('batch_id', 'attempt_id')) or proof.get('job_id') != job_id or
                    proof.get('spec_sha256') != job['launch_metadata'].get('spec_sha256') or
                    not isinstance(proof.get('evidence_sha256'), dict) or len(proof['evidence_sha256']) != 6 or
                    not isinstance(worker.get('version'), str) or not re.fullmatch(r'2\.\d+\.\d+', worker['version']) or not isinstance(worker.get('path'), str) or not Path(worker['path']).is_absolute() or
                    not isinstance(worker.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', worker['sha256'])):
                raise DashboardError('Invalid identity-bound startup recovery proof')
            self._preflight(db, job)
            db.execute("UPDATE jobs SET state='queued',reason=NULL,blocked_by=NULL,resume_authorized=1,updated_at=? WHERE id=?", (time.time(), job_id))
            self._event(db, job['batch_id'], job_id, job['attempt_id'], 'cli_startup_retry_authorized', receipt)
            return self._job(db, job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            job = self._job(db, job_id)
            if job["state"] not in ("blocked", "interrupted", "retry_wait"):
                raise DashboardError("Only blocked or interrupted jobs can be explicitly resumed")
            if job["attempts"] and job["attempts"][-1]["ended_at"] is None:
                raise DashboardError("Active or uncertain coordinator cannot be resumed")
            if job["attempts"] and not job["session_id"]:
                raise DashboardError("Prior coordinator has no saved session; explicit external reconciliation is required")
            self._preflight(db, job)
            db.execute("UPDATE jobs SET state='queued',reason=NULL,blocked_by=NULL,resume_authorized=1,updated_at=? WHERE id=?", (time.time(), job_id))
            self._event(db, job["batch_id"], job_id, job["attempt_id"], "job_resume_authorized", {"preserve_paid_journals": True})
            return self._job(db, job_id)

    def _auto_history(self, db: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        history: dict[str, Any] = {"scheduled": [], "authorized": set(), "exhausted": set()}
        for row in db.execute("SELECT id,attempt_id,kind,details FROM events WHERE job_id=? AND kind IN ('auto_resume_scheduled','auto_resume_authorized','auto_resume_exhausted') ORDER BY id", (job_id,)):
            details = json.loads(row["details"])
            if row["kind"] == "auto_resume_scheduled":
                history["scheduled"].append({**details, "event_id": row["id"], "attempt_id": row["attempt_id"]})
            elif row["kind"] == "auto_resume_authorized":
                history["authorized"].add(details.get("schedule_event_id"))
            else:
                history["exhausted"].add(row["attempt_id"])
        return history

    def auto_resume_status(self, job_id: str) -> dict[str, Any]:
        """Persisted automatic-resume ledger: lifetime count never resets; the latest schedule is pending until authorized."""
        with self._connection() as db:
            db.execute("BEGIN")
            history = self._auto_history(db, job_id)
            latest = history["scheduled"][-1] if history["scheduled"] else None
            pending = latest if latest is not None and latest["event_id"] not in history["authorized"] else None
            return {"count": len(history["scheduled"]), "limit": AUTO_RESUME_LIMIT, "pending": pending, "exhausted_attempts": sorted(history["exhausted"]), "handled_attempts": sorted({item["attempt_id"] for item in history["scheduled"]} | history["exhausted"])}

    def auto_resume_candidates(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT j.id FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.paused=0 AND j.state IN ('interrupted','retry_wait') AND j.session_id IS NOT NULL AND j.attempt_id IS NOT NULL ORDER BY b.created_at,j.position,j.id").fetchall()
            return [self._job(db, row[0]) for row in rows]

    def schedule_auto_resume(self, job_id: str, attempt_id: str, evidence: dict[str, Any], *, adopt: bool = False) -> dict[str, Any]:
        """End or adopt a verified transient-interrupted attempt into a bounded, persisted, same-session retry wait."""
        if not isinstance(evidence, dict) or set(evidence) != {"signature", "evidence_sha256", "spec_sha256", "session_id", "exit_code"} or not isinstance(evidence["signature"], str) or not re.fullmatch(r"[a-z0-9_]{1,64}", evidence["signature"]) or any(not isinstance(evidence[key], str) or not re.fullmatch(r"[0-9a-f]{64}", evidence[key]) for key in ("evidence_sha256", "spec_sha256")) or type(evidence["exit_code"]) is not int:
            raise DashboardError("Verified transient interruption evidence required")
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id, active=False)
            metadata = attempt["launch_metadata"]
            if not job["session_id"] or evidence["session_id"] != job["session_id"] or attempt["session_id"] != job["session_id"] or metadata.get("spec_sha256") != evidence["spec_sha256"] or metadata.get("mode") is not None or metadata.get("aborted_before_launch") is not None or attempt["completion_status"] is not None:
                raise DashboardError("Automatic resume requires the exact saved session and unreceipted managed attempt")
            history = self._auto_history(db, job_id)
            if attempt_id in history["exhausted"] or any(item["attempt_id"] == attempt_id for item in history["scheduled"]):
                return job
            if adopt:
                if job["state"] != "interrupted" or attempt["ended_at"] is None or self._batch(db, job["batch_id"])["paused"]:
                    raise DashboardError("Only an ended interrupted attempt of an unpaused batch can be adopted")
            elif job["state"] not in ACTIVE_STATES or attempt["ended_at"] is not None:
                raise DashboardError("Attempt is not active")
            now = time.time()
            count = len(history["scheduled"])
            label = f"Verified transient provider interruption ({evidence['signature']})"
            if count >= AUTO_RESUME_LIMIT:
                state, reason = "interrupted", f"{label}; automatic same-session resume limit {count}/{AUTO_RESUME_LIMIT} reached; explicit review required"
                kind, details = "auto_resume_exhausted", {**evidence, "count": count, "limit": AUTO_RESUME_LIMIT}
            else:
                due_at = now + AUTO_RESUME_DELAYS[count]
                state = "retry_wait"
                reason = f"{label}; automatic same-session resume {count + 1}/{AUTO_RESUME_LIMIT} due {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(due_at))}; waits while the batch is paused, never after cancel"
                kind, details = "auto_resume_scheduled", {**evidence, "count": count + 1, "limit": AUTO_RESUME_LIMIT, "due_at": due_at, "adopted": adopt}
            if attempt["ended_at"] is None:
                db.execute("UPDATE attempts SET state=?,ended_at=?,exit_code=? WHERE id=?", (state, now, evidence["exit_code"], attempt_id))
                self._event(db, job["batch_id"], job_id, attempt_id, "attempt_finished", {"state": state, "reason": reason, "exit_code": evidence["exit_code"]})
            self._block(db, job, state, reason)
            self._event(db, job["batch_id"], job_id, attempt_id, kind, details)
            return self._job(db, job_id)

    def authorize_auto_resume(self, job_id: str, attempt_id: str, schedule_event_id: int, evidence: dict[str, Any]) -> dict[str, Any]:
        """Queue the exact saved session once its persisted due time passed; preflight and pause/cancel still apply."""
        with self._transaction() as db:
            job, attempt = self._attempt(db, job_id, attempt_id, active=False)
            history = self._auto_history(db, job_id)
            latest = history["scheduled"][-1] if history["scheduled"] else None
            if job["state"] != "retry_wait" or attempt["ended_at"] is None or latest is None or latest["event_id"] != schedule_event_id or latest["attempt_id"] != attempt_id or schedule_event_id in history["authorized"]:
                raise DashboardError("No pending automatic resume for this exact attempt")
            if any(latest.get(key) != evidence.get(key) for key in ("signature", "evidence_sha256", "spec_sha256", "session_id")) or job["session_id"] != latest["session_id"] or attempt["session_id"] != job["session_id"]:
                raise DashboardError("Automatic resume evidence changed; explicit review required")
            if self._batch(db, job["batch_id"])["paused"]:
                raise DashboardError("Paused batches never start automatic resumes")
            if time.time() < latest["due_at"]:
                raise DashboardError("Automatic resume is not due yet")
            try:
                self._preflight(db, job)
            except (core.AuthoringError, OSError, ValueError, sqlite3.DatabaseError):
                self._block(db, job, "blocked", "Automatic resume preflight failed; explicit reconciliation required")
                self._event(db, job["batch_id"], job_id, attempt_id, "auto_resume_authorized", {"schedule_event_id": schedule_event_id, "preflight": "blocked"})
                return self._job(db, job_id)
            reason = f"Automatic same-session resume {latest['count']}/{latest['limit']} authorized at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} after verified transient provider interruption ({latest['signature']})"
            db.execute("UPDATE jobs SET state='queued',reason=?,blocked_by=NULL,resume_authorized=1,updated_at=? WHERE id=?", (reason, time.time(), job_id))
            self._event(db, job["batch_id"], job_id, attempt_id, "auto_resume_authorized", {"schedule_event_id": schedule_event_id, "session_id": job["session_id"], "count": latest["count"], "preserve_paid_journals": True})
            return self._job(db, job_id)

    def reject_auto_resume(self, job_id: str, attempt_id: str) -> dict[str, Any]:
        """A pending automatic resume whose evidence no longer verifies returns to explicit review, permanently for this attempt."""
        with self._transaction() as db:
            job, _ = self._attempt(db, job_id, attempt_id, active=False)
            if job["state"] != "retry_wait":
                raise DashboardError("No pending automatic resume for this exact attempt")
            self._block(db, job, "interrupted", "Automatic resume evidence changed or could not be re-verified; explicit review required")
            self._event(db, job["batch_id"], job_id, attempt_id, "auto_resume_exhausted", {"rejected": True})
            return self._job(db, job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            job = self._job(db, job_id)
            if job["state"] in ACTIVE_STATES or job["state"] == "succeeded" or job["attempts"] and job["attempts"][-1]["ended_at"] is None:
                raise DashboardError("Only nonactive unfinished jobs can be cancelled")
            self._block(db, job, "cancelled", "Cancelled explicitly; descendants remain blocked")
            self._event(db, job["batch_id"], job_id, job["attempt_id"], "job_cancelled", {})
            return self._job(db, job_id)

    def events(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id") if job_id is None else db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,))
            return [{**dict(row), "details": json.loads(row["details"])} for row in rows]
