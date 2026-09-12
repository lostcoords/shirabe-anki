"""Round-trip native template removal and Undo through the Rails sync endpoints."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from unittest.mock import patch

import support
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit, source_rows


def run(config):
    from gui_safety import offscreen_test_application
    from aqt.qt import QDialogButtonBox, QPlainTextEdit
    from shirabe_addon.sync_conflict_ui import TemplateChecksDialog, TypeMappingDialog

    app = offscreen_test_application([])
    assert version == "26.08.1"
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck_id = collection.decks.id("Template lifecycle")
        model = collection.models.by_name("Basic (and reversed card)")
        note = collection.new_note(model)
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck_id)
        original = copy.deepcopy(collection.models.get(model["id"]))
        cards = note.cards()
        removed_id = cards[1].id
        cards[1].start_timer()
        collection.sched.answerCard(cards[1], 3)
        history = source_rows(collection, "revlog")
        client = SyncLogin(root / "sync.sqlite3", config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(root / "sync.sqlite3", client)
        source_entities = snapshot._source_entities

        def legacy_entities(*args):
            for kind, entries in source_entities(*args):
                if kind == "note_types":
                    entries = copy.deepcopy(list(entries))
                    for entry in entries:
                        for group in ("flds", "tmpls"):
                            for item in entry["definition"][group]:
                                item.pop("id", None)
                yield kind, entries

        with patch.object(snapshot, "_source_entities", legacy_entities):
            captured = snapshot.capture(collection, [deck_id])
        profile = snapshot.upload()
        changes = SyncChanges(root / "sync.sqlite3", client)
        delivery = SyncDelivery(root / "sync.sqlite3", client, profile_id=profile["id"],
                                key=captured["pair_request"]["key"])

        def sync():
            changes.capture(collection)
            changes.upload()
            try:
                changes.apply(collection)
            except SyncConflict as error:
                with SyncState(root / "sync.sqlite3") as state:
                    types = [state.batch(key)["payload"].get("note_types") for key in state.get("changes")["keys"]]
                raise AssertionError((error.conflicts, types)) from error
            delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()

        emit({"stage": "templates_uploaded", "card_id": removed_id, "definition": original})
        assert json.loads(sys.stdin.readline())["continue"]
        rows_before = source_rows(collection, "cards")
        changed = collection.models.get(model["id"])
        collection.models.rename_field(changed, changed["flds"][0], "Expression")
        collection.models.rename_field(changed, changed["flds"][1], "Meaning")
        changed["tmpls"][0]["name"] = "Recognition"
        changed["tmpls"][1]["name"] = "Recall"
        collection.models.save(changed)
        try:
            changes.capture(collection)
        except SyncConflict as error:
            problem = error.conflicts[0]
            assert problem["status"] == "mapping_required", problem
        else:
            raise AssertionError("Legacy rename was mapped without confirmation")
        prepared = changes.prepare_type_mapping(collection, problem)
        submitted = []
        chooser = TypeMappingDialog(None, prepared, submitted.append)
        chooser.show()
        app.processEvents()
        chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        assert not submitted
        chooser = TypeMappingDialog(None, prepared, submitted.append)
        for choices in chooser.choices.values():
            for ordinal, choice in enumerate(choices):
                choice.setCurrentIndex(choice.findData(ordinal))
        chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        assert submitted == [{"field_map": [0, 1], "template_map": [0, 1]}]
        changes.resolve_type_mapping(collection, prepared, submitted[0])
        changes = SyncChanges(root / "sync.sqlite3", client)
        sync()
        assert source_rows(collection, "cards") == rows_before
        assert source_rows(collection, "revlog") == history
        assert collection.get_note(note.id).fields == ["猫", "cat"]
        original = copy.deepcopy(collection.models.get(model["id"]))
        emit({"stage": "legacy_mapped", "definition": original})
        assert json.loads(sys.stdin.readline())["continue"]
        changed = collection.models.get(model["id"])
        changed["tmpls"][0]["afmt"] += "{{edit:Meaning}}"
        collection.models.save(changed)
        sync()
        reports = snapshot.template_diagnostics()
        assert reports[0]["issues"] == [{"template": 0, "side": "answer", "code": "unknown_filter", "detail": "edit"}], reports
        diagnostics = TemplateChecksDialog(None, reports)
        diagnostics.show()
        app.processEvents()
        assert "unknown_filter - edit" in diagnostics.findChild(QPlainTextEdit).toPlainText()
        diagnostics.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close).click()
        assert source_rows(collection, "cards") == rows_before
        assert source_rows(collection, "revlog") == history
        emit({"stage": "template_checked", "reports": reports})
        assert json.loads(sys.stdin.readline())["continue"]
        changed = collection.models.get(model["id"])
        changed["tmpls"][0]["afmt"] = changed["tmpls"][0]["afmt"].removesuffix("{{edit:Meaning}}")
        collection.models.save(changed)
        sync()
        assert snapshot.template_diagnostics()[0]["issues"] == []
        original = copy.deepcopy(collection.models.get(model["id"]))
        emit({"stage": "template_checks_cleared"})
        assert json.loads(sys.stdin.readline())["continue"]
        changed = collection.models.get(model["id"])
        collection.models.remove_template(changed, changed["tmpls"][1])
        collection.models.save(changed)
        assert collection.card_count() == 1
        sync()
        emit({"stage": "template_removed"})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        restored = collection.models.get(model["id"])["tmpls"]
        assert restored == original["tmpls"], (original["tmpls"], restored)
        assert collection.get_card(removed_id).id == removed_id
        assert source_rows(collection, "revlog") == history
        sync()
        assert collection.card_count() == 2
        assert source_rows(collection, "revlog") == history
        sync()
        client.sign_out()
        emit({"stage": "template_restored", "card_id": removed_id})
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
