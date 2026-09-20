#!/usr/bin/env python3
"""Run add-on tests; use Anki's Python to include collection and Qt checks."""

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))


def needs_anki(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    return any(
        (isinstance(node, ast.Import) and any(name.name.split(".")[0] in {"anki", "aqt"} for name in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"anki", "aqt"})
        for node in ast.walk(tree)
    )


def suite():
    loader = unittest.defaultTestLoader
    if importlib.util.find_spec("anki"):
        return loader.discover(str(HERE), pattern="test_*.py")

    tests = unittest.TestSuite()
    for path in sorted(HERE.glob("test_*.py")):
        if not needs_anki(path):
            tests.addTests(loader.loadTestsFromName(path.stem))
    return tests

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=1).run(suite())
    sys.exit(0 if result.wasSuccessful() else 1)
