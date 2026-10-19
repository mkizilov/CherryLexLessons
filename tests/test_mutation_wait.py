"""Local lock contention must not become content failure or repeat a mutation."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from lesson_factory import core, family, family_cli
import test_family_core


class MutationWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / '.authoring.lock'

    @contextlib.contextmanager
    def held(self):
        with self.path.open('w+b') as handle:
            handle.write(b'fixture-owner')
            handle.flush()
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield handle
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def test_wait_then_acquire_same_inode_without_touching_owner(self):
        acquired = threading.Event()
        started = threading.Event()
        errors = []
        def wait():
            started.set()
            try:
                with core.mutation_lock(self.root, timeout=2):
                    acquired.set()
            except BaseException as exc:
                errors.append(exc)
        with self.held():
            inode = self.path.stat().st_ino
            worker = threading.Thread(target=wait)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(acquired.wait(.15))
            self.assertEqual(self.path.read_bytes(), b'fixture-owner')
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(acquired.is_set())
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.path.read_text(), str(os.getpid()))

    def test_timeout_is_typed_and_preserves_holder_bytes(self):
        with self.held():
            start = time.monotonic()
            with self.assertRaises(core.MutationLockBusy):
                with core.mutation_lock(self.root, timeout=.08):
                    self.fail('Concurrent mutation admitted')
            self.assertLess(time.monotonic() - start, 1)
            self.assertEqual(self.path.read_bytes(), b'fixture-owner')
        with core.mutation_lock(self.root):
            pass

    def test_cancel_during_wait_and_before_acquisition(self):
        stop = threading.Event()
        with self.held():
            timer = threading.Timer(.05, stop.set)
            timer.start()
            try:
                with self.assertRaisesRegex(core.AuthoringError, 'cancelled'):
                    with core.mutation_lock(self.root, timeout=2, stop_event=stop):
                        self.fail('Cancelled waiter entered')
            finally:
                timer.join()
            self.assertEqual(self.path.read_bytes(), b'fixture-owner')
        with self.assertRaisesRegex(core.AuthoringError, 'cancelled'):
            with core.mutation_lock(self.root, timeout=2, stop_event=stop):
                self.fail('Already-cancelled waiter entered')

    def test_overdue_wakeup_never_admits_mutation(self):
        now = [0.0]
        with self.held() as owner:
            def oversleep(_):
                fcntl.flock(owner, fcntl.LOCK_UN)
                now[0] = .2
            with patch.object(core.time, 'monotonic', side_effect=lambda: now[0]), patch.object(core.time, 'sleep', side_effect=oversleep):
                with self.assertRaises(core.MutationLockBusy):
                    with core.mutation_lock(self.root, timeout=.05):
                        self.fail('Overdue waiter admitted')
            self.assertEqual(self.path.read_bytes(), b'fixture-owner')
        with core.mutation_lock(self.root):
            pass

    def test_deadline_crossed_during_successful_retry_releases_lock(self):
        now = [0.0]
        real_flock = fcntl.flock
        with self.held() as owner:
            def release(_):
                real_flock(owner, fcntl.LOCK_UN)
                now[0] = .01
            def acquire(fd, flags):
                result = real_flock(fd, flags)
                if flags & fcntl.LOCK_EX:
                    now[0] = .2
                return result
            with patch.object(core.time, 'monotonic', side_effect=lambda: now[0]), patch.object(core.time, 'sleep', side_effect=release), patch.object(core.fcntl, 'flock', side_effect=acquire):
                with self.assertRaises(core.MutationLockBusy):
                    with core.mutation_lock(self.root, timeout=.05):
                        self.fail('Expired acquisition admitted')
            self.assertEqual(self.path.read_bytes(), b'fixture-owner')
        with core.mutation_lock(self.root):
            pass

    def test_invalid_timeouts_reject_before_opening_state(self):
        for value in (-1, True, None, '1', float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with core.mutation_lock(self.root, timeout=value):
                    self.fail('Bad duration accepted')
        self.assertFalse(self.path.exists())

    def test_exception_in_body_is_not_retried_and_lock_is_released(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, 'after mutation began'):
            with core.mutation_lock(self.root, timeout=1):
                calls.append(True)
                raise RuntimeError('after mutation began')
        with core.mutation_lock(self.root):
            pass
        self.assertEqual(calls, [True])

    def test_killed_holder_releases_kernel_lock_without_deletion(self):
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'import fcntl,sys,time; f=open(sys.argv[1],"a+b"); '
            'fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); time.sleep(30)', str(self.path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            inode = self.path.stat().st_ino
            with self.assertRaises(core.MutationLockBusy):
                with core.mutation_lock(self.root):
                    self.fail('Live holder bypassed')
            child.terminate()
            child.wait(timeout=3)
            with core.mutation_lock(self.root, timeout=1):
                self.assertEqual(self.path.stat().st_ino, inode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()

    def test_replaced_lock_is_rejected_before_body(self):
        real_flock = fcntl.flock
        def replace_on_acquire(fd, flags):
            result = real_flock(fd, flags)
            if flags & fcntl.LOCK_EX:
                self.path.unlink()
                self.path.write_bytes(b'replacement')
            return result
        with patch.object(core.fcntl, 'flock', side_effect=replace_on_acquire):
            with self.assertRaisesRegex(core.AuthoringError, 'replaced'):
                with core.mutation_lock(self.root, timeout=1):
                    self.fail('Replacement allowed')
        self.assertEqual(self.path.read_bytes(), b'replacement')


class FamilyCommandWaitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_family_core.FamilyCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.root, self.run = self.fixture.root, self.fixture.run

    def test_cli_defaults_and_bounds(self):
        for args in (['prepare', '--language', 'de', '--checkpoint', '20'],
                     ['review', '--run', self.run, '--role', 'pedagogical', '--file', 'report.json'],
                     ['finalize', '--run', self.run]):
            self.assertEqual(family_cli.parser().parse_args(args).lock_wait, 300)
            self.assertEqual(family_cli.parser().parse_args(args + ['--lock-wait', '0']).lock_wait, 0)
            for value in ('-1', '1801', 'nan', '2.5'):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    family_cli.parser().parse_args(args + ['--lock-wait', value])

    def test_prepare_waits_and_runs_inside_acquired_lock(self):
        self.fixture.publish()
        args = ['--root', str(self.root), 'prepare', '--language', 'de', '--checkpoint', '40', '--lock-wait', '2']
        marker = self.root / 'reference/formats/spec.txt'
        entered = threading.Event()
        original = family._external_history
        def observe(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        with (self.root / '.authoring.lock').open('a+b') as handle, patch.object(family, '_external_history', side_effect=observe):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            timer = threading.Timer(.1, lambda: fcntl.flock(handle, fcntl.LOCK_UN))
            timer.start()
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = family_cli.main(args)
            finally:
                timer.join()
            self.assertEqual(code, 0)
            self.assertTrue(entered.is_set())
            self.assertEqual(json.loads(output.getvalue())['run'], 'family_runs/de/000040')
            self.assertEqual(core.read_json(self.root / 'family_runs/de/000040/family-context.json')['dependencies']['reference/formats/spec.txt'], core.digest(marker))

    def test_all_mutating_commands_forward_wait_and_timeout_before_mutation(self):
        self.fixture.author()
        payload = self.fixture.report('pedagogical')
        core.write_json(self.root / 'report.json', payload)
        cases = [(['prepare', '--language', 'de', '--checkpoint', '20'], family.prepare_family),
                 (['review', '--run', self.run, '--role', 'pedagogical', '--file', 'report.json'], family.submit_family_review),
                 (['finalize', '--run', self.run], family.finalize_family)]
        with (self.root / '.authoring.lock').open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for args, method in cases:
                with self.subTest(args=args), patch.object(family, method.__name__, wraps=method) as called:
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        code = family_cli.main(['--root', str(self.root), *args, '--lock-wait', '0'])
                    self.assertEqual(code, 75)
                    message = json.loads(err.getvalue())
                    self.assertEqual(message['state'], 'waiting_for_lock')
                    self.assertFalse(message['mutation_started'])
                    self.assertTrue(message['retryable'])
                    self.assertEqual(called.call_args.kwargs['lock_timeout'], 0)
        self.assertFalse((self.root / self.run / 'reviews/pedagogical.json').exists())
        self.assertFalse((self.root / 'family_packs/de/000020').exists())

    def test_review_and_finalize_reject_source_change_while_waiting(self):
        self.fixture.author()
        self.fixture.approve()
        source = family.family_locale_path(self.root, self.run, 'en', 0) / 'lesson.md'
        original = source.read_bytes()
        payload = self.fixture.report('pedagogical')
        for action in ('review', 'finalize'):
            source.write_bytes(original)
            with self.subTest(action=action), (self.root / '.authoring.lock').open('a+b') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def change_then_release(_):
                    source.write_bytes(original + b' changed during wait')
                    fcntl.flock(owner, fcntl.LOCK_UN)
                with patch.object(core.time, 'sleep', side_effect=change_then_release):
                    with self.assertRaisesRegex(core.AuthoringError, 'binding|stale'):
                        if action == 'review':
                            family.submit_family_review(self.root, self.run, 'pedagogical', payload, lock_timeout=1)
                        else:
                            family.finalize_family(self.root, self.run, lock_timeout=1)
            self.assertFalse((self.root / 'family_packs/de/000020').exists())

    def test_same_family_worker_appearing_during_wait_still_blocks(self):
        self.fixture.author()
        self.fixture.approve()
        worker_path = self.root / 'work/translations/families/de/000020/fr/worker.lock'
        with (self.root / '.authoring.lock').open('a+b') as owner, worker_path.open('a+b') as worker:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def activate_then_release(_):
                fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(owner, fcntl.LOCK_UN)
            with patch.object(core.time, 'sleep', side_effect=activate_then_release):
                with self.assertRaisesRegex(core.AuthoringError, 'worker is active'):
                    family.finalize_family(self.root, self.run, lock_timeout=1)
        self.assertFalse((self.root / 'family_packs/de/000020').exists())

    def test_real_content_error_is_not_temporary_busy(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = family_cli.main(['--root', str(self.root), 'prepare', '--language', 'de', '--checkpoint', '40'])
        self.assertEqual(code, 1)
        self.assertIn('earlier', err.getvalue())
        self.assertNotIn('waiting_for_lock', err.getvalue())


if __name__ == '__main__':
    unittest.main()
