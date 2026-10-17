"""Package the add-on using the same archive checks as the desktop harness."""

import sys
import json
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from desktop_check import package

if not (ROOT / "addon/src/web/shirabe-card.js").is_file():
    raise SystemExit("Run npm run build first")
output = ROOT / "artifacts"
output.mkdir(exist_ok=True)
(ROOT / "tmp").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="package-", dir=ROOT / "tmp") as scratch:
    archive = package(Path(scratch))
    shutil.copy2(archive, output / "shirabe-anki.ankiaddon")
    manifest = json.loads((Path(scratch) / "package-manifest.json").read_text())
    manifest["package"] = "shirabe-anki.ankiaddon"
    (output / "package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(output / "shirabe-anki.ankiaddon")
