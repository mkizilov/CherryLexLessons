(function (root) {
  'use strict';
  const node = typeof module !== 'undefined' && module.exports;
  const Engine = node ? require('../reference/formats/kit/qmd.js') : root.QMD;
  const GMD = node ? require('../reference/formats/kit/gmd.js') : root.GMD;
  const VERSION = '0.2';
  const SEMANTICS = 'choice-optional-blank-required-v1';
  if (Engine.VERSION !== '0.1' || GMD.VERSION !== '0.3') throw new Error('Review project quiz policy for changed frozen parser versions');
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const canonical = value => Array.isArray(value) ? value.map(canonical) : object(value) ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
  const hash = text => {
    let a = 2166136261, b = 2654435769;
    for (let i = 0; i < text.length; i++) { a = Math.imul(a ^ text.charCodeAt(i), 16777619) >>> 0; b = Math.imul(b ^ text.charCodeAt(i), 16777619) >>> 0; }
    return a.toString(36) + '-' + b.toString(36);
  };
  function prepare(src) {
    if (typeof src !== 'string' || src.length > Engine.LIMITS.source) return {source: src, empty: new Set(), modern: false};
    const text = src.replace(/\r\n?/g, '\n');
    const front = text.match(/^---\n([\s\S]*?)\n---(?:\n|$)/);
    if (!front || !/^qmd:\s*(?:0\.2|"0\.2"|'0\.2')\s*$/m.test(front[1])) return {source: src, empty: new Set(), modern: false};
    const empty = new Set(), lines = text.slice(front[0].length).split('\n'), output = [];
    let i = 0;
    while (i < lines.length) {
      if (!/^# [a-z0-9]+(?:-[a-z0-9]+)*$/.test(lines[i])) { output.push(lines[i++]); continue; }
      const start = i++, id = lines[start].slice(2);
      let fence = false;
      while (i < lines.length && (fence || !/^# /.test(lines[i]))) { if (/^```/.test(lines[i])) fence = !fence; i++; }
      const block = lines.slice(start, i);
      const configFence = block.findIndex(line => line === '```quiz');
      const configEnd = block.indexOf('```', configFence + 1);
      let config;
      try { config = JSON.parse(block.slice(configFence + 1, configEnd).join('\n')); } catch {}
      const sections = [];
      fence = false;
      block.forEach((line, index) => { if (!fence && /^## (Prompt|Sentence|Explanation)$/.test(line)) sections.push({name: line.slice(3), index}); if (/^```/.test(line)) fence = !fence; });
      const sentences = sections.filter(section => section.name === 'Sentence');
      if (config?.type === 'choice' && sentences.length <= 1) {
        const sentence = sentences[0], next = sentence && sections.find(section => section.index > sentence.index);
        const end = next?.index ?? block.length;
        if (!sentence || !block.slice(sentence.index + 1, end).join('\n').trim()) {
          empty.add(id);
          if (sentence) block.splice(sentence.index + 1, end - sentence.index - 1, 'Quiz content');
          else {
            const explanation = sections.find(section => section.name === 'Explanation');
            block.splice(explanation?.index ?? block.length, 0, '## Sentence', 'Quiz content', '');
          }
        }
      }
      output.push(...block);
    }
    return {source: front[0].replace(/^(qmd:\s*)(?:0\.2|"0\.2"|'0\.2')(\s*)$/m, '$10.1$2') + output.join('\n'), empty, modern: true};
  }
  function visible(value) {
    if (!value || typeof value !== 'object' || value.t === 'hide' || (value.styles || []).includes('hide')) return false;
    if ((value.t === 'text' && value.v.trim()) || (value.t === 'ruby' && value.base.trim()) || (typeof value.alt === 'string' && value.alt.trim())) return true;
    if (value.type === 'mermaid' && (value.title?.trim() || value.nodes?.some(node => node.label?.some(label => label.trim())))) return true;
    return Object.values(value).some(child => Array.isArray(child) ? child.some(visible) : object(child) && visible(child));
  }
  function parseRich(src, options = {}) {
    const prepared = prepare(src), captured = [], original = GMD.parse;
    let parsed;
    try {
      GMD.parse = (...args) => { const result = original(...args); captured.push(result); return result; };
      parsed = Engine.parse(prepared.source, options);
    } finally { GMD.parse = original; }
    if (!prepared.modern) {
      parsed.issues.push({level: 'error', line: 1, msg: 'Project qmd must be 0.2.'});
      parsed.ok = false;
    }
    if (prepared.modern) {
      parsed.meta.qmd = VERSION;
      parsed.sentence_semantics = SEMANTICS;
      for (const question of parsed.questions) {
        if (prepared.empty.has(question.id)) question.sentence = [];
        for (const name of ['prompt', 'explanation']) if (!question[name].some(visible)) parsed.issues.push({level: 'error', line: 1, msg: `Nonempty visible ${name} is required.`});
      }
      parsed.ok = !parsed.issues.some(issue => issue.level === 'error');
    }
    let rich = [];
    if (parsed.ok) {
      if (captured.length !== parsed.questions.length * 3 || captured.some(lesson => !lesson.ok || lesson.pages.length !== 1)) {
        parsed.issues.push({level: 'error', line: 1, msg: 'Canonical rich-section parser contract changed.'}); parsed.ok = false;
      } else rich = parsed.questions.map((question, index) => ({id: question.id, prompt: captured[index * 3], sentence: prepared.empty.has(question.id) ? null : captured[index * 3 + 1], explanation: captured[index * 3 + 2]}));
    }
    return {parsed, rich};
  }
  const parse = (src, options) => parseRich(src, options).parsed;
  function toJSON(parsed) {
    if (parsed?.meta?.qmd !== VERSION || !parsed.ok || parsed.draft || parsed.sentence_semantics !== SEMANTICS) throw new Error('QMD: invalid or draft banks cannot be exported');
    const delegated = Engine.toJSON({...parsed, meta: {...parsed.meta, qmd: '0.1'}});
    const {revision, ...content} = delegated;
    const out = {...content, qmd: VERSION, sentence_semantics: SEMANTICS};
    return {...out, revision: 'qmd02-' + hash(JSON.stringify(canonical(out)))};
  }
  function engineBank(bank) {
    if (bank?.qmd !== VERSION || bank.sentence_semantics !== SEMANTICS) throw new Error('QMD: invalid project bank semantics');
    const {revision, ...content} = bank;
    if (revision !== 'qmd02-' + hash(JSON.stringify(canonical(content)))) throw new Error('QMD: invalid or old project bank revision');
    return {...bank, qmd: '0.1'};
  }
  function snapshotFor(bank, snapshot) {
    if (!object(snapshot) || snapshot.qmd !== VERSION) throw new Error('QMD: invalid or old snapshot identity');
    engineBank(bank);
    return {...snapshot, qmd: '0.1'};
  }
  function instantiate(bank, id, options) {
    return {...Engine.instantiate(engineBank(bank), id, options), qmd: VERSION};
  }
  function present(bank, snapshot) {
    return Engine.present(engineBank(bank), snapshotFor(bank, snapshot));
  }
  function grade(bank, snapshot, answer, options) {
    return Engine.grade(engineBank(bank), snapshotFor(bank, snapshot), answer, options);
  }
  const API = {VERSION, SEMANTICS, LIMITS: Engine.LIMITS, parse, parseRich, toJSON, instantiate, present, grade};
  if (node) module.exports = API; else root.ProjectQMD = API;
})(typeof window !== 'undefined' ? window : globalThis);
