"""Source-only public snapshot checks for tools/check.py.

Synthetic fixtures only: verifies that ``check(source_only=True)`` passes
without omitted reference parsers/manifest, secrets or prompts, that the
default full check stays fail-closed without references, and that source
syntax/config errors still fail in source-only mode.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.check import check

PROJECT = Path(__file__).resolve().parents[1]
AGENT_MODES = {
    'lesson-coordinator': 'primary',
    'lesson-assessment-reviewer': 'subagent',
    'lesson-format-reviewer': 'subagent',
    'lesson-language-expert': 'subagent',
    'lesson-pedagogy-reviewer': 'subagent',
    'lesson-toolsmith': 'subagent',
}
SKILLS = ('lesson-authoring', 'lesson-localization', 'pack-review', 'authoring-tooling')
MODEL = 'opencode-omniroute/Antharas'
PERMISSION = '{"*": "allow", "question": "deny"}'
VALID_JS = '"use strict";\nmodule.exports = {};\n'


def make_source_root(base):
    """Build a minimal valid public-source tree without reference/ or secrets/."""
    root = Path(base)
    (root / 'lesson_factory').mkdir(parents=True)
    (root / 'tools').mkdir(parents=True)
    (root / 'tests').mkdir(parents=True)
    (root / 'lesson_factory/own.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'tools/helper.py').write_text('VALUE = 2\n', encoding='utf-8')
    (root / 'tools/own.cjs').write_text(VALID_JS, encoding='utf-8')
    (root / 'tests/helper_check.py').write_text('VALUE = 3\n', encoding='utf-8')
    shutil.copyfile(PROJECT / 'opencode.json', root / 'opencode.json')
    agents = root / '.opencode/agents'
    agents.mkdir(parents=True)
    for name, mode in AGENT_MODES.items():
        agents.joinpath(f'{name}.md').write_text(
            '---\n'
            f'description: Synthetic {name}.\n'
            f'mode: {mode}\n'
            f'model: {MODEL}\n'
            'variant: xhigh\n'
            f'permission: {PERMISSION}\n'
            '---\n'
            '\nSynthetic instructions.\n',
            encoding='utf-8',
        )
    for name in SKILLS:
        skill = root / '.opencode/skills' / name
        skill.mkdir(parents=True)
        skill.joinpath('SKILL.md').write_text(
            '---\n'
            f'name: {name}\n'
            f'description: Synthetic {name}.\n'
            '---\n'
            '\nSynthetic skill.\n',
            encoding='utf-8',
        )
    commands = root / '.opencode/commands'
    commands.mkdir(parents=True)
    commands.joinpath('lesson.md').write_text(
        '---\n'
        'description: Synthetic lesson command.\n'
        'agent: lesson-coordinator\n'
        '---\n'
        '\nSynthetic command.\n',
        encoding='utf-8',
    )
    templates = root / 'templates'
    templates.mkdir(parents=True)
    templates.joinpath('sample.json').write_text('{"sample": true}\n', encoding='utf-8')
    root.joinpath('.gitignore').write_text(
        'secrets/\nreference/\nfamily_runs/\nfamily_packs/\n'
        'family-registry.sqlite*\nwork/\n',
        encoding='utf-8',
    )
    return root


def rewrite_json(path, mutate):
    config = json.loads(path.read_text(encoding='utf-8'))
    mutate(config)
    path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')


class PublicSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = make_source_root(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_source_only_succeeds_without_reference_or_secrets(self):
        self.assertFalse((self.root / 'reference').exists())
        self.assertFalse((self.root / 'secrets').exists())
        report = check(self.root, source_only=True)
        self.assertTrue(report['source_only'])
        self.assertGreaterEqual(report['python_syntax'], 3)
        self.assertGreaterEqual(report['javascript_syntax'], 1)
        self.assertEqual(
            report['authoring_definitions'], {'agents': 6, 'skills': 4, 'commands': 1}
        )
        self.assertIn('frozen parsers/manifest', report['reference'])
        self.assertIn('not checked', report['reference'])
        self.assertIn('not checked', report['credentials'])
        self.assertIn('values not read', report['credentials'])
        self.assertIn('parsers/manifest', report['scope'])
        self.assertIn('private credentials', report['scope'])
        self.assertIn('live model not checked', report['scope'])
        self.assertIn('secrets/mcp-exa-url', report['credential_substitutions'])
        self.assertNotIn('reference_hashes', report)

    def test_default_check_still_requires_reference(self):
        with self.assertRaises((OSError, ValueError, KeyError, subprocess.SubprocessError)):
            check(self.root)

    def test_source_only_rejects_source_syntax_errors(self):
        (self.root / 'lesson_factory/own.py').write_text('def broken(:\n', encoding='utf-8')
        with self.assertRaises(SyntaxError):
            check(self.root, source_only=True)
        (self.root / 'lesson_factory/own.py').write_text('VALUE = 1\n', encoding='utf-8')
        (self.root / 'tools/own.cjs').write_text('function broken( {\n', encoding='utf-8')
        with self.assertRaises(Exception):
            check(self.root, source_only=True)

    def test_source_only_rejects_config_errors(self):
        rewrite_json(self.root / 'opencode.json', lambda config: config.update(model='other/model'))
        with self.assertRaises(ValueError):
            check(self.root, source_only=True)
        shutil.copyfile(PROJECT / 'opencode.json', self.root / 'opencode.json')

        def inline_secret(config):
            config['mcp']['exa']['url'] = 'https://example.invalid'

        rewrite_json(self.root / 'opencode.json', inline_secret)
        with self.assertRaises(ValueError):
            check(self.root, source_only=True)
        shutil.copyfile(PROJECT / 'opencode.json', self.root / 'opencode.json')

        self.root.joinpath('.gitignore').write_text('secrets/\nwork/\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            check(self.root, source_only=True)


if __name__ == '__main__':
    unittest.main()
