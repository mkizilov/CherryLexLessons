import csv
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lesson_factory import core, family
from lesson_factory.dashboard_store import DashboardError, Store


class DashboardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for code in ("de", "fr", "ru"):
            self.vocabulary(code, 1000)
        self.store = Store(self.root)

    def vocabulary(self, code, count):
        name = core.LANGUAGES[code]
        folder = self.root / "reference/vocabulary" / name
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{name.lower()}_vocabulary.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Rank", "Word"])
            writer.writerows((rank, f"word{rank}") for rank in range(1, count + 1))

    def authorization(self, languages, n):
        plan = self.store.plan(languages, n)
        return {"paid_scope_acknowledged": True, "max_translation_attempts": 4, "families": [{"language": row["language"], "base_checkpoint": row["base_checkpoint"]} for row in plan["families"]]}

    def batch(self, languages=None, n=2, parallel=2):
        languages = languages or ["de"]
        return self.store.create_batch(languages, n, parallel, self.authorization(languages, n))

    def claim(self, languages=None, n=2, parallel=2):
        batch_id = self.batch(languages, n, parallel)
        self.store.resume(batch_id)
        return self.store.claim_next("synthetic-owner")

    def publish(self, code="de", base=20):
        (self.root / "reference/formats").mkdir(parents=True, exist_ok=True)
        (self.root / "reference/formats/spec.txt").write_text("Synthetic format")
        (self.root / "reference/grammar").mkdir(parents=True, exist_ok=True)
        (self.root / "reference/grammar/grammar.sqlite").write_bytes(b"synthetic")
        run = family.prepare_family(self.root, code, base)
        for stage in family.STAGES:
            brief = {"topic": "Synthetic fixture", "objective": "Synthetic regression", "sources": [{"reference": "synthetic:test", "justification": "Offline test"}], "prerequisites": [], "lexical_audit": [{"target_form": "word1", "known_rank": 1, "justification": "Synthetic canonical word"}]}
            if stage:
                brief["extension"] = {"mode": "practice", "rationale": "Synthetic practice"}
            core.write_json(self.root / run / f"stage-{stage}/brief.json", brief)
            for locale in core.learner_locales(code):
                folder = family.family_locale_path(self.root, run, locale, stage)
                (folder / "lesson.md").write_text("" if stage else "Synthetic word1")
                (folder / "quiz.md").write_text("Synthetic quiz")
                core.write_json(folder / "adaptation-notes.json", [])
        validation = {"ok": True, "family_contract": ["synthetic"], "stages": [{"contract": {"questions": [{"id": f"q-{stage}-{index}"} for index in range(25)]}} for stage in family.STAGES]}
        with patch.object(family, "validate_family", return_value=validation):
            for role in core.review_roles(code) + ["final"]:
                stages = [{"stage": stage, "checklist": ["Synthetic check"], "findings": ["Synthetic test only"], "question_checks": [{"id": f"q-{stage}-{index}", "status": "approved", "rationale": "Synthetic evidence", "answer": "word1", "alternatives": "Synthetic", "teaching_page": "Synthetic"} for index in range(25)]} for stage in family.STAGES]
                report = {"status": "approved", "reviewer_session": f"synthetic:{role}", "checklist": ["Synthetic"], "findings": ["Synthetic only"], "stages": stages, "binding": family.family_binding(self.root, run, role)}
                family.submit_family_review(self.root, run, role, report)
            return family.finalize_family(self.root, run)

    def test_spec_only_launch_failure_can_abort_without_launching_or_changing_session(self):
        from lesson_factory.dashboard_runner import Runner
        from lesson_factory.dashboard_supervisor import fingerprint, isolated_environment, private_directory, saved_environment, write_json
        job = self.claim(n=1)
        home = private_directory(self.store.directory / 'jobs' / job['id'] / 'home')
        directory = private_directory(home.parent / 'attempts' / job['attempt_id'])
        spec = {'root': str(self.root), 'job_id': job['id'], 'attempt_id': job['attempt_id'], 'command': [sys.executable, 'run', '--standalone', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas#xhigh', 'fixture'], 'environment': saved_environment(isolated_environment(home), home)}
        write_json(directory / 'spec.json', spec)
        self.store.attach_launch(job['id'], job['attempt_id'], {'spec_sha256': fingerprint(spec)})
        self.store.set_session(job['id'], job['attempt_id'], 'ses_preserved')
        self.store.finish_attempt(job['id'], job['attempt_id'], 'interrupted', 'Synthetic stopped predecessor')
        self.store.resume_job(job['id'])
        job = self.store.claim_next('synthetic-owner')
        runner = Runner(self.root, self.store)
        self.addCleanup(runner.close)
        with patch('lesson_factory.dashboard_runner.worker.verify_binary', return_value='2.0.29'), patch('lesson_factory.dashboard_runner.worker.require_session'), patch('lesson_factory.dashboard_runner.shutil.which', return_value=sys.executable), patch.dict(os.environ, {'OPENCODE_BINARY': sys.executable}), patch.object(self.store, 'attach_launch', side_effect=ValueError('Synthetic pre-spawn failure')), patch('lesson_factory.dashboard_runner.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'pre-spawn'):
                runner._launch(job)
            spawn.assert_not_called()
        _, directory = runner._paths(job)
        (directory / 'process.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'spec-only'):
            runner.abort_unlaunched(job['id'])
        (directory / 'process.json').unlink()
        self.store.attach_launch(job['id'], job['attempt_id'], {'diagnostic': 'Synthetic original failure'})
        result = runner.abort_unlaunched(job['id'])
        self.assertEqual(result['attempts'][-1]['launch_metadata']['diagnostic'], 'Synthetic original failure')
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['session_id'], 'ses_preserved')
        details = runner.recovery_details(job['id'])
        self.assertTrue(details['process']['quiescent'])
        self.assertFalse(details['process']['started'])
        self.assertFalse(details['can_resolve'])
        self.assertEqual(runner.resume_job(job['id'])['state'], 'queued')
        self.assertEqual({p.name for p in directory.iterdir()}, {'spec.json'})

    def test_private_wal_full_and_no_family_mutation(self):
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        with self.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
            for suffix in ("-wal", "-shm"):
                path = Path(str(self.store.path) + suffix)
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "family-registry.sqlite").exists())
        self.assertFalse((self.root / "registry.sqlite").exists())
        self.assertFalse((self.root / "family_runs").exists())

    def test_authorization_exact_scope_immutable(self):
        authorization = self.authorization(["de"], 2)
        for invalid in (None, True, {}, {**authorization, "paid_scope_acknowledged": False}, {**authorization, "paid_scope_acknowledged": 1}, {**authorization, "max_translation_attempts": 5}, {**authorization, "families": authorization["families"][:1]}):
            with self.subTest(invalid=invalid), self.assertRaises(DashboardError):
                self.store.create_batch(["de"], 2, 1, invalid)
        self.assertEqual(self.store.snapshot()["jobs"], [])
        batch_id = self.batch()
        before = self.store.snapshot()["batches"][0]["authorization"]
        self.store.set_parallel(batch_id, 12)
        after = self.store.snapshot()["batches"][0]
        self.assertEqual(after["authorization"], before)
        self.assertTrue(after["paused"])
        self.assertEqual(len(self.store.snapshot()["jobs"]), 2)
        with self.store._transaction() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE batches SET authorization='{}' WHERE id=?", (batch_id,))

    def test_exact_order_no_skip_and_unowned_language_isolation(self):
        plan = self.store.plan(["de"], 22)
        self.assertEqual([row["base_checkpoint"] for row in plan["families"]], list(range(20, 201, 20)) + list(range(10, 200, 20)) + [220, 240])
        unowned = self.root / "family_runs/ru/000020"
        unowned.mkdir(parents=True)
        evidence = unowned / "untouched.txt"
        evidence.write_text("pilot")
        batch_id = self.batch(["ru", "de"], 2, 2)
        jobs = self.store.snapshot()["jobs"]
        russian = [row for row in jobs if row["language"] == "ru"]
        self.assertEqual([row["base_checkpoint"] for row in russian], [20, 40])
        self.assertTrue(all(row["state"] == "blocked" for row in russian))
        self.assertIn("ownership", russian[0]["reason"])
        self.store.resume(batch_id)
        self.assertEqual(self.store.claim_next("worker")["language"], "de")
        self.assertEqual(evidence.read_text(), "pilot")

    def test_full_horizon_preserves_blocked_tail(self):
        self.vocabulary("de", 250)
        self.batch(["de"], 3, 1)
        jobs = self.store.snapshot()["jobs"]
        self.assertEqual([job["base_checkpoint"] for job in jobs], [20, 40, 60])
        self.assertEqual([job["state"] for job in jobs], ["queued", "blocked", "blocked"])
        self.assertIn("265", jobs[1]["reason"])

    def test_existing_unowned_mutable_run_blocks(self):
        folder = self.root / "family_runs/de/000020"
        folder.mkdir(parents=True)
        (folder / "evidence").write_text("external")
        self.batch(n=1)
        job = self.store.snapshot()["jobs"][0]
        self.assertEqual(job["state"], "blocked")
        with self.assertRaisesRegex(DashboardError, "ownership"):
            self.store.resume_job(job["id"])
        self.assertEqual((folder / "evidence").read_text(), "external")

    def test_duplicate_batch_rolls_back_all_languages(self):
        self.batch(["de"], 1, 1)
        with self.assertRaisesRegex(DashboardError, "already"):
            self.batch(["fr", "de"], 1, 2)
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot["jobs"]), 1)
        self.assertEqual(len(snapshot["batches"]), 1)

    def test_duplicate_creation_race(self):
        barrier = threading.Barrier(2)
        authorization = self.authorization(["de"], 1)

        def create():
            barrier.wait()
            try:
                return self.store.create_batch(["de"], 1, 1, authorization)
            except DashboardError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(len(self.store.snapshot()["jobs"]), 1)

    def test_atomic_batch_and_claim_event_rollback(self):
        with patch.object(self.store, "_event", side_effect=RuntimeError("synthetic crash")):
            with self.assertRaises(RuntimeError):
                self.batch()
        self.assertEqual(self.store.snapshot()["batches"], [])
        batch_id = self.batch(n=1)
        self.store.resume(batch_id)
        with patch.object(self.store, "_event", side_effect=RuntimeError("synthetic crash")):
            with self.assertRaises(RuntimeError):
                self.store.claim_next("owner")
        job = self.store.snapshot()["jobs"][0]
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["attempts"], [])

    def test_process_crash_rolls_back_uncommitted_claim(self):
        batch_id = self.batch(n=1)
        self.store.resume(batch_id)
        code = "import os,sys; from lesson_factory.dashboard_store import Store; s=Store(sys.argv[1]); s._event=lambda *args: os._exit(79); s.claim_next('crash-test')"
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 79)
        job = Store(self.root).snapshot()["jobs"][0]
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["attempts"], [])
        self.assertIsNotNone(self.store.claim_next("next-owner"))

    def test_claim_race_global_and_per_language_limits(self):
        batch_id = self.batch(["de", "fr", "ru"], 2, 2)
        self.store.resume(batch_id)
        barrier = threading.Barrier(8)

        def claim():
            barrier.wait()
            return self.store.claim_next("racing-owner")

        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [job for job in pool.map(lambda _: claim(), range(8)) if job]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len({job["language"] for job in jobs}), 2)
        self.assertTrue(all(job["base_checkpoint"] == 20 and job["attempts"][0]["ended_at"] is None for job in jobs))
        self.store.set_parallel(batch_id, 1)
        self.assertIsNone(self.store.claim_next("lower-limit"))

    def test_restart_pauses_but_retains_active_attempt(self):
        job = self.claim(["de", "fr"], 1)
        restarted = Store(self.root)
        restarted.pause_all()
        self.assertIsNone(restarted.claim_next("restart-owner"))
        recovered = restarted.get_job(job["id"])
        self.assertEqual(recovered["attempt_id"], job["attempt_id"])
        self.assertEqual(recovered["state"], "launching")
        with self.assertRaisesRegex(DashboardError, "termination"):
            restarted.reconcile(job["id"])

    def test_identity_guard_and_no_exit_text_success(self):
        job = self.claim()
        with self.assertRaisesRegex(DashboardError, "identity"):
            self.store.record_completion(job["id"], "wrong", "blocked")
        with self.assertRaisesRegex(DashboardError, "registry"):
            self.store.record_completion(job["id"], job["attempt_id"], "complete")
        result = self.store.finish_attempt(job["id"], job["attempt_id"], "succeeded", exit_code=0)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.store.snapshot()["jobs"][1]["state"], "blocked")
        self.assertIsNone(self.store.claim_next("must-not-skip"))

    def test_blocked_receipt_keeps_slot_until_process_end(self):
        job = self.claim(["de", "fr"], 2, 1)
        self.store.set_session(job["id"], job["attempt_id"], "ses_synthetic")
        result = self.store.record_completion(job["id"], job["attempt_id"], "blocked", "Synthetic unresolved source")
        self.assertEqual(result["state"], "running")
        self.assertIsNone(self.store.claim_next("cannot-launch-yet"))
        self.store.finish_attempt(job["id"], job["attempt_id"], "blocked", "Synthetic unresolved source", exit_code=0)
        self.assertEqual(self.store.claim_next("other-language")["language"], "fr")

    def test_complete_receipt_without_saved_session_cannot_succeed(self):
        job = self.claim(n=1)
        self.publish()
        self.store.record_completion(job["id"], job["attempt_id"], "complete")
        result = self.store.finish_attempt(job["id"], job["attempt_id"], "succeeded", exit_code=0)
        self.assertEqual(result["state"], "blocked")
        with self.assertRaisesRegex(DashboardError, "saved coordinator session"):
            self.store.reconcile(job["id"])
        self.assertEqual(self.store.get_job(job["id"])["state"], "blocked")

    def test_source_only_cannot_complete(self):
        job = self.claim(n=1)
        folder = self.root / job["run"] / "en"
        folder.mkdir(parents=True)
        (folder / "lesson.md").write_text("Source only")
        with self.assertRaisesRegex(DashboardError, "registry"):
            self.store.record_completion(job["id"], job["attempt_id"], "complete")
        self.assertEqual(self.store.get_job(job["id"])["state"], "launching")

    def test_real_verified_completion_dual_gate_and_corrupt_pack(self):
        job = self.claim(n=2)
        self.store.set_session(job["id"], job["attempt_id"], "ses_complete")
        path = self.publish()
        receipt = self.store.record_completion(job["id"], job["attempt_id"], "complete")
        self.assertEqual(receipt["state"], "running")
        self.assertEqual(receipt["attempts"][0]["completion_proof"]["path"], path)
        self.assertIsNone(self.store.claim_next("before-exit"))
        with self.assertRaisesRegex(DashboardError, "termination"):
            self.store.reconcile(job["id"])
        quiz = self.root / path / "en/stage-0/quiz.md"
        original = quiz.read_bytes()
        quiz.write_bytes(original + b"changed")
        ended = self.store.finish_attempt(job["id"], job["attempt_id"], "succeeded", exit_code=0)
        self.assertEqual(ended["state"], "blocked")
        with self.assertRaises(core.AuthoringError):
            self.store.reconcile(job["id"])
        quiz.write_bytes(original)
        self.assertEqual(self.store.reconcile(job["id"])["state"], "succeeded")
        next_job = self.store.claim_next("after-reconciliation")
        self.assertEqual(next_job["base_checkpoint"], 40)
        self.assertEqual(self.store.plan(["de"], 1)["families"][0]["base_checkpoint"], 40)
        quiz.write_bytes(original + b"tampered again")
        self.store.finish_attempt(next_job["id"], next_job["attempt_id"], "interrupted")
        corrupt_plan = self.store.plan(["de"], 1)["families"][0]
        self.assertEqual(corrupt_plan["state"], "blocked")
        self.assertEqual(corrupt_plan["base_checkpoint"], 40)

    def test_operator_verification_keeps_agent_receipt_and_requires_original_publication(self):
        job = self.claim(n=2)
        self.store.set_session(job["id"], job["attempt_id"], "ses_operator")
        self.store.attach_launch(job["id"], job["attempt_id"], {"spec_sha256": "synthetic-runtime"})
        self.store.record_completion(job["id"], job["attempt_id"], "blocked", "Original blocker")
        self.store.finish_attempt(job["id"], job["attempt_id"], "blocked")
        runtime = {"job_id": job["id"], "attempt_id": job["attempt_id"], "spec_sha256": "synthetic-runtime", "quiescent": True, "started": True}
        with self.assertRaises(core.AuthoringError):
            self.store.resolve_verified(job["id"], job["attempt_id"], "Operator inspection", runtime)
        self.publish()
        receipt = self.root / "work/family-publications/de/000020/receipt.json"
        original = receipt.read_bytes()
        receipt.write_text("{}")
        with self.assertRaisesRegex(DashboardError, "publication receipt"):
            self.store.resolve_verified(job["id"], job["attempt_id"], "Operator inspection", runtime)
        receipt.write_bytes(original)
        for invalid in ({**runtime, "quiescent": False}, {**runtime, "started": False}, {**runtime, "attempt_id": "other"}):
            with self.assertRaisesRegex(DashboardError, "termination verification"):
                self.store.resolve_verified(job["id"], job["attempt_id"], "Operator inspection", invalid)
        result = self.store.resolve_verified(job["id"], job["attempt_id"], "Operator inspection", runtime)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["attempts"][-1]["completion_status"], "blocked")
        self.assertEqual(result["attempts"][-1]["completion_reason"], "Original blocker")
        self.assertIsNone(result["attempts"][-1]["completion_proof"])
        self.assertEqual(result["launch_metadata"]["operator_verification"]["kind"], "operator_verification")
        self.assertEqual(self.store.claim_next("verified-next")["base_checkpoint"], 40)

    def test_external_reservation_obeys_global_limit(self):
        job = self.claim(["de", "fr"], n=1, parallel=1)
        self.store.set_session(job["id"], job["attempt_id"], "ses_operator")
        self.store.finish_attempt(job["id"], job["attempt_id"], "interrupted")
        self.assertEqual(self.store.claim_next("other-coordinator")["language"], "fr")
        with self.assertRaisesRegex(DashboardError, "No coordinator slot"):
            self.store.reserve_external(job["id"])
        self.assertEqual(len(self.store.get_job(job["id"])["attempts"]), 1)

    def test_active_query_omits_completed_history(self):
        job = self.claim(n=1)
        self.assertEqual([row["id"] for row in self.store.active_jobs()], [job["id"]])
        self.store.finish_attempt(job["id"], job["attempt_id"], "interrupted")
        with patch.object(self.store, "_job", side_effect=AssertionError("inactive history loaded")):
            self.assertEqual(self.store.active_jobs(), [])

    def test_attempt_order_survives_clock_reversal(self):
        with patch("lesson_factory.dashboard_store.time.time", return_value=200.0):
            job = self.claim(n=1)
            self.store.set_session(job["id"], job["attempt_id"], "ses_clock")
            self.store.finish_attempt(job["id"], job["attempt_id"], "interrupted")
        with patch("lesson_factory.dashboard_store.time.time", return_value=100.0):
            self.store.resume_job(job["id"])
            resumed = self.store.claim_next("clock-recovery")
        self.assertEqual(resumed["attempts"][-1]["id"], resumed["attempt_id"])
        self.assertEqual(resumed["attempts"][0]["id"], job["attempt_id"])
        self.store.record_completion(job["id"], resumed["attempt_id"], "blocked")

    def test_resume_same_job_session_and_journal_no_reset(self):
        job = self.claim()
        attempt = job["attempt_id"]
        self.store.set_session(job["id"], attempt, "ses_saved")
        self.store.attach_launch(job["id"], attempt, {"child": {"pid": 123, "create_time": 5}})
        journal = self.root / "work/translations/families/de/000020/fr/journal.json"
        journal.parent.mkdir(parents=True)
        journal.write_text('{"attempts":4,"synthetic":true}')
        self.store.finish_attempt(job["id"], attempt, "interrupted", "Synthetic stopped worker")
        before = self.store.get_job(job["id"])["authorization"]
        resumed = self.store.resume_job(job["id"])
        self.assertTrue(resumed["resume_authorized"])
        next_attempt = self.store.claim_next("new-owner")
        self.assertEqual(next_attempt["id"], job["id"])
        self.assertEqual(next_attempt["session_id"], "ses_saved")
        self.assertNotEqual(next_attempt["attempt_id"], attempt)
        self.assertEqual(len(next_attempt["attempts"]), 2)
        self.assertEqual(next_attempt["authorization"], before)
        self.assertEqual(journal.read_text(), '{"attempts":4,"synthetic":true}')
        with self.assertRaises(DashboardError):
            self.store.set_session(job["id"], next_attempt["attempt_id"], "ses_replacement")
        with self.assertRaises(DashboardError):
            self.store.record_completion(job["id"], attempt, "blocked")
        self.assertEqual(len(self.store.snapshot()["jobs"]), 2)

    def test_finalized_before_receipt_recovers_original_session(self):
        job = self.claim(n=2)
        self.store.set_session(job["id"], job["attempt_id"], "ses_finalized_crash")
        path = self.publish()
        before = family._inventory(self.root, self.root / path)
        self.store.finish_attempt(job["id"], job["attempt_id"], "interrupted", "Synthetic crash before receipt")
        with self.assertRaisesRegex(DashboardError, "receipt"):
            self.store.reconcile(job["id"])
        self.store.resume_job(job["id"])
        resumed = self.store.claim_next("recovery-owner")
        self.assertEqual(resumed["id"], job["id"])
        self.assertEqual(resumed["session_id"], "ses_finalized_crash")
        self.store.record_completion(job["id"], resumed["attempt_id"], "complete")
        self.assertIsNone(self.store.claim_next("before-recovery-exit"))
        result = self.store.finish_attempt(job["id"], resumed["attempt_id"], "succeeded", exit_code=0)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(family._inventory(self.root, self.root / path), before)
        self.assertEqual(self.store.claim_next("after-recovery")["base_checkpoint"], 40)

    def test_rename_before_registry_recovers_only_verified_publication(self):
        job = self.claim(n=1)
        self.store.set_session(job["id"], job["attempt_id"], "ses_publication_crash")
        with patch.object(family, "_register", side_effect=RuntimeError("synthetic commit crash")):
            with self.assertRaisesRegex(RuntimeError, "commit crash"):
                self.publish()
        self.assertTrue((self.root / job["path"]).is_dir())
        self.assertEqual(family.family_registry_facts(self.root, "de"), [])
        self.store.finish_attempt(job["id"], job["attempt_id"], "interrupted")
        receipt_path = self.root / "work/family-publications/de/000020/receipt.json"
        original = receipt_path.read_bytes()
        receipt_path.write_text("{}")
        with self.assertRaisesRegex(DashboardError, "publication receipt"):
            self.store.resume_job(job["id"])
        receipt_path.write_bytes(original)
        self.store.resume_job(job["id"])
        resumed = self.store.claim_next("publication-recovery")
        self.assertEqual(resumed["session_id"], "ses_publication_crash")
        self.assertEqual(family.family_registry_facts(self.root, "de"), [])
        with self.assertRaisesRegex(DashboardError, "registry"):
            self.store.record_completion(job["id"], resumed["attempt_id"], "complete")
        family.finalize_family(self.root, job["run"])
        self.store.record_completion(job["id"], resumed["attempt_id"], "complete")
        self.assertEqual(self.store.finish_attempt(job["id"], resumed["attempt_id"], "succeeded")["state"], "succeeded")

    def test_cross_batch_predecessor_failure_cannot_be_skipped(self):
        first = self.claim(n=1)
        self.store.set_session(first["id"], first["attempt_id"], "ses_cross_batch")
        self.publish()
        second_batch = self.batch(n=1)
        self.store.resume(second_batch)
        self.assertIsNone(self.store.claim_next("first-still-active"))
        self.store.finish_attempt(first["id"], first["attempt_id"], "interrupted")
        second = next(row for row in self.store.snapshot()["jobs"] if row["batch_id"] == second_batch)
        self.assertEqual(second["base_checkpoint"], 40)
        self.assertEqual(second["state"], "blocked")
        with self.assertRaisesRegex(DashboardError, "Earlier job"):
            self.store.resume_job(second["id"])
        self.assertIsNone(self.store.claim_next("must-not-skip-cross-batch"))
        self.store.resume_job(first["id"])
        resumed = self.store.claim_next("first-recovery")
        self.store.record_completion(first["id"], resumed["attempt_id"], "complete")
        self.store.finish_attempt(first["id"], resumed["attempt_id"], "succeeded")
        self.assertEqual(self.store.claim_next("second-can-proceed")["id"], second["id"])

    def test_unknown_session_and_unowned_draft_cannot_resume(self):
        job = self.claim(n=1)
        self.store.finish_attempt(job["id"], job["attempt_id"], "interrupted")
        with self.assertRaisesRegex(DashboardError, "saved session"):
            self.store.resume_job(job["id"])
        unowned = self.root / "family_runs/ru/000020"
        unowned.mkdir(parents=True)
        self.batch(["ru"], 1, 1)
        russian = next(row for row in self.store.snapshot()["jobs"] if row["language"] == "ru")
        with self.assertRaisesRegex(core.AuthoringError, "ownership"):
            self.store.resume_job(russian["id"])
        unowned.rmdir()
        self.assertEqual(self.store.resume_job(russian["id"])["state"], "queued")

    def test_symlink_and_hardlink_guards(self):
        other = self.root / "other.sqlite"
        other.write_text("untouched")
        wal = Path(str(self.store.path) + "-wal")
        wal.symlink_to(other)
        with self.assertRaisesRegex(core.AuthoringError, "Symlink"):
            self.store.snapshot()
        wal.unlink()
        os.link(other, wal)
        with self.assertRaisesRegex(DashboardError, "private regular"):
            self.store.snapshot()
        wal.unlink()
        self.assertEqual(other.read_text(), "untouched")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").symlink_to(self.root / "work", target_is_directory=True)
            with self.assertRaisesRegex(core.AuthoringError, "Symlink"):
                Store(root)

    def test_completion_cli_flags_and_invalid_identity(self):
        tool = Path(__file__).resolve().parents[1] / "tools/dashboard-job.py"
        spec = importlib.util.spec_from_file_location("dashboard_job_tool", tool)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job = self.claim(n=1)
        args = ["--root", str(self.root), "--job", job["id"], "--attempt", job["attempt_id"], "--status", "blocked", "--reason", "Synthetic blocker"]
        with patch("builtins.print"):
            self.assertEqual(module.main(args), 0)
            self.assertEqual(module.main([*args[:5], "incorrect", *args[6:]]), 1)
        self.assertEqual(self.store.get_job(job["id"])["attempts"][0]["completion_status"], "blocked")
        with self.assertRaises(DashboardError):
            self.store.record_completion(job["id"], job["attempt_id"], "blocked", "api_key=not-for-output")


if __name__ == "__main__":
    unittest.main()
