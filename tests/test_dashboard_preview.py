import base64
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lesson_factory.dashboard_preview import PreviewError, PreviewService
from quiz_fixtures import lesson_text, quiz_text


PROJECT = Path(__file__).resolve().parents[1]
NODE = os.environ.get('NODE_BINARY', 'node')


DOM_VM = """
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
class E {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.attrs = {}; this.dataset = {}; this.style = {setProperty() {}}; this.classList = {add() {}}; this.textContent = ''; this.value = ''; this.hidden = false; }
  append(...children) {
    const values = data.parseRichHTML ? children.flatMap(child => child.tag === 'fragment' ? child.children : [child]) : children;
    values.forEach(child => { child.parent = this; }); this.children.push(...values);
    if (this.tag === 'select' && !this.value) this.value = values[0]?.value || '';
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  prepend(...children) { children.forEach(child => { child.parent = this; }); this.children.unshift(...children); }
  after(child) { child.parent = this.parent; const siblings = this.parent.children; siblings.splice(siblings.indexOf(this) + 1, 0, child); }
  get childNodes() { return this.children; }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolledIntoView = true; }
  setAttribute(name, value) {
    this.attrs[name] = value;
    if (name === 'class') this.className = value;
    if (['id', 'lang', 'dir'].includes(name)) this[name] = value;
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
  }
  getAttribute(name) { return this.attrs[name]; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelectorAll(selector) {
    const descendants = this.children.filter(child => child instanceof E).flatMap(child => [child, ...child.querySelectorAll('*')]);
    if (!data.parseRichHTML) return selector === '*' ? descendants : selector === '.author-metadata' ? descendants.filter(child => child.className === 'author-metadata') : [];
    const matches = (child, part) => {
      if (part === '*') return true;
      if (part.startsWith('.')) return (child.className || '').split(' ').includes(part.slice(1));
      if (part.startsWith('[')) { const [name, value] = part.slice(1, -1).split('='); return value === undefined ? child.getAttribute(name) !== undefined : child.getAttribute(name) === value.replace(/^['\"]|['\"]$/g, ''); }
      return child.tag === part;
    };
    if (selector.startsWith(':scope > ')) return this.children.filter(child => matches(child, selector.slice(9)));
    return descendants.filter(child => selector.split(',').some(part => matches(child, part)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({name, value})); }
  set innerHTML(value) {
    this.content = new E('fragment');
    if (!data.parseRichHTML) { this.content.textContent = value; return; }
    // Limited trusted-fragment test adapter, not a browser or sanitizer model.
    const stack = [this.content], decode = text => text.replace(/&quot;/g, '\"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    for (const token of value.match(/<[^>]*>|[^<]+/g) || []) {
      if (token.startsWith('</')) { stack.pop(); continue; }
      if (!token.startsWith('<')) { const text = new E('#text'); text.textContent = decode(token); stack[stack.length - 1].append(text); continue; }
      const tag = token.match(/^<([a-z0-9-]+)/i)[1], element = new E(tag);
      for (const match of token.matchAll(/([a-z][\\w-]*)="([^"]*)"/gi)) element.setAttribute(match[1], decode(match[2]));
      stack[stack.length - 1].append(element);
      if (!['br', 'hr'].includes(tag)) stack.push(element);
    }
  }
}
const main = new E('main');
const document = {getElementById: id => id === 'preview' ? main : {textContent: JSON.stringify(data)}, createElement: tag => new E(tag)};
const realQ = require('./tools/quiz-policy.cjs'), realG = require('./reference/formats/kit/gmd.js');
const counts = {renders: 0, draws: 0, grades: 0};
const QMD = {...realQ, instantiate: (...args) => { counts.draws++; return realQ.instantiate(...args); }, grade: (...args) => { counts.grades++; return realQ.grade(...args); }};
const GMD = {...realG, renderBlocks: (...args) => { counts.renders++; return realG.renderBlocks(...args); }};
const start = performance.now();
vm.runInNewContext(fs.readFileSync('./lesson_factory/dashboard_assets/preview.js', 'utf8'), {window: {GMD, QMD, ProjectQMD: QMD}, document});
const initialMs = performance.now() - start;
const walk = element => [element, ...element.children.filter(child => child instanceof E).flatMap(walk)];
const visibleWalk = element => element.hidden ? [] : [element, ...element.children.filter(child => child instanceof E).flatMap(visibleWalk)];
const find = test => walk(main).find(test);
const text = element => walk(element).map(child => child.textContent).join('\u241e');
const visibleText = element => visibleWalk(element).map(child => child.textContent).join('\u241e');
const click = label => { const control = find(element => element.tag === 'button' && element.textContent === label); assert.ok(control, label); control.events.click(); };
const byId = id => find(element => element.id === id);
const byClass = (root, name) => walk(root).filter(element => element.className === name);
const exposedStatus = 'Correct. Answer previously viewed; not a blind check.';
assert.ok(!text(main).includes('Preview rendering failed safely'), text(main));
"""


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.scripts = []
        self.styles = []
        self.active = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ('script', 'style'):
            self.active = tag
            (self.scripts if tag == 'script' else self.styles).append('')

    def handle_data(self, data):
        if self.active:
            target = self.scripts if self.active == 'script' else self.styles
            target[-1] += data

    def handle_endtag(self, tag):
        if tag == self.active:
            self.active = None


class DashboardPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = PreviewService(self.root)
        self.relative = 'family_runs/de/000020'

    def member(self, stage=0, lesson=None, quiz=None, locale='en', mode=None, relative=None):
        relative = relative or self.relative
        folder = self.root / relative / locale / f'stage-{stage}'
        folder.mkdir(parents=True, exist_ok=True)
        if lesson is None:
            lesson = lesson_text('base-lesson', ('base-page',), locale=locale) if not stage else ''
        if quiz is None:
            quiz = quiz_text(f'bank-{stage}', 'base-lesson', 'base-page', f'q-{stage}', locale=locale)
        (folder / 'lesson.md').write_text(lesson)
        (folder / 'quiz.md').write_text(quiz)
        brief = self.root / relative / f'stage-{stage}'
        brief.mkdir(parents=True, exist_ok=True)
        (brief / 'brief.json').write_text(json.dumps({'extension': {'mode': mode or ('practice' if stage else 'base'), 'rationale': 'Fixture'}}))
        return folder

    def report(self, stage=0):
        return self.service.render_report(self.relative, 'en', stage)

    def test_catalog_family_drafts_pack_files_and_excluded_locale(self):
        self.member()
        self.member(relative='family_packs/de/000040')
        (self.root / self.relative / 'de').mkdir()
        (self.root / 'packs/ru/000020/en').mkdir(parents=True)
        catalog = self.service.catalog()
        self.assertEqual(len(catalog), 2)
        draft = next(item for item in catalog if item['path'] == self.relative)
        self.assertEqual(draft['kind'], 'draft')
        self.assertEqual(draft['stages'], [0, 1, 2, 3, 4])
        self.assertEqual(draft['locales'], ['en'])
        pack = next(item for item in catalog if item['path'].startswith('family_packs/'))
        self.assertIn('pack files', pack['label'])
        self.assertNotIn('finalized', pack['label'])
        self.assertTrue(all('legacy' not in item for item in catalog))
        self.assertFalse((self.root / 'registry.sqlite').exists())

    def test_long_teaching_renders_completely_with_advisory_warnings(self):
        body = '\n\n'.join([' '.join(['word'] * 60)] * 12)
        self.member(lesson=lesson_text(body=body))
        report = self.report()
        self.assertTrue(report['ok'], report['issues'])
        self.assertIn('page-length', json.dumps(report['issues']))
        self.assertEqual(report['page_map'][0]['html'].count('word'), 720)
        self.assertEqual(len(report['bank']['questions']), 25)

    def test_base_uses_canonical_full_bank_and_html(self):
        self.member()
        report = self.report()
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(len(report['bank']['questions']), 25)
        self.assertEqual(len(report['rich']), 25)
        self.assertIn('g-p', report['page_map'][0]['html'])
        self.assertEqual(report['page_map'][0]['id'], 'base-page')
        self.assertTrue(report['bank']['lessonRevision'].startswith('family-'))

    def test_practice_uses_prior_pages_without_requiring_future_or_other_locales(self):
        self.member()
        self.member(1)
        report = self.report(1)
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(report['mode'], 'practice')
        self.assertEqual([page['stage'] for page in report['page_map']], [0])
        self.assertIsNone(report['members'][1]['parsed'])
        self.assertFalse((self.root / self.relative / 'en/stage-2').exists())

    def test_extension_composes_more_than_five_family_pages(self):
        self.member(lesson=lesson_text('base-lesson', tuple(f'base-{i}' for i in range(5))), quiz=quiz_text(page='base-0'))
        self.member(1, lesson=lesson_text('extension', ('new-one', 'new-two')), quiz=quiz_text('return-bank', page='new-two'), mode='extend')
        report = self.report(1)
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(len(report['page_map']), 7)
        self.assertEqual(report['bank']['questions'][0]['page'], 'new-two')

    def test_all_five_stages_individually_preview(self):
        for stage in range(5):
            self.member(stage)
        for stage in range(5):
            with self.subTest(stage=stage):
                self.assertTrue(self.report(stage)['ok'])

    def test_family_duplicate_anchor_and_unknown_page_fail_closed(self):
        self.member()
        self.member(1, lesson=lesson_text('extension', ('base-page',)), mode='extend')
        report = self.report(1)
        self.assertFalse(report['ok'])
        self.assertIsNone(report['bank'])
        self.assertIn('Duplicate family page', json.dumps(report['issues']))
        self.member(1, quiz=quiz_text('new-bank', page='future-page'))
        report = self.report(1)
        self.assertFalse(report['ok'])
        self.assertIsNone(report['bank'])
        self.assertIn('Unknown linked page', json.dumps(report['issues']))

    def test_missing_files_and_empty_practice_without_brief_are_diagnostics(self):
        report = self.report()
        self.assertFalse(report['ok'])
        self.assertIn('not written yet', json.dumps(report['issues']))
        self.member()
        folder = self.member(1)
        (self.root / self.relative / 'stage-1/brief.json').unlink()
        report = self.report(1)
        self.assertFalse(report['ok'])
        self.assertIsNone(report['bank'])
        (folder / 'lesson.md').unlink()
        self.assertIn('Lesson not written yet', json.dumps(self.report(1)['issues']))

    def test_partial_quiz_does_not_silently_export(self):
        self.member(quiz=quiz_text(count=4))
        report = self.report()
        self.assertFalse(report['ok'])
        self.assertIsNone(report['bank'])
        self.assertEqual(len(report['page_map']), 1)
        self.assertIn('25..100', json.dumps(report['issues']))

    def test_family_pool_and_bank_limits(self):
        for count, options in ((51, 4), (25, 11)):
            self.member(quiz=quiz_text(count=count, options=options))
            report = self.report()
            self.assertFalse(report['ok'])
            self.assertIsNone(report['bank'])

    def test_language_stage_and_path_restrictions(self):
        self.member()
        for path in ('../secrets', '/etc/passwd', 'packs/de/000020/../../../secrets', 'family_runs/de/000020/en', 'family_runs/de/000020-return-1', 'family_runs/de/000021'):
            with self.subTest(path=path), self.assertRaises(PreviewError):
                self.service.render(path, 'en', 0)
        for locale, stage in (('de', 0), ('../../en', 0), ('en', True), ('en', 5), ('en', '0')):
            with self.subTest(locale=locale, stage=stage), self.assertRaises(PreviewError):
                self.service.render(self.relative, locale, stage)

    def test_symlink_file_locale_and_parent_denied_before_read(self):
        folder = self.member()
        source = folder / 'lesson.md'
        source.unlink()
        source.symlink_to(PROJECT / 'packs/ru/000020/en/lesson.md')
        with self.assertRaises(PreviewError):
            self.report()
        source.unlink()
        source.write_text(lesson_text())
        (self.root / self.relative / 'fr').symlink_to(folder, target_is_directory=True)
        with self.assertRaises(PreviewError):
            self.service.render(self.relative, 'fr')
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other)
            (other_root / 'packs').symlink_to(PROJECT / 'packs', target_is_directory=True)
            with self.assertRaises(PreviewError):
                PreviewService(other_root).render('packs/ru/000020', 'en')

    def test_size_and_timeout_bounds(self):
        folder = self.member()
        (folder / 'quiz.md').write_text('x' * (self.service.MAX_FILE + 1))
        with self.assertRaises(PreviewError):
            self.report()
        self.member()
        with patch('lesson_factory.dashboard_preview.subprocess.run', side_effect=subprocess.TimeoutExpired('node', 10)):
            with self.assertRaisesRegex(PreviewError, 'exceeded'):
                self.report()
        process = subprocess.run([NODE, str(PROJECT / 'tools/render-preview.cjs')], input=b' ' * (self.service.MAX_INPUT + 1), capture_output=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('2 MiB', process.stdout.decode())

    def test_script_frame_injection_is_inert_json_and_csp_hashes_match(self):
        attack = '</script></iframe><script>parent.document.body.remove()</script><img src=x onerror=alert(1)>\u2028\u2029'
        self.member(lesson=lesson_text(body=attack))
        self.assertFalse(self.report()['ok'])
        self.member(quiz=quiz_text().replace('title: Test quiz', 'title: ' + json.dumps(attack)))
        source = self.service.render(self.relative, 'en')
        doc = Document(source)
        self.assertEqual(len(doc.scripts), 5)
        self.assertFalse(any(tag in ('iframe', 'img') for tag, _ in doc.tags))
        self.assertNotIn(attack, source)
        report = json.loads(doc.scripts[0])
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(report['bank']['title'], attack)
        self.assertIn('\\u003c', doc.scripts[0])
        self.assertNotIn('\u2028', doc.scripts[0])
        csp = next(attrs['content'] for tag, attrs in doc.tags if tag == 'meta' and attrs.get('http-equiv') == 'Content-Security-Policy')
        for section in ("default-src 'none'", "connect-src 'none'", "form-action 'none'", "object-src 'none'", "frame-src 'none'", "base-uri 'none'"):
            self.assertIn(section, csp)
        self.assertNotIn('unsafe-inline', csp)
        for script in doc.scripts[1:] + doc.styles:
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertIn('sha256-' + digest, csp)
        self.assertFalse(any(any(name.lower().startswith('on') for name in attrs) for _, attrs in doc.tags))

    def test_unsafe_raw_links_are_not_navigation(self):
        self.member(lesson=lesson_text(body='[Visit](javascript:alert(1))'))
        report = self.report()
        self.assertFalse(report['ok'])
        self.assertFalse(report['page_map'])
        self.member(lesson=lesson_text(body='[[unknown-lesson#other-page|Teaching elsewhere]]'))
        report = self.report()
        self.assertTrue(report['ok'], report['issues'])
        self.assertIn('outside this preview', json.dumps(report['issues']))
        self.assertNotIn('href=', report['page_map'][0]['html'])

    def test_rich_blocks_are_preserved_in_canonical_renderer_and_quiz(self):
        bodies = {
            'table': '| Form | Use |\n|---|---|\n| `wort1` | One |',
            'tabs': '> [!tabs] Compare\n> **First**\n>\n> `wort1`\n>\n> **Second**\n>\n> `wort2`',
            'morph': '```morph\n`wort` + `-en`\n= `worten` | Surface form\n```',
            'timeline': '```timeline\nnow | Present\npoint -2 | Earlier\nspan -3..-1 | Duration\nref -1 | Reference\n```',
            'mermaid': '```mermaid\nflowchart TD\naccTitle: Stages\naccDescr: A simple derivation\nA[Start] --> B[First]\nA --> C[Second]\n```',
            'pattern': '```pattern\n`wort1` + `wort2`\n```',
            'gloss': '```gloss\n`wort1` `wort2`\nNOUN VERB\n```',
            'rule': '> [!rule] A useful rule\n> Use `wort1`.',
            'example': '> [!example]+ Samples\n> - `wort1`\n>   *First meaning*',
            'dialogue': '> [!dialogue]+ Conversation\n> - **A:** `wort1`\n> - **B:** `wort2`',
            'compare': '> [!compare] Compare\n> **First**\n>\n> `wort1`\n>\n> **Second**\n>\n> `wort2`',
            'recall': '> [!recall]- Which form?\n> `wort1`.',
            'list': '- First item\n- Second item',
            'deflist': '`wort1`\n: A first meaning',
            'heading': '## A useful heading\n\n**Bold** and *emphasis*; ~~wrong~~ then {++added++}, ||hidden|| and [word]{tip="A short hint"}.',
            'ruby': '{字|じ}',
            'image': '```image\nalt: A learner speaking\nprompt: Original illustration of a learner speaking\nratio: 3:2\n```',
        }
        for name, body in bodies.items():
            with self.subTest(block=name):
                self.member(lesson=lesson_text(body=body), quiz=quiz_text(explanation=body))
                report = self.report()
                self.assertTrue(report['ok'], report['issues'])
                self.assertEqual(len(report['rich']), 25)
                self.assertTrue(report['rich'][0]['explanation']['ok'])
                expected = {'ruby': '<ruby', 'timeline': 'g-tline', 'mermaid': 'g-dia', 'pattern': 'g-pat', 'image': 'g-img', 'deflist': 'g-dl', 'heading': 'g-h2'}.get(name, f'g-{name}')
                self.assertIn(expected, report['page_map'][0]['html'])

    def test_canonical_attempt_present_grade_draw_and_no_answer_leak(self):
        self.member(quiz=quiz_text(options=10))
        report = self.report()
        script = """
const fs = require('node:fs');
const {createAttempt} = require('./lesson_factory/dashboard_assets/preview.js');
const bank = JSON.parse(fs.readFileSync(0, 'utf8'));
const attempt = createAttempt(bank, bank.questions[0].id);
const before = attempt.snapshot(), shown = attempt.present();
const correct = bank.questions[0].options.find(option => option.correct);
const grade = attempt.check(correct.id);
const after = attempt.snapshot();
const answer = attempt.reveal();
const draws = [attempt.newDraw(), attempt.newDraw(), attempt.newDraw()];
process.stdout.write(JSON.stringify({before, after, shown, grade, answer, draws}));
"""
        result = subprocess.run([NODE, '-e', script], input=json.dumps(report['bank']), capture_output=True, text=True, cwd=PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual['before'], actual['after'])
        self.assertEqual(actual['grade']['status'], 'correct')
        self.assertEqual(actual['grade']['correctAnswer'], 'wort1')
        self.assertTrue(actual['grade']['explanation'])
        self.assertNotIn('explanation', actual['shown'])
        self.assertTrue(all('correct' not in option for option in actual['shown']['options']))
        self.assertEqual(actual['answer']['correctAnswer'], 'wort1')
        for draw in [actual['shown'], *actual['draws']]:
            self.assertEqual(len(draw['options']), 4)
            self.assertEqual([option['label'] for option in draw['options']], ['A', 'B', 'C', 'D'])
            self.assertEqual(sum(option['text'] == 'wort1' for option in draw['options']), 1)
            self.assertEqual(len(set(option['id'] for option in draw['options'])), 4)

    def test_browserless_input_lifecycle_reset_draw_and_picker(self):
        self.member()
        self.run_dom(self.report(), """
const input = () => byId('blank-answer');
const check = () => { input().value = 'wort1'; input().events.input(); click('Check'); return byClass(main, 'answer-status')[0].textContent; };
input().events.compositionstart(); click('Reset answer'); assert.equal(check(), 'Correct.');
input().events.compositionstart(); click('New draw'); assert.equal(check(), exposedStatus);
input().events.compositionstart(); const picker = byId('question-picker');
picker.value = data.bank.questions[1].id; picker.events.change();
assert.equal(check(), 'Correct.');
picker.value = data.bank.questions[0].id; picker.events.change(); assert.equal(check(), exposedStatus);
process.stdout.write(JSON.stringify({ok: true}));
""")

    def run_dom(self, report, script):
        result = subprocess.run([NODE, '-e', DOM_VM + script], input=json.dumps(report), capture_output=True, text=True, cwd=PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_all_questions_full_pools_answers_hidden_and_lazy_reuse(self):
        choices = quiz_text(options=10, prefix='choice', explanation='AUDITEXPLANATION [wort1]{.tl}.').replace('"correct": false', '"correct": false, "why": "OPTIONREASON"')
        blanks = quiz_text(prefix='blank', explanation='BLANKEXPLANATION [wort1]{.tl}.').replace('"answers": ["wort1"]', '"answers": ["wort1", "wort2"], "case": "fold"')
        self.member(quiz=choices + blanks.split('---', 2)[2])
        report = self.report()
        self.assertTrue(report['ok'], report['issues'])
        actual = self.run_dom(report, """
const initial = {...counts};
assert.equal(byClass(main, 'audit-question').length, 0);
assert.equal(byClass(main, 'canonical-answer').length, 0);
assert.equal(text(main).includes('AUDITEXPLANATION'), false);
const before = performance.now(); click('All questions'); const buildMs = performance.now() - before;
const all = byId('quiz-all'), single = byId('quiz-single');
assert.equal(all.hidden, false); assert.equal(single.hidden, true);
const articles = byClass(all, 'audit-question');
assert.deepEqual(articles.map(item => item.dataset.questionId), data.bank.questions.map(item => item.id));
assert.equal(articles.length, 50);
for (const [index, question] of data.bank.questions.entries()) {
  const article = articles[index];
  assert.equal(article.children[0].textContent, `Question ${index + 1}`);
  assert.equal(visibleText(article).includes(question.id), false);
  assert.ok(text(article).includes(question.type));
  assert.ok(text(article).includes('Choose the form.'));
  assert.ok(text(article).includes('blank-slot'));
  if (question.type === 'choice') {
    const options = byClass(article, 'audit-option');
    assert.equal(options.length, question.options.length);
    question.options.forEach((option, n) => {
      assert.equal(byClass(options[n], 'pool-position')[0].textContent, `Pool item ${n + 1}`);
      assert.equal(byClass(options[n], 'option-text')[0].textContent, option.text);
      assert.equal(byClass(options[n], 'author-metadata')[0].textContent, `Option ID: ${option.id}`);
      assert.equal(byClass(options[n], 'author-metadata')[0].hidden, true);
    });
    assert.ok(text(article).includes('Full option pool'));
    assert.ok(text(article).includes('draws 4 options'));
  }
}
assert.ok(visibleText(all).includes('this is not a blind check'));
assert.ok(byClass(all, 'answer-panel').every(panel => panel.hidden && panel.children.length === 0));
assert.equal(text(all).includes('OPTIONREASON'), false);
assert.equal(text(all).includes('EXPLANATION'), false);
assert.equal(counts.draws, 1); assert.equal(counts.grades, 0);
assert.equal(counts.renders - initial.renders, 100);
const built = {...counts};
click('Single question'); click('All questions');
assert.equal(counts.renders, built.renders);
assert.equal(byClass(all, 'audit-question')[0], articles[0]);
const revealStart = performance.now(); click('Show answers'); const revealMs = performance.now() - revealStart;
assert.ok(byClass(all, 'answer-panel').every(panel => !panel.hidden));
assert.equal(byClass(all, 'canonical-answer').length, 50);
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.equal(visibleText(all).includes('Correct option ID:'), false);
assert.ok(visibleText(all).includes('AUDITEXPLANATION'));
assert.ok(visibleText(all).includes('BLANKEXPLANATION'));
assert.ok(visibleText(all).includes('Case policy: fold'));
assert.ok(visibleText(all).includes('OPTIONREASON'));
assert.equal(byClass(all, 'accepted-forms').length, 25);
assert.deepEqual(byClass(all, 'accepted-forms')[0].children.map(item => item.children[0].textContent), ['wort1', 'wort2']);
assert.equal(counts.renders - built.renders, 50);
const revealed = {...counts};
click('Hide answers');
assert.ok(byClass(all, 'answer-panel').every(panel => panel.hidden));
assert.equal(visibleText(all).includes('EXPLANATION'), false);
click('Show answers'); click('Single question');
assert.equal(counts.renders, revealed.renders);
assert.equal(counts.draws, 1); assert.equal(counts.grades, 0);
assert.equal(single.hidden, false); assert.equal(all.hidden, true);
assert.equal(byClass(single, 'canonical-answer').length, 0);
process.stdout.write(JSON.stringify({initialMs, buildMs, revealMs, counts}));
""")
        self.assertEqual(actual['counts'], {'renders': 153, 'draws': 1, 'grades': 0})

    def test_view_switch_preserves_choice_draw_answer_status_and_individual_reveal(self):
        self.member(quiz=quiz_text(options=10))
        actual = self.run_dom(self.report(), """
const single = byId('quiz-single');
const radios = () => walk(single).filter(element => element.type === 'radio');
const before = radios();
const correct = data.bank.questions[0].options.find(option => option.correct).id;
const selected = before.find(input => input.value === correct);
selected.checked = true; selected.events.change(); click('Check');
assert.equal(byClass(single, 'answer-status')[0].textContent, 'Correct.');
const initial = {...counts};
click('All questions'); click('Show answers'); click('Single question');
assert.equal(radios()[0], before[0]);
assert.deepEqual(radios().map(item => item.value), before.map(item => item.value));
assert.equal(selected.checked, true);
assert.equal(byClass(single, 'answer-status')[0].textContent, 'Correct.');
assert.equal(byClass(single, 'canonical-answer').length, 1);
assert.equal(counts.draws, initial.draws); assert.equal(counts.grades, initial.grades);
click('Show answer');
const canonical = byClass(single, 'canonical-answer')[0];
click('All questions'); click('Single question');
assert.equal(byClass(single, 'canonical-answer')[0], canonical);
click('Reset answer');
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.deepEqual(radios().map(item => item.value), before.map(item => item.value));
process.stdout.write(JSON.stringify({draws: counts.draws}));
""")
        self.assertEqual(actual['draws'], 1)

    def test_view_switch_defers_during_ime_without_grading_and_preserves_typed_input(self):
        self.member()
        actual = self.run_dom(self.report(), """
const input = byId('blank-answer'), single = byId('quiz-single');
input.events.compositionstart(); input.value = 'wort'; input.events.input();
click('All questions');
assert.equal(single.hidden, false); assert.equal(byId('quiz-all').hidden, true);
assert.equal(byClass(main, 'audit-question').length, 0);
click('Check'); assert.equal(counts.grades, 0);
input.value = 'wort1'; input.events.compositionend(); input.events.input();
assert.equal(single.hidden, true); assert.equal(byId('quiz-all').hidden, false);
click('Single question');
assert.equal(byId('blank-answer'), input); assert.equal(input.value, 'wort1');
click('Check'); assert.equal(counts.grades, 1);
assert.equal(byClass(single, 'answer-status')[0].textContent, 'Correct.');
click('All questions'); click('Single question');
assert.equal(byClass(single, 'answer-status')[0].textContent, 'Correct.');
assert.equal(byId('blank-answer'), input);
input.events.compositionstart(); click('All questions'); click('Single question');
input.events.compositionend(); assert.equal(single.hidden, false);
process.stdout.write(JSON.stringify({grades: counts.grades, draws: counts.draws}));
""")
        self.assertEqual(actual, {'grades': 1, 'draws': 1})

    def test_complete_choice_check_feedback_and_assisted_reveal(self):
        quiz = quiz_text(options=4, explanation='GENERALFEEDBACK [wort1]{.tl}.')
        for number in range(1, 4):
            quiz = quiz.replace(f'"text": "wort{number + 1}", "correct": false', f'"text": "wort{number + 1}", "correct": false, "why": "SELECTEDWHY-{number}"')
        self.member(quiz=quiz)
        actual = self.run_dom(self.report(), """
const single = byId('quiz-single');
const radios = walk(single).filter(element => element.type === 'radio');
const original = radios.map(element => element.value);
click('Check');
assert.equal(byClass(single, 'canonical-answer').length, 0);
const wrong = radios.find(element => element.value !== 'option-0');
wrong.checked = true; wrong.events.change(); click('Check');
assert.equal(byClass(single, 'canonical-answer')[0].textContent, 'wort1');
assert.ok(text(single).includes(`SELECTEDWHY-${wrong.value.split('-')[1]}`));
for (const option of radios.filter(element => element.value !== wrong.value && element.value !== 'option-0')) assert.ok(!text(single).includes(`SELECTEDWHY-${option.value.split('-')[1]}`));
assert.ok(text(single).includes('GENERALFEEDBACK'));
click('All questions'); click('Single question');
assert.deepEqual(walk(single).filter(element => element.type === 'radio').map(element => element.value), original);
assert.equal(wrong.checked, true);
assert.ok(text(single).includes('SELECTEDWHY'));
click('Reset answer');
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.ok(!text(single).includes('SELECTEDWHY'));
click('Show answer');
assert.ok(byClass(single, 'answer-status')[0].textContent.includes('assistance'));
const correct = walk(single).find(element => element.type === 'radio' && element.value === 'option-0');
correct.checked = true; correct.events.change(); click('Check');
assert.equal(byClass(single, 'answer-status')[0].textContent, exposedStatus);
click('New draw');
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.equal(byClass(single, 'answer-status')[0].textContent, '');
process.stdout.write(JSON.stringify({grades: counts.grades, draws: counts.draws}));
""")
        self.assertEqual(actual, {'grades': 3, 'draws': 2})

    def test_blank_complete_check_feedback_and_no_composing_reveal(self):
        self.member(quiz=quiz_text(explanation='BLANKREASON [wort1]{.tl}.'))
        self.run_dom(self.report(), """
const single = byId('quiz-single'), input = byId('blank-answer');
click('Check'); assert.equal(byClass(single, 'canonical-answer').length, 0);
input.events.compositionstart(); input.value = 'wrong'; input.events.input();
click('Check'); click('Show answer');
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.equal(counts.grades, 1);
input.events.compositionend(); click('Check');
assert.equal(input.value, 'wrong');
assert.equal(byClass(single, 'canonical-answer')[0].textContent, 'wort1');
assert.ok(text(single).includes('BLANKREASON'));
click('All questions'); click('Single question');
assert.equal(byId('blank-answer'), input); assert.equal(input.value, 'wrong');
assert.equal(byClass(single, 'canonical-answer').length, 1);
click('Reset answer'); assert.equal(byClass(single, 'canonical-answer').length, 0);
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_prompt_only_choice_and_language_policy_real_bridge(self):
        quiz = quiz_text(options=4).replace('qmd: 0.1', 'qmd: 0.2').replace('## Sentence\n{{blank}}\n', '')
        self.member(quiz=quiz)
        report = self.report()
        self.assertTrue(report['ok'], report['issues'])
        self.assertTrue(all(question['sentence'] == [] for question in report['bank']['questions']))
        self.assertTrue(all(value['sentence'] is None for value in report['rich']))
        self.run_dom(report, """
assert.equal(byClass(byId('quiz-single'), 'quiz-sentence').length, 0);
click('All questions'); assert.equal(byClass(byId('quiz-all'), 'quiz-sentence').length, 0);
process.stdout.write(JSON.stringify({ok: true}));
""")
        self.run_dom(report, """
const browserMain = new E('main');
const browserDocument = {getElementById: id => id === 'preview' ? browserMain : {textContent: JSON.stringify(data)}, createElement: tag => new E(tag)};
const browserWindow = {};
const context = vm.createContext({window: browserWindow, document: browserDocument});
for (const filename of ['reference/formats/kit/gmd.js', 'reference/formats/kit/qmd.js', 'tools/quiz-policy.cjs', 'lesson_factory/dashboard_assets/preview.js']) vm.runInContext(fs.readFileSync(filename, 'utf8'), context);
assert.ok(browserWindow.ProjectQMD);
assert.ok(!text(browserMain).includes('Preview rendering failed safely'));
assert.equal(byClass(browserMain, 'quiz-sentence').length, 0);
assert.equal(byClass(browserMain, 'option').length, 4);
process.stdout.write(JSON.stringify({ok: true}));
""")
        self.member(quiz=quiz.replace('qmd: 0.2', 'qmd: 0.1'))
        self.assertFalse(self.report()['ok'])
        self.member(quiz=quiz)
        brief = self.root / self.relative / 'stage-0/brief.json'
        policies = {question['id']: 'learner' for question in report['bank']['questions']}
        brief.write_text(json.dumps({'option_text_policies': policies}))
        report = self.report()
        self.assertTrue(report['ok'], report['issues'])
        self.assertTrue(all(value['option_language'] == 'en' for value in report['rich']))
        self.run_dom(report, """
const single = byId('quiz-single');
assert.ok(byClass(single, 'option-text').every(value => value.lang === 'en'));
click('Show answer'); assert.equal(byClass(single, 'canonical-answer')[0].lang, 'en');
click('All questions');
assert.ok(byClass(byId('quiz-all'), 'option-text').every(value => value.lang === 'en'));
process.stdout.write(JSON.stringify({ok: true}));
""")
        for invalid in (None, [], {'missing': 'learner'}, {report['bank']['questions'][0]['id']: 'mixed'}):
            brief.write_text(json.dumps({'option_text_policies': invalid}))
            self.assertFalse(self.report()['ok'])
        self.member(quiz=quiz_text(options=4))
        brief.write_text(json.dumps({'option_text_policies': {'base-0': 'learner'}}))
        self.assertFalse(self.report()['ok'])

    def test_target_sentence_options_answer_language_and_direction(self):
        self.member(lesson=lesson_text().replace('lang: de', 'lang: ar'), quiz=quiz_text(options=4).replace('lang: de', 'lang: ar'), relative='family_runs/ar/000020')
        report = self.service.render_report('family_runs/ar/000020', 'en', 0)
        self.assertTrue(report['ok'], report['issues'])
        self.run_dom(report, """
const single = byId('quiz-single'), sentence = byClass(single, 'quiz-sentence')[0];
assert.equal(sentence.lang, 'ar'); assert.equal(sentence.dir, 'rtl');
assert.ok(byClass(single, 'option-text').every(value => value.lang === 'ar' && value.dir === 'rtl'));
click('Show answer');
assert.equal(byClass(single, 'canonical-answer')[0].lang, 'ar');
assert.equal(byClass(single, 'canonical-answer')[0].dir, 'rtl');
process.stdout.write(JSON.stringify({ok: true}));
""")
        source = self.service.render('family_runs/ar/000020', 'en', 0)
        self.assertIn('controls and status are in English', source)
        self.assertIn('ProjectQMD', source)

    def test_semantic_ids_metadata_gate_and_numbered_pool_positions(self):
        for pool_size in (4, 10):
            for correct_first in (True, False):
                with self.subTest(pool_size=pool_size, correct_first=correct_first):
                    quiz = quiz_text(options=pool_size, prefix='semantic-question', explanation='KEYEXPLANATION [wort1]{.tl}.')
                    semantic_ids = ['friend', 'family', 'correct', 'wrong-agreement']
                    for number, identity in enumerate(semantic_ids):
                        quiz = quiz.replace(f'"id": "option-{number}"', f'"id": "{identity}"')
                    quiz = quiz.replace('"correct": false', '"correct": false, "why": "HIDDENWHY"')
                    if not correct_first:
                        quiz = quiz.replace('"text": "wort1", "correct": true', '"text": "wort1", "correct": false').replace('"text": "wort2", "correct": false', '"text": "wort2", "correct": true')
                    self.member(quiz=quiz)
                    report = self.report()
                    self.assertTrue(report['ok'], report['issues'])
                    self.run_dom(report, """
const originalBank = JSON.stringify(data.bank);
const single = byId('quiz-single'), picker = byId('question-picker');
const rows = byClass(single, 'option'), radios = rows.map(row => row.children[0]);
assert.deepEqual(byClass(single, 'option-letter').map(value => value.textContent), ['A', 'B', 'C', 'D']);
assert.equal(radios.length, 4);
assert.ok(radios.every(radio => data.bank.questions[0].options.some(option => option.id === radio.value)));
assert.equal(picker.children[0].textContent, 'Question 1 · choice');
assert.equal(picker.children[0].value, data.bank.questions[0].id);
const chosen = radios[0]; chosen.checked = true; chosen.events.change();
const before = {...counts};
assert.ok(byClass(main, 'author-metadata').every(value => value.hidden));
assert.equal(visibleText(main).includes('Question ID:'), false);
assert.equal(visibleText(main).includes('Skill ID:'), false);
assert.equal(visibleText(main).includes('Page ID:'), false);
assert.equal(visibleText(main).includes('Option ID:'), false);
assert.equal(visibleWalk(main).some(value => ['friend', 'family', 'correct', 'wrong-agreement'].includes(value.textContent)), false);
click('Show author metadata');
const metadataButton = find(value => value.tag === 'button' && value.textContent === 'Hide author metadata');
assert.equal(metadataButton.attrs['aria-pressed'], 'true');
assert.equal(metadataButton.attrs['aria-controls'], 'preview-content');
assert.equal(metadataButton.attrs['aria-describedby'], 'metadata-warning');
assert.ok(visibleText(main).includes('Question ID: semantic-question-0'));
assert.ok(visibleText(main).includes('Skill ID: form'));
assert.ok(visibleText(main).includes('Page ID: base-page'));
assert.equal(text(main).includes('Correct option ID:'), false);
assert.equal(text(main).includes('HIDDENWHY'), false);
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.equal(byClass(single, 'option')[0], rows[0]);
assert.equal(chosen.checked, true);
assert.equal(rows[0].children.length, 3); // Source IDs are outside the radio label.
assert.equal(byClass(rows[0], 'author-metadata').length, 0);
click('All questions');
const all = byId('quiz-all'), article = byClass(all, 'audit-question')[0];
assert.equal(article.children[0].textContent, 'Question 1');
assert.equal(article.attrs['aria-labelledby'], article.children[0].id);
assert.deepEqual(byClass(article, 'pool-position').map(value => value.textContent), data.bank.questions[0].options.map((_, index) => `Pool item ${index + 1}`));
assert.deepEqual(byClass(article, 'option-text').map(value => value.textContent), data.bank.questions[0].options.map(option => option.text));
assert.ok(visibleText(article).includes('Option ID: friend'));
assert.equal(text(all).includes('Correct option ID:'), false);
assert.ok(byClass(all, 'answer-panel').every(panel => panel.hidden && panel.children.length === 0));
click('Hide author metadata');
assert.ok(byClass(main, 'author-metadata').every(value => value.hidden));
assert.equal(visibleText(all).includes('Option ID:'), false);
assert.equal(visibleText(all).includes('semantic-question-0'), false);
assert.equal(visibleText(all).includes('HIDDENWHY'), false);
click('Show answers');
assert.equal(visibleText(all).includes('Correct option ID:'), false);
assert.ok(visibleText(all).includes('HIDDENWHY'));
const why = byClass(article, 'option-why')[0];
const reason = walk(article).find(value => value.tag === 'li' && value.children.includes(why));
const optionIndex = data.bank.questions[0].options.findIndex(option => option.why);
assert.equal(reason.children[0].textContent, `Pool item ${optionIndex + 1} — `);
assert.equal(reason.children[1].textContent, data.bank.questions[0].options[optionIndex].text);
click('Show author metadata');
assert.ok(visibleText(all).includes(`Correct option ID: ${data.bank.questions[0].options.find(option => option.correct).id}`));
click('Hide answers');
assert.equal(visibleText(all).includes('Correct option ID:'), false);
assert.equal(visibleText(all).includes('HIDDENWHY'), false);
click('Hide author metadata'); click('Single question');
assert.equal(byClass(single, 'option')[0], rows[0]); assert.equal(chosen.checked, true);
assert.deepEqual(walk(single).filter(value => value.type === 'radio').map(value => value.value), radios.map(value => value.value));
assert.equal(JSON.stringify(data.bank), originalBank);
assert.equal(counts.draws, before.draws); assert.equal(counts.grades, 0);
click('Show answer');
assert.equal(visibleText(single).includes('Correct option ID:'), false);
click('Show author metadata');
assert.ok(visibleText(single).includes(`Correct option ID: ${data.bank.questions[0].options.find(option => option.correct).id}`));
click('Reset answer');
assert.equal(text(single).includes('Correct option ID:'), false);
assert.equal(byClass(single, 'canonical-answer').length, 0);
click('Hide author metadata');
assert.equal(JSON.stringify(data.bank), originalBank);
assert.equal(counts.draws, before.draws); assert.equal(counts.grades, 0);
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_answer_exposure_survives_reset_new_draw_and_global_reveal(self):
        self.member(quiz=quiz_text(options=10))
        self.run_dom(self.report(), """
const single = byId('quiz-single'), picker = byId('question-picker');
const answerCorrectly = () => {
  const question = data.bank.questions.find(item => item.id === picker.value);
  const correct = question.options.find(option => option.correct).id;
  const radio = walk(single).find(value => value.type === 'radio' && value.value === correct);
  assert.ok(radio); radio.checked = true; radio.events.change(); click('Check');
  return byClass(single, 'answer-status')[0].textContent;
};
const beforeReveal = {...counts};
click('Show answer');
assert.equal(counts.grades, beforeReveal.grades);
click('Reset answer');
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.equal(answerCorrectly(), exposedStatus);
click('New draw');
assert.equal(byClass(single, 'canonical-answer').length, 0);
assert.equal(answerCorrectly(), exposedStatus);
picker.value = data.bank.questions[1].id; picker.events.change();
assert.equal(answerCorrectly(), 'Correct.');
click('Reset answer'); assert.equal(answerCorrectly(), exposedStatus); // Check itself exposed the key.
picker.value = data.bank.questions[2].id; picker.events.change();
click('All questions'); const grades = counts.grades; click('Show answers'); click('Hide answers');
assert.equal(counts.grades, grades);
click('Single question'); assert.equal(byClass(single, 'canonical-answer').length, 0);
click('Reset answer'); click('New draw');
assert.equal(answerCorrectly(), exposedStatus);
picker.value = data.bank.questions[3].id; picker.events.change();
assert.equal(answerCorrectly(), exposedStatus); // Global reveal covers attempts not created yet.
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_metadata_toggle_preserves_ime_and_blank_exposure_is_not_erased(self):
        self.member()
        self.run_dom(self.report(), """
const single = byId('quiz-single'), input = byId('blank-answer');
input.events.compositionstart(); input.value = 'wort'; input.events.input();
const before = {...counts};
click('Show author metadata'); click('Hide author metadata');
assert.equal(byId('blank-answer'), input); assert.equal(input.value, 'wort');
click('Show answer'); click('Check');
assert.equal(counts.grades, before.grades);
assert.equal(byClass(single, 'canonical-answer').length, 0);
input.value = 'wort1'; input.events.compositionend(); input.events.input();
click('Check'); assert.equal(byClass(single, 'answer-status')[0].textContent, 'Correct.');
click('Reset answer');
const next = byId('blank-answer'); next.value = 'wrong'; next.events.input(); click('Check');
assert.ok(byClass(single, 'answer-status')[0].textContent.endsWith('Answer previously viewed; not a blind check.'));
click('New draw'); click('Show answer'); click('Reset answer');
const last = byId('blank-answer'); last.value = 'wort1'; last.events.input(); click('Check');
assert.equal(byClass(single, 'answer-status')[0].textContent, exposedStatus);
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_english_chrome_arabic_learner_content_and_rationale_attributes(self):
        quiz = quiz_text(options=4, locale='ar').replace('"correct": false', '"correct": false, "why": "تفسير الاختيار"')
        self.member(lesson=lesson_text(locale='ar'), quiz=quiz, locale='ar')
        report = self.service.render_report(self.relative, 'ar', 0)
        self.assertTrue(report['ok'], report['issues'])
        doc = Document(self.service.render(self.relative, 'ar', 0))
        self.assertEqual(next(attrs for tag, attrs in doc.tags if tag == 'html'), {'lang': 'en', 'dir': 'ltr'})
        self.run_dom(report, """
const single = byId('quiz-single');
const checkLanguage = (value, lang, dir) => { assert.equal(value.lang, lang); assert.equal(value.dir, dir); };
checkLanguage(byClass(main, 'grammar-body')[0], 'ar', 'rtl');
checkLanguage(byClass(single, 'quiz-prompt')[0], 'ar', 'rtl');
checkLanguage(byClass(single, 'quiz-sentence')[0], 'de', 'ltr');
byClass(single, 'option-text').forEach(value => checkLanguage(value, 'de', 'ltr'));
byClass(main, 'author-metadata').forEach(value => checkLanguage(value, 'en', 'ltr'));
const wrong = walk(single).find(value => value.type === 'radio' && value.value !== 'option-0');
wrong.checked = true; wrong.events.change(); click('Check');
checkLanguage(byClass(single, 'option-why')[0], 'ar', 'rtl');
checkLanguage(byClass(single, 'quiz-explanation')[0], 'ar', 'rtl');
checkLanguage(byClass(single, 'canonical-answer')[0], 'de', 'ltr');
click('All questions'); click('Show answers');
const all = byId('quiz-all');
byClass(all, 'quiz-prompt').forEach(value => checkLanguage(value, 'ar', 'rtl'));
byClass(all, 'quiz-explanation').forEach(value => checkLanguage(value, 'ar', 'rtl'));
byClass(all, 'option-why').forEach(value => checkLanguage(value, 'ar', 'rtl'));
byClass(all, 'pool-position').forEach(value => checkLanguage(value, 'en', 'ltr'));
process.stdout.write(JSON.stringify({ok: true}));
""")
        quiz = quiz.replace('## Sentence\n{{blank}}\n', '')
        self.member(lesson=lesson_text(locale='ar'), quiz=quiz, locale='ar')
        brief = self.root / self.relative / 'stage-0/brief.json'
        brief.write_text(json.dumps({'option_text_policies': {f'base-{index}': 'learner' for index in range(25)}}))
        self.run_dom(self.service.render_report(self.relative, 'ar', 0), """
const single = byId('quiz-single');
assert.ok(byClass(single, 'option-text').every(value => value.lang === 'ar' && value.dir === 'rtl'));
click('Show answer'); assert.equal(byClass(single, 'canonical-answer')[0].lang, 'ar');
assert.equal(byClass(single, 'canonical-answer')[0].dir, 'rtl');
click('All questions');
assert.ok(byClass(byId('quiz-all'), 'option-text').every(value => value.lang === 'ar' && value.dir === 'rtl'));
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_accepted_forms_keep_target_language_in_both_views(self):
        self.member(lesson=lesson_text().replace('lang: de', 'lang: ar'), quiz=quiz_text().replace('lang: de', 'lang: ar').replace('"answers": ["wort1"]', '"answers": ["أول", "ثان"]'), relative='family_runs/ar/000020')
        self.run_dom(self.service.render_report('family_runs/ar/000020', 'en', 0), """
const single = byId('quiz-single'); click('Show answer');
const checkForms = root => {
  const forms = byClass(root, 'accepted-forms'); assert.ok(forms.length > 0);
  for (const list of forms) {
    assert.deepEqual(list.children.map(item => item.children[0].textContent), ['أول', 'ثان']);
    list.children.forEach(item => { assert.equal(item.children[0].lang, 'ar'); assert.equal(item.children[0].dir, 'rtl'); });
  }
};
checkForms(single); click('All questions'); click('Show answers'); checkForms(byId('quiz-all'));
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_unlabeled_teaching_links_resolve_titles_without_exposing_ids(self):
        body = ('راجع [[base-lesson#base-page]] و[[base-lesson]]. '
                '[[base-lesson#base-page|عنوان المستخدم]] '
                '[[base-lesson#base-page|base-lesson#base-page]] '
                '[[base-lesson#base-page|`wort1`]] '
                '[[outside-lesson#outside-page]]. [wort1]{.tl}.')
        lesson = lesson_text(locale='ar', body=body).replace('title: Test lesson', 'title: عنوان الدرس').replace('# Page 1 ', '# عنوان الصفحة ')
        quiz = quiz_text(options=4, locale='ar', explanation=body)
        self.member(lesson=lesson, quiz=quiz, locale='ar')
        report = self.service.render_report(self.relative, 'ar', 0)
        self.assertTrue(report['ok'], report['issues'])
        report['parseRichHTML'] = True
        self.run_dom(report, """
const links = root => walk(root).filter(value => value.attrs?.['data-ref']);
const grammar = byClass(main, 'grammar-body')[0], single = byId('quiz-single');
const checkLinks = root => {
  const values = links(root); assert.ok(values.length >= 5);
  const unlabeled = values.filter(value => value.dataset.previewReference === 'unlabeled');
  assert.ok(unlabeled.some(value => value.dataset.ref === 'base-lesson#base-page' && visibleText(value).includes('عنوان الصفحة')));
  assert.ok(unlabeled.some(value => value.dataset.ref === 'base-lesson' && visibleText(value).includes('عنوان الدرس')));
  assert.ok(unlabeled.some(value => value.dataset.ref === 'outside-lesson#outside-page' && visibleText(value).includes('Teaching reference')));
  for (const value of unlabeled) {
    assert.equal(visibleText(value).includes(value.dataset.ref), false);
    assert.equal(value.attrs['role'], 'link'); assert.equal(value.tabIndex, 0);
    assert.ok(value.events.click); assert.ok(value.events.keydown);
    assert.equal(value.lang, value.dataset.ref.startsWith('outside-') ? 'en' : 'ar');
    assert.equal(value.dir, value.lang === 'ar' ? 'rtl' : 'ltr');
    const siblings = value.parent.children, metadata = siblings[siblings.indexOf(value) + 1];
    assert.equal(metadata.textContent, `Reference ID: ${value.dataset.ref}`);
    assert.equal(metadata.hidden, true); assert.equal(metadata.lang, 'en');
  }
  const authored = values.filter(value => !value.dataset.previewReference);
  assert.ok(authored.some(value => visibleText(value).includes('عنوان المستخدم')));
  assert.ok(authored.some(value => visibleText(value).includes('base-lesson#base-page'))); // Explicit labels are never guessed from their text.
  const targetLabel = authored.find(value => visibleText(value).includes('wort1'));
  assert.ok(targetLabel); assert.ok(walk(targetLabel).some(value => value.lang === 'de' && value.className === 'g-tl'));
  return values;
};
checkLinks(grammar);
assert.equal(links(byClass(single, 'quiz-prompt')[0]).length, 0); // QMD disallows pre-answer reference controls.
click('Show answer'); checkLinks(byClass(single, 'quiz-explanation')[0]);
click('All questions');
click('Show answers'); checkLinks(byClass(byId('quiz-all'), 'quiz-explanation')[0]);
click('Show author metadata');
assert.ok(visibleText(byId('quiz-all')).includes('Reference ID: base-lesson#base-page'));
click('Hide author metadata');
assert.equal(visibleText(byId('quiz-all')).includes('Reference ID:'), false);
click('Single question');
const reference = links(byClass(single, 'quiz-explanation')[0]).find(value => value.dataset.previewReference === 'unlabeled' && value.dataset.ref === 'base-lesson#base-page');
let prevented = false; reference.events.keydown({key: 'Enter', preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(byClass(main, 'card grammar')[0].scrolledIntoView, true);
checkLinks(byClass(main, 'grammar-body')[0]);
const outside = links(byClass(single, 'quiz-explanation')[0]).find(value => value.dataset.ref.startsWith('outside-'));
outside.events.click();
assert.ok(byClass(main, 'notices')[0].textContent.includes('outside this preview'));
assert.equal(byClass(main, 'notices')[0].textContent.includes('outside-lesson'), false);
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_generated_callout_chrome_is_english_without_relabeling_arabic(self):
        body = ('> [!recall]- عنوان عربي\n> [wort1]{.tl}.\n\n'
                '> [!rule] قاعدة عربية\n> شرح عربي [wort1]{.tl}.\n\n'
                '> [!note]- ملاحظة المستخدم\n> جسم عربي [wort1]{.tl}.\n\n'
                '> [!tip]-\n> تلميح عربي [wort1]{.tl}.\n\n'
                '~~خطأ~~ ثم {++إضافة++} و{--حذف--} و{~~قديم~>جديد~~}. '
                '[مهم]{.important} و[تشديد]{.stress} و[عنوان موضعي]{tip="incorrect: "}.')
        self.member(lesson=lesson_text(locale='ar', body=body), quiz=quiz_text(locale='ar', explanation=body), locale='ar')
        report = self.service.render_report(self.relative, 'ar', 0)
        self.assertTrue(report['ok'], report['issues'])
        report['parseRichHTML'] = True
        self.run_dom(report, """
const inherited = (element, field) => element?.[field] || (element?.parent && inherited(element.parent, field));
const checkChrome = root => {
  assert.equal(root.lang, 'ar'); assert.equal(root.dir, 'rtl');
  const callouts = walk(root).filter(value => (value.className || '').split(' ').includes('g-co'));
  assert.equal(callouts.length, 4);
  const labels = callouts.map(value => byClass(value, 'g-co-l')[0]);
  assert.ok(visibleText(labels[0]).includes('Check yourself')); assert.equal(labels[0].lang, 'en'); assert.equal(labels[0].dir, 'ltr');
  assert.equal(byClass(callouts[0], 'g-co-h')[0].lang, 'en'); assert.equal(byClass(callouts[0], 'g-co-h')[0].dir, 'ltr');
  assert.ok(visibleText(labels[1]).includes('Key rule')); assert.equal(labels[1].lang, 'en');
  assert.ok(visibleText(labels[2]).includes('ملاحظة المستخدم')); assert.equal(labels[2].lang, undefined);
  assert.equal(inherited(labels[2], 'lang'), 'ar'); assert.equal(inherited(labels[2], 'dir'), 'rtl');
  assert.ok(visibleText(labels[3]).includes('Tip')); assert.equal(labels[3].lang, 'en');
  for (const [index, callout] of callouts.entries()) {
    assert.equal(callout.attrs['aria-label'], undefined);
    assert.equal(callout.attrs['aria-labelledby'], labels[index].id);
    assert.equal(inherited(callout, 'lang'), 'ar');
    byClass(callout, 'g-fold-hint').forEach(hint => { assert.equal(hint.lang, 'en'); assert.equal(hint.dir, 'ltr'); });
  }
  const authoredTitles = byClass(root, 'g-co-t');
  assert.ok(authoredTitles.some(value => visibleText(value).includes('عنوان عربي')));
  assert.ok(authoredTitles.some(value => visibleText(value).includes('قاعدة عربية')));
  authoredTitles.forEach(value => assert.equal(inherited(value, 'lang'), 'ar'));
  const generated = byClass(root, 'sr').filter(value => /^(incorrect: |added: |dropped: |changes | to |Important: | \\(stressed\\))$/.test(value.children.map(child => child.textContent).join('')));
  assert.ok(generated.length >= 7);
  generated.forEach(value => { assert.equal(value.lang, 'en'); assert.equal(value.dir, 'ltr'); });
  const tip = walk(root).find(value => value.dataset?.tip === 'incorrect: ');
  assert.ok(tip); assert.equal(inherited(tip, 'lang'), 'ar'); // User-authored tooltip is not renderer chrome.
  const target = walk(root).find(value => value.lang === 'de'); assert.ok(target); assert.equal(target.dir, 'ltr');
  return callouts;
};
let root = byClass(main, 'grammar-body')[0];
const callouts = checkChrome(root), recall = byClass(callouts[0], 'g-co-h')[0];
assert.equal(recall.attrs['aria-expanded'], 'false'); recall.events.click();
root = byClass(main, 'grammar-body')[0];
const opened = checkChrome(root);
assert.equal(byClass(opened[0], 'g-co-h')[0].attrs['aria-expanded'], 'true');
assert.ok(visibleText(root).includes('wort1'));
click('Show answer'); checkChrome(byClass(byId('quiz-single'), 'quiz-explanation')[0]);
click('All questions'); click('Show answers');
checkChrome(byClass(byId('quiz-all'), 'quiz-explanation')[0]);
process.stdout.write(JSON.stringify({ok: true}));
""")

    def test_single_artifact_preview_paths_rejected(self):
        for relative in ('runs/de/000020', 'packs/de/000020', 'packs/de/000020-return-1'):
            with self.subTest(relative=relative), self.assertRaises(PreviewError):
                self.service.render_report(relative, 'en', 0)


if __name__ == '__main__':
    unittest.main()
