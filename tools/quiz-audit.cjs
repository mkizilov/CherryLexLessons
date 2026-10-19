#!/usr/bin/env node
'use strict';
// Staged reviewer aid. No writes, network, lesson loading, or grading/draw mutations.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {TextDecoder} = require('node:util');

const DEFAULT_SEED = 'review-v1';
const MAX_INPUTS = 25;
const MAX_OUTPUT_BYTES = 16 * 1024 * 1024;
const SCOPE = 'Project QMD 0.2 canonical parse with shared conventions, WITHOUT a linked lesson. Not full family, teaching, vocabulary, approval, localization, prose/linguistic, or publication validation.';
const HELP = `Usage (from project root): node tools/quiz-audit.cjs --input family_packs/.../quiz.md [--input family_runs/.../quiz.md ...] [--mode summary|blind] [--question ID] [--seed SEED]
1..25 distinct root-relative .md files under family_runs/ or family_packs/ only; no traversal, symlinks, or stdin.
Default mode: summary. --question: optional single exact question ID, blind mode only.
Default display seed: review-v1; 1..128 printable ASCII characters. Deterministic, NOT cryptographic or blinding security.
JSON on stdout; errors are bounded JSON with nonzero exit. No files are written.
Summary exposes keys; NEVER give it to a blind solver before commitment. Blind emits only neutral tokens, learner-visible content and the shuffled FULL pool, not a four-option app draw.
${SCOPE}\n`;
const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
const stable = value => JSON.stringify(value);
const sorted = values => values.slice().sort();
class AuditError extends Error {
  constructor(code, details = {}) { super(code); this.code = code; this.details = details; }
}
function fail(code, details) { throw new AuditError(code, details); }

function argumentsFor(argv) {
  if (argv.length === 1 && argv[0] === '--help') return {help: true};
  const options = {inputs: [], mode: 'summary', seed: DEFAULT_SEED};
  const once = new Set();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index], value = argv[index + 1];
    if (!['--input', '--mode', '--question', '--seed'].includes(flag) || value === undefined || value.startsWith('--')) fail('invalid_arguments');
    if (flag !== '--input') {
      if (once.has(flag)) fail('duplicate_argument');
      once.add(flag);
    }
    if (flag === '--input') {
      options.inputs.push(value);
      if (options.inputs.length > MAX_INPUTS) fail('input_count');
    } else options[flag.slice(2)] = value;
  }
  if (!options.inputs.length) fail('input_count');
  if (!['summary', 'blind'].includes(options.mode)) fail('invalid_mode');
  if (options.question !== undefined && (options.mode !== 'blind' || options.question.length > 200 || /[^a-z0-9-]/.test(options.question) || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(options.question))) fail('invalid_question_filter');
  if (options.seed.length < 1 || options.seed.length > 128 || /[^\x20-\x7e]/.test(options.seed)) fail('invalid_seed');
  if (new Set(options.inputs).size !== options.inputs.length) fail('duplicate_input');
  return options;
}

function allowedParts(input) {
  // Do not normalize first: even an otherwise harmless traversal spelling is rejected.
  if (typeof input !== 'string' || input.length > 1024 || !input.endsWith('.md')) fail('invalid_input_path');
  const parts = input.split('/');
  if (parts.length < 2 || !['family_runs', 'family_packs'].includes(parts[0]) || parts.some(part => !/^[A-Za-z0-9]/.test(part) || /[^A-Za-z0-9._-]/.test(part))) fail('invalid_input_path');
  return parts;
}
function readBounded(root, input, maxBytes) {
  const parts = allowedParts(input);
  const rootReal = fs.realpathSync(root);
  let filename = rootReal;
  let before;
  try {
    for (let index = 0; index < parts.length; index++) {
      filename = path.join(filename, parts[index]);
      const stat = fs.lstatSync(filename);
      if (stat.isSymbolicLink()) fail('symlink_input');
      if (index < parts.length - 1 ? !stat.isDirectory() : !stat.isFile()) fail('not_regular_input');
      before = stat;
    }
    if (before.size > maxBytes) fail('input_too_large');
    // The final component cannot be followed even if it changes after lstat.
    const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    try {
      const opened = fs.fstatSync(fd);
      if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino || opened.size > maxBytes) fail('input_changed');
      const buffer = Buffer.alloc(maxBytes + 1);
      let length = 0;
      while (length < buffer.length) {
        const count = fs.readSync(fd, buffer, length, buffer.length - length, null);
        if (!count) break;
        length += count;
      }
      if (length > maxBytes) fail('input_too_large');
      const after = fs.fstatSync(fd);
      // Recheck ancestors/real path too; not a sandbox against a hostile concurrent filesystem owner.
      let checked = rootReal;
      for (const part of parts) {
        checked = path.join(checked, part);
        if (fs.lstatSync(checked).isSymbolicLink()) fail('input_changed');
      }
      const current = fs.lstatSync(filename);
      if (fs.realpathSync(filename) !== filename || current.dev !== opened.dev || current.ino !== opened.ino || after.size !== length || opened.size !== after.size || opened.mtimeMs !== after.mtimeMs || opened.ctimeMs !== after.ctimeMs) fail('input_changed');
      const bytes = buffer.subarray(0, length);
      let text;
      try { text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(bytes); }
      catch { fail('invalid_utf8'); }
      return {text, sha256: sha256(bytes)};
    } finally { fs.closeSync(fd); }
  } catch (error) {
    if (error instanceof AuditError) throw error;
    fail('input_unavailable'); // Never echo native errors, absolute paths, or arbitrary file bytes.
  }
}

function projectParser(root) {
  const GMD = require(path.join(root, 'reference/formats/kit/gmd.js'));
  const QMD = require(path.join(root, 'tools/quiz-policy.cjs'));
  GMD.useConventions(JSON.parse(fs.readFileSync(path.join(root, 'reference/formats/grammar/conventions.json'), 'utf8')));
  return {GMD, QMD};
}
function readCanonicalProjectQMD(root, input, QMD) {
  const source = readBounded(root, input, QMD.LIMITS.source * 4);
  const parsed = QMD.parse(source.text); // Intentionally no lessons, not a family validation substitute.
  if (!parsed.ok) fail('qmd_parse_failed', {input, sha256: source.sha256, issue_count: parsed.issues.length});
  return {input, sha256: source.sha256, bank: QMD.toJSON(parsed)};
}

// Explicit display allowlists: never spread author objects into a blind packet.
// Keep visible formatting, ruby readings and graph wiring; strip diagnostic roles/IDs,
// image-generation prompts, explanations, audio and source metadata.
function inline(nodes) {
  return nodes.flatMap(node => {
    if (node.t === 'text') return [{t: 'text', v: node.v}];
    if (node.t === 'blank' || node.t === 'br') return [{t: node.t}];
    if (node.t === 'ruby') return [{t: 'ruby', base: node.base, rt: node.rt.slice()}];
    if (node.t === 'span') return [{t: 'span', ...(node.styles?.length ? {styles: node.styles.slice()} : {}), ...(node.lang ? {lang: node.lang} : {}), kids: inline(node.kids)}];
    if (node.t === 'chg') return [{t: 'chg', from: inline(node.from), kids: inline(node.kids)}];
    if (['code', 'strong', 'em', 'del', 'mark', 'sub', 'sup', 'ins', 'rem'].includes(node.t)) return [{t: node.t, ...(node.color ? {color: node.color} : {}), kids: inline(node.kids)}];
    fail('unsupported_display_content');
  });
}
function listItems(items) {
  return items.map(item => ({...(item.who ? {speaker: item.who} : {}), text: inline(item.text), lines: item.lines.map(line => ({kind: line.kind, text: inline(line.text)})), lists: item.lists.map(list => ({ordered: list.ordered, start: list.start, items: listItems(list.items)}))}));
}
function blocks(values, GMD) {
  return values.map(block => {
    const type = block.type;
    if (['p', 'h2', 'h3'].includes(type)) return {type, text: inline(block.text)};
    if (type === 'hr') return {type};
    if (type === 'deflist') return {type, items: block.items.map(item => ({term: inline(item.term), defs: item.defs.map(inline)}))};
    if (type === 'list') return {type, ordered: block.ordered, start: block.start, items: listItems(block.items)};
    if (type === 'table') return {type, align: block.align.slice(), head: block.head.map(inline), rows: block.rows.map(row => row.map(cell => ({text: inline(cell.text)}))), caption: block.caption ? inline(block.caption) : null};
    if (type === 'callout') {
      const title = block.title ? inline(block.title) : [{t: 'text', v: block.custom || GMD.TYPES[block.kind]?.label || ''}];
      if (block.columns || block.tabs) return {type, title, layout: block.tabs ? 'tabs' : 'columns', sections: (block.tabs || block.columns).map(column => ({head: inline(column.head), blocks: blocks(column.blocks, GMD)}))};
      return {type, title, blocks: blocks(block.blocks, GMD)};
    }
    if (type === 'pattern') return {type, parts: block.parts.map(inline)};
    if (type === 'morph') return {type, parts: block.parts.map(inline), labels: block.labels.map(inline), steps: block.steps.map(step => ({form: inline(step.form), note: step.note ? inline(step.note) : null}))};
    if (type === 'gloss') return {type, rows: block.rows.map(row => row.map(inline)), translation: block.translation ? inline(block.translation) : null};
    if (type === 'timeline') return {type, items: block.items.map(item => ({kind: item.kind, ...(item.at === undefined ? {} : {at: item.at}), ...(item.from === undefined ? {} : {from: item.from, to: item.to}), label: inline(item.label)}))};
    if (type === 'image') return {type, alt: block.alt, ratio: block.ratio, caption: block.caption ? inline(block.caption) : null, media: 'unavailable'};
    if (type === 'mermaid') {
      const identifiers = new Map(block.nodes.map((node, index) => [node.id, index + 1]));
      const edges = block.edges.map(edge => {
        if (!identifiers.has(edge.from) || !identifiers.has(edge.to)) fail('unsupported_display_content');
        return {from: identifiers.get(edge.from), to: identifiers.get(edge.to), label: edge.label};
      });
      return {type, title: block.title, description: block.description, nodes: block.nodes.map((node, index) => ({number: index + 1, label: node.label.slice(), shape: node.shape, rank: node.rank})), edges};
    }
    fail('unsupported_display_content');
  });
}
function visibleQuestion(question, GMD) {
  return {type: question.type, prompt: blocks(question.prompt, GMD), sentence: inline(question.sentence)};
}
function responseContent(question) {
  return question.type === 'choice'
    ? {options: question.options.map(option => ({text: option.text, correct: option.correct})).sort((a, b) => stable(a) < stable(b) ? -1 : stable(a) > stable(b) ? 1 : 0)}
    : {case: question.case, accepted_forms: sorted(question.answers)};
}
function locator(record, question) {
  return {input: record.input, sha256: record.sha256, question_id: question.id};
}
function groupsFor(rows, contentFor) {
  const groups = new Map();
  for (const row of rows) {
    const content = contentFor(row);
    const signature = stable(content);
    if (!groups.has(signature)) groups.set(signature, {signature_sha256: sha256(signature), content, occurrences: []});
    groups.get(signature).occurrences.push(row.location);
  }
  return [...groups.values()].filter(group => group.occurrences.length > 1);
}
function summary(records, GMD) {
  const rows = records.flatMap(record => record.bank.questions.map(question => ({question, location: locator(record, question), languages: [record.bank.lang, record.bank.explained_in], visible: visibleQuestion(question, GMD)})));
  return {
    ok: true, mode: 'summary', scope: SCOPE,
    interpretation: 'Advisory comparison flags only, never rejection, thresholds, quality scores, coverage/mastery estimates or linguistic verdicts. Repetition can be purposeful reinforcement; a long key can be necessary.',
    definitions: {
      exact_visible_task_groups: 'Same canonical display projection of prompt/sentence, type, language pair and keyed response content. Ignore question/option IDs, option/accepted-form order, skill/page, explanations and why. No paraphrase or mechanism inference. Role/color cues and source annotations are not compared.',
      blank_frame_accepted_form_groups: 'Same canonical sentence frame, language pair, case policy and exact accepted strings, ignoring prompt. Does not mean the required contextual decision is the same.',
      option_keyset_groups: 'Same full option texts and correct flags in a language pair, ignoring IDs, order, prompt and sentence. Does not establish duplicate tasks.',
      correct_longest: 'Unicode code-point counts of stored option text, not words, display width, normalized grading forms, difficulty, or evidence a learner used a shortcut. Compare only within each complete pool.'
    },
    banks: records.map(record => {
      const questions = record.bank.questions, choice = questions.filter(question => question.type === 'choice');
      const distribution = {};
      const longest = choice.map(question => {
        const lengths = question.options.map(option => Array.from(option.text).length);
        const keyLength = lengths[question.options.findIndex(option => option.correct)];
        const maxLength = Math.max(...lengths);
        const result = keyLength < maxLength ? 'not_longest' : lengths.filter(length => length === maxLength).length === 1 ? 'uniquely_longest' : 'tied_longest';
        distribution[question.options.length] = (distribution[question.options.length] || 0) + 1;
        return {...locator(record, question), result, key_code_points: keyLength, max_code_points: maxLength};
      });
      return {input: record.input, sha256: record.sha256, bank_id: record.bank.id, lang: record.bank.lang, explained_in: record.bank.explained_in, question_ids: questions.map(question => question.id),
        counts: {questions: questions.length, choice: choice.length, blank: questions.length - choice.length}, pool_distribution: distribution,
        correct_longest: {choice_questions: choice.length, uniquely_longest: longest.filter(row => row.result === 'uniquely_longest').length, tied_longest: longest.filter(row => row.result === 'tied_longest').length, not_longest: longest.filter(row => row.result === 'not_longest').length, observations: longest}};
    }),
    advisory_flags: {
      exact_visible_task_groups: groupsFor(rows, row => ({languages: row.languages, ...row.visible, response: responseContent(row.question)})),
      blank_frame_accepted_form_groups: groupsFor(rows.filter(row => row.question.type === 'blank'), row => ({languages: row.languages, sentence: row.visible.sentence, response: responseContent(row.question)})),
      option_keyset_groups: groupsFor(rows.filter(row => row.question.type === 'choice'), row => ({languages: row.languages, response: responseContent(row.question)}))
    }
  };
}

function displayShuffle(options, seed, index) {
  // Separate display-only RNG. Does NOT call QMD.instantiate, draw distractors, grade,
  // change authored arrays, or supply an attempt snapshot to another consumer.
  let state = 2166136261;
  for (const character of `${seed}\0${index}`) state = Math.imul(state ^ character.codePointAt(0), 16777619) >>> 0;
  const random = () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = Math.imul(state ^ state >>> 15, state | 1);
    value ^= value + Math.imul(value ^ value >>> 7, value | 61);
    return ((value ^ value >>> 14) >>> 0) / 4294967296;
  };
  const shuffled = options.map(option => option.text);
  for (let index = shuffled.length - 1; index > 0; index--) {
    const other = Math.floor(random() * (index + 1));
    [shuffled[index], shuffled[other]] = [shuffled[other], shuffled[index]];
  }
  return shuffled.map((text, index) => ({label: index + 1, text}));
}
function blind(records, options, GMD) {
  const questions = records.flatMap(record => record.bank.questions).filter(question => !options.question || question.id === options.question);
  if (!questions.length) fail('question_not_found');
  return {ok: true, mode: 'blind', scope: SCOPE,
    display: 'Neutral tokens in selected input/authored question order; full option pools shuffled separately for inspection. Source role/ID annotations are withheld; text, ruby and supported structure remain. Images are placeholders, not rendered media. This is not an app attempt, secure anonymization or proof of independent solving.',
    questions: questions.map((question, index) => ({token: `Q${index + 1}`, ...visibleQuestion(question, GMD), ...(question.type === 'choice' ? {options: displayShuffle(question.options, options.seed, index)} : {})}))};
}
function run(argv, root = process.cwd()) {
  const options = argumentsFor(argv);
  if (options.help) return HELP;
  // Validate ALL path spellings/counts before reading files or loading project tools.
  options.inputs.forEach(allowedParts);
  const {QMD, GMD} = projectParser(root);
  const records = options.inputs.map(input => readCanonicalProjectQMD(root, input, QMD));
  return options.mode === 'blind' ? blind(records, options, GMD) : summary(records, GMD);
}
function main() {
  try {
    const result = run(process.argv.slice(2));
    const output = typeof result === 'string' ? result : JSON.stringify(result, null, 2) + '\n';
    if (Buffer.byteLength(output) > MAX_OUTPUT_BYTES) fail('output_too_large');
    process.stdout.write(output);
  } catch (error) {
    process.stdout.write(JSON.stringify({ok: false, error: error instanceof AuditError ? error.code : 'audit_failed', ...(error instanceof AuditError ? error.details : {}), scope: SCOPE}) + '\n');
    process.exitCode = 1;
  }
}
if (require.main === module) main();
module.exports = {run, readCanonicalProjectQMD};
