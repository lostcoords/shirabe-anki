"""Compare source and Shirabe packages with native Anki in disposable collections."""

from pathlib import Path
import json
import shutil
import tempfile
import zipfile

from fixture_deck import save_json


def verify(root):
    from anki.collection import Collection
    from anki.import_export_pb2 import ImportAnkiPackageRequest, ImportAnkiPackageOptions
    from study_corpus import snapshot

    package = root / "shirabe-export.apkg"

    def open_package(path, folder):
        folder.mkdir()
        with zipfile.ZipFile(path) as archive:
            collection_path = folder / "collection.anki2"
            entry = "collection.anki21" if "collection.anki21" in archive.namelist() else "collection.anki2"
            collection_path.write_bytes(archive.read(entry))
            col = Collection(str(collection_path))
            try:
                for index, name in json.loads(archive.read("media")).items():
                    actual_name = col.media.write_data(name, archive.read(index))
                    if actual_name != name:
                        raise AssertionError(f"Anki renamed source media {name!r}")
                return snapshot(col)
            finally:
                col.close()

    with tempfile.TemporaryDirectory(prefix="export-check-", dir=root) as scratch:
        folder = Path(scratch)
        expected = open_package(root / "study-corpus.apkg", folder / "source")
        actual = open_package(package, folder / "exported")
        checks = {
            "collection_clock_and_settings": expected["collection"] == actual["collection"],
            "notes": expected["notes"] == actual["notes"],
            "card_rows_and_rendering": expected["cards"] == actual["cards"],
            "review_history": expected["reviews"] == actual["reviews"],
            "media": expected["media"] == actual["media"],
            "note_types": expected["note_types"] == actual["note_types"],
            "decks": expected["decks"] == actual["decks"],
            "presets": expected["presets"] == actual["presets"],
        }
        save_json(root / "shirabe-export-source.json", expected)
        save_json(root / "shirabe-export-opened.json", actual)
        seed_folder = folder / "seed"
        seed_folder.mkdir()
        seed_path = seed_folder / "collection.anki2"
        col = Collection(str(seed_path))
        col.crt = expected["collection"]["creation_time"] - 10 * 86400
        for deck in expected["decks"]:
            if not deck["dyn"]:
                col.decks.id(deck["name"])
        col.close()

        reference_folder = folder / "reference"
        reference_folder.mkdir()
        shutil.copyfile(seed_path, reference_folder / "collection.anki2")
        col = Collection(str(reference_folder / "collection.anki2"))
        try:
            col.import_anki_package(ImportAnkiPackageRequest(package_path=str(root / "study-corpus.apkg"),
                options=ImportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True)))
            reference = snapshot(col)
        finally:
            col.close()
        save_json(root / "shirabe-export-reference-imported.json", reference)
        imported_folder = folder / "imported"
        imported_folder.mkdir()
        shutil.copyfile(seed_path, imported_folder / "collection.anki2")
        col = Collection(str(imported_folder / "collection.anki2"))
        try:
            col.import_anki_package(ImportAnkiPackageRequest(package_path=str(package),
                options=ImportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True)))
            imported = snapshot(col)
        finally:
            col.close()
        save_json(root / "shirabe-export-imported.json", imported)
        checks["apkg_destination_clock_shifted"] = imported["collection"]["creation_time"] == expected["collection"]["creation_time"] - 10 * 86400
        # Anki stamps deck modification times when applying package settings.
        checks["apkg_native_import_graph"] = {
            **reference, "decks": [{key: value for key, value in deck.items() if key != "mod"} for deck in reference["decks"]],
        } == {
            **imported, "decks": [{key: value for key, value in deck.items() if key != "mod"} for deck in imported["decks"]],
        }
        expected_notes = {note["guid"]: note for note in expected["notes"]}
        imported_notes = {note["guid"]: note for note in imported["notes"]}
        checks["apkg_notes"] = expected_notes.keys() == imported_notes.keys() and all(
            note["fields"] == imported_notes[guid]["fields"] and note["tags"] == imported_notes[guid]["tags"]
            for guid, note in expected_notes.items()
        )

        def faces(data):
            notes = {note["id"]: note["guid"] for note in data["notes"]}
            return {(notes[card["nid"]], card["ord"]): (card["question"], card["answer"]) for card in data["cards"]}

        def schedules(data):
            notes = {note["id"]: note["guid"] for note in data["notes"]}
            decks = {deck["id"]: deck["name"] for deck in data["decks"]}
            rows = {}
            for card in data["cards"]:
                row = {key: card[key] for key in ("type", "queue", "due", "ivl", "factor", "reps", "lapses", "left", "flags", "data")}
                if card["queue"] in (2, 3):
                    row["due"] -= data["collection"]["today"]
                row["deck"] = decks[card["did"]]
                rows[notes[card["nid"]], card["ord"]] = row
            return rows

        checks["apkg_cards_and_rendering"] = faces(expected) == faces(imported)
        checks["apkg_schedules_and_decks"] = schedules(expected) == schedules(imported)
        checks["apkg_media"] = expected["media"] == imported["media"]
        checks["apkg_review_history"] = expected["reviews"] == imported["reviews"]
        report = {"checks": checks, "passed": all(checks.values()), "package": str(package)}
        save_json(root / "shirabe-export-report.json", report)
        return report


if __name__ == "__main__":
    import sys
    from fixture_deck import require_anki

    require_anki()
    report = verify(Path(sys.argv[1]).resolve())
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)
