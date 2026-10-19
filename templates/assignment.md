# Agent assignment

Coordinator replaces every placeholder before dispatch. This is a handoff, not learner content. Use absolute read/edit paths when working directories differ; CLI `--run` remains root-relative, e.g. `family_runs/ru/000020`. Execute CLI from the specified project root.

- Agent / review role: REPLACE.
- Actual reviewer session: obtain from Task/Ensemble metadata, never invent before spawn. Record role, member, actual session ID, metadata source and unique report path; source reviewers must be independent sessions.
- Project root / exact family run / base checkpoint: REPLACE.
- Five stage identities/unlocks: REPLACE from family-context.json; exactly stage0..4 at base/+25/+75/+125/+225.
- Study / assigned learner / pinned source / eleven final locales: REPLACE. No same-language pair; ten translated experts exclude study/source.
- Mode: source report-only / one full-locale editing / explicitly scoped tool repair.
- Family capability/exclusions and five stage purposes/decisions/reinforcement/extensions: REPLACE from stage0 brief.family_plan.
- Stage objectives, extension modes/rationales and literacy policies: REPLACE from all five stage briefs.
- Pinned context path/hash, catalogue command and all five brief paths: REPLACE. Use `python3 tools/history.py --run RUN --catalog --limit 20`; follow next_offset with --context-hash from page one. This gives compact metadata without dumping external_history; never rewrite the stored context.
- Source tree: REPLACE with `<run>/<source>/stage-0..4/{lesson.md,quiz.md}`.
- Assigned locale tree/notes: REPLACE with `<run>/<locale>/stage-0..4/{lesson.md,quiz.md,adaptation-notes.json}` if relevant.
- Authoring block/wave/order versus five learner unlock ranks: REPLACE; these are not the same order.
- Historical retrieval: scan every catalogue page, including future/planning topics. Independently open original lessons, relevant quiz sections, briefs and notes for prerequisites, inherited forms/senses, overlap, contrasts, terminology, references and findings; follow dependencies and broaden uncertain/missing metadata. Source roles read pinned-source originals; locale roles additionally read the relevant assigned-locale originals, not every translated copy. Record selected keys/anchors/reasons in existing evidence fields. All current-family content/item audits remain complete.
- Each stage's eligible external keys and earlier own-stage references: REPLACE. Only actual prior teaching is inherited; `stage:N` means an earlier member, not a future known lesson.
- Each stage's planning-only relationships and selected historical file pointers: REPLACE from the catalogue. Future material informs gaps/overlap, never prerequisite/known-form/taught_in/link permission. Missing future bases cannot be assumed.
- Exact allowed write paths: REPLACE; locale experts own only their locale, not source/other locales/shared findings/registry. Reconstruction/replacement editors use a new attempt-and-session-specific workspace outside the family; never reuse a predecessor's workspace or publish while it may still write. Late outputs are preserved as unapproved evidence, never overwritten or trusted solely by their stated author.
- Exclusive report path outside accepted reviews/: REPLACE. Preserve rejected revisions.
- Starting binding: REPLACE with current `family_cli binding --run RUN --role ROLE`, or require obtaining before reading.
- Validation: `python3 -m lesson_factory.family_cli check --run RUN --locale CODE` plus assigned lens-specific checks. Content reviewers do not run concurrent full suites; coordinator assigns one tooling-suite owner.
- Report template: `templates/family-review.json`, exactly five stage entries. Assessment/language `question_checks` covers every ID in its stage with status/rationale and meaningful answer/alternatives/teaching_page evidence. These must be actual audits, not placeholders or copied keys.
- Open findings / requested recheck / shared acknowledgments: REPLACE or explicitly none.
- Offline repair/recheck budget: follow pack-review's progress-aware policy: up to six substantive grouped passes per affected review scope, preserve counts/evidence on resume, adjudicate a repeated defect after two failed fixes and stop after three unchanged checks. One bounded targeted integration correction for a newly found local typo/heading/format defect is allowed beyond that cap, with genuine affected-reviewer reapproval and new final sign-off. No paid-budget increase or source/answer/prerequisite relaxation.
- Required deliverable checks: format reviewer inspects every Markdown teaching page and parsed question/feedback structure; language expert checks localized content, explicit language/direction/readings and every answer. Browser inspection, capability discovery, screenshots and walkthroughs are outside this assignment; do not add optional browser work or skip/waiver fields. Record actual content IDs/checks without claiming visual/native/audio/accessibility verification. Renderer/UI tests belong to a separate engineering assignment.
- Research/tool scope: local tools plus named unresolved research questions; no credentials in context, paid generation or nested agents.
- Tool-change ownership: none unless exact paths/acceptance checks explicitly assigned; diagnose via authoring-tooling first.
- Deliverable: current-byte report path, actual checks/results, changed files, stage-specific evidence/coverage and blockers. If mandatory files, binding or actual session metadata are unavailable, return a non-review blocked message with missing inputs and attempted checks; do not create a fabricated report or submit it. Inspectable content defects get a current-bound rejected report. Never invent results or claim whole-family approval from one locale.

Source reviewers inspect the same frozen five-stage source and propose repairs; coordinator edits after reports arrive. Locale experts edit only assigned locale members/notes. Shared target corrections go through coordinator and all affected locales. Active experts inspect relevant peer broadcasts; completed experts need concrete reactivation/recheck, stopped experts a new real review. Three source plus ten locale plus final are14family-wide reports, not one review set per stage.
