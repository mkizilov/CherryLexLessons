"""Offline staged reviewer-aid tests; copy existing parsers into an isolated overlay.

From the project root:
  python3 -B -m unittest discover -s work/audits/output-quality-20261017/staged-audit-tools/tests -v
No live tools, packs, workflow, or frozen files are changed.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


STAGED = Path(__file__).resolve().parents[1]
PROJECT = STAGED.parents[3] if STAGED.parent.name == 'output-quality-20261017' else STAGED
NODE = os.environ.get('NODE_BINARY', 'node')
INPUT = 'family_packs/de/000020/en/stage-0/quiz.md'


def choice(number, count=4, **changes):
    question = {
        'id': f'source-question-{number}',
        'config': {'type': 'choice', 'page': 'semantic-teaching-page', 'skill': 'diagnostic-skill',
                   'options': [{'id': f'correct-semantic-{number}' if index == 0 else f'error-semantic-{number}-{index}',
                                'text': f'form-{number}-{index}', 'correct': index == 0,
                                'why': f'PRIVATE-OPTION-RATIONALE-{number}-{index}'} for index in range(count)]},
        'prompt': f'Choose the form for context {number}.',
        'sentence': None,
        'explanation': f'PRIVATE-EXPLANATION-{number}: the form is [form-{number}-0]{{.tl}}.'}
    question.update(changes)
    return question


def blank(number, **changes):
    question = {'id': f'source-question-{number}',
                'config': {'type': 'blank', 'page': 'semantic-teaching-page', 'skill': 'diagnostic-skill',
                           'answers': ['PRIVATE-TYPED-FORM', 'PRIVATE-TYPED-ALTERNATIVE'], 'case': 'fold'},
                'prompt': f'Complete the sentence in context {number}.',
                'sentence': 'Ich {{blank}} hier.',
                'explanation': 'PRIVATE-EXPLANATION: use [PRIVATE-TYPED-FORM]{.tl}.'}
    question.update(changes)
    return question


def bank_text(questions=None, identity='source-bank'):
    questions = questions if questions is not None else [choice(index) for index in range(25)]
    text = f'---\nqmd: 0.2\nid: {identity}\nlesson: absent-linked-lesson\ntitle: Synthetic bank\nlang: de\nexplained_in: en\n---\n'
    for question in questions:
        text += f'\n# {question["id"]}\n```quiz\n{json.dumps(question["config"], ensure_ascii=False)}\n```\n## Prompt\n{question["prompt"]}\n'
        if question['sentence'] is not None:
            text += f'## Sentence\n{question["sentence"]}\n'
        text += f'## Explanation\n{question["explanation"]}\n'
    return text


def leaf_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from leaf_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from leaf_strings(child)


def field_names(value):
    if isinstance(value, dict):
        yield from value.keys()
        for child in value.values():
            yield from field_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from field_names(child)


class QuizAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quiz-audit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in ('reference/formats/kit/gmd.js', 'reference/formats/kit/qmd.js',
                         'reference/formats/grammar/conventions.json', 'tools/quiz-policy.cjs'):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / relative, target)
        target = self.root / 'tools/quiz-audit.cjs'
        shutil.copyfile(STAGED / 'tools/quiz-audit.cjs', target)
        self.tool = target

    def write_bank(self, text=None, relative=INPUT):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bank_text() if text is None else text, encoding='utf-8')
        return target

    def invoke(self, *arguments, success=True):
        result = subprocess.run([NODE, str(self.tool), *arguments], cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def audit(self, *arguments, success=True):
        return self.invoke('--input', INPUT, *arguments, success=success)

    def canonical(self, text, linked=False):
        script = r'''
const fs = require('node:fs');
const G = require('./reference/formats/kit/gmd.js');
const Q = require('./tools/quiz-policy.cjs');
G.useConventions(JSON.parse(fs.readFileSync('./reference/formats/grammar/conventions.json', 'utf8')));
const p = Q.parse(fs.readFileSync(0, 'utf8'), process.argv[1] === 'linked' ? {lessons: []} : undefined);
console.log(JSON.stringify(p.ok ? {ok:true, bank:Q.toJSON(p)} : {ok:false, issues:p.issues}));
'''
        result = subprocess.run([NODE, '-e', script, 'linked' if linked else 'unlinked'], cwd=self.root,
                                input=text, capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def test_canonical_parse_scope_conventions_hash_counts_and_no_writes(self):
        questions = [choice(index) for index in range(25)]
        questions[0]['prompt'] = 'Choose [ich]{.nom} with [bin]{.tl}.'
        questions[1] = choice(1, 10)
        questions[2] = blank(2)
        text = bank_text(questions)
        target = self.write_bank(text)
        # An invalid neighbouring lesson must not be consulted by this aid.
        target.with_name('lesson.md').write_text('This is deliberately not GMD.')
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        parsed = self.canonical(text)
        self.assertTrue(parsed['ok'])
        self.assertFalse(self.canonical(text, linked=True)['ok'])
        report = self.audit()
        self.assertTrue(report['ok'])
        self.assertEqual(report['mode'], 'summary')
        self.assertIn('WITHOUT a linked lesson', report['scope'])
        self.assertIn('Not full family', report['scope'])
        result = report['banks'][0]
        self.assertEqual(result['counts'], {'questions': 25, 'choice': 24, 'blank': 1})
        self.assertEqual(result['pool_distribution'], {'4': 23, '10': 1})
        self.assertEqual(result['question_ids'], [question['id'] for question in questions])
        self.assertEqual(result['sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(len(parsed['bank']['questions']), result['counts']['questions'])
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_repetition_is_flag_only_ignores_ids_order_feedback_but_not_key(self):
        questions = [choice(index) for index in range(25)]
        original = questions[0]
        repeated = copy.deepcopy(original)
        repeated['id'] = 'legitimate-reinforcement'
        repeated['config']['page'] = 'other-anchor'
        repeated['config']['skill'] = 'other-skill'
        repeated['config']['options'].reverse()
        for index, option in enumerate(repeated['config']['options']):
            option['id'] = f'new-id-{index}'
            option['why'] = 'Different feedback.'
        repeated['explanation'] = 'Different explanation of [form-0-0]{.tl}.'
        questions[1] = repeated
        alternate = copy.deepcopy(original)
        alternate['id'] = 'different-key'
        alternate['config']['options'][0]['correct'] = False
        alternate['config']['options'][1]['correct'] = True
        questions[2] = alternate
        self.write_bank(bank_text(questions))
        report = self.audit()
        self.assertTrue(report['ok'])
        self.assertIn('purposeful reinforcement', report['interpretation'])
        self.assertIn('never rejection', report['interpretation'])
        self.assertFalse({'score', 'quality_score', 'threshold', 'reject'} & set(field_names(report)))
        for key in ('exact_visible_task_groups', 'option_keyset_groups'):
            groups = report['advisory_flags'][key]
            self.assertEqual(len(groups), 1)
            self.assertEqual([row['question_id'] for row in groups[0]['occurrences']],
                             [original['id'], repeated['id']])
            self.assertTrue(all(row['input'] == INPUT and len(row['sha256']) == 64 for row in groups[0]['occurrences']))

    def test_blank_frame_and_case_accepted_form_set_groups_ignore_prompt_only(self):
        questions = [choice(index) for index in range(25)]
        first = blank(0)
        second = blank(1, prompt='A different actor and communicative context.')
        second['config']['answers'].reverse()
        third = blank(2)
        third['config']['case'] = 'exact'
        questions[:3] = [first, second, third]
        self.write_bank(bank_text(questions))
        flags = self.audit()['advisory_flags']
        self.assertEqual(flags['exact_visible_task_groups'], [])
        self.assertEqual(len(flags['blank_frame_accepted_form_groups']), 1)
        group = flags['blank_frame_accepted_form_groups'][0]
        self.assertEqual(group['content']['response']['case'], 'fold')
        self.assertEqual(group['content']['response']['accepted_forms'], sorted(first['config']['answers']))
        self.assertEqual([row['question_id'] for row in group['occurrences']], [first['id'], second['id']])

    def test_cross_file_groups_have_unambiguous_id_and_hash_locators(self):
        text = bank_text()
        self.write_bank(text)
        other = 'family_runs/de/000040/en/stage-1/quiz.md'
        changed = text.replace('id: source-bank\n', 'id: other-bank\n')
        self.write_bank(changed, other)
        report = self.audit('--input', other)
        self.assertEqual(len(report['banks']), 2)
        groups = report['advisory_flags']['exact_visible_task_groups']
        self.assertEqual(len(groups), 25)
        self.assertEqual(groups[0]['occurrences'], [
            {'input': INPUT, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'question_id': 'source-question-0'},
            {'input': other, 'sha256': hashlib.sha256(changed.encode()).hexdigest(), 'question_id': 'source-question-0'}])

    def test_correct_longest_counts_code_points_and_ties_without_quality_claim(self):
        questions = [choice(index) for index in range(25)]
        for index, values in enumerate((['necessary qualification', 'a', 'b', 'c'],
                                       ['aa', 'bb', 'c', 'd'], ['a', 'longer', 'c', 'd'],
                                       ['😀', 'ab', 'c', 'd'])):
            for option, value in zip(questions[index]['config']['options'], values):
                option['text'] = value
        self.write_bank(bank_text(questions))
        stats = self.audit()['banks'][0]['correct_longest']
        self.assertEqual([row['result'] for row in stats['observations'][:4]],
                         ['uniquely_longest', 'tied_longest', 'not_longest', 'not_longest'])
        self.assertEqual(stats['observations'][3]['key_code_points'], 1)
        self.assertEqual(stats['choice_questions'], stats['uniquely_longest'] + stats['tied_longest'] + stats['not_longest'])
        self.assertEqual(len(stats['observations'][0]['sha256']), 64)

    def test_blind_full_pools_blank_content_and_diagnostic_fields_withheld(self):
        questions = [choice(index) for index in range(25)]
        questions[0]['prompt'] = 'Choose **carefully**: [ich]{.nom} and {行く|いく}.'
        questions[0]['sentence'] = 'Ich {{blank}} hier.'
        questions[1] = choice(1, 10)
        questions[2] = blank(2)
        self.write_bank(bank_text(questions))
        report = self.audit('--mode', 'blind')
        self.assertTrue(report['ok'])
        encoded = json.dumps(report)
        forbidden = {'correct', 'why', 'answer', 'answers', 'accepted_forms', 'explanation', 'skill', 'page',
                     'id', 'question_id', 'bank_id', 'input', 'role', 'ref', 'clip'}
        self.assertFalse(forbidden & set(field_names(report)))
        for marker in ('source-question', 'source-bank', 'semantic-teaching-page', 'diagnostic-skill',
                       'correct-semantic', 'error-semantic', 'PRIVATE-EXPLANATION', 'PRIVATE-OPTION-RATIONALE',
                       'PRIVATE-TYPED', 'absent-linked-lesson'):
            self.assertNotIn(marker, encoded)
        self.assertEqual([question['token'] for question in report['questions']], [f'Q{index + 1}' for index in range(25)])
        for index in (0, 1):
            result = report['questions'][index]
            self.assertCountEqual([option['text'] for option in result['options']],
                                  [option['text'] for option in questions[index]['config']['options']])
            self.assertEqual([option['label'] for option in result['options']], list(range(1, len(result['options']) + 1)))
        self.assertNotIn('options', report['questions'][2])
        self.assertEqual(report['questions'][2]['sentence'], [{'t': 'text', 'v': 'Ich '}, {'t': 'blank'}, {'t': 'text', 'v': ' hier.'}])
        visible = list(leaf_strings(report['questions'][0]['prompt']))
        for content in ('carefully', 'ich', '行く', 'いく'):
            self.assertIn(content, visible)

    def test_blind_rich_structures_preserve_content_strip_generation_and_graph_ids(self):
        questions = [choice(index) for index in range(25)]
        prompts = [
            '| Form | Reading |\n| --- | --- |\n| [ich]{.nom} | I |',
            '- First visible context\n- Second visible context',
            '> [!rule] A visible rule\n> Use [ich]{.tl} here.',
            '```pattern\nich + bin\n```',
            '```mermaid\nflowchart LR\naccTitle: Cue sequence\naccDescr: First cue leads to second cue.\ncorrectsemantic[First cue] --> wrongsemantic[Second cue]\n```',
            '```image\nalt: A visible shape\nprompt: PRIVATE-GENERATION-PROMPT\nratio: 3:2\ncaption: Visible image caption.\n```']
        for question, prompt in zip(questions, prompts):
            question['prompt'] = prompt
        self.write_bank(bank_text(questions))
        report = self.audit('--mode', 'blind')
        shown = report['questions']
        self.assertEqual(shown[0]['prompt'][0]['rows'][0][0]['text'][0]['kids'][0]['v'], 'ich')
        self.assertIn('Second visible context', list(leaf_strings(shown[1]['prompt'])))
        self.assertIn('A visible rule', list(leaf_strings(shown[2]['prompt'])))
        self.assertIn('bin', list(leaf_strings(shown[3]['prompt'])))
        graph = shown[4]['prompt'][0]
        self.assertEqual([node['number'] for node in graph['nodes']], [1, 2])
        self.assertEqual(graph['edges'], [{'from': 1, 'to': 2, 'label': ''}])
        self.assertIn('Second cue', list(leaf_strings(graph)))
        self.assertEqual(shown[5]['prompt'][0]['media'], 'unavailable')
        self.assertIn('Visible image caption.', list(leaf_strings(shown[5]['prompt'])))
        for marker in ('correctsemantic', 'wrongsemantic', 'PRIVATE-GENERATION-PROMPT'):
            self.assertNotIn(marker, json.dumps(report))

    def test_blind_seed_is_display_only_deterministic_and_filter_is_exact(self):
        self.write_bank()
        before = (self.root / INPUT).read_bytes()
        default = self.audit('--mode', 'blind')
        explicit = self.audit('--mode', 'blind', '--seed', 'review-v1')
        self.assertEqual(default, explicit)
        changed = self.audit('--mode', 'blind', '--seed', 'different-review')
        self.assertNotEqual([row['options'] for row in default['questions']], [row['options'] for row in changed['questions']])
        for original, new in zip(default['questions'], changed['questions']):
            self.assertCountEqual([option['text'] for option in original['options']], [option['text'] for option in new['options']])
            self.assertEqual(original['prompt'], new['prompt'])
        filtered = self.audit('--mode', 'blind', '--question', 'source-question-12')
        self.assertEqual(len(filtered['questions']), 1)
        self.assertEqual(filtered['questions'][0]['token'], 'Q1')
        self.assertIn('Choose the form for context 12.', list(leaf_strings(filtered['questions'][0]['prompt'])))
        self.assertEqual(before, (self.root / INPUT).read_bytes())
        self.assertEqual(self.audit('--mode', 'blind', '--question', 'unknown-id', success=False)['error'], 'question_not_found')

    def test_malformed_canonical_failures_have_no_file_content_or_partial_packet(self):
        valid = bank_text()
        cases = [valid.replace('qmd: 0.2', 'qmd: 0.1'),
                 valid + '\n# orphan-question\nnot quiz syntax\n',
                 valid.replace('"correct": false', '"correct": true', 1),
                 valid.replace('Choose the form for context 0.', '[solution]{.hide}'),
                 valid.replace('Choose the form for context 0.', 'x' * 200001),
                 bank_text([blank(index, sentence='Ich hier.') for index in range(25)])]
        for text in cases:
            with self.subTest(prefix=text[:70]):
                self.assertFalse(self.canonical(text)['ok'])
                self.write_bank(text)
                for mode in ('summary', 'blind'):
                    report = self.audit('--mode', mode, success=False)
                    self.assertEqual(report['error'], 'qmd_parse_failed')
                    self.assertNotIn('PRIVATE-', json.dumps(report))
                    self.assertNotIn('questions', report)
                    self.assertNotIn('banks', report)
        self.write_bank().write_bytes(b'\xffinvalid utf8')
        self.assertEqual(self.audit(success=False)['error'], 'invalid_utf8')
        self.write_bank().write_bytes(b'x' * 800001)
        self.assertEqual(self.audit(success=False)['error'], 'input_too_large')

    def test_invalid_paths_cannot_read_secrets_traversal_or_arbitrary_files(self):
        secret = self.root / 'secrets/private.md'
        secret.parent.mkdir()
        secret.write_text('DO-NOT-EXPOSE-SECRET')
        paths = ['secrets/private.md', str(secret), '../secrets/private.md',
                 'family_packs/../secrets/private.md', 'family_packs/./quiz.md',
                 'family_packs//quiz.md', 'family_packs/quiz.json',
                 'family_packs\\quiz.md', 'family_packs/quiz.md\n', 'family_packs/evil\n/quiz.md', 'family_packs/%2e%2e/private.md']
        for value in paths:
            with self.subTest(value=value):
                report = self.invoke('--input', value, success=False)
                self.assertEqual(report['error'], 'invalid_input_path')
                self.assertNotIn('DO-NOT-EXPOSE', json.dumps(report))
                self.assertNotIn(str(self.root), json.dumps(report))
        self.assertEqual(self.invoke('--input', 'family_packs/missing.md', success=False)['error'], 'input_unavailable')
        (self.root / 'family_packs/directory.md').mkdir(parents=True)
        self.assertEqual(self.invoke('--input', 'family_packs/directory.md', success=False)['error'], 'not_regular_input')

    def test_reject_symlinks_in_file_and_each_ancestor_and_named_pipe(self):
        target = self.write_bank()
        symlink_file = target.with_name('link.md')
        symlink_file.symlink_to(target)
        self.assertEqual(self.invoke('--input', str(symlink_file.relative_to(self.root)), success=False)['error'], 'symlink_input')
        for relative, destination in [('family_packs/linked', target.parent), ('family_runs', self.root / 'family_packs')]:
            link = self.root / relative
            link.symlink_to(destination, target_is_directory=True)
            suffix = '/quiz.md' if relative.endswith('linked') else '/de/000020/en/stage-0/quiz.md'
            self.assertEqual(self.invoke('--input', relative + suffix, success=False)['error'], 'symlink_input')
        pipe = self.root / 'family_packs/pipe.md'
        os.mkfifo(pipe)
        self.assertEqual(self.invoke('--input', 'family_packs/pipe.md', success=False)['error'], 'not_regular_input')

    def test_strict_argument_cardinality_modes_and_bounds(self):
        invalid = [([], 'input_count'),
                   (['--input'], 'invalid_arguments'),
                   (['--input', INPUT, '--input', INPUT], 'duplicate_input'),
                   (sum((['--input', f'family_packs/bank-{index}.md'] for index in range(26)), []), 'input_count'),
                   (['--input', INPUT, '--mode', 'key'], 'invalid_mode'),
                   (['--input', INPUT, '--mode', 'blind', '--mode', 'summary'], 'duplicate_argument'),
                   (['--input', INPUT, '--question', 'source-question-0'], 'invalid_question_filter'),
                   (['--input', INPUT, '--mode', 'blind', '--question', '../private'], 'invalid_question_filter'),
                   (['--input', INPUT, '--seed', 'x' * 129], 'invalid_seed'),
                   (['--input', INPUT, '--seed', ''], 'invalid_seed'),
                   (['--input', INPUT, '--seed', '\n'], 'invalid_seed'),
                   (['--input', INPUT, '--seed', 'review\n'], 'invalid_seed'),
                   (['--input', INPUT, '--mode', 'blind', '--question', 'source-question-0\n'], 'invalid_question_filter'),
                   (['--input', INPUT, '--output', 'file.json'], 'invalid_arguments'),
                   (['--input', INPUT, '--help'], 'invalid_arguments')]
        for arguments, error in invalid:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(*arguments, success=False)['error'], error)
        # The inclusive upper bound must work with real parseable distinct files.
        arguments = []
        for index in range(25):
            relative = f'family_packs/bank-{index}.md'
            self.write_bank(bank_text(identity=f'bank-{index}'), relative)
            arguments += ['--input', relative]
        self.assertEqual(len(self.invoke(*arguments)['banks']), 25)
        help_result = subprocess.run([NODE, str(self.tool), '--help'], cwd=self.root,
                                     capture_output=True, text=True, timeout=30, check=True)
        self.assertIn('summary|blind', help_result.stdout)
        self.assertIn('review-v1', help_result.stdout)


if __name__ == '__main__':
    unittest.main()
