const fs = require('node:fs');
const path = require('node:path');
const {virtualFamily} = require('./content-contract.cjs');
const {parseLesson: parseTeachingLesson} = require('./lesson-policy.cjs');
const GMD = require('../reference/formats/kit/gmd.js');
const QMD = require('./quiz-policy.cjs');
const ROOT = path.resolve(__dirname, '..');
const MAX_BYTES = 2 * 1024 * 1024;
const LOCALES = ['en', 'ar', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh'];
GMD.useConventions(JSON.parse(fs.readFileSync(path.join(ROOT, 'reference/formats/grammar/conventions.json'), 'utf8')));

function renderPreview(input) {
  const issues = [], records = [], members = [], ids = new Set();
  const stage = input.stage ?? 0;
  if (!input || typeof input !== 'object' || !Number.isInteger(stage) || stage < 0 || stage > 4 || !LOCALES.includes(input.language) || !LOCALES.includes(input.locale)) throw new Error('Invalid preview identity');
  if (Object.hasOwn(input, 'legacy')) throw new Error('Unsupported preview identity field');
  if (input.language === input.locale) throw new Error('Study and learner language must differ');
  const prior = input.prior_lessons ?? [];
  if (!Array.isArray(prior) || prior.length !== stage) throw new Error('Preview requires each earlier family stage, in order');
  const issue = (level, source, msg, line = 1) => issues.push({level, source, line, msg});
  const values = [...prior, {stage, lesson_md: input.lesson_md, mode: input.mode}];
  for (const [index, value] of values.entries()) {
    if (!value || value.stage !== index || typeof value.lesson_md !== 'string') { issue('error', `stage-${index}/lesson.md`, 'Missing lesson.md or invalid earlier stage identity'); break; }
    const source = `stage-${index}/lesson.md`;
    const mode = index ? value.mode : 'base';
    if (index && !['practice', 'extend'].includes(mode)) { issue('error', source, 'Return requires brief.extension.mode practice or extend'); break; }
    if (!value.lesson_md.trim()) {
      if (mode !== 'practice') { issue('error', source, 'Empty lesson is allowed only for a known practice return'); break; }
      records.push({stage: index, lesson: null});
      members.push({stage: index, mode, parsed: null, semantic: null});
      continue;
    }
    if (mode === 'practice') { issue('error', source, 'Practice return lesson.md must be empty'); break; }
    const parsed = parseTeachingLesson(GMD, value.lesson_md);
    issues.push(...parsed.issues.map(entry => ({...entry, source})));
    if (!parsed.ok) break;
    const semantic = GMD.toJSON(parsed);
    if (semantic.meta.lang !== input.language || semantic.meta.explained_in !== input.locale) { issue('error', source, 'Lesson language identity mismatch'); break; }
    if (semantic.pages.length > (index ? 2 : 5)) { issue('error', source, 'Extension requires 1–2 pages; base requires 1–5 pages'); break; }
    if (ids.has(semantic.id)) { issue('error', source, `Duplicate family lesson ID ${semantic.id}`); break; }
    ids.add(semantic.id);
    const needed = new Set([...Object.keys(semantic.roles), ...Object.values(parsed.roles).filter(role => role.source === 'lesson').map(role => role.name)]);
    for (const name of needed) if (parsed.roles[name]?.same_as) needed.add(parsed.roles[name].same_as);
    semantic.roles = Object.fromEntries([...needed].sort().map(name => {
      const role = parsed.roles[name];
      return [name, {color: role.color, line: role.line, label: role.label, desc: role.desc, group: role.group, same_as: role.same_as}];
    }));
    records.push({stage: index, lesson: semantic});
    members.push({stage: index, mode, parsed, semantic});
  }
  let family = null, bank = null, rich = [];
  if (records.length === stage + 1) {
    try { family = virtualFamily(records); }
    catch (error) { issue('error', 'family', error.message); }
  }
  if (typeof input.quiz_md !== 'string' || !input.quiz_md.trim()) issue('error', `stage-${stage}/quiz.md`, 'Quiz not written yet: missing or empty quiz.md');
  else if (family) {
    const {parsed, rich: sections} = QMD.parseRich(input.quiz_md, {lessons: [family]});
    issues.push(...parsed.issues.map(entry => ({...entry, source: `stage-${stage}/quiz.md`})));
    if (parsed.ok) {
      const candidate = QMD.toJSON(parsed);
      if (candidate.questions.length > 50) issue('error', `stage-${stage}/quiz.md`, 'Family bank must contain 25–50 questions');
      else if (candidate.questions.some(question => question.type === 'choice' && question.options.length > 10)) issue('error', `stage-${stage}/quiz.md`, 'Choice pools must contain 4–10 options');
      else {
        const policies = Object.hasOwn(input, 'option_text_policies') ? input.option_text_policies : {};
        if (!policies || typeof policies !== 'object' || Array.isArray(policies) || Object.entries(policies).some(([id, policy]) => {
          const question = candidate.questions.find(value => value.id === id);
          return !question || question.type !== 'choice' || !['target', 'learner'].includes(policy) || (policy === 'learner' && question.sentence.some(node => node.t === 'blank'));
        })) issue('error', 'quiz-policy', 'Invalid option_text_policies map');
        else {
          bank = candidate;
          rich = sections.map(section => ({...section, option_text_policy: policies[section.id] || 'target', option_language: policies[section.id] === 'learner' ? input.locale : input.language}));
        }
      }
    }
  }
  const pageMap = members.flatMap(member => (member.semantic?.pages || []).map((page, index) => ({stage: member.stage, lesson_id: member.semantic.id, id: page.id, index, title: GMD.plain(member.parsed.pages[index].title), html: GMD.renderBlocks(member.parsed, index)})));
  const available = new Set(pageMap.flatMap(page => [page.lesson_id, `${page.lesson_id}#${page.id}`]));
  function references(value) {
    if (!value || typeof value !== 'object') return;
    if (value.t === 'link' && !available.has(value.ref)) issue('warn', 'reference', `Linked teaching ${value.ref} is outside this preview; no external navigation is enabled`);
    for (const child of Object.values(value)) if (child && typeof child === 'object') references(child);
  }
  members.forEach(member => references(member.semantic?.pages));
  if (bank) references(bank.questions);
  return {ok: !issues.some(entry => entry.level === 'error'), scope: 'Reference preview; no native/audio verification', language: input.language, locale: input.locale, stage, mode: input.mode || 'base', issues, members, page_map: pageMap, bank, rich};
}

async function main() {
  let bytes = 0;
  const chunks = [];
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > MAX_BYTES) throw new Error('Preview input exceeds 2 MiB');
    chunks.push(chunk);
  }
  const report = renderPreview(JSON.parse(Buffer.concat(chunks).toString('utf8')));
  const output = JSON.stringify(report);
  if (Buffer.byteLength(output) > 8 * 1024 * 1024) throw new Error('Preview output exceeds 8 MiB');
  process.stdout.write(output + '\n');
}
if (require.main === module) main().catch(error => { process.stdout.write(JSON.stringify({ok: false, issues: [{level: 'error', source: 'preview', line: 1, msg: error.message}]})); process.exitCode = 1; });
module.exports = {renderPreview};
