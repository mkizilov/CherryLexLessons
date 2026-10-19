"""Prospective incomplete-output retries use the original budget, not a new grant.
Run: .venv/bin/python -m unittest discover -s tests -p test_incomplete_retry.py -v
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from lesson_factory import translation as transport
from lesson_factory.translation_scheduler import retry_worker


class IncompleteRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'secrets').mkdir()
        (self.root / 'secrets/contactbox_key').write_text('offline-fixture')
        self.body, self.summary = transport.prepare({'study_language': 'de', 'source_locale': 'en'}, 'fr', 'fixture')
        self.path = self.root / 'work/translations/default/fr/journal.json'
        self.now = 1000.0
        self.calls = 0
        self.waits = []

    def response(self, text='{"stages":[', **fields):
        return {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'},
                'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}], **fields}

    def opener(self, response, before=b''):
        def open_fixture(request, timeout):
            self.calls += 1
            self.assertNotIn('max_output_tokens', json.loads(request.data))
            stream = io.BytesIO(before + b'data: ' + transport.encode({'type': 'response.' + response['status'], 'response': response}) + b'\n\n')
            stream.headers = {'Retry-After': '2400'}
            return stream
        return open_fixture

    def run_attempt(self, response, **options):
        return transport.execute(self.root, self.body, self.summary, lambda _: True,
                                 gate=lambda: True, opener=self.opener(response),
                                 clock=lambda: self.now, jitter=lambda: 1, **options)

    def record(self):
        return json.loads(self.path.read_bytes())

    def test_incomplete_partial_waits_and_stops_at_original_four(self):
        history = {}
        for attempt in range(1, 5):
            with self.assertRaises(transport.TranslationError):
                self.run_attempt(self.response(), resume=True, retry=attempt > 1)
            record = self.record()
            self.assertEqual(record['attempt'], attempt)
            self.assertEqual(record['error_code'], 'max_output_tokens')
            self.assertEqual(record['retry_eligible'], attempt < 4)
            if attempt < 4:
                self.assertEqual(record['retry_at'], self.now + 2401)
                before = self.calls
                with self.assertRaisesRegex(transport.TranslationError, 'not due'):
                    self.run_attempt(self.response(), resume=True, retry=True)
                self.assertEqual(before, self.calls)
                self.now = record['retry_at']
            history[attempt] = (self.path.parent / f'attempt-{attempt}/journal.json').read_bytes()
        with self.assertRaises(transport.TranslationError):
            self.run_attempt(self.response(), resume=True, retry=True)
        self.assertEqual(self.calls, 4)
        for attempt, raw in history.items():
            self.assertEqual((self.path.parent / f'attempt-{attempt}/journal.json').read_bytes(), raw)

    def test_without_retry_after_uses_thirty_minute_floor(self):
        def opener(request, timeout):
            stream = self.opener(self.response())(request, timeout)
            stream.headers = {}
            return stream
        with self.assertRaises(transport.TranslationError):
            transport.execute(self.root, self.body, self.summary, lambda _: True,
                              gate=lambda: True, opener=opener, clock=lambda: self.now, jitter=lambda: 1)
        self.assertEqual(self.record()['retry_at'], self.now + 1801)
        self.assertTrue(self.record()['retry_eligible'])

    def test_worker_persists_wait_beyond_deadline_and_resumes(self):
        def wait(seconds):
            self.waits.append(seconds)
            self.now += seconds
        def attempt(**kwargs):
            kwargs.pop('remaining')
            return self.run_attempt(self.response(), **kwargs)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(transport.TranslationError):
            retry_worker(attempt, self.path, resume=True, clock=lambda: self.now, sleep=wait, deadline=2000)
        self.assertEqual(self.calls, 1)
        due = self.record()['retry_at']
        self.assertGreater(due, self.now)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(transport.TranslationError):
            retry_worker(attempt, self.path, resume=True, clock=lambda: self.now, sleep=wait, deadline=100)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.record()['retry_at'], due)

    def test_complete_family_in_failed_stream_is_not_repurchased(self):
        candidate = {'stages': [{'stage': n, 'lesson_md': 'lesson', 'quiz_md': 'quiz', 'adaptation_notes': []} for n in range(5)]}
        with self.assertRaises(transport.TranslationError):
            self.run_attempt(self.response(json.dumps(candidate)))
        self.assertFalse(self.record()['retry_eligible'])
        with self.assertRaises(transport.TranslationError):
            self.run_attempt(self.response(), resume=True, retry=True)
        self.assertEqual(self.calls, 1)

    def test_old_nonretryable_journal_is_not_reclassified(self):
        with self.assertRaises(transport.TranslationError):
            self.run_attempt(self.response())
        record = self.record()
        record.update(retry_eligible=False, retry_at=None)
        for path in (self.path, self.path.parent / 'attempt-1/journal.json'):
            path.write_bytes(transport.encode(record))
        before = self.path.read_bytes()
        with self.assertRaises(transport.TranslationError):
            self.run_attempt(self.response(), resume=True, retry=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, 1)

    def test_completed_rejected_and_permanent_remain_blocked(self):
        for status, details, error in [('completed', None, None), ('incomplete', {'reason': 'context_length_exceeded'}, None),
                                       ('incomplete', {'reason': 'max_output_tokens'}, {'code': 'billing_error'})]:
            with self.subTest(status=status, details=details, error=error):
                scope = f'case-{status}-{details and details["reason"]}-{bool(error)}'
                with self.assertRaises(transport.TranslationError):
                    self.run_attempt(self.response('invalid', status=status, incomplete_details=details, error=error), scope=scope)
                saved = json.loads((self.root / 'work/translations' / scope / 'fr/journal.json').read_bytes())
                self.assertFalse(saved['retry_eligible'])

    def test_pending_terminal_recovery_gets_same_bounded_policy(self):
        with self.assertRaises(transport.TranslationError):
            self.run_attempt(self.response())
        record = self.record()
        record.update(state='pending', retry_eligible=False, retry_at=None)
        for path in (self.path, self.path.parent / 'attempt-1/journal.json'):
            path.write_bytes(transport.encode(record))
        with self.assertRaisesRegex(transport.TranslationError, 'fresh resume'):
            self.run_attempt(self.response(), resume=True)
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.record()['retry_eligible'])
        self.assertEqual(self.record()['retry_at'], self.now + 2401)
