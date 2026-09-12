"""Exercise native sync against the Rails test server using a new Anki collection."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import support
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_notes import apply_note_change, NoteConflict
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState


def emit(payload):
    print(json.dumps(payload), flush=True)


def authorize(client):
    authorization = client.start_login()
    time.sleep(max(0, authorization["next_poll_at"] - time.time()))
    assert not client.poll_login()
    emit({"stage": "authorize", "user_code": authorization["user_code"]})
    assert json.loads(sys.stdin.readline())["continue"]
    deadline = time.monotonic() + 30
    while not client.poll_login():
        if time.monotonic() >= deadline:
            raise AssertionError("Native device approval did not complete")
        time.sleep(0.05)


def source_rows(collection, table):
    if table not in ("notes", "cards", "revlog"):
        raise ValueError("Invalid source table")
    columns = [row[1] for row in collection.db.all("SELECT * FROM pragma_table_info(?) ORDER BY cid", table)]
    return [dict(zip(columns, row)) for row in collection.db.all(f"select * from {table} order by id")]


def run(config):
    if version != "26.08.1":
        raise RuntimeError(f"Expected Anki 26.08.1, found {version}")
    root = Path(config["directory"])
    path = root / "collection.anki2"
    if path.exists():
        raise RuntimeError("This probe only creates a new disposable collection")
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    state = SyncState(root / "sync.sqlite3")
    try:
        deck_id = collection.decks.id("Japanese sync roundtrip")
        model = collection.models.by_name("Basic")
        note = collection.new_note(model)
        note.fields = ["猫", "cat"]
        note.tags = ["Japanese"]
        collection.add_note(note, deck_id)
        card = note.cards()[0]
        card.start_timer()
        collection.sched.answerCard(card, 3)
        client = SyncLogin(root / "sync.sqlite3", config["origin"])
        authorize(client)
        credentials = state.get("native_login")
        previous_refresh = credentials["refresh_token"]
        credentials["expires_at"] = 0
        state.put("native_login", credentials)
        snapshot = SyncSnapshot(root / "sync.sqlite3", client)
        captured = snapshot.capture(collection, [deck_id])
        epoch = captured["pair_request"]["key"]
        cards = source_rows(collection, "cards")
        history = source_rows(collection, "revlog")
        received = []
        send_batch = client.batch

        def lose_response(profile_id, payload):
            received.append(send_batch(profile_id, payload))
            raise ConnectionError("Discard the first committed batch response")

        client.batch = lose_response
        try:
            snapshot.upload()
        except ConnectionError:
            assert len(received) == 1
        else:
            raise AssertionError("The batch response was not interrupted")
        finally:
            client.batch = send_batch
        state.close()
        state = SyncState(root / "sync.sqlite3")
        snapshot = SyncSnapshot(root / "sync.sqlite3", client)
        profile = snapshot.upload()
        assert received[0] == state.batch(state.snapshot_batch_keys()[0])["receipt"]
        assert all(state.batch_applied(key) for key in state.snapshot_batch_keys())
        profile_id = profile["id"]
        sync = SyncDelivery(root / "sync.sqlite3", client, profile_id=profile_id, key=epoch)
        emit({"stage": "uploaded", "note_id": note.id, "card_id": card.id, "history_count": len(history),
              "refreshed": state.get("native_login")["refresh_token"] != previous_refresh})
        command = json.loads(sys.stdin.readline())
        assert command["continue"]
        delivery = sync.receive()
        assert [change["entity_kind"] for change in delivery["changes"]] == ["note", "card"], delivery
        sync.apply(collection)
        sync.apply(collection)
        assert collection.get_note(note.id).fields == ["猫", "feline from Shirabe"]
        assert collection.get_card(card.id).queue == -1
        assert source_rows(collection, "revlog") == history
        sync.acknowledge()
        edited = collection.get_note(note.id)
        edited.fields[0] = "ねこ from Anki"
        collection.update_note(edited)
        changes = SyncChanges(root / "sync.sqlite3", client)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        sync.receive()
        sync.apply(collection)
        sync.acknowledge()

        edited_card = collection.get_card(card.id)
        edited_card.flags = 3
        collection.update_card(edited_card)
        for suspended in (True, False):
            if not suspended:
                edited_card = collection.get_card(card.id)
                edited_card.queue = cards[0]["queue"]
                collection.update_card(edited_card)
            changes.capture(collection)
            changes.upload()
            changes.apply(collection)
            assert (collection.get_card(card.id).queue == -1) == suspended
            assert collection.get_card(card.id).flags == 3
            card_delivery = sync.receive()
            assert any(change["entity_kind"] == "card" for change in card_delivery["changes"])
            sync.apply(collection)
            sync.acknowledge()

        model = collection.get_note(note.id).note_type()
        model["flds"][0]["name"] = "Expression"
        model["tmpls"][0]["qfmt"] = "{{Expression}}"
        model["tmpls"][0]["afmt"] = "{{FrontSide}}<hr>{{Back}}"
        collection.models.save(model)
        added = collection.new_note(model)
        added.fields = ["犬", "dog"]
        collection.add_note(added, deck_id)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        sync.receive()
        sync.apply(collection)
        sync.acknowledge()

        native_card = collection.get_card(card.id)
        native_card.start_timer()
        collection.sched.answerCard(native_card, 3)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        sync.receive()
        sync.apply(collection)
        sync.acknowledge()
        cards = source_rows(collection, "cards")

        conflict_note = collection.get_note(note.id)
        conflict_note.fields[0] = "independent local edit"
        collection.update_note(conflict_note)
        conflicting = next(change["payload"] for change in delivery["changes"] if change["entity_kind"] == "note")
        conflicting["note"]["field_names"] = [field["name"] for field in conflict_note.note_type()["flds"]]
        conflicting["note"]["fields"][0] = "competing remote edit"
        try:
            apply_note_change(collection, conflicting)
        except NoteConflict as error:
            assert "field changed independently" in str(error)
            assert collection.get_note(note.id).fields[0] == "independent local edit"
        else:
            raise AssertionError("A local field conflict was overwritten")
        emit({"stage": "field_roundtrip", "source_notes": collection.note_count(), "source_cards": collection.card_count(),
              "pending_batches": len(state.pending()), "review_count": len(source_rows(collection, "revlog")),
              "card_flags": collection.get_card(card.id).flags, "card_queue": collection.get_card(card.id).queue})
        assert json.loads(sys.stdin.readline())["continue"]
        reviews = sync.receive()
        review_changes = [change for change in reviews["changes"] if change["entity_kind"] == "review"]
        for index, review in enumerate(review_changes, 1):
            assert review["payload"]["anki_card_id"] == card.id
            assert review["payload"]["anki"]["card_after"]["reps"] == cards[0]["reps"] + index
        sync.apply(collection)
        state.close()
        state = SyncState(root / "sync.sqlite3")
        sync = SyncDelivery(root / "sync.sqlite3", client, profile_id=profile_id, key=epoch)
        sync.apply(collection)
        return_key = state.get("delivery")["return_key"]
        batch = state.batch(return_key)["payload"]
        receipt = client.batch(profile_id, batch)
        assert all(row["status"] == "accepted" for row in receipt["history"]), receipt
        sync.acknowledge()
        assert receipt == state.batch(return_key)["receipt"]
        assert state.get("delivery_cursor") == reviews["cursor"]
        assert state.get("delivery") is None
        native_card = collection.get_card(card.id)
        native_card.start_timer()
        collection.sched.answerCard(native_card, 3)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        sync.receive()
        sync.apply(collection)
        sync.acknowledge()
        changes.capture(collection)
        with SyncState(root / "sync.sqlite3") as quiet_state:
            assert not quiet_state.get("changes")["keys"], "An unchanged collection queued another upload"
        changes.upload()
        changes.apply(collection)
        client.sign_out()
        assert state.get("native_login") is None
        emit({"stage": "review_delivered", "sync_id": review["payload"]["sync_id"], "rating": review["payload"]["rating"],
              "review_count": len(source_rows(collection, "revlog")), "reps": collection.get_card(card.id).reps,
              "returned_sync_id": receipt["history"][-1]["sync_id"]})
    finally:
        state.close()
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
