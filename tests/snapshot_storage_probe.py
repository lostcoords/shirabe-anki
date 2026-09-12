from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

import support
from offline_fixture import identify, parse, prepared
from shirabe_addon import assets, offline, offline_ui
from anki.collection import Collection


class NativeSnapshotStorage(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shirabe-snapshot-storage-")
        self.addCleanup(directory.cleanup)
        self.col = Collection(str(Path(directory.name) / "collection.anki2"))
        self.addCleanup(self.col.close)
        self.ids = []
        for _ in range(2):
            note = self.col.new_note(self.col.models.by_name("Basic"))
            note["Front"] = "猫"
            self.col.add_note(note, 1)
            self.ids.append(note.id)

    def apply(self, preparations, limit=None):
        captures = offline_ui.capture_notes(self.col, self.ids)
        batch = offline_ui.PreparedBatch(rows=list(zip(captures, preparations)))
        with patch.object(offline_ui, "storage_budget", return_value=limit or offline.MAX_TOTAL_BYTES):
            return offline_ui.apply_prepared(self.col, batch, {})

    def test_duplicate_versions_and_repeated_batches_charge_only_new_physical_bytes(self):
        first = prepared(("猫",))
        first_size = len(offline.snapshot_file(first.snapshot)[1])
        self.assertEqual(first.bytes_used, len(offline._json(first.snapshot)))
        self.assertLess(first_size, first.bytes_used)
        result = self.apply([first, first])
        self.assertEqual((result.saved, result.bytes_used), (2, first_size))
        self.assertEqual(offline.snapshot_storage(self.col.media), offline.SnapshotStorage(first_size, 1))
        refs = [self.col.get_note(note_id)["ShirabeLookup"] for note_id in self.ids]
        self.assertEqual(refs[0], refs[1])
        reused = self.apply([first, first], first_size)
        self.assertEqual((reused.saved, reused.bytes_used), (2, 0))
        second = prepared(("猫",), content_version="new-version")
        second_size = len(offline.snapshot_file(second.snapshot)[1])
        refreshed = self.apply([second, second])
        self.assertEqual((refreshed.saved, refreshed.bytes_used), (2, second_size))
        used = first_size + second_size
        self.assertEqual(offline.snapshot_storage(self.col.media), offline.SnapshotStorage(used, 2))
        self.assertIsNotNone(offline.read_snapshot(self.col.media, offline.snapshot_filename(refs[0])))
        third = prepared(("猫",), content_version="third-version")
        third_name, third_data = offline.snapshot_file(third.snapshot)
        third_size = len(third_data)
        full = self.apply([second, third], used + third_size - 1)
        self.assertEqual((full.saved, full.bytes_used, full.issues), (1, 0, {"storage_limit"}))
        self.assertEqual(offline.snapshot_storage(self.col.media).bytes_used, used)
        self.assertFalse(self.col.media.have(third_name))
        self.assertEqual(self.col.get_note(self.ids[0])["ShirabeLookup"], self.col.get_note(self.ids[1])["ShirabeLookup"])
        fitted = self.apply([second, third], used + third_size)
        self.assertEqual((fitted.saved, fitted.bytes_used), (2, third_size))
        for item in (first, second, third):
            name, data = offline.snapshot_file(item.snapshot)
            self.assertEqual((Path(self.col.media.dir()) / name).read_bytes(), data)
            self.assertEqual(offline.read_snapshot(self.col.media, name), item.snapshot)
        inventory = offline_ui.inspect_cleanup(self.col)
        self.assertEqual(inventory.snapshots, offline.SnapshotStorage(used + third_size, 3))
        self.assertEqual(inventory.code, assets.bundle_storage(self.col.media))
        self.assertGreater(inventory.code.bytes_used, 0)

    def test_corrupt_owned_generation_counts_and_batch_remaining_is_checked_before_write(self):
        media = self.col.media
        corrupt_data = b"broken"
        corrupt = media.write_data("_shirabe_snapshot_v1_" + "0" * 64 + ".json", corrupt_data)
        item = prepared(("猫",))
        name, data = offline.snapshot_file(item.snapshot)
        size = len(data)
        before = offline.snapshot_storage(media)
        self.assertEqual(before, offline.SnapshotStorage(len(corrupt_data), 1))
        with self.assertRaises(offline.SnapshotError):
            offline.persist_snapshot(media, item, remaining_bytes=size - 1)
        self.assertFalse(media.have(name))
        with self.assertRaises(offline.SnapshotError) as raised:
            offline.persist_snapshot(media, item, total_budget_bytes=size + before.bytes_used - 1)
        self.assertEqual(raised.exception.code, "storage_limit")
        self.assertEqual(offline.snapshot_storage(media), before)
        stored = offline.persist_snapshot(media, item, remaining_bytes=size, total_budget_bytes=size + before.bytes_used)
        reused = offline.persist_snapshot(media, item, remaining_bytes=0, total_budget_bytes=size + before.bytes_used)
        self.assertEqual((stored.filename, stored.bytes_used, reused.filename, reused.bytes_used), (name, size, name, 0))
        self.assertEqual((Path(media.dir()) / name).read_bytes(), data)
        self.assertEqual(offline.read_snapshot(media, name), item.snapshot)
        self.assertEqual(offline.snapshot_storage(media), offline.SnapshotStorage(size + before.bytes_used, 2))
        self.assertTrue(media.have(corrupt))
        self.assertNotIn(corrupt, offline_ui.unreferenced_snapshots(self.col))

    def test_incomplete_dictionary_revision_keeps_prior_complete_reference_and_date(self):
        first = prepared(("猫", "犬"))
        self.apply([first, first])
        references = [self.col.get_note(note_id)["ShirabeLookup"] for note_id in self.ids]
        partial = prepared(("猫", "犬"), content_version="updated-dictionaries", budget=offline.Budget(max_requests=2))
        self.assertFalse(partial.complete)
        self.assertEqual(partial.coverage.prepared, 1)
        result = self.apply([partial, partial])
        self.assertEqual((result.saved, result.kept_previous, result.bytes_used), (2, 2, 0))
        self.assertEqual([self.col.get_note(note_id)["ShirabeLookup"] for note_id in self.ids], references)
        for reference in references:
            self.assertEqual(offline.read_snapshot(self.col.media, offline.snapshot_filename(reference)), first.snapshot)

    def test_later_prepared_date_is_a_counted_new_generation_even_without_dictionary_changes(self):
        first = prepared(("猫",))
        later = offline.prepare_snapshot(["猫"], parse=parse, identify=identify,
                                         content_version=first.snapshot["contentVersion"], allowed_dictionaries={"jmdict"},
                                         now=lambda: 1_700_000_001)
        self.assertEqual(later.snapshot, {**first.snapshot, "createdAt": 1_700_000_001_000})
        self.apply([first, first])
        result = self.apply([later, later])
        first_size = len(offline.snapshot_file(first.snapshot)[1])
        later_name, later_data = offline.snapshot_file(later.snapshot)
        self.assertEqual(result.bytes_used, len(later_data))
        self.assertEqual(offline.snapshot_storage(self.col.media), offline.SnapshotStorage(first_size + len(later_data), 2))
        self.assertEqual((Path(self.col.media.dir()) / later_name).read_bytes(), later_data)
        self.assertEqual(offline.read_snapshot(self.col.media, later_name), later.snapshot)

    def test_mixed_v1_v2_cleanup_rechecks_references_and_restores_exact_media(self):
        media = self.col.media
        generations = []
        payloads = {}
        for version in ("retained", "unused"):
            item = prepared(("猫",), content_version=version)
            raw = offline._json(item.snapshot, sort_keys=True)
            legacy = media.write_data(f"_shirabe_snapshot_v1_{hashlib.sha256(raw).hexdigest()}.json", raw)
            name, data = offline.snapshot_file(item.snapshot)
            self.assertTrue(name.startswith("_shirabe_snapshot_v2_"))
            stored = offline.persist_snapshot(media, item)
            self.assertEqual((stored.filename, stored.bytes_used), (name, len(data)))
            generations.append((legacy, name))
            payloads.update({legacy: (raw, item.snapshot), name: (data, item.snapshot)})
        for note_id, name in zip(self.ids, generations[0]):
            note = self.col.get_note(note_id)
            note["Back"] = offline.snapshot_field(name)
            self.col.update_note(note)
        usage = offline.SnapshotStorage(sum(len(data) for data, _ in payloads.values()), len(payloads))
        review = offline_ui.inspect_cleanup(self.col)
        self.assertEqual(review.snapshots, usage)
        self.assertCountEqual(review.names, generations[1])
        note = self.col.get_note(self.ids[0])
        note["Back"] += offline.snapshot_field(generations[1][0])
        self.col.update_note(note)
        self.assertEqual(offline_ui.trash_reviewed(self.col, review.names), [generations[1][1]])
        self.assertTrue(all(media.have(name) for name in (*generations[0], generations[1][0])))
        self.assertFalse(media.have(generations[1][1]))
        note["Back"] = offline.snapshot_field(generations[0][0])
        self.col.update_note(note)
        self.assertEqual(offline_ui.trash_reviewed(self.col, review.names), [generations[1][0]])
        self.assertTrue(all(media.have(name) for name in generations[0]))
        media.restore_trash()
        self.assertEqual(offline.snapshot_storage(media), usage)
        for name, (data, snapshot) in payloads.items():
            self.assertEqual((Path(media.dir()) / name).read_bytes(), data)
            self.assertEqual(offline.read_snapshot(media, name), snapshot)


if __name__ == "__main__":
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeSnapshotStorage))
    if result.wasSuccessful():
        print("native snapshot storage passed")
    raise SystemExit(not result.wasSuccessful())
