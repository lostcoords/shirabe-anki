from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from ankiweb_media_probe import PreservedMedia


class PreservedMediaTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "original"
        self.media = self.profile / "collection.media"
        self.media.mkdir(parents=True)
        (self.media / "existing.mp3").write_bytes(b"original bytes")
        self.index = self.profile / "collection.media.db2"
        with closing(sqlite3.connect(self.index)) as db:
            db.execute("CREATE TABLE media (fname TEXT PRIMARY KEY, csum TEXT, mtime INTEGER, dirty INTEGER)")
            db.execute("INSERT INTO media VALUES (?, ?, 0, 0)", ("existing.mp3", hashlib.sha1(b"original bytes").hexdigest()))
            db.commit()

    def test_clone_does_not_share_mutable_files_with_the_source(self):
        probe = PreservedMedia(self.profile)
        destination = self.root / "copied.media"
        probe.copy(destination)
        self.assertNotEqual((self.media / "existing.mp3").stat().st_ino, (destination / "existing.mp3").stat().st_ino)
        (destination / "existing.mp3").write_bytes(b"changed test copy")
        self.assertEqual(1, probe.unchanged())
        self.assertEqual(b"original bytes", (self.media / "existing.mp3").read_bytes())

    def test_missing_media_prevents_any_native_sync(self):
        probe = PreservedMedia(self.profile)
        destination = self.root / "copied.media"
        probe.copy(destination)
        (destination / "existing.mp3").unlink()
        collection = Mock()
        collection.media.dir.return_value = str(destination)
        with self.assertRaisesRegex(AssertionError, "missing or changed"):
            probe.sync(collection, Mock())
        collection.sync_media.assert_not_called()

    def test_changed_media_prevents_any_native_sync(self):
        probe = PreservedMedia(self.profile)
        destination = self.root / "copied.media"
        probe.copy(destination)
        (destination / "existing.mp3").write_bytes(b"changed")
        collection = Mock()
        collection.media.dir.return_value = str(destination)
        with self.assertRaisesRegex(AssertionError, "missing or changed"):
            probe.sync(collection, Mock())
        collection.sync_media.assert_not_called()

    def test_original_profile_is_never_synced(self):
        probe = PreservedMedia(self.profile)
        collection = Mock()
        collection.media.dir.return_value = str(self.media)
        with self.assertRaisesRegex(ValueError, "original profile"):
            probe.sync(collection, Mock())
        collection.sync_media.assert_not_called()

    def test_unindexed_original_file_refuses_the_probe(self):
        (self.media / "unpublished.mp3").write_bytes(b"do not upload")
        with self.assertRaisesRegex(AssertionError, "unindexed"):
            PreservedMedia(self.profile)

    def test_dirty_media_index_refuses_the_probe(self):
        with closing(sqlite3.connect(self.index)) as db:
            db.execute("UPDATE media SET dirty = 1")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unpublished"):
            PreservedMedia(self.profile)

    def test_open_wal_refuses_a_cold_index_copy(self):
        Path(str(self.index) + "-wal").write_bytes(b"pending WAL")
        with self.assertRaisesRegex(ValueError, "Close the source"):
            PreservedMedia(self.profile)

    def test_copy_refuses_an_existing_destination(self):
        probe = PreservedMedia(self.profile)
        destination = self.root / "copied.media"
        destination.mkdir()
        with self.assertRaisesRegex(ValueError, "must be new"):
            probe.copy(destination)

    def test_index_cannot_reference_a_file_outside_its_media_directory(self):
        with closing(sqlite3.connect(self.index)) as db:
            db.execute("UPDATE media SET fname = '../outside.mp3'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "outside its directory"):
            PreservedMedia(self.profile)
