import hashlib
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

import support
from shirabe_addon import assets


class Media:
    def __init__(self):
        self.files = {}

    def write_data(self, name, data):
        self.files[name] = data
        return name


class LocalAssets(unittest.TestCase):
    def test_versions_are_content_addressed_and_only_our_assets_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            web = Path(directory)
            (web / "shirabe-card.js").write_bytes(b"card")
            (web / "attach.js").write_bytes(b"attach")
            media = Media()
            first = assets.install(media, web)
            self.assertEqual(first, assets.install(media, web))
            self.assertEqual(len(media.files), 2)
            self.assertTrue(all(name.startswith("_shirabe_") for name in media.files))
            (web / "attach.js").write_bytes(b"new attach")
            second = assets.install(media, web)
            self.assertEqual(first["shirabe-card.js"], second["shirabe-card.js"])
            self.assertNotEqual(first["attach.js"], second["attach.js"])
            self.assertIn(first["attach.js"], media.files)

    def test_missing_bundle_fails_before_writing_any_media(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Media()
            with self.assertRaises(FileNotFoundError):
                assets.install(media, Path(directory))
            self.assertEqual(media.files, {})

    def test_verification_accepts_only_bounded_regular_content_addressed_bundles(self):
        with tempfile.TemporaryDirectory() as directory:
            media = SimpleNamespace(dir=lambda: directory)
            data = b"// local bundle\n" * 10000
            digest = hashlib.sha256(data).hexdigest()[:20]
            for suffix in ("attach.js", "shirabe-card.js"):
                name = f"_shirabe_{digest}_{suffix}"
                (Path(directory) / name).write_bytes(data)
                self.assertEqual(assets.verified_bundle_size(media, name), len(data))
            for name in (None, "../" + name, name + ".old", name.upper(), f"_shirabe_{digest}_other.js"):
                self.assertIsNone(assets.verified_bundle_size(media, name))
            bad = "_shirabe_" + "0" * 20 + "_attach.js"
            path = Path(directory) / bad
            path.write_bytes(b"wrong hash")
            self.assertIsNone(assets.verified_bundle_size(media, bad))
            path.write_bytes(b"")
            self.assertIsNone(assets.verified_bundle_size(media, bad))
            with path.open("wb") as stream:
                stream.truncate(assets.MAX_BUNDLE_BYTES + 1)
            with patch.object(assets.hashlib, "sha256", side_effect=AssertionError("Oversized files must be rejected before hashing")):
                self.assertIsNone(assets.verified_bundle_size(media, bad))
            self.assertEqual(assets.bundle_storage(media), assets.BundleStorage(2 * len(data) + assets.MAX_BUNDLE_BYTES + 1, 3))
            path.unlink()
            path.symlink_to(Path(directory) / f"_shirabe_{digest}_attach.js")
            self.assertIsNone(assets.verified_bundle_size(media, bad))
            self.assertEqual(assets.bundle_storage(media).files, 2)
            path.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(path)
                self.assertIsNone(assets.verified_bundle_size(media, bad))
                self.assertEqual(assets.bundle_storage(media).files, 2)

    def test_installer_rejects_oversized_and_empty_bundles_before_any_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            web = Path(directory)
            (web / "attach.js").write_bytes(b"attach")
            for size in (0, assets.MAX_BUNDLE_BYTES + 1):
                with (web / "shirabe-card.js").open("wb") as stream:
                    stream.truncate(size)
                media = Media()
                with self.assertRaises(ValueError):
                    assets.install(media, web)
                self.assertEqual(media.files, {})
