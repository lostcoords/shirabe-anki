import base64
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import support
from offline_fixture import inflected
from shirabe_addon import offline


def envelope(raw, **changes):
    return json.dumps({"formatVersion": 2, "encoding": "gzip-base64", "decodedBytes": len(raw),
                       "data": base64.b64encode(gzip.compress(raw, mtime=0)).decode(), **changes},
                      separators=(",", ":")).encode()


class SnapshotCompressionTests(unittest.TestCase):
    def read_media(self, data, version=2):
        with tempfile.TemporaryDirectory(prefix="shirabe-compressed-snapshot-") as directory:
            name = f"_shirabe_snapshot_v{version}_{hashlib.sha256(data).hexdigest()}.json"
            Path(directory, name).write_bytes(data)
            return offline.read_snapshot(SimpleNamespace(dir=lambda: directory), name)

    def test_compressed_snapshot_retains_exact_offsets_entries_and_inflections(self):
        snapshot = inflected().snapshot
        raw = offline._json(snapshot)
        self.assertEqual(self.read_media(envelope(raw)), snapshot)

    def test_existing_uncompressed_media_remains_readable(self):
        snapshot = inflected().snapshot
        self.assertEqual(self.read_media(offline._json(snapshot), version=1), snapshot)

    def test_saved_encoding_is_deterministic_smaller_and_roundtrips_without_pruning(self):
        snapshot = inflected().snapshot
        name, data = offline.snapshot_file(snapshot)
        self.assertTrue(name.startswith("_shirabe_snapshot_v2_"))
        self.assertEqual(name, f"_shirabe_snapshot_v2_{hashlib.sha256(data).hexdigest()}.json")
        self.assertLess(len(data), len(offline._json(snapshot)))
        self.assertEqual((name, data), offline.snapshot_file(snapshot))
        self.assertEqual(self.read_media(data), snapshot)
        self.assertEqual(offline.snapshot_filename(offline.snapshot_field(name)), name)

    def test_compression_never_makes_tiny_notes_larger(self):
        snapshot = {"formatVersion": 1, "contentVersion": "v", "createdAt": 0, "segments": [], "complete": True}
        name, data = offline.snapshot_file(snapshot)
        self.assertTrue(name.startswith("_shirabe_snapshot_v1_"))
        self.assertEqual(json.loads(data), snapshot)
        self.assertEqual(len(data), len(offline._json(snapshot)))
        self.assertEqual(self.read_media(data, version=1), snapshot)

    def test_equivalent_object_key_order_uses_the_same_media_file(self):
        snapshot = inflected().snapshot
        normalized = offline.validate_snapshot(snapshot)
        reordered = dict(reversed(list(snapshot.items())))
        self.assertEqual(offline.snapshot_file(snapshot), offline.snapshot_file(normalized))
        self.assertEqual(offline.snapshot_file(snapshot), offline.snapshot_file(reordered))

    def test_media_version_must_match_the_stored_format(self):
        raw = offline._json(inflected().snapshot)
        self.assertIsNone(self.read_media(raw, version=2))
        self.assertIsNone(self.read_media(envelope(raw), version=1))

    def test_corrupt_mislabeled_and_oversized_compressed_payloads_are_rejected(self):
        raw = offline._json(inflected().snapshot)
        for changes in ({"decodedBytes": len(raw) + 1}, {"decodedBytes": True}, {"decodedBytes": 0},
                        {"decodedBytes": offline.MAX_SNAPSHOT_BYTES + 1}, {"encoding": "unknown"},
                        {"data": "not-base64"}, {"data": ""}, {"formatVersion": 3}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.read_media(envelope(raw, **changes)))
        self.assertIsNone(self.read_media(envelope(b"x" * (offline.MAX_SNAPSHOT_BYTES + 1), decodedBytes=1)))
        compressed = gzip.compress(raw, mtime=0)
        for bad in (compressed[:-3], compressed[:-8] + b"\x00" * 8, compressed + b"junk"):
            self.assertIsNone(self.read_media(envelope(raw, data=base64.b64encode(bad).decode())))


if __name__ == "__main__":
    unittest.main()
