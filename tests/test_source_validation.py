"""Source proof reuse remains byte-bound, invocation-local, and cancellation aware."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import unittest
from unittest.mock import patch

from lesson_factory import core, family
from lesson_factory.source_validation import SourceValidation, VALIDATOR_FILES
from lesson_factory.translation import TranslationError


class SourceValidationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_family_core import FamilyCoreTests
        self.fixture = FamilyCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run = self.fixture.root, self.fixture.run
        self.fixture.author()
        for role in ('pedagogical', 'assessment', 'format'):
            family.submit_family_review(self.root, self.run, role, self.fixture.report(role))
        for name in VALIDATOR_FILES:
            p = self.root / name
            p.parent.mkdir(exist_ok=True)
            p.write_text('synthetic validator')
        self.context = family.load_family(self.root, self.run)
        self.stop = threading.Event()
        self.proof = SourceValidation(self.root, self.run, self.context, self.stop)

    def test_warm_gates_share_proof_not_parser_but_reload_full_context(self):
        self.proof.establish(10)
        before = self.proof.original
        self.assertEqual(before, {'binding': family.family_binding(self.root, self.run, 'pedagogical'), 'context_digest': core.digest(self.root / self.run / 'family-context.json'), 'reviews': {role: core.digest(family.family_review_path(self.root, self.run, role)) for role in ('pedagogical', 'assessment', 'format')}})
        with patch.object(family, 'validate_family', side_effect=AssertionError('no repeated parser')), patch.object(family, 'load_family', wraps=family.load_family) as load:
            with ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(lambda _: self.proof.check(10), range(10)))
        self.assertEqual(load.call_count, 10)
        self.assertEqual(self.proof.original, before)

    def test_initial_validation_cannot_bind_changed_source_or_review(self):
        for name in ('en/stage-0/lesson.md', 'reviews/format.json', 'stage-0/brief.json'):
            path = self.root / self.run / name
            old = path.read_bytes()
            self.stop.clear()
            def change(*args, **kwargs):
                path.write_bytes(old + b' ')
                return {'ok': True, 'family_contract': []}
            with patch.object(family, 'require_family_source_reviews', return_value=[]), patch.object(family, 'validate_family', side_effect=change), self.assertRaisesRegex(TranslationError, 'drift'):
                self.proof.establish(10)
            self.assertIsNone(self.proof.proof)
            self.assertTrue(self.stop.is_set())
            path.write_bytes(old)

    def test_byte_changes_in_every_proof_category_reject(self):
        self.proof.establish(10)
        names = [f'{self.run}/family-context.json', f'{self.run}/stage-1/brief.json', f'{self.run}/en/stage-0/lesson.md', f'{self.run}/en/stage-2/quiz.md', f'{self.run}/en/stage-3/adaptation-notes.json', f'{self.run}/reviews/format.json', *VALIDATOR_FILES, 'reference/formats/spec.txt']
        for name in names:
            path = self.root / name
            old = path.read_bytes()
            path.write_bytes(old + b' ')
            try:
                with self.subTest(name=name), self.assertRaises((TranslationError, core.AuthoringError)):
                    self.proof.check(10)
                self.assertTrue(self.stop.is_set())
            finally:
                path.write_bytes(old)
                self.stop.clear()
        with patch.dict(os.environ, {'NODE_BINARY': '/other/node'}), self.assertRaisesRegex(TranslationError, 'drift'):
            self.proof.check(10)

    def test_new_dependency_and_symlink_rejected_by_full_loader(self):
        self.proof.establish(10)
        p = self.root / 'reference/formats/extra.txt'
        p.write_text('unreviewed addition')
        with self.assertRaisesRegex(core.AuthoringError, 'inventory'):
            self.proof.check(10)
        p.unlink()
        self.stop.clear()
        p.symlink_to(self.root / 'reference/formats/spec.txt')
        with self.assertRaisesRegex(core.AuthoringError, 'Symlink'):
            self.proof.check(10)

    def test_identical_source_brief_and_tool_symlinks_are_not_accepted(self):
        self.proof.establish(10)
        for name in (f'{self.run}/en/stage-0/lesson.md', f'{self.run}/en/stage-0/quiz.md', f'{self.run}/en/stage-1/adaptation-notes.json', f'{self.run}/stage-1/brief.json', *VALIDATOR_FILES):
            path = self.root / name
            old = path.read_bytes()
            target = self.root / 'identical-copy'
            target.write_bytes(old)
            path.unlink()
            path.symlink_to(target)
            try:
                with self.subTest(name=name), self.assertRaises((TranslationError, core.AuthoringError)):
                    self.proof.check(10)
            finally:
                path.unlink()
                path.write_bytes(old)
                self.stop.clear()

    def test_first_validation_edit_rejects_and_wire_evidence_stays_identical(self):
        path = self.root / self.run / 'en/stage-0/lesson.md'
        old = path.read_bytes()
        def change(*args, **kwargs):
            path.write_bytes(old + b' changed')
            return []
        with patch.object(family, 'require_family_source_reviews', side_effect=change), self.assertRaisesRegex(TranslationError, 'drift'):
            self.proof.establish(10)
        self.assertIsNone(self.proof.proof)
        path.write_bytes(old)
        self.stop.clear()
        self.proof.establish(10)
        from lesson_factory.translation_payload import build_payload
        from lesson_factory.translation import prepare
        before = {'binding': family.family_binding(self.root, self.run, 'pedagogical'), 'context_digest': core.digest(self.root / self.run / 'family-context.json'), 'reviews': {role: core.digest(family.family_review_path(self.root, self.run, role)) for role in ('pedagogical', 'assessment', 'format')}}
        for locale in core.translation_locales('de'):
            original = prepare(build_payload(self.root, self.run, self.context, locale, before), locale, 'fixture instructions')
            current = prepare(build_payload(self.root, self.run, self.context, locale, self.proof.original), locale, 'fixture instructions')
            self.assertEqual(current, original)

    def test_waiting_for_gate_lock_is_cancellable_and_deadline_bounded(self):
        self.proof.establish(10)
        self.proof.lock.acquire()
        try:
            start = time.monotonic()
            with self.assertRaisesRegex(TranslationError, 'deadline'):
                self.proof.check(.05)
            self.assertLess(time.monotonic() - start, .5)
            self.stop.clear()
            timer = threading.Timer(.03, self.stop.set)
            timer.start()
            try:
                with self.assertRaisesRegex(TranslationError, 'cancelled'):
                    self.proof.check(5)
            finally:
                timer.cancel()
        finally:
            self.proof.lock.release()

    def test_unestablished_proof_rejects(self):
        with self.assertRaisesRegex(TranslationError, 'not been established'):
            self.proof.check(1)


if __name__ == '__main__':
    unittest.main()
