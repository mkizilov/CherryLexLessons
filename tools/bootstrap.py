import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path


def jsonc(text):
    out, i, quoted = [], 0, False
    while i < len(text):
        c = text[i]
        if quoted:
            out.append(c)
            if c == '\\' and i + 1 < len(text):
                i += 1
                out.append(text[i])
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
            out.append(c)
        elif text[i:i + 2] == '//':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
            out.append('\n')
        elif text[i:i + 2] == '/*':
            end = text.find('*/', i + 2)
            if end < 0:
                raise ValueError('Unterminated configuration comment')
            i = end + 1
            out.append(' ')
        else:
            out.append(c)
        i += 1
    text = ''.join(out)
    out, i, quoted = [], 0, False
    while i < len(text):
        c = text[i]
        if quoted:
            out.append(c)
            if c == '\\':
                i += 1
                out.append(text[i])
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
            out.append(c)
        elif c == ',':
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j == len(text) or text[j] not in '}]':
                out.append(c)
        else:
            out.append(c)
        i += 1
    return json.loads(''.join(out))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_references(source, root):
    destination = root / 'reference'
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Reference snapshot already exists; use a fresh project for a different corpus revision')
    source = source.resolve()
    manifest = {'source': str(source), 'files': {}, 'reference_only': True}
    def copy(src, relative):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        manifest['files'][relative] = sha(target)
    for directory in sorted((source / 'Vocabulary').iterdir()):
        if directory.is_dir():
            for file in sorted(directory.iterdir()):
                if file.is_file() and file.suffix in ('.csv', '.json', '.tsv', '.md'):
                    copy(file, 'reference/vocabulary/' + directory.name + '/' + file.name)
    formats = source / 'work/design/study-card-concepts'
    for relative in ['kit/gmd.js', 'kit/qmd.js', 'grammar/conventions.json', 'grammar/TEMPLATE.md', 'grammar/QUIZ_TEMPLATE.md', 'grammar/GRAMMAR_MARKDOWN.md', 'grammar/QUIZ_MARKDOWN.md']:
        copy(formats / relative, 'reference/formats/' + relative)
    for relative in ['README.md', 'BRIEF.md', 'INDEX_BRIEF.md']:
        copy(source / 'work/grammar' / relative, 'reference/grammar/' + relative)
    database = destination / 'grammar/grammar.sqlite'
    with sqlite3.connect((source / 'work/grammar/grammar.sqlite').as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(database) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Copied grammar database failed quick_check')
    manifest['files']['reference/grammar/grammar.sqlite'] = sha(database)
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return len(manifest['files'])


def secure_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as file:
        file.write(data)


def copy_credentials(source, root, configurations):
    merged = {}
    for path in configurations:
        config = jsonc(path.read_text())
        for name in ('exa', 'tavily'):
            if name in config.get('mcp', {}):
                merged[name] = config['mcp'][name]
    if set(merged) != {'exa', 'tavily'}:
        raise ValueError('Both exa and tavily must exist in the selected source configurations')
    outputs, servers = {}, {}
    def protected(value, name):
        if '{env:' in value or '{file:' in value:
            raise ValueError('Resolve credential substitutions at the source before copying; values are never displayed')
        filename = 'mcp-' + name
        outputs[root / 'secrets' / filename] = value
        return '{file:./secrets/' + filename + '}'
    for name, entry in merged.items():
        if entry.get('type') != 'remote':
            raise ValueError('This installer supports copied remote research MCP definitions only')
        server = {'type': 'remote', 'url': protected(entry['url'], name + '-url'), 'enabled': True}
        for key, value in entry.get('headers', {}).items():
            suffix = re.sub('[^a-zA-Z0-9_-]', '-', key)
            server.setdefault('headers', {})[key] = protected(value, name + '-header-' + suffix)
        if 'oauth' in entry:
            server['oauth'] = entry['oauth']
        servers[name] = server
    key = (source / 'work/secrets/contactbox_key').read_text().strip()
    if not key:
        raise ValueError('Contactbox key file is empty')
    outputs[root / 'secrets/contactbox_key'] = key
    if any(path.exists() for path in outputs):
        raise ValueError('Credential destination already exists; refusing to overwrite')
    for path, value in outputs.items():
        secure_write(path, value)
    config_path = root / 'opencode.json'
    config = json.loads(config_path.read_text())
    config['mcp'].update(servers)
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    return {'research_servers': sorted(servers), 'contactbox': 'copied', 'credential_values': 'never displayed'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--root', default=Path(__file__).resolve().parents[1], type=Path)
    parser.add_argument('--references', action='store_true')
    parser.add_argument('--copy-secrets', action='store_true')
    parser.add_argument('--config', action='append', type=Path, default=[])
    args = parser.parse_args()
    report = {}
    if args.references:
        report['reference_files'] = copy_references(args.source, args.root)
    if args.copy_secrets:
        report['credentials'] = copy_credentials(args.source, args.root, args.config)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
