import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest


PROJECT = Path(__file__).resolve().parents[1]
LESSON_POLICY = PROJECT / 'tools/lesson-policy.cjs'
TABLE = '| Form | Meaning |\n| --- | --- |\n| eins | one |\n'
CAPTION_MESSAGE = 'possible translated syntax marker; if caption intended use literal Table: and keep translated value'


def page_body(words):
    chunks = []
    while words:
        count = min(words, 60)
        chunks.append(' '.join(['word'] * count))
        words -= count
    return '\n\n'.join(chunks)


def document(counts):
    return '---\ngmd: 0.3\nid: de-length-fixture\ntitle: Length fixture\nlang: de\nexplained_in: en\nlevel: A1\n---\n\n' + ''.join(
        f'# Topic {{#page-{index}}}\n\n{page_body(count - 1)}\n\n' for index, count in enumerate(counts)
    )


def caption_document(body):
    return document([2]) + body


def caption_warnings(result):
    return [issue for issue in result['adjusted']['issues'] if issue.get('code') == 'table-caption-prefix']


class LessonPolicyTests(unittest.TestCase):
    def run_node(self, script, value=None):
        result = subprocess.run([os.environ.get('NODE_BINARY', 'node'), '-e', script, str(LESSON_POLICY)], cwd=PROJECT,
                                input=json.dumps(value), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def parse(self, text):
        script = '''
const fs = require('node:fs');
const G = require('./reference/formats/kit/gmd.js');
const {POLICY, parseLesson} = require(process.argv[1]);
const before = JSON.stringify(G.LIMITS);
const text = JSON.parse(fs.readFileSync(0, 'utf8'));
const raw = G.parse(text);
const adjusted = parseLesson(G, text);
const rawAgain = G.parse(text);
const semantic = adjusted.ok ? G.toJSON(adjusted) : null;
console.log(JSON.stringify({policy: POLICY, raw, adjusted, unchanged: before === JSON.stringify(G.LIMITS) && JSON.stringify(raw) === JSON.stringify(rawAgain), sameExport: raw.ok && adjusted.ok ? JSON.stringify(G.toJSON(raw)) === JSON.stringify(semantic) : null, html: adjusted.ok ? G.renderBlocks(adjusted, 0) : null}));
'''
        return self.run_node(script, text)

    def test_page_threshold_inclusive_and_no_old_warning(self):
        for count in (350, 351, 599, 600):
            with self.subTest(words=count):
                result = self.parse(document([count]))
                self.assertEqual(result['adjusted']['stats']['words'], count)
                self.assertTrue(result['adjusted']['ok'])
                self.assertEqual(result['adjusted']['issues'], [])
                self.assertTrue(result['sameExport'])
                self.assertTrue(result['unchanged'])

    def test_long_pages_warn_without_word_count_rejection(self):
        for count in (601, 1200, 4000):
            with self.subTest(words=count):
                result = self.parse(document([count]))
                self.assertFalse(result['raw']['ok'])
                self.assertTrue(result['adjusted']['ok'], result['adjusted']['issues'])
                warnings = [entry for entry in result['adjusted']['issues'] if entry.get('code') == 'page-length']
                self.assertEqual(len(warnings), 1)
                self.assertEqual(warnings[0]['level'], 'warn')
                self.assertEqual(result['html'].count('word'), count - 1)
                self.assertTrue(result['unchanged'])

    def test_lesson_threshold_not_old_1600_and_not_family_total(self):
        for counts, lesson_warning in (([600] * 3, False), ([600] * 5, False), ([600] * 4 + [601], True)):
            with self.subTest(counts=counts):
                result = self.parse(document(counts))['adjusted']
                self.assertTrue(result['ok'], result['issues'])
                warnings = [entry for entry in result['issues'] if entry.get('code') == 'lesson-length']
                self.assertEqual(len(warnings), int(lesson_warning))
                if warnings:
                    self.assertIn('3000-word recommendation', warnings[0]['msg'])

    def test_structural_and_resource_errors_still_block(self):
        for text in (document([601] * 6), document([601]) + '\n<h1>Unsafe</h1>\n', document([601]) + '\n' + 'x' * 200001):
            with self.subTest(text_length=len(text)):
                result = self.parse(text)
                self.assertFalse(result['adjusted']['ok'])
                self.assertTrue(any(issue['level'] == 'error' for issue in result['adjusted']['issues']))
                self.assertTrue(result['unchanged'])

    def test_short_content_diagnostics_and_semantics_unchanged(self):
        result = self.parse(document([12]) + '\n[example]{.undefined}\n')
        self.assertEqual(result['raw']['issues'], result['adjusted']['issues'])
        self.assertTrue(result['sameExport'])

    def test_reference_version_drift_requires_explicit_policy_review(self):
        script = '''
const {parseLesson} = require(process.argv[1]);
const G = require('./reference/formats/kit/gmd.js');
try { parseLesson({...G, VERSION: '0.4'}, '---\\ngmd: 0.3\\nid: fixture\\ntitle: Fixture\\nlang: de\\nlevel: A1\\n---\\n# Topic\\ntext'); process.exit(1); }
catch (error) { if (!error.message.includes('Review lesson-length policy')) throw error; }
console.log('true');
'''
        self.assertTrue(self.run_node(script))

    def test_localized_caption_prefixes_are_nonblocking_without_rewrite(self):
        captions = (
            'Tabla: Tres formas.', 'Tabelle: Drei Formen.', 'Tableau : trois formes.',
            'Tableau\u202f: trois formes.', 'Tabella: Tre forme.', 'Tabela: Três formas.',
            'Таблица: Три формы.', '表: 三种形式。', '表：三种形式。', '표: 세 가지 형태.',
            'तालिका: तीन रूप।', 'جدول: ثلاثة أشكال.',
        )
        for caption in captions:
            with self.subTest(caption=caption):
                text = caption_document(TABLE + caption + '\n')
                result = self.parse(text)
                self.assertTrue(result['raw']['ok'])
                self.assertTrue(result['adjusted']['ok'])
                self.assertEqual(caption_warnings(result), [{
                    'level': 'warn', 'line': text.splitlines().index(caption) + 1,
                    'code': 'table-caption-prefix', 'msg': CAPTION_MESSAGE,
                }])
                self.assertEqual(result['adjusted']['pages'], result['raw']['pages'])
                self.assertNotIn('caption', result['adjusted']['pages'][0]['blocks'][-2])
                self.assertEqual(result['adjusted']['pages'][0]['blocks'][-1]['text'], caption)
                self.assertTrue(result['sameExport'])
                self.assertTrue(result['unchanged'])

    def test_english_caption_and_uncaptioned_table_are_unchanged(self):
        for suffix in ('', 'Table: Trois formes.\n', 'Table: 表：三种形式。\n'):
            with self.subTest(suffix=suffix):
                result = self.parse(caption_document(TABLE + suffix))
                self.assertTrue(result['adjusted']['ok'])
                self.assertEqual(result['raw']['issues'], result['adjusted']['issues'])
                self.assertEqual(caption_warnings(result), [])
                self.assertTrue(result['sameExport'])
                table = result['adjusted']['pages'][0]['blocks'][-1]
                self.assertEqual(table.get('caption'), suffix.removeprefix('Table: ').strip() or None)

    def test_only_immediate_sibling_paragraph_after_table_is_advised(self):
        bodies = (
            'Tabla: Ordinary prose, with no table.\n',
            '# Tabla: Title {#caption-title}\n',
            '## Tabelle: Heading\n',
            TABLE + '# Tabla: Next page {#next-page}\n',
            TABLE + '## Tableau : section\n',
            TABLE + '\nTabla: Detached paragraph.\n',
            TABLE + '\n\nTabla: Detached paragraph.\n',
            TABLE + 'Ordinary explanation.\nTabla: Part of that prose, not a caption line.\n',
            TABLE + 'A Tabla: mention in ordinary prose.\n',
            TABLE + 'Graphique: A different label.\n',
            TABLE + '`Tabla: Inline code.`\n',
            TABLE + '- Tabla: List item.\n',
            TABLE + 'Table: Correct caption.\nTabla: Already captioned.\n',
            TABLE + 'Tabla: Definition term\n: Definition value\n',
        )
        for body in bodies:
            with self.subTest(body=body):
                result = self.parse(caption_document(body))
                self.assertTrue(result['adjusted']['ok'], result['adjusted']['issues'])
                self.assertEqual(caption_warnings(result), [])
                self.assertEqual(result['raw']['issues'], result['adjusted']['issues'])
                self.assertTrue(result['sameExport'])

    def test_fences_callout_titles_and_comments_do_not_supply_table_relation(self):
        bodies = (
            '```pattern\n' + TABLE + 'Tabla: Fenced table-like text.\n```\n',
            TABLE + '```pattern\nTabla: Fenced text after real table.\n```\n',
            TABLE + '> [!note] Tabla: Callout title\n> Ordinary note.\n',
            TABLE + '> [!note]\n> Tabla: Callout prose after real table.\n',
            '> [!note]\n> ```pattern\n> ' + TABLE.replace('\n', '\n> ') + 'Tabla: Fenced in callout.\n> ```\n',
            '%%\n' + TABLE + 'Tabla: Commented-out table.\n%%\n',
            TABLE + '%%\nTabla: Commented-out caption.\n%%\n',
        )
        for body in bodies:
            with self.subTest(body=body):
                result = self.parse(caption_document(body))
                self.assertEqual(caption_warnings(result), [])
                self.assertEqual(result['raw']['issues'], result['adjusted']['issues'])
                self.assertTrue(result['unchanged'])

    def test_actual_tables_in_tabs_use_same_sibling_relation(self):
        body = ('> [!tabs]\n> **First**\n>\n> ' + TABLE.replace('\n', '\n> ') +
                'Tabla: Primera forma.\n>\n> **Second**\n>\n> ' + TABLE.replace('\n', '\n> ') +
                'Table: Deuxième forme.\n')
        text = caption_document(body)
        result = self.parse(text)
        self.assertTrue(result['adjusted']['ok'], result['adjusted']['issues'])
        self.assertEqual(caption_warnings(result), [{
            'level': 'warn', 'line': text.splitlines().index('> Tabla: Primera forma.') + 1,
            'code': 'table-caption-prefix', 'msg': CAPTION_MESSAGE,
        }])
        self.assertTrue(result['sameExport'])
        self.assertTrue(result['unchanged'])

    def test_multiple_tables_and_source_newline_normalization(self):
        body = TABLE + 'Tabla: Primera.\n\n' + TABLE + '表：第二。\n'
        text = caption_document(body)
        for source in (text, '\ufeff' + text.replace('\n', '\r\n'), text.replace('\n', '\r')):
            with self.subTest(prefix=repr(source[:16])):
                result = self.parse(source)
                self.assertEqual([issue['line'] for issue in caption_warnings(result)], [
                    text.splitlines().index('Tabla: Primera.') + 1,
                    text.splitlines().index('表：第二。') + 1,
                ])
                self.assertTrue(result['sameExport'])
                self.assertTrue(result['unchanged'])

    def test_caption_advisory_does_not_hide_invalid_syntax_or_length_policy(self):
        for body in (TABLE + 'Tabla: Caption.\n\n<h1>Unsafe</h1>\n',
                     '| Form | Meaning |\n| eins | one |\nTabla: Missing separator.\n',
                     TABLE + 'Tabla: Caption.\n\n```pattern\nnot closed\n'):
            with self.subTest(body=body):
                result = self.parse(caption_document(body))
                self.assertFalse(result['adjusted']['ok'])
                self.assertEqual([issue for issue in result['raw']['issues'] if issue['level'] == 'error'],
                                 [issue for issue in result['adjusted']['issues'] if issue['level'] == 'error'])
                self.assertTrue(result['unchanged'])
        result = self.parse(document([601]) + TABLE + 'Tabla: Caption.\n')
        self.assertFalse(result['raw']['ok'])
        self.assertTrue(result['adjusted']['ok'])
        self.assertEqual(sorted(issue.get('code') for issue in result['adjusted']['issues']),
                         ['page-length', 'table-caption-prefix'])
        self.assertEqual(result['policy'], {'id': 'lesson-length-v1', 'pageWordsWarn': 600, 'lessonWordsWarn': 3000})

    def test_advisory_is_runtime_only_and_family_contract_unchanged(self):
        # Load the selected wrapper at the normal require path without editing production files.
        from quiz_fixtures import quiz_text

        text = caption_document(TABLE + 'Tabla: Tres formas.\n')
        quiz = quiz_text(lesson='de-length-fixture', page='page-0', options=4)
        script = '''
const fs = require('node:fs'), assert = require('node:assert/strict');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const G = require('./reference/formats/kit/gmd.js');
const original = G.parse;
const selected = require(process.argv[1]);
const policyPath = require.resolve('./tools/lesson-policy.cjs');
require.cache[policyPath] = {id: policyPath, filename: policyPath, loaded: true, exports: selected};
const rawLesson = G.parse(input.lesson);
const parsed = selected.parseLesson(G, input.lesson);
const {contractFor, virtualFamily} = require('./tools/content-contract.cjs');
const {renderPreview} = require('./tools/render-preview.cjs');
const preview = renderPreview({stage: 0, language: 'de', locale: 'en', lesson_md: input.lesson, quiz_md: input.quiz});
assert.equal(preview.ok, true);
assert.equal(preview.issues.filter(issue => issue.code === 'table-caption-prefix').length, 1);
const rawRecord = {stage: 0, lesson: G.toJSON(rawLesson), bank: preview.bank};
const adjustedRecord = {...rawRecord, lesson: G.toJSON(parsed)};
const contract = record => contractFor(record.lesson, record.bank, virtualFamily([record]), [record], [], {}, 'de');
assert.deepEqual(contract(rawRecord), contract(adjustedRecord));
// Embedded quiz GMD sections deliberately do not route through parseLesson.
const Q = require('./tools/quiz-policy.cjs');
const rich = Q.parseRich(input.quiz);
assert.equal(rich.parsed.ok, true);
assert.equal(rich.parsed.issues.some(issue => issue.code === 'table-caption-prefix'), false);
assert.equal(rich.rich.some(section => section.explanation.issues.some(issue => issue.code === 'table-caption-prefix')), false);
assert.equal(G.parse, original);
console.log(JSON.stringify({ok: true}));
'''
        quiz_with_table = quiz_text(lesson='de-length-fixture', page='page-0', options=4,
                                   explanation=TABLE + 'Tabla: Explanatory prose remains unchanged.')
        for bank in (quiz, quiz_with_table):
            with self.subTest(quiz_sections_have_table=bank == quiz_with_table):
                self.assertEqual(self.run_node(script, {'lesson': text, 'quiz': bank}), {'ok': True})

    def test_caption_warning_leaves_review_binding_and_approval_stable(self):
        from unittest.mock import patch
        from lesson_factory import core, family

        run = 'family_runs/de/000020'
        context = {'context_hash': 'synthetic-context-hash', 'source_locale': 'en'}
        # Synthetic immutable bytes: no authoring lock, publication or review submission.
        files = {'lesson.md': caption_document(TABLE + 'Tabla: Tres formas.\n').encode(),
                 'quiz.md': b'Synthetic quiz', 'adaptation-notes.json': b'[]'}
        digests = {}
        for stage in family.STAGES:
            digest = hashlib.sha256()
            for data in files.values():
                digest.update(len(data).to_bytes(8, 'big'))
                digest.update(data)
            digests[f'stage:{stage}'] = digest.hexdigest()
        with patch.object(family, 'load_family', return_value=context), \
                patch.object(Path, 'read_bytes', autospec=True, side_effect=lambda path: files[path.name]), \
                patch.object(core, 'digest', return_value='synthetic-brief-hash'):
            before = family.family_binding(PROJECT, run, 'format')
            before_locale = family.family_binding(PROJECT, run, 'language:fr')
            result = self.parse(files['lesson.md'].decode())
            after = family.family_binding(PROJECT, run, 'format')
            after_locale = family.family_binding(PROJECT, run, 'language:fr')
        self.assertEqual(len(caption_warnings(result)), 1)
        self.assertEqual(before, after)
        self.assertEqual(before_locale, after_locale)
        self.assertEqual(after['source_digests'], digests)
        self.assertEqual(after_locale['locale_digests'], digests)
        review = {
            'role': 'format', 'status': 'approved', 'reviewer_session': 'synthetic:caption-regression',
            'binding': before, 'checklist': ['Synthetic regression, not a genuine approval'],
            'findings': ['Synthetic fixture'], 'stages': [
                {'stage': stage, 'checklist': ['Synthetic fixture'], 'findings': ['Synthetic fixture']}
                for stage in family.STAGES
            ],
        }
        with patch.object(core, 'read_json', return_value=review), \
                patch.object(family, 'family_binding', return_value=after):
            self.assertEqual(family.require_family_review(PROJECT, run, 'format'), review)
