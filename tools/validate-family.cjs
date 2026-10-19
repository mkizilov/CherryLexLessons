const fs = require('node:fs');
const path = require('node:path');
const {parseLesson: parseTeachingLesson} = require('./lesson-policy.cjs');
const {same, identity, virtualFamily, contractFor} = require('./content-contract.cjs');
const OFFSETS = [25, 75, 125, 225];
const LOCALES = ['en', 'ar', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh'];
const order = (left, right) => left.checkpoint - right.checkpoint || left.base_checkpoint - right.base_checkpoint || left.stage - right.stage;
const wordRange = event => event.stage ? [event.base_checkpoint + (event.stage > 1 ? OFFSETS[event.stage - 2] : 0) + 1, event.checkpoint] : [event.base_checkpoint - (event.base_checkpoint % 20 ? 9 : 19), event.checkpoint];
function safeRead(root, relative) {
  if (typeof relative !== 'string' || path.isAbsolute(relative) || relative.includes('\\') || relative.split('/').some(part => !part || part === '.' || part === '..')) throw new Error(`Unsafe content path ${relative}`);
  let filename = root;
  for (const part of relative.split('/')) {
    filename = path.join(filename, part);
    if (fs.lstatSync(filename).isSymbolicLink()) throw new Error(`Content path cannot be a symlink: ${relative}`);
  }
  if (!fs.statSync(filename).isFile()) throw new Error(`Missing content file ${relative}`);
  return fs.readFileSync(filename, 'utf8');
}
function checkIdentity(event, base) {
  if (!Number.isSafeInteger(event.base_checkpoint) || event.base_checkpoint < 10 || event.base_checkpoint > 999999 || event.base_checkpoint % 10 || !Number.isInteger(event.stage) || event.stage < 0 || event.stage > 4 || event.checkpoint !== event.base_checkpoint + (event.stage ? OFFSETS[event.stage - 1] : 0) || (base !== undefined && event.base_checkpoint !== base)) throw new Error('Invalid family member identity');
}
function parser(root, study, locale) {
  const GMD = require(path.join(root, 'reference/formats/kit/gmd.js'));
  const QMD = require('./quiz-policy.cjs');
  GMD.useConventions(JSON.parse(safeRead(root, 'reference/formats/grammar/conventions.json')));
  const lessons = new Map(), reserved = new Map();
  function requireParsed(parsed, label) {
    if (!parsed.ok) throw new Error(`${label}: ${parsed.issues.filter(issue => issue.level === 'error').map(issue => issue.msg).join('; ')}`);
  }
  function reserve(text, origin) {
    if (!text.trim()) return null;
    const header = text.replace(/\r\n?/g, '\n').match(/^---[ \t]*\n[\s\S]*?\n---[ \t]*(?:\n|$)/)?.[0];
    const id = header && GMD.parse(header).meta.id;
    if (typeof id !== 'string' || !id || (reserved.has(id) && reserved.get(id) !== origin)) throw new Error('Invalid or duplicate reserved lesson identity');
    reserved.set(id, origin);
    return id;
  }
  function references(value) {
    if (!value || typeof value !== 'object') return;
    if (value.t === 'link') {
      const [id, page] = value.ref.split('#');
      const linked = lessons.get(id);
      if (!linked || (page && !linked.pages.some(item => item.id === page))) throw new Error(`Unresolved lesson reference ${value.ref}`);
    }
    for (const child of Object.values(value)) if (child && typeof child === 'object') references(child);
  }
  function parseMember(text, quiz, event, brief, previous, origin) {
    if (event.stage && (!brief.extension || !['practice', 'extend'].includes(brief.extension.mode) || typeof brief.extension.rationale !== 'string' || !brief.extension.rationale.trim())) throw new Error('Return requires extension mode practice/extend and a nonempty rationale');
    let lesson = null, issues = [];
    if (event.stage && brief.extension.mode === 'practice') {
      if (text.trim()) throw new Error('Practice return lesson.md must be empty/whitespace');
    } else {
      if (!text.trim()) throw new Error('Empty lesson is allowed only for a known practice return');
      const parsed = parseTeachingLesson(GMD, text);
      requireParsed(parsed, 'Invalid lesson');
      lesson = GMD.toJSON(parsed); issues = parsed.issues;
      if (lesson.meta.lang !== study || lesson.meta.explained_in !== locale) throw new Error('Lesson language identity mismatch');
      if (!lesson.pages.length || lesson.pages.length > (event.stage ? 2 : 5)) throw new Error(event.stage ? 'Extension must have 1..2 pages' : 'Lesson must have 1..5 pages');
      if (lessons.has(lesson.id) || (reserved.has(lesson.id) && reserved.get(lesson.id) !== origin)) throw new Error('Lesson ID already used in history or family');
      const needed = new Set([...Object.keys(lesson.roles), ...Object.values(parsed.roles).filter(role => role.source === 'lesson').map(role => role.name)]);
      for (const name of needed) if (parsed.roles[name]?.same_as) needed.add(parsed.roles[name].same_as);
      lesson.roles = Object.fromEntries([...needed].sort().map(name => {
        const role = parsed.roles[name];
        return [name, {color: role.color, line: role.line, label: role.label, desc: role.desc, group: role.group, same_as: role.same_as}];
      }));
    }
    const record = {...identity(event), path: origin, lesson};
    const records = [...previous, record], family = virtualFamily(records);
    if (lesson) { lessons.set(lesson.id, lesson); references(lesson.pages); }
    const parsed = QMD.parse(quiz, {lessons: [family]});
    requireParsed(parsed, 'Invalid quiz');
    const bank = QMD.toJSON(parsed);
    if (bank.lang !== study || bank.explained_in !== locale) throw new Error('Quiz language identity mismatch');
    if (bank.lesson !== family.id) throw new Error('Quiz lesson must link the base lesson ID');
    if (bank.questions.length < 25 || bank.questions.length > 50) throw new Error('Quiz bank must have 25..50 questions');
    if (previous.some(item => item.bank.id === bank.id)) throw new Error('Quiz bank ID already used in family');
    const ids = new Set(previous.flatMap(item => item.bank.questions.map(question => question.id)));
    for (const question of bank.questions) {
      if (ids.has(question.id)) throw new Error('Question ID already used in family');
      ids.add(question.id);
      if (question.type === 'choice' && (question.options.length < 4 || question.options.length > 10 || question.options.filter(option => option.correct).length !== 1)) throw new Error(`Choice ${question.id} requires 4..10 options and exactly one correct`);
    }
    references(bank.questions);
    record.bank = bank;
    return {record, records, family, issues};
  }
  return {reserve, parseMember};
}
function main() {
  const args = {};
  for (let index = 2; index < process.argv.length; index += 2) {
    const key = process.argv[index];
    if (!['--root', '--run', '--locale', '--candidate'].includes(key) || !process.argv[index + 1] || Object.hasOwn(args, key.slice(2))) throw new Error('Invalid validator arguments');
    args[key.slice(2)] = process.argv[index + 1];
  }
  if (!args.root || !args.run || !args.locale) throw new Error('Required arguments: --root --run --locale');
  const root = path.resolve(args.root), match = args.run.match(/^family_runs\/([a-z]{2})\/(\d{6})$/);
  if (!match) throw new Error('Invalid family run path');
  const study = match[1], base = Number(match[2]);
  const source = study === 'en' ? 'es' : 'en';
  const context = JSON.parse(safeRead(root, `${args.run}/family-context.json`));
  if (!LOCALES.includes(study) || !LOCALES.includes(args.locale) || args.locale === study || context.schema !== 5 || context.study_language !== study || context.base_checkpoint !== base || context.source_locale !== source || !same(context.locales, LOCALES.filter(code => code !== study)) || !same(context.return_offsets, OFFSETS) || context.family_path !== `family_packs/${study}/${match[2]}` || context.vocabulary_horizon !== base + 225 || !Array.isArray(context.words) || context.words.length !== base + 225 || !Array.isArray(context.stages) || context.stages.length !== 5 || !Array.isArray(context.external_history)) throw new Error('Complete canonical schema-5 family context required');
  const blockStart = Math.floor((base - 1) / 200) * 200;
  const wave = base % 20 ? 2 : 1;
  if (!same(context.authoring, {block: blockStart + 200, wave, order: blockStart / 10 + Math.floor((base - blockStart + (wave === 2 ? 10 : 0)) / 20) + (wave === 2 ? 10 : 0)})) throw new Error('Invalid family authoring metadata');
  const history = context.external_history, keys = new Set();
  for (let index = 0; index < history.length; index++) {
    const item = history[index];
    checkIdentity(item);
    const familyPath = `family_packs/${study}/${String(item.base_checkpoint).padStart(6, '0')}`;
    if (item.base_checkpoint === base || keys.has(item.key) || item.path !== familyPath || item.key !== `${familyPath}#stage:${item.stage}` || (index && order(history[index - 1], item) >= 0) || !item.manifest || typeof item.lesson !== 'string' || typeof item.quiz !== 'string') throw new Error('Invalid or duplicate external family history identity');
    keys.add(item.key);
    const manifest = JSON.parse(safeRead(root, `${item.path}/manifest.json`));
    if (manifest.schema !== 5 || manifest.study_language !== study || manifest.base_checkpoint !== item.base_checkpoint || manifest.source_locale !== source || !same(manifest.locales, context.locales) || !same(manifest.return_offsets, OFFSETS) || manifest.family_path !== familyPath) throw new Error('Invalid external family manifest identity');
    if (item.source_locale !== source) throw new Error('Invalid external source identity');
    const prefix = code => `${item.path}/${code}/stage-${item.stage}/`;
    const sourceLesson = safeRead(root, `${prefix(source)}lesson.md`), sourceQuiz = safeRead(root, `${prefix(source)}quiz.md`);
    if (sourceLesson.replace(/\r\n?/g, '\n') !== item.lesson.replace(/\r\n?/g, '\n') || sourceQuiz.replace(/\r\n?/g, '\n') !== item.quiz.replace(/\r\n?/g, '\n')) throw new Error('External source history differs from actual files');
    item.localizedLesson = safeRead(root, `${prefix(args.locale)}lesson.md`);
    item.localizedQuiz = safeRead(root, `${prefix(args.locale)}quiz.md`);
    const actualManifest = JSON.parse(safeRead(root, `${item.path}/stage-${item.stage}/brief.json`));
    if (!same(actualManifest, item.manifest)) throw new Error('External manifest differs from actual file');
  }
  for (const item of history) {
    const members = history.filter(other => other.base_checkpoint === item.base_checkpoint);
    if (members.some((member, index) => member.stage !== index) || members.length !== 5) throw new Error('Missing or inconsistent external family history');
  }
  for (let stage = 0; stage < 5; stage++) {
    const event = context.stages[stage];
    checkIdentity(event, base);
    if (event.stage !== stage || !same(event.new_word_range, wordRange(event)) || !same(event.prior_external_keys, history.filter(item => order(item, event) < 0).map(item => item.key)) || !same(event.planning_external_keys, history.filter(item => order(item, event) >= 0).map(item => item.key)) || !same(event.prior_family_stages, Array.from({length: stage}, (_, previous) => `stage:${previous}`))) throw new Error('Invalid stage word bounds or history partition');
  }
  let candidate;
  if (args.candidate) {
    candidate = JSON.parse(fs.readFileSync(args.candidate, 'utf8'));
    if (!candidate || !same(Object.keys(candidate).sort(), ['stages']) || !Array.isArray(candidate.stages) || candidate.stages.length !== 5 || candidate.stages.some((item, stage) => !item || !same(Object.keys(item).sort(), ['adaptation_notes', 'lesson_md', 'quiz_md', 'stage']) || item.stage !== stage || ['lesson_md', 'quiz_md'].some(key => typeof item[key] !== 'string') || !Array.isArray(item.adaptation_notes) || item.adaptation_notes.some(note => typeof note !== 'string'))) throw new Error('Candidate must contain exactly five ordered complete stages');
  }
  const texts = context.stages.map((event, stage) => candidate?.stages[stage] || {stage, lesson_md: safeRead(root, `${args.run}/${args.locale}/stage-${stage}/lesson.md`), quiz_md: safeRead(root, `${args.run}/${args.locale}/stage-${stage}/quiz.md`)});
  const briefs = context.stages.map((event, stage) => JSON.parse(safeRead(root, `${args.run}/stage-${stage}/brief.json`)));
  const stages = [];
  let questions = 0, pages = 0;
  for (let stage = 0; stage < 5; stage++) {
    const current = context.stages[stage], reader = parser(root, study, args.locale), families = new Map(), parsedHistory = [];
    for (const item of history) {
      if (reader.reserve(item.lesson, item.key) !== reader.reserve(item.localizedLesson, item.key)) throw new Error('External localized lesson identity differs from source');
    }
    for (const item of texts) reader.reserve(item.lesson_md, `${args.run}#stage:${item.stage}`);
    const events = [...history.filter(item => order(item, current) < 0).map(item => ({...item, external: true})), ...context.stages.slice(0, stage + 1).map((item, index) => ({...item, key: `${args.run}#stage:${index}`, lesson: texts[index].lesson_md, quiz: texts[index].quiz_md, manifest: briefs[index]}))].sort(order);
    let output;
    for (const event of events) {
      const previous = families.get(event.base_checkpoint) || [];
      const parsed = reader.parseMember(event.external ? event.localizedLesson : event.lesson, event.external ? event.localizedQuiz : event.quiz, event, event.manifest, previous, event.key);
      families.set(event.base_checkpoint, parsed.records);
      if (!event.external && event.stage === stage) output = parsed;
      else parsedHistory.push(parsed.record);
    }
    const contract = contractFor(output.record.lesson, output.record.bank, output.family, output.records, parsedHistory, briefs[stage], study);
    const teaching = {lesson_id: contract.lesson_id, pages: contract.pages};
    stages.push({stage, contract, teaching, issues: output.issues, family_revision: output.family.revision});
    questions += output.record.bank.questions.length; pages += contract.pages.length;
  }
  return {ok: true, stages, family_contract: stages.map(item => item.contract), totals: {questions, pages}};
}
try {
  process.stdout.write(JSON.stringify(main()) + '\n');
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, issues: [{msg: error.message}]}) + '\n');
  process.exitCode = 1;
}
