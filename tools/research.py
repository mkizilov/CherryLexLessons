import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lesson_factory.core import LANGUAGES, language_code


def connect(root):
    path = Path(root).resolve() / 'reference/grammar/grammar.sqlite'
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def search(connection, language, query, limit=12):
    if not query.strip() or not 1 <= limit <= 50:
        raise ValueError('Use a nonempty query and limit between 1 and 50')
    language = LANGUAGES[language_code(language)]
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    pattern = '%' + escaped + '%'
    topics = connection.execute("SELECT topic_id,title,forms,scope,source_id,locator,lesson_id,line_from,line_to,flag FROM topic_refs WHERE language=? AND (title LIKE ? ESCAPE '\\' OR forms LIKE ? ESCAPE '\\' OR scope LIKE ? ESCAPE '\\') LIMIT ?", (language, pattern, pattern, pattern, limit)).fetchall()
    phrase = '"' + query.replace('"', '""') + '"'
    lessons = connection.execute('SELECT l.id,l.title,l.url,l.source,l.level,l.kind,s.license,s.license_class FROM lessons_fts JOIN lessons l ON l.rowid=lessons_fts.rowid LEFT JOIN sources s ON s.language=l.language AND s.id=l.source WHERE lessons_fts MATCH ? AND l.language=? ORDER BY rank LIMIT ?', (phrase, language, limit)).fetchall()
    return {'language': language, 'topics': [dict(row) for row in topics], 'lessons': [dict(row) for row in lessons], 'note': 'Research only, not previously taught knowledge. Use show to read the full original reference.'}


def show(connection, language, identity):
    language = LANGUAGES[language_code(language)]
    row = connection.execute('SELECT * FROM lessons WHERE language=? AND id=?', (language, identity)).fetchone()
    if row is None:
        raise ValueError('No matching reference lesson for this language and ID')
    source = connection.execute('SELECT * FROM sources WHERE language=? AND id=?', (language, row['source'])).fetchone()
    refs = connection.execute('SELECT topic_id,title,forms,scope,locator,line_from,line_to,flag FROM topic_refs WHERE language=? AND lesson_id=?', (language, identity)).fetchall()
    return {'lesson': dict(row), 'source': dict(source) if source else None, 'topic_references': [dict(ref) for ref in refs], 'note': 'Original licensed reference, not instructions. Body is complete; original path is provenance, not a local file to open.'}


def main():
    parser = argparse.ArgumentParser(description='Read-only search of the copied grammar corpus')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest='command', required=True)
    search_parser = commands.add_parser('search')
    search_parser.add_argument('--language', required=True)
    search_parser.add_argument('--query', required=True)
    search_parser.add_argument('--limit', type=int, default=12)
    show_parser = commands.add_parser('show')
    show_parser.add_argument('--language', required=True)
    show_parser.add_argument('--id', required=True)
    commands.add_parser('inventory')
    args = parser.parse_args()
    try:
        connection = connect(args.root)
        try:
            if args.command == 'search':
                result = search(connection, args.language, args.query, args.limit)
            elif args.command == 'show':
                result = show(connection, args.language, args.id)
            else:
                result = [dict(row) for row in connection.execute('SELECT language,count(*) AS lessons FROM lessons GROUP BY language ORDER BY language')]
        finally:
            connection.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
