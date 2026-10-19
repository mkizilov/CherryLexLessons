"""Synthetic-only correction edition / NiceGUI integration, no live queue or browser."""
import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from lesson_factory import core, correction_edition as edition
from lesson_factory.dashboard_preview import PreviewError, PreviewService, _CorrectionInput
from lesson_factory.dashboard_ui import DashboardController, DashboardPage, DashboardRuntime

# The independent engine's synthetic fixture uses real parser/protected contracts,
# but marks ordinary family-approval setup as synthetic and never reads live packs.
FIXTURE = Path(__file__).with_name('test_correction_edition.py')
loader = importlib.util.spec_from_file_location('correction_preview_fixture', FIXTURE)
fixture = importlib.util.module_from_spec(loader)
loader.loader.exec_module(fixture)


class CorrectionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CorrectionEditionTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.family = self.fixture.relative
        self.identity = f'correction-edition:synthetic-1:{self.family}'
        self.service = PreviewService(self.root)
        # Renderer is loaded from the disposable overlay; content root is synthetic.
        self.service.code_root = fixture.PROJECT

    def build(self, reviewed=False):
        if reviewed:
            self.fixture.approve()
        self.receipt = edition.build(self.root, self.fixture.spec_ref(), draft=not reviewed)
        return self.receipt

    def test_catalog_opt_in_is_cheap_distinct_and_never_a_latest_alias(self):
        self.build()
        with patch.object(edition, 'verify', side_effect=AssertionError('Catalog must not deep-verify')), patch.object(edition, 'resolution_map', side_effect=AssertionError('Catalog must not resolve')):
            self.assertTrue(all(row['kind'] != 'correction' for row in self.service.catalog()))
            rows = self.service.catalog(include_corrections=True)
        corrected = next(row for row in rows if row['kind'] == 'correction')
        self.assertEqual(corrected['relative'], self.identity)
        self.assertEqual(corrected['locales'], list(core.learner_locales('de')))
        self.assertEqual(corrected['stages'], list(range(5)))
        self.assertEqual(corrected['receipt_sha256'], self.receipt['receipt_sha256'])
        self.assertIn('unverified until render', corrected['label'])
        self.assertIn('draft', corrected['label'])
        self.assertTrue(any(row['relative'] == self.family for row in rows))
        self.assertNotEqual(corrected['relative'], self.family)

    def test_catalog_bounds_receipts_and_edition_count_and_surfaces_invalid_receipt(self):
        self.build()
        with patch.object(self.service, 'MAX_RECEIPT', 4):
            with self.assertRaisesRegex(PreviewError, 'exceeds'):
                self.service.catalog(include_corrections=True)
        with patch.object(self.service, 'MAX_EDITIONS', 0):
            with self.assertRaisesRegex(PreviewError, 'exceeds'):
                self.service.catalog(include_corrections=True)
        path = self.root / 'work/correction-editions/synthetic-1/receipt.json'
        path.chmod(0o644)
        path.write_text('{}')
        with self.assertRaisesRegex(PreviewError, 'Invalid correction preview receipt'):
            self.service.catalog(include_corrections=True)
        # Explicit opt-out does not consult a broken correction directory at all.
        self.service.catalog()

    def test_private_in_progress_editions_are_not_catalog_entries(self):
        folder = self.root / 'work/correction-editions/.edition-in-progress'
        folder.mkdir(parents=True)
        (folder / 'receipt.json').write_text('incomplete')
        self.assertFalse(any(row['kind'] == 'correction' for row in self.service.catalog(include_corrections=True)))

    def test_render_reads_corrected_content_original_briefs_and_one_full_verify(self):
        self.build(reviewed=True)
        with patch.object(edition, 'verify', wraps=edition.verify) as verify, patch.object(edition, 'resolution_map', wraps=edition.resolution_map) as resolve:
            report = self.service.render_report(self.identity, 'fr', 1)
        verify.assert_called_once_with(self.root, 'synthetic-1')
        resolve.assert_called_once_with(self.root, 'synthetic-1', self.family)
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(report['mode'], 'practice')
        self.assertIn('Synthetic title correction', json.dumps(report))
        meta = report['correction_preview']
        self.assertEqual(meta['receipt_sha256'], self.receipt['receipt_sha256'])
        self.assertFalse(meta['production_approved'])
        for label in ('Draft correction preview', 'scoped-reviewed', 'old reviews are not correction approvals', 'NOT production approved', 'no app adoption or canonical history change'):
            self.assertIn(label, report['scope'])
        self.assertNotIn('reports', meta)
        self.assertNotIn('spec', meta)
        original = self.service.render_report(self.family, 'fr', 0)
        self.assertNotIn('correction_preview', original)
        self.assertIn('Original pack files', original['scope'])
        self.assertNotIn('Synthetic title correction', json.dumps(original))

    def test_draft_receipt_never_becomes_a_scoped_review_or_production_approval(self):
        self.build()
        report = self.service.render_report(self.identity, 'fr', 0)
        self.assertEqual(report['correction_preview']['review_status'], 'draft')
        self.assertIn('Draft correction preview · draft', report['scope'])
        self.assertIn('Draft status grants no scoped review approval', report['scope'])
        self.assertNotIn('Scoped correction reviews only', report['scope'])
        self.assertFalse(report['correction_preview']['production_approved'])

    def test_strict_identity_rejects_aliases_traversal_unknown_ids_and_wrong_pairs(self):
        self.build()
        for relative in ('correction-edition', f'correction-edition::{self.family}', f'correction-edition:../synthetic-1:{self.family}', f'correction-edition:synthetic-1:{self.family}/', 'correction-edition:synthetic-1:family_runs/de/000020', f'correction-edition:synthetic-1:{self.family}:extra', f'correction-edition:latest:{self.family}', 'correction-edition:synthetic-1:family_packs/de/000040'):
            with self.subTest(relative=relative), self.assertRaises((PreviewError, core.AuthoringError, OSError)):
                self.service.render_report(relative, 'fr', 0)
        for locale, stage in (('de', 0), ('xx', 0), ('fr', True), ('fr', '0'), ('fr', 5)):
            with self.subTest(locale=locale, stage=stage), self.assertRaises(PreviewError):
                self.service.render_report(self.identity, locale, stage)
        # Original generic identity validation remains unchanged.
        for relative in ('work/correction-editions/synthetic-1/effective/' + self.family, '../' + self.family):
            with self.assertRaises(PreviewError):
                self.service._identity(relative)

    def test_read_map_excludes_old_reviews_context_and_unselected_files(self):
        self.build()
        mapping = edition.resolution_map(self.root, 'synthetic-1', self.family)
        self.assertEqual(len(mapping), 170)
        view = _CorrectionInput(self.service, {name: str(path.relative_to(self.root)) for name, path in mapping.items()}, {})
        for relative in (self.family + '/review-final.json', self.family + '/family-context.json', 'family_packs/de/000040/fr/stage-0/lesson.md'):
            with self.subTest(relative=relative), self.assertRaisesRegex(PreviewError, 'outside exact'):
                view._read(relative, optional=True)

    def test_map_cannot_redirect_to_source_or_drop_expected_member(self):
        self.build()
        mapping = edition.resolution_map(self.root, 'synthetic-1', self.family)
        name = self.family + '/' + self.fixture.member
        redirected = dict(mapping, **{name: self.root / name})
        with patch.object(edition, 'resolution_map', return_value=redirected):
            with self.assertRaisesRegex(PreviewError, 'map or file pins'):
                self.service.render_report(self.identity, 'fr', 0)
        mapping.pop(name)
        with patch.object(edition, 'resolution_map', return_value=mapping):
            with self.assertRaisesRegex(PreviewError, 'exactly 165'):
                self.service.render_report(self.identity, 'fr', 0)

    def test_payload_and_original_brief_hash_on_read_reject_post_verify_races(self):
        self.build()
        resolve = edition.resolution_map
        for name in (self.family + '/' + self.fixture.member, self.family + '/stage-1/brief.json'):
            with self.subTest(name=name):
                target = None
                original = None
                def raced(*args):
                    nonlocal target, original
                    mapping = resolve(*args)
                    target = mapping[name]
                    original = target.read_bytes()
                    target.chmod(0o644)
                    target.write_bytes(original + b' ')
                    return mapping
                try:
                    with patch.object(edition, 'resolution_map', side_effect=raced):
                        with self.assertRaisesRegex(PreviewError, 'changed after verification'):
                            self.service.render_report(self.identity, 'fr', 1)
                finally:
                    if target is not None:
                        target.write_bytes(original)

    def test_receipt_change_between_verification_and_input_is_rejected(self):
        self.build()
        receipt = deepcopy(self.receipt)
        changed = dict(receipt, receipt_sha256='a' * 64)
        with patch.object(self.service, '_receipt', side_effect=[receipt, changed]):
            with self.assertRaisesRegex(PreviewError, 'receipt changed'):
                self.service.render_report(self.identity, 'fr', 0)

    def test_no_symlink_or_special_file_after_verify_and_no_missing_file_fallback(self):
        self.build()
        resolve = edition.resolution_map
        name = self.family + '/' + self.fixture.member
        target = None
        original = None
        def raced(*args):
            nonlocal target, original
            mapping = resolve(*args)
            target = mapping[name]
            original = target.read_bytes()
            target.parent.chmod(0o755)
            target.unlink()
            target.symlink_to(self.root / name)
            return mapping
        with patch.object(edition, 'resolution_map', side_effect=raced):
            with self.assertRaises((PreviewError, core.AuthoringError)):
                self.service.render_report(self.identity, 'fr', 0)
        target.unlink()
        target.write_bytes(original)
        target.unlink()
        with self.assertRaises((PreviewError, core.AuthoringError, OSError)):
            self.service.render_report(self.identity, 'fr', 0)

    def test_csp_language_metadata_and_sandbox_bundles_preserved(self):
        self.build()
        document = self.service.render(self.identity, 'ar', 0)
        self.assertIn('<html lang="en" dir="ltr">', document)
        self.assertIn("default-src &#x27;none&#x27;", document)
        self.assertIn("connect-src &#x27;none&#x27;", document)
        self.assertIn('receipt SHA256', document)
        self.assertIn('authorMetadata', document)
        self.assertIn('"locale": "ar"', document)
        self.assertIn('element.lang = lang', document)
        self.assertIn('element.dir = G.dirOf(lang)', document)
        self.assertNotIn('synthetic:independent', document)
        self.assertNotIn('NOT real approval', document)


@unittest.skipUnless(importlib.util.find_spec('nicegui'), 'NiceGUI optional')
class CorrectionNiceGUIStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nicegui import app
        from nicegui.storage import Storage
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'PYTEST_CURRENT_TEST': self.id()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.storage = patch.object(Storage, 'path', Path(self.temp.name) / 'storage')
        self.storage.start()
        self.addCleanup(self.storage.stop)
        app.storage = Storage()
        self.original = dict(kind='finalized', relative='family_packs/de/000020', label='DE · 20 · original pack files', locales=['en', 'fr'], stages=[0, 1], base_checkpoint=20)
        self.corrected = dict(self.original, kind='correction', relative='correction-edition:exact-id:family_packs/de/000020', label='DE · 20 · correction preview — unverified until render · draft · exact-id')
        self.preview = Mock()
        self.preview.catalog.side_effect = lambda **kw: [self.original, self.corrected] if kw.get('include_corrections') else [self.original]
        self.preview.render.return_value = '<!doctype html><html><body>Fixture preview</body></html>'
        self.store, self.runner = Mock(), Mock()
        self.store.snapshot.return_value = {'batches': [], 'jobs': []}
        self.controller = DashboardController(self.store, self.runner, self.preview)
        self.runtime = DashboardRuntime(self.controller)
        self.pages = []

    async def opt_in(self, page, value):
        request = getattr(page, 'catalog_request', 0)
        page.correction_opt_in.set_value(value)
        # NiceGUI dispatches async on_change handlers as tasks. Wait for the real
        # callback instead of starting a competing direct refresh in the test.
        async with asyncio.timeout(5):
            while getattr(page, 'catalog_request', 0) <= request or page.catalog_loading:
                await asyncio.sleep(0.01)

    async def test_explicit_opt_in_rows_selection_clear_and_opt_out_no_queue_calls(self):
        from nicegui.testing.user_simulation import user_simulation
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await page.initial_load()
                await page.render_preview()
            self.assertFalse(page.correction_opt_in.value)
            self.assertNotIn(self.corrected['relative'], page.family_select.options)
            original_frame = list(page.preview_host)[0]
            with user:
                await self.opt_in(page, True)
            self.assertEqual(page.family_select.value, self.original['relative'])
            self.assertIs(list(page.preview_host)[0], original_frame)
            self.assertIn(self.corrected['relative'], page.family_select.options)
            with user:
                page.family_select.set_value(self.corrected['relative'])
            self.assertIsNone(page.rendered_selection)
            self.assertNotIn(original_frame, list(page.preview_host))
            with user:
                await page.render_preview()
            self.assertEqual(page.rendered_selection[0], self.corrected['relative'])
            corrected_frame = list(page.preview_host)[0]
            self.assertIn('sandbox="allow-scripts"', corrected_frame.content)
            with user:
                await self.opt_in(page, False)
            self.assertNotIn(self.corrected['relative'], page.family_select.options)
            self.assertNotIn(corrected_frame, list(page.preview_host))
            self.assertIsNone(page.rendered_selection)
            self.assertFalse(self.runner.mock_calls)
            self.assertTrue(all(call[0] == 'snapshot' for call in self.store.mock_calls))

    async def test_correction_only_catalog_never_auto_selects_and_catalog_errors_clear_rows(self):
        from nicegui.testing.user_simulation import user_simulation
        self.preview.catalog.side_effect = lambda **kw: [self.corrected] if kw.get('include_corrections') else []
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await self.opt_in(page, True)
            self.assertIsNone(page.family_select.value)
            self.assertFalse(page.render_button.enabled)
            with user:
                page.family_select.set_value(self.corrected['relative'])
                await page.render_preview()
            self.assertIsNotNone(page.rendered_selection)
            self.preview.catalog.side_effect = PreviewError('Invalid correction preview receipt: exact-id')
            with user:
                await page.load_catalog()
            self.assertEqual(page.family_select.options, {})
            self.assertIsNone(page.rendered_selection)
            self.assertIn('Invalid correction preview receipt', page.library_status.text)

    async def test_inflight_original_cannot_replace_changed_correction_selection(self):
        from nicegui.testing.user_simulation import user_simulation
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_render(*args):
            started.set()
            await release.wait()
            return '<iframe>OLD ORIGINAL</iframe>'
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await self.opt_in(page, True)
                with patch.object(self.controller, 'render', side_effect=delayed_render):
                    task = asyncio.create_task(page.render_preview())
                    await started.wait()
                    page.family_select.set_value(self.corrected['relative'])
                    release.set()
                    await task
            self.assertIsNone(page.rendered_selection)
            self.assertFalse(any('OLD ORIGINAL' in getattr(child, 'content', '') for child in page.preview_host))
            self.assertIn('Selection changed', page.library_status.text)

    async def test_opt_out_revokes_rows_before_delayed_refresh_and_old_opt_in_cannot_win(self):
        from nicegui.testing.user_simulation import user_simulation
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await self.opt_in(page, True)
                page.family_select.set_value(self.corrected['relative'])
                await page.render_preview()
            frame = list(page.preview_host)[0]
            old_started, old_release = asyncio.Event(), asyncio.Event()
            new_started, new_release = asyncio.Event(), asyncio.Event()
            async def delayed_catalog(include_corrections=False):
                if include_corrections:
                    old_started.set()
                    await old_release.wait()
                    return [self.original, self.corrected]
                new_started.set()
                await new_release.wait()
                return [self.original]
            with patch.object(self.controller, 'catalog', side_effect=delayed_catalog):
                with user:
                    stale = asyncio.create_task(page.load_catalog())
                    await old_started.wait()
                    page.correction_opt_in.set_value(False)
                    await new_started.wait()
                    self.assertNotIn(self.corrected['relative'], page.family_select.options)
                    self.assertNotIn(frame, list(page.preview_host))
                    self.assertIsNone(page.rendered_selection)
                    new_release.set()
                    while page.catalog_loading:
                        await asyncio.sleep(0.01)
                    old_release.set()
                    await stale
            self.assertNotIn(self.corrected['relative'], page.family_select.options)
            self.assertEqual(page.family_select.value, self.original['relative'])

    async def test_invalid_empty_selection_never_calls_render(self):
        from nicegui.testing.user_simulation import user_simulation
        self.preview.catalog.return_value = []
        self.preview.catalog.side_effect = None
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await page.load_catalog()
                await page.render_preview(force=True)
            self.preview.render.assert_not_called()
            self.assertIsNone(page.rendered_selection)

    async def test_opt_out_opt_in_same_tuple_cannot_install_revoked_render(self):
        from nicegui.testing.user_simulation import user_simulation
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_render(*args):
            started.set()
            await release.wait()
            return '<iframe>REVOKED CORRECTION</iframe>'
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await self.opt_in(page, True)
                page.family_select.set_value(self.corrected['relative'])
                selected = (page.family_select.value, page.locale_select.value, page.stage_select.value)
                with patch.object(self.controller, 'render', side_effect=delayed_render) as render:
                    task = asyncio.create_task(page.render_preview())
                    await started.wait()
                    await self.opt_in(page, False)
                    await self.opt_in(page, True)
                    page.family_select.set_value(self.corrected['relative'])
                    self.assertEqual((page.family_select.value, page.locale_select.value, page.stage_select.value), selected)
                    release.set()
                    await task
                    render.assert_awaited_once_with(*selected)
                self.assertIsNone(page.rendered_selection)
                self.assertFalse(any('REVOKED CORRECTION' in getattr(child, 'content', '') for child in page.preview_host))
                self.assertTrue(page.render_button.enabled)
                self.preview.render.assert_not_called()
                await page.render_preview()
            # Returning to the row alone is not fresh consent. A new Render calls
            # the real controller/backend boundary, performing a fresh verify.
            self.preview.render.assert_called_once_with(*selected)
            self.assertEqual(page.rendered_selection, selected)
            self.assertIn('Fixture preview', list(page.preview_host)[0].content)
            self.assertFalse(self.runner.mock_calls)

    async def test_selection_aba_cannot_install_old_render(self):
        from nicegui.testing.user_simulation import user_simulation
        for field, alternate in (('family_select', self.original['relative']), ('locale_select', 'fr'), ('stage_select', 1)):
            with self.subTest(field=field):
                self.pages = []
                self.preview.render.reset_mock()
                started, release = asyncio.Event(), asyncio.Event()
                async def delayed_render(*args):
                    started.set()
                    await release.wait()
                    return '<iframe>STALE ABA RENDER</iframe>'
                async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
                    await user.open('/')
                    page = self.pages[0]
                    with user:
                        await self.opt_in(page, True)
                        page.family_select.set_value(self.corrected['relative'])
                        selected = (page.family_select.value, page.locale_select.value, page.stage_select.value)
                        control = getattr(page, field)
                        original_value = control.value
                        with patch.object(self.controller, 'render', side_effect=delayed_render):
                            task = asyncio.create_task(page.render_preview())
                            await started.wait()
                            control.set_value(alternate)
                            control.set_value(original_value)
                            self.assertEqual((page.family_select.value, page.locale_select.value, page.stage_select.value), selected)
                            release.set()
                            await task
                        self.assertIsNone(page.rendered_selection)
                        self.assertFalse(any('STALE ABA RENDER' in getattr(child, 'content', '') for child in page.preview_host))
                        self.assertTrue(page.render_button.enabled)
                        await page.render_preview()
                    self.preview.render.assert_called_once_with(*selected)
                    self.assertEqual(page.rendered_selection, selected)

    async def test_same_selection_catalog_refresh_and_queue_poll_keep_inflight_render_and_loaded_frame(self):
        from nicegui.testing.user_simulation import user_simulation
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_render(*args):
            started.set()
            await release.wait()
            return '<iframe>CURRENT UNCHANGED RENDER</iframe>'
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await self.opt_in(page, True)
                page.family_select.set_value(self.corrected['relative'])
                selected = (page.family_select.value, page.locale_select.value, page.stage_select.value)
                with patch.object(self.controller, 'render', side_effect=delayed_render) as render:
                    task = asyncio.create_task(page.render_preview())
                    await started.wait()
                    await page.load_catalog()
                    await page.refresh(force=True)
                    page.invalidate_preview()
                    release.set()
                    await task
                    render.assert_awaited_once_with(*selected)
                self.assertEqual(page.rendered_selection, selected)
                frame = list(page.preview_host)[0]
                self.assertIn('CURRENT UNCHANGED RENDER', frame.content)
                for _ in range(3):
                    await page.load_catalog()
                    await page.refresh(force=True)
                    page.invalidate_preview()
                self.assertIs(list(page.preview_host)[0], frame)
                self.assertEqual(page.rendered_selection, selected)
                self.assertFalse(page.render_button.enabled)

    async def test_failed_correction_reload_surfaces_error_without_changing_loaded_proof(self):
        from nicegui.testing.user_simulation import user_simulation
        async with user_simulation(lambda: self.pages.append(DashboardPage(self.controller, self.runtime))) as user:
            await user.open('/')
            page = self.pages[0]
            with user:
                await self.opt_in(page, True)
                page.family_select.set_value(self.corrected['relative'])
                await page.render_preview()
            frame = list(page.preview_host)[0]
            self.preview.render.side_effect = PreviewError('Correction preview file changed after verification')
            with user:
                await page.render_preview(force=True)
            self.assertIs(list(page.preview_host)[0], frame)
            self.assertIn('Reload failed; the previous loaded preview is unchanged', page.library_status.text)
            self.assertIn('changed after verification', page.library_status.text)


if __name__ == '__main__':
    unittest.main()
