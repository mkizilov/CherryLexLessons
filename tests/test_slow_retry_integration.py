import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lesson_factory import core, family, family_translation, translation, translation_recovery as recovery, translation_scheduler as scheduler
from tests import test_slow_retry_recovery as fixtures


class IntegratedReviewChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SlowRetryRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run, self.logs = self.fixture.root, self.fixture.run, self.fixture.logs
        for number in range(1, 5):
            record = core.read_json(self.logs / f'attempt-{number}/journal.json')
            record['reviewed_practice_stages'] = [1, 2, 3, 4]
            core.write_json(self.logs / f'attempt-{number}/journal.json', record)
            if number == 4:
                core.write_json(self.logs / 'journal.json', record)
        anchor = getattr(self, 'anchor_response', {'status': 'completed', 'output': []})
        self.fixture.terminal(anchor, state='rejected' if anchor['status'] == 'completed' else 'provider_failed', error_code='validation_rejected' if anchor['status'] == 'completed' else 'max_output_tokens')
        if anchor['status'] == 'completed':
            core.write_json(self.logs / 'attempt-4/raw-candidate.json', [])
        self.fixture.authorize()
        self.policy = self.fixture.policy()
        self.now = self.policy['extension_receipt']['created_at']
        self.calls = []
        self.waits = []
        self.candidate = {'stages': [{'stage': stage, 'lesson_md': 'lesson' if stage == 0 else '', 'quiz_md': 'quiz', 'adaptation_notes': []} for stage in range(5)]}
        self.failure = {'status': 'incomplete', 'output': [], 'incomplete_details': {'reason': 'max_output_tokens'}}
        self.success = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(self.candidate)}]}]}
        self.key = self.root / 'synthetic-test-key'
        self.key.write_text('offline-fixture')
        self.prefix = {str(path): path.read_bytes() for number in range(1, 5) for path in (self.logs / f'attempt-{number}').iterdir()}

    def seed_pending(self, response=None, *, saved=True, raw=True, count=5):
        for number in range(5, count + 1):
            folder = self.logs / f'attempt-{number}'
            for name in ('request.json', 'summary.json'):
                translation.atomic_write(folder / name, (self.logs / 'attempt-4' / name).read_bytes())
            record = core.read_json(self.logs / 'journal.json')
            record.update(attempt=number, state='pending' if number == count else 'provider_failed', error_code=None if number == count else 'provider_max_output_tokens_empty', retry_policy=dict(self.policy), request_id=f'pending-{number}', retry_eligible=number < count, retry_at=self.now + 1801 if number < count else None, retry_scheduled_at=self.now if number < count else None)
            core.write_json(folder / 'journal.json', record)
            core.write_json(self.logs / 'journal.json', record)
            value = response if number == count else self.failure
            if raw or number < count:
                (folder / 'raw.sse').write_bytes(self.wire(value) if value else b'')
            if value and (saved or number < count):
                core.write_json(folder / 'response.json', value)

    def wire(self, response):
        return b'data: ' + translation.encode({'type': 'response.' + response['status'], 'response': response}) + b'\n\n'

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def run_family(self, responses, deadline=100):
        iterator = iter(responses)
        actual_execute, actual_worker = translation.execute, scheduler.retry_worker
        def opener(request, timeout):
            self.calls.append(request)
            return io.BytesIO(self.wire(next(iterator)))
        def execute(*args, **kwargs):
            return actual_execute(*args, **kwargs, clock=lambda: self.now, jitter=lambda: 1, opener=opener)
        def worker(*args, **kwargs):
            kwargs['deadline'] = deadline
            return actual_worker(*args, **kwargs, clock=lambda: self.now, sleep=self.sleep)
        args = SimpleNamespace(root=self.root, run=self.run, lang='fr', all=False, execute=True, resume=True, repair_candidate=None, max_input_bytes=translation.MAX_INPUT_BYTES, workers=1, timeout=120, deadline=3600)
        report = family.validate_family(self.root, self.run, 'en')
        with patch.dict('os.environ', {'CONTACTBOX_KEY_FILE': str(self.key)}), patch.object(family_translation, 'execute', side_effect=execute), patch.object(scheduler, 'retry_worker', side_effect=worker), patch.object(family_translation, 'build_prompt', return_value='focused synthetic prompt'), patch.object(scheduler, 'validate_process', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr='')), contextlib.redirect_stdout(io.StringIO()):
            return family_translation.run(args)

    def assert_prefix_unchanged(self):
        self.assertEqual(self.prefix, {name: Path(name).read_bytes() for name in self.prefix})

    def test_pending_no_raw_recovers_waits_without_purchase(self):
        self.seed_pending(raw=False)
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([], deadline=100)
        first = core.read_json(self.logs / 'journal.json')
        self.assertEqual(first['state'], 'uncertain')
        self.assertEqual(first['attempt'], 5)
        self.assertEqual(first['error_code'], 'interrupted_attempt')
        self.assertTrue(first['raw_missing_before_recovery'])
        self.assertEqual(first['retry_at'], 3801)
        self.assertEqual(self.calls, [])
        with self.assertRaises(translation.TranslationError):
            self.run_family([], deadline=200)
        self.assertEqual(core.read_json(self.logs / 'journal.json')['retry_at'], first['retry_at'])
        self.assertEqual(self.calls, [])
        self.assert_prefix_unchanged()

    def test_pending_completed_empty_response_recovers_then_waits(self):
        self.seed_pending({'status': 'completed', 'output': []})
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([], deadline=100)
        current = core.read_json(self.logs / 'journal.json')
        self.assertEqual(current['state'], 'provider_failed')
        self.assertEqual(current['error_code'], 'provider_completed_empty')
        self.assertEqual(current['retry_at'], 3801)
        self.assertEqual(self.calls, [])
        self.assert_prefix_unchanged()

    def test_pending_completed_empty_raw_only_recovers_then_waits(self):
        self.seed_pending({'status': 'completed', 'output': []}, saved=False)
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([], deadline=100)
        current = core.read_json(self.logs / 'journal.json')
        self.assertEqual(current['state'], 'provider_failed')
        self.assertEqual(current['error_code'], 'provider_completed_empty')
        self.assertEqual(current['retry_at'], 3801)
        self.assertEqual(self.calls, [])
        self.assert_prefix_unchanged()

    def test_pending_substantive_completed_never_repurchase(self):
        self.seed_pending(self.success)
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([], deadline=21600)
        self.assertEqual(core.read_json(self.logs / 'journal.json')['state'], 'pending')
        self.assertEqual(self.calls, [])
        self.assert_prefix_unchanged()

    def test_pending_eighth_empty_terminal_records_exhaustion(self):
        self.seed_pending({'status': 'completed', 'output': []}, saved=False, count=8)
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([], deadline=21600)
        record = core.read_json(self.logs / 'journal.json')
        self.assertEqual(record['attempt'], 8)
        self.assertEqual(record['state'], 'provider_failed')
        self.assertFalse(record['retry_eligible'])
        self.assertIsNone(record['retry_at'])
        self.assertEqual(self.calls, [])
        self.assert_prefix_unchanged()

    def test_four_appended_failures_exhaust_eighth_and_resume_no_ninth(self):
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([self.failure] * 4, deadline=21600)
        record = core.read_json(self.logs / 'journal.json')
        self.assertEqual(record['attempt'], 8)
        self.assertFalse(record['retry_eligible'])
        self.assertIsNone(record['retry_at'])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.waits, [1800.0100000000002, 1801, 1801, 1801])
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([], deadline=21600)
        self.assertEqual(len(self.calls), 4)
        self.assertFalse((self.logs / 'attempt-9').exists())
        self.assert_prefix_unchanged()

    def test_eighth_success_publishes_and_preserves_prior_empty_anchor(self):
        self.run_family([self.failure] * 3 + [self.success], deadline=21600)
        record = core.read_json(self.logs / 'journal.json')
        self.assertEqual(record['attempt'], 8)
        self.assertEqual(record['state'], 'completed')
        self.assertFalse(record['family_publication_pending'])
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, 'fr')['disposition'], 'preserve')
        self.assertEqual(len(self.calls), 4)
        self.run_family([], deadline=21600)
        self.assertEqual(len(self.calls), 4)
        self.assert_prefix_unchanged()


class PartialIntegratedReviewChecks(IntegratedReviewChecks):
    anchor_response = {'status': 'incomplete', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"stages":[{"stage":0,"lesson_md":"unfinished'}]}], 'incomplete_details': {'reason': 'max_output_tokens'}}

    def test_partial_max_tokens_on_attempts_five_to_eight_remains_bounded(self):
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([self.anchor_response] * 4, deadline=21600)
        record = core.read_json(self.logs / 'journal.json')
        self.assertEqual(record['attempt'], 8)
        self.assertFalse(record['retry_eligible'])
        self.assertIsNone(record['retry_at'])
        self.assertEqual(len(self.calls), 4)
        self.assert_prefix_unchanged()

    def test_pending_partial_max_tokens_recovers_fixed_wait_no_purchase(self):
        self.seed_pending(self.anchor_response, saved=False)
        with self.assertRaisesRegex(translation.TranslationError, 'workers failed'):
            self.run_family([], deadline=100)
        record = core.read_json(self.logs / 'journal.json')
        self.assertEqual(record['attempt'], 5)
        self.assertEqual(record['state'], 'provider_failed')
        self.assertTrue(record['retry_eligible'])
        self.assertEqual(record['retry_at'], 3801)
        self.assertEqual(self.calls, [])
        self.assert_prefix_unchanged()


class PartialClassifierReviewChecks(unittest.TestCase):
    def output(self, text):
        return [{'type': 'message', 'id': 'message-1', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]

    def response(self, text='', **changes):
        value = {'id': 'response-1', 'status': 'incomplete', 'output': self.output(text) if text else [], 'incomplete_details': {'reason': 'max_output_tokens'}}
        value.update(changes)
        return value

    def event(self, kind, **payload):
        return b'data: ' + translation.encode({'type': kind, **payload}) + b'\n\n'

    def terminal(self, response):
        return self.event('response.' + response['status'], response=response)

    def eligible(self, response, raw, **changes):
        record = {'state': 'provider_failed', 'error_code': 'max_output_tokens', **changes}
        return translation.retained_retry_eligible(record, response, raw)

    def complete(self):
        return json.dumps({'stages': [{'stage': stage, 'lesson_md': 'lesson', 'quiz_md': 'quiz', 'adaptation_notes': []} for stage in range(5)]})

    def test_partial_incomplete_and_failed_permitted_only_with_verified_terminal(self):
        for response in (self.response('{"stages":[{"stage":0'), self.response('{"stages":[{"stage":0', status='failed', incomplete_details=None, error={'code': 'upstream_stream_read_error'})):
            with self.subTest(status=response['status']):
                self.assertTrue(self.eligible(response, self.terminal(response)))
                self.assertFalse(self.eligible(response, self.event('response.output_text.delta', delta='{"stages":[{"stage":0', item_id='message-1', output_index=0, content_index=0)))
                self.assertFalse(self.eligible(response, self.terminal(response), state='rejected'))
                self.assertFalse(self.eligible(response, self.terminal(response), state='completed'))

    def test_numeric_scalar_delta_inside_incomplete_json_remains_retryable(self):
        text = '{"stages":[{"stage":0,"lesson_md":"unfinished'
        response = self.response(text)
        raw = b''.join(self.event('response.output_text.delta', delta=chunk, item_id='message-1', output_index=0, content_index=0) for chunk in ('{"stages":[{"stage":', '0', ',"lesson_md":"unfinished'))
        self.assertTrue(self.eligible(response, raw + self.terminal(response)))

    def test_permanent_and_refusal_in_secondary_response_fields_block(self):
        for response in (
            self.response('{"stages":[', status='failed', error={'code': 'upstream_stream_read_error'}, incomplete_details={'reason': 'billing_error'}),
            self.response('{"stages":[', error={'code': 'authentication_error'}),
            self.response('{"stages":[', refusal='No'),
        ):
            with self.subTest(response=response):
                self.assertFalse(self.eligible(response, self.terminal(response)))

    def test_any_recoverable_candidate_in_terminal_done_delta_or_fences_blocks(self):
        complete = self.complete()
        for text in (complete, '```json\n' + complete + '\n```', 'preface\n' + complete, complete + '\n{"unfinished":', '{broken preface}\n' + complete):
            response = self.response(text)
            with self.subTest(surface='terminal', text=text[:15]):
                self.assertFalse(self.eligible(response, self.terminal(response)))
        response = self.response('{"stages":[')
        for before in (
            self.event('response.output_text.done', text=complete, item_id='message-1', output_index=0, content_index=0),
            self.event('response.output_item.done', item=self.output(complete)[0], output_index=0),
            self.event('response.content_part.done', part={'type': 'output_text', 'text': complete}, item_id='message-1', output_index=0, content_index=0),
            self.event('response.output_text.delta', delta=complete[:30], item_id='message-1', output_index=0, content_index=0) + self.event('response.output_text.delta', delta=complete[30:], item_id='message-1', output_index=0, content_index=0),
        ):
            with self.subTest(surface=before[:70]):
                self.assertFalse(self.eligible(response, before + self.terminal(response)))

    def test_complete_family_split_across_done_parts_without_deltas_blocks(self):
        response = self.response()
        complete = self.complete()
        for kind in ('response.output_text.done', 'response.content_part.done', 'response.output_item.done'):
            events = []
            for index, text in enumerate((complete[:-1], complete[-1:])):
                if kind == 'response.output_text.done':
                    event = self.event(kind, text=text, item_id='message-1', output_index=0, content_index=index)
                elif kind == 'response.content_part.done':
                    event = self.event(kind, part={'type': 'output_text', 'text': text}, item_id='message-1', output_index=0, content_index=index)
                else:
                    item = self.output(text)[0]
                    item['id'] = 'message-' + str(index)
                    event = self.event(kind, item=item, output_index=index)
                events.append(event)
            with self.subTest(kind=kind):
                self.assertFalse(self.eligible(response, b''.join(events) + self.terminal(response)))

    def test_complete_candidate_in_mixed_delta_done_views_blocks(self):
        response = self.response()
        complete = self.complete()
        raw = self.event('response.output_text.delta', delta=complete[:-1], item_id='message-1', output_index=0, content_index=0)
        raw += self.event('response.output_text.done', text=complete[-1:], item_id='message-1', output_index=0, content_index=1)
        self.assertFalse(self.eligible(response, raw + self.terminal(response)))

    def test_partial_delta_and_matching_done_are_not_duplicated_into_output(self):
        text = '{"stages":[{"stage":0,"lesson_md":"unfinished'
        response = self.response(text)
        raw = self.event('response.output_text.delta', delta=text, item_id='message-1', output_index=0, content_index=0)
        raw += self.event('response.output_text.done', text=text, item_id='message-1', output_index=0, content_index=0)
        raw += self.event('response.content_part.done', part={'type': 'output_text', 'text': text}, item_id='message-1', output_index=0, content_index=0)
        raw += self.event('response.output_item.done', item=self.output(text)[0], output_index=0)
        self.assertTrue(self.eligible(response, raw + self.terminal(response)))

    def test_completed_unknown_conflicting_and_refusal_stream_evidence_block(self):
        response = self.response('{"stages":[')
        for before in (
            self.terminal(self.response('', status='completed', incomplete_details=None)),
            self.terminal(self.response('', id='other-response')),
            self.event('response.refusal.delta', delta='No'),
            self.event('response.unknown_output', content='ambiguous'),
            self.event('codex.response.metadata', text='ambiguous'),
        ):
            with self.subTest(before=before):
                self.assertFalse(self.eligible(response, before + self.terminal(response)))


class SkeletonReviewChecks(unittest.TestCase):
    def setUp(self):
        self.helper = PartialClassifierReviewChecks()
        self.skeleton = {'stages': [{'stage': stage, 'lesson_md': '', 'quiz_md': '', 'adaptation_notes': []} for stage in range(5)]}

    def eligible(self, value, *, status='incomplete', state='provider_failed'):
        response = self.helper.response(json.dumps(value), status=status, error={'code': 'upstream_stream_read_error'} if status == 'failed' else None, incomplete_details={'reason': 'max_output_tokens'} if status == 'incomplete' else None)
        return self.helper.eligible(response, self.helper.terminal(response), state=state)

    def test_exact_empty_skeleton_only_failed_or_incomplete_provider_failure(self):
        self.assertTrue(self.eligible(self.skeleton))
        self.assertTrue(self.eligible(self.skeleton, status='failed'))
        self.assertFalse(self.eligible(self.skeleton, status='completed'))
        self.assertFalse(self.eligible(self.skeleton, state='rejected'))
        self.assertFalse(self.eligible(self.skeleton, state='completed'))

    def test_substantive_whitespace_unknown_and_partial_schemas_block(self):
        variants = []
        for field, value in [('lesson_md', ' '), ('quiz_md', '\n'), ('lesson_md', 'lesson'), ('quiz_md', 'quiz'), ('adaptation_notes', ['note']), ('extra', '')]:
            candidate = json.loads(json.dumps(self.skeleton))
            candidate['stages'][0][field] = value
            variants.append(candidate)
        candidate = json.loads(json.dumps(self.skeleton))
        candidate['extra'] = ''
        variants.append(candidate)
        for bad in variants:
            with self.subTest(bad=bad):
                self.assertFalse(self.eligible(bad))

    def test_stage_identity_and_exact_fields_required(self):
        variants = []
        for field in ('stage', 'lesson_md', 'quiz_md', 'adaptation_notes'):
            candidate = json.loads(json.dumps(self.skeleton))
            del candidate['stages'][0][field]
            variants.append(candidate)
        for replacement in (False, 1, '0'):
            candidate = json.loads(json.dumps(self.skeleton))
            candidate['stages'][0]['stage'] = replacement
            variants.append(candidate)
        for count in (4, 6):
            candidate = json.loads(json.dumps(self.skeleton))
            candidate['stages'] = candidate['stages'][:count] if count == 4 else candidate['stages'] + [candidate['stages'][0]]
            variants.append(candidate)
        for bad in variants:
            with self.subTest(bad=bad):
                self.assertFalse(self.eligible(bad))

    def test_empty_skeleton_never_hides_later_substantive_complete_family(self):
        skeleton = json.dumps(self.skeleton)
        complete = self.helper.complete()
        response = self.helper.response(skeleton + '\n' + complete)
        self.assertFalse(self.helper.eligible(response, self.helper.terminal(response)))
        response = self.helper.response(skeleton)
        raw = self.helper.event('response.output_text.done', text=skeleton + '\n' + complete, output_index=0, content_index=0, item_id='message-1')
        self.assertFalse(self.helper.eligible(response, raw + self.helper.terminal(response)))


class SlowRetryCLITests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'tools/reauthorize-translations.py'
        specification = importlib.util.spec_from_file_location('slow_retry_cli', path)
        self.module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(self.module)
        self.root = Path('/synthetic/readonly-cli-root')
        self.run = 'family_runs/ja/000020'
        self.arguments = ['--root', str(self.root), '--run', self.run]
        stack = self.enterContext(contextlib.ExitStack())
        self.routes = {
            name: stack.enter_context(patch.object(self.module, name, return_value={'synthetic': name}))
            for name in ('authorize_slow_retries', 'plan_slow_retries', 'authorize_translation_recovery', 'authorize_translation_correction', 'plan_translation_recovery')
        }
        self.output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def assert_only_route(self, selected):
        for name, route in self.routes.items():
            if name != selected:
                route.assert_not_called()

    def test_preview_only_routes_readonly_plan(self):
        self.assertEqual(self.module.main([*self.arguments, '--preview-slow', 'hi', 'ja', '--reason', 'synthetic preview', '--timeout', '45']), 0)
        self.routes['plan_slow_retries'].assert_called_once_with(self.root, self.run, ['hi', 'ja'], reason='synthetic preview', timeout=45)
        self.assert_only_route('plan_slow_retries')
        self.assertEqual(json.loads(self.output.getvalue()), {'synthetic': 'plan_slow_retries'})

    def test_explicit_extend_passes_operator_scope_and_reason(self):
        self.assertEqual(self.module.main([*self.arguments, '--extend-slow', 'hi', '--reason', 'synthetic authorization']), 0)
        self.routes['authorize_slow_retries'].assert_called_once_with(self.root, self.run, ['hi'], reason='synthetic authorization', operator=True, timeout=120)
        self.assert_only_route('authorize_slow_retries')

    def test_reason_without_explicit_action_never_authorizes(self):
        self.assertEqual(self.module.main([*self.arguments, '--reason', 'not an operator flag']), 1)
        self.assert_only_route(None)

    def test_default_remains_original_readonly_plan(self):
        self.assertEqual(self.module.main(self.arguments), 0)
        self.routes['plan_translation_recovery'].assert_called_once_with(self.root, self.run, timeout=120)
        self.assert_only_route('plan_translation_recovery')

    def test_slow_actions_mutually_exclusive_with_each_other_and_old_actions(self):
        for actions in (
            ['--extend-slow', 'hi', '--preview-slow', 'hi'],
            ['--extend-slow', 'hi', '--authorize'],
            ['--extend-slow', 'hi', '--correct-placeholder', 'hi'],
            ['--preview-slow', 'hi', '--authorize'],
            ['--preview-slow', 'hi', '--correct-placeholder', 'hi'],
        ):
            with self.subTest(actions=actions), self.assertRaises(SystemExit) as error:
                self.module.main([*self.arguments, *actions, '--reason', 'synthetic scope'])
            self.assertEqual(error.exception.code, 2)
        self.assert_only_route(None)


if __name__ == '__main__':
    unittest.main()
