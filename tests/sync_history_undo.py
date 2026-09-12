"""Round-trip native review Undo through the Rails sync endpoints."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import support
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from sync_http_roundtrip import authorize, emit
from gui_safety import offscreen_test_application


def run(config):
    assert version == "26.08.1"
    application = offscreen_test_application([])
    from aqt.qt import QDialogButtonBox
    from shirabe_addon.sync_conflict_ui import HistoryRemovalDialog
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck_id = collection.decks.id("Native review Undo")
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck_id)
        card = note.cards()[0]
        client = SyncLogin(root / "sync.sqlite3", config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(root / "sync.sqlite3", client)
        captured = snapshot.capture(collection, [deck_id])
        profile = snapshot.upload()
        changes = SyncChanges(root / "sync.sqlite3", client)

        def sync():
            changes.capture(collection)
            changes.upload()
            changes.apply(collection)

        card.start_timer()
        collection.sched.answerCard(card, 3)
        review_id = collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", card.id)
        sync()
        emit({"stage": "review_synced", "card_id": card.id, "review_id": review_id})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        assert collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id) == 0
        assert collection.get_card(card.id).reps == 0
        changes.capture(collection)
        send = client.batch
        sent = []

        def lose_response(profile_id, payload):
            sent.append(payload)
            result = send(profile_id, payload)
            if len(sent) == 1:
                raise ConnectionError("Lost accepted native history change response")
            return result

        client.batch = lose_response
        try:
            changes.upload()
        except ConnectionError:
            changes = SyncChanges(root / "sync.sqlite3", client)
            changes.upload()
        else:
            raise AssertionError("Expected a lost Undo response")
        assert len(sent) == 2 and sent[0] == sent[1]
        collection.redo()
        changes.apply(collection)
        assert collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", card.id) == review_id
        assert collection.get_card(card.id).reps == 1
        client.batch = send
        emit({"stage": "undo_synced", "native_reps": collection.get_card(card.id).reps})
        assert json.loads(sys.stdin.readline())["continue"]
        assert collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", card.id) == review_id
        changes.capture(collection)
        sent.clear()
        client.batch = lose_response
        try:
            changes.upload()
        except ConnectionError:
            changes = SyncChanges(root / "sync.sqlite3", client)
            changes.upload()
        else:
            raise AssertionError("Expected a lost Redo response")
        assert len(sent) == 2 and sent[0] == sent[1]
        collection.undo()
        changes.apply(collection)
        assert collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id) == 0
        assert collection.get_card(card.id).reps == 0
        client.batch = send
        emit({"stage": "redo_synced", "review_id": review_id, "native_reps": collection.get_card(card.id).reps})
        assert json.loads(sys.stdin.readline())["continue"]
        sync()
        emit({"stage": "second_undo_synced"})
        assert json.loads(sys.stdin.readline())["continue"]
        card = collection.get_card(card.id)
        card.start_timer()
        collection.sched.answerCard(card, 3)
        sync()
        emit({"stage": "concurrent_review_synced"})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        changes.capture(collection)
        changes.upload()
        try:
            changes.apply(collection)
        except SyncConflict as error:
            assert len(error.conflicts) == 1 and error.conflicts[0]["code"] == "history_changed_before_undo", error.conflicts
            problem = error.conflicts[0]
        else:
            raise AssertionError("Expected the concurrent review to prevent automatic Undo")
        assert collection.get_card(card.id).reps == 0
        emit({"stage": "undo_conflicted"})
        assert json.loads(sys.stdin.readline())["continue"]
        detail = changes.card_conflict(problem)
        prepared = changes.prepare_history_removal(collection, problem, detail)
        replacements = []
        dialog = HistoryRemovalDialog(None, prepared, lambda schedule: replacements.append(
            changes.resolve_history_removal(collection, prepared, schedule=schedule)))
        dialog.show()
        application.processEvents()
        dialog.schedule.setCurrentIndex(dialog.schedule.findData(config["schedule"]))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        assert len(replacements) == 1
        sent.clear()
        client.batch = lose_response
        try:
            changes.upload()
        except ConnectionError:
            changes = SyncChanges(root / "sync.sqlite3", client)
            changes.upload()
        else:
            raise AssertionError("Expected a lost Undo choice response")
        assert len(sent) == 2 and sent[0] == sent[1]
        changes.apply(collection)
        client.batch = send
        selected = collection.get_card(card.id).reps
        assert selected == (0 if config["schedule"] == "anki" else detail["anki"]["card_after"]["reps"])
        delivery = SyncDelivery(root / "sync.sqlite3", client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        for _ in range(10):
            page = delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            if not page["changes"]:
                break
        else:
            raise AssertionError("History delivery did not drain after the Undo choice")
        assert collection.get_card(card.id).reps == selected
        assert collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id) == 1
        sync()
        emit({"stage": "undo_choice_synced", "native_reps": selected})
        assert json.loads(sys.stdin.readline())["continue"]
        dialog.deleteLater()
        client.sign_out()
        emit({"stage": "undo_replayed"})
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
