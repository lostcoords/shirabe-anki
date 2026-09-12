#!/usr/bin/env python3
"""Compare real M4A and MP3 playback in an existing isolated AnkiWeb test fixture."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from fixture_deck import output_directory, require_anki, save_json


def build(manifest_path, root):
    import support
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest
    from shirabe_addon.authoring_media import DownloadedMedia, store_selected_media

    manifest = json.loads(manifest_path.read_text())
    if not manifest["deck"].startswith("Shirabe Test "):
        raise ValueError("Only a named Shirabe Test fixture can be changed.")
    filename = manifest["audio"]
    if not re.fullmatch(r"shirabe_[a-f0-9]{64}\.m4a", filename):
        raise ValueError("Expected the verified fixture recording.")
    source = Path(manifest["apkg"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest["apkg_sha256"]:
        raise ValueError("The fixture package changed.")
    col = Collection(str(root / "collection.anki2"))
    try:
        col.import_anki_package(ImportAnkiPackageRequest(package_path=str(source)))
        notes = [col.get_note(row["id"]) for row in manifest["notes"]]
        if set(col.find_notes("")) != {n.id for n in notes}:
            raise ValueError("The package contains notes outside the fixture.")
        for note, row in zip(notes, manifest["notes"]):
            if note.guid != row["guid"] or note.fields != row["fields"]:
                raise ValueError("The fixture note changed.")
        audio_notes = [n for n in notes if "Back" in n and f"[sound:{filename}]" in n["Back"]]
        if len(audio_notes) != 1:
            raise ValueError("Expected exactly one dedicated audio test note.")
        note = audio_notes[0]
        original = Path(col.media.dir()) / filename
        if hashlib.sha256(original.read_bytes()).hexdigest() != manifest["media"][filename]["sha256"]:
            raise ValueError("The original recording changed.")
        mp3 = root / "comparison.mp3"
        subprocess.run(["ffmpeg", "-n", "-loglevel", "error", "-i", str(original),
                        "-map_metadata", "-1", "-codec:a", "libmp3lame", "-b:a", "96k",
                        "-ac", "1", "-ar", "48000", str(mp3)], check=True, timeout=30)
        media = store_selected_media(col, DownloadedMedia(mp3.read_bytes(), "mp3", "Shirabe",
                                     "Permitted for Anki deck synchronization and export"))
        note["Back"] = (f"M4A sound tag<br>[sound:{filename}]<br>"
                        f"MP3 sound tag<br>{media.field_html}<br>"
                        f'M4A HTML control<br><audio controls preload="none" src="{filename}"></audio><br>'
                        "Shirabe<br>Permitted for Anki deck synchronization and export")
        col.update_note(note)
        deck_id = col.decks.by_name(manifest["deck"])["id"]
        apkg = root / "shirabe-ankiweb-audio-comparison.apkg"
        col.export_anki_package(out_path=str(apkg), limit=DeckIdLimit(deck_id),
            options=ExportAnkiPackageOptions(with_media=True, with_scheduling=False, with_deck_configs=False, legacy=True))
        check = Collection(str(root / "verification.anki2"))
        try:
            check.import_anki_package(ImportAnkiPackageRequest(package_path=str(apkg)))
            assert set(check.find_notes("")) == {n.id for n in notes}
            for expected in notes:
                actual = check.get_note(expected.id)
                assert actual.fields == expected.fields and actual.guid == expected.guid
                assert len(actual.cards()) == len(expected.cards())
            tags = check.get_note(note.id).cards()[0].answer_av_tags()
            assert {tag.filename for tag in tags} == {filename, media.filename}
            assert (Path(check.media.dir()) / media.filename).read_bytes() == mp3.read_bytes()
            files = [p for p in Path(check.media.dir()).iterdir() if p.is_file()]
            manifest["media"] = {p.name: {"bytes": p.stat().st_size,
                                 "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
        finally:
            check.close()
        manifest.update(apkg=str(apkg), apkg_sha256=hashlib.sha256(apkg.read_bytes()).hexdigest(),
                        native_reimport_verified=True, audio_note_id=note.id, mp3=media.filename,
                        notes=[{"id": n.id, "guid": n.guid, "fields": n.fields,
                                "cards": [c.id for c in n.cards()]} for n in notes])
        save_json(root / "ankiweb-audio-comparison.json", manifest)
        return apkg
    finally:
        col.close()


if __name__ == "__main__":
    require_anki()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(build(args.manifest.resolve(strict=True), output_directory(args.output)))
