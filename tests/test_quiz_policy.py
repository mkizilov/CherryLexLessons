import copy
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_family_bridge
from quiz_fixtures import lesson_text, quiz_text


PROJECT = Path(__file__).resolve().parents[1]


def prompt_only(text):
    return text.replace('qmd: 0.1', 'qmd: 0.2').replace('## Sentence\n{{blank}}\n', '')


class QuizPolicyTests(unittest.TestCase):
    def evaluate(self, text, extra=''):
        script = '''
const fs = require('node:fs');
const Q = require('./tools/quiz-policy.cjs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const parsed = Q.parse(input);
let bank = null, stem = null, grade = null, rich = null;
if (parsed.ok) {
 bank = Q.toJSON(parsed);
 const snapshot = Q.instantiate(bank, bank.questions[0].id, {seed: 42});
 stem = Q.present(bank, snapshot);
 grade = Q.grade(bank, snapshot, bank.questions[0].options?.find(o => o.correct).id || 'wort1');
 rich = Q.parseRich(input).rich;
}
''' + extra + '''
console.log(JSON.stringify({parsed, bank, stem, grade, rich}));
'''
        result = subprocess.run([os.environ.get('NODE_BINARY', 'node'), '-e', script], cwd=PROJECT,
                                input=json.dumps(text), capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def test_prompt_only_and_empty_sentence_without_pseudo_cue(self):
        source = prompt_only(quiz_text(options=4))
        for text in (source, source.replace('## Explanation', '## Sentence\n \t\n## Explanation')):
            result = self.evaluate(text)
            self.assertTrue(result['parsed']['ok'], result['parsed']['issues'])
            self.assertEqual(result['bank']['qmd'], '0.2')
            self.assertEqual(result['bank']['sentence_semantics'], 'choice-optional-blank-required-v1')
            self.assertEqual(result['stem']['sentence'], [])
            self.assertTrue(result['stem']['prompt'])
            self.assertEqual(result['grade']['status'], 'correct')
            self.assertNotIn('explanation', result['stem'])
            self.assertNotIn('correct', json.dumps(result['stem']))
            self.assertTrue(all(section['sentence'] is None for section in result['rich']))
            self.assertNotIn('Quiz content', json.dumps(result['bank']))
            self.assertNotIn('Quiz content', json.dumps(result['stem']))

    def test_current_version_only_public_api(self):
        self.assertFalse(self.evaluate(quiz_text(options=4).replace('qmd: 0.2', 'qmd: 0.1'))['parsed']['ok'])
        self.evaluate(quiz_text(options=4), '''
const old = {...bank, qmd:'0.1'};
const snapshot = Q.instantiate(bank, bank.questions[0].id, {seed:1});
for (const call of [()=>Q.toJSON({...parsed,meta:{...parsed.meta,qmd:'0.1'}}),()=>Q.instantiate(old,old.questions[0].id,{seed:1}),()=>Q.present(old,snapshot),()=>Q.grade(old,snapshot,'option-0')]) {
 let failed=false; try { call(); } catch { failed=true; } if (!failed) throw new Error('Old version accepted');
}
''')

    def test_invalid_sections_controls_metadata_and_resource_bounds(self):
        valid = prompt_only(quiz_text(options=4))
        malformed = [valid.replace('Choose the form.', ''), valid.replace('The form is [wort1]{.tl}.', ''),
                     valid.replace('qmd: 0.2', 'qmd: 0.3'), valid.replace('title: Test quiz', 'unknown: value\ntitle: Test quiz'),
                     valid.replace('"skill": "form"', '"skill": "form", "unknown": 1'),
                     valid.replace('Choose the form.', '[answer]{.hide}'),
                     valid.replace('Choose the form.', '[answer]{tip="solution"}'), valid + 'x' * 200001,
                     valid.replace('Choose the form.', '{{blank}}\n\n# Unexpected'),
                     valid.replace('"text": "wort1"', '"text": "[wort1]{.tl}"'),
                     valid.replace('"correct": false', '"correct": true', 1),
                     valid.replace('"text": "wort2"', '"text": "wort1"')]
        for text in malformed:
            with self.subTest(prefix=text[:70]):
                self.assertFalse(self.evaluate(text)['parsed']['ok'])

    def test_typed_blank_remains_one_visible_plain_gap(self):
        valid = quiz_text().replace('qmd: 0.1', 'qmd: 0.2')
        self.assertTrue(self.evaluate(valid)['parsed']['ok'])
        for text in (valid.replace('## Sentence\n{{blank}}\n', ''), valid.replace('{{blank}}', ''),
                     valid.replace('{{blank}}', '{{blank}} {{blank}}'), valid.replace('{{blank}}', '[{{blank}}]{.tl}'),
                     valid.replace('{{blank}}', '[{{blank}}]{.hide}'), valid.replace('{{blank}}', '[{{blank}}]{say}'),
                     valid.replace('{{blank}}', '[{{blank}}]{tip="hint"}')):
            self.assertFalse(self.evaluate(text)['parsed']['ok'])

    def test_snapshot_revision_and_semantics_fail_closed(self):
        self.evaluate(prompt_only(quiz_text(options=4)), '''
const snapshot = Q.instantiate(bank, bank.questions[0].id, {seed: 'test'});
for (const changed of [{...snapshot, qmd:'0.1'}, {...snapshot, revision:'old'}, {...snapshot, extra:true}, {...snapshot, optionIds:['option-0','option-0','option-2','option-3']}]) {
 let failed = false; try { Q.present(bank, changed); } catch { failed = true; } if (!failed) throw new Error('Snapshot accepted');
}
for (const changed of [{...bank, sentence_semantics:'old'}, {...bank, revision:'old'}, {...bank, questions:bank.questions.map(q => ({...q, sentence:[{t:'text',v:'cue'}]}))}]) {
 let failed = false; try { Q.instantiate(changed, changed.questions[0].id, {seed:1}); } catch { failed = true; } if (!failed) throw new Error('Old bank accepted');
}
''')

    def test_project_template_draft_and_browser_api(self):
        script = '''
const fs = require('node:fs'), vm = require('node:vm');
const Q = require('./tools/quiz-policy.cjs');
const text = fs.readFileSync('./templates/quiz.md','utf8');
const draft = Q.parse(text,{draft:true});
if (!draft.ok || draft.questions.length !== 2 || draft.questions[0].sentence.length || draft.questions[1].sentence.filter(n=>n.t==='blank').length !== 1) throw new Error(JSON.stringify(draft.issues));
const context = vm.createContext({Intl});
for (const filename of ['reference/formats/kit/gmd.js','reference/formats/kit/qmd.js','tools/quiz-policy.cjs']) vm.runInContext(fs.readFileSync(filename,'utf8'),context);
if (!context.ProjectQMD.parse(text,{draft:true}).ok) throw new Error('Browser parse failed');
'''
        subprocess.run([os.environ.get('NODE_BINARY', 'node'), '-e', script], cwd=PROJECT,
                       capture_output=True, text=True, check=True, timeout=30)

    def test_actual_preview_rich_mapping_and_policy(self):
        script = '''
const {renderPreview} = require('./tools/render-preview.cjs');
const fs = require('node:fs');
console.log(JSON.stringify(renderPreview(JSON.parse(fs.readFileSync(0,'utf8')))));
'''
        value = {'language': 'de', 'locale': 'en', 'lesson_md': lesson_text(),
                 'quiz_md': prompt_only(quiz_text(options=4)), 'option_text_policies': {'base-0': 'learner'}}
        def preview(payload):
            result = subprocess.run([os.environ.get('NODE_BINARY', 'node'), '-e', script], cwd=PROJECT,
                                    input=json.dumps(payload), capture_output=True, text=True, check=True, timeout=30)
            return json.loads(result.stdout)
        result = preview(value)
        self.assertTrue(result['ok'], result['issues'])
        self.assertEqual(len(result['rich']), 25)
        self.assertIsNone(result['rich'][0]['sentence'])
        self.assertEqual(result['rich'][0]['option_language'], 'en')
        self.assertEqual(result['rich'][1]['option_language'], 'de')
        self.assertTrue(all(section['explanation']['pages'] for section in result['rich']))
        value['option_text_policies'] = {'unknown': 'learner'}
        self.assertFalse(preview(value)['ok'])


class QuizFamilyPolicyTests(unittest.TestCase):
    setUp = test_family_bridge.FamilyBridgeTests.setUp
    validate = test_family_bridge.FamilyBridgeTests.validate

    def test_prompt_only_five_stage_contract_and_localization(self):
        for stage in self.stages:
            stage['quiz_md'] = prompt_only(quiz_text(f"bank-{stage['stage']}", 'family-base', 'base-page',
                                                   f"q-{stage['stage']}", options=4))
        for number in range(5):
            brief = {} if not number else {'extension': {'mode': 'practice', 'rationale': 'Existing teaching'}}
            brief['option_text_policies'] = {f'q-{number}-0': 'learner'}
            (self.folder / f'stage-{number}/brief.json').write_text(json.dumps(brief))
        source = self.validate()
        self.assertTrue(source['ok'], source)
        self.assertEqual(source['totals']['questions'], 125)
        self.assertTrue(all(item['quiz_format'] == '0.2' for item in source['family_contract']))
        translated = copy.deepcopy(self.stages)
        for stage in translated:
            stage['lesson_md'] = stage['lesson_md'].replace('explained_in: en', 'explained_in: fr')
            stage['quiz_md'] = stage['quiz_md'].replace('explained_in: en', 'explained_in: fr').replace('Choose the form.', 'Choisissez la forme.')
            stage['quiz_md'] = stage['quiz_md'].replace('"text": "wort1"', '"text": "concept traduit"', 1)
        localized = self.validate(translated, 'fr')
        self.assertTrue(localized['ok'], localized)
        self.assertEqual(source['family_contract'], localized['family_contract'])
        translated[0]['quiz_md'] = translated[0]['quiz_md'].replace('"text": "wort2"', '"text": "changed"')
        self.assertNotEqual(source['family_contract'], self.validate(translated, 'fr')['family_contract'])
        changed = copy.deepcopy(self.stages)
        changed[0]['quiz_md'] = changed[0]['quiz_md'].replace('"page": "base-page"', '"page": "unknown"')
        self.assertFalse(self.validate(changed)['ok'])
