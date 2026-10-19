(function (root) {
  'use strict';
  const G = root.GMD || (typeof module !== 'undefined' ? require('../../reference/formats/kit/gmd.js') : null);
  const Q = root.ProjectQMD || (typeof module !== 'undefined' && module.exports ? require('../../tools/quiz-policy.cjs') : null);
  if (!Q) throw new Error('Project quiz runtime is required');
  function createAttempt(bank, questionId) {
    let draw = 0, snapshot = Q.instantiate(bank, questionId, {seed: 'preview-0'});
    return {
      present: () => Q.present(bank, snapshot),
      snapshot: () => JSON.parse(JSON.stringify(snapshot)),
      check: (answer, composing = false) => Q.grade(bank, snapshot, answer, {composing}),
      reveal: () => {
        const question = bank.questions.find(item => item.id === questionId);
        const answer = question.type === 'choice' ? question.options.find(option => option.correct).id : question.answers[0];
        return Q.grade(bank, snapshot, answer);
      },
      newDraw: () => { snapshot = Q.instantiate(bank, questionId, {seed: `preview-${++draw}`}); return Q.present(bank, snapshot); },
    };
  }
  if (typeof module !== 'undefined' && module.exports) { module.exports = {createAttempt}; return; }
  const data = JSON.parse(document.getElementById('preview-data').textContent);
  const main = document.getElementById('preview');
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const language = (element, lang) => {
    element.lang = lang;
    element.dir = G.dirOf(lang);
    return element;
  };
  const button = (label, callback, className) => {
    const element = node('button', label, className);
    element.type = 'button';
    element.addEventListener('click', callback);
    return element;
  };
  const icon = (name, cls = '') => `<span class="g-icon ${G.esc(cls)}" aria-hidden="true">${({key: '◆', quote: '“', chat: '…', columns: '▥', pen: '✎', help: '?', check: '✓', bulb: '◇', info: 'i', alert: '!', globe: '◎', eye: '◉', fwd: '→', chd: '⌄', x: '×', spk: '♪', image: '▧', expand: '↗'})[name] || '·'}</span>`;
  const allowedStyle = /^(?:left|width|aspect-ratio|text-align|--list-start)$/;
  function markup(target, source) {
    const template = document.createElement('template');
    template.innerHTML = source;
    template.content.querySelectorAll('script,style,iframe,object,embed,form,input,link,meta,base,img,audio,video,source').forEach(element => element.remove());
    template.content.querySelectorAll('*').forEach(element => {
      const styles = element.getAttribute('style') || '';
      for (const attribute of [...element.attributes]) {
        if (/^on/i.test(attribute.name) || /^(?:href|src|srcdoc|action|formaction|style|xlink:href)$/i.test(attribute.name)) element.removeAttribute(attribute.name);
      }
      for (const declaration of styles.split(';')) {
        const separator = declaration.indexOf(':');
        const property = declaration.slice(0, separator).trim(), value = declaration.slice(separator + 1).trim();
        if (separator > 0 && allowedStyle.test(property) && /^(?:[-\d.%/ ]+|left|center|right)$/.test(value)) element.style.setProperty(property, value);
      }
    });
    target.replaceChildren(template.content);
  }
  function accessibleAction(element, callback) {
    element.tabIndex = 0;
    element.addEventListener('click', callback);
    element.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); callback(event); }
    });
  }
  let metadataVisible = false;
  function authorMetadata(text, tag = 'div') {
    const element = language(node(tag, text, 'author-metadata'), 'en');
    element.hidden = !metadataVisible;
    return element;
  }
  const metadataToggle = button('Show author metadata', () => {
    metadataVisible = !metadataVisible;
    metadataToggle.textContent = metadataVisible ? 'Hide author metadata' : 'Show author metadata';
    metadataToggle.setAttribute('aria-pressed', String(metadataVisible));
    main.querySelectorAll('.author-metadata').forEach(element => { element.hidden = !metadataVisible; });
  });
  metadataToggle.setAttribute('aria-pressed', 'false');
  metadataToggle.setAttribute('aria-controls', 'preview-content');
  metadataToggle.setAttribute('aria-describedby', 'metadata-warning');
  const metadataWarning = node('p', 'Author metadata may cue answers. Source IDs are for tracing records, not learner labels.', 'muted');
  metadataWarning.id = 'metadata-warning';
  const pages = data.page_map || [], members = data.members || [];
  const notices = node('div', undefined, 'notices');
  notices.setAttribute('role', 'status');
  const grammar = node('section', undefined, 'card grammar');
  const quiz = node('section', undefined, 'card quiz');
  const navigation = node('nav', undefined, 'page-nav');
  navigation.setAttribute('aria-label', 'Teaching pages');
  let pageIndex = Math.max(0, pages.findIndex(page => page.stage === data.stage));
  function gotoReference(ref) {
    const index = pages.findIndex(page => `${page.lesson_id}#${page.id}` === ref || page.lesson_id === ref || page.id === ref);
    if (index < 0) { notices.textContent = 'Linked teaching is outside this preview. External navigation is disabled.'; return; }
    pageIndex = index; showPage(); grammar.scrollIntoView({block: 'start', behavior: 'auto'});
  }
  function diagram(figure) {
    const flow = G.parseFlow(figure.dataset.mermaid);
    if (flow.bad.length || flow.cycle || !flow.nodes.length) { figure.replaceChildren(language(node('p', 'Diagram could not be displayed.'), 'en')); return; }
    const colors = JSON.parse(figure.dataset.colors || '{}');
    const ranks = new Map();
    for (const item of flow.nodes) { if (!ranks.has(item.rank)) ranks.set(item.rank, []); ranks.get(item.rank).push(item); }
    const width = Math.max(1, ...[...ranks.values()].map(items => items.length)) * 190;
    const height = ranks.size * 116 + 24;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `${flow.title}. ${flow.desc}`);
    const positions = new Map();
    const shape = (tag, attrs) => {
      const element = document.createElementNS(svg.namespaceURI, tag);
      for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
      return element;
    };
    for (const [rank, items] of ranks) items.forEach((item, index) => positions.set(item.id, {x: width / (items.length + 1) * (index + 1), y: rank * 116 + 44}));
    for (const edge of flow.edges) {
      const from = positions.get(edge.from), to = positions.get(edge.to);
      const x = to.x, y = to.y - 31;
      svg.append(shape('path', {d: `M ${from.x} ${from.y + 31} L ${x} ${y}`, class: 'diagram-edge'}));
      svg.append(shape('path', {d: `M ${x - 4} ${y - 7} L ${x} ${y} L ${x + 4} ${y - 7}`, class: 'diagram-edge'}));
      if (edge.label) { const text = shape('text', {x: (from.x + to.x) / 2 + 8, y: (from.y + to.y) / 2, class: 'diagram-label'}); text.textContent = edge.label; svg.append(text); }
    }
    for (const item of flow.nodes) {
      const {x, y} = positions.get(item.id);
      const cls = `diagram-node c-${colors[item.id]?.c || 'gray'}`;
      svg.append(item.shape === 'diamond' ? shape('polygon', {points: `${x},${y - 36} ${x + 84},${y} ${x},${y + 36} ${x - 84},${y}`, class: cls}) : shape('rect', {x: x - 82, y: y - 30, width: 164, height: 60, rx: item.shape === 'round' ? 24 : 7, class: cls}));
      const labels = item.label.split(/<br\s*\/?>/);
      labels.forEach((label, index) => { const text = shape('text', {x, y: y + 5 + (index - (labels.length - 1) / 2) * 18, class: 'diagram-text'}); text.textContent = label; svg.append(text); });
    }
    figure.querySelector('.g-dia-svg').replaceChildren(svg);
    figure.querySelector('.g-dia-x')?.remove();
    const description = node('p', flow.desc, 'muted');
    figure.append(description);
  }
  function renderContent(parsed, page, options = {}) {
    // The frozen renderer reads meta.title only for unlabeled links. Mark that
    // branch, not text equal to an ID: authored labels must remain verbatim.
    const original = JSON.stringify(parsed);
    let marker = 'preview-unlabeled-reference';
    while (original.includes(marker)) marker += '-';
    const lessons = new Proxy({}, {get: () => ({meta: {title: marker}})});
    const content = G.renderBlocks(parsed, page, {icon, ...options, lessons});
    const fallback = new RegExp(`(<span class="g-ref" role="link" data-ref="([^"]+)">)${marker}(?=<span class="g-icon )`, 'g');
    return content.replace(fallback, (match, opening, ref) => {
      const page = pages.find(item => G.esc(`${item.lesson_id}#${item.id}`) === ref);
      const member = members.find(item => G.esc(item.parsed?.meta.id) === ref.split('#')[0]);
      const title = page?.title || (!ref.includes('#') && member?.parsed && G.plain(member.parsed.meta.title));
      const lang = title ? data.locale : 'en';
      return `${opening.slice(0, -1)} data-preview-reference="unlabeled" lang="${G.esc(lang)}" dir="${G.dirOf(lang)}">${G.esc(title || 'Teaching reference')}`;
    }).replace(/<span class="sr">(incorrect: |added: |dropped: |changes | to |Important: | \(stressed\)|Incorrect sentence|Wrong: |Right: )<\/span>/g, '<span class="sr" lang="en" dir="ltr">$1</span>');
  }
  function enhance(target) {
    target.querySelectorAll('[data-clip]').forEach(element => {
      element.removeAttribute('role'); element.removeAttribute('aria-label');
      element.classList.add('audio-unavailable');
      element.textContent = 'Audio unavailable';
      language(element, 'en');
      element.title = 'No verified audio asset is attached to this reference preview';
    });
    target.querySelectorAll('.g-hide').forEach(element => {
      const details = node('details', undefined, 'inline-reveal');
      details.append(language(node('summary', 'Reveal hidden text'), 'en'), ...element.childNodes);
      element.replaceWith(details);
    });
    target.querySelectorAll('[data-ref]').forEach(element => {
      accessibleAction(element, () => gotoReference(element.dataset.ref));
      if (element.dataset.previewReference === 'unlabeled') element.after(authorMetadata(`Reference ID: ${element.dataset.ref}`, 'span'));
    });
    target.querySelectorAll('[data-tip]').forEach(element => { element.title = element.dataset.tip; });
    target.querySelectorAll('.g-dia[data-mermaid]').forEach(diagram);
    target.querySelectorAll('.g-imgph').forEach(element => element.append(language(node('small', 'Image placeholder — no asset generated'), 'en')));
  }
  let calloutLabelId = 0;
  function richView(target, parsed, page = 0, pageTitle = false) {
    language(target, parsed.meta.explained_in || data.locale);
    const lesson = JSON.parse(JSON.stringify(parsed)), expand = {};
    function draw(focusKey) {
      markup(target, renderContent(lesson, page, {reveal: true, expand}));
      const callouts = [...target.querySelectorAll(':scope > .g-co')];
      let calloutIndex = 0;
      lesson.pages[page].blocks.forEach((block, index) => {
        if (block.t !== 'callout') return;
        const element = callouts[calloutIndex++];
        if (!element) return;
        const header = element.querySelector('.g-co-h');
        const key = `${page}-${index}`;
        const generatedLabel = !block.custom && (['rule', 'mistake', 'recall'].includes(block.type) || !block.title);
        const calloutLabel = element.querySelector('.g-co-l');
        if (generatedLabel) { language(calloutLabel, 'en'); language(header, 'en'); }
        // The frozen aria-label is always an English type name (or a custom
        // key). Name the note through its visible label without relabeling
        // authored titles, target spans or the Arabic body.
        calloutLabel.id = `preview-callout-label-${++calloutLabelId}`;
        element.removeAttribute('aria-label');
        element.setAttribute('aria-labelledby', calloutLabel.id);
        element.querySelectorAll('.g-fold-hint').forEach(hint => language(hint, 'en'));
        if (block.fold) {
          header.dataset.focus = key;
          accessibleAction(header, () => { block.fold = block.fold === '-' ? '+' : '-'; draw(key); });
        }
        element.querySelectorAll('.g-tab').forEach((tab, tabIndex) => {
          tab.dataset.focus = `${key}-${tabIndex}`;
          accessibleAction(tab, () => { expand[`${key}:tab`] = tabIndex; draw(`${key}-${tabIndex}`); });
        });
      });
      enhance(target);
      if (focusKey) [...target.querySelectorAll('[data-focus]')].find(element => element.dataset.focus === focusKey)?.focus();
    }
    draw();
    if (pageTitle) target.prepend(node('h2', G.plain(lesson.pages[page].title)));
  }
  function showPage() {
    grammar.replaceChildren(); navigation.replaceChildren();
    if (!pages.length) { grammar.append(node('h2', 'Grammar not available'), node('p', 'Write a valid lesson to see its rendered teaching pages.')); return; }
    pages.forEach((page, index) => {
      const control = button(`Stage ${page.stage} · `, () => { pageIndex = index; showPage(); });
      control.append(language(node('span', page.title), data.locale));
      if (index === pageIndex) control.setAttribute('aria-current', 'page');
      navigation.append(control);
    });
    const page = pages[pageIndex], member = members.find(item => item.stage === page.stage);
    grammar.append(node('div', `Stage ${page.stage}`, 'eyebrow'), authorMetadata(`Lesson ID: ${page.lesson_id} · Page ID: ${page.id}`), language(node('h2', page.title), data.locale));
    if (data.mode === 'practice') grammar.append(node('p', 'Practice-only stage: these are prior teaching pages, not new lesson content.', 'notice'));
    const names = member.parsed.stats.pages[page.index].roles;
    const legend = language(node('div'), data.locale);
    markup(legend, G.legendHTML(member.parsed.roles, names, true));
    const body = node('div', undefined, 'grammar-body');
    grammar.append(legend, body);
    richView(body, member.parsed, page.index);
  }
  function buildQuiz() {
    const bank = data.bank;
    if (!bank) { quiz.append(node('h2', 'Quiz not available'), node('p', 'The diagnostic report explains what is missing or invalid. No partial or guessed quiz is shown.')); return; }
    const heading = language(node('h2', bank.title), bank.explained_in);
    const summary = node('p', `${bank.questions.length} authored questions. Single question tries one stable draw; All questions audits the complete bank. Nothing is saved outside this frame.`, 'muted');
    const richById = new Map((data.rich || []).map(item => [item.id, item]));
    const single = node('div'); single.id = 'quiz-single';
    const overview = node('div'); overview.id = 'quiz-all'; overview.hidden = true;
    const viewStatus = node('p', '', 'muted'); viewStatus.setAttribute('role', 'status');
    const viewControls = node('div', undefined, 'quiz-view-controls');
    viewControls.setAttribute('role', 'group'); viewControls.setAttribute('aria-label', 'Quiz view');
    const singleButton = button('Single question', () => setView(false));
    const allButton = button('All questions', () => setView(true));
    singleButton.setAttribute('aria-controls', single.id); singleButton.setAttribute('aria-pressed', 'true');
    allButton.setAttribute('aria-controls', overview.id); allButton.setAttribute('aria-pressed', 'false');
    viewControls.append(singleButton, allButton);
    let overviewBuilt = false, answersVisible = false, pendingView = null;
    const auditAnswers = [], exposedAnswers = new Set();
    const select = node('select'); select.id = 'question-picker';
    const label = node('label', 'Question'); label.htmlFor = select.id;
    bank.questions.forEach((question, index) => { const option = node('option', `Question ${index + 1} · ${question.type}`); option.value = question.id; select.append(option); });
    const questionBox = node('div');
    const attempts = new Map();
    const record = id => {
      if (!attempts.has(id)) attempts.set(id, {attempt: createAttempt(bank, id), answer: '', composing: false, revealed: false, feedback: null, status: ''});
      return attempts.get(id);
    };
    function questionMetadata(question) {
      return authorMetadata(`Question ID: ${question.id} · Skill ID: ${question.skill} · Teaching page ID: ${question.page}`);
    }
    function sentenceView(source) {
      if (!source.sentence) return null;
      const sentence = language(node('div', undefined, 'quiz-sentence'), bank.lang);
      markup(sentence, renderContent(source.sentence, 0).replace('{{blank}}', '<span class="blank-slot" lang="en" dir="ltr" aria-label="blank">_____</span>'));
      enhance(sentence);
      return sentence;
    }
    function renderAnswer(target, question, source, audit = false, feedback = null) {
      const correct = question.type === 'choice' ? question.options.find(option => option.correct) : null;
      const optionLanguage = source.option_language || bank.lang;
      const canonical = language(node('bdi', correct ? correct.text : question.answers[0], 'canonical-answer'), correct ? optionLanguage : bank.lang);
      target.append(node('h4', 'Canonical answer'), canonical);
      if (correct) target.append(authorMetadata(`Correct option ID: ${correct.id}`));
      if (!correct) {
        target.append(node('h4', 'Accepted forms'));
        const forms = node('ul', undefined, 'accepted-forms');
        question.answers.forEach(value => { const item = node('li'); item.append(language(node('bdi', value), bank.lang)); forms.append(item); });
        target.append(forms, node('p', `Case policy: ${question.case}.`, 'muted'));
      }
      if (audit && correct) {
        const reasons = question.options.filter(option => option.why);
        if (reasons.length) {
          target.append(node('h4', 'Option explanations'));
          const list = node('ul');
          reasons.forEach(option => {
            const item = node('li');
            item.append(language(node('span', `Pool item ${question.options.indexOf(option) + 1} — `), 'en'), language(node('bdi', option.text), optionLanguage), language(node('p', option.why, 'option-why'), bank.explained_in));
            list.append(item);
          });
          target.append(list);
        }
      }
      if (feedback?.why) target.append(node('h4', 'Selected option explanation'), language(node('p', feedback.why, 'option-why'), bank.explained_in));
      target.append(node('h4', 'Explanation'));
      const explanation = node('div', undefined, 'quiz-explanation'); target.append(explanation); richView(explanation, source.explanation);
    }
    function setAnswers(visible) {
      answersVisible = visible;
      answerToggle.setAttribute('aria-pressed', String(visible));
      answerToggle.textContent = visible ? 'Hide answers' : 'Show answers';
      for (const item of auditAnswers) {
        if (visible) exposedAnswers.add(item.question.id);
        if (visible && !item.rendered) {
          renderAnswer(item.panel, item.question, item.source, true);
          item.rendered = true;
        }
        item.panel.hidden = !visible;
      }
    }
    const answerToggle = button('Show answers', () => setAnswers(!answersVisible));
    answerToggle.setAttribute('aria-pressed', 'false');
    answerToggle.setAttribute('aria-controls', 'audit-questions');
    function buildOverview() {
      const toolbar = node('div', undefined, 'audit-toolbar');
      toolbar.append(node('h3', `All ${bank.questions.length} questions`), answerToggle);
      overview.append(toolbar, node('p', 'Authoring audit · Complete pools in source order, not randomized draws or a learner session. Source order and metadata may cue answers; this is not a blind check. Answer keys and explanations are collapsed until you choose Show answers.', 'muted'));
      const list = node('div', undefined, 'audit-questions'); list.id = 'audit-questions';
      bank.questions.forEach((question, index) => {
        const item = node('article', undefined, 'audit-question'); item.dataset.questionId = question.id;
        const title = node('h3', `Question ${index + 1}`); title.id = `audit-question-${index}`;
        item.setAttribute('aria-labelledby', title.id);
        item.append(title, node('p', question.type, 'question-meta'), questionMetadata(question));
        const source = richById.get(question.id);
        if (!source) item.append(node('p', 'Canonical rich content is unavailable for this question.', 'error'));
        else {
          item.append(node('h4', 'Prompt'));
          const prompt = node('div', undefined, 'quiz-prompt'); richView(prompt, source.prompt); item.append(prompt);
          if (question.sentence.length) item.append(sentenceView(source));
          if (question.type === 'choice') {
            item.append(node('h4', `Full option pool · ${question.options.length} options`), node('p', 'Authored order below. Pool item numbers are audit positions, not learner labels. Single question draws 4 options: the correct answer and 3 distractors, then shuffles them into A–D.', 'muted'));
            const pool = node('ul', undefined, 'audit-options');
            question.options.forEach((option, position) => {
              const row = node('li', undefined, 'audit-option');
              const text = language(node('bdi', option.text, 'option-text'), source.option_language || bank.lang);
              row.append(language(node('span', `Pool item ${position + 1}`, 'pool-position'), 'en'), text, authorMetadata(`Option ID: ${option.id}`)); pool.append(row);
            });
            item.append(pool);
          } else item.append(node('p', 'Fill one blank · Accepted forms and case policy are in the answer panel.', 'muted'));
          const panel = node('section', undefined, 'answer-panel'); panel.hidden = true;
          auditAnswers.push({question, source, panel, rendered: false}); item.append(panel);
        }
        list.append(item);
      });
      overview.append(list); overviewBuilt = true;
    }
    function setView(all) {
      pendingView = null; viewStatus.textContent = '';
      if (record(select.value).composing && all) {
        pendingView = all; viewStatus.textContent = 'Finish entering your answer to open All questions. Your current attempt will be kept.';
        return;
      }
      if (all && !overviewBuilt) buildOverview();
      single.hidden = all; overview.hidden = !all;
      singleButton.setAttribute('aria-pressed', String(!all)); allButton.setAttribute('aria-pressed', String(all));
    }
    function drawQuestion() {
      const state = record(select.value), shown = state.attempt.present();
      for (const attempt of attempts.values()) attempt.composing = false;
      pendingView = null; viewStatus.textContent = '';
      const source = richById.get(shown.questionId);
      questionBox.replaceChildren();
      if (!source) { questionBox.append(node('p', 'Canonical rich content is unavailable for this question.', 'error')); return; }
      questionBox.append(questionMetadata(bank.questions.find(item => item.id === shown.questionId)));
      const prompt = node('div', undefined, 'quiz-prompt'); richView(prompt, source.prompt);
      questionBox.append(prompt);
      const sentence = sentenceView(source);
      if (sentence) questionBox.append(sentence);
      if (shown.type === 'choice') {
        const group = node('fieldset', undefined, 'options');
        group.append(node('legend', 'Choose one answer'));
        shown.options.forEach(option => {
          const row = node('label', undefined, 'option');
          const radio = node('input'); radio.type = 'radio'; radio.name = 'preview-choice'; radio.value = option.id; radio.checked = state.answer === option.id;
          radio.addEventListener('change', () => { state.answer = option.id; state.status = ''; state.feedback = null; state.revealed = false; status.textContent = ''; answer.replaceChildren(); });
          const text = language(node('bdi', option.text, 'option-text'), source.option_language || shown.lang);
          row.append(radio, node('span', option.label, 'option-letter'), text);
          group.append(row, authorMetadata(`Option ${option.label} ID: ${option.id}`));
        });
        questionBox.append(group);
      } else {
        const label = node('label', 'Your answer'); label.htmlFor = 'blank-answer';
        const input = language(node('input'), shown.lang); input.id = 'blank-answer'; input.type = 'text'; input.autocomplete = 'off'; input.spellcheck = false; input.value = state.answer;
        input.addEventListener('input', () => { state.answer = input.value; state.status = ''; state.feedback = null; state.revealed = false; status.textContent = ''; answer.replaceChildren(); });
        input.addEventListener('compositionstart', () => { state.composing = true; });
        input.addEventListener('compositionend', () => {
          state.composing = false; state.answer = input.value;
          if (pendingView !== null) setView(pendingView);
        });
        questionBox.append(label, input);
      }
      const status = node('p', state.status, 'answer-status'); status.setAttribute('aria-live', 'polite');
      const answer = node('section', undefined, 'answer-panel');
      function renderFeedback() {
        answer.replaceChildren();
        if (state.feedback || state.revealed) renderAnswer(answer, bank.questions.find(item => item.id === shown.questionId), source, false, state.feedback);
      }
      function showAnswer() {
        if (state.composing) { status.textContent = 'Finish entering your answer first.'; return; }
        state.revealed = true;
        exposedAnswers.add(shown.questionId);
        state.status = 'Answer shown with assistance; not an unassisted check.';
        status.textContent = state.status;
        renderFeedback();
      }
      const controls = node('div', undefined, 'quiz-controls');
      controls.append(button('Check', () => {
        if (state.composing) { status.textContent = 'Finish entering your answer first.'; return; }
        const result = state.attempt.check(state.answer);
        const complete = result.status === 'correct' || result.status === 'incorrect';
        state.status = ({correct: 'Correct.', incorrect: 'Not quite. Review the expected form and explanation.', incomplete: 'Choose or enter an answer first.', composing: 'Finish entering your answer first.'})[result.status] || result.status;
        if (complete) {
          state.feedback = result;
          if (exposedAnswers.has(shown.questionId)) state.status += ' Answer previously viewed; not a blind check.';
          exposedAnswers.add(shown.questionId);
        }
        status.textContent = state.status;
        renderFeedback();
      }, 'primary'), button('Show answer', showAnswer), button('Reset answer', () => { state.answer = ''; state.revealed = false; state.feedback = null; state.status = ''; drawQuestion(); }), button('New draw', () => { state.attempt.newDraw(); state.answer = ''; state.revealed = false; state.feedback = null; state.status = ''; drawQuestion(); }), button('Teaching page', () => gotoReference(shown.page)));
      questionBox.append(controls, status, answer);
      renderFeedback();
    }
    select.addEventListener('change', drawQuestion);
    single.append(label, select, questionBox);
    quiz.append(heading, summary, viewControls, viewStatus, single, overview);
    drawQuestion();
  }
  main.replaceChildren();
  const header = node('header', undefined, 'preview-header');
  header.append(node('p', 'CHERRYLEX · LESSON LAB', 'eyebrow'), node('h1', 'Read. Try. Refine.'), node('p', data.scope || 'Reference preview; no native/audio verification', 'scope'), node('p', `${data.locale} learner · stage ${data.stage}`, 'muted'), authorMetadata(`Source path: ${data.path || ''}`), metadataToggle, metadataWarning);
  main.append(header, notices);
  if (data.issues?.length) {
    const details = node('details', undefined, 'diagnostics');
    details.open = data.issues.some(issue => issue.level === 'error');
    details.append(node('summary', `${data.issues.length} preview diagnostics${data.ok ? '' : ' · not a valid completed pair'}`));
    const list = node('ul');
    data.issues.forEach(issue => list.append(node('li', `${issue.level || 'error'} · ${issue.source || 'preview'}${issue.line ? ':' + issue.line : ''} — ${issue.msg}`, issue.level === 'error' ? 'error' : 'warning')));
    details.append(list); main.append(details);
  }
  const content = node('div'); content.id = 'preview-content';
  content.append(navigation, grammar, quiz); main.append(content);
  try { showPage(); buildQuiz(); }
  catch (error) { notices.textContent = `Preview rendering failed safely: ${error.message}`; }
})(typeof window !== 'undefined' ? window : globalThis);
