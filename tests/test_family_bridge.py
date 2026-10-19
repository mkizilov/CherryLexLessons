import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from quiz_fixtures import lesson_text, quiz_text


PROJECT = Path(__file__).resolve().parents[1]
OFFSETS = [25, 75, 125, 225]
LOCALES = ['en', 'ar', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh']


class FamilyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / 'reference/formats', self.root / 'reference/formats')
        self.run = 'family_runs/de/000040'
        self.folder = self.root / self.run
        self.folder.mkdir(parents=True)
        self.context = {
            'schema': 5, 'study_language': 'de', 'base_checkpoint': 40,
            'authoring': {'block': 200, 'wave': 1, 'order': 2},
            'source_locale': 'en', 'locales': [code for code in LOCALES if code != 'de'],
            'return_offsets': OFFSETS, 'family_path': 'family_packs/de/000040',
            'vocabulary_horizon': 265, 'words': [{} for _ in range(265)],
            'external_history': [], 'stages': [{
                'stage': stage, 'base_checkpoint': 40,
                'checkpoint': 40 + (OFFSETS[stage - 1] if stage else 0),
                'new_word_range': [40 + (OFFSETS[stage - 2] if stage > 1 else 0) + 1, 40 + OFFSETS[stage - 1]] if stage else [21, 40],
                'prior_external_keys': [], 'planning_external_keys': [],
                'prior_family_stages': [f'stage:{previous}' for previous in range(stage)],
            } for stage in range(5)],
        }
        self.stages = [{
            'stage': stage,
            'lesson_md': lesson_text('family-base', ('base-page',)) if not stage else '',
            'quiz_md': quiz_text(f'bank-{stage}', 'family-base', 'base-page', f'q-{stage}'),
            'adaptation_notes': [],
        } for stage in range(5)]
        for stage in range(5):
            directory = self.folder / f'stage-{stage}'
            directory.mkdir()
            brief = {} if not stage else {'extension': {'mode': 'practice', 'rationale': 'Prior teaching practice'}}
            (directory / 'brief.json').write_text(json.dumps(brief))

    def validate(self, stages=None, locale='en'):
        (self.folder / 'family-context.json').write_text(json.dumps(self.context))
        candidate = self.root / 'candidate.json'
        candidate.write_text(json.dumps({'stages': self.stages if stages is None else stages}))
        result = subprocess.run([
            os.environ.get('NODE_BINARY', 'node'), str(PROJECT / 'tools/validate-family.cjs'),
            '--root', str(self.root), '--run', self.run, '--locale', locale, '--candidate', str(candidate),
        ], capture_output=True, text=True)
        self.assertEqual(result.stderr, '')
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if report['ok'] else 1, report)
        return report

    def assert_invalid(self, stages=None, message=None):
        report = self.validate(stages)
        self.assertFalse(report['ok'], report)
        if message:
            self.assertIn(message, json.dumps(report))

    def test_teaching_length_is_advisory_per_file_not_full_family(self):
        body = '\n\n'.join([' '.join(['word'] * 60)] * 12)
        self.stages[0]['lesson_md'] = lesson_text('family-base', ('base-page',), body=body)
        for stage in range(1, 5):
            (self.folder / f'stage-{stage}' / 'brief.json').write_text(json.dumps({'extension': {'mode': 'extend', 'rationale': 'Length policy fixture'}}))
            self.stages[stage]['lesson_md'] = lesson_text(f'extension-{stage}', (f'page-{stage}',), body=body)
        report = self.validate()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['totals']['pages'], 5)
        self.assertIn('page-length', json.dumps(report))
        self.assertFalse(any(issue.get('code') == 'lesson-length' for stage in report['stages'] for issue in stage['issues']))

    def test_disk_locale_layout_without_candidate(self):
        (self.folder / 'family-context.json').write_text(json.dumps(self.context))
        for stage in self.stages:
            folder = self.folder / 'en' / f"stage-{stage['stage']}"
            folder.mkdir(parents=True)
            (folder / 'lesson.md').write_text(stage['lesson_md'])
            (folder / 'quiz.md').write_text(stage['quiz_md'])
        result = subprocess.run([os.environ.get('NODE_BINARY', 'node'), str(PROJECT / 'tools/validate-family.cjs'),
                                 '--root', str(self.root), '--run', self.run, '--locale', 'en'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['totals']['questions'], 125)

    def test_valid_five_stage_practice_family(self):
        report = self.validate()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['totals'], {'questions': 125, 'pages': 1})
        self.assertEqual(len(report['family_contract']), 5)

    def test_invalid_stage_count_order_and_extra_fields(self):
        for stages in (self.stages[:-1], list(reversed(self.stages)), self.stages + [self.stages[-1]]):
            with self.subTest(stages=len(stages)):
                self.assert_invalid(stages)
        stages = copy.deepcopy(self.stages)
        stages[0]['extra'] = 'forbidden'
        self.assert_invalid(stages)

    def test_page_bounds_and_empty_base(self):
        for pages in ((), tuple(f'p-{number}' for number in range(6))):
            stages = copy.deepcopy(self.stages)
            stages[0]['lesson_md'] = lesson_text('family-base', pages) if pages else ''
            self.assert_invalid(stages)

    def test_practice_teaching_rejected(self):
        stages = copy.deepcopy(self.stages)
        stages[1]['lesson_md'] = lesson_text('extension', ('extension-page',))
        self.assert_invalid(stages, 'Practice')

    def test_bank_question_and_anchor_collisions(self):
        stages = copy.deepcopy(self.stages)
        stages[1]['quiz_md'] = stages[0]['quiz_md']
        self.assert_invalid(stages, 'bank ID')
        stages[1]['quiz_md'] = quiz_text('new-bank', 'family-base', 'base-page', 'q-0')
        self.assert_invalid(stages, 'Question ID')

    def test_own_future_link_rejected(self):
        stages = copy.deepcopy(self.stages)
        stages[0]['lesson_md'] = lesson_text('family-base', ('base-page',), body='[[future-extension#future-page|Later]]')
        self.assert_invalid(stages)

    def test_role_coherence_across_extension(self):
        (self.folder / 'stage-1/brief.json').write_text(json.dumps({'extension': {'mode': 'extend', 'rationale': 'Teach extension'}}))
        stages = copy.deepcopy(self.stages)
        roles = 'roles:\n  custom: blue · solid · Custom role · Explanation\n'
        stages[0]['lesson_md'] = lesson_text('family-base', ('base-page',), roles=roles)
        stages[1]['lesson_md'] = lesson_text('extension', ('new-page',), roles=roles)
        self.assertTrue(self.validate(stages)['ok'])
        stages[1]['lesson_md'] = stages[1]['lesson_md'].replace('custom: blue', 'custom: purple')
        self.assert_invalid(stages, 'Conflicting family role')

    def test_timeline_facts_and_translatable_labels(self):
        body = '```timeline\nnow | Present\npoint -2 | Earlier\nspan -3..-1 | Duration\nref -1 | Reference\n```'
        stages = copy.deepcopy(self.stages)
        stages[0]['lesson_md'] = lesson_text('family-base', ('base-page',), body=body)
        source = self.validate(stages)
        self.assertTrue(source['ok'], source)
        for before, after in (('point -2', 'point 2'), ('span -3..-1', 'span -3..0'), ('ref -1', 'ref 1'), ('point -2', 'span -2')):
            changed = copy.deepcopy(stages)
            changed[0]['lesson_md'] = changed[0]['lesson_md'].replace(before, after)
            result = self.validate(changed)
            self.assertTrue(result['ok'], result)
            self.assertNotEqual(source['family_contract'], result['family_contract'])
        translated = copy.deepcopy(stages)
        for stage in translated:
            stage['lesson_md'] = stage['lesson_md'].replace('explained_in: en', 'explained_in: fr').replace('Present', 'Présent').replace('Earlier', 'Avant').replace('Duration', 'Durée').replace('Reference', 'Repère')
            stage['quiz_md'] = stage['quiz_md'].replace('explained_in: en', 'explained_in: fr')
        result = self.validate(translated, 'fr')
        self.assertTrue(result['ok'], result)
        self.assertEqual(source['family_contract'], result['family_contract'])

    def test_mermaid_wiring_and_translatable_labels(self):
        body = '```mermaid\nflowchart TD\naccTitle: Stages\naccDescr: A simple derivation\nA[Start] --> B[First]\nA --> C[Second]\n```'
        stages = copy.deepcopy(self.stages)
        stages[0]['lesson_md'] = lesson_text('family-base', ('base-page',), body=body)
        source = self.validate(stages)
        self.assertTrue(source['ok'], source)
        changed = copy.deepcopy(stages)
        changed[0]['lesson_md'] = changed[0]['lesson_md'].replace('A --> C', 'B --> C')
        result = self.validate(changed)
        self.assertTrue(result['ok'], result)
        self.assertNotEqual(source['family_contract'], result['family_contract'])
        translated = copy.deepcopy(stages)
        for stage in translated:
            stage['lesson_md'] = stage['lesson_md'].replace('explained_in: en', 'explained_in: fr').replace('Start', 'Début').replace('First', 'Premier').replace('Second', 'Deuxième').replace('Stages', 'Étapes').replace('A simple derivation', 'Une dérivation simple')
            stage['quiz_md'] = stage['quiz_md'].replace('explained_in: en', 'explained_in: fr')
        result = self.validate(translated, 'fr')
        self.assertTrue(result['ok'], result)
        self.assertEqual(source['family_contract'], result['family_contract'])

    def test_morph_order_remains_protected(self):
        stages = copy.deepcopy(self.stages)
        stages[0]['lesson_md'] = lesson_text('family-base', ('base-page',), body='```morph\n`wort` + `-en`\n= `worten` | Surface form\n```')
        source = self.validate(stages)
        self.assertTrue(source['ok'], source)
        changed = copy.deepcopy(stages)
        changed[0]['lesson_md'] = changed[0]['lesson_md'].replace('`wort` + `-en`', '`-en` + `wort`')
        result = self.validate(changed)
        self.assertTrue(result['ok'], result)
        self.assertNotEqual(source['family_contract'], result['family_contract'])

    def test_span_anchor_identity_reference_and_translatable_labels(self):
        body = '[wort1]{.tl #first} [wort2]{.tl #second} [This one]{ref=first}'
        stages = copy.deepcopy(self.stages)
        stages[0]['lesson_md'] = lesson_text('family-base', ('base-page',), body=body)
        source = self.validate(stages)
        self.assertTrue(source['ok'], source)
        for mutation in (body.replace('ref=first', 'ref=second'), body.replace('#first', '#renamed').replace('ref=first', 'ref=renamed')):
            with self.subTest(body=mutation):
                changed = copy.deepcopy(stages)
                changed[0]['lesson_md'] = lesson_text('family-base', ('base-page',), body=mutation)
                report = self.validate(changed)
                self.assertTrue(report['ok'], report)
                self.assertNotEqual(source['family_contract'], report['family_contract'])
        translated = copy.deepcopy(stages)
        for stage in translated:
            stage['lesson_md'] = stage['lesson_md'].replace('explained_in: en', 'explained_in: fr').replace('This one', 'Celui-ci')
            stage['quiz_md'] = stage['quiz_md'].replace('explained_in: en', 'explained_in: fr')
        result = self.validate(translated, 'fr')
        self.assertTrue(result['ok'], result)
        self.assertEqual(source['family_contract'], result['family_contract'])

    def test_target_and_reveal_contract_drift(self):
        source = self.validate()
        self.assertTrue(source['ok'], source)
        stages = copy.deepcopy(self.stages)
        stages[0]['lesson_md'] = stages[0]['lesson_md'].replace('[wort1]{.tl}', '[wort2]{.tl .wrong}')
        changed = self.validate(stages)
        self.assertTrue(changed['ok'], changed)
        self.assertNotEqual(source['family_contract'], changed['family_contract'])

    def add_external_family(self, base=20):
        relative = f'family_packs/de/{base:06d}'
        records = []
        (self.root / relative).mkdir(parents=True)
        manifest = {'schema': 5, 'study_language': 'de', 'base_checkpoint': base,
                    'source_locale': 'en', 'locales': self.context['locales'],
                    'return_offsets': OFFSETS, 'family_path': relative}
        (self.root / relative / 'manifest.json').write_text(json.dumps(manifest))
        for stage in range(5):
            event = {'base_checkpoint': base, 'stage': stage, 'checkpoint': base + (OFFSETS[stage - 1] if stage else 0)}
            brief = {} if not stage else {'extension': {'mode': 'extend', 'rationale': 'New extension'}}
            folder = self.root / relative / f'stage-{stage}'
            folder.mkdir(parents=True)
            (folder / 'brief.json').write_text(json.dumps(brief))
            lesson_id = f'external-{base}-{stage}'
            lesson = lesson_text(lesson_id, (f'external-page-{stage}',))
            quiz = quiz_text(f'external-bank-{stage}', f'external-{base}-0', f'external-page-{stage}', f'external-q-{stage}')
            locale_folder = self.root / relative / 'en' / f'stage-{stage}'
            locale_folder.mkdir(parents=True)
            (locale_folder / 'lesson.md').write_text(lesson)
            (locale_folder / 'quiz.md').write_text(quiz)
            records.append({**event, 'key': f'{relative}#stage:{stage}', 'path': relative, 'source_locale': 'en', 'manifest': brief, 'lesson': lesson, 'quiz': quiz})
        self.context['external_history'] = records
        for event in self.context['stages']:
            event['prior_external_keys'] = [record['key'] for record in records if record['checkpoint'] < event['checkpoint']]
            event['planning_external_keys'] = [record['key'] for record in records if record['checkpoint'] >= event['checkpoint']]

    def test_sparse_external_future_not_known_until_eligible(self):
        self.add_external_family()
        report = self.validate()
        self.assertTrue(report['ok'], report)
        self.assertEqual(len(report['stages'][2]['contract']['prerequisites']), 5)
        stages = copy.deepcopy(self.stages)
        stages[2]['quiz_md'] = stages[2]['quiz_md'].replace('The form is', '[[external-20-4#external-page-4|Future]] The form is')
        self.assert_invalid(stages, 'Unresolved lesson reference')
        stages[2]['quiz_md'] = stages[2]['quiz_md'].replace('external-20-4#external-page-4', 'external-20-2#external-page-2')
        self.assertTrue(self.validate(stages)['ok'])
        stages[4]['quiz_md'] = stages[4]['quiz_md'].replace('The form is', '[[external-20-4#external-page-4|Earlier]] The form is')
        self.assertTrue(self.validate(stages)['ok'])

    def test_single_artifact_external_history_rejected(self):
        relative = 'packs/de/000020'
        manifest = {'schema': 3, 'study_language': 'de', 'base_checkpoint': 20, 'checkpoint': 20,
                    'stage': 0, 'return_offsets': OFFSETS, 'family_path': relative,
                    'new_word_range': [1, 20]}
        folder = self.root / relative
        (folder / 'en').mkdir(parents=True)
        lesson = lesson_text('legacy-base', ('legacy-page',))
        quiz = quiz_text('legacy-bank', 'legacy-base', 'legacy-page', 'legacy-q')
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        (folder / 'en/lesson.md').write_text(lesson)
        (folder / 'en/quiz.md').write_text(quiz)
        self.context['external_history'] = [{**{key: manifest[key] for key in ('base_checkpoint', 'checkpoint', 'stage')},
                                             'path': relative, 'key': relative, 'source_locale': 'en',
                                             'manifest': manifest, 'lesson': lesson, 'quiz': quiz}]
        for event in self.context['stages']:
            event['prior_external_keys'] = [relative]
        stages = copy.deepcopy(self.stages)
        stages[0]['lesson_md'] += '\n[[legacy-base#legacy-page|Prior]]\n'
        self.assertFalse(self.validate(stages)['ok'])
        self.assertFalse(self.validate(stages, 'fr')['ok'])

    def test_external_paths_source_and_partition_checked(self):
        self.add_external_family()
        self.context['stages'][2]['prior_external_keys'].append(self.context['external_history'][-1]['key'])
        self.assert_invalid(message='partition')
        self.context['stages'][2]['prior_external_keys'].pop()
        self.context['external_history'][0]['lesson'] += 'edited'
        self.assert_invalid(message='source history')

    def test_infill_own_prior_extension_and_anchor_collision(self):
        brief = self.folder / 'stage-1/brief.json'
        brief.write_text(json.dumps({'extension': {'mode': 'extend', 'rationale': 'Teach new form'}}))
        stages = copy.deepcopy(self.stages)
        stages[1]['lesson_md'] = lesson_text('extension-1', ('extension-page',), body='[[family-base#base-page|Prior]]')
        stages[2]['quiz_md'] = stages[2]['quiz_md'].replace('The form is', '[[extension-1#extension-page|Prior]] The form is')
        report = self.validate(stages)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['totals']['pages'], 2)
        stages[1]['lesson_md'] = lesson_text('extension-1', ('base-page',))
        self.assert_invalid(stages, 'Duplicate family page anchor')
        stages[1]['lesson_md'] = lesson_text('family-base', ('extension-page',))
        self.assert_invalid(stages, 'reserved lesson')

    def test_translation_prose_and_protected_policy_contract(self):
        source = self.validate()
        translated = copy.deepcopy(self.stages)
        for stage in translated:
            stage['lesson_md'] = stage['lesson_md'].replace('explained_in: en', 'explained_in: fr').replace('Test lesson', 'Leçon')
            stage['quiz_md'] = stage['quiz_md'].replace('explained_in: en', 'explained_in: fr').replace('The form is', 'La forme est').replace('Choose the form.', 'Choisissez la forme.')
        result = self.validate(translated, 'fr')
        self.assertTrue(result['ok'], result)
        self.assertEqual(source['family_contract'], result['family_contract'])

    def test_concept_option_translation_and_policy_changes(self):
        stages = copy.deepcopy(self.stages)
        stages[0]['quiz_md'] = quiz_text('bank-0', 'family-base', 'base-page', 'q-0', options=4).replace('{{blank}}', '[wort1]{.tl}')
        policies = {f'q-0-{index}': 'learner' for index in range(25)}
        (self.folder / 'stage-0/brief.json').write_text(json.dumps({'option_text_policies': policies}))
        source = self.validate(stages)
        self.assertTrue(source['ok'], source)
        translated = copy.deepcopy(stages)
        for stage in translated:
            stage['lesson_md'] = stage['lesson_md'].replace('explained_in: en', 'explained_in: fr')
            stage['quiz_md'] = stage['quiz_md'].replace('explained_in: en', 'explained_in: fr')
        translated[0]['quiz_md'] = translated[0]['quiz_md'].replace('"text": "wort', '"text": "concept')
        result = self.validate(translated, 'fr')
        self.assertTrue(result['ok'], result)
        self.assertEqual(source['family_contract'], result['family_contract'])
        (self.folder / 'stage-0/brief.json').write_text('{}')
        changed = self.validate(stages)
        self.assertTrue(changed['ok'], changed)
        self.assertNotEqual(source['family_contract'], changed['family_contract'])

    def test_extension_page_limit_and_bank_bounds(self):
        (self.folder / 'stage-1/brief.json').write_text(json.dumps({'extension': {'mode': 'extend', 'rationale': 'Teach extension'}}))
        for pages in ((), ('one', 'two', 'three')):
            stages = copy.deepcopy(self.stages)
            stages[1]['lesson_md'] = lesson_text('extension', pages) if pages else ''
            self.assert_invalid(stages)
        for count in (24, 51):
            stages = copy.deepcopy(self.stages)
            stages[1]['lesson_md'] = lesson_text('extension', ('one',))
            stages[1]['quiz_md'] = quiz_text('bank-1', 'family-base', 'one', 'q-1', count=count)
            self.assert_invalid(stages, '25..')

    def test_wave_two_authoring_matches_python_family_order(self):
        from lesson_factory.family import family_order
        for base in (10, 30, 190, 210):
            with self.subTest(base=base):
                self.run = f'family_runs/de/{base:06d}'
                self.folder = self.root / self.run
                self.folder.mkdir(parents=True)
                self.context.update(base_checkpoint=base, family_path=f'family_packs/de/{base:06d}',
                                    vocabulary_horizon=base + 225, words=[{} for _ in range(base + 225)],
                                    authoring=next(row['authoring'] for row in family_order(max(200, base)) if row['base_checkpoint'] == base))
                for stage, event in enumerate(self.context['stages']):
                    event.update(base_checkpoint=base, checkpoint=base + (OFFSETS[stage - 1] if stage else 0),
                                 new_word_range=[base + (OFFSETS[stage - 2] if stage > 1 else 0) + 1, base + OFFSETS[stage - 1]] if stage else [base - 9, base])
                    folder = self.folder / f'stage-{stage}'
                    folder.mkdir()
                    brief = {} if not stage else {'extension': {'mode': 'practice', 'rationale': 'Practice'}}
                    (folder / 'brief.json').write_text(json.dumps(brief))
                report = self.validate()
                self.assertTrue(report['ok'], report)

    def test_canonical_identity_word_bounds_and_notes(self):
        self.context['stages'][2]['new_word_range'][0] -= 1
        self.assert_invalid(message='word bounds')
        self.context['stages'][2]['new_word_range'][0] += 1
        self.context['source_locale'] = 'es'
        self.assert_invalid(message='schema-5')
        self.context['source_locale'] = 'en'
        stages = copy.deepcopy(self.stages)
        stages[0]['adaptation_notes'] = 'wrong type'
        self.assert_invalid(stages, 'complete stages')

    def test_option_policy_strict_and_protected_target_feedback(self):
        stages = copy.deepcopy(self.stages)
        stages[0]['quiz_md'] = quiz_text('bank-0', 'family-base', 'base-page', 'q-0', options=4)
        brief = self.folder / 'stage-0/brief.json'
        for policy in (None, [], {'missing': 'learner'}, {'q-0-0': 'invalid'}, {'q-0-0': 'learner'}):
            brief.write_text(json.dumps({'option_text_policies': policy}))
            self.assertFalse(self.validate(stages)['ok'])
        brief.write_text('{}')
        source = self.validate(stages)
        self.assertTrue(source['ok'], source)
        for needle, replacement in (('{{blank}}', '[wort2]{.tl} {{blank}}'), ('[wort1]{.tl}.', '[wort2]{.tl}.'), ('"text": "wort1"', '"text": "wort2-new"')):
            changed = copy.deepcopy(stages)
            changed[0]['quiz_md'] = changed[0]['quiz_md'].replace(needle, replacement)
            result = self.validate(changed)
            self.assertTrue(result['ok'], result)
            self.assertNotEqual(source['family_contract'], result['family_contract'])
        changed = copy.deepcopy(stages)
        changed[0]['quiz_md'] = changed[0]['quiz_md'].replace('The form is', '[[unknown-lesson#unknown-page|Missing]] The form is')
        self.assertFalse(self.validate(changed)['ok'])
        translated = copy.deepcopy(stages)
        for stage in translated:
            stage['lesson_md'] = stage['lesson_md'].replace('explained_in: en', 'explained_in: fr')
            stage['quiz_md'] = stage['quiz_md'].replace('explained_in: en', 'explained_in: fr').replace('The form is', 'La forme est').replace('Choose the form.', 'Une question.\n\nUne autre phrase.')
        self.assertEqual(source['family_contract'], self.validate(translated, 'fr')['family_contract'])

    def test_current_only_history_manifest_and_quiz_versions(self):
        self.add_external_family()
        manifest = self.root / 'family_packs/de/000020/manifest.json'
        current = json.loads(manifest.read_text())
        for schema in (1, 2, 3, 4):
            changed = {**current, 'schema': schema}
            manifest.write_text(json.dumps(changed))
            self.assertFalse(self.validate()['ok'])
        manifest.write_text(json.dumps(current))
        stages = copy.deepcopy(self.stages)
        stages[0]['quiz_md'] = stages[0]['quiz_md'].replace('qmd: 0.2', 'qmd: 0.1')
        self.assertFalse(self.validate(stages)['ok'])

    def test_choice_pool_limits(self):
        for count in (3, 11):
            stages = copy.deepcopy(self.stages)
            stages[0]['quiz_md'] = quiz_text('bank-0', 'family-base', 'base-page', 'q-0', options=count)
            self.assert_invalid(stages, 'options')
