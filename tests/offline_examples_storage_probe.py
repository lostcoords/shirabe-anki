from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from offline_fixture import prepared
from test_offline_examples import CORPUS, examples, response, sentence
from shirabe_addon import offline, offline_ui
from anki.collection import Collection, DeckIdLimit
from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest


def preparation(**options):
    return prepared(("猫",), sources=[CORPUS], examples=options.pop("examples", lambda _: response(sentence())),
                    preferences={"languages": ["es", "en"], "dictionaryReveal": {}}, **options)


class NativeExampleStorage(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shirabe-offline-examples-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.col = Collection(str(self.root / "collection.anki2"))
        self.addCleanup(self.col.close)
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"], note["Back"] = "猫", "cat"
        self.col.add_note(note, 1)
        self.note_id = note.id

    def apply(self, item, limit=offline.MAX_TOTAL_BYTES):
        captures = offline_ui.capture_notes(self.col, [self.note_id])
        with patch.object(offline_ui, "storage_budget", return_value=limit):
            return offline_ui.apply_prepared(self.col, offline_ui.PreparedBatch(rows=[(captures[0], item)]), {})

    def test_export_both_formats_retains_exact_examples_and_notices_without_static_template_names(self):
        item = preparation()
        expected_name, expected_data = offline.snapshot_file(item.snapshot)
        applied = self.apply(item)
        self.assertEqual((applied.saved, applied.bytes_used), (1, len(expected_data)))
        note = self.col.get_note(self.note_id)
        name = offline.snapshot_filename(note["ShirabeLookup"])
        self.assertEqual(name, expected_name)
        self.assertEqual((Path(self.col.media.dir()) / name).read_bytes(), expected_data)
        self.assertIn(name, self.col.media.files_in_str(note.mid, note["ShirabeLookup"]))
        self.assertNotIn(name, str(note.note_type()["tmpls"]))
        self.assertNotIn(name, self.col.media.check().unused)
        self.assertFalse(self.col.media.check().missing)
        for legacy in (True, False):
            package = self.root / f"examples-{legacy}.apkg"
            self.col.export_anki_package(out_path=str(package), options=ExportAnkiPackageOptions(with_media=True, legacy=legacy), limit=DeckIdLimit(1))
            imported = Collection(str(self.root / f"imported-{legacy}.anki2"))
            try:
                imported.import_anki_package(ImportAnkiPackageRequest(package_path=str(package)))
                snapshot = offline.read_snapshot(imported.media, name)
                self.assertEqual((Path(imported.media.dir()) / name).read_bytes(), expected_data)
                self.assertEqual(snapshot, item.snapshot)
                self.assertEqual(len(examples(snapshot)), 1)
                self.assertIn("https://tatoeba.org/en/sentences/show/123", snapshot["sources"][0]["attribution"])
                self.assertEqual(snapshot["sources"][0]["source"]["licenseUrl"], CORPUS["source"]["licenseUrl"])
            finally:
                imported.close()

    def test_storage_versions_budget_missing_sync_and_failed_partial_refresh(self):
        item = preparation(static_text_only=True)
        expected_name, data = offline.snapshot_file(item.snapshot)
        self.assertEqual(item.bytes_used, len(offline._json(item.snapshot)))
        with self.assertRaises(offline.SnapshotError):
            offline.persist_snapshot(self.col.media, item, remaining_bytes=len(data) - 1)
        self.assertEqual(offline.snapshot_storage(self.col.media).bytes_used, 0)
        applied = self.apply(item, len(data))
        self.assertEqual((applied.saved, applied.bytes_used), (1, len(data)))
        reference = self.col.get_note(self.note_id)["ShirabeLookup"]
        name = offline.snapshot_filename(reference)
        self.assertEqual(name, expected_name)
        self.assertEqual((Path(self.col.media.dir()) / name).read_bytes(), data)
        self.assertEqual(self.apply(item).bytes_used, 0)
        failed = preparation(examples=lambda _: response(), content_version="new", previous=item.snapshot)
        self.assertIn("examples_unavailable", failed.issues)
        result = self.apply(failed)
        self.assertEqual((result.kept_previous, result.bytes_used), (1, 0))
        self.assertEqual(self.col.get_note(self.note_id)["ShirabeLookup"], reference)
        second = preparation(content_version="new", examples=lambda _: response(sentence(), more=True))
        second_name, second_data = offline.snapshot_file(second.snapshot)
        combined_size = len(data) + len(second_data)
        with self.assertRaises(offline.SnapshotError) as raised:
            offline.persist_snapshot(self.col.media, second, total_budget_bytes=combined_size - 1)
        self.assertEqual(raised.exception.code, "storage_limit")
        self.assertFalse(self.col.media.have(second_name))
        self.assertEqual(offline.snapshot_storage(self.col.media), offline.SnapshotStorage(len(data), 1))
        self.assertEqual(self.apply(second, combined_size).bytes_used, len(second_data))
        self.assertEqual(offline.snapshot_storage(self.col.media), offline.SnapshotStorage(combined_size, 2))
        current = offline.snapshot_filename(self.col.get_note(self.note_id)["ShirabeLookup"])
        self.assertEqual(current, second_name)
        self.assertEqual((Path(self.col.media.dir()) / current).read_bytes(), second_data)
        self.col.media.trash_files([current])
        self.assertIsNone(offline.read_snapshot(self.col.media, current))
        self.assertEqual(self.apply(second, combined_size).bytes_used, len(second_data))
        self.assertEqual((Path(self.col.media.dir()) / current).read_bytes(), second_data)
        self.assertEqual(offline.read_snapshot(self.col.media, current), second.snapshot)


if __name__ == "__main__":
    unittest.main()
