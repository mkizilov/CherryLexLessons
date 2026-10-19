"""Opt-in immutable, non-production correction previews; no registry/history adoption."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from . import core, family

Error = core.AuthoringError
SCHEMA = "cherrylex-correction-preview/1"
VALIDATION_SCOPE = "own-family stages and protected contracts only; no external-history, linguistic or production approval"
TOOLS = ("tools/render-preview.cjs", "tools/content-contract.cjs", "tools/lesson-policy.cjs", "tools/quiz-policy.cjs", "reference/formats/kit/gmd.js", "reference/formats/kit/qmd.js", "reference/formats/grammar/conventions.json")


def _need(ok, message):
    if not ok:
        raise Error(message)


def _hash(value):
    return hashlib.sha256(core.canonical(value)).hexdigest()


def _path(root, relative):
    _need(isinstance(relative, str) and relative and not any(p in ("", ".", "..") for p in relative.split("/")) and "\\" not in relative, "Non-exact relative path")
    return core.safe_path(root, relative)


def _read(root, relative, *, check_only=False, limit=64 * 1024 * 1024):
    _path(root, relative)
    fd = os.open(Path(root).resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split("/")
        for index, part in enumerate(parts):
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if index < len(parts) - 1 else os.O_NONBLOCK), dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_size <= limit, f"Invalid/oversized input: {relative}")
        if check_only:
            return
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(limit + 1)
        _need(len(data) <= limit, "Input grew beyond limit")
        return data
    finally:
        os.close(fd)


def _ref(root, ref):
    _need(isinstance(ref, dict) and isinstance(ref.get("sha256"), str) and re.fullmatch("[0-9a-f]{64}", ref["sha256"]), "Exact SHA256 reference required")
    data = _read(root, ref.get("path"))
    _need(hashlib.sha256(data).hexdigest() == ref["sha256"], f"Input drift: {ref['path']}")
    return data


def _identity(relative):
    _need(isinstance(relative, str) and relative.startswith("family_packs/"), "Only original family_packs permitted")
    return family.family_identity(relative.replace("family_packs/", "family_runs/", 1))


def _members(code):
    return {f"{locale}/stage-{stage}/{name}" for locale in core.learner_locales(code) for stage in range(5) for name in ("lesson.md", "quiz.md", "adaptation-notes.json")}


def _preflight_original(root, relative, manifest):
    # The legacy verifier directly reads context, content, briefs, reviews and
    # shared findings, then hashes pinned dependencies (including the grammar DB).
    code, _ = _identity(relative)
    required = _members(code) | {f"stage-{n}/brief.json" for n in range(5)} | {"family-context.json", "shared-findings.json"} | {f"reviews/{role.replace(':', '-')}.json" for role in core.review_roles(code) + ["final"]}
    _need(isinstance(manifest.get("files"), dict) and isinstance(manifest.get("dependencies"), dict), "Invalid original inventories")
    for name in set(manifest["files"]) | required:
        _read(root, f"{relative}/{name}", check_only=True)
    for name in manifest["dependencies"]:
        _read(root, name, check_only=True, limit=512 * 1024 * 1024)


def _bounded_proof(proof):
    _need(type(proof) is dict and set(proof) == {"ok", "scope", "stages"} and proof["ok"] is True and type(proof["scope"]) is str and proof["scope"] == VALIDATION_SCOPE and type(proof["stages"]) is int and proof["stages"] == 55, "Invalid bounded validation proof")


# Reuse current parsers/contracts, but deliberately omit external-history claims.
VALIDATE = r"""
const fs=require('node:fs'), p=JSON.parse(fs.readFileSync(0,'utf8'));
const {renderPreview}=require('./tools/render-preview.cjs');
const {contractFor,virtualFamily,same}=require('./tools/content-contract.cjs');
function contracts(files,locale) {
 const previous=[], banks=new Set(), ids=new Set(), contracts=[];
 for(let stage=0;stage<5;stage++) {
  const mode=n=>n?p.briefs[n].extension.mode:'base';
  const lesson=n=>files[`${locale}/stage-${n}/lesson.md`];
  const r=renderPreview({language:p.code,locale,stage,mode:mode(stage),lesson_md:lesson(stage),quiz_md:files[`${locale}/stage-${stage}/quiz.md`],option_text_policies:p.briefs[stage].option_text_policies||{},prior_lessons:Array.from({length:stage},(_,n)=>({stage:n,mode:mode(n),lesson_md:lesson(n)}))});
  if(!r.ok) throw Error(JSON.stringify(r.issues));
  const bank=r.bank, current=r.members[stage].semantic;
  if(bank.lang!==p.code || bank.explained_in!==locale || bank.questions.length<25 || banks.has(bank.id)) throw Error('Bank identity/count/duplicate');
  banks.add(bank.id);
  for(const q of bank.questions) {if(ids.has(q.id)) throw Error('Duplicate family question'); ids.add(q.id);}
  const event={base_checkpoint:p.base,checkpoint:p.base+(stage?[25,75,125,225][stage-1]:0),stage};
  const records=[...previous,{...event,lesson:current}], composed=virtualFamily(records);
  if(bank.lesson!==composed.id) throw Error('Quiz must link base lesson');
  contracts.push(contractFor(current,bank,composed,records,[],p.briefs[stage],p.code));
  previous.push({...event,lesson:current});
 }
 return contracts;
}
try {
 const effective={};
 for(const locale of p.locales) {
  const before=contracts(p.original,locale), after=contracts(p.effective,locale);
  if(!same(before,after)) throw Error(`Protected contract changed: ${locale}`);
  effective[locale]=after;
 }
 for(const locale of p.locales) if(!same(effective[p.source],effective[locale])) throw Error(`Source/locale protected mismatch: ${locale}`);
 process.stdout.write(JSON.stringify({ok:true,scope:'own-family stages and protected contracts only; no external-history, linguistic or production approval',stages:p.locales.length*5}));
} catch(e) {process.stdout.write(JSON.stringify({ok:false,error:e.message}));process.exitCode=1;}
"""


def _validate(root, relative, original, effective):
    code, base = _identity(relative)
    briefs = [json.loads(original[f"stage-{n}/brief.json"]) for n in range(5)]
    for name in _members(code):
        if name.endswith("adaptation-notes.json"):
            notes = json.loads(effective[name])
            _need(isinstance(notes, list) and all(isinstance(s, str) for s in notes), "Notes must be string arrays")
    payload = dict(code=code, base=base, source=core.source_locale(code), locales=core.learner_locales(code), briefs=briefs, original={k: v.decode("utf-8") for k, v in original.items() if k in _members(code)}, effective={k: v.decode("utf-8") for k, v in effective.items()})
    process = subprocess.run([os.environ.get("NODE_BINARY", "node"), "--max-old-space-size=256", "-e", VALIDATE], input=core.canonical(payload), capture_output=True, timeout=120, cwd=root)
    _need(len(process.stdout) < 1024 * 1024, "Oversized validator output")
    report = json.loads(process.stdout)
    _need(process.returncode == 0, f"Bounded validation failed: {report}")
    _bounded_proof(report)
    return report


def _plan(root, spec):
    _need(spec.get("schema") == SCHEMA and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", spec.get("edition_id", "")), "Invalid spec identity")
    _need(isinstance(spec.get("families"), dict) and spec["families"] and isinstance(spec.get("replacements"), list) and spec["replacements"], "Explicit families and replacements required")
    origins, payload, originals = {}, {}, {}
    for relative, pins in sorted(spec["families"].items()):
        code, _ = _identity(relative)
        _read(root, "family-registry.sqlite", check_only=True)
        rows = [r for r in family.family_registry_facts(root, code) if r["path"] == relative]
        _need(len(rows) == 1, "Original must be registered")
        raw = _read(root, f"{relative}/manifest.json")
        manifest = json.loads(raw)
        _need(type(manifest) is dict and _hash(manifest) == rows[0]["manifest_sha256"], "Original manifest differs from registry")
        _preflight_original(root, relative, manifest)
        verified = family.verify_family_pack(root, rows[0])
        _need(_read(root, f"{relative}/manifest.json") == raw and core.canonical(verified) == core.canonical(manifest), "Manifest changed during verification")
        expected = dict(manifest_raw_sha256=hashlib.sha256(raw).hexdigest(), manifest_canonical_sha256=_hash(manifest), context_hash=manifest["context_hash"])
        _need(pins == expected, f"Original identity drift: {relative}")
        origins[relative] = dict(**expected, original_files=manifest["files"])
        originals[relative] = {name: _read(root, f"{relative}/{name}") for name in _members(code) | {f"stage-{n}/brief.json" for n in range(5)}}
        _need(all(hashlib.sha256(data).hexdigest() == manifest["files"][name] for name, data in originals[relative].items()), "Original changed during read")
        payload[relative] = {name: originals[relative][name] for name in _members(code)}
    changes = {}
    for change in spec["replacements"]:
        relative, name = change.get("family"), change.get("file")
        _need(relative in payload and name in payload[relative], "Replacement outside explicit content allowlist")
        key = f"{relative}/{name}"
        _need(key not in changes, "Duplicate/conflicting replacement")
        _need(name.split("/")[0] != core.source_locale(_identity(relative)[0]), "Source corrections require a separate impact proposal; first preview route is locale-only")
        _need(all(isinstance(change.get(k), str) and change[k].strip() for k in ("reason", "author_session", "propagation")) and isinstance(change.get("anchors"), list) and change["anchors"] and all(isinstance(s, str) and s.strip() for s in change["anchors"]), "Correction attribution/scope required")
        _need(isinstance(change.get("evidence"), list) and change["evidence"], "Patch evidence required")
        for ref in change["evidence"]:
            _ref(root, ref)
        data = _ref(root, change["candidate"])
        _need(data != payload[relative][name], "Replacement must change bytes")
        payload[relative][name] = data
        changes[key] = change
    _need(set(spec["families"]) == {v["family"] for v in changes.values()}, "No untouched family may be selected")
    files = {f"{relative}/{name}": dict(original_sha256=hashlib.sha256(originals[relative][name]).hexdigest(), effective_sha256=hashlib.sha256(data).hexdigest()) for relative, members in sorted(payload.items()) for name, data in sorted(members.items())}
    inventory = dict(origins=origins, files=files, replacements=changes)
    return inventory, payload, originals


def _reviews(root, spec, inventory, draft):
    binding, changed = _hash(inventory), set(inventory["replacements"])
    reports, covered, sessions, report_ids = [], set(), set(), set()
    refs = spec.get("reviews", [])
    _need(isinstance(refs, list), "Explicit review reference list required")
    for ref in refs + ([spec["integration"]] if spec.get("integration") else []):
        report = json.loads(_ref(root, ref))
        _need(report.get("report_id") == ref.get("report_id") and report.get("report_id") and report["report_id"] not in report_ids, "Unique exact report ID required")
        report_ids.add(report["report_id"])
        _need(report.get("inventory_sha256") == binding and report.get("inventions") == "none" and isinstance(report.get("reviewer_session"), str) and report["reviewer_session"].strip(), "Current binding, actual reviewer SID and inventions:none required")
        scope = report.get("scope")
        _need(isinstance(scope, list) and scope and len(scope) == len(set(scope)) and set(scope) <= changed, "Report scope must name exact changed files")
        _need(report.get("verdict") in ("approved", "hold", "rejected"), "Explicit verdict required; prose is not approval")
        _need(isinstance(report.get("evidence"), list) and report["evidence"], "Current receipt must reference real report evidence")
        for evidence in report["evidence"]:
            _ref(root, evidence)
        if ref is spec.get("integration"):
            _need(report.get("role") == "integration" and set(scope) == changed and report.get("reviewed_reports") == refs, "Integration must bind all changes and exact independent reports")
        else:
            _need(all(report.get("role") == "language:" + inventory["replacements"][key]["file"].split("/")[0] and report["reviewer_session"] != inventory["replacements"][key]["author_session"] for key in scope), "Independent affected-locale review required")
            sessions.add(report["reviewer_session"])
            if report["verdict"] == "approved":
                covered.update(scope)
        reports.append(dict(reference=ref, receipt=report))
    if not draft:
        _need(covered == changed and spec.get("integration") and reports[-1]["receipt"]["verdict"] == "approved", "Reviewed corrections and integration required (or explicit --draft)")
        _need(reports[-1]["receipt"]["reviewer_session"] not in sessions, "Integration must not impersonate independent review")
        _need(all(r["receipt"]["verdict"] == "approved" for r in reports), "Unresolved report blocks reviewed preview")
    return reports


def preview(root, spec_ref, *, draft=False, validate=True):
    """Read-only plan; inventory hash is the noncircular binding for new reviews."""
    spec = json.loads(_ref(root, spec_ref))
    inventory, payload, originals = _plan(root, spec)
    reports = _reviews(root, spec, inventory, draft)
    tools = {name: hashlib.sha256(_read(root, name)).hexdigest() for name in TOOLS}
    validation = {relative: _validate(root, relative, originals[relative], members) for relative, members in payload.items()} if validate else {}
    _need(tools == {name: hashlib.sha256(_read(root, name)).hexdigest() for name in TOOLS}, "Validator tool drift")
    return dict(schema=SCHEMA, edition_id=spec["edition_id"], production_approved=False, review_status="draft" if draft else "scoped-reviewed", spec=spec_ref, inventory=inventory, inventory_sha256=_hash(inventory), reports=reports, validation=validation, tool_sha256=tools, validator_sha256=hashlib.sha256(VALIDATE.encode()).hexdigest())


def _tree(root, relative, allowed):
    folder = _path(root, relative)
    expected_dirs = {str(p) for name in allowed for p in Path(name).parents if str(p) != "."}
    actual, dirs = set(), set()
    for current, children, files in os.walk(folder, followlinks=False):
        for name in children + files:
            path = Path(current) / name
            mode = path.lstat().st_mode
            _need(not path.is_symlink() and (stat.S_ISDIR(mode) or stat.S_ISREG(mode)), "Symlink/special edition object")
            key = path.relative_to(folder).as_posix()
            (dirs if stat.S_ISDIR(mode) else actual).add(key)
    _need(actual == set(allowed) and dirs == expected_dirs, "Missing/unlisted edition files or directories")


def verify(root, edition_id):
    _need(isinstance(edition_id, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", edition_id), "Invalid edition ID")
    relative = f"work/correction-editions/{edition_id}"
    raw = _read(root, f"{relative}/receipt.json")
    receipt = json.loads(raw)
    _need(raw == core.canonical(receipt) + b"\n", "Receipt bytes are not canonical")
    digest = receipt.pop("receipt_sha256", None)
    _need(digest == _hash(receipt) and receipt.get("edition_id") == edition_id, "Receipt drift/identity")
    _need(receipt.get("production_approved") is False, "Preview production_approved must be literal false")
    _need(receipt.get("review_status") in ("draft", "scoped-reviewed"), "Invalid preview status")
    current = preview(root, receipt["spec"], draft=receipt["review_status"] == "draft", validate=False)
    _need(set(receipt) == set(current) and all(core.canonical(receipt[k]) == core.canonical(v) for k, v in current.items() if k != "validation"), "Receipt/input drift")
    _need(type(receipt["validation"]) is dict and set(receipt["validation"]) == set(current["inventory"]["origins"]), "Missing validation receipt")
    for proof in receipt["validation"].values():
        _bounded_proof(proof)
    allowed = {f"effective/{name}" for name in current["inventory"]["files"]} | {"receipt.json"}
    _tree(root, relative, allowed)
    for name, pins in current["inventory"]["files"].items():
        _need(hashlib.sha256(_read(root, f"{relative}/effective/{name}")).hexdigest() == pins["effective_sha256"], f"Effective payload drift: {name}")
    return dict(receipt, receipt_sha256=digest)


def resolution_map(root, edition_id, family_relative):
    """Exact preview read map: content from edition, five briefs from pinned original."""
    receipt = verify(root, edition_id)
    _need(family_relative in receipt["inventory"]["origins"], "Family not selected; no silent original fallback")
    result = {name: _path(root, f"work/correction-editions/{edition_id}/effective/{name}") for name in receipt["inventory"]["files"] if name.startswith(family_relative + "/")}
    result.update({f"{family_relative}/stage-{n}/brief.json": _path(root, f"{family_relative}/stage-{n}/brief.json") for n in range(5)})
    return result


def resolver(root, edition_id, family_relative):
    resolution_map(root, edition_id, family_relative)
    return _path(root, f"work/correction-editions/{edition_id}/effective/{family_relative}")


def _publish(source, destination):
    """Kernel-exclusive atomic directory rename, never check-then-replace."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        call, args = getattr(libc, "renamex_np", None), (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        call, args = getattr(libc, "renameat2", None), (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise Error("Exclusive directory rename unavailable; publication refused")
    _need(call is not None, "Exclusive rename unavailable")
    if call(*args):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def build(root, spec_ref, *, draft=False):
    receipt = preview(root, spec_ref, draft=draft)
    spec = json.loads(_ref(root, spec_ref))
    inventory, payload, _ = _plan(root, spec)
    _need(inventory == receipt["inventory"], "Inputs drifted during validation")
    parent = _path(root, "work/correction-editions")
    parent.mkdir(parents=True, exist_ok=True)
    destination = _path(root, f"work/correction-editions/{receipt['edition_id']}")
    temporary = Path(tempfile.mkdtemp(prefix=".edition-", dir=parent))
    try:
        for relative, members in payload.items():
            for name, data in members.items():
                path = temporary / "effective" / relative / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        receipt["receipt_sha256"] = _hash(receipt)
        (temporary / "receipt.json").write_bytes(core.canonical(receipt) + b"\n")
        # Recheck references and originals immediately before atomic exposure.
        check = preview(root, spec_ref, draft=draft, validate=False)
        _need(all(receipt[k] == v for k, v in check.items() if k != "validation"), "Prepublication drift")
        for path in temporary.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        temporary.chmod(0o555)
        _publish(temporary, destination)
        return receipt
    finally:
        if temporary.exists():
            temporary.chmod(0o700)
            for path in temporary.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            shutil.rmtree(temporary)
