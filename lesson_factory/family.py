import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import core

AuthoringError = core.AuthoringError
STAGES = tuple(range(5))


def family_identity(run):
    match = re.fullmatch(r"family_runs/(" + "|".join(core.LOCALES) + r")/([0-9]{6})", str(run))
    if not match:
        raise AuthoringError("Family run must be family_runs/<study>/<six-digit-base>")
    core.stage_identity(int(match[2]))
    return match[1], int(match[2])


def family_order(through):
    if isinstance(through, bool) or not isinstance(through, int) or not 1 <= through <= 999999:
        raise AuthoringError("Through must be a positive integer below 1000000")
    result = []
    for start in range(0, through, 200):
        for wave, offset in ((1, 20), (2, 10)):
            for base in range(start + offset, min(start + 200, 999990) + 1, 20):
                if base <= through:
                    result.append({"base_checkpoint": base, "authoring": {"block": start + 200, "wave": wave, "order": start // 10 + (base - start + (10 if wave == 2 else 0)) // 20 + (10 if wave == 2 else 0)}})
    return result


def family_path(root, run):
    family_identity(run)
    return core.safe_path(root, run)


def family_locale_path(root, run, locale, stage=None):
    code, _ = family_identity(run)
    if locale not in core.learner_locales(code):
        raise AuthoringError("Unknown learner locale or same-language pair")
    relative = f"{run}/{locale}"
    if stage is not None:
        core.stage_identity(10, stage)
        relative += f"/stage-{stage}"
    return core.safe_path(root, relative)


def _hash(value):
    return hashlib.sha256(core.canonical(value)).hexdigest()


@contextmanager
def family_registry(root):
    connection = sqlite3.connect(core.safe_path(root, "family-registry.sqlite"))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS families (language TEXT NOT NULL, base_checkpoint INTEGER NOT NULL, path TEXT NOT NULL UNIQUE, context_hash TEXT NOT NULL, manifest TEXT NOT NULL, PRIMARY KEY(language,base_checkpoint))")
        with connection:
            yield connection
    finally:
        connection.close()


def family_registry_facts(root, code):
    path = core.safe_path(root, "family-registry.sqlite")
    if not path.exists():
        return []
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "families" not in tables:
            raise AuthoringError("Invalid family registry")
        facts = []
        for row in connection.execute("SELECT * FROM families WHERE language=?", (code,)):
            fact = dict(row)
            core.stage_identity(fact["base_checkpoint"])
            if fact["path"] != f"family_packs/{code}/{fact['base_checkpoint']:06d}":
                raise AuthoringError("Registered family path mismatch")
            fact["manifest_sha256"] = _hash(json.loads(fact.pop("manifest")))
            facts.append(fact)
        order = {item["base_checkpoint"]: index for index, item in enumerate(family_order(max([20] + [row["base_checkpoint"] for row in facts])))}
        return sorted(facts, key=lambda row: order[row["base_checkpoint"]])
    finally:
        connection.close()


def _prefix(facts, base):
    horizon = max([200, base] + [row["base_checkpoint"] for row in facts])
    order = [item["base_checkpoint"] for item in family_order(min(999999, ((horizon + 199) // 200) * 200))]
    bases = [row["base_checkpoint"] for row in facts]
    if bases != order[:len(bases)]:
        raise AuthoringError("Family registry is not a finalized authoring prefix")
    if order.index(base) != len(facts):
        raise AuthoringError("All earlier lesson families must be finalized first")
    return facts


def _inventory(root, folder):
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise AuthoringError("Symlinks are forbidden in family state")
        if not path.is_file() and not path.is_dir():
            raise AuthoringError("Special filesystem objects are forbidden in family state")
        if path.is_file():
            relative = path.relative_to(folder).as_posix()
            files[relative] = core.digest(core.safe_path(root, path.relative_to(Path(root).resolve()).as_posix()))
    return files


def _validate_inventory(code, files):
    expected = {f"{locale}/stage-{stage}/{name}" for locale in core.learner_locales(code) for stage in STAGES for name in ("lesson.md", "quiz.md", "adaptation-notes.json")}
    actual = {name for name in files if name.rsplit("/", 1)[-1] in ("lesson.md", "quiz.md", "adaptation-notes.json")}
    if actual != expected or any(name.split("/", 1)[0] == code for name in files):
        raise AuthoringError("Family learner inventory must contain exactly eleven locales by five stages")
    briefs = {name for name in files if name.rsplit("/", 1)[-1] == "brief.json"}
    if briefs != {f"stage-{stage}/brief.json" for stage in STAGES}:
        raise AuthoringError("Family must contain exactly five stage briefs")
    for name in files:
        parts = name.split("/")
        if parts[0] in core.LOCALES and (len(parts) != 3 or parts[1] not in {f"stage-{stage}" for stage in STAGES} or parts[2] not in ("lesson.md", "quiz.md", "adaptation-notes.json")):
            raise AuthoringError("Unexpected or nested learner artifact")


def verify_family_pack(root, row):
    folder = core.safe_path(root, row["path"])
    manifest = core.read_json(folder / "manifest.json")
    if _hash(manifest) != row["manifest_sha256"] or manifest.get("context_hash") != row["context_hash"]:
        raise AuthoringError("Family manifest differs from committed registry receipt")
    _verify_manifest(root, row["path"], manifest)
    if manifest["study_language"] != row["language"] or manifest["base_checkpoint"] != row["base_checkpoint"]:
        raise AuthoringError("Family manifest identity differs from registry")
    return manifest


def _verify_manifest(root, relative, manifest):
    folder = core.safe_path(root, relative)
    code, base = family_identity(relative.replace("family_packs/", "family_runs/", 1))
    if manifest.get("schema") != 5 or manifest.get("study_language") != code or manifest.get("base_checkpoint") != base or manifest.get("source_locale") != core.source_locale(code) or manifest.get("locales") != list(core.learner_locales(code)) or manifest.get("family_path") != f"family_packs/{code}/{base:06d}":
        raise AuthoringError("Invalid schema5 family manifest identity")
    if (folder / code).exists():
        raise AuthoringError("Same-language learner directory is forbidden")
    files = _inventory(root, folder)
    files.pop("manifest.json", None)
    if files != manifest.get("files"):
        raise AuthoringError("Finalized family file inventory or bytes changed")
    _validate_inventory(code, files)
    context = core.read_json(folder / "family-context.json")
    if context.get("context_hash") != manifest.get("context_hash") or _hash({key: value for key, value in context.items() if key != "context_hash"}) != context.get("context_hash"):
        raise AuthoringError("Invalid immutable family context")
    if not isinstance(context.get("registry_facts"), dict) or set(context["registry_facts"]) != {"families"} or not isinstance(context["registry_facts"]["families"], list):
        raise AuthoringError("Family receipt requires families-only registry facts")
    if manifest.get("registry_facts") != context.get("registry_facts") or manifest.get("dependencies") != context.get("dependencies"):
        raise AuthoringError("Family receipt dependency mismatch")
    if _dependency_paths(root, code, context["registry_facts"]["families"]) != set(context["dependencies"]):
        raise AuthoringError("Stale immutable family dependency inventory")
    for path, expected in context["dependencies"].items():
        if core.digest(core.safe_path(root, path)) != expected:
            raise AuthoringError(f"Stale family dependency: {path}")
    if manifest.get("locale_digests") != {locale: _folder_digests(folder, locale) for locale in context["locales"]}:
        raise AuthoringError("Family receipt locale digest mismatch")
    if manifest.get("stages") != context.get("stages") or manifest.get("return_offsets") != list(core.RETURN_OFFSETS):
        raise AuthoringError("Family stage receipt mismatch")
    _verify_receipt_reviews(folder, context)
    return manifest


def _folder_digests(folder, locale):
    result = {}
    for stage in STAGES:
        hasher = hashlib.sha256()
        for name in ("lesson.md", "quiz.md", "adaptation-notes.json"):
            data = (folder / locale / f"stage-{stage}" / name).read_bytes()
            hasher.update(len(data).to_bytes(8, "big"))
            hasher.update(data)
        result[f"stage:{stage}"] = hasher.hexdigest()
    return result


def _verify_receipt_reviews(folder, context):
    binding = {"context_hash": context["context_hash"], "brief_digests": {f"stage:{stage}": core.digest(folder / f"stage-{stage}/brief.json") for stage in STAGES}, "source_digests": _folder_digests(folder, context["source_locale"])}
    roles = core.review_roles(context["study_language"])
    for role in roles + ["final"]:
        expected = dict(binding)
        if role.startswith("language:"):
            expected["locale_digests"] = _folder_digests(folder, role.split(":", 1)[1])
        if role == "final":
            expected.update({"locale_digests": {locale: _folder_digests(folder, locale) for locale in context["locales"]}, "findings_digest": core.digest(folder / "shared-findings.json"), "reviews_digest": _hash({review: core.digest(folder / "reviews" / (review.replace(":", "-") + ".json")) for review in roles})})
        payload = core.read_json(folder / "reviews" / (role.replace(":", "-") + ".json"))
        if payload.get("role") != role or payload.get("status") != "approved" or payload.get("binding") != expected or not payload.get("reviewer_session") or not payload.get("checklist") or not payload.get("findings") or not isinstance(payload.get("stages"), list) or [item.get("stage") for item in payload["stages"]] != list(STAGES):
            raise AuthoringError(f"Invalid family committed review receipt: {role}")


def _dependency_paths(root, code, families):
    scopes = [core.safe_path(root, f"reference/vocabulary/{core.LANGUAGES[code]}"), core.safe_path(root, "reference/formats")]
    scopes.extend(core.safe_path(root, row["path"]) for row in families)
    paths = set()
    for scope in scopes:
        for path in scope.rglob("*"):
            if path.is_symlink():
                raise AuthoringError("Symlink dependency is forbidden")
            if path.is_file():
                paths.add(path.relative_to(Path(root).resolve()).as_posix())
    paths.add("reference/grammar/grammar.sqlite")
    if core.safe_path(root, "reference/manifest.json").exists():
        paths.add("reference/manifest.json")
    return paths


def _external_history(root, families):
    history = []
    for row in families:
        manifest = verify_family_pack(root, row)
        source = manifest["source_locale"]
        folder = core.safe_path(root, row["path"])
        for stage in STAGES:
            identity = core.stage_identity(row["base_checkpoint"], stage)
            history.append({**identity, "key": f"{row['path']}#stage:{stage}", "path": row["path"], "source_locale": source, "manifest": core.read_json(folder / f"stage-{stage}/brief.json"), "lesson": (folder / source / f"stage-{stage}/lesson.md").read_text(encoding="utf-8"), "quiz": (folder / source / f"stage-{stage}/quiz.md").read_text(encoding="utf-8")})
    return sorted(history, key=core.event_key)


def prepare_family(root, language, base_checkpoint, *, lock_timeout=0):
    root = Path(root).resolve()
    code = core.language_code(language)
    core.stage_identity(base_checkpoint)
    run = f"family_runs/{code}/{base_checkpoint:06d}"
    words = core.vocabulary(root, code)
    if len(words) < base_checkpoint + 225:
        raise AuthoringError(f"Family blocked: full vocabulary horizon {base_checkpoint + 225} required, only {len(words)} available")
    with core.mutation_lock(root, timeout=lock_timeout):
        destination = family_path(root, run)
        if destination.exists():
            load_family(root, run)
            return run
        families = family_registry_facts(root, code)
        _prefix(families, base_checkpoint)
        if core.safe_path(root, f"family_packs/{code}/{base_checkpoint:06d}").exists():
            raise AuthoringError("Unregistered family pack exists; finalize its original run to recover")
        history = _external_history(root, families)
        paths = _dependency_paths(root, code, families)
        dependencies = {path: core.digest(core.safe_path(root, path)) for path in sorted(paths)}
        snapshot = core.safe_path(root, "reference/manifest.json")
        if snapshot.exists():
            reference = core.read_json(snapshot).get("files", {})
            prefix = f"reference/vocabulary/{core.LANGUAGES[code]}/"
            expected = {path for path in reference if path.startswith(prefix) or path.startswith("reference/formats/") or path == "reference/grammar/grammar.sqlite"}
            actual = {path for path in paths if path.startswith("reference/") and path != "reference/manifest.json"}
            if actual != expected or any(reference.get(path) != dependencies[path] for path in actual):
                raise AuthoringError("Copied reference differs from frozen manifest")
        stages = []
        for stage in STAGES:
            event = core.stage_identity(base_checkpoint, stage)
            stages.append({**event, "new_word_range": core.word_range(event), "prior_external_keys": [record["key"] for record in history if core.event_key(record) < core.event_key(event)], "planning_external_keys": [record["key"] for record in history if core.event_key(record) >= core.event_key(event)], "prior_family_stages": [f"stage:{previous}" for previous in range(stage)]})
        context = {"schema": 5, "study_language": code, "base_checkpoint": base_checkpoint, "source_locale": core.source_locale(code), "locales": list(core.learner_locales(code)), "return_offsets": list(core.RETURN_OFFSETS), "family_path": f"family_packs/{code}/{base_checkpoint:06d}", "authoring": next(item["authoring"] for item in family_order(max(200, base_checkpoint)) if item["base_checkpoint"] == base_checkpoint), "vocabulary_horizon": base_checkpoint + 225, "words": words[:base_checkpoint + 225], "external_history": history, "stages": stages, "registry_facts": {"families": families}, "dependencies": dependencies, "reference_inventory": sorted(paths), "grammar_registry": {"path": "reference/grammar/grammar.sqlite", "sha256": dependencies["reference/grammar/grammar.sqlite"], "purpose": "Research corpus, not assumed learner knowledge"}, "knowledge_policy": "Only earlier learner events and explicitly taught earlier family stages are prerequisites; planning material is never known teaching."}
        context["context_hash"] = _hash(context)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".family-", dir=destination.parent))
        try:
            core.write_json(temporary / "family-context.json", context)
            core.write_json(temporary / "shared-findings.json", [])
            (temporary / "reviews").mkdir()
            for stage in STAGES:
                (temporary / f"stage-{stage}").mkdir()
                for locale in context["locales"]:
                    folder = temporary / locale / f"stage-{stage}"
                    folder.mkdir(parents=True)
                    if stage:
                        (folder / "lesson.md").write_text("", encoding="utf-8")
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return run


def _load_family(root, run, folder):
    code, base = family_identity(run)
    context = core.read_json(core.safe_path(root, (folder / "family-context.json").relative_to(Path(root).resolve()).as_posix()))
    if context.get("schema") != 5 or context.get("context_hash") != _hash({key: value for key, value in context.items() if key != "context_hash"}):
        raise AuthoringError("Family context snapshot was modified or has unsupported schema")
    if context.get("study_language") != code or context.get("base_checkpoint") != base or context.get("source_locale") != core.source_locale(code) or context.get("locales") != list(core.learner_locales(code)) or context.get("family_path") != f"family_packs/{code}/{base:06d}" or context.get("return_offsets") != list(core.RETURN_OFFSETS) or context.get("vocabulary_horizon") != base + 225:
        raise AuthoringError("Family context identity mismatch")
    families = family_registry_facts(root, code)
    families = [row for row in families if row["base_checkpoint"] != base]
    _prefix(families, base)
    if context.get("registry_facts") != {"families": families}:
        raise AuthoringError("Family registry history changed")
    paths = _dependency_paths(root, code, families)
    if paths != set(context.get("dependencies", {})) or sorted(paths) != context.get("reference_inventory"):
        raise AuthoringError("Stale family dependency inventory")
    for path, expected in context["dependencies"].items():
        if core.digest(core.safe_path(root, path)) != expected:
            raise AuthoringError(f"Stale family dependency: {path}")
    words = core.vocabulary(root, code)
    if len(words) < base + 225 or context.get("words") != words[:base + 225]:
        raise AuthoringError("Family vocabulary horizon mismatch")
    history = _external_history(root, families)
    if history != context.get("external_history"):
        raise AuthoringError("Family external history changed")
    for stage in STAGES:
        event = core.stage_identity(base, stage)
        expected = {**event, "new_word_range": core.word_range(event), "prior_external_keys": [record["key"] for record in history if core.event_key(record) < core.event_key(event)], "planning_external_keys": [record["key"] for record in history if core.event_key(record) >= core.event_key(event)], "prior_family_stages": [f"stage:{previous}" for previous in range(stage)]}
        if context.get("stages", [])[stage:stage + 1] != [expected]:
            raise AuthoringError("Family stage knowledge window mismatch")
    if len(context["stages"]) != 5 or context.get("authoring") != next(item["authoring"] for item in family_order(max(200, base)) if item["base_checkpoint"] == base):
        raise AuthoringError("Family authoring identity mismatch")
    return context


def load_family(root, family_run):
    return _load_family(root, family_run, family_path(root, family_run))


def _stage_context_loaded(root, family_run, stage, _context):
    core.stage_identity(10, stage)
    context = _context
    code, base = family_identity(family_run)
    if not isinstance(context, dict) or context.get("study_language") != code or context.get("base_checkpoint") != base:
        raise AuthoringError("Family context identity mismatch")
    identity = context["stages"][stage]
    records = {record["key"]: record for record in context["external_history"]}
    prior = [records[key] for key in identity["prior_external_keys"]]
    planning = [records[key] for key in identity["planning_external_keys"]]
    own = []
    for previous in range(stage):
        folder = family_locale_path(root, family_run, context["source_locale"], previous)
        own.append({**context["stages"][previous], "key": f"stage:{previous}", "path": f"stage:{previous}", "source_locale": context["source_locale"], "manifest": core.read_json(family_path(root, family_run) / f"stage-{previous}/brief.json"), "lesson": (folder / "lesson.md").read_text(encoding="utf-8"), "quiz": (folder / "quiz.md").read_text(encoding="utf-8")})
    return {**context, **identity, "words": context["words"][:identity["checkpoint"]], "new_words": context["words"][identity["new_word_range"][0] - 1:identity["checkpoint"]], "prior_packs": prior, "planning_packs": planning, "family_packs": own}


def stage_context(root, family_run, stage):
    return _stage_context_loaded(root, family_run, stage, load_family(root, family_run))


def _validate_family_briefs_loaded(root, run, _context):
    context = _context
    code, base = family_identity(run)
    if not isinstance(context, dict) or context.get("study_language") != code or context.get("base_checkpoint") != base:
        raise AuthoringError("Family context identity mismatch")
    briefs = []
    for stage in STAGES:
        brief = core.read_json(family_path(root, run) / f"stage-{stage}/brief.json")
        view = _stage_context_loaded(root, run, stage, _context)
        allowed = set(view["prior_external_keys"]) | set(view["prior_family_stages"])
        practice = False
        if stage:
            extension = brief.get("extension", {})
            if extension.get("mode") not in ("practice", "extend") or not isinstance(extension.get("rationale"), str) or not extension["rationale"].strip():
                raise AuthoringError("Return brief requires practice/extend and rationale")
            practice = extension["mode"] == "practice"
            lesson = (family_locale_path(root, run, context["source_locale"], stage) / "lesson.md").read_text(encoding="utf-8")
            if bool(lesson.strip()) == practice:
                raise AuthoringError("Extension mode does not match source lesson pages")
        for key in ("topic", "objective"):
            if not isinstance(brief.get(key), str) or not brief[key].strip():
                raise AuthoringError(f"Stage {stage} brief requires {key}")
        if not isinstance(brief.get("sources"), list) or not brief["sources"] or any(not isinstance(item, dict) or not isinstance(item.get("reference"), str) or not item["reference"].strip() or not isinstance(item.get("justification"), str) or not item["justification"].strip() for item in brief["sources"]):
            raise AuthoringError("Brief requires justified sources")
        if not isinstance(brief.get("prerequisites"), list) or any(not isinstance(item, dict) or item.get("source") not in allowed or not item.get("justification") for item in brief["prerequisites"]):
            raise AuthoringError("Prerequisites must cite earlier external keys or earlier stage:N")
        audit = brief.get("lexical_audit")
        if not isinstance(audit, list) or not audit:
            raise AuthoringError("Explicit lexical audit required for every stage")
        teaching = {record["key"]: record["lesson"] for record in view["prior_packs"] + view["family_packs"]}
        teaching["this"] = (family_locale_path(root, run, context["source_locale"], stage) / "lesson.md").read_text(encoding="utf-8")
        for item in audit:
            if not isinstance(item, dict) or not isinstance(item.get("target_form"), str) or not item["target_form"].strip() or not isinstance(item.get("justification"), str) or not item["justification"].strip():
                raise AuthoringError("Audit requires justified target form")
            taught = item.get("taught_in")
            if "taught_in" in item and (taught not in allowed | {"this"} or practice and taught == "this"):
                raise AuthoringError("Audit cannot cite future stages/planning or practice-only this")
            if "known_rank" in item:
                rank = item["known_rank"]
                if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= view["checkpoint"]:
                    raise AuthoringError("Audit known_rank outside stage vocabulary")
                lexical = [value for key, value in view["words"][rank - 1].items() if key.casefold() in ("word", "simplified", "traditional")]
                if item["target_form"] not in lexical:
                    raise AuthoringError("Known form must match canonical ranked vocabulary")
            elif taught not in teaching or item["target_form"] not in teaching[taught]:
                raise AuthoringError("Audited taught form absent from eligible cited lesson")
        briefs.append(brief)
    return briefs


def validate_family_briefs(root, run):
    return _validate_family_briefs_loaded(root, run, load_family(root, run))


def validate_family(root, family_run, locale=None, *, timeout=120, stop_event=None):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise AuthoringError("Family validator timeout must be positive finite seconds")
    deadline = time.monotonic() + timeout

    def interrupted():
        if stop_event is not None and stop_event.is_set():
            raise AuthoringError("Family validation cancelled; evidence preserved")
        if time.monotonic() >= deadline:
            raise AuthoringError("Family validation deadline exceeded; evidence preserved")

    interrupted()
    context = load_family(root, family_run)
    interrupted()
    locale = context["source_locale"] if locale is None else locale
    family_locale_path(root, family_run, locale)
    _validate_family_briefs_loaded(root, family_run, context)
    interrupted()
    process = None
    try:
        process = subprocess.Popen([os.environ.get("NODE_BINARY", "node"), str(core.safe_path(root, "tools/validate-family.cjs")), "--root", str(Path(root).resolve()), "--run", family_run, "--locale", locale], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while True:
            interrupted()
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
        interrupted()
    except OSError as exc:
        raise AuthoringError(f"Family validator failed; evidence preserved: {exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=1)
    try:
        report = json.loads(stdout)
    except ValueError as exc:
        raise AuthoringError(f"Family parser bridge failed: {stderr.strip()}") from exc
    if process.returncode or not report.get("ok"):
        raise AuthoringError(f"Invalid {locale} family: {json.dumps(report, ensure_ascii=False)}")
    return report


def family_locale_scaffold(root, run, locale):
    folder = family_locale_path(root, run, locale)
    if not folder.exists():
        return None
    directories = set()
    files = {}
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise AuthoringError("Locale scaffold contains a symlink")
        relative = path.relative_to(folder).as_posix()
        if path.is_dir():
            directories.add(relative)
        elif path.is_file():
            files[relative] = core.digest(path)
        else:
            raise AuthoringError("Unexpected locale scaffold object")
    expected = {f"stage-{stage}/lesson.md": hashlib.sha256(b"").hexdigest() for stage in range(1, 5)}
    if directories != {f"stage-{stage}" for stage in STAGES} or files != expected:
        raise AuthoringError("Locale is not an exact pristine generated family scaffold")
    return files


def family_pair_digests(root, run, locale):
    result = {}
    for stage in STAGES:
        hasher = hashlib.sha256()
        for name in ("lesson.md", "quiz.md", "adaptation-notes.json"):
            data = (family_locale_path(root, run, locale, stage) / name).read_bytes()
            hasher.update(len(data).to_bytes(8, "big"))
            hasher.update(data)
        result[f"stage:{stage}"] = hasher.hexdigest()
    return result


def family_review_path(root, run, role):
    code, _ = family_identity(run)
    if role not in core.review_roles(code) + ["final"]:
        raise AuthoringError("Unknown family review role")
    return core.safe_path(root, f"{run}/reviews/{role.replace(':', '-')}.json")


def family_binding(root, family_run, role):
    context = load_family(root, family_run)
    family_review_path(root, family_run, role)
    binding = {"context_hash": context["context_hash"], "brief_digests": {f"stage:{stage}": core.digest(family_path(root, family_run) / f"stage-{stage}/brief.json") for stage in STAGES}, "source_digests": family_pair_digests(root, family_run, context["source_locale"])}
    if role == "final":
        binding.update({"locale_digests": {locale: family_pair_digests(root, family_run, locale) for locale in context["locales"]}, "findings_digest": core.digest(family_path(root, family_run) / "shared-findings.json"), "reviews_digest": _hash({review: core.digest(family_review_path(root, family_run, review)) for review in core.review_roles(context["study_language"])})})
    elif role.startswith("language:"):
        binding["locale_digests"] = family_pair_digests(root, family_run, role.split(":", 1)[1])
    return binding


@contextmanager
def family_publication_lock(root, run):
    code, base = family_identity(run)
    handles = []
    try:
        for locale in core.translation_locales(code):
            path = core.safe_path(root, f"work/translations/families/{code}/{base:06d}/{locale}/worker.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AuthoringError(f"Family translation worker is active: {locale}") from exc
        yield
    finally:
        for handle in reversed(handles):
            handle.close()


def _question_ids(report, stage):
    try:
        return [question["id"] for question in report["stages"][stage]["contract"]["questions"]]
    except (KeyError, IndexError, TypeError) as exc:
        raise AuthoringError("Family validator must return actual per-stage question contracts") from exc


def _review_shape(root, run, role, payload, validation_report=None):
    if not isinstance(payload, dict) or payload.get("status") not in ("approved", "rejected") or not isinstance(payload.get("reviewer_session"), str) or not payload["reviewer_session"].strip():
        raise AuthoringError("Review requires status and independent reviewer session")
    for key in ("checklist", "findings"):
        if not isinstance(payload.get(key), list) or not payload[key]:
            raise AuthoringError("Review requires nonempty checklist and findings")
    stages = payload.get("stages")
    if not isinstance(stages, list) or len(stages) != 5 or [item.get("stage") if isinstance(item, dict) else None for item in stages] != list(STAGES):
        raise AuthoringError("Review must explicitly cover each of five stages in order")
    report = validation_report
    if report is None and (role == "assessment" or role.startswith("language:")):
        report = validate_family(root, run)
    for stage, item in enumerate(stages):
        if type(item["stage"]) is not int:
            raise AuthoringError("Review stage identities must be integers")
        if not isinstance(item.get("checklist"), list) or not item["checklist"] or not isinstance(item.get("findings"), list) or not item["findings"]:
            raise AuthoringError("Each review stage requires checklist and findings")
        if role == "assessment" or role.startswith("language:"):
            checks = item.get("question_checks")
            expected = _question_ids(report, stage)
            if not expected or len(expected) != len(set(expected)) or not isinstance(checks, list) or len(checks) != len(expected) or {check.get("id") for check in checks if isinstance(check, dict)} != set(expected):
                raise AuthoringError("Assessment/language review must check every actual stage question exactly once")
            if any(not isinstance(check, dict) or check.get("status") not in ("approved", "rejected") or not isinstance(check.get("rationale"), str) or not check["rationale"].strip() or payload["status"] == "approved" and check["status"] != "approved" for check in checks):
                raise AuthoringError("Question checks require explicit justified verdicts")
            if any(any(not isinstance(check.get(field), str) or not check[field].strip() for field in ("answer", "alternatives", "teaching_page")) for check in checks):
                raise AuthoringError("Question checks require answer, alternatives and teaching_page evidence")


def submit_family_review(root, family_run, role, payload, *, lock_timeout=0):
    with core.mutation_lock(root, timeout=lock_timeout), family_publication_lock(root, family_run):
        binding = family_binding(root, family_run, role)
        _review_shape(root, family_run, role, payload)
        if payload.get("binding") != binding:
            raise AuthoringError("Reviewer must supply exact current five-stage binding")
        result = {**payload, "role": role}
        core.write_json(family_review_path(root, family_run, role), result)
        return result


def require_family_review(root, run, role, *, validation_report=None):
    payload = core.read_json(family_review_path(root, run, role))
    _review_shape(root, run, role, payload, validation_report)
    if payload.get("role") != role or payload.get("status") != "approved" or payload.get("binding") != family_binding(root, run, role):
        raise AuthoringError(f"Missing, rejected or stale family review: {role}")
    return payload


def require_family_source_reviews(root, family_run, *, timeout=120, stop_event=None):
    started = time.monotonic()
    report = validate_family(root, family_run, timeout=timeout, stop_event=stop_event)
    result = []
    for role in ("pedagogical", "assessment", "format"):
        if stop_event is not None and stop_event.is_set():
            raise AuthoringError("Family source review gate cancelled; evidence preserved")
        if time.monotonic() - started >= timeout:
            raise AuthoringError("Family source review gate deadline exceeded; evidence preserved")
        result.append(require_family_review(root, family_run, role, validation_report=report))
    if stop_event is not None and stop_event.is_set():
        raise AuthoringError("Family source review gate cancelled; evidence preserved")
    if time.monotonic() - started >= timeout:
        raise AuthoringError("Family source review gate deadline exceeded; evidence preserved")
    return result


def _findings(root, run):
    code, _ = family_identity(run)
    data = core.read_json(family_path(root, run) / "shared-findings.json")
    if not isinstance(data, list):
        raise AuthoringError("Shared findings must be a list")
    for item in data:
        if not isinstance(item, dict) or item.get("status") != "resolved" or not isinstance(item.get("resolution"), str) or not item["resolution"].strip():
            raise AuthoringError("Shared findings require explicit resolutions")
        acknowledgments = item.get("locale_acknowledgments")
        if not isinstance(acknowledgments, dict) or set(acknowledgments) != set(core.translation_locales(code)) or any(not isinstance(reason, str) or not reason.strip() for reason in acknowledgments.values()):
            raise AuthoringError("Shared findings require all ten translated locale acknowledgments")
    return data


def _register(db, code, base, destination, manifest):
    db.execute("INSERT INTO families VALUES (?,?,?,?,?)", (code, base, destination, manifest["context_hash"], core.canonical(manifest).decode()))


def finalize_family(root, family_run, *, lock_timeout=0):
    root = Path(root).resolve()
    code, base = family_identity(family_run)
    destination_relative = f"family_packs/{code}/{base:06d}"
    destination = core.safe_path(root, destination_relative)
    source = family_path(root, family_run)
    with core.mutation_lock(root, timeout=lock_timeout), family_publication_lock(root, family_run):
        facts = family_registry_facts(root, code)
        existing = next((row for row in facts if row["base_checkpoint"] == base), None)
        if existing:
            if source.exists():
                raise AuthoringError("Registered pack and unfinished family both exist")
            verify_family_pack(root, existing)
            return destination_relative
        _prefix(facts, base)
        if destination.exists():
            if source.exists():
                raise AuthoringError("Recovery conflict: family run and pack both exist")
            manifest = core.read_json(destination / "manifest.json")
            receipt = core.read_json(core.safe_path(root, f"work/family-publications/{code}/{base:06d}/receipt.json"))
            if receipt != {"schema": 5, "run": family_run, "path": destination_relative, "manifest_sha256": _hash(manifest), "context_hash": manifest.get("context_hash"), "files": manifest.get("files"), "dependencies": manifest.get("dependencies"), "registry_facts": manifest.get("registry_facts")}:
                raise AuthoringError("Family recovery does not match validated publication receipt")
            _verify_manifest(root, destination_relative, manifest)
            _load_family(root, family_run, destination)
            with family_registry(root) as db:
                _register(db, code, base, destination_relative, manifest)
            return destination_relative
        context = load_family(root, family_run)
        if (source / code).exists():
            raise AuthoringError("Same-language learner directory is forbidden")
        files = _inventory(root, source)
        files.pop("manifest.json", None)
        _validate_inventory(code, files)
        require_family_source_reviews(root, family_run)
        baseline = validate_family(root, family_run, context["source_locale"])["family_contract"]
        for locale in core.translation_locales(code):
            if validate_family(root, family_run, locale)["family_contract"] != baseline:
                raise AuthoringError(f"Family meaning contract changed in {locale}")
            require_family_review(root, family_run, f"language:{locale}")
        _findings(root, family_run)
        require_family_review(root, family_run, "final")
        manifest = {"schema": 5, "study_language": code, "base_checkpoint": base, "source_locale": context["source_locale"], "locales": context["locales"], "return_offsets": context["return_offsets"], "family_path": destination_relative, "context_hash": context["context_hash"], "authoring": context["authoring"], "stages": context["stages"], "registry_facts": context["registry_facts"], "dependencies": context["dependencies"], "locale_digests": {locale: family_pair_digests(root, family_run, locale) for locale in context["locales"]}, "files": files}
        load_family(root, family_run)
        require_family_review(root, family_run, "final")
        current = _inventory(root, source)
        current.pop("manifest.json", None)
        if current != files:
            raise AuthoringError("Family changed during finalization")
        core.write_json(source / "manifest.json", manifest)
        core.write_json(core.safe_path(root, f"work/family-publications/{code}/{base:06d}/receipt.json"), {"schema": 5, "run": family_run, "path": destination_relative, "manifest_sha256": _hash(manifest), "context_hash": manifest["context_hash"], "files": manifest["files"], "dependencies": manifest["dependencies"], "registry_facts": manifest["registry_facts"]})
        destination.parent.mkdir(parents=True, exist_ok=True)
        with family_registry(root) as db:
            os.rename(source, destination)
            _register(db, code, base, destination_relative, manifest)
        return destination_relative


def family_plan(root, language, through):
    code = core.language_code(language)
    schedule = family_order(through)
    words = core.vocabulary(root, code)
    facts = family_registry_facts(root, code)
    indexed = {row["base_checkpoint"]: row for row in facts}
    horizon = max([through, 200] + [row["base_checkpoint"] for row in facts])
    order = family_order(min(999999, ((horizon + 199) // 200) * 200))
    bases = [item["base_checkpoint"] for item in order]
    prefix_valid = [row["base_checkpoint"] for row in facts] == bases[:len(facts)]
    candidate = bases[len(facts)] if prefix_valid and len(facts) < len(bases) else None
    entries = []
    next_eligible = None
    for item in schedule:
        base = item["base_checkpoint"]
        row = indexed.get(base)
        if row:
            verify_family_pack(root, row)
        entry = {**item, "run": f"family_runs/{code}/{base:06d}", "path": f"family_packs/{code}/{base:06d}", "vocabulary_horizon": base + 225, "status": "finalized" if row else "pending", "eligible": False}
        if not row:
            try:
                if not prefix_valid:
                    raise AuthoringError("Family registry is not a finalized authoring prefix")
                if len(words) < base + 225:
                    raise AuthoringError(f"Full vocabulary horizon {base + 225} required, only {len(words)} available")
                if candidate == base:
                    entry["eligible"] = True
                    next_eligible = {**item, "run": entry["run"]}
            except AuthoringError as exc:
                entry["blocked_reason"] = str(exc)
        entries.append(entry)
    return {"schema": 5, "study_language": code, "through": through, "vocabulary_count": len(words), "return_offsets": list(core.RETURN_OFFSETS), "families": entries, "next_eligible": next_eligible}
