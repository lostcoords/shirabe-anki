from paths import SCRATCH
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from study_linked_trial import configuration


class StudyLinkedTrialTest(unittest.TestCase):
    def setUp(self):
        scratch = SCRATCH
        self.temporary = tempfile.TemporaryDirectory(prefix="study-linked-trial-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "sample.apkg"
        self.package.write_bytes(b"fixture package")
        self.descriptor = {"kind": "shirabe-linked-development-trial", "origin": "http://127.0.0.1:3917",
                           "package": str(self.package), "package_sha256": hashlib.sha256(self.package.read_bytes()).hexdigest()}
        self.save()

    def save(self):
        (self.root / "trial.json").write_text(json.dumps(self.descriptor))

    def test_a_private_saved_trial_can_resume(self):
        result = configuration(self.root)
        self.assertTrue(result["retained_trial"])
        self.assertEqual(result["directory"], str(self.root))
        self.assertEqual(result["origin"], self.descriptor["origin"])

    def test_foreign_or_ambiguous_origins_refuse_before_opening_anki(self):
        for origin in ("https://shirabe.org", "http://localhost:3917", "http://127.0.0.1:3917/path",
                       "http://user@127.0.0.1:3917", "http://127.0.0.1:3917?x=1"):
            with self.subTest(origin=origin):
                self.descriptor["origin"] = origin
                self.save()
                with self.assertRaises(ValueError):
                    configuration(self.root)

    def test_changed_source_package_refuses(self):
        self.package.write_bytes(b"changed package")
        with self.assertRaisesRegex(ValueError, "package changed"):
            configuration(self.root)

    def test_collection_symlinks_and_hardlinks_refuse(self):
        collection = self.root / "collection.anki2"
        collection.symlink_to(self.package)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            configuration(self.root)
        collection.unlink()
        os.link(self.package, collection)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            configuration(self.root)

    def test_the_retained_marker_is_required(self):
        self.descriptor["kind"] = "ordinary-profile"
        self.save()
        with self.assertRaises(ValueError):
            configuration(self.root)

    def test_an_unmarked_directory_refuses(self):
        with tempfile.TemporaryDirectory(prefix="other-", dir=self.root.parent) as directory:
            with self.assertRaisesRegex(ValueError, "retained study-linked-trial"):
                configuration(directory)


if __name__ == "__main__":
    unittest.main()
