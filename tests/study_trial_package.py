"""Export the saved mixed-card samples for a disposable browser study trial."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from fixture_deck import output_directory, require_anki, save_json


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export(report_path, root):
    from anki.collection import Collection
    from anki.import_export_pb2 import ExportAnkiPackageOptions
    from anki.collection import NoteIdsLimit

    scratch = Path(__file__).resolve().parents[2] / "tmp"
    report_path = report_path.resolve(strict=True)
    root = root.resolve(strict=True)
    source = (report_path.parent / "collection.anki2").resolve(strict=True)
    package = root / "mixed-card-trial.apkg"
    if not all(path.is_relative_to(scratch) for path in (report_path, source, root)) or package.exists():
        raise ValueError("Use a saved snapshot and a fresh output directory under workspace tmp")
    report = json.loads(report_path.read_text())
    originals = {path: digest(path) for path in (report_path, source)}
    fixtures = report["fixtures"]
    if not fixtures:
        raise ValueError("The saved report contains no trial notes")
    with tempfile.TemporaryDirectory(prefix="trial-export-", dir=root) as directory:
        destination = Path(directory) / "collection.anki2"
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as origin:
            with sqlite3.connect(destination) as copied:
                origin.backup(copied)
        collection = Collection(str(destination))
        try:
            for fixture in fixtures:
                note = collection.get_note(fixture["id"])
                if (note.guid, note.mid, note.fields) != (fixture["guid"], fixture["mid"], fixture["fields"]):
                    raise ValueError(f"The saved sample no longer matches note {fixture['id']}")
            for name, metadata in report["sample_media"].items():
                if not metadata.get("sha256"):
                    continue
                if Path(name).name != name or name in (".", ".."):
                    raise ValueError("Sample media must use flat filenames")
                media = (report_path.parent / "collection.media" / name).resolve(strict=True)
                if not media.is_relative_to(scratch) or digest(media) != metadata["sha256"]:
                    raise ValueError(f"The saved media no longer matches {name}")
                originals[media] = metadata["sha256"]
                shutil.copyfile(media, Path(collection.media.dir()) / name)
            collection.export_anki_package(out_path=str(package), limit=NoteIdsLimit([note["id"] for note in fixtures]),
                options=ExportAnkiPackageOptions(with_scheduling=False, with_deck_configs=True, with_media=True, legacy=True))
        finally:
            collection.close()
            if any(digest(path) != expected for path, expected in originals.items()):
                raise AssertionError("The saved source snapshot changed")
    result = {"package": str(package), "sha256": digest(package), "notes": len(fixtures),
              "cards": sum(len(note["cards"]) for note in fixtures), "note_types": len({note["mid"] for note in fixtures}),
              "source_unchanged": True, "scheduling": "fresh trial cards; original progress is not included",
              "scope": "Saved sample media only. Native export may omit unprefixed CSS-only assets. This package is not paired with the original profile."}
    save_json(root / "trial-report.json", result)
    return result


if __name__ == "__main__":
    require_anki()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.report, output_directory(args.output)), indent=2))
