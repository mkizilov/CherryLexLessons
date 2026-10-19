const POLICY = Object.freeze({id: 'lesson-length-v1', pageWordsWarn: 600, lessonWordsWarn: 3000});

function tableCaptionWarnings(pages) {
  const warnings = [];
  const prefix = /^(?:Tabla|Tabelle|Tableau|Tabella|Tabela|Таблица|表|표|तालिका|جدول)[ \t\u00a0\u202f]*[:：]/u;
  const visit = blocks => blocks.forEach((block, index) => {
    const next = blocks[index + 1];
    // GMD 0.3: header + separator + body, then the caption immediately (no blank).
    // Sibling AST blocks keep code, callout titles and unrelated prose out of scope.
    if (block.t === 'table' && !block.caption && next?.t === 'p' && !next.say &&
        next.line === block.line + block.body.length + 2 && prefix.test(next.text)) {
      warnings.push({level: 'warn', line: next.line, code: 'table-caption-prefix', msg: 'possible translated syntax marker; if caption intended use literal Table: and keep translated value'});
    }
    if (block.blocks) visit(block.blocks);
  });
  pages.forEach(page => visit(page.blocks));
  return warnings;
}

// Project lesson.md wrapper only; QMD quiz sections still use the frozen parser.
function parseLesson(GMD, text) {
  const parsed = GMD.parse(text);
  if (!parsed.stats?.pages?.length) return parsed;
  if (GMD.VERSION !== '0.3' || GMD.LIMITS.pageWordsWarn !== 350 || GMD.LIMITS.pageWordsMax !== 600 || GMD.LIMITS.lessonWordsWarn !== 1600) throw new Error('Review lesson-length policy against the changed reference parser');
  const replaced = new Set();
  const key = issue => JSON.stringify([issue.level, issue.line, issue.msg]);
  const warnings = tableCaptionWarnings(parsed.pages);
  parsed.stats.pages.forEach((page, index) => {
    const line = parsed.pages[index].line;
    if (page.words > GMD.LIMITS.pageWordsMax) replaced.add(key({level: 'error', line, msg: `Page has ${page.words} words; the limit is ${GMD.LIMITS.pageWordsMax}. Split the page.`}));
    else if (page.words > GMD.LIMITS.pageWordsWarn) replaced.add(key({level: 'warn', line, msg: `Page has ${page.words} words (about ${Math.round(page.words / 150 * 10) / 10} screens); aim for ${GMD.LIMITS.pageWordsWarn} or fewer.`}));
    if (page.words > POLICY.pageWordsWarn) warnings.push({level: 'warn', line, code: 'page-length', policy: POLICY.id, msg: `Page has ${page.words} words, above the ${POLICY.pageWordsWarn}-word recommendation. Review readability; length alone does not block export.`});
  });
  if (parsed.stats.words > GMD.LIMITS.lessonWordsWarn) replaced.add(key({level: 'warn', line: 1, msg: `Lesson has ${parsed.stats.words} words; aim for ${GMD.LIMITS.lessonWordsWarn} or fewer (about 6 minutes).`}));
  if (parsed.stats.words > POLICY.lessonWordsWarn) warnings.push({level: 'warn', line: 1, code: 'lesson-length', policy: POLICY.id, msg: `Lesson has ${parsed.stats.words} words, above the ${POLICY.lessonWordsWarn}-word recommendation. Review pacing; length alone does not block export.`});
  const issues = [...parsed.issues.filter(issue => !replaced.has(key(issue))), ...warnings];
  return {...parsed, issues, ok: !issues.some(issue => issue.level === 'error')};
}

module.exports = {POLICY, parseLesson};
