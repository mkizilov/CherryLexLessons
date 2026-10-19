import argparse
import asyncio
from copy import deepcopy
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from lesson_factory.dashboard_ui import (
    DashboardController, DashboardPage, DashboardRuntime, LocalOnlyMiddleware, bounded_integer,
    active_scope, batch_views, coordinator_hint, iframe_document, integer_error, may_resume, queue_summary, safe_error, safe_job, safe_text,
    selected_languages, stage_label,
)
from tools.dashboard import check_dependencies, parser, port_number


def make_plan(languages=('de',), count=1):
    groups = []
    for language in languages:
        families = [{'language': language, 'base_checkpoint': 20 * (index + 1),
                     'run': f'family_runs/{language}/{20 * (index + 1):06d}',
                     'path': f'family_packs/{language}/{20 * (index + 1):06d}',
                     'state': 'blocked' if language == 'ru' else 'queued',
                     'reason': 'Legacy collision requires explicit reconciliation.' if language == 'ru' else None}
                    for index in range(count)]
        groups.append({'language': language, 'vocabulary_count': 9000, 'finalized_count': 0,
                       'families': families, 'blocked_reason': families[0]['reason']})
    return {'languages': groups, 'families': [row for group in groups for row in group['families']]}


def make_job(**changes):
    return {'id': 'job1', 'batch_id': 'batch1', 'language': 'de', 'base_checkpoint': 20,
            'run': 'family_runs/de/000020', 'path': 'family_packs/de/000020',
            'state': 'queued', 'reason': None, 'attempt_id': None, 'session_id': None,
            'attempts': [], 'launch_metadata': {}, **changes}


def make_recovery(**changes):
    return {'job_id': 'job1', 'attempt_id': 'attempt1', 'session_id': 'ses_same', 'attempts': [],
            'process': {'supervisor': 'dead', 'child': 'dead', 'group': 'dead', 'lock': 'free',
                        'quiescent': True, 'diagnostic': None},
            'external': False, 'resume_command': None,
            'inspect_command': 'opencode export ses_same', 'can_handoff': True,
            'handoff_reason': None, 'can_resolve': True, **changes}


def services(snapshot=None):
    store = Mock()
    store.plan.side_effect = lambda languages, count: make_plan(languages, count)
    store.create_batch.return_value = 'batch1'
    store.get_job.return_value = make_job(state='blocked')
    store.snapshot.return_value = snapshot or {'batches': [], 'jobs': []}
    runner = Mock()
    runner.recover.return_value = []
    runner.tick.return_value = []
    runner.recovery_details.return_value = make_recovery()
    runner.handoff_job.return_value = make_recovery(external=True, can_handoff=False, resume_command='python tools/dashboard-terminal.py --job job1')
    preview = Mock()
    preview.catalog.return_value = []
    preview.render.return_value = '<!doctype html><html><body>Fixture lesson</body></html>'
    return store, runner, preview


class FrameParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class DashboardHelperTests(unittest.TestCase):
    def test_integer_validation_rejects_fraction_boolean_infinite_and_out_of_bounds(self):
        self.assertEqual(bounded_integer(12.0, 'Parallel', 12), 12)
        for value in (True, False, None, 0, -1, 13, 1.1, 'x', float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bounded_integer(value, 'Parallel', 12)

    def test_inline_integer_error_and_cap_explanation(self):
        for value in (13, 0, None, 2.5):
            self.assertEqual(integer_error(value, 'Parallel coordinators', 12), 'Parallel coordinators must be a whole number from 1 to 12.')
        self.assertIsNone(integer_error(12, 'Parallel coordinators', 12))
        hint = coordinator_hint(['de', 'fr'], 12)
        self.assertIn('at most 2 coordinators', hint)
        self.assertIn('translation/API workers are additional', hint)
        self.assertIn('One family per study language', hint)
        self.assertIn('Enter a whole number from 1 to 12', coordinator_hint(['de'], 13))

    def test_safe_job_includes_only_recovery_presentation_fields(self):
        row = safe_job(make_job(state='interrupted', session_id='ses_failure',
            launch_metadata={'mode': 'external', 'phase': 'awaiting_terminal', 'diagnostic': 'token=PRIVATE', 'environment': {'SECRET': 'PRIVATE'}}))
        self.assertEqual(row['session_label'], 'Session needing attention')
        self.assertTrue(row['external'])
        self.assertEqual(row['phase'], 'awaiting_terminal')
        self.assertNotIn('PRIVATE', json.dumps(row))
        self.assertNotIn('environment', row)

    def test_first_load_prefers_running_batch_over_old_finished_batch(self):
        snapshot = {'batches': [{'id': 'old', 'languages': ['de'], 'paused': True, 'created_at': 1}, {'id': 'new', 'languages': ['de'], 'paused': False, 'created_at': 2}], 'jobs': [make_job(batch_id='old', state='succeeded'), make_job(id='active', batch_id='new', state='running')]}
        options, preferred = batch_views(snapshot)
        self.assertEqual(preferred, 'new')
        self.assertEqual(list(options), ['new', 'old'])
        self.assertIn('1 active', options['new'])
        self.assertIn('1 complete · Finished', options['old'])
        snapshot['batches'][0]['created_at'] = 3
        self.assertEqual(batch_views(snapshot)[1], 'new')
        self.assertEqual(list(batch_views(snapshot)[0]), ['old', 'new'])
        snapshot['jobs'][1]['state'] = 'queued'
        self.assertEqual(batch_views(snapshot)[1], 'new')
        snapshot['jobs'][0]['state'] = 'queued'
        self.assertEqual(batch_views(snapshot)[1], 'new')
        self.assertEqual(batch_views({'batches': [], 'jobs': []}), ({}, None))

    def test_stage_labels_use_actual_unlock_ranks(self):
        self.assertEqual([stage_label(20, stage) for stage in range(5)], [
            'Stage 0 · Base · after 20 words', 'Stage 1 · Return 1 · after 45 words',
            'Stage 2 · Return 2 · after 95 words', 'Stage 3 · Return 3 · after 145 words',
            'Stage 4 · Return 4 · after 245 words'])

    def test_active_scope_excludes_queued_jobs_and_names_selected_batch(self):
        jobs = [make_job(id=f'active{index}', batch_id='batch1', state='running') for index in range(10)]
        jobs += [make_job(id=f'recovery{index}', batch_id='batch1', state='blocked' if index % 2 else 'interrupted', session_id=f'ses_recovery{index}') for index in range(2)]
        jobs += [make_job(id='unstarted-blocked', batch_id='batch1', state='blocked'), make_job(id='queued', batch_id='batch1', state='queued'), make_job(id='other', batch_id='batch2', state='running')]
        scope = active_scope(jobs, 'batch1')
        self.assertIn('12 running, launching, reserved or needs-attention jobs', scope)
        self.assertIn('Interrupted and blocked workers keep their recorded session IDs visible', scope)
        self.assertIn('queued and finished jobs stay in All batch jobs.', scope)
        self.assertIn('1 more active or needing attention in other batches', scope)

    def test_waiting_auto_resume_remains_in_active_view(self):
        scope = active_scope([make_job(id='retry', batch_id='batch1', state='retry_wait', session_id='ses_retry')], 'batch1')
        self.assertIn('1 running, launching, reserved or needs-attention jobs', scope)

    def test_language_selection_validates_and_preserves_exact_order(self):
        self.assertEqual(selected_languages(['ru', 'de', 'ru']), ['ru', 'de'])
        for invalid in ([], ['xx'], 'de', None):
            with self.assertRaises(ValueError):
                selected_languages(invalid)

    def test_srcdoc_is_one_sandboxed_iframe_and_never_root_markup(self):
        payload = '<script>parent.document.body.innerHTML="bad"</script><img src=x onerror=alert(1)>"</iframe><script>bad()</script>'
        parser = FrameParser()
        parser.feed(iframe_document(payload))
        self.assertEqual([tag for tag, attrs in parser.tags], ['iframe'])
        attrs = parser.tags[0][1]
        self.assertEqual(attrs['srcdoc'], payload)
        self.assertEqual(attrs['sandbox'], 'allow-scripts')
        self.assertEqual(attrs['referrerpolicy'], 'no-referrer')
        self.assertNotIn('allow-same-origin', attrs['sandbox'])

    def test_safe_metadata_excludes_raw_logs_launch_specs_and_credentials(self):
        job = make_job(reason='api_key=SUPERSECRET https://example.invalid/?token=OTHER',
                       launch_metadata={'secret': 'PRIVATE'}, authorization={'secret': 'PRIVATE'},
                       attempts=[{'id': 'attempt1', 'session_id': 'ses_1', 'raw_output': 'PRIVATE'}])
        rendered = json.dumps(safe_job(job))
        for forbidden in ('SUPERSECRET', 'OTHER', 'PRIVATE', 'launch_metadata', 'authorization', 'raw_output'):
            self.assertNotIn(forbidden, rendered)
        self.assertIn('ses_1', rendered)
        self.assertNotIn('secret-value', safe_text('Bearer secret-value /root/secrets/api_key'))
        self.assertNotIn('PRIVATE', safe_error(RuntimeError('PRIVATE')))

    def test_resume_never_guesses_session_after_an_attempt(self):
        self.assertTrue(may_resume(make_job(state='blocked')))
        self.assertTrue(may_resume(make_job(state='interrupted', attempts=[{'id': 'a'}], session_id='ses_same')))
        self.assertFalse(may_resume(make_job(state='blocked', attempts=[{'id': 'a'}])))
        self.assertFalse(may_resume(make_job(state='interrupted', attempt_id='a')))
        self.assertFalse(may_resume(make_job(state='running', session_id='ses_same')))

    def test_effective_cap_is_minimum_of_unpaused_unfinished_batches(self):
        snapshot = {'batches': [
            {'id': 'a', 'max_parallel': 8, 'paused': False},
            {'id': 'b', 'max_parallel': 3, 'paused': False},
            {'id': 'c', 'max_parallel': 1, 'paused': True},
            {'id': 'd', 'max_parallel': 1, 'paused': False},
        ], 'jobs': [make_job(batch_id='a', state='running'), make_job(batch_id='b', state='blocked'),
                    make_job(batch_id='c'), make_job(batch_id='d', state='succeeded')]}
        self.assertEqual(queue_summary(snapshot), {'total': 4, 'active': 1, 'complete': 1, 'attention': 1, 'cap': 3})

    def test_launcher_has_no_public_bind_and_validates_port(self):
        self.assertEqual(parser().parse_args([]).port, 8091)
        self.assertEqual(port_number('65535'), 65535)
        for value in ('0', '65536', 'a'):
            with self.assertRaises(argparse.ArgumentTypeError):
                port_number(value)
        self.assertNotIn('--host', parser().format_help())
        self.assertIn('127.0.0.1', parser().format_help())

    def test_launcher_missing_dependency_has_local_setup_hint(self):
        from importlib.metadata import PackageNotFoundError
        with patch('tools.dashboard.metadata.version', side_effect=PackageNotFoundError('nicegui')):
            with self.assertRaisesRegex(ValueError, r'\.venv/bin/python -m pip install -r requirements-dashboard.txt'):
                check_dependencies()


class DashboardControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.runner, self.preview = services()
        self.controller = DashboardController(self.store, self.runner, self.preview)

    async def test_preview_is_read_only_and_preserves_ownership_blocker(self):
        draft = await self.controller.plan(['ru', 'de'], 2, 4)
        self.assertEqual([row['language'] for row in draft.plan['families']], ['ru', 'ru', 'de', 'de'])
        self.assertEqual(draft.plan['families'][0]['base_checkpoint'], 20)
        self.assertEqual(draft.plan['families'][0]['state'], 'blocked')
        self.store.create_batch.assert_not_called()
        self.runner.tick.assert_not_called()
        self.runner.resume_job.assert_not_called()

    async def test_creation_requires_ack_and_exact_frozen_scope_without_resume(self):
        draft = await self.controller.plan(['de', 'fr'], 2, 3)
        with self.assertRaises(ValueError):
            await self.controller.create(draft, False)
        self.store.create_batch.assert_not_called()
        self.assertEqual(await self.controller.create(draft, True), 'batch1')
        self.store.create_batch.assert_called_once_with(['de', 'fr'], 2, 3, {
            'paid_scope_acknowledged': True,
            'families': [{'language': language, 'base_checkpoint': base} for language in ('de', 'fr') for base in (20, 40)],
            'max_translation_attempts': 4,
        })
        self.store.resume.assert_not_called()
        self.runner.tick.assert_not_called()

    async def test_numeric_validation_happens_before_store_calls(self):
        for count, parallel in ((0, 1), (100000, 1), (1, 13), (1, 1.5)):
            with self.assertRaises(ValueError):
                await self.controller.plan(['de'], count, parallel)
        self.store.plan.assert_not_called()

    async def test_blocking_services_are_called_off_event_loop(self):
        main_thread = threading.get_ident()
        threads = []
        self.store.plan.side_effect = lambda languages, count: (threads.append(threading.get_ident()), make_plan())[1]
        await self.controller.plan(['de'], 1, 1)
        self.assertNotEqual(threads, [main_thread])

    async def test_batch_pause_does_not_stop_coordinator(self):
        await self.controller.batch_action('pause', 'batch1')
        self.store.pause.assert_called_once_with('batch1')
        self.runner.stop_job.assert_not_called()
        await self.controller.batch_action('resume', 'batch1')
        self.store.resume.assert_called_once_with('batch1')

    async def test_resume_requires_ack_and_never_replaces_missing_attempt_session(self):
        with self.assertRaises(ValueError):
            await self.controller.job_action('resume', 'job1')
        self.store.get_job.return_value = make_job(state='interrupted', attempts=[{'id': 'a'}])
        with self.assertRaisesRegex(ValueError, 'exact recorded session ID'):
            await self.controller.job_action('resume', 'job1', True)
        self.runner.resume_job.assert_not_called()
        self.store.get_job.return_value = make_job(state='interrupted', attempts=[{'id': 'a'}], session_id='ses_same')
        await self.controller.job_action('resume', 'job1', True)
        self.runner.resume_job.assert_called_once_with('job1')

    async def test_stop_requires_confirmation_and_reconcile_uses_real_verifier(self):
        with self.assertRaises(ValueError):
            await self.controller.job_action('stop', 'job1')
        self.runner.stop_job.assert_not_called()
        self.store.get_job.return_value = make_job(state='running')
        await self.controller.job_action('stop', 'job1', True)
        self.runner.stop_job.assert_called_once_with('job1')
        with self.assertRaises(ValueError):
            await self.controller.job_action('reconcile', 'job1')
        with self.assertRaises(ValueError):
            await self.controller.job_action('reconcile', 'job1', True)
        await self.controller.job_action('reconcile', 'job1', True, 'Inspected the repaired family.')
        self.runner.reconcile_job.assert_called_once_with('job1', 'Inspected the repaired family.')
        self.store.reconcile.assert_not_called()

    async def test_inspection_is_read_only_off_thread_and_handoff_requires_ack(self):
        threads = []
        self.runner.recovery_details.side_effect = lambda job: (threads.append(threading.get_ident()), make_recovery())[1]
        self.assertEqual((await self.controller.recovery_details('job1'))['session_id'], 'ses_same')
        self.assertNotEqual(threads, [threading.get_ident()])
        self.runner.handoff_job.assert_not_called()
        self.runner.resume_job.assert_not_called()
        self.runner.reconcile_job.assert_not_called()
        with self.assertRaises(ValueError):
            await self.controller.job_action('handoff', 'job1')
        self.runner.handoff_job.assert_not_called()
        await self.controller.job_action('handoff', 'job1', True)
        self.runner.handoff_job.assert_called_once_with('job1')
        self.runner.tick.assert_not_called()

    async def test_recovery_reuses_scheduler_lock_and_preserves_rejection(self):
        await self.controller.runner_lock.acquire()
        pending = asyncio.create_task(self.controller.recovery_details('job1'))
        await asyncio.sleep(0)
        self.runner.recovery_details.assert_not_called()
        self.controller.runner_lock.release()
        await pending
        self.runner.reconcile_job.side_effect = ValueError('Immutable family proof missing')
        with self.assertRaisesRegex(ValueError, 'Immutable family proof missing'):
            await self.controller.job_action('reconcile', 'job1', True, 'Checked locally')
        self.store.reconcile.assert_not_called()

    async def test_library_only_calls_catalog_and_render(self):
        await self.controller.catalog()
        result = await self.controller.render('packs/ru/000020', 'en', 0)
        self.preview.render.assert_called_once_with('packs/ru/000020', 'en', 0)
        self.assertIn('sandbox="allow-scripts"', result)
        self.store.create_batch.assert_not_called()

    async def test_one_server_scheduler_recovers_once_and_releases_without_killing(self):
        runtime = DashboardRuntime(self.controller, interval=0.01)
        await runtime.start()
        await runtime.start()
        await asyncio.sleep(0.035)
        await runtime.stop()
        self.runner.recover.assert_called_once_with()
        self.assertGreaterEqual(self.runner.tick.call_count, 1)
        self.runner.close.assert_called_once_with()
        self.runner.stop_job.assert_not_called()
        self.store.resume.assert_not_called()

    async def test_server_tick_is_nonoverlapping_and_shutdown_waits_for_inflight_call(self):
        active = 0
        maximum = 0
        def tick():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.025)
            active -= 1
            return []
        self.runner.tick.side_effect = tick
        runtime = DashboardRuntime(self.controller, interval=0.001)
        await runtime.start()
        await asyncio.sleep(0.01)
        await runtime.stop()
        self.assertEqual(maximum, 1)
        self.assertEqual(active, 0)
        self.runner.close.assert_called_once()


class DashboardOriginTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, kind='http', host='127.0.0.1:8091', origin=None, extra=()):
        reached = []
        messages = []
        async def target(scope, receive, send):
            reached.append(True)
            if kind == 'http':
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'OK'})
            else:
                await send({'type': 'websocket.accept'})
        async def send(message):
            messages.append(message)
        headers = [(b'host', host.encode())]
        if origin is not None:
            headers.append((b'origin', origin.encode()))
        headers.extend(extra)
        await LocalOnlyMiddleware(target, 8091)({'type': kind, 'headers': headers}, None, send)
        return reached, messages

    async def test_local_get_and_same_origin_are_allowed_with_frame_protection(self):
        for host, origin in [('127.0.0.1:8091', None), ('localhost:8091', 'http://localhost:8091')]:
            reached, messages = await self.request(host=host, origin=origin)
            self.assertTrue(reached)
            headers = dict(messages[0]['headers'])
            self.assertEqual(headers[b'content-security-policy'], b"frame-ancestors 'none'")
            self.assertEqual(headers[b'x-frame-options'], b'DENY')
        reached, messages = await self.request(kind='websocket', origin='http://127.0.0.1:8091')
        self.assertTrue(reached)
        self.assertEqual(messages[0]['type'], 'websocket.accept')

    async def test_http_and_websocket_reject_foreign_null_wrong_port_and_duplicate_origins(self):
        cases = [
            {'host': 'evil.invalid:8091'},
            {'origin': 'https://evil.invalid'},
            {'origin': 'null'},
            {'origin': 'http://127.0.0.1:8092'},
            {'origin': 'http://localhost:8091'},
            {'extra': [(b'host', b'localhost:8091')]},
            {'origin': 'http://127.0.0.1:8091', 'extra': [(b'origin', b'https://evil.invalid')]},
        ]
        for kind in ('http', 'websocket'):
            for case in cases:
                with self.subTest(kind=kind, case=case):
                    reached, messages = await self.request(kind=kind, **case)
                    self.assertFalse(reached)
                    if kind == 'http':
                        self.assertEqual(messages[0]['status'], 403)
                    else:
                        self.assertEqual(messages[0], {'type': 'websocket.close', 'code': 1008})


@unittest.skipUnless(importlib.util.find_spec('nicegui'), 'NiceGUI is optional in the offline authoring environment')
class DashboardNiceGUIStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nicegui import app
        from nicegui.storage import Storage
        self.environment = patch.dict(os.environ, {'PYTEST_CURRENT_TEST': self.id()})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage_path = patch.object(Storage, 'path', Path(self.temp.name) / 'storage')
        self.storage_path.start()
        self.addCleanup(self.storage_path.stop)
        app.storage = Storage()

    async def test_real_page_builds_without_launch_and_shows_blocker_preview(self):
        from nicegui.testing.user_simulation import user_simulation
        store, runner, preview = services()
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        def build():
            pages.append(DashboardPage(controller, runtime))
        async with user_simulation(build) as user:
            await user.open('/')
            await user.should_see('A little language. A complete family.')
            page = pages[0]
            with user:
                await page.initial_load()
                page.languages.set_value(['ru'])
                await page.preview_plan()
            await user.should_see('Legacy collision requires explicit reconciliation.')
            await user.should_see('Base checkpoints: 20')
            with user:
                page.show_confirmation()
            await user.should_see('Create a paused queue')
            await user.should_see('I authorize this exact family scope and bounded paid translation attempts.')
            store.create_batch.assert_not_called()
            runner.tick.assert_not_called()
            runner.recover.assert_not_called()
            runner.resume_job.assert_not_called()

    async def test_real_confirmation_requires_checkbox_then_creates_paused_batch_only(self):
        from nicegui.testing.user_simulation import user_simulation
        store, runner, preview = services()
        def created(*args):
            store.snapshot.return_value = {
                'batches': [{'id': 'batch1', 'languages': ['de'], 'max_parallel': 1, 'paused': True, 'state': 'paused'}],
                'jobs': [make_job()],
            }
            return 'batch1'
        store.create_batch.side_effect = created
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
                await page.preview_plan()
                page.show_confirmation()
            from nicegui import ui
            buttons = [element for element in user._client.elements.values() if isinstance(element, ui.button) and element.text == 'Create paused queue']
            self.assertEqual(len(buttons), 1)
            self.assertFalse(buttons[0].enabled)
            store.create_batch.assert_not_called()
            user.find('I authorize this exact family scope and bounded paid translation attempts.').click()
            self.assertTrue(buttons[0].enabled)
            user.find('Create paused queue').click()
            await user.should_see('Queue created paused. Resume scheduling is a separate action.')
            store.create_batch.assert_called_once()
            self.assertEqual(page.batch_select.value, 'batch1')
            self.assertTrue(page.resume_batch.enabled)
            self.assertFalse(page.pause_batch.enabled)
            runner.tick.assert_not_called()
            store.resume.assert_not_called()

    async def test_parallel_caps_validate_immediately_without_silent_clamping(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'batch1', 'languages': ['de', 'fr'], 'max_parallel': 2, 'paused': True}], 'jobs': [make_job()]}
        store, runner, preview = services(snapshot)
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
                page.parallel.set_value(13)
                page.parallel.sanitize()
                page.batch_parallel.set_value(13)
                page.batch_parallel.sanitize()
            self.assertEqual(page.parallel.value, 13)
            self.assertEqual(page.batch_parallel.value, 13)
            self.assertIn('from 1 to 12', page.parallel.error)
            self.assertIn('from 1 to 12', page.batch_parallel.error)
            self.assertFalse(page.plan_button.enabled)
            self.assertFalse(page.confirm_plan.enabled)
            self.assertFalse(page.parallel_save.enabled)
            with user:
                await page.refresh(force=True)
            self.assertFalse(page.parallel_save.enabled)
            store.plan.assert_not_called()
            store.set_parallel.assert_not_called()
            with user:
                page.parallel.set_value(12)
                page.batch_parallel.set_value(12)
            self.assertTrue(page.plan_button.enabled)
            self.assertTrue(page.parallel_save.enabled)
            self.assertIn('at most 1 coordinator', page.parallel_hint.text)
            self.assertIn('at most 2 coordinators', page.batch_cap_hint.text)
            await user.should_see('125–250 questions')
            runner.tick.assert_not_called()

    async def test_refresh_does_not_rebuild_selected_views_for_unrelated_updates(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'batch1', 'languages': ['de'], 'max_parallel': 2, 'paused': True},
                                {'id': 'batch2', 'languages': ['fr'], 'max_parallel': 3, 'paused': True}],
                    'jobs': [make_job(), make_job(id='job2', batch_id='batch2', language='fr')]}
        store, runner, preview = services(snapshot)
        preview.catalog.return_value = [{'relative': 'packs/ru/000020', 'label': 'Russian 20', 'locales': ['en'], 'stages': [0]}]
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
                await page.render_preview()
            frame = list(page.preview_host)[0]
            with patch.object(page.jobs_table, 'update', wraps=page.jobs_table.update) as table_update, patch.object(page.job_details, 'clear', wraps=page.job_details.clear) as details_clear:
                with user:
                    for number in range(25):
                        store.snapshot.return_value = deepcopy(snapshot)
                        store.snapshot.return_value['jobs'][1]['reason'] = f'Unrelated update {number}'
                        await page.refresh(force=True)
                        await page.render_preview()
                    await page.load_catalog()
                self.assertEqual(table_update.call_count, 0)
                self.assertEqual(details_clear.call_count, 0)
            self.assertIs(list(page.preview_host)[0], frame)
            self.assertEqual(page.jobs_table.selected[0]['id'], 'job1')
            preview.render.assert_called_once()
            runner.recovery_details.assert_not_called()
            with user:
                await page.render_preview(force=True)
            self.assertEqual(preview.render.call_count, 2)
            self.assertIsNot(list(page.preview_host)[0], frame)

    async def test_active_batch_defaults_and_history_selection_is_preserved(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'old', 'languages': ['de'], 'paused': True, 'max_parallel': 12, 'created_at': 1}, {'id': 'new', 'languages': ['de'], 'paused': False, 'max_parallel': 12, 'created_at': 2}], 'jobs': [make_job(id='old-job', batch_id='old', state='succeeded'), make_job(id='new-job', batch_id='new', state='running', session_id='ses_live')]}
        store, runner, preview = services(snapshot)
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
            self.assertEqual(page.batch_select.value, 'new')
            self.assertEqual(page.job_select.value, 'new-job')
            self.assertEqual([r['id'] for r in page.active_table.rows], ['new-job'])
            with user:
                page.batch_select.set_value('old')
                page.update_batch()
                await page.refresh(force=True)
            self.assertEqual(page.batch_select.value, 'old')
            self.assertEqual(page.active_table.rows, [])
            self.assertTrue(page.show_active_batch.visible)
            with user:
                page.select_active_batch()
                page.update_batch()
            self.assertEqual(page.batch_select.value, 'new')
            self.assertFalse(page.show_active_batch.visible)
            self.assertEqual(page.job_select.value, 'new-job')
            store.resume.assert_not_called()
            runner.resume_job.assert_not_called()
            runner.tick.assert_not_called()

    async def test_active_workers_show_all_twelve_without_queued_history_and_keep_selection(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'batch1', 'languages': ['de'], 'max_parallel': 12, 'paused': False}],
                    'jobs': [make_job(id=f'job{index}', state='running' if index % 2 else 'launching') for index in range(10)]
                            + [make_job(id='recovery1', state='interrupted', session_id='ses_recovery1'),
                               make_job(id='recovery2', state='blocked', session_id='ses_recovery2'),
                                make_job(id='queued', state='queued'),
                                make_job(id='waiting-predecessor', state='blocked')]}
        store, runner, preview = services(snapshot)
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
                page.job_select.set_value('job7')
                await page.refresh(force=True)
            self.assertEqual(len(page.active_table.rows), 12)
            active_ids = {row['id'] for row in page.active_table.rows}
            self.assertNotIn('queued', active_ids)
            self.assertEqual(active_ids & {'recovery1', 'recovery2'}, {'recovery1', 'recovery2'})
            self.assertEqual(page.active_table.rows[10]['session_id'], 'ses_recovery1')
            self.assertEqual(page.active_table.rows[10]['status'], 'Interrupted')
            self.assertEqual(page.active_table.rows[11]['status'], 'Blocked')
            self.assertEqual(len(page.jobs_table.rows), 14)
            self.assertNotIn('waiting-predecessor', active_ids)
            self.assertEqual(page.job_select.value, 'job7')
            self.assertEqual(page.active_table.selected[0]['id'], 'job7')
            self.assertEqual(page.jobs_table.selected[0]['id'], 'job7')

    async def test_external_slot_shows_cancel_stop_and_end_without_resolving_actions(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'batch1', 'languages': ['de'], 'max_parallel': 1, 'paused': True}],
                    'jobs': [make_job(state='launching', session_id='ses_same', launch_metadata={'mode': 'external', 'phase': 'awaiting_terminal'})]}
        store, runner, preview = services(snapshot)
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
            self.assertTrue(page.stop_job_button.enabled)
            self.assertEqual(page.stop_job_button.text, 'Cancel unstarted handoff')
            self.assertFalse(page.resume_job_button.enabled)
            self.assertTrue(page.inspect_job_button.enabled)
            self.assertEqual(page.stats['active'].text, '1')
            self.assertEqual(page.jobs_table.rows[0]['status'], 'Manual recovery · slot held')
            with user:
                store.snapshot.return_value = deepcopy(snapshot)
                store.snapshot.return_value['jobs'][0]['launch_metadata']['phase'] = 'running'
                await page.refresh()
            self.assertTrue(page.stop_job_button.enabled)
            self.assertEqual(page.stop_job_button.text, 'Stop active coordinator')
            with user:
                store.snapshot.return_value = deepcopy(snapshot)
                store.snapshot.return_value['jobs'][0]['launch_metadata']['phase'] = 'stopped'
                await page.refresh()
            self.assertTrue(page.stop_job_button.enabled)
            self.assertEqual(page.stop_job_button.text, 'End recovery without resolving')
            self.assertTrue(page.inspect_job_button.enabled)
            with user:
                page.confirm_job_action('stop')
            await user.should_see('I want to end this stopped recovery attempt without marking the family complete.')
            runner.stop_job.assert_not_called()
            runner.reconcile_job.assert_not_called()

    async def test_runtime_readiness_updates_controls_even_with_identical_snapshot(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'batch1', 'languages': ['de'], 'max_parallel': 1, 'paused': True}], 'jobs': [make_job(state='blocked')]}
        store, runner, preview = services(snapshot)
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.refresh()
            self.assertFalse(page.resume_batch.enabled)
            self.assertFalse(page.inspect_job_button.enabled)
            runtime.ready = True
            with user:
                await page.refresh()
            self.assertTrue(page.resume_batch.enabled)
            self.assertTrue(page.inspect_job_button.enabled)
            self.assertTrue(page.resume_job_button.enabled)

    async def test_recovery_inspection_and_copy_do_not_resume_or_resolve(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'batch1', 'languages': ['de'], 'max_parallel': 1, 'paused': True}],
                    'jobs': [make_job(state='interrupted', session_id='ses_same', attempt_id='a1',
                                      attempts=[{'id': 'a1', 'state': 'interrupted', 'session_id': 'ses_same'}])]}
        store, runner, preview = services(snapshot)
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
                await page.inspect_recovery()
            await user.should_see('Inspect the saved session')
            from nicegui import ui
            inputs = [element for element in user._client.elements.values() if isinstance(element, ui.input)]
            self.assertGreaterEqual(sum(element.value == 'ses_same' for element in inputs), 3)
            with patch.object(ui.clipboard, 'write') as copy:
                with user:
                    page.copy_table_session(type('Event', (), {'args': 'job1'})())
                copy.assert_called_once_with('ses_same')
            runner.recovery_details.assert_called_once_with('job1')
            runner.resume_job.assert_not_called()
            runner.handoff_job.assert_not_called()
            runner.reconcile_job.assert_not_called()
            runner.tick.assert_not_called()
            self.assertIn('session_id', [column['field'] for column in page.jobs_table.columns])

    async def test_recovery_eligibility_is_backend_owned_and_diagnostics_are_redacted(self):
        from nicegui.testing.user_simulation import user_simulation
        from nicegui import ui
        store, runner, preview = services()
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        pages = []
        details = make_recovery(can_handoff=False, can_resolve=False, inspect_command=None, session_id=None,
            handoff_reason='Saved environment unavailable', process={'supervisor': 'unknown', 'child': 'unknown',
                'group': 'unknown', 'lock': 'held', 'diagnostic': 'token=PRIVATE https://private.invalid/?key=SECRET'})
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            with user:
                pages[0].show_recovery(details)
            buttons = [element for element in user._client.elements.values() if isinstance(element, ui.button)
                       and element.text in ('Reserve manual terminal recovery…', 'Verify repaired family…')]
            self.assertEqual(len(buttons), 2)
            self.assertTrue(all(not button.enabled for button in buttons))
            labels = ' '.join(element.text for element in user._client.elements.values() if isinstance(element, ui.label))
            self.assertNotIn('PRIVATE', labels)
            self.assertNotIn('SECRET', labels)
            self.assertIn('Saved environment unavailable', labels)
            self.assertIn('Session identity cannot be supplied or guessed', labels)
            runner.handoff_job.assert_not_called()
            runner.reconcile_job.assert_not_called()

    async def test_manual_recovery_and_resolution_require_explicit_confirmation(self):
        from nicegui.testing.user_simulation import user_simulation
        from nicegui import ui
        store, runner, preview = services()
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            inspection = Mock()
            with user:
                page.confirm_recovery_action('handoff', make_recovery(), inspection)
            button = next(element for element in user._client.elements.values() if isinstance(element, ui.button) and element.text == 'Reserve manual recovery')
            self.assertFalse(button.enabled)
            user.find('I authorize manual recovery of this exact session with its existing paid-request limits.').click()
            self.assertTrue(button.enabled)
            user.find('Reserve manual recovery').click()
            await user.should_see('Tracked terminal recovery command')
            runner.handoff_job.assert_called_once_with('job1')
            runner.resume_job.assert_not_called()
            runner.tick.assert_not_called()
            with user:
                page.confirm_recovery_action('reconcile', make_recovery(), inspection)
            button = next(element for element in user._client.elements.values() if isinstance(element, ui.button) and element.text == 'Verify and resolve')
            user.find('I have stopped manual work and request independent verification, not forced completion.').click()
            self.assertFalse(button.enabled)
            note = next(element for element in user._client.elements.values() if isinstance(element, ui.textarea) and element.label == 'What was repaired or verified?')
            with user:
                note.set_value('Reviewed immutable family and stopped manual work.')
            self.assertTrue(button.enabled)
            runner.reconcile_job.side_effect = ValueError('Complete immutable proof missing')
            user.find('Verify and resolve').click()
            await user.should_see('Complete immutable proof missing')
            runner.reconcile_job.assert_called_once_with('job1', 'Reviewed immutable family and stopped manual work.')
            store.reconcile.assert_not_called()

    async def test_refresh_preserves_selection_inputs_and_library_embeds_only_iframe(self):
        from nicegui.testing.user_simulation import user_simulation
        snapshot = {'batches': [{'id': 'batch1', 'languages': ['de'], 'max_parallel': 2, 'paused': True, 'state': 'paused'}],
                    'jobs': [make_job(), make_job(id='job2', base_checkpoint=40)]}
        store, runner, preview = services(snapshot)
        preview.catalog.return_value = [{'relative': 'packs/ru/000020', 'label': 'Russian 20 · Legacy finalized',
                                         'locales': ['en', 'fr'], 'stages': [0]}]
        controller = DashboardController(store, runner, preview)
        runtime = DashboardRuntime(controller)
        runtime.ready = True
        pages = []
        async with user_simulation(lambda: pages.append(DashboardPage(controller, runtime))) as user:
            await user.open('/')
            page = pages[0]
            with user:
                await page.initial_load()
                page.job_select.set_value('job2')
                page.batch_parallel.set_value(7)
                page.locale_select.set_value('fr')
                store.snapshot.return_value = deepcopy(snapshot)
                store.snapshot.return_value['jobs'][0]['state'] = 'running'
                await page.refresh(force=True)
                await page.load_catalog()
                await page.render_preview()
            self.assertEqual(page.job_select.value, 'job2')
            self.assertEqual(page.batch_parallel.value, 7)
            self.assertEqual(page.locale_select.value, 'fr')
            preview.render.assert_called_once_with('packs/ru/000020', 'fr', 0)
            frame = list(page.preview_host)[0]
            self.assertIn('sandbox="allow-scripts"', frame.content)
            self.assertFalse(frame._props['sanitize'])
            self.assertFalse(page.stop_job_button.enabled)
            self.assertFalse(page.resume_job_button.enabled)
            with user:
                page.locale_select.set_value('en')
            self.assertIsNone(page.rendered_selection)
            self.assertNotIn(frame, list(page.preview_host))
            self.assertIn('Selection changed', page.library_status.text)
            runner.tick.assert_not_called()


if __name__ == '__main__':
    unittest.main()
