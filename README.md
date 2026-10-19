# CherryLexLessons

**Structured lesson authoring with explicit prerequisites, review evidence and reproducible publication.**

CherryLexLessons is a local authoring toolkit for multilingual language lessons. It groups a capability into five related stages, checks teaching and quiz structure, tracks review approvals against exact content versions, and publishes immutable lesson families for the CherryLex app.

## What it does

- Plans cumulative lesson families for twelve study languages.
- Validates Markdown lessons, question banks and cross-stage prerequisite boundaries.
- Tracks independent source and localization reviews without treating an edited file as an approval.
- Provides a local NiceGUI dashboard for queue management and read-only previews.
- Preserves job, retry and publication evidence through interrupted authoring work.

**Status:** active development. This repository contains the software and synthetic test code—not the private reference corpus, production prompts, generated lessons or running authoring jobs. It is not a complete language course or the mobile app.

## Start with the public source

Requires **Python 3.11+** and **Node 24+** (`NODE_BINARY` can select Node).

```sh
python3 tools/check.py --source-only
python3 -m unittest discover -s tests -p 'test_public_source.py' -v
```

These checks need no model calls, credentials or production content. The full validation and test suites require separately provisioned frozen reference files; see [development](docs/development.md).

For the optional dashboard dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dashboard.txt
# Only after provisioning the documented authoring inputs and tools:
.venv/bin/python tools/dashboard.py --port 8091
```

The dashboard is local-only. Its worker preflight requires a configured OpenCode V2/Ensemble environment. Installing dependencies does not authorize generation or start a queue.

## Project map

| Directory | Purpose |
| --- | --- |
| `lesson_factory/` | Family contracts, validation, publication and dashboard logic |
| `tools/` | CLI helpers, syntax policies and preview tools |
| `templates/` | Structured briefs, reviews and quiz examples |
| `tests/` | Offline regression and synthetic-fixture test code |
| `.opencode/` | Authoring roles, skills and commands |

**Stack:** Python · SQLite · Node.js · NiceGUI · OpenCode-assisted workflows.

## The CherryLex projects

[CherryLexVocabulary](https://github.com/mkizilov/CherryLexVocabulary) owns vocabulary authoring. [CherryLexApp](https://github.com/mkizilov/CherryLexApp) packages finalized lesson families and delivers the learner experience. This project does not implement the app's scheduler or generate media.

See the [roadmap](ROADMAP.md) for the next engineering work.

## License and provenance

Original source is [MIT licensed](LICENSE); external corpora and tools retain their own licenses. See [third-party notices](THIRD_PARTY_NOTICES.md).

The first Git commit is a source snapshot of the existing standalone CherryLexLessons project, published on 19 October 2026. The live authoring directory had no Git history and was left untouched while a worker was active. This is a new publication of existing work—not a claim of a long public commit history. Its earlier development context belongs to the [CherryLex project](https://github.com/mkizilov/CherryLex).
