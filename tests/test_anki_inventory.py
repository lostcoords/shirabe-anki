import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import check_against_anki as check


class NativeInventory(unittest.TestCase):
    def check_source(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'fixture.py').write_text(textwrap.dedent(source))
            with patch.object(check, 'SRC', path):
                self.test_observed_native_calls_are_in_the_runtime_inventory()

    def test_prepared_media_dictionaries_are_not_native_media_managers(self):
        self.check_source('''
            class Preparation:
                media: dict

            def prepare_snapshot(audio, snapshot):
                media = audio.prepare(snapshot) if audio is not None else {}
                media = {name: download for name, download in media.items()}
                return media

            def persist_snapshot(media, preparation: Preparation):
                download = preparation.media.get('recording')
                media.dir()
                media.write_data('recording', download)
                media.trash_files([])
        ''')

    def test_dictionary_annotation_overrides_native_receiver_name(self):
        self.check_source('''
            def prepared_downloads(media: dict[str, bytes]):
                return media.get('recording'), media.items()
        ''')

    def test_local_deck_dictionaries_are_not_native_deck_managers(self):
        self.assertEqual(set(), check.observed_native_calls('''
def captured_decks():
    decks = {source_id: value for source_id, value in [(1, {})]}
    return decks.get(1), decks.items()
'''))
        calls = check.observed_native_calls('''
def captured_decks():
    decks = {}
    return decks.items()

def native_decks(decks, col):
    return decks.missing(), col.decks.items()
''')
        self.assertEqual({('DeckManager', 'missing'), ('DeckManager', 'items')},
                         {(klass, member) for _, klass, member in calls})

    def test_local_media_binding_does_not_hide_native_calls_in_other_scopes(self):
        for method, native_call in (('get', 'media.get("recording")'), ('items', 'col.media.items()'),
                                    ('_shirabe_missing_method', 'store._shirabe_missing_method()')):
            with self.subTest(native_call=native_call):
                with self.assertRaisesRegex(AssertionError, 'MediaManager\\.' + method):
                    self.check_source(f'''
                        def prepare_snapshot():
                            media = {{}}
                            return media.items()

                        def persist_snapshot(media, col):
                            store = col.media
                            {native_call}
                    ''')

    def test_observed_native_calls_are_in_the_runtime_inventory(self):
        inventory = {klass: set(members) for _, klass, members in check.RUNTIME}
        missing = set()
        for path in check.SRC.glob('*.py'):
            for _, klass, member in check.observed_native_calls(path.read_text()):
                if member not in inventory.get(klass, ()):
                    missing.add(f'{path.name}: {klass}.{member}')
        self.assertEqual('\n'.join(sorted(missing)), '')

    @unittest.skipUnless(check.anki_python(), 'Installed Anki required for symbol probing')
    def test_probe_reports_a_missing_method_and_accepts_native_note_state(self):
        runtime = [('anki.notes', 'Note', ['keys', 'fields', 'mid', '_shirabe_missing_method'])]
        result = subprocess.run([str(check.anki_python()), '-c', check.PROBE, '{}', '[]', json.dumps(runtime)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['missing'], ['Note._shirabe_missing_method'])


if __name__ == '__main__':
    unittest.main()
