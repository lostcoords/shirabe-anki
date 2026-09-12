#!/usr/bin/env python3
"""Build a fresh test deck from native-prepared cards, without opening a user profile."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re

from fixture_deck import output_directory, require_anki, save_json


def build(root, source, name, recording_url=None):
    import support
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest
    from shirabe_addon import assets, template
    from shirabe_addon.authoring_media import LicensedMedia, download_selected_media, store_selected_media

    if not name.startswith("Shirabe Test "):
        raise ValueError("Use a clearly named Shirabe Test deck.")
    col = Collection(str(root / "collection.anki2"))
    try:
        col.import_anki_package(ImportAnkiPackageRequest(package_path=str(source)))
        originals = [col.get_note(nid) for nid in col.find_notes("")]
        if not originals or any("ShirabeLookup" not in n for n in originals):
            raise ValueError("Use an APKG prepared through the native offline workflow.")
        installed = assets.install(col.media)
        deck_id = col.decks.id(name)
        models, notes = {}, []
        for original in originals:
            if original.mid not in models:
                model = copy.deepcopy(original.note_type())
                model["id"] = 0
                model["name"] = name + " " + model["name"]
                for card in model["tmpls"]:
                    card["qfmt"] = template.with_question_guard(card["qfmt"])
                    card["afmt"] = template.with_block(card["afmt"], template.PRODUCTION,
                        template.CLIENT_ID, "click", assets=installed, snapshot_field=True)
                models[original.mid] = col.models.get(col.models.add_dict(model).id)
            note = col.new_note(models[original.mid])
            note.fields = original.fields[:]
            note.tags = ["shirabe_ankiweb_test"]
            col.add_note(note, deck_id)
            assert note.id != original.id and note.guid != original.guid
            notes.append(note)
        audio = None
        if recording_url:
            if not re.fullmatch(r"https://cdn\.shirabe\.org/audio/words/[a-f0-9]{64}\.m4a", recording_url):
                raise ValueError("Choose an approved Shirabe word recording.")
            selection = LicensedMedia(recording_url, "Shirabe", "Permitted for Anki deck synchronization and export")
            downloaded = download_selected_media(selection, allowed_hosts={"cdn.shirabe.org"})
            audio = store_selected_media(col, downloaded)
            model = copy.deepcopy(col.models.by_name("Basic"))
            model["id"] = 0
            model["name"] = name + " Audio"
            model = col.models.get(col.models.add_dict(model).id)
            note = col.new_note(model)
            note["Front"] = "猫 / ねこ (audio sync test)"
            note["Back"] = audio.field_html + "<br>Shirabe<br>Permitted for Anki deck synchronization and export"
            note.tags = ["shirabe_ankiweb_test"]
            col.add_note(note, deck_id)
            notes.append(note)
        apkg = root / "shirabe-ankiweb-test.apkg"
        col.export_anki_package(out_path=str(apkg), limit=DeckIdLimit(deck_id),
            options=ExportAnkiPackageOptions(with_media=True, with_scheduling=False, with_deck_configs=False, legacy=True))
        imported = Collection(str(root / "verification.anki2"))
        try:
            imported.import_anki_package(ImportAnkiPackageRequest(package_path=str(apkg)))
            assert set(imported.find_notes("")) == {n.id for n in notes}
            for note in notes:
                actual = imported.get_note(note.id)
                assert actual.fields == note.fields and actual.guid == note.guid
                assert len(actual.cards()) == len(note.cards())
            for filename in installed.values():
                assert (Path(imported.media.dir()) / filename).read_bytes() == (Path(col.media.dir()) / filename).read_bytes()
            if audio:
                assert (Path(imported.media.dir()) / audio.filename).read_bytes() == downloaded.data
                assert notes[-1].cards()[0].answer_av_tags()[0].filename == audio.filename
            exported_media = [p.name for p in Path(imported.media.dir()).iterdir() if p.is_file()]
        finally:
            imported.close()
        media = {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in Path(col.media.dir()).iterdir() if p.name in exported_media}
        report = {"source": str(source), "apkg": str(apkg), "deck": name, "deck_id": deck_id,
                  "notes": [{"id": n.id, "guid": n.guid, "fields": n.fields, "cards": [c.id for c in n.cards()]} for n in notes],
                  "media": media, "audio": audio.filename if audio else None,
                  "api_base": template.PRODUCTION, "online_parsing": False,
                  "native_reimport_verified": True,
                  "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "apkg_sha256": hashlib.sha256(apkg.read_bytes()).hexdigest()}
        save_json(root / "ankiweb-fixture.json", report)
        return report
    finally:
        col.close()


def refresh(root):
    import support
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions
    from shirabe_addon import assets, template

    report = json.loads((root / "ankiweb-fixture.json").read_text())
    if not report["deck"].startswith("Shirabe Test "):
        raise ValueError("Expected an isolated AnkiWeb test fixture.")
    apkg = root / "shirabe-ankiweb-test-updated.apkg"
    if apkg.exists():
        raise ValueError("The refreshed package already exists; retain its evidence.")
    col = Collection(str(root / "collection.anki2"))
    try:
        notes = [col.get_note(row["id"]) for row in report["notes"]]
        for note, row in zip(notes, report["notes"]):
            assert note.fields == row["fields"] and note.guid == row["guid"]
            assert note.note_type()["name"].startswith(report["deck"])
        installed = assets.install(col.media)
        for mid in {n.mid for n in notes if "ShirabeLookup" in n}:
            model = col.models.get(mid)
            for card in model["tmpls"]:
                card["afmt"] = template.with_block(card["afmt"], template.PRODUCTION,
                    template.CLIENT_ID, "click", assets=installed, snapshot_field=True)
            col.models.update_dict(model)
        col.export_anki_package(out_path=str(apkg), limit=DeckIdLimit(report["deck_id"]),
            options=ExportAnkiPackageOptions(with_media=True, with_scheduling=False, with_deck_configs=False, legacy=True))
        report["apkg"] = str(apkg)
        report["apkg_sha256"] = hashlib.sha256(apkg.read_bytes()).hexdigest()
        for name in installed.values():
            data = (Path(col.media.dir()) / name).read_bytes()
            report["media"][name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        report["native_reimport_verified"] = False
        save_json(root / "ankiweb-fixture-updated.json", report)
        return report
    finally:
        col.close()


if __name__ == "__main__":
    require_anki()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apkg", type=Path, nargs="?")
    parser.add_argument("--name")
    parser.add_argument("--refresh", type=Path, help="Refresh only the original test fixture's popup files and templates")
    parser.add_argument("--recording")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.refresh:
        report = refresh(args.refresh.resolve(strict=True))
    else:
        if not args.apkg or not args.name:
            parser.error("Provide an APKG and --name, or --refresh.")
        report = build(output_directory(args.output), args.apkg.resolve(strict=True), args.name, args.recording)
    print(report["apkg"])
