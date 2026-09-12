import json
from pathlib import Path
import sys

import support
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from sync_http_roundtrip import authorize, emit


def run(config):
    assert version == "26.08.1"
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        cloze = config["kind"] == "cloze"
        deck_id = collection.decks.id("Creation roundtrip")
        preset = collection.decks.config_dict_for_deck_id(deck_id)
        preset["new"]["bury"] = True
        collection.decks.update_config(preset)
        model = collection.models.by_name("Cloze" if cloze else "Basic (and reversed card)")
        note = collection.new_note(model)
        note.fields = ["{{c1::猫}}" if cloze else "猫", "cat"]
        collection.add_note(note, deck_id)
        sidecar = root / "sync.sqlite3"
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        captured = snapshot.capture(collection, [deck_id])
        profile = snapshot.upload()
        epoch = captured["pair_request"]["key"]
        emit({"stage": "creation_ready", "card_id": note.cards()[0].id})
        assert json.loads(sys.stdin.readline())["continue"]
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=epoch)
        page = delivery.receive()
        assert len(page["changes"]) == 1
        delivery.apply(collection)
        delivery.apply(collection)
        created_id = collection.db.scalar("SELECT id FROM notes WHERE id != ?", note.id)
        created = collection.get_note(created_id)
        assert sorted(card.ord for card in created.cards()) == ([0, 3] if cloze else [0, 1])
        emit({"stage": "native_created", "note_id": created.id, "card_ids": [card.id for card in created.cards()]})
        assert json.loads(sys.stdin.readline())["continue"]
        created.fields[1] = "later Anki edit"
        collection.update_note(created)
        send = client.batch

        def lose_response(profile_id, payload):
            assert len(payload["creations"]) == 1
            send(profile_id, payload)
            raise ConnectionError("lost committed creation response")

        client.batch = lose_response
        try:
            delivery.acknowledge()
        except ConnectionError:
            emit({"stage": "binding_response_lost"})
        else:
            raise AssertionError("Expected lost creation response")
        finally:
            client.batch = send
        assert json.loads(sys.stdin.readline())["continue"]
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=epoch)
        delivery.acknowledge()
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        changes = SyncChanges(sidecar, client)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        emit({"stage": "creation_synced", "fields": collection.get_note(created.id).fields,
              "cards": [{"id": card.id, "queue": card.queue, "reps": card.reps} for card in created.cards()],
              "reviews": collection.db.scalar("SELECT count(*) FROM revlog"),
              "notes": collection.db.scalar("SELECT count(*) FROM notes")})
        command = json.loads(sys.stdin.readline())
        assert command["continue"]
        for review_id in command["review_ids"]:
            client._profile_request(profile["id"], "/review_undos",
                                    {"review_id": review_id, "request_key": f"creation-undo-{review_id}"})
            delivery.receive()
            try:
                delivery.apply(collection)
            except DeliveryConflict as error:
                problem = next(item for item in error.conflicts if item["kind"] == "review_undos")
            else:
                raise AssertionError("Undo applied without confirmation")
            prepared = delivery.prepare_review_undo_resolution(collection, problem)
            assert "undo_error" not in prepared, prepared
            delivery.confirm_review_undo(collection, prepared)
            delivery.apply(collection)
            delivery.acknowledge()
            changes.capture(collection)
            changes.upload()
            changes.apply(collection)
        if command["review_ids"]:
            assert collection.db.scalar("SELECT count(*) FROM revlog") == 0
            cards = [{"id": card.id, "ord": card.ord, "reps": card.reps, "queue": card.queue} for card in created.cards()]
            assert all(card["reps"] == 0 and card["queue"] == 0 for card in cards), cards
            emit({"stage": "creation_reviews_undone"})
            assert json.loads(sys.stdin.readline())["continue"]
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
