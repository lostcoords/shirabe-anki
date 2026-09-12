#!/usr/bin/env python3
"""Repackage a test APKG into a uniquely named deck for isolated client review."""

import argparse
import hashlib
from pathlib import Path

from fixture_deck import output_directory, require_anki, save_json


def main():
    require_anki()
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apkg", type=Path)
    parser.add_argument("--name", default="Shirabe Phase0 Native prepared")
    args = parser.parse_args()
    source = args.apkg.resolve(strict=True)
    if not args.name.startswith("Shirabe Phase0 "):
        parser.error("Use an explicitly named Phase0 test deck")
    root = output_directory(None)
    col = Collection(str(root / "collection.anki2"))
    try:
        col.import_anki_package(ImportAnkiPackageRequest(package_path=str(source)))
        before = {nid: col.get_note(nid).fields[:] for nid in col.find_notes("")}
        cards = col.find_cards("")
        deck_id = col.decks.id(args.name)
        col.set_deck(cards, deck_id)
        assert before == {nid: col.get_note(nid).fields for nid in before}
        apkg = root / "native-android.apkg"
        col.export_anki_package(out_path=str(apkg), options=ExportAnkiPackageOptions(with_media=True, legacy=True), limit=DeckIdLimit(deck_id))
        report = {"input": str(source), "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "apkg": str(apkg), "deck": args.name, "notes": list(before), "cards": list(cards),
                  "changed": "Card deck only; note fields, templates and media imported unchanged"}
        save_json(root / "repackage-report.json", report)
        print(apkg)
    finally:
        col.close()


if __name__ == "__main__":
    main()
