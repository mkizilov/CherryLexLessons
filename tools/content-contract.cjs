const crypto = require('node:crypto');
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
const same = (left, right) => JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
const identity = value => ({checkpoint: value.checkpoint, base_checkpoint: value.base_checkpoint, stage: value.stage});
function stableInline(value) {
  if (Array.isArray(value)) return value.map(stableInline);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).filter(([key]) => !['tip', 'clip'].includes(key)).map(([key, child]) => [key, stableInline(child)]));
}
function virtualFamily(records) {
  const base = records[0]?.lesson;
  if (!base || records[0].stage !== 0 || records.some((record, index) => record.stage !== index)) throw new Error('Missing base or earlier return stage in family');
  const pages = [], roles = {}, anchors = new Set();
  for (const record of records) {
    if (!record.lesson) continue;
    for (const page of record.lesson.pages) {
      if (anchors.has(page.id)) throw new Error(`Duplicate family page anchor ${page.id}`);
      anchors.add(page.id);
      pages.push(page);
    }
    for (const [name, role] of Object.entries(record.lesson.roles)) {
      if (Object.hasOwn(roles, name) && !same(roles[name], role)) throw new Error(`Conflicting family role ${name}`);
      roles[name] = role;
    }
  }
  const revision = 'family-' + crypto.createHash('sha256').update(JSON.stringify(canonical(records.map(record => ({...identity(record), lesson: record.lesson}))))).digest('hex');
  return {...base, roles, pages, revision};
}
function contractFor(json, bank, family, records, history, brief, studyLanguage) {
  if (bank.qmd !== '0.2' || bank.sentence_semantics !== 'choice-optional-blank-required-v1') throw new Error('Current project quiz semantics required');
  const policies = Object.hasOwn(brief, 'option_text_policies') ? brief.option_text_policies : {};
  if (!policies || typeof policies !== 'object' || Array.isArray(policies)) throw new Error('option_text_policies must be a question-ID map');
  for (const [id, policy] of Object.entries(policies)) {
    const question = bank.questions.find(item => item.id === id);
    if (!question || question.type !== 'choice' || !['target', 'learner'].includes(policy)) throw new Error(`Invalid option_text_policies entry ${id}`);
    if (policy === 'learner' && question.sentence.some(node => node.t === 'blank')) throw new Error(`Blank choice ${id} must preserve target option text`);
  }
  const optionPolicy = id => Object.hasOwn(policies, id) ? policies[id] : 'target';
  const study = [];
  let units = study;
  function scan(value, inherited = false, semantics = []) {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) return value.forEach(child => scan(child, inherited, semantics));
    const targetLanguage = typeof value.lang === 'string' && value.lang.toLowerCase().split('-')[0] === studyLanguage;
    const target = inherited || targetLanguage || (value.styles || []).includes('tl');
    const scope = [...semantics];
    if (targetLanguage) scope.push(['lang', value.lang.toLowerCase()]);
    if (['del', 'hide', 'rem', 'ins', 'chg', 'sub', 'sup'].includes(value.t)) scope.push(['mark', value.t]);
    const styles = (value.styles || []).filter(style => ['wrong', 'hide', 'stress', 'blank'].includes(style)).sort();
    if (styles.length) scope.push(['styles', styles]);
    const emit = unit => units.push(scope.length ? [...unit, {semantics: scope}] : unit);
    if (target && value.t === 'text') emit(['text', value.v]);
    if (value.t === 'code') emit(['code', stableInline(value)]);
    if (value.t === 'ruby') emit(['ruby', value.base, value.rt]);
    if (value.t === 'link') emit(['link', value.ref]);
    if (value.t === 'span' && value.id) emit(['span-id', value.id]);
    if (value.t === 'span' && value.ref) emit(['span-ref', value.ref]);
    if (value.role) emit(['role', value.role]);
    if (value.type === 'line') emit(['line', stableInline(value.text)]);
    if (value.type === 'gloss' && value.rows?.[0]) emit(['target-row', stableInline(value.rows[0])]);
    if (value.type === 'timeline') emit(['timeline', value.items.map(item => Object.fromEntries(['kind', 'from', 'to', 'at', 'role'].filter(key => Object.hasOwn(item, key)).map(key => [key, item[key]])))]);
    if (value.type === 'mermaid') emit(['mermaid', value.nodes.map(node => Object.fromEntries(['id', 'shape', 'rank', 'role', 'color'].filter(key => Object.hasOwn(node, key)).map(key => [key, node[key]]))), value.edges.map(edge => ({from: edge.from, to: edge.to}))]);
    if (value.type === 'pattern' && value.parts) emit(['pattern', stableInline(value.parts)]);
    if (value.type === 'morph' && value.parts) emit(['morph', stableInline(value.parts), stableInline(value.steps?.map(item => item.form))]);
    for (const [key, child] of Object.entries(value)) if (child && typeof child === 'object') scan(child, target, value.t === 'chg' && key === 'from' ? [...scope, ['side', 'from']] : scope);
  }
  for (const page of json?.pages || []) { units.push(['page', page.id]); scan(page.title); scan(page.blocks); }
  const quizStudy = [];
  units = quizStudy;
  for (const question of bank.questions) {
    units.push(['question', question.id]); scan(question.prompt); scan(question.explanation);
    for (const clip of question.audio || []) units.push(['quiz-audio', clip.text, clip.voice]);
  }
  const member = item => ({...identity(item), lesson_id: item.lesson?.id || null, pages: (item.lesson?.pages || []).map(page => page.id)});
  const roles = value => Object.fromEntries(Object.keys(value).sort().map(name => {
    const role = value[name];
    return [name, {color: role.color, line: role.line, group: role.group, same_as: role.same_as || family.roles[name]?.same_as || ''}];
  }));
  return {
    lesson_id: json?.id || null, base_lesson_id: family.id, family: records.map(member),
    prerequisites: history.map(item => ({...member(item), path: item.path, quiz_id: item.bank.id})),
    quiz_id: bank.id, quiz_format: bank.qmd, sentence_semantics: bank.sentence_semantics, pages: (json?.pages || []).map(page => page.id), study, quiz_study: quizStudy,
    roles: roles(json?.roles || {}), quiz_roles: roles(bank.roles),
    audio: (json?.audio || []).map(clip => ({text: clip.text, voice: clip.voice, lang: clip.lang})),
    questions: bank.questions.map(question => ({id: question.id, page: question.page, type: question.type, skill: question.skill, sentence: stableInline(question.sentence), ...(question.type === 'blank' ? {answers: question.answers, case: question.case} : {option_text_policy: optionPolicy(question.id), options: question.options.map(option => ({id: option.id, correct: option.correct, ...(optionPolicy(question.id) !== 'learner' ? {text: option.text} : {})}))})}))
  };
}
module.exports = {canonical, same, identity, virtualFamily, contractFor};
