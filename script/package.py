"""Package the add-on using the same archive checks as the desktop harness."""

import sys
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from desktop_check import package

if not (ROOT / "src/web/shirabe-card.js").is_file():
    raise SystemExit("Run npm run build first")
output = ROOT / "artifacts"
output.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="package-", dir=ROOT / "tmp") as scratch:
    archive = package(Path(scratch))
    shutil.copy2(archive, output / "shirabe-anki.ankiaddon")
    shutil.copy2(Path(scratch) / "package-manifest.json", output / "package-manifest.json")
print(output / "shirabe-anki.ankiaddon")
