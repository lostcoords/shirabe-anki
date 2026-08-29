"""Stand `src/` up as a package, so tests import it the way Anki does.

The add-on's modules import each other relatively (`from . import net`) because
inside Anki they are a package. Importing `src/__init__.py` to get that would
pull in `aqt`, and a test that needs a GUI is a test nobody runs, so the package
is assembled here without it.
"""

import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

if "shirabe_addon" not in sys.modules:
    package = types.ModuleType("shirabe_addon")
    package.__path__ = [str(SRC)]
    sys.modules["shirabe_addon"] = package
