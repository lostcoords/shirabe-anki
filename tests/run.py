#!/usr/bin/env python3
"""Every test that needs no Anki, which is all of them.

The add-on's own halves (injection, collection reading, linking) are testable
here; the three Anki hooks are not, because a test that imports `aqt` is a test
that needs a GUI.
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
