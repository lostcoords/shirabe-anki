from pathlib import Path
import subprocess
import unittest

from check_against_anki import anki_python


class SnapshotPackage(unittest.TestCase):
    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_real_prepare_export_import_keeps_per_note_media(self):
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("snapshot_export_probe.py"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
