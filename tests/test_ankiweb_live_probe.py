from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from anki.collection import Collection
    from anki.buildinfo import version
    from anki.sync import SyncAuth, SyncStatus
    from ankiweb_live_probe import LiveAccountProbe, copy_collection, require_preserved
except ImportError:
    Collection, version = None, None


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class LiveAccountProbeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        collection = Collection(str(self.source / "collection.anki2"))
        collection.set_config("fsrs", True)
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["Existing front", "Existing back"]
        self.deck_id = collection.decks.id("Existing deck")
        collection.add_note(note, self.deck_id)
        self.note_id = note.id
        collection.close()
        self.probe = LiveAccountProbe(self.root / "copies", self.source, auth=SyncAuth(hkey="unused"))
        self.addCleanup(self.probe.close)

    def copied(self):
        path = self.root / "copies" / "desktop" / "collection.anki2"
        copy_collection(self.source / "collection.anki2", path)
        collection = Collection(str(path))
        self.addCleanup(collection.close)
        return collection

    def test_copy_never_overwrites_an_existing_destination(self):
        collection = self.copied()
        with self.assertRaisesRegex(ValueError, "must be new"):
            copy_collection(self.source / "collection.anki2", Path(collection.path))

    def test_unpublished_original_changes_are_not_uploaded(self):
        with patch.object(Collection, "sync_status") as status, self.assertRaisesRegex(AssertionError, "unpublished"):
            self.probe.clone("desktop")
        status.assert_not_called()

    def test_existing_note_edit_refuses_sync_before_network_io(self):
        collection = self.copied()
        note = collection.get_note(self.note_id)
        note.fields[0] = "Accidentally changed"
        collection.update_note(note)
        with patch.object(collection, "sync_status") as status, self.assertRaisesRegex(AssertionError, "Existing collection"):
            self.probe.sync(collection)
        status.assert_not_called()

    def test_existing_deck_removal_refuses_sync_before_network_io(self):
        collection = self.copied()
        collection.decks.remove([self.deck_id])
        with patch.object(collection, "sync_status") as status, self.assertRaisesRegex(AssertionError, "Existing collection"):
            self.probe.sync(collection)
        status.assert_not_called()

    def test_full_sync_never_calls_sync_collection(self):
        collection = self.copied()
        with patch.object(collection, "sync_status", return_value=SyncStatus(required=SyncStatus.FULL_SYNC)), \
                patch.object(collection, "sync_collection") as sync, self.assertRaisesRegex(AssertionError, "Full sync"):
            self.probe.sync(collection)
        sync.assert_not_called()

    def test_test_deck_additions_preserve_every_original_row(self):
        collection = self.copied()
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["Test front", "Test back"]
        collection.add_note(note, collection.decks.id("Shirabe Test disposable"))
        self.assertEqual(1, require_preserved(self.probe.baseline, collection.path)["notes"])
        self.assertEqual(self.probe.source_digest, self.probe.digest())
