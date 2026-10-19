import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
AGENTS = {'lesson-coordinator', 'lesson-assessment-reviewer', 'lesson-format-reviewer', 'lesson-language-expert', 'lesson-pedagogy-reviewer', 'lesson-toolsmith'}
SKILLS = {'lesson-authoring', 'lesson-localization', 'pack-review', 'authoring-tooling'}


def frontmatter(path):
    text = path.read_text(encoding='utf-8')
    parts = text.split('---', 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError(f'Missing frontmatter: {path.name}')
    values = {}
    for line in parts[1].strip().splitlines():
        key, separator, value = line.partition(':')
        if not separator or key.strip() in values:
            raise ValueError(f'Unsupported or repeated frontmatter field: {path.name}')
        values[key.strip()] = value.strip()
    if not parts[2].strip():
        raise ValueError(f'Empty instructions: {path.name}')
    return values


def validate_permissions(value):
    if value != {'*': 'allow', 'question': 'deny'} or list(value) != ['*', 'question']:
        raise ValueError('Authoring permissions must allow tools but deny blocking user questions last')


def validate_context_management(config):
    """V2 compaction has an explicit input budget, buffer and retained-context budget."""
    compaction = config.get('compaction', {})
    limits = config.get('provider', {}).get('opencode-omniroute', {}).get('models', {}).get('Antharas', {}).get('limit', {})
    if compaction != {'auto': True, 'buffer': 100000, 'keep': {'tokens': 20000}}:
        raise ValueError('Lesson coordinators require V2 automatic summaries with explicit buffer and retention')
    if limits != {'context': 1000000, 'input': 872000, 'output': 128000}:
        raise ValueError('Antharas input limit must match the observed 872000-token routing limit')


def validate_credential_substitutions(config):
    """Validate research MCP shape without reading secret values."""
    credential_paths = {Path('secrets/contactbox_key')}
    mcp = config.get('mcp', {})
    for name in ('exa', 'tavily'):
        server = mcp[name]
        if server.get('type') != 'remote' or server.get('enabled') is not True:
            raise ValueError('Research MCP must be an enabled remote server')
        for value in [server['url'], *server.get('headers', {}).values()]:
            match = re.fullmatch(r'\{file:\./(secrets/[A-Za-z0-9_-]+)\}', value)
            if not match:
                raise ValueError('Research credentials must use local file substitutions')
            credential_paths.add(Path(match[1]))
    return credential_paths


def validate_ignore_policy(root):
    gitignore = set((root / '.gitignore').read_text(encoding='utf-8').splitlines())
    if not {'secrets/', 'reference/', 'family_runs/', 'family_packs/', 'family-registry.sqlite*', 'work/'} <= gitignore:
        raise ValueError('Sensitive/generated paths must remain ignored')


def check(root=ROOT, source_only=False):
    root = Path(root).resolve()
    report = {}
    python_files = sorted(path for folder in ('lesson_factory', 'tools', 'tests') for path in (root / folder).rglob('*.py'))
    for path in python_files:
        text = path.read_text(encoding='utf-8')
        ast.parse(text, filename=str(path))
        if any(line.rstrip() != line for line in text.splitlines()):
            raise ValueError(f'Trailing whitespace: {path.relative_to(root)}')
    report['python_syntax'] = len(python_files)
    node = os.environ.get('NODE_BINARY', 'node')
    major = int(subprocess.check_output([node, '--version'], text=True).strip().lstrip('v').split('.')[0])
    if major < 24:
        raise ValueError('Node 24+ required; set NODE_BINARY when necessary')
    if source_only:
        javascript_files = sorted((root / 'tools').glob('*.cjs'))
    else:
        javascript_files = sorted((root / 'tools').glob('*.cjs')) + [root / 'reference/formats/kit/gmd.js', root / 'reference/formats/kit/qmd.js']
    for path in javascript_files:
        subprocess.run([node, '--check', str(path)], check=True, capture_output=True)
    report['javascript_syntax'] = len(javascript_files)
    config = json.loads((root / 'opencode.json').read_text())
    if config.get('default_agent') != 'lesson-coordinator' or config.get('share') != 'disabled':
        raise ValueError('Coordinator must be default and sharing disabled')
    if config.get('model') != 'opencode-omniroute/Antharas':
        raise ValueError('Unexpected authoring model')
    if config.get('plugin') or config.get('plugins'):
        raise ValueError('Lesson project inherits global plugins; do not register Ensemble twice')
    validate_permissions(config.get('permission'))
    validate_context_management(config)
    agents = {path.stem: path for path in (root / '.opencode/agents').glob('*.md')}
    if set(agents) != AGENTS:
        raise ValueError('Unexpected agent inventory')
    for name, path in agents.items():
        fields = frontmatter(path)
        expected_mode = 'primary' if name == 'lesson-coordinator' else 'subagent'
        if any(fields.get(key) != value for key, value in {'mode': expected_mode, 'model': config['model'], 'variant': 'xhigh'}.items()) or not fields.get('description'):
            raise ValueError(f'Invalid agent setup: {name}')
        validate_permissions(json.loads(fields.get('permission', 'null')))
    skills = {path.parent.name: path for path in (root / '.opencode/skills').glob('*/SKILL.md')}
    if set(skills) != SKILLS:
        raise ValueError('Unexpected skill inventory')
    for name, path in skills.items():
        fields = frontmatter(path)
        if fields.get('name') != name or not fields.get('description'):
            raise ValueError(f'Invalid skill: {name}')
    command = frontmatter(root / '.opencode/commands/lesson.md')
    if command.get('agent') != config['default_agent']:
        raise ValueError('Lesson command targets the wrong agent')
    for path in (root / 'templates').glob('*.json'):
        json.loads(path.read_text())
    report['authoring_definitions'] = {'agents': len(agents), 'skills': len(skills), 'commands': 1}
    validate_ignore_policy(root)
    substitutions = validate_credential_substitutions(config)
    if source_only:
        report['credential_substitutions'] = sorted(path.as_posix() for path in substitutions)
        report['reference'] = 'not checked (omitted frozen parsers/manifest)'
        report['credentials'] = 'not checked (private); substitution shape valid, values not read'
        report['scope'] = 'Offline source-only syntax/config checks; reference parsers/manifest, private credentials and live model not checked'
        report['source_only'] = True
        return report
    manifest = json.loads((root / 'reference/manifest.json').read_text())
    expected = manifest['files']
    actual = {path.relative_to(root).as_posix() for path in (root / 'reference').rglob('*') if path.is_file() and path != root / 'reference/manifest.json'}
    if actual != set(expected):
        raise ValueError('Copied reference inventory differs from bootstrap manifest')
    for relative, wanted in expected.items():
        path = root / relative
        if not relative.startswith('reference/') or '..' in Path(relative).parts or path.is_symlink():
            raise ValueError('Unsafe reference path')
        with path.open('rb') as handle:
            current = hashlib.file_digest(handle, 'sha256').hexdigest()
        if current != wanted:
            raise ValueError(f'Reference hash mismatch: {relative}')
    report['reference_hashes'] = len(expected)
    credential_paths = {root / relative for relative in substitutions}
    for path in credential_paths:
        if path.is_symlink() or not path.is_file() or not path.stat().st_size or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError('Credential file missing, linked or not private; values were not read')
    if stat.S_IMODE((root / 'secrets').stat().st_mode) & 0o077:
        raise ValueError('Secrets directory must be private')
    report['credentials'] = 'present and private; values not read'
    report['scope'] = 'Offline syntax/config/integrity checks, not a live model or linguistic assessment'
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Offline lesson project checks')
    parser.add_argument('--source-only', action='store_true', help='Validate published source without omitted reference parsers/manifest, secrets or prompts')
    options = parser.parse_args()
    try:
        print(json.dumps(check(source_only=options.source_only), indent=2))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'Check failed: {type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(1)
