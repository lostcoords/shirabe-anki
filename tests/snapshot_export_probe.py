import json
from pathlib import Path
import tempfile
import unittest

import support
from offline_fixture import prepared
from shirabe_addon import assets, offline, offline_ui
from anki.collection import Collection, DeckIdLimit
from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest


class SnapshotExport(unittest.TestCase):
    def test_native_prepare_exports_and_imports_every_per_note_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="shirabe-snapshot-export-") as directory:
            root = Path(directory)
            col = Collection(str(root / "source.anki2"))
            try:
                model = col.models.by_name("Basic")
                note_ids = []
                payloads = []
                for front, back in (("😀猫は猫", "犬"), ("鳥", "猫")):
                    note = col.new_note(model)
                    note["Front"] = front
                    note["Back"] = back
                    col.add_note(note, 1)
                    note_ids.append(note.id)
                    payloads.append(prepared((front, back)))
                captured = offline_ui.capture_notes(col, note_ids)
                result = offline_ui.apply_prepared(col, offline_ui.PreparedBatch(rows=list(zip(captured, payloads))), {})
                self.assertEqual(result.saved, 2)
                expected = {offline.persist_snapshot(col.media, payload).filename: payload.snapshot for payload in payloads}
                self.assertEqual(len(expected), 2)
                decoy = offline.persist_snapshot(col.media, prepared(content_version="unreferenced")).filename
                templates = json.dumps(col.models.all())
                for name in expected:
                    self.assertNotIn(name, templates, "Per-note filenames must not be hardcoded into templates")
                code = assets.install(col.media)
                original_fields = {note_id: col.get_note(note_id).fields for note_id in note_ids}
                for legacy in (True, False):
                    col.export_anki_package(out_path=str(root / f"prepared-{legacy}.apkg"),
                                           options=ExportAnkiPackageOptions(with_media=True, legacy=legacy), limit=DeckIdLimit(1))
            finally:
                col.close()

            for legacy in (True, False):
                with self.subTest(legacy=legacy):
                    imported = Collection(str(root / f"imported-{legacy}.anki2"))
                    try:
                        imported.import_anki_package(ImportAnkiPackageRequest(package_path=str(root / f"prepared-{legacy}.apkg")))
                        for name, payload in expected.items():
                            self.assertTrue(imported.media.have(name), f"Prepared snapshot was omitted from the APKG: {name}")
                            self.assertEqual(offline.read_snapshot(imported.media, name), payload)
                        self.assertFalse(imported.media.have(decoy))
                        self.assertTrue(all(imported.media.have(name) for name in code.values()))
                        for note_id in note_ids:
                            note = imported.get_note(note_id)
                            self.assertEqual(note.fields, original_fields[note_id])
                            filename = offline.snapshot_filename(note["ShirabeLookup"])
                            self.assertIn(filename, expected)
                            for card in note.cards():
                                self.assertIn(f'data-shirabe-snapshot>{filename}</span>', card.answer())
                                for rendered in (card.question(), card.answer()):
                                    for name in expected:
                                        self.assertNotIn(f'src="{name}"', rendered, "The export marker must not render an image")
                            self.assertIn("{{text:ShirabeLookup}}", note.note_type()["tmpls"][0]["afmt"])
                        self.assertTrue(set(expected).isdisjoint(offline_ui.unreferenced_files(imported)))
                        captures = offline_ui.capture_notes(imported, note_ids)
                        self.assertEqual({capture.previous_name: capture.previous for capture in captures}, expected)
                    finally:
                        imported.close()


if __name__ == "__main__":
    unittest.main()
