import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = next(p for p in Path(__file__).resolve().parents if (p / 'lesson_factory/family.py').exists())
sys.path[:0] = [str(PROJECT), str(PROJECT / 'tests')]
from lesson_factory import core, family
from quiz_fixtures import lesson_text, quiz_text

MODULE = Path(__file__).resolve().parents[1] / 'lesson_factory/correction_edition.py'
if not MODULE.exists():
    MODULE = PROJECT / 'lesson_factory/correction_edition.py'
loader = importlib.util.spec_from_file_location('lesson_factory.correction_edition', MODULE)
edition = importlib.util.module_from_spec(loader)
loader.loader.exec_module(edition)


class CorrectionEditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in edition.TOOLS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / name, path)
        vocabulary = self.root / 'reference/vocabulary/German/german_vocabulary.csv'
        vocabulary.parent.mkdir(parents=True)
        with vocabulary.open('w') as handle:
            writer = csv.writer(handle)
            writer.writerow(['Rank', 'Word'])
            writer.writerows((n, f'wort{n}') for n in range(1, 246))
        (self.root / 'reference/grammar').mkdir()
        (self.root / 'reference/grammar/grammar.sqlite').write_bytes(b'synthetic, not a database')
        run = family.prepare_family(self.root, 'de', 20)
        for stage in range(5):
            brief = dict(topic='Synthetic', objective='Test only', sources=[dict(reference='synthetic:test', justification='fixture')], prerequisites=[], lexical_audit=[dict(target_form='wort1', known_rank=1, justification='fixture')])
            if stage:
                brief['extension'] = dict(mode='practice', rationale='fixture')
            core.write_json(self.root / run / f'stage-{stage}/brief.json', brief)
            for locale in core.learner_locales('de'):
                folder = self.root / run / locale / f'stage-{stage}'
                (folder / 'lesson.md').write_text('' if stage else lesson_text(locale=locale))
                (folder / 'quiz.md').write_text(quiz_text(f'bank-{stage}', prefix=f'q-{stage}', locale=locale))
                core.write_json(folder / 'adaptation-notes.json', [])
        checks = lambda stage: [dict(id=f'q-{stage}-{n}', status='approved', rationale='synthetic', answer='wort1', alternatives='synthetic', teaching_page='base-page') for n in range(25)]
        def validator(*args, **kwargs):
            return dict(ok=True, family_contract=[], stages=[dict(stage=s, contract=dict(questions=checks(s))) for s in range(5)])
        with patch.object(family, 'validate_family', validator):
            for role in core.review_roles('de') + ['final']:
                report = dict(status='approved', reviewer_session=f'synthetic:{role}', checklist=['synthetic only'], findings=['NOT real approval'], stages=[dict(stage=s, checklist=['synthetic'], findings=['synthetic'], question_checks=checks(s)) for s in range(5)], binding=family.family_binding(self.root, run, role))
                family.submit_family_review(self.root, run, role, report)
            self.relative = family.finalize_family(self.root, run)
        manifest = core.read_json(self.root / self.relative / 'manifest.json')
        self.member = 'fr/stage-0/lesson.md'
        candidate = self.root / 'candidate.md'
        candidate.write_text((self.root / self.relative / self.member).read_text().replace('Test lesson', 'Synthetic title correction'))
        self.evidence = self.reference('evidence.md', b'SYNTHETIC historical HOLD evidence; never infer approval')
        self.spec = dict(schema=edition.SCHEMA, edition_id='synthetic-1', families={self.relative: dict(manifest_raw_sha256=core.digest(self.root / self.relative / 'manifest.json'), manifest_canonical_sha256=family._hash(manifest), context_hash=manifest['context_hash'])}, replacements=[dict(family=self.relative, file=self.member, candidate=dict(path='candidate.md', sha256=core.digest(candidate)), reason='synthetic correction', author_session='synthetic:author', propagation='fr only; no target change', anchors=['stage:0/base-page'], evidence=[self.evidence])], reviews=[])

    def reference(self, name, data):
        path = self.root / name
        path.write_bytes(core.canonical(data) + b'\n' if isinstance(data, dict) else data)
        return dict(path=name, sha256=core.digest(path))

    def spec_ref(self):
        return self.reference('spec.json', self.spec)

    def approve(self):
        inventory = edition.preview(self.root, self.spec_ref(), draft=True, validate=False)['inventory_sha256']
        scope = [f'{self.relative}/{self.member}']
        report = dict(report_id='synthetic-locale', role='language:fr', reviewer_session='synthetic:independent', verdict='approved', inventions='none', inventory_sha256=inventory, scope=scope, evidence=[self.evidence])
        ref = dict(self.reference('review.json', report), report_id=report['report_id'])
        self.spec['reviews'] = [ref]
        report.update(report_id='synthetic-integration', role='integration', reviewer_session='synthetic:integrator', reviewed_reports=[ref])
        self.spec['integration'] = dict(self.reference('integration.json', report), report_id=report['report_id'])

    def test_complete_reviewed_copy_receipt_and_exact_brief_map(self):
        self.approve()
        receipt = edition.build(self.root, self.spec_ref())
        self.assertIs(receipt['production_approved'], False)
        self.assertEqual(receipt['review_status'], 'scoped-reviewed')
        root = edition.resolver(self.root, 'synthetic-1', self.relative)
        self.assertEqual(len(list(root.rglob('*.*'))), 165)
        for path in root.rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o222, 0)
        self.assertFalse((root / 'manifest.json').exists())
        mapping = edition.resolution_map(self.root, 'synthetic-1', self.relative)
        self.assertEqual(len(mapping), 170)
        self.assertEqual(mapping[f'{self.relative}/stage-0/brief.json'], self.root / self.relative / 'stage-0/brief.json')
        self.assertTrue(all(p['original_sha256'] == p['effective_sha256'] for key, p in receipt['inventory']['files'].items() if key != f'{self.relative}/{self.member}'))
        with self.assertRaises(core.AuthoringError):
            edition.resolver(self.root, 'synthetic-1', 'family_packs/de/000040')

    def test_draft_is_explicit_no_inferred_prose_approval_and_no_overwrite(self):
        with self.assertRaisesRegex(core.AuthoringError, 'Reviewed corrections'):
            edition.build(self.root, self.spec_ref())
        receipt = edition.build(self.root, self.spec_ref(), draft=True)
        with self.assertRaises(OSError):
            edition.build(self.root, self.spec_ref(), draft=True)
        self.assertEqual(edition.verify(self.root, 'synthetic-1'), receipt)

    def test_contract_change_duplicate_source_and_path_rejected(self):
        self.spec['replacements'].append(self.spec['replacements'][0])
        with self.assertRaisesRegex(core.AuthoringError, 'Duplicate'):
            edition.preview(self.root, self.spec_ref(), draft=True)
        self.spec['replacements'].pop()
        for name in ('en/stage-0/lesson.md', '../lesson.md'):
            self.spec['replacements'][0]['file'] = name
            with self.assertRaises(core.AuthoringError):
                edition.preview(self.root, self.spec_ref(), draft=True)
        self.spec['replacements'][0]['file'] = 'fr/stage-0/quiz.md'
        self.spec['replacements'][0]['candidate'] = self.reference('answer.md', (self.root / self.relative / 'fr/stage-0/quiz.md').read_bytes().replace(b'"wort1"', b'"wort2"'))
        with self.assertRaisesRegex(core.AuthoringError, 'Protected contract'):
            edition.build(self.root, self.spec_ref(), draft=True)
        self.assertFalse((self.root / 'work/correction-editions').exists())

    def test_current_review_scope_sid_verdict_and_evidence_checked(self):
        self.approve()
        original = core.read_json(self.root / 'review.json')
        for field, value in [('reviewer_session', 'synthetic:author'), ('scope', []), ('verdict', 'GO'), ('inventory_sha256', '0' * 64), ('evidence', [dict(path='missing', sha256='0' * 64)])]:
            with self.subTest(field=field):
                changed = dict(original, **{field: value})
                self.spec['reviews'][0] = dict(self.reference('review.json', changed), report_id=original['report_id'])
                with self.assertRaises((core.AuthoringError, OSError)):
                    edition.preview(self.root, self.spec_ref())

    def test_resolver_rejects_each_drift_symlink_and_unlisted_input(self):
        self.approve()
        edition.build(self.root, self.spec_ref())
        folder = self.root / 'work/correction-editions/synthetic-1'
        targets = [folder / 'receipt.json', folder / 'effective' / self.relative / self.member, self.root / 'spec.json', self.root / 'review.json', self.root / 'integration.json', self.root / 'candidate.md', self.root / 'evidence.md', self.root / self.relative / 'stage-0/brief.json', self.root / self.relative / 'manifest.json', self.root / 'tools/content-contract.cjs']
        for target in targets:
            with self.subTest(path=target):
                data = target.read_bytes()
                target.chmod(0o644)
                target.write_bytes(data + b' ')
                with self.assertRaises((core.AuthoringError, OSError, ValueError)):
                    edition.verify(self.root, 'synthetic-1')
                target.write_bytes(data)
        folder.chmod(0o755)
        (folder / 'unlisted').mkdir()
        with self.assertRaisesRegex(core.AuthoringError, 'unlisted'):
            edition.verify(self.root, 'synthetic-1')
        (folder / 'unlisted').rmdir()
        (folder / 'extra.txt').write_text('unlisted')
        with self.assertRaises(core.AuthoringError):
            edition.verify(self.root, 'synthetic-1')
        (folder / 'extra.txt').unlink()
        missing = folder / 'effective' / self.relative / 'fr/stage-1/quiz.md'
        missing.parent.chmod(0o755)
        missing.rename(missing.with_suffix('.hidden'))
        with self.assertRaises(core.AuthoringError):
            edition.verify(self.root, 'synthetic-1')
        missing.with_suffix('.hidden').rename(missing)
        target = self.root / 'candidate.md'
        data = target.read_bytes()
        target.unlink()
        target.symlink_to(self.root / 'evidence.md')
        with self.assertRaises(core.AuthoringError):
            edition.verify(self.root, 'synthetic-1')
        target.unlink()
        target.write_bytes(data)
        effective = folder / 'effective' / self.relative / self.member
        effective.parent.chmod(0o755)
        effective.unlink()
        effective.symlink_to(target)
        with self.assertRaises(core.AuthoringError):
            edition.verify(self.root, 'synthetic-1')

    def test_receipt_requires_literal_false_even_when_resigned(self):
        receipt = edition.build(self.root, self.spec_ref(), draft=True)
        path = self.root / 'work/correction-editions/synthetic-1/receipt.json'
        path.chmod(0o644)
        for value in (0, None, True, '', [], {}):
            with self.subTest(value=value):
                changed = dict(receipt, production_approved=value)
                changed.pop('receipt_sha256')
                changed['receipt_sha256'] = edition._hash(changed)
                path.write_bytes(core.canonical(changed) + b'\n')
                with self.assertRaisesRegex(core.AuthoringError, 'literal false'):
                    edition.verify(self.root, 'synthetic-1')

    def test_validation_proof_has_exact_types_scope_count_and_keys(self):
        receipt = edition.build(self.root, self.spec_ref(), draft=True)
        path = self.root / 'work/correction-editions/synthetic-1/receipt.json'
        path.chmod(0o644)
        proof = receipt['validation'][self.relative]
        malformed = [None, [], True, {}, dict(proof, extra=True), {k: v for k, v in proof.items() if k != 'scope'}]
        malformed += [dict(proof, **{key: value}) for key, values in [('ok', [1, False, None]), ('stages', [55.0, True, False, 54, None, '55']), ('scope', [None, False, 'PRODUCTION APPROVAL'])] for value in values]
        for value in malformed:
            with self.subTest(proof=value):
                changed = dict(receipt, validation={self.relative: value})
                changed.pop('receipt_sha256')
                changed['receipt_sha256'] = edition._hash(changed)
                path.write_bytes(core.canonical(changed) + b'\n')
                with self.assertRaisesRegex(core.AuthoringError, 'bounded validation proof'):
                    edition.verify(self.root, 'synthetic-1')
        # The producer rejects the same malformed tool result, not just verify.
        _, payload, originals = edition._plan(self.root, self.spec)
        with patch.object(edition.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, core.canonical(dict(proof, stages=False)), b'')):
            with self.assertRaisesRegex(core.AuthoringError, 'bounded validation proof'):
                edition._validate(self.root, self.relative, originals[self.relative], payload[self.relative])
        for value in (None, [], {}, dict(receipt['validation'], extra=proof)):
            changed = dict(receipt, validation=value)
            changed.pop('receipt_sha256')
            changed['receipt_sha256'] = edition._hash(changed)
            path.write_bytes(core.canonical(changed) + b'\n')
            with self.assertRaisesRegex(core.AuthoringError, 'Missing validation'):
                edition.verify(self.root, 'synthetic-1')

    def test_original_preflight_rejects_special_files_without_hanging(self):
        ref = self.spec_ref()
        script = '''
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location('lesson_factory.correction_edition', sys.argv[2])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    m.preview(sys.argv[3], json.loads(sys.argv[4]), draft=True, validate=False)
except (ValueError, OSError) as exc:
    print(type(exc).__name__, str(exc))
else:
    raise AssertionError('unsafe original accepted')
'''
        manifest = self.root / self.relative / 'manifest.json'
        cases = [(manifest, kind) for kind in ('fifo', 'symlink', 'oversized')]
        cases += [(self.root / self.relative / name, 'fifo') for name in ('family-context.json', self.member, 'stage-0/brief.json', 'shared-findings.json', 'reviews/language-fr.json')]
        cases += [(self.root / name, 'fifo') for name in ('reference/grammar/grammar.sqlite', 'reference/formats/kit/gmd.js', 'family-registry.sqlite')]
        for target, kind in cases:
            with self.subTest(path=target, kind=kind):
                data = target.read_bytes()
                target.unlink()
                try:
                    if kind == 'fifo':
                        os.mkfifo(target)
                    elif kind == 'symlink':
                        target.symlink_to(self.root / 'evidence.md')
                    else:
                        with target.open('wb') as handle:
                            handle.truncate(64 * 1024 * 1024 + 1)
                    process = subprocess.run([sys.executable, '-B', '-c', script, str(PROJECT), str(MODULE), str(self.root), json.dumps(ref)], capture_output=True, text=True, timeout=3)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertRegex(process.stdout, 'Invalid/oversized input|Symlink')
                    self.assertFalse((self.root / 'work/correction-editions').exists())
                finally:
                    target.unlink()
                    target.write_bytes(data)

    def test_atomic_publish_never_replaces_even_empty_destination(self):
        source, destination = self.root / 'source', self.root / 'destination'
        source.mkdir()
        destination.mkdir()
        with self.assertRaises(OSError):
            edition._publish(source, destination)
        self.assertTrue(source.is_dir())
        destination.rmdir()
        edition._publish(source, destination)
        self.assertTrue(destination.is_dir())
        self.assertFalse(source.exists())


if __name__ == '__main__':
    unittest.main()
