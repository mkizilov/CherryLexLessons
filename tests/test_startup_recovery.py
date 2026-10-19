import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from lesson_factory.dashboard_runner import Runner
from lesson_factory.dashboard_supervisor import boot_identity, fingerprint, isolated_environment, saved_environment, write_json
from lesson_factory import startup_recovery as recovery
import test_dashboard_store as store_fixture
from tools.dashboard import configure_worker_binary


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = store_fixture.DashboardStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.store = fixture.root, fixture.store
        self.job = fixture.claim(n=2)
        self.runner = Runner(self.root, self.store)
        self.addCleanup(self.runner.close)
        self.directory = self.root / 'work/dashboard/jobs' / self.job['id'] / 'attempts' / self.job['attempt_id']
        self.directory.mkdir(parents=True)
        self.home = self.directory.parent.parent / 'home'
        self.home.mkdir()
        self.command = [sys.executable, 'run', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas', '--variant', 'xhigh', 'Synthetic prompt']
        spec = {'root': str(self.root), 'job_id': self.job['id'], 'attempt_id': self.job['attempt_id'], 'session_id': None, 'command': self.command, 'environment': saved_environment(isolated_environment(self.home), self.home)}
        write_json(self.directory/'spec.json', spec)
        identity = {'pid': 999999999, 'create_time': 1, 'boot_id': boot_identity(), 'command_fingerprint': 'dead'}
        evidence = {'job_id': self.job['id'], 'attempt_id': self.job['attempt_id'], 'session_id': None, 'session_conflict': False, 'spec_sha256': fingerprint(spec), 'supervisor': identity, 'child': identity, 'phase': 'stopped'}
        write_json(self.directory/'process.json', evidence)
        write_json(self.directory/'exit.json', {**evidence, 'exit_code': 1, 'stopped': False, 'group_stopped': True})
        (self.directory/'events.jsonl').write_bytes(b'EXACT SYNTHETIC HELP\n')
        (self.directory/'stderr.log').write_bytes(recovery.ERROR)
        (self.directory/'supervisor.log').write_bytes(b'')
        self.store.attach_launch(self.job['id'], self.job['attempt_id'], {'spec_sha256':fingerprint(spec), 'supervisor':identity, 'phase':'prepared'})
        self.store.finish_attempt(self.job['id'], self.job['attempt_id'], 'blocked', exit_code=1)
        self.store.pause(self.job['batch_id'])
        self.worker = {'path':sys.executable,'sha256':'a'*64,'version':'2.0.29'}
        self.addCleanup(patch.stopall)
        patch.object(recovery, 'HELP_SHA256', hashlib.sha256(b'EXACT SYNTHETIC HELP\n').hexdigest()).start()
        patch.object(recovery, 'worker_identity', return_value=self.worker).start()

    def authorize(self):
        return recovery.authorize(self.runner, self.job['id'], sys.executable)

    def claim_retry(self):
        self.authorize()
        self.store.resume(self.job['batch_id'])
        return self.store.claim_next('synthetic-owner')

    def test_exact_rejection_append_only_idempotent_and_rechecked(self):
        before={p.name:p.read_bytes() for p in self.directory.iterdir()}
        first=self.authorize()
        self.assertEqual(first['state'],'queued')
        self.assertIsNone(first['session_id'])
        again=self.authorize()
        self.assertEqual(len(again['attempts']),1)
        self.store.resume(self.job['batch_id'])
        job=self.store.claim_next('synthetic-owner')
        self.assertEqual(len(job['attempts']),2)
        self.assertIsNone(job['session_id'])
        self.assertEqual(recovery.verify_launch(self.runner,job,sys.executable)['worker'],self.worker)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.directory.iterdir()})
        with self.assertRaises(ValueError):self.authorize()
        self.assertIsNone(self.store.claim_next('other'))

    def test_any_extra_output_or_stop_blocks(self):
        for name,extra in [('events.jsonl',b'{"type":"text"}\n'),('stderr.log',b'other\n'),('supervisor.log',b'unknown')]:
            p=self.directory/name;old=p.read_bytes();p.write_bytes(old+extra)
            with self.subTest(name=name),self.assertRaises(ValueError):self.authorize()
            p.write_bytes(old)
        (self.directory/'stop.json').write_text('{}')
        with self.assertRaises(ValueError):self.authorize()

    def test_content_or_paid_evidence_blocks(self):
        for relative in (self.job['run'],self.job['path'],"work/translations/families/de/000020","work/family-publications/de/000020"):
            p=self.root/relative;p.mkdir(parents=True)
            with self.subTest(path=relative),self.assertRaises(ValueError):self.authorize()
            p.rmdir()

    def test_drift_after_authorization_blocks_launch(self):
        job=self.claim_retry()
        (self.directory/'events.jsonl').write_bytes(b'EXACT SYNTHETIC HELP\nextra')
        with self.assertRaises(ValueError):recovery.verify_launch(self.runner,job,sys.executable)

    def test_changed_worker_or_unknown_runtime_blocks(self):
        job=self.claim_retry()
        with patch.object(recovery,'worker_identity',return_value={**self.worker,'sha256':'b'*64}),self.assertRaises(ValueError):
            recovery.verify_launch(self.runner,job,sys.executable)
        with patch.object(self.runner,'_runtime',return_value={'quiescent':False}),self.assertRaises(ValueError):
            recovery.verify_launch(self.runner,job,sys.executable)

    def test_nonregular_or_oversized_json_rejected_before_helpers(self):
        for name in ('spec.json', 'process.json', 'exit.json'):
            p = self.directory/name
            original = p.read_bytes()
            p.unlink()
            os.mkfifo(p)
            with patch.object(self.runner, '_verified_spec', side_effect=AssertionError('Must not open unsafe evidence')), self.assertRaises(ValueError):
                self.authorize()
            p.unlink()
            p.write_bytes(b' ' * 65537)
            with patch.object(self.runner, '_verified_spec', side_effect=AssertionError('Must not open oversized evidence')), self.assertRaises(ValueError):
                self.authorize()
            p.write_bytes(original)
        self.assertIsNone(self.store.cli_startup_retry(self.job['id']))

    def test_cancelled_retry_cannot_be_claimed(self):
        self.authorize()
        self.store.cancel_job(self.job['id'])
        self.store.resume(self.job['batch_id'])
        self.assertIsNone(self.store.claim_next('synthetic-owner'))
        self.assertEqual(len(self.store.get_job(self.job['id'])['attempts']), 1)

    def test_changed_scope_or_completion_never_authorizes(self):
        original = self.runner._verified_spec(self.store.get_job(self.job['id']))
        changed = dict(original, session_id='ses_unexpected')
        write_json(self.directory/'spec.json', changed)
        with self.assertRaises(ValueError):
            self.authorize()
        write_json(self.directory/'spec.json', original)
        receipt = __import__('json').loads((self.directory/'exit.json').read_text())
        write_json(self.directory/'exit.json', {**receipt, 'stopped': True})
        with self.assertRaises(ValueError):
            self.authorize()

    def test_ordinary_resume_remains_fail_closed(self):
        with self.assertRaises(ValueError):self.runner.resume_job(self.job['id'])
        with self.assertRaises(ValueError):self.store.resume_job(self.job['id'])

    def test_runtime_preflight_requires_v2_before_queue_access(self):
        with patch.dict(os.environ,{'OPENCODE_BINARY':sys.executable}),patch('lesson_factory.opencode_worker.verify_binary', side_effect=ValueError('V2 required')):
            with self.assertRaisesRegex(ValueError,'V2'):configure_worker_binary()
        with patch.dict(os.environ,{'OPENCODE_BINARY':sys.executable}),patch('lesson_factory.opencode_worker.verify_binary',return_value='2.0.29'):
            self.assertEqual(configure_worker_binary(),str(Path(sys.executable).resolve()))


if __name__=='__main__':unittest.main()
