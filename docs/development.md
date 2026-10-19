# Development

## Public source versus local authoring inputs

This repository publishes the lesson-authoring software. It does not include `reference/`, `prompts/`, `secrets/`, generated families, SQLite registries or operational journals. Templates and OpenCode role/skill instructions are public development source; the provider prompt library is excluded.

Python 3.11+ and Node 24+ are required. Run the portable check from this root:

```sh
python3 tools/check.py --source-only
python3 -m unittest discover -s tests -p 'test_public_source.py' -v
```

The source-only mode verifies source syntax, authoring/configuration structure and ignore protections. It does **not** validate missing reference hashes, credential permissions, live providers or content quality. The default `python3 tools/check.py` retains the full operational checks and fails when required inputs are absent.

The broader suite, `python3 -m unittest discover -s tests -v`, contains tests coupled to the frozen format parsers and private authoring inputs. A public-clone source check is not a substitute for that suite on a fully provisioned checkout. Do not manufacture reference hashes or copy credentials to make a test pass.

## Operational prerequisites

Full authoring requires an explicitly provisioned frozen `reference/manifest.json` and its exact vocabulary/format/grammar inputs, the local `prompts/localize-family.md`, and any privately configured service credentials. `tools/bootstrap.py` is a legacy importer for a fully provisioned CherryLex workspace, not a downloader or automatic clone setup. Its `--copy-secrets` option is sensitive and must never be run as part of a generic installation.

The dashboard additionally needs:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dashboard.txt
```

`tools/dashboard.py` verifies OpenCode V2 and the expected inherited Ensemble/agent configuration before opening its queue. The committed OpenCode configuration references absent `secrets/` files; configure services privately before enabling an operational workflow. No provider calls or queue launches are authorized by setup.

The detailed authoring/review contract is in `AGENTS.md`; recovery tooling is documented in `tools/RECOVERY.md`. Preserve immutable packs, request evidence, review bindings, retry limits and exclusive ownership of writers.

## Git, Kandev and the live workspace

The public repository starts from an isolated source snapshot taken on 19 October 2026. It is separate from the original non-Git live authoring directory, which was deliberately not altered while a worker was active.

Kandev may create ordinary source-code worktrees from this repository. Leave Copy Files empty for source checks; each worktree owns its local dependencies and state. Do not link live `work/`, family runs/packs, registries, secrets or reference data into writing agents.

**Concurrent production authoring in Git worktrees has not been validated.** Review Ensemble project/team isolation and run a non-model smoke test before launching authoring coordinators in a Git checkout. Do not move a live queue or resume an old job under a new path/environment merely because the software is now public.

After live jobs are stopped, reconcile the publication checkout with the operational directory under explicit maintenance ownership. Keep the original journal and session identities intact. The publication snapshot does not migrate running jobs.

## Contributions

Prefer small patches with synthetic regression tests. Do not include production prompts, lesson packs, third-party corpora, personal data or secrets. Keep paid generation and curriculum authoring separate from engineering validation.
