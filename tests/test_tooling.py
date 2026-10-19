import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from tools.bootstrap import jsonc, secure_write
from tools.check import frontmatter, validate_permissions, validate_context_management
from tools.research import connect, search, show


class ToolingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        folder = self.root / 'reference/grammar'
        folder.mkdir(parents=True)
        db = sqlite3.connect(folder / 'grammar.sqlite')
        db.executescript('''
            CREATE TABLE sources(language TEXT, id TEXT, license TEXT, license_class TEXT);
            CREATE TABLE lessons(id TEXT, language TEXT, title TEXT, url TEXT, source TEXT, level TEXT, kind TEXT, topics TEXT, body TEXT);
            CREATE TABLE topic_refs(language TEXT, topic_id TEXT, title TEXT, forms TEXT, scope TEXT, source_id TEXT, locator TEXT, lesson_id TEXT, line_from INTEGER, line_to INTEGER, flag TEXT);
            CREATE VIRTUAL TABLE lessons_fts USING fts5(title, topics, body, content='lessons', content_rowid='rowid');
        ''')
        db.execute('INSERT INTO sources VALUES (?,?,?,?)', ('German', 'example', 'Example license', 'fixture'))
        self.body = 'Complete reference body with sein.\n' + 'x' * 20000
        db.execute('INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?)', ('example/sein', 'German', 'Use sein', 'https://example.invalid/sein', 'example', 'A1', 'fixture', 'sein', self.body))
        db.execute('INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?)', ('example/autre', 'French', 'Autre sein', '', 'example', 'A1', 'fixture', 'sein', 'Not German'))
        db.execute("INSERT INTO lessons_fts(lessons_fts) VALUES ('rebuild')")
        db.execute('INSERT INTO topic_refs VALUES (?,?,?,?,?,?,?,?,?,?,?)', ('German', 'sein-form', 'Use sein', '["sein"]', 'A focused form', 'example', 'example/sein#L1', 'example/sein', 1, 1, None))
        db.commit()
        db.close()
        self.db = connect(self.root)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_search_is_language_scoped_and_preserves_license(self):
        result = search(self.db, 'de', 'sein', 1)
        self.assertEqual(result['language'], 'German')
        self.assertEqual([row['id'] for row in result['lessons']], ['example/sein'])
        self.assertEqual(result['lessons'][0]['license'], 'Example license')
        self.assertEqual(result['topics'][0]['locator'], 'example/sein#L1')
        self.assertNotIn('body', result['lessons'][0])

    def test_show_keeps_complete_body_and_source(self):
        result = show(self.db, 'German', 'example/sein')
        self.assertEqual(result['lesson']['body'], self.body)
        self.assertEqual(result['source']['license_class'], 'fixture')
        with self.assertRaises(ValueError):
            show(self.db, 'fr', 'example/sein')

    def test_queries_are_literal_and_database_readonly(self):
        self.assertEqual(search(self.db, 'de', '" OR sein*')['lessons'], [])
        self.assertEqual(search(self.db, 'de', '%')['topics'], [])
        with self.assertRaises(sqlite3.OperationalError):
            self.db.execute('DELETE FROM lessons')
        for query, limit in (('', 1), ('sein', 0), ('sein', 51)):
            with self.assertRaises(ValueError):
                search(self.db, 'de', query, limit)

    def test_jsonc_preserves_url_comments_and_trailing_commas_in_strings(self):
        data = jsonc('''{
            // comment
            "url": "https://example.invalid/?v=//text",
            "value": "/* not a comment */ ,}",
            "list": [1, 2,], /* comment */
        }''')
        self.assertEqual(data['value'], '/* not a comment */ ,}')
        self.assertEqual(data['list'], [1, 2])
        self.assertEqual(data['url'], 'https://example.invalid/?v=//text')
        with self.assertRaises(ValueError):
            jsonc('{/* unfinished')

    def test_secure_copy_is_private_nonoverwriting_and_refuses_links(self):
        path = self.root / 'secrets/fixture-key'
        secure_write(path, 'not-a-real-key')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        with self.assertRaises(FileExistsError):
            secure_write(path, 'replacement')
        link = path.parent / 'linked-key'
        link.symlink_to(path)
        with self.assertRaises(FileExistsError):
            secure_write(link, 'replacement')
        self.assertEqual(path.read_text(), 'not-a-real-key')

    def test_question_tool_is_denied_after_allow_wildcard(self):
        validate_permissions({'*': 'allow', 'question': 'deny'})
        for value in ('allow', None, {}, {'*': 'allow'}, {'*': 'allow', 'question': 'ask'}, {'question': 'deny', '*': 'allow'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_permissions(value)
        project = Path(__file__).resolve().parents[1]
        validate_permissions(json.loads((project / 'opencode.json').read_text())['permission'])
        agents = list((project / '.opencode/agents').glob('*.md'))
        self.assertEqual(len(agents), 6)
        for path in agents:
            validate_permissions(json.loads(frontmatter(path)['permission']))

    def test_context_management_retains_history_and_starts_before_provider_limit(self):
        project = Path(__file__).resolve().parents[1]
        config = json.loads((project / 'opencode.json').read_text())
        validate_context_management(config)
        limit = config['provider']['opencode-omniroute']['models']['Antharas']['limit']['input']
        self.assertEqual(limit - config['compaction']['buffer'], 772000)
        for key, value in [('auto', False), ('keep', {'tokens': 0}), ('buffer', 0), ('prune', False)]:
            changed = json.loads(json.dumps(config))
            changed['compaction'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_context_management(changed)
        changed = json.loads(json.dumps(config))
        del changed['provider']['opencode-omniroute']['models']['Antharas']['limit']['input']
        with self.assertRaises(ValueError):
            validate_context_management(changed)

    def test_agent_frontmatter_rejects_duplicate_fields(self):
        path = self.root / 'agent.md'
        path.write_text('---\nmode: primary\nmode: subagent\n---\nInstructions.\n')
        with self.assertRaises(ValueError):
            frontmatter(path)
        path.write_text('---\nmode: primary\ndescription: A focused role\n---\nInstructions.\n')
        self.assertEqual(frontmatter(path)['mode'], 'primary')
        self.assertEqual(json.loads(json.dumps(frontmatter(path)))['description'], 'A focused role')


if __name__ == '__main__':
    unittest.main()
