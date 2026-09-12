from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import support
from anki.collection import Collection
from anki.scheduler.v3 import CardAnswer
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit, source_rows


def drain(delivery, collection):
    for _ in range(10):
        page = delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        if not page["changes"]:
            return
    raise AssertionError("Delivery did not drain")


def run(config):
    root = Path(config["directory"])
    collection_path = root / "competing.anki2"
    if collection_path.exists():
        raise RuntimeError("This probe requires a new disposable collection")
    collection = Collection(str(collection_path))
    collection.set_config("fsrs", True)
    path = root / "sync.sqlite3"
    client = SyncLogin(path, config["origin"])
    try:
        deck = collection.decks.id("Competing reviews")
        card_ids = []
        for front in ("Anki later", "Shirabe later", "Equal time Anki wins", "Equal time Shirabe wins"):
            note = collection.new_note(collection.models.by_name("Basic"))
            note.fields = [front, "猫"]
            collection.add_note(note, deck)
            card_ids.append(note.cards()[0].id)
        authorize(client)
        snapshot = SyncSnapshot(path, client)
        captured = snapshot.capture(collection, [deck])
        profile = snapshot.upload()
        answered_at = int(time.time()) * 1000 + 999
        for card_id in card_ids:
            card = collection.get_card(card_id)
            card.start_timer()
            states = collection._backend.get_scheduling_states(card.id)
            answer = collection.sched.build_answer(card=card, states=states, rating=CardAnswer.EASY)
            answer.answered_at_millis = answered_at
            collection.sched.answer_card(answer)
        history = source_rows(collection, "revlog")
        assert [row["id"] for row in history] == [answered_at + index for index in range(len(card_ids))]
        assert all(json.loads(row["data"])["lrt"] == answered_at // 1000 for row in source_rows(collection, "cards"))
        emit({"stage": "offline_reviews", "cards": card_ids, "history": history})
        assert json.loads(sys.stdin.readline())["continue"]
        changes = SyncChanges(path, client)
        delivery = SyncDelivery(path, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        assert len(source_rows(collection, "revlog")) == 8
        with SyncState(path) as state:
            assert state.get("delivery") is None and state.get("changes") is None
        emit({"stage": "reviews_reconciled", "cards": source_rows(collection, "cards"), "history": source_rows(collection, "revlog")})
        assert json.loads(sys.stdin.readline())["continue"]
        history = source_rows(collection, "revlog")
        drain(delivery, collection)
        assert all(row["type"] == row["queue"] == row["reps"] == 0 for row in source_rows(collection, "cards"))
        for card_id in card_ids:
            card = collection.get_card(card_id)
            card.flags = 1
            collection.update_card(card)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        assert history == source_rows(collection, "revlog")
        emit({"stage": "reset_reconciled", "cards": source_rows(collection, "cards")})
        assert json.loads(sys.stdin.readline())["continue"]
        deleted_card = collection.get_card(card_ids[0])
        collection.remove_notes([deleted_card.nid])
        assert collection.undo_status().undo, "Anki deletion did not create an undo entry"
        changes.capture(collection)
        assert collection.undo_status().undo, "Capturing a deletion cleared Anki undo"
        send = client.batch

        def lose_deletion_response(profile_id, payload):
            send(profile_id, payload)
            raise ConnectionError("Deletion receipt lost after server commit")

        client.batch = lose_deletion_response
        try:
            changes.upload()
        except ConnectionError:
            pass
        else:
            raise AssertionError("Deletion did not reach the server")
        finally:
            client.batch = send
        emit({"stage": "deletion_committed", "card_id": deleted_card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        changes.upload()
        changes.apply(collection)
        assert collection.undo_status().undo, "Applying deletion receipts cleared Anki undo"
        drain(delivery, collection)
        assert collection.undo_status().undo, "Acknowledging deletion cleared Anki undo"
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        emit({"stage": "deletion_reconciled", "card_id": deleted_card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        assert collection.get_card(deleted_card.id).nid == deleted_card.nid
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        assert history == source_rows(collection, "revlog")
        emit({"stage": "deletion_undone", "card_id": deleted_card.id, "cards": source_rows(collection, "cards")})
        assert json.loads(sys.stdin.readline())["continue"]
        note = collection.get_note(deleted_card.nid)
        note.fields[1] = "Edited while hidden"
        collection.update_note(note)
        card = collection.get_card(deleted_card.id)
        card.flags = 3
        collection.update_card(card)
        card.start_timer()
        collection.sched.answerCard(card, 3)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        changes.capture(collection)
        with SyncState(path) as state:
            assert not state.get("changes")["keys"], "Hidden cards were recaptured unchanged"
        changes.upload()
        changes.apply(collection)
        emit({"stage": "hidden_synced", "card_id": card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        hidden_history = source_rows(collection, "revlog")
        collection.remove_notes([note.id])
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        collection.undo()
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        assert collection.get_note(note.id).fields[1] == "Edited while hidden"
        assert hidden_history == source_rows(collection, "revlog")
        note = collection.get_note(card.nid)
        emit({"stage": "hidden_deletion_undone", "card_id": card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        drain(delivery, collection)
        assert not collection.db.scalar("SELECT count(*) FROM notes WHERE id = ?", note.id)
        assert len(source_rows(collection, "cards")) == 3
        assert hidden_history == source_rows(collection, "revlog")
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        emit({"stage": "requested_note_deleted", "card_id": card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        assert hidden_history == source_rows(collection, "revlog")
        assert len(source_rows(collection, "cards")) == 4
        emit({"stage": "requested_deletion_undone", "card_id": card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.remove_notes([note.id])
        delivery.receive()
        try:
            delivery.apply(collection)
        except DeliveryConflict as error:
            problem = error.conflicts[0]
        else:
            raise AssertionError("An already absent note was deleted without confirmation")
        prepared = delivery.prepare_note_deletion_resolution(collection, problem)
        assert prepared["local"] is None
        delivery.confirm_note_absence(collection, prepared)
        collection.undo()
        assert collection.get_note(note.id).fields[1] == "Edited while hidden"
        delivery.apply(collection)
        original_batch = client.batch

        def lose_absence_response(profile_id, payload):
            original_batch(profile_id, payload)
            raise ConnectionError("Lost source absence response")

        client.batch = lose_absence_response
        try:
            delivery.acknowledge()
        except ConnectionError:
            pass
        else:
            raise AssertionError("Expected a lost source absence response")
        client.batch = original_batch
        emit({"stage": "absence_reconciled_before_ack", "card_id": card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        delivery = SyncDelivery(path, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        delivery.apply(collection)
        delivery.acknowledge()
        drain(delivery, collection)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        assert hidden_history == source_rows(collection, "revlog")
        assert len(source_rows(collection, "cards")) == 4
        emit({"stage": "confirmed_absence_undone", "card_id": card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        delivery.receive()
        collection.remove_notes([note.id])
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        emit({"stage": "source_absence_accepted", "card_id": card.id})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        drain(delivery, collection)
        assert collection.get_note(note.id).fields[1] == "Edited while hidden"
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        drain(delivery, collection)
        assert hidden_history == source_rows(collection, "revlog")
        assert len(source_rows(collection, "cards")) == 4
        emit({"stage": "accepted_absence_undone", "card_id": card.id})
    finally:
        collection.close()


if __name__ == "__main__":
    try:
        run(json.loads(sys.stdin.readline()))
    except SyncConflict as error:
        raise AssertionError(json.dumps(error.conflicts)) from error
