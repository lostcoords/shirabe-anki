from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from offline_fixture import prepared
from test_offline_audio import M4A, PERMISSION, recording, first_candidate
from shirabe_addon import authoring_media, offline, offline_audio, offline_ui
from anki.collection import Collection, DeckIdLimit
from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest


def with_audio(data=M4A, *, version="v1", partial=False):
    result = prepared(("猫",), content_version=version, static_text_only=partial)
    info = recording(data)
    first_candidate(result.snapshot)["pitch"][0]["recording"] = info
    return replace(result, bytes_used=len(offline._json(result.snapshot)),
                   media={info["filename"]: authoring_media.DownloadedMedia(data, "m4a", "Shirabe", PERMISSION)})


class NativeAudioStorage(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shirabe-offline-audio-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.col = Collection(str(self.root / "collection.anki2"))
        self.addCleanup(self.col.close)
        self.ids = []
        for _ in range(2):
            note = self.col.new_note(self.col.models.by_name("Basic"))
            note["Front"], note["Back"] = "猫", "cat"
            self.col.add_note(note, 1)
            self.ids.append(note.id)

    def apply(self, preparations, limit=offline.MAX_TOTAL_BYTES):
        captures = offline_ui.capture_notes(self.col, self.ids)
        batch = offline_ui.PreparedBatch(rows=list(zip(captures, preparations)))
        with patch.object(offline_ui, "storage_budget", return_value=limit):
            return offline_ui.apply_prepared(self.col, batch, {})

    def test_shared_disk_and_batch_budget_preflight_dedup_refresh_and_damaged_owned_files(self):
        first = with_audio()
        first_name, first_data = offline.snapshot_file(first.snapshot)
        self.assertEqual(first.bytes_used, len(offline._json(first.snapshot)))
        required = len(first_data) + len(M4A)
        with self.assertRaises(offline.SnapshotError):
            offline.persist_snapshot(self.col.media, first, remaining_bytes=required - 1)
        self.assertEqual(offline.snapshot_storage(self.col.media, audio=True).bytes_used, 0)
        result = self.apply([first, first], required - 1)
        self.assertEqual((result.saved, result.bytes_used, result.issues), (0, 0, {"storage_limit"}))
        self.assertEqual(offline.snapshot_storage(self.col.media, audio=True).bytes_used, 0)
        result = self.apply([first, first], required)
        self.assertEqual((result.saved, result.bytes_used), (2, required))
        self.assertEqual((Path(self.col.media.dir()) / first_name).read_bytes(), first_data)
        self.assertEqual(offline.read_snapshot(self.col.media, first_name), first.snapshot)
        self.assertEqual((Path(self.col.media.dir()) / recording()["filename"]).read_bytes(), M4A)
        self.assertEqual(len(result.audio_files), 1)
        self.assertEqual(offline.snapshot_storage(self.col.media, audio=True), offline.SnapshotStorage(required, 2))
        self.assertEqual(self.apply([first, first], required).bytes_used, 0)
        second = with_audio(version="v2")
        second_size = len(offline.snapshot_file(second.snapshot)[1])
        result = self.apply([second, second], required + second_size)
        self.assertEqual(result.bytes_used, second_size)
        review = offline_ui.inspect_cleanup(self.col)
        self.assertEqual(review.audio, offline.SnapshotStorage(len(M4A), 1))
        self.assertEqual(review.snapshots.bytes_used, len(first_data) + second_size)
        self.col.media.write_data("shirabe_" + "0" * 64 + ".m4a", b"damaged")
        usage = offline.snapshot_storage(self.col.media, audio=True)
        self.assertEqual(usage.bytes_used, required + second_size + len(b"damaged"))
        third = with_audio(M4A + b"new", version="v3")
        third_name, third_data = offline.snapshot_file(third.snapshot)
        third_required = len(third_data) + len(M4A + b"new")
        result = self.apply([third, third], usage.bytes_used + third_required - 1)
        self.assertEqual((result.saved, result.bytes_used, result.issues), (0, 0, {"storage_limit"}))
        self.assertEqual(offline.snapshot_storage(self.col.media, audio=True), usage)
        self.assertFalse(self.col.media.have(third_name))
        self.assertFalse(self.col.media.have(recording(M4A + b"new")["filename"]))
        fitted = self.apply([third, third], usage.bytes_used + third_required)
        self.assertEqual((fitted.saved, fitted.bytes_used), (2, third_required))
        self.assertEqual(offline.snapshot_storage(self.col.media, audio=True),
                         offline.SnapshotStorage(usage.bytes_used + third_required, usage.files + 2))
        self.assertEqual((Path(self.col.media.dir()) / third_name).read_bytes(), third_data)
        self.assertEqual(offline.read_snapshot(self.col.media, third_name), third.snapshot)
        self.assertEqual((Path(self.col.media.dir()) / recording(M4A + b"new")["filename"]).read_bytes(), M4A + b"new")

    def test_failed_audio_refresh_retains_prior_references_even_for_static_partial_snapshot(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                for nid in self.ids:
                    note = self.col.get_note(nid)
                    if "ShirabeLookup" in note:
                        note["ShirabeLookup"] = ""
                        self.col.update_note(note)
                first = with_audio(version=str(partial), partial=partial)
                self.apply([first, first])
                before = [self.col.get_note(nid)["ShirabeLookup"] for nid in self.ids]
                self.assertEqual(offline.read_snapshot(self.col.media, offline.snapshot_filename(before[0]))["complete"], not partial)
                failed = prepared(("猫",), content_version="new")
                failed.snapshot["complete"] = False
                failed = replace(failed, coverage=offline.coverage(failed.snapshot), issues=("audio_unavailable",))
                result = self.apply([failed, failed])
                self.assertEqual((result.kept_previous, result.bytes_used), (2, 0))
                self.assertEqual([self.col.get_note(nid)["ShirabeLookup"] for nid in self.ids], before)
                self.assertTrue(self.col.media.have(recording()["filename"]))

    def test_actual_check_media_export_import_and_no_rendered_audio_element(self):
        item = with_audio()
        self.apply([item, item])
        name = recording()["filename"]
        snapshot_name, snapshot_data = offline.snapshot_file(item.snapshot)
        for nid in self.ids:
            note = self.col.get_note(nid)
            reference = note["ShirabeLookup"]
            self.assertIn(name, self.col.media.files_in_str(note.mid, reference))
            self.assertIsNotNone(offline.snapshot_filename(reference))
            for card in note.cards():
                for rendered in (card.question(), card.answer()):
                    self.assertNotIn("<audio", rendered)
                    self.assertNotIn("[sound:", rendered)
                    self.assertNotIn(name, rendered)
        check = self.col.media.check()
        self.assertNotIn(name, check.unused)
        self.assertFalse(check.missing)
        for legacy in (True, False):
            package = self.root / f"audio-{legacy}.apkg"
            self.col.export_anki_package(out_path=str(package), options=ExportAnkiPackageOptions(with_media=True, legacy=legacy), limit=DeckIdLimit(1))
            imported = Collection(str(self.root / f"imported-{legacy}.anki2"))
            try:
                imported.import_anki_package(ImportAnkiPackageRequest(package_path=str(package)))
                self.assertEqual(offline_audio.verified_size(imported.media, name), len(M4A))
                self.assertEqual((Path(imported.media.dir()) / name).read_bytes(), M4A)
                self.assertEqual((Path(imported.media.dir()) / snapshot_name).read_bytes(), snapshot_data)
                self.assertNotIn(name, imported.media.check().unused)
                captures = offline_ui.capture_notes(imported, self.ids)
                self.assertTrue(all(capture.previous == item.snapshot for capture in captures))
            finally:
                imported.close()

    def test_failed_note_commit_rolls_back_models_and_refs_and_leaves_bounded_reviewable_media(self):
        first = with_audio()
        self.apply([first, first])
        fields = [list(self.col.get_note(nid).fields) for nid in self.ids]
        cards = self.col.db.all("select * from cards order by id")
        next_item = with_audio(M4A + b"new", version="new")
        with patch.object(self.col, "update_notes", side_effect=RuntimeError("injected failure")), self.assertRaises(RuntimeError):
            self.apply([next_item, next_item])
        self.assertEqual([self.col.get_note(nid).fields for nid in self.ids], fields)
        self.assertEqual(self.col.db.all("select * from cards order by id"), cards)
        self.assertEqual(offline.snapshot_storage(self.col.media, audio=True).bytes_used,
                         len(offline.snapshot_file(first.snapshot)[1]) + len(offline.snapshot_file(next_item.snapshot)[1])
                         + len(M4A) + len(M4A + b"new"))
        unused = offline_ui.unreferenced_files(self.col)
        self.assertEqual(len(unused), 1)
        self.assertTrue(offline.MEDIA_NAME.fullmatch(unused[0]))

    def test_missing_sync_file_is_repaired_but_changed_or_linked_media_is_not_overwritten(self):
        item = with_audio()
        self.apply([item, item])
        name = recording()["filename"]
        self.col.media.trash_files([name])
        result = self.apply([item, item])
        self.assertEqual(result.bytes_used, len(M4A))
        self.assertEqual(offline_audio.verified_size(self.col.media, name), len(M4A))
        self.col.media.trash_files([name])
        path = Path(self.col.media.dir()) / name
        path.symlink_to(self.root / "missing-target")
        before = [list(self.col.get_note(nid).fields) for nid in self.ids]
        with self.assertRaises(offline.SnapshotError):
            self.apply([item, item])
        self.assertTrue(path.is_symlink())
        self.assertIsNone(offline_audio.verified_size(self.col.media, name))
        self.assertNotIn(name, offline_ui.unreferenced_files(self.col))
        self.assertEqual([self.col.get_note(nid).fields for nid in self.ids], before)

    def test_reviewed_trash_protects_retained_snapshots_new_note_references_and_corrupt_files(self):
        item = with_audio()
        stored = offline.persist_snapshot(self.col.media, item)
        name = recording()["filename"]
        other = authoring_media.store_selected_media(self.col, authoring_media.DownloadedMedia(M4A + b"authoring", "m4a", "Shirabe", PERMISSION)).filename
        bad = self.col.media.write_data("shirabe_" + "b" * 64 + ".m4a", M4A)
        photo = self.col.media.write_data("personal.jpg", b"photo")
        review = offline_ui.inspect_cleanup(self.col)
        self.assertIn(stored.filename, review.names)
        self.assertIn(other, review.names)
        self.assertNotIn(name, review.names)
        self.assertNotIn(bad, review.names)
        note = self.col.get_note(self.ids[0])
        note["Back"] = f"[sound:{other}]"
        self.col.update_note(note)
        self.assertEqual(offline_ui.trash_reviewed(self.col, review.names), [stored.filename])
        self.assertTrue(self.col.media.have(name))
        following = offline_ui.inspect_cleanup(self.col)
        self.assertEqual(following.names, (name,))
        self.assertEqual(offline_ui.trash_reviewed(self.col, following.names), [name])
        self.assertTrue(all(self.col.media.have(kept) for kept in (other, bad, photo)))
        self.col.media.restore_trash()
        self.assertEqual(offline_audio.verified_size(self.col.media, name), len(M4A))
        self.assertIsNotNone(offline.read_snapshot(self.col.media, stored.filename))


if __name__ == "__main__":
    unittest.main()
