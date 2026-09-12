"""Snapshot an authorized local profile for background study compatibility tests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3

from fixture_deck import output_directory, require_anki, save_json


def capture(source, root):
    from anki.buildinfo import version
    from anki.collection import Collection

    destination = root / "collection.anki2"
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as copied:
            original.backup(copied, pages=1000)
    col = Collection(str(destination))
    try:
        result = col.db.scalar("pragma integrity_check")
        if result != "ok":
            raise RuntimeError(f"Snapshot integrity check failed: {result}")
        types = []
        fixtures = []
        references = set()
        source_media = source.with_suffix(".media")
        for model in col.models.all():
            note_ids = col.db.list("select id from notes where mid = ? order by id limit 3", model["id"])
            if not note_ids:
                continue
            types.append({
                "definition": model,
                "notes": col.db.scalar("select count(*) from notes where mid = ?", model["id"]),
                "cards": col.db.scalar("select count(*) from cards join notes on notes.id = cards.nid where notes.mid = ?", model["id"]),
                "filters": sorted(set(re.findall(r"{{([^{}]+:[^{}]+)}}", " ".join(template[side] for template in model["tmpls"] for side in ("qfmt", "afmt"))))),
            })
            template_source = model["css"] + "\n".join(template[side] for template in model["tmpls"] for side in ("qfmt", "afmt"))
            references.update(col.media.files_in_str(model["id"], template_source))
            references.update(re.findall(r'''(?:src=["']|url\(["']?)([^"'\s)>]+)''', template_source))
            for note_id in note_ids:
                note = col.get_note(note_id)
                references.update(col.media.files_in_str(note.mid, "\x1f".join(note.fields)))
                cards = []
                for card in note.cards():
                    try:
                        cards.append({"id": card.id, "ord": card.ord, "question": card.question(), "answer": card.answer()})
                    except Exception as error:
                        cards.append({"id": card.id, "ord": card.ord, "error": str(error)})
                fixtures.append({"id": note.id, "guid": note.guid, "mid": note.mid, "fields": note.fields, "tags": note.tags, "cards": cards})
        media = {}
        for name in sorted(references):
            if Path(name).name != name or name in {".", ".."}:
                continue
            path = source_media / name
            if not path.is_file():
                media[name] = {"missing": True}
                continue
            copied = Path(col.media.dir()) / name
            shutil.copyfile(path, copied)
            with copied.open("rb") as stream:
                media[name] = {"bytes": copied.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
        report = {
            "anki": version, "collection": str(destination), "integrity": result,
            "notes": col.db.scalar("select count(*) from notes"),
            "cards": col.db.scalar("select count(*) from cards"),
            "reviews": col.db.scalar("select count(*) from revlog"),
            "note_types": types, "fixtures": fixtures, "sample_media": media,
            "scope": "Read-only SQLite backup; Anki opened only the copy. Up to three notes per note type rendered; only referenced sample media copied. No account credentials or network sync.",
        }
        save_json(root / "profile-report.json", report)
        return report
    finally:
        col.close()


if __name__ == "__main__":
    require_anki()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = output_directory(args.output)
    report = capture(args.collection.resolve(strict=True), root)
    print(json.dumps({"root": str(root), "notes": report["notes"], "cards": report["cards"], "reviews": report["reviews"], "note_types": len(report["note_types"]), "sampled_notes": len(report["fixtures"])}, indent=2))
