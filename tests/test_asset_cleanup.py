import os
from pathlib import Path
import subprocess
import unittest

from check_against_anki import anki_python


class AssetCleanup(unittest.TestCase):
    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_collection_references_and_media_trash(self):
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("asset_cleanup_probe.py"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_cleanup_dialog_needs_neither_sign_in_nor_selected_notes(self):
        for font_size, completed in ((13, False), (28, False), (28, True)):
            with self.subTest(font_size=font_size, completed_before_callback=completed):
                result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("asset_cleanup_ui_probe.py")),
                                         "--font-size", str(font_size), *(["--completed-before-callback"] if completed else [])],
                                        capture_output=True, text=True, timeout=30,
                                        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("serialized QueryOp", result.stdout)


if __name__ == "__main__":
    unittest.main()
