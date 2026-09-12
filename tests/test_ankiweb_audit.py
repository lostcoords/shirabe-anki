import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name('ankiweb_audit.py')
TABLES = ('notes', 'cards', 'revlog', 'notetypes', 'decks')


class AnkiWebAudit(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.collection = self.root / 'collection #1?.anki2'
        self.baseline = self.root / 'baseline.json'
        with sqlite3.connect(self.collection) as db:
            db.executescript('''
                CREATE TABLE notes (id INTEGER PRIMARY KEY, usn INTEGER, mod INTEGER, flds TEXT);
                CREATE TABLE cards (id INTEGER PRIMARY KEY, usn INTEGER, nid INTEGER, due INTEGER);
                CREATE TABLE revlog (id INTEGER PRIMARY KEY, usn INTEGER, cid INTEGER, ease INTEGER);
                CREATE TABLE notetypes (id INTEGER PRIMARY KEY, usn INTEGER, name TEXT, config BLOB);
                CREATE TABLE decks (id INTEGER PRIMARY KEY, usn INTEGER, name TEXT, common BLOB);
                INSERT INTO notes VALUES (1, -1, 100, 'PRIVATE-FIELD-猫');
                INSERT INTO cards VALUES (1, -1, 1, 25);
                INSERT INTO revlog VALUES (1, -1, 1, 3);
            ''')
            for table in ('notetypes', 'decks'):
                db.execute(f'INSERT INTO {table} VALUES (1, -1, ?, ?)',
                           ('PRIVATE-NAME', b'\x08\x01\x12\x00\xffPRIVATE-PROTOBUF'))

    def command(self, action, expected=0, baseline=None, collection=None):
        result = subprocess.run([
            sys.executable, str(SCRIPT), action, str(baseline or self.baseline),
            '--collection', str(collection or self.collection),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return result

    def change(self, sql, parameters=()):
        with sqlite3.connect(self.collection) as db:
            db.execute(sql, parameters)

    def test_snapshot_has_only_row_hashes_and_counts_and_does_not_write_database(self):
        before = self.collection.read_bytes()
        mtime = self.collection.stat().st_mtime_ns
        self.command('snapshot')
        data = json.loads(self.baseline.read_text())
        self.assertEqual(set(data['tables']), set(TABLES))
        self.assertEqual(data['ignored_columns'], ['usn'])
        for table in data['tables'].values():
            self.assertEqual(table['count'], 1)
            self.assertNotIn('usn', table['columns'])
            self.assertEqual(set(table['rows']), {'1'})
            self.assertRegex(table['rows']['1'], r'^[a-f0-9]{64}$')
        self.assertNotIn('PRIVATE', self.baseline.read_text())
        self.assertNotIn('猫', self.baseline.read_text())
        self.assertEqual(self.collection.read_bytes(), before)
        self.assertEqual(self.collection.stat().st_mtime_ns, mtime)
        self.assertEqual({path.name for path in self.root.iterdir()},
                         {self.collection.name, self.baseline.name})

    def test_changes_to_existing_notes_cards_reviews_notetypes_and_decks_fail(self):
        self.command('snapshot')
        changes = {
            'notes': "flds = 'changed'",
            'cards': 'due = 26',
            'revlog': 'ease = 1',
            'notetypes': "config = X'0802'",
            'decks': "name = 'renamed'",
        }
        for table, assignment in changes.items():
            with self.subTest(table=table):
                self.change(f'UPDATE {table} SET {assignment} WHERE id = 1')
                result = json.loads(self.command('compare', expected=1).stdout)
                self.assertFalse(result['ok'])
                self.assertEqual(result['tables'][table]['changed'], ['1'])

    def test_missing_original_rows_fail_even_when_replaced_with_new_ids(self):
        self.command('snapshot')
        for table in TABLES:
            self.change(f'UPDATE {table} SET id = 2 WHERE id = 1')
        result = json.loads(self.command('compare', expected=1).stdout)
        for table in result['tables'].values():
            self.assertEqual(table['missing'], ['1'])
            self.assertEqual(table['added'], 1)

    def test_additions_and_usn_bookkeeping_are_allowed_and_counted_separately(self):
        self.command('snapshot')
        self.change('INSERT INTO notes SELECT 2, -1, mod, flds FROM notes')
        self.change('INSERT INTO cards SELECT 2, -1, 2, due FROM cards')
        for table in TABLES:
            self.change(f'UPDATE {table} SET usn = 127')
        before = self.collection.read_bytes()
        result = json.loads(self.command('compare').stdout)
        self.assertTrue(result['ok'])
        for name, table in result['tables'].items():
            self.assertEqual(table['added'], int(name in ('notes', 'cards')))
            self.assertEqual(table['missing'], [])
            self.assertEqual(table['changed'], [])
        self.assertEqual(self.collection.read_bytes(), before)

    def test_blob_changes_and_blob_to_text_changes_are_detected(self):
        self.command('snapshot')
        for value in (b'\x08\x02', '08011200ff505249564154452d50524f544f425546'):
            with self.subTest(value_type=type(value).__name__):
                self.change('UPDATE decks SET common = ? WHERE id = 1', (value,))
                result = json.loads(self.command('compare', expected=1).stdout)
                self.assertEqual(result['tables']['decks']['changed'], ['1'])

    def test_other_metadata_and_schema_changes_are_not_ignored(self):
        self.command('snapshot')
        self.change('UPDATE notes SET mod = mod + 1')
        self.change('ALTER TABLE decks ADD COLUMN extra BLOB')
        result = json.loads(self.command('compare', expected=1).stdout)
        self.assertEqual(result['tables']['notes']['changed'], ['1'])
        self.assertTrue(result['tables']['decks']['schema_changed'])

    def test_snapshot_refuses_to_overwrite_existing_output(self):
        self.command('snapshot')
        before = self.baseline.read_bytes()
        self.change("UPDATE notes SET flds = 'changed'")
        self.command('snapshot', expected=2)
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_missing_database_is_not_created(self):
        missing = self.root / 'missing.anki2'
        self.command('snapshot', expected=2, collection=missing)
        self.assertFalse(missing.exists())
        self.assertFalse(self.baseline.exists())

    def test_exclusive_lock_fails_without_creating_a_baseline(self):
        with sqlite3.connect(self.collection) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            self.command('snapshot', expected=2)
        self.assertFalse(self.baseline.exists())

    def test_reads_one_wal_snapshot_even_if_another_writer_commits_between_tables(self):
        spec = importlib.util.spec_from_file_location('ankiweb_audit', SCRIPT)
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        real_connect = sqlite3.connect
        writer = real_connect(self.collection)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode = WAL')
        writer.execute('UPDATE notes SET usn = 10')
        writer.commit()
        committed = False

        def observe(statement):
            nonlocal committed
            if not committed and statement.startswith('SELECT') and '"cards"' in statement:
                writer.execute('INSERT INTO cards VALUES (2, -1, 1, 99)')
                writer.commit()
                committed = True

        def connect(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE forbidden (id INTEGER)')
            db.set_trace_callback(observe)
            return db

        with patch.object(audit.sqlite3, 'connect', side_effect=connect):
            snapshot = audit.snapshot(self.collection)
        self.assertTrue(committed)
        self.assertEqual(snapshot['tables']['cards']['count'], 1)
        self.assertEqual(writer.execute('SELECT count(*) FROM cards').fetchone()[0], 2)
        self.assertIsNone(writer.execute("SELECT name FROM sqlite_master WHERE name = 'forbidden'").fetchone())


if __name__ == '__main__':
    unittest.main()
