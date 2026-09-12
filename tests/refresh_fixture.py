#!/usr/bin/env python3
"""Re-export an existing isolated fixture with current portable assets, retaining note IDs."""

import argparse
import json
from pathlib import Path
import re
import shutil

from fixture_deck import DECK, output_directory, require_anki, save_json


def main() -> None:
    require_anki()
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions
    import support
    from shirabe_addon import assets, offline, template
    from platform_check import check

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--web", type=Path, default=assets.WEB)
    args = parser.parse_args()
    original = args.fixture.resolve()
    manifest = json.loads((original / "manifest.json").read_text())
    if manifest["deck"]["name"] != DECK or Path(manifest["collection"]).resolve() != original / "source/collection.anki2":
        raise SystemExit("Expected a collection created by fixture_deck.py in its original fixture directory.")
    root = output_directory(None)
    shutil.copytree(original / "source", root / "source")
    manifest["collection"] = str(root / "source/collection.anki2")
    manifest["apkg"] = str(root / "shirabe-phase0.apkg")
    col = Collection(manifest["collection"])
    try:
        installed = assets.install(col.media, args.web)
        settings = manifest["portable_settings"]
        for model_id in {note["notetype"] for note in manifest["notes"]}:
            model = col.models.get(model_id)
            prepared = model["name"].endswith(" prepared")
            if prepared and not any(field["name"] == "ShirabeLookup" for field in model["flds"]):
                col.models.add_field(model, col.models.new_field("ShirabeLookup"))
            for card in model["tmpls"]:
                if prepared:
                    card["afmt"] = re.sub(r'<span hidden data-shirabe-snapshot data-phase0-media="[^"]+">[^<]+</span>', "", card["afmt"])
                    if "<div>{{Front}}</div>" not in card["qfmt"]:
                        card["qfmt"] = card["qfmt"].replace("{{Front}}", "<div>{{Front}}</div>")
                    if "<div>{{Back}}</div>" not in card["afmt"]:
                        card["afmt"] = card["afmt"].replace("{{Back}}", "<div>{{Back}}</div>")
                card["qfmt"] = template.with_question_guard(card["qfmt"])
                card["afmt"] = template.with_block(card["afmt"], settings["api_base"], template.CLIENT_ID, settings["trigger"], online_parsing=settings["online_parsing"], assets=installed, snapshot_field=prepared)
            col.models.update_dict(model)
        for row in manifest["notes"]:
            note = col.get_note(row["id"])
            if row["case"] == "prepared":
                note["ShirabeLookup"] = offline.snapshot_field(manifest["snapshot"])
                col.update_note(note)
            row["fields"] = note.fields[:]
        manifest["exported_count"] = col.export_anki_package(out_path=manifest["apkg"], options=ExportAnkiPackageOptions(with_media=True, legacy=True), limit=DeckIdLimit(manifest["deck"]["id"]))
        old_assets = set(manifest["portable_assets"].values())
        manifest["media"] = sorted((set(manifest["media"]) - old_assets) | set(installed.values()))
        manifest["portable_assets"] = installed
        rendered = root / "rendered"
        rendered.mkdir()
        for note in manifest["notes"]:
            for card in col.get_note(note["id"]).cards():
                for side, html in (("question", card.question()), ("answer", card.answer())):
                    (rendered / f"{note['case']}-{card.ord}-{side}.html").write_text(html)
        save_json(root / "manifest.json", manifest)
    finally:
        col.close()
    report = check(root, manifest)
    save_json(root / "platform-report.json", report)
    print(json.dumps({"root": str(root), "apkg": manifest["apkg"], "passed": report["passed"]}, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
