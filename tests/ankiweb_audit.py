"""Hash existing collection rows without opening Anki or exposing their content."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


TABLES = ('notes', 'cards', 'revlog', 'notetypes', 'decks')


def row_hash(row):
    digest = hashlib.sha256()
    for value in row:
        if value is None:
            tag, data = b'n', b''
        elif isinstance(value, bytes):
            tag, data = b'b', value
        elif isinstance(value, str):
            tag, data = b's', value.encode('utf-8')
        elif isinstance(value, int):
            tag, data = b'i', str(value).encode('ascii')
        elif isinstance(value, float):
            tag, data = b'f', value.hex().encode('ascii')
        else:
            raise ValueError('Unsupported SQLite value type')
        digest.update(tag + len(data).to_bytes(8, 'big') + data)
    return digest.hexdigest()


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(collection):
    result = {'version': 1, 'ignored_columns': ['usn'], 'tables': {}}
    uri = Path(collection).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('BEGIN')
        for table in TABLES:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info({identifier(table)})')
                       if row[1] != 'usn']
            if 'id' not in columns:
                raise ValueError(f'Missing required table or id column: {table}')
            selected = ', '.join(identifier(name) for name in columns)
            rows = {}
            id_index = columns.index('id')
            for row in db.execute(f'SELECT {selected} FROM {identifier(table)} ORDER BY id'):
                row_id = row[id_index]
                if not isinstance(row_id, int) or str(row_id) in rows:
                    raise ValueError(f'Invalid or duplicate row id in {table}')
                rows[str(row_id)] = row_hash(row)
            result['tables'][table] = {'columns': columns, 'count': len(rows), 'rows': rows}
    return result


def compare(baseline, current):
    if baseline.get('version') != 1 or baseline.get('ignored_columns') != ['usn']:
        raise ValueError('Unsupported audit baseline format')
    if set(baseline.get('tables', {})) != set(TABLES):
        raise ValueError('Baseline must contain all five audited tables')
    result = {'ok': True, 'tables': {}}
    for table in TABLES:
        original, now = baseline['tables'][table], current['tables'][table]
        rows = original['rows']
        if original['count'] != len(rows) or 'id' not in original['columns']:
            raise ValueError(f'Invalid baseline table: {table}')
        if any(len(value) != 64 or any(char not in '0123456789abcdef' for char in value)
               for value in rows.values()):
            raise ValueError(f'Invalid baseline row hash: {table}')
        missing = sorted(rows.keys() - now['rows'].keys(), key=int)
        changed = sorted((key for key in rows.keys() & now['rows'].keys()
                          if rows[key] != now['rows'][key]), key=int)
        schema_changed = original['columns'] != now['columns']
        result['tables'][table] = {
            'baseline_count': original['count'], 'current_count': now['count'],
            'added': len(now['rows'].keys() - rows.keys()),
            'missing': missing, 'changed': changed, 'schema_changed': schema_changed,
        }
        if missing or changed or schema_changed:
            result['ok'] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'compare'))
    parser.add_argument('baseline', type=Path, help='New JSON output for snapshot; original JSON for compare')
    parser.add_argument('--collection', type=Path, required=True, help='Existing collection.anki2; always read-only')
    args = parser.parse_args(argv)
    try:
        if args.action == 'snapshot':
            current = snapshot(args.collection)
            with args.baseline.open('x', encoding='utf-8') as output:
                json.dump(current, output, ensure_ascii=True, indent=2)
                output.write('\n')
            result = {'counts': {name: table['count'] for name, table in current['tables'].items()}}
        else:
            with args.baseline.open(encoding='utf-8') as source:
                baseline = json.load(source)
            result = compare(baseline, snapshot(args.collection))
    except sqlite3.Error as error:
        reason = 'Collection is locked' if 'locked' in str(error).lower() else 'Collection read failed'
        print(json.dumps({'error': reason, 'kind': type(error).__name__}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({'error': 'Unable to read, validate or create audit file',
                          'kind': type(error).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get('ok', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
