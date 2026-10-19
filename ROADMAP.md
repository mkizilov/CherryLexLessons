# Roadmap

Proposed software-engineering milestones; not completed features or promises of curriculum quality.

## A self-contained public toolkit

- [ ] Document the frozen-reference input contract without redistributing third-party corpora.
- [ ] Separate format parsers from private reference data with explicit version/license pins.
- [ ] Make more regression tests run on small synthetic reference sets in a clean clone.
- [ ] Add CI for public-source validation and synthetic tests.

## Reliable authoring operations

- [ ] Expand interrupted-publication, cancellation and same-session recovery tests.
- [ ] Improve queue diagnostics and explain blocked prerequisites directly in the dashboard.
- [ ] Verify Git/worktree-compatible worker isolation before moving live authoring into a Git checkout.
- [ ] Document and rehearse backups and restores without purchasing new output.

## Better review and export tools

- [ ] Improve navigation between a review finding and the exact affected lesson/question.
- [ ] Add clearer compatibility reports for lesson-pack revisions consumed by the app.
- [ ] Audit preview keyboard accessibility, long text and mixed-script layouts on synthetic content.

Content authoring, paid localization and model comparisons are separate activities; they are not required to contribute to these engineering tasks.
