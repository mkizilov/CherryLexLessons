from contextlib import ExitStack
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

from lesson_factory.dashboard_runner import Runner
from lesson_factory.dashboard_store import DashboardError
from lesson_factory import dashboard_supervisor as supervisor
import test_dashboard_store as fixture_module


class SupervisorReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DashboardStoreTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.store = self.fixture.store
        self.runner = Runner(self.root, self.store)
        self.addCleanup(self.runner.close)
        self.job = self.fixture.claim(n=2, parallel=1)
        self.job_id = self.job['id']
        self.attempt_id = self.job['attempt_id']
        self.store.set_session(self.job_id, self.attempt_id, 'ses_preserved')
        self.job_dir, self.directory = self.runner._paths(self.job)
        supervisor.private_directory(self.directory)
        supervisor.private_directory(self.job_dir / 'home')
        (self.job_dir / 'job.lock').touch()
        self.environment = supervisor.saved_environment(supervisor.isolated_environment(self.job_dir / 'home', {'HOME': '/synthetic'}), self.job_dir / 'home')
        self.command = [sys.executable, 'run', '--format', 'json', '--agent', 'lesson-coordinator', '--model', 'opencode-omniroute/Antharas', '--variant', 'xhigh', '--session', 'ses_preserved', 'Synthetic authorized prompt']
        self.spec = {'root': str(self.root), 'job_id': self.job_id, 'attempt_id': self.attempt_id, 'session_id': 'ses_preserved', 'command': self.command, 'environment': self.environment}
        supervisor.write_json(self.directory / 'spec.json', self.spec)
        self.supervisor_command = [sys.executable, str(self.root / 'lesson_factory/dashboard_supervisor.py'), '--spec', str(self.directory / 'spec.json')]
        self.child_command = [*self.supervisor_command, '--child', '--gate-fd', '7', '--lock-fd', '8']
        self.old = {'pid': 15765, 'create_time': 100., 'boot_id': 'synthetic-boot', 'command_fingerprint': supervisor.fingerprint(self.supervisor_command)}
        self.new = {**self.old, 'pid': 18978, 'create_time': 200.}
        self.child = {**self.old, 'pid': 18979, 'create_time': 201., 'command_fingerprint': supervisor.fingerprint(self.child_command)}
        self.metadata = {'attempt_dir': str(self.directory.relative_to(self.root)), 'spec_sha256': supervisor.fingerprint(self.spec), 'phase': 'prepared', 'supervisor': self.old, 'diagnostic': 'Old supervisor journal missing'}
        self.store.attach_launch(self.job_id, self.attempt_id, self.metadata)
        self.process = {'job_id': self.job_id, 'attempt_id': self.attempt_id, 'session_id': 'ses_preserved', 'spec_sha256': supervisor.fingerprint(self.spec), 'supervisor': self.new, 'child': self.child, 'phase': 'running'}
        self.write_process()
        self.attestation = {'kind': 'operator_attested_prelaunch', 'old_supervisor': self.old, 'job_id': self.job_id, 'attempt_id': self.attempt_id, 'session_id': 'ses_preserved', 'spec_sha256': supervisor.fingerprint(self.spec), 'checks': {'old_supervisor_dead': True, 'job_lock_free': True, 'phase': 'prepared', 'child_metadata_absent': True, 'directory_files': ['spec.json', 'supervisor.log'], 'supervisor_log_empty': True}, 'script_sha256': 'a' * 64, 'report_sha256': 'b' * 64}
        self.states = {15765: 'dead', 18978: 'alive', 18979: 'alive'}
        self.group = 'alive'
        self.lock_free = False
        self.live_parent = Mock()
        self.live_parent.create_time.return_value = 200.
        self.live_parent.cmdline.return_value = self.supervisor_command
        self.live_parent.cwd.return_value = str(self.root)
        self.live_child = Mock()
        self.live_child.create_time.return_value = 201.
        self.live_child.cmdline.return_value = self.child_command
        self.live_child.cwd.return_value = str(self.root)
        self.live_child.ppid.return_value = self.new['pid']
        self.live_child.environ.return_value = {**self.environment, 'CHERRYLEX_DASHBOARD_JOB': self.job_id, 'CHERRYLEX_DASHBOARD_ATTEMPT': self.attempt_id}
        self.live_child.open_files.return_value = [Mock(path=str(self.job_dir / 'job.lock'), fd=8)]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('lesson_factory.dashboard_runner.boot_identity', return_value='synthetic-boot'))
        self.stack.enter_context(patch('lesson_factory.dashboard_runner.process_state', side_effect=lambda identity: self.states.get(identity['pid'], 'unknown')))
        self.stack.enter_context(patch('lesson_factory.dashboard_runner.group_state', side_effect=lambda identity: self.group))
        self.stack.enter_context(patch('lesson_factory.dashboard_runner.psutil.Process', side_effect=self.fake_process))
        self.stack.enter_context(patch('lesson_factory.dashboard_runner.os.getpgid', return_value=self.child['pid']))
        self.stack.enter_context(patch.object(self.runner, '_job_lock_free', side_effect=lambda path: self.lock_free))
        self.spawn = self.stack.enter_context(patch('lesson_factory.dashboard_runner.subprocess.Popen', side_effect=AssertionError('No process launches allowed')))
        self.signals = self.stack.enter_context(patch('lesson_factory.dashboard_runner.signal_group', side_effect=AssertionError('No signals allowed')))

    def fake_process(self, pid):
        if self.states.get(pid) == 'dead':
            raise supervisor.psutil.NoSuchProcess(pid)
        return self.live_child if pid == self.child['pid'] else self.live_parent

    def write_process(self):
        supervisor.write_json(self.directory / 'process.json', self.process)

    def reconcile(self, note='Observed exact checked one-time relaunch', attestation=None, new=None):
        return self.runner.reconcile_supervisor(self.job_id, self.attempt_id, self.new if new is None else new, note, self.attestation if attestation is None else attestation)

    def events(self):
        return [event for event in self.store.events(self.job_id) if event['kind'] == 'supervisor_reconciled']

    def stopped(self):
        self.states[self.new['pid']] = self.states[self.child['pid']] = 'dead'
        self.group = 'dead'
        self.lock_free = True
        self.process['phase'] = 'stopped'
        self.write_process()
        supervisor.write_json(self.directory / 'exit.json', {**self.process, 'exit_code': 0, 'stopped': False, 'group_stopped': True, 'ended_at': time.time()})

    def test_normal_attach_stays_immutable_and_normal_recovery_cannot_replace(self):
        with self.assertRaisesRegex(DashboardError, 'cannot be replaced'):
            self.store.attach_launch(self.job_id, self.attempt_id, {'supervisor': self.new})
        self.runner._reconcile(self.store.get_job(self.job_id))
        self.assertEqual(self.store.get_job(self.job_id)['launch_metadata']['supervisor'], self.old)
        self.assertEqual(self.events(), [])
        self.stopped()
        self.runner._reconcile(self.store.get_job(self.job_id))
        self.assertIsNone(self.store.get_job(self.job_id)['attempts'][-1]['ended_at'])

    def test_live_adoption_preserves_completion_and_scope_and_single_current_dated_event(self):
        self.store.record_completion(self.job_id, self.attempt_id, 'blocked', 'Original blocker')
        before = self.store.get_job(self.job_id)
        started = time.time()
        result = self.reconcile()
        self.assertEqual(result['launch_metadata']['supervisor'], self.new)
        self.assertEqual(result['launch_metadata']['child'], self.child)
        self.assertNotIn('diagnostic', result['launch_metadata'])
        self.assertEqual(result['authorization'], before['authorization'])
        for key in ('session_id', 'attempt_id', 'resume_authorized'):
            self.assertEqual(result[key], before[key])
        for key in ('completion_status', 'completion_reason', 'completion_proof', 'completion_at', 'ended_at'):
            self.assertEqual(result['attempts'][-1][key], before['attempts'][-1][key])
        event = self.events()[0]['details']
        self.assertGreaterEqual(event['recorded_at'], started)
        self.assertEqual(event['request']['old_supervisor'], self.old)
        self.assertEqual(event['request']['attestation']['kind'], 'operator_attested_prelaunch')
        self.assertEqual(event['proof']['hashes']['process'], supervisor.fingerprint(self.process))
        self.assertEqual(self.reconcile(), result)
        self.assertEqual(len(self.events()), 1)
        self.assertIsNone(self.store.claim_next('no-release'))
        self.assertEqual(self.runner._reconcile(result)['state'], 'running')
        self.spawn.assert_not_called()
        self.signals.assert_not_called()

    def test_different_repeat_refused_and_normal_attach_cannot_change_audit_marker(self):
        result = self.reconcile()
        with self.assertRaisesRegex(DashboardError, 'already exists'):
            self.reconcile(note='Different attestation')
        with self.assertRaisesRegex(DashboardError, 'cannot be replaced'):
            self.store.attach_launch(self.job_id, self.attempt_id, {'supervisor_reconciliation': 'other'})
        self.assertEqual(self.store.get_job(self.job_id), result)

    def test_missing_unknown_live_reused_and_command_mismatch_old_supervisor_refused(self):
        for state in ('alive', 'unknown'):
            with self.subTest(state=state):
                self.states[self.old['pid']] = state
                with self.assertRaises(ValueError):
                    self.reconcile()
        self.states[self.old['pid']] = 'dead'
        with patch('lesson_factory.dashboard_runner.psutil.Process', return_value=Mock(create_time=lambda: 999.)):
            with self.assertRaisesRegex(ValueError, 'reused'):
                self.reconcile()
        with self.store._transaction() as db:
            changed = {**self.metadata, 'supervisor': None}
            db.execute('UPDATE attempts SET launch_metadata=? WHERE id=?', (fixture_module.core.canonical(changed).decode(), self.attempt_id))
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.events(), [])

    def test_old_child_metadata_any_pre_child_phase_drift_refused(self):
        for change in ({'child': None}, {'child': self.child}, {'phase': 'spawning'}, {'mode': 'external'}):
            with self.subTest(change=change), self.store._transaction() as db:
                db.execute('UPDATE attempts SET launch_metadata=? WHERE id=?', (fixture_module.core.canonical({**self.metadata, **change}).decode(), self.attempt_id))
            with self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.events(), [])

    def test_attestation_not_inferred_and_scope_or_historical_timestamp_rejected(self):
        for change in ({'job_id': 'other'}, {'attempt_id': 'other'}, {'session_id': 'ses_other'}, {'spec_sha256': 'c' * 64}, {'old_supervisor': self.new}, {'script_sha256': None}, {'report_sha256': ''}, {'checked_at': 100.}, {'checks': {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.reconcile(attestation={**self.attestation, **change})
        for note in ('', None, 'api_key=synthetic'):
            with self.subTest(note=note), self.assertRaises(DashboardError):
                self.reconcile(note=note)
        self.assertEqual(self.events(), [])

    def test_spec_session_environment_child_or_exit_drift_rejected(self):
        for name, value in (('spec.json', {**self.spec, 'session_id': 'ses_other'}), ('spec.json', {**self.spec, 'environment': {**self.environment, 'HOME': '/wrong'}}), ('process.json', {**self.process, 'session_id': 'ses_other'}), ('process.json', {**self.process, 'attempt_id': 'other'}), ('process.json', {**self.process, 'supervisor': self.old}), ('process.json', {**self.process, 'child': self.new}), ('exit.json', {**self.process, 'child': self.old, 'group_stopped': True})):
            with self.subTest(name=name, value=value):
                supervisor.write_json(self.directory / name, value)
                with self.assertRaises(ValueError):
                    self.reconcile()
                supervisor.write_json(self.directory / 'spec.json', self.spec)
                self.write_process()
                (self.directory / 'exit.json').unlink(missing_ok=True)
        self.assertEqual(self.events(), [])

    def test_live_child_parent_command_environment_group_lock_descriptor_checked(self):
        targets = ((self.live_child.ppid, 1), (self.live_child.cmdline, [sys.executable, 'wrong']), (self.live_parent.cmdline, ['wrong']), (self.live_parent.cwd, '/wrong'), (self.live_child.environ, {}), (self.live_child.open_files, []))
        for mock, value in targets:
            original = mock.return_value
            mock.return_value = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reconcile()
            mock.return_value = original
        self.lock_free = True
        with self.assertRaisesRegex(ValueError, 'hold the job lock'):
            self.reconcile()
        self.assertEqual(self.events(), [])

    def test_unknown_new_child_and_incoherent_live_or_orphan_groups_refused(self):
        for new_state, child_state, group in (('unknown', 'alive', 'alive'), ('alive', 'unknown', 'alive'), ('dead', 'alive', 'alive'), ('dead', 'dead', 'alive'), ('dead', 'dead', 'unknown')):
            self.states[self.new['pid']] = new_state
            self.states[self.child['pid']] = child_state
            self.group = group
            with self.subTest(states=(new_state, child_state, group)), self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.events(), [])

    def test_stopped_adoption_requires_real_exit_then_normal_reconcile_not_forged_success(self):
        self.stopped()
        exit_path = self.directory / 'exit.json'
        receipt = supervisor.read_json(exit_path)
        exit_path.unlink()
        with self.assertRaises(ValueError):
            self.reconcile()
        supervisor.write_json(exit_path, receipt)
        result = self.reconcile()
        self.assertIsNone(result['attempts'][-1]['completion_status'])
        self.runner._reconcile(result)
        self.assertEqual(self.store.get_job(self.job_id)['state'], 'interrupted')
        self.assertEqual(len(self.events()), 1)
        self.reconcile()
        self.assertEqual(len(self.events()), 1)

    def test_genuine_completion_remains_gated_until_adopted_process_stops(self):
        self.fixture.publish()
        self.store.record_completion(self.job_id, self.attempt_id, 'complete')
        result = self.reconcile()
        self.assertEqual(result['state'], 'running')
        self.assertIsNone(self.store.claim_next('held-before-exit'))
        self.stopped()
        self.runner._reconcile(self.store.get_job(self.job_id))
        self.assertEqual(self.store.get_job(self.job_id)['state'], 'succeeded')
        self.assertEqual(self.store.claim_next('next-after-real-completion')['base_checkpoint'], 40)

    def test_stop_lock_held_refuses_adoption(self):
        self.stopped()
        with patch.object(self.runner, '_lock_job', side_effect=ValueError('Lock held')):
            with self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.events(), [])

    def test_event_receipt_failure_rolls_back_identity_then_retry_is_idempotent(self):
        before = self.store.get_job(self.job_id)
        with patch.object(self.store, '_event', side_effect=RuntimeError('receipt crash')):
            with self.assertRaisesRegex(RuntimeError, 'receipt crash'):
                self.reconcile()
        self.assertEqual(self.store.get_job(self.job_id), before)
        self.assertEqual(self.events(), [])
        self.reconcile()
        self.reconcile()
        self.assertEqual(len(self.events()), 1)

    def test_metadata_change_between_verification_and_store_compare_refused(self):
        original = self.store.reconcile_supervisor
        def raced(job_id, attempt_id, request, proof=None):
            self.store.attach_launch(job_id, attempt_id, {'diagnostic': 'Concurrent observation'})
            return original(job_id, attempt_id, request, proof)
        with patch.object(self.store, 'reconcile_supervisor', side_effect=raced):
            with self.assertRaisesRegex(DashboardError, 'metadata changed'):
                self.reconcile()
        self.assertEqual(self.store.get_job(self.job_id)['launch_metadata']['supervisor'], self.old)
        self.assertEqual(self.events(), [])

    def test_conflicting_stream_and_mutating_journal_refused(self):
        (self.directory / 'events.jsonl').write_text('{"type":"text","sessionID":"ses_other"}\n')
        with self.assertRaisesRegex(ValueError, 'stream identity'):
            self.reconcile()
        (self.directory / 'events.jsonl').unlink()
        original = supervisor.read_json
        calls = 0
        def changed(path):
            nonlocal calls
            value = original(path)
            if Path(path).name == 'process.json':
                calls += 1
                if calls == 2:
                    return {**value, 'phase': 'stopped'}
            return value
        with patch('lesson_factory.dashboard_runner.read_json', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                self.reconcile()
        self.assertEqual(self.events(), [])


if __name__ == '__main__':
    unittest.main()
