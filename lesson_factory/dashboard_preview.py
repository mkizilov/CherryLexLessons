import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from . import core


class PreviewError(core.AuthoringError):
    pass


class PreviewService:
    MAX_INPUT = 2 * 1024 * 1024
    MAX_FILE = 800_000
    MAX_OUTPUT = 8 * 1024 * 1024
    TIMEOUT = 10
    PATTERN = re.compile(r"(family_runs|family_packs)/(" + "|".join(core.LOCALES) + r")/([0-9]{6})")
    EDITION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')
    CORRECTION_PREFIX = 'correction-edition:'
    MAX_RECEIPT = 4 * 1024 * 1024
    MAX_EDITIONS = 64
    MAX_EDITION_FAMILIES = 64

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.code_root = Path(__file__).resolve().parents[1]

    def _path(self, relative):
        try:
            return core.safe_path(self.root, relative)
        except core.AuthoringError as exc:
            raise PreviewError(str(exc)) from exc

    def _identity(self, relative):
        match = self.PATTERN.fullmatch(str(relative))
        if not match:
            raise PreviewError('Preview path must be a family_runs or family_packs study/checkpoint directory')
        try:
            core.stage_identity(int(match[3]), 0)
        except core.AuthoringError as exc:
            raise PreviewError(str(exc)) from exc
        self._path(relative)
        return match

    def _read(self, relative, *, optional=False):
        path = self._path(relative)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.MAX_FILE:
                raise PreviewError(f'Preview file is not regular or exceeds {self.MAX_FILE} bytes: {relative}')
            with path.open('rb') as handle:
                data = handle.read(self.MAX_FILE + 1)
            if len(data) > self.MAX_FILE:
                raise PreviewError(f'Preview file exceeds size limit: {relative}')
            return data.decode('utf-8')
        except FileNotFoundError:
            if optional:
                return None
            raise PreviewError(f'Missing preview file: {relative}') from None
        except (OSError, UnicodeError) as exc:
            raise PreviewError(f'Cannot read preview file: {relative} ({type(exc).__name__})') from exc

    def catalog(self, *, include_corrections=False):
        result = []
        for kind in ('family_runs', 'family_packs'):
            for language in core.LOCALES:
                folder = self._path(f'{kind}/{language}')
                if not folder.is_dir():
                    continue
                for child in sorted(folder.iterdir(), key=lambda path: path.name):
                    relative = f'{kind}/{language}/{child.name}'
                    try:
                        match = self._identity(relative)
                        if not self._path(relative).is_dir():
                            continue
                    except PreviewError:
                        continue
                    locales = [locale for locale in core.LOCALES if locale != language and self._path(f'{relative}/{locale}').is_dir()]
                    stages = list(range(5))
                    state = 'draft' if kind.endswith('runs') else 'finalized'
                    label_state = 'original author draft' if state == 'draft' else 'original pack files'
                    result.append({'kind': state, 'language': language, 'base_checkpoint': int(match[3]), 'path': relative, 'relative': relative, 'run': relative, 'locales': locales, 'stages': stages, 'label': f'{language.upper()} · {int(match[3])} · {label_state}' + ' · five-stage family'})
        if include_corrections:
            result.extend(self._correction_catalog())
        return sorted(result, key=lambda item: (item['language'], item['base_checkpoint'], item['path']))

    def _correction_identity(self, relative):
        parts = str(relative).split(':')
        if len(parts) != 3 or parts[0] != 'correction-edition' or not self.EDITION_ID.fullmatch(parts[1]):
            raise PreviewError('Correction preview requires an exact edition ID and family_packs identity')
        match = self._identity(parts[2])
        if match[1] != 'family_packs':
            raise PreviewError('Correction preview only accepts original family_packs identities')
        return parts[1], parts[2], match

    def _bounded_bytes(self, relative, limit):
        """Correction-only descriptor walk: no symlink or special-file race on reads."""
        if not isinstance(relative, str) or not relative or '\\' in relative or any(part in ('', '.', '..') for part in relative.split('/')):
            raise PreviewError('Non-exact correction preview path')
        self._path(relative)
        fd = None
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parts = relative.split('/')
            for index, part in enumerate(parts):
                child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if index < len(parts) - 1 else os.O_NONBLOCK), dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise PreviewError(f'Correction preview input is not regular or exceeds {limit} bytes: {relative}')
            with os.fdopen(os.dup(fd), 'rb') as handle:
                data = handle.read(limit + 1)
            if len(data) > limit:
                raise PreviewError(f'Correction preview input exceeds {limit} bytes: {relative}')
            return data
        except OSError as exc:
            raise PreviewError(f'Cannot read correction preview input: {relative} ({type(exc).__name__})') from exc
        finally:
            if fd is not None:
                os.close(fd)

    def _receipt(self, edition_id):
        """Bounded receipt metadata, NOT edition verification or an approval claim."""
        raw = self._bounded_bytes(f'work/correction-editions/{edition_id}/receipt.json', self.MAX_RECEIPT)
        try:
            receipt = json.loads(raw)
            unsigned = dict(receipt)
            digest = unsigned.pop('receipt_sha256', None)
            origins = receipt['inventory']['origins']
            if (receipt.get('schema') != 'cherrylex-correction-preview/1' or
                    receipt.get('edition_id') != edition_id or receipt.get('production_approved') is not False or
                    receipt.get('review_status') not in ('draft', 'scoped-reviewed') or
                    not isinstance(origins, dict) or not 1 <= len(origins) <= self.MAX_EDITION_FAMILIES or
                    not isinstance(receipt['inventory']['files'], dict) or
                    raw != core.canonical(receipt) + b'\n' or
                    digest != hashlib.sha256(core.canonical(unsigned)).hexdigest()):
                raise ValueError('shape/digest')
            return receipt
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError) as exc:
            raise PreviewError(f'Invalid correction preview receipt: {edition_id}') from exc

    def _correction_catalog(self):
        folder = self._path('work/correction-editions')
        if not folder.exists():
            return []
        result, ids = [], []
        with os.scandir(folder) as entries:
            for entry in entries:
                # Private .edition-* build directories are never selectable.
                if entry.name.startswith('.'):
                    continue
                if not self.EDITION_ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                    raise PreviewError('Invalid correction edition directory; no entries were inferred')
                ids.append(entry.name)
                if len(ids) > self.MAX_EDITIONS:
                    raise PreviewError(f'Correction preview catalog exceeds {self.MAX_EDITIONS} editions')
        for edition_id in sorted(ids):
            receipt = self._receipt(edition_id)
            for relative in sorted(receipt['inventory']['origins']):
                identity = f'{self.CORRECTION_PREFIX}{edition_id}:{relative}'
                _, _, match = self._correction_identity(identity)
                language, base = match[2], int(match[3])
                result.append(dict(kind='correction', language=language, base_checkpoint=base,
                    path=identity, relative=identity, run=relative, edition_id=edition_id,
                    receipt_sha256=receipt['receipt_sha256'], review_status=receipt['review_status'],
                    locales=list(core.learner_locales(language)), stages=list(range(5)),
                    label=f'{language.upper()} · {base} · correction preview — unverified until render · {receipt["review_status"]} · {edition_id}'))
        return result

    def _correction_input(self, relative, locale, stage):
        from . import correction_edition
        edition_id, family_relative, match = self._correction_identity(relative)
        if locale not in core.learner_locales(match[2]):
            raise PreviewError('Unknown learner locale or excluded study-language learner pair')
        if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= 4:
            raise PreviewError('Preview stage must be an integer from 0 to 4')
        receipt = self._receipt(edition_id)
        # Exactly one full verification per call, owned by resolution_map.
        mapping = correction_edition.resolution_map(self.root, edition_id, family_relative)
        if self._receipt(edition_id) != receipt:
            raise PreviewError('Correction preview receipt changed during verification')
        origins = receipt['inventory']['origins']
        if family_relative not in origins:
            raise PreviewError('Family not selected in correction receipt; no original fallback')
        content = {f'{family_relative}/{code}/stage-{number}/{name}'
                   for code in core.learner_locales(match[2]) for number in range(5)
                   for name in ('lesson.md', 'quiz.md', 'adaptation-notes.json')}
        briefs = {f'{family_relative}/stage-{number}/brief.json' for number in range(5)}
        if set(mapping) != content | briefs:
            raise PreviewError('Correction preview requires exactly 165 content files and five original briefs')
        pins, paths = {}, {}
        try:
            for name in sorted(mapping):
                if name in content:
                    target = f'work/correction-editions/{edition_id}/effective/{name}'
                    pins[name] = receipt['inventory']['files'][name]['effective_sha256']
                else:
                    target = name
                    pins[name] = origins[family_relative]['original_files'][name[len(family_relative) + 1:]]
                if Path(mapping[name]) != self.root / target or not re.fullmatch('[0-9a-f]{64}', pins[name]):
                    raise ValueError('map/pin')
                paths[name] = target
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PreviewError('Correction preview read map or file pins are invalid') from exc
        view = _CorrectionInput(self, paths, pins)
        payload, issues = view._input(family_relative, locale, stage)
        metadata = dict(edition_id=edition_id, family_relative=family_relative,
                        receipt_sha256=receipt['receipt_sha256'], review_status=receipt['review_status'],
                        production_approved=False)
        return payload, issues, metadata

    def _mode(self, relative, stage, issues):
        if not stage:
            return 'base'
        filename = f'{relative}/stage-{stage}/brief.json'
        text = self._read(filename, optional=True)
        if text is None:
            issues.append({'level': 'error', 'source': filename, 'msg': 'Stage brief not written yet'})
            return None
        try:
            value = json.loads(text)
            mode = value.get('extension', {}).get('mode')
            if mode not in ('practice', 'extend'):
                raise ValueError('mode')
            return mode
        except (ValueError, AttributeError):
            issues.append({'level': 'error', 'source': filename, 'msg': 'Invalid JSON or missing brief.extension.mode (practice/extend)'})
            return None

    def _input(self, relative, locale, stage):
        match = self._identity(relative)
        if locale not in core.LOCALES or locale == match[2]:
            raise PreviewError('Unknown learner locale or excluded study-language learner pair')
        if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= 4:
            raise PreviewError('Preview stage must be an integer from 0 to 4')
        issues = []
        def member(number):
            prefix = f'{relative}/{locale}/stage-{number}'
            mode = self._mode(relative, number, issues)
            lesson = self._read(f'{prefix}/lesson.md', optional=True)
            if lesson is None:
                issues.append({'level': 'error', 'source': f'{prefix}/lesson.md', 'msg': 'Lesson not written yet'})
            return {'stage': number, 'lesson_md': lesson, 'mode': mode}, prefix
        values = [member(number) for number in range(stage + 1)]
        selected, prefix = values[-1]
        quiz = self._read(f'{prefix}/quiz.md', optional=True)
        if quiz is None:
            issues.append({'level': 'error', 'source': f'{prefix}/quiz.md', 'msg': 'Quiz not written yet'})
        brief_path = f'{relative}/stage-{stage}/brief.json'
        brief_text = self._read(brief_path, optional=True)
        policies = {}
        if brief_text is not None:
            try:
                brief = json.loads(brief_text)
                if not isinstance(brief, dict):
                    raise ValueError('brief')
                policies = brief.get('option_text_policies', {})
            except (ValueError, AttributeError):
                issues.append({'level': 'error', 'source': brief_path, 'msg': 'Invalid stage brief JSON for option language policy'})
                policies = None
        return {'language': match[2], 'locale': locale, 'stage': stage, 'mode': selected['mode'], 'lesson_md': selected['lesson_md'], 'quiz_md': quiz, 'option_text_policies': policies, 'prior_lessons': [value for value, _ in values[:-1]]}, issues

    def render_report(self, relative, locale, stage=0):
        correction = None
        if str(relative).startswith('correction-edition'):
            payload, issues, correction = self._correction_input(relative, locale, stage)
        else:
            payload, issues = self._input(relative, locale, stage)
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(data) > self.MAX_INPUT:
            raise PreviewError('Preview input exceeds 2 MiB')
        for fixed in ('tools/render-preview.cjs', 'tools/content-contract.cjs', 'tools/lesson-policy.cjs', 'tools/quiz-policy.cjs', 'reference/formats/kit/gmd.js', 'reference/formats/kit/qmd.js', 'reference/formats/grammar/conventions.json'):
            core.safe_path(self.code_root, fixed)
        try:
            process = subprocess.run([os.environ.get('NODE_BINARY', 'node'), '--max-old-space-size=128', str(self.code_root / 'tools/render-preview.cjs')], input=data, capture_output=True, timeout=self.TIMEOUT, cwd=self.code_root)
        except subprocess.TimeoutExpired as exc:
            raise PreviewError(f'Preview parsing exceeded {self.TIMEOUT} seconds') from exc
        except OSError as exc:
            raise PreviewError('Node preview bridge unavailable; check NODE_BINARY') from exc
        if len(process.stdout) > self.MAX_OUTPUT:
            raise PreviewError('Preview output exceeds 8 MiB')
        try:
            report = json.loads(process.stdout)
            if not isinstance(report, dict) or not isinstance(report.get('issues'), list):
                raise ValueError('shape')
        except (ValueError, UnicodeError) as exc:
            raise PreviewError('Node preview bridge returned an invalid report') from exc
        if process.returncode and not report.get('issues'):
            raise PreviewError('Node preview bridge failed')
        report.update({key: payload[key] for key in ('language', 'locale', 'stage', 'mode')})
        report['path'] = str(relative)
        if correction is not None:
            report['correction_preview'] = correction
            review_scope = ('Scoped correction reviews only, not a full-family approval. '
                            if correction['review_status'] == 'scoped-reviewed' else
                            'Draft status grants no scoped review approval. ')
            report['lifecycle'] = ('Draft correction preview · ' + correction['review_status'] +
                ' · receipt SHA256 ' + correction['receipt_sha256'] +
                ' · scope: own-family syntax/protected contracts. ' + review_scope +
                'Original briefs are pinned; old reviews are not correction approvals. '
                'NOT production approved; no app adoption or canonical history change.')
        else:
            report['lifecycle'] = 'Original author draft — not finalized' if str(relative).split('/')[0].endswith('runs') else 'Original pack files — completion receipt not verified here'
        report['scope'] = 'Author reference preview — controls and status are in English; content uses the selected learner locale. No native/audio verification. ' + report['lifecycle']
        report['issues'] = issues + report['issues']
        report['ok'] = bool(report.get('ok')) and not any(issue.get('level') == 'error' for issue in issues)
        return report

    def render(self, relative, locale, stage=0):
        report = self.render_report(relative, locale, stage)
        def bundle(relative):
            return core.safe_path(self.code_root, relative).read_text(encoding='utf-8')
        css = bundle('lesson_factory/dashboard_assets/preview.css')
        scripts = [bundle(relative) for relative in ('reference/formats/kit/gmd.js', 'reference/formats/kit/qmd.js', 'tools/quiz-policy.cjs', 'lesson_factory/dashboard_assets/preview.js')]
        if '</style' in css.lower() or any('</script' in source.lower() for source in scripts):
            raise PreviewError('Unsafe closing tag in trusted preview bundle')
        def digest(text):
            return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"
        csp = "default-src 'none'; script-src " + ' '.join(map(digest, scripts)) + "; style-src " + digest(css) + "; base-uri 'none'; form-action 'none'; connect-src 'none'; img-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'"
        serialized = json.dumps(report, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
        markup = f'<!doctype html><html lang="en" dir="ltr"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CherryLex reference preview</title><style>{css}</style></head><body><main id="preview"><h1>Reference preview</h1><p>Loading local grammar and quiz preview…</p></main><script id="preview-data" type="application/json">{serialized}</script>'
        return markup + ''.join(f'<script>{source}</script>' for source in scripts) + '</body></html>'


class _CorrectionInput(PreviewService):
    """Per-render view: never shared mutable resolver state or source fallback."""
    def __init__(self, service, paths, pins):
        super().__init__(service.root)
        self.service = service
        self.paths = paths
        self.pins = pins
        self.cache = {}

    def _read(self, relative, *, optional=False):
        if relative not in self.paths:
            raise PreviewError('Read outside exact correction preview map')
        if relative not in self.cache:
            data = self.service._bounded_bytes(self.paths[relative], self.MAX_FILE)
            if hashlib.sha256(data).hexdigest() != self.pins[relative]:
                raise PreviewError(f'Correction preview file changed after verification: {relative}')
            try:
                self.cache[relative] = data.decode('utf-8')
            except UnicodeError as exc:
                raise PreviewError(f'Invalid UTF-8 correction preview file: {relative}') from exc
        return self.cache[relative]
