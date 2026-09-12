"""Probe delivered-review removal bookkeeping, not a user-facing remote Undo request."""

import json
from pathlib import Path
import sys

import support
from anki.buildinfo import version
from anki.collection import Collection
from shirabe_addon.sync_cards import apply_card_change
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import card_row, history_row
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit


def run(config):
    assert version == "26.08.1"
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck_id = collection.decks.id("Delivered review recovery")
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck_id)
        card_id = note.cards()[0].id
        sidecar = root / "sync.sqlite3"
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        captured = snapshot.capture(collection, [deck_id])
        profile = snapshot.upload()
        key = captured["pair_request"]["key"]
        changes = SyncChanges(sidecar, client)
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=key)
        emit({"stage": "awaiting_review", "card_id": card_id})
        assert json.loads(sys.stdin.readline())["continue"]
        page = delivery.receive()
        change = next(item for item in page["changes"] if item["entity_kind"] == "review")
        delivery.apply(collection)
        with SyncState(sidecar) as state:
            pending = state.batch(state.get("delivery")["return_key"])["payload"]
        returned = pending["history"][0]
        assert returned["delivery_cursor"] == change["cursor"]
        result = client.batch(profile["id"], {**pending, "request_key": "early-review-identity"})
        assert result["history"][0]["status"] == "accepted", result

        def sync():
            changes.capture(collection)
            changes.upload()
            try:
                changes.apply(collection)
            except SyncConflict as error:
                raise AssertionError(error.conflicts) from error

        sync()
        row = history_row(collection, returned["row"]["id"])
        source = change["payload"]["anki"]

        def reserve_fixture_removal(_exists):
            with SyncState(sidecar) as state:
                state.put("fixture_removal", {"row": row, "change": change})

        apply_card_change(collection, {"anki": {**source, "card_before": card_row(collection, card_id), "card_after": source["card_before"]}},
            removed_history=[{"row": row, "removal_id": 1}], before_history_removal=reserve_fixture_removal)
        assert history_row(collection, row["id"]) is None
        sync()
        send = client.batch
        returned_requests = []

        def lose_response(profile_id, payload):
            returned_requests.append(payload)
            result = send(profile_id, payload)
            assert result["history"][0].get("removed") is True, result
            if len(returned_requests) == 1:
                raise ConnectionError("Lost archived review receipt")
            return result

        client.batch = lose_response
        try:
            delivery.acknowledge()
        except ConnectionError:
            delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=key)
            delivery.acknowledge()
        else:
            raise AssertionError("Expected the lost receipt")
        client.batch = send
        assert returned_requests == [pending, pending]
        assert history_row(collection, row["id"]) is None
        emit({"stage": "removed_receipt_acknowledged", "review_id": row["id"], "native_reps": collection.get_card(card_id).reps})
        assert json.loads(sys.stdin.readline())["continue"]
        with SyncState(sidecar) as state:
            removal_id = int(state.checkpoint_digest("history_removal", row["id"]))
        apply_card_change(collection, {"anki": {**source, "card_before": card_row(collection, card_id)}},
            restored_history=[{"row": row, "removal_id": removal_id}])
        sync()
        for _ in range(10):
            page = delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            if not page["changes"]:
                break
        else:
            raise AssertionError("The restored review delivery did not drain")
        assert collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 1
        assert history_row(collection, row["id"]) == row
        emit({"stage": "delivered_review_restored", "review_id": row["id"], "native_reps": collection.get_card(card_id).reps})
        assert json.loads(sys.stdin.readline())["continue"]
        client.sign_out()
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
