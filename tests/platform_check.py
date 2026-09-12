#!/usr/bin/env python3
"""Run fixture import, media, rendering, search and undo in installed Anki."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import re
import sys

from fixture_deck import arguments, build_from_arguments, output_directory, require_anki, save_json


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(root: Path, manifest: dict) -> dict:
    from anki.buildinfo import version
    from anki.collection import Collection
    from anki.import_export_pb2 import ImportAnkiPackageRequest
    from anki.search_pb2 import SearchNode
    from aqt.addcards import AddCards
    from aqt.operations.note import update_note
    from aqt.qt import PYQT_VERSION_STR, QT_VERSION_STR
    from google.protobuf.json_format import MessageToDict

    imported = root / "roundtrip"
    imported.mkdir()
    col = Collection(str(imported / "collection.anki2"))
    report = {"anki": version, "qt": QT_VERSION_STR, "pyqt": PYQT_VERSION_STR, "python": sys.version, "collection_class": inspect.getfile(Collection), "scope": "Real Anki backend, no GUI or Android validation", "checks": {}}
    checks = report["checks"]
    try:
        log = col.import_anki_package(ImportAnkiPackageRequest(package_path=manifest["apkg"]))
        report["import"] = MessageToDict(log)
        expected_notes = manifest["notes"]
        checks["apkg_note_count"] = len(col.find_notes("tag:shirabe_phase0")) == len(expected_notes)
        checks["apkg_card_count"] = len(col.find_cards("tag:shirabe_phase0")) == sum(len(n["cards"]) for n in expected_notes)
        for note in expected_notes:
            actual = col.get_note(note["id"])
            checks[f"{note['case']}_fields_roundtrip"] = actual.fields == note["fields"]
            checks[f"{note['case']}_guid_roundtrip"] = actual.guid == note["guid"]
        source_media = Path(manifest["collection"]).with_suffix(".media")
        target_media = Path(col.media.dir())
        report["media"] = {}
        for name in manifest["media"]:
            target = target_media / name
            same = target.is_file() and digest(target) == digest(source_media / name)
            checks[f"media_{name}"] = same
            report["media"][name] = {"roundtrip_identical": same, "bytes": target.stat().st_size if target.exists() else None}

        by_case = {n["case"]: n for n in expected_notes}
        if manifest.get("snapshot"):
            import support
            from shirabe_addon.offline import snapshot_field
            prepared = col.get_note(by_case["prepared"]["id"])
            checks["snapshot_uses_canonical_field"] = prepared["ShirabeLookup"] == snapshot_field(manifest["snapshot"])
            checks["snapshot_has_no_static_fixture_reference"] = all("data-phase0-media" not in side for template in prepared.note_type()["tmpls"] for side in (template["qfmt"], template["afmt"]))
        clozes = col.get_note(by_case["cloze"]["id"]).cards()
        checks["cloze_hides_active_deletion_text"] = all(("寿司" if card.ord == 0 else "食べました") not in re.sub(r"<[^>]*>", "", card.question()) for card in clozes)
        report["cloze_hidden_answer_in_attribute"] = [re.findall(r'data-cloze="([^"]*)"', card.question()) for card in clozes]
        checks["cloze_other_deletion_visible"] = all(("食べました" if card.ord == 0 else "寿司") in card.question() for card in clozes)
        ruby = col.get_note(by_case["ruby-inline"]["id"]).cards()[0].question()
        checks["ruby_markup_preserved"] = "<rt>にほんご</rt>" in ruby and "勉<span>強</span>" in ruby
        checks["hidden_inline_still_in_rendered_html"] = "HIDDEN_INLINE_7F3A" in ruby
        report["search"] = {}
        for term in ("猫", "日本語", "食べる", "phase0::reverse", "a*b", "a:b", 'a"b'):
            try:
                query = col.build_search_string(term)
                report["search"][term] = {"query": query, "note_ids": list(col.find_notes(query))}
            except Exception as error:
                report["search"][term] = {"error_type": type(error).__name__, "error": str(error)}
            literal_query = col.build_search_string(SearchNode(literal_text=term))
            report["search"][term]["literal_query"] = literal_query
            report["search"][term]["literal_note_ids"] = list(col.find_notes(literal_query))

        reverse_id = by_case["reverse"]["id"]
        before = col.get_note(reverse_id)
        old_fields, old_tags, old_guid = before.fields[:], before.tags[:], before.guid
        before_cards = col.db.all("select * from cards where nid = ? order by id", reverse_id)
        before_revlog = col.db.all("select * from revlog order by id")
        entry = col.add_custom_undo_entry("Phase 0 enrich note")
        before["Back"] += "<br>fixture enrichment"
        update = col.update_note(before)
        merged = col.merge_undo_entries(entry)
        report["update_note"] = {"return_type": type(update).__name__, "changes": MessageToDict(update), "merged": MessageToDict(merged), "undo_label": col.undo_status().undo, "note_mod": col.get_note(reverse_id).mod}
        checks["update_persists_fields"] = col.get_note(reverse_id)["Back"].endswith("fixture enrichment")
        checks["update_preserves_tags_guid"] = col.get_note(reverse_id).tags == old_tags and col.get_note(reverse_id).guid == old_guid
        checks["update_preserves_entire_sibling_card_rows"] = col.db.all("select * from cards where nid = ? order by id", reverse_id) == before_cards
        checks["update_preserves_revlog"] = col.db.all("select * from revlog order by id") == before_revlog
        col.undo()
        checks["undo_restores_fields"] = col.get_note(reverse_id).fields == old_fields
        checks["undo_preserves_siblings"] = col.db.all("select * from cards where nid = ? order by id", reverse_id) == before_cards

        stale = col.get_note(reverse_id)
        current = col.get_note(reverse_id)
        stale_entry = col.add_custom_undo_entry("Phase 0 stale write probe")
        current["Back"] = "newer field edit"
        col.update_note(current)
        stale["Front"] = "stale draft front"
        col.update_note(stale)
        report["stale_note_update"] = {"back_after_stale_save": col.get_note(reverse_id)["Back"], "newer_edit_was_overwritten": col.get_note(reverse_id)["Back"] == old_fields[1], "conclusion": "update_note has no automatic compare-and-swap; reread and compare all proposed fields before saving"}
        col.merge_undo_entries(stale_entry)
        col.undo()
        checks["stale_probe_restored"] = col.get_note(reverse_id).fields == old_fields
        report["media_filename_behavior"] = [col.media.write_data("_phase0_collision.json", data) for data in (b"one", b"one", b"two")]
        report["native_editor_interface"] = {"set_note_signature": str(inspect.signature(AddCards.set_note)), "set_note_source": inspect.getsource(AddCards.set_note), "update_note_operation_source": inspect.getsource(update_note), "validation": "Inspected installed implementation, did not open editor"}
        report["passed"] = all(checks.values())
        return report
    finally:
        col.close()


if __name__ == "__main__":
    require_anki()
    args = arguments()
    root = output_directory(args.output)
    manifest = build_from_arguments(root, args)
    report = check(root, manifest)
    from study_corpus import build as build_study_corpus
    report["study"] = build_study_corpus(root / "study", manifest, args.study_sizes)
    report["checks"]["study_corpus"] = report["study"]["passed"]
    report["checks"]["study_shirabe_import"] = report["study"]["shirabe_import"]["passed"]
    report["passed"] = all(report["checks"].values())
    save_json(root / "platform-report.json", report)
    print(json.dumps({"root": str(root), "apkg": manifest["apkg"], "report": str(root / "platform-report.json"), "anki": report["anki"], "passed": report["passed"], "failed": [name for name, passed in report["checks"].items() if not passed]}, indent=2))
    sys.exit(0 if report["passed"] else 1)
