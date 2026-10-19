# Lesson-job recovery

Run commands from the CherryLexLessons root. This is an operational guide, not permission to change source, reviewer evidence or paid-request limits.

## Failure classes and durable handling

| Evidence | Recovery | What must not happen |
| --- | --- | --- |
| Managed coordinator exited with an allowlisted transient API/stream/service-pressure error; original SID and stopped process group verified | Dashboard retries the same session after 2/5/30 minutes, max three automatic resumes per job. Persistent event ledger retains due time and count. | No automatic resume of user stop/cancel, content-blocked receipt, unknown ownership or permanent error. No new session or reset of translation budgets. |
| Complete family in one retained message, even if provider terminal failed | Extract as an unapproved draft, verify source provenance, validate/repair all five stages, integrate under exclusive ownership, then genuine locale review. | Never concatenate independent messages or rewrite failed provider state as completed. Multiple different candidates require explicit selection. |
| Provider returned exact empty skeleton before a valid family | New parser ignores only the exact all-empty five-stage skeleton; accepts one unambiguous distinct full family, then normal deep validation. | Any malformed/competing message remains offline triage. Existing journals remain untouched. |
| Incomplete remote `max_output_tokens`, no client cap, no recoverable full output | Prospective original-budget retry after at least 30 minutes; longer Retry-After wins. Remaining attempts and due time persist. | Not a new eight-attempt default. Old nonretryable or exhausted records are not rewritten. |
| Retained rejected translations, truncated/missing learner units or exhausted old API attempts | Bilingual editor salvages validated whole units and translates missing learner content from approved source in the ordinary assigned agent session; a different reviewer audits it. | No new Contactbox/provider dispatch implied, self-approval, target/answer fork, or blind byte repair. |
| Partial/expert-edited destination | Snapshot outside family tree; reconcile explicitly, validate the complete 15-file locale, then independent review. | API publication cannot overwrite expert edits; partial inventory is not complete. |
| New local typo/heading defect during integration | Current progress-aware repair policy permits six substantive grouped passes and one bounded targeted integration fix, retaining old counts and requiring affected reviewer reapproval/final sign-off. | No style churn, resets, majority votes or weakening answer/prerequisite correctness. Three unchanged checks or unresolved substantive dispute still block. |
| Missing approved source, frozen-history drift, billing/refusal, explicit cancellation or unknown process ownership | Preserve evidence and report the precise operator-required condition. | No claim of “zero stops” can safely waive these safeguards. |

## Offline translation path

1. Inspect all original and renewal evidence read-only. Review real message items, not only response terminal status. The source `brief.extension` decides practice versus extension; provider notes cannot change it.
2. Extract draft candidates with `tools/reauthorize-translations.py --extract-retained LOCALE --run FAMILY_RUN --output work/content-recovery/STUDY/BASE/LOCALE/candidate.json`. A provenance sidecar and validation diagnostics are evidence, not approval. Use explicit `--item` selection if multiple distinct candidates exist.
3. Missing/truncated units are completed from approved source by an exclusively assigned bilingual editor. Keep workspace and snapshots outside `family_runs` so strict publication inventory stays valid. Record retained versus newly authored units and actual editor identity.
4. Validate the complete five-stage candidate with `tools/validate-family.cjs --root ROOT --run FAMILY_RUN --locale LOCALE --candidate PATH`, compare `family_contract` to current source, then integrate the complete mutable locale under coordinator control.
5. Run `family_cli check`, obtain a **different** genuine reviewer, reconcile shared findings and all current report bindings. Finalization and dashboard completion still require the real immutable pack/registry and stopped writers.

## Operating the dashboard

- Restart with `.venv/bin/python tools/dashboard.py`; startup pauses new scheduling.
- Resume only saved sessions through the dashboard/Runner API, after verified quiescence. Do not hand-edit queue.sqlite or launch an untracked duplicate.
- Job-specific `work/dashboard/jobs/<id>/recovery-instructions.md` is an operator plan read on resume. It never enters source/provider payloads and cannot grant extra paid attempts or approvals. Explicit operator updates must retain earlier evidence and clarify which old instruction is superseded.
- `retry_wait` remains visible in the unpaginated active/attention view. User pause stops new launches; user cancellation is never automatically undone.
- Completion releases successors only after independent pack verification and process termination, not a model's success claim.

## Checks

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
.venv/bin/python tools/check.py
../CherryLex/work/venvs/pipeline/bin/python -m pyflakes lesson_factory tools tests
```

Focused tests cover message-item parsing, prospective incomplete retries, retained recovery, coordinator autorecovery and dashboard state. No offline test proves every live provider failure will be recoverable or that linguistic output is correct.
