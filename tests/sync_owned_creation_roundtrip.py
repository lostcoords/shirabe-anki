import json
from pathlib import Path
import sys

import support
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_collection import SyncCollection
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from sync_http_roundtrip import authorize, emit


def prepare_with_lost_response(sidecar, client):
    prepare = client.prepare_publications
    requests = []

    def lose_preparation(profile_id, payload):
        requests.append(dict(payload))
        response = prepare(profile_id, payload)
        if len(requests) == 1:
            raise ConnectionError("lost publication preparation response")
        return response

    client.prepare_publications = lose_preparation
    try:
        try:
            SyncCollection(sidecar, client).prepare_publications()
        except ConnectionError:
            pass
        else:
            raise AssertionError("Expected lost publication preparation response")
        SyncCollection(sidecar, client).prepare_publications()
        assert requests[0] == requests[1]
    finally:
        client.prepare_publications = prepare


def run(config):
    assert version == "26.08.1"
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck_id = collection.decks.id("Owned creation roundtrip")
        source = collection.new_note(collection.models.by_name("Basic"))
        source.fields = ["source", "source"]
        collection.add_note(source, deck_id)
        foreign = collection.models.new("Shirabe " + config["kind"])
        collection.models.add_field(foreign, collection.models.new_field("Foreign"))
        template = collection.models.new_template("Foreign")
        template["qfmt"] = "{{Foreign}}"
        template["afmt"] = "{{FrontSide}}"
        collection.models.add_template(foreign, template)
        collection.models.add_dict(foreign)
        sidecar = root / "sync.sqlite3"
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        captured = snapshot.capture(collection, [deck_id])
        profile = snapshot.upload()
        epoch = captured["pair_request"]["key"]
        emit({"stage": "owned_ready", "card_id": source.cards()[0].id})
        assert json.loads(sys.stdin.readline())["continue"]
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=epoch)
        page = delivery.receive()
        assert len(page["changes"]) == 1
        delivery.apply(collection)
        delivery.apply(collection)
        created_id = collection.db.scalar("SELECT id FROM notes WHERE id != ?", source.id)
        created = collection.get_note(created_id)
        assert len(created.cards()) == 1
        assert created.note_type()["shirabe"]["kind"] == config["kind"]
        assert created.note_type()["name"].startswith(f"Shirabe {config['kind']}-")
        emit({"stage": "owned_created", "note_id": created.id, "definition": created.note_type()})
        assert json.loads(sys.stdin.readline())["continue"]
        created["Hint"] = "native hint"
        collection.update_note(created)
        send = client.batch

        def lose_response(profile_id, payload):
            assert len(payload["creations"]) == 1
            send(profile_id, payload)
            raise ConnectionError("lost owned binding response")

        client.batch = lose_response
        try:
            delivery.acknowledge()
        except ConnectionError:
            pass
        else:
            raise AssertionError("Expected lost binding response")
        finally:
            client.batch = send
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=epoch)
        delivery.acknowledge()
        for _ in range(6):
            page = delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            if not page["changes"]:
                break
        else:
            raise AssertionError("Owned creation deliveries did not drain")
        changes = SyncChanges(sidecar, client)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        created = collection.get_note(created_id)
        notes = collection.db.list("SELECT id FROM notes WHERE mid = ? ORDER BY id", created.mid)
        assert len(notes) == 2
        emit({"stage": "owned_synced", "note_ids": notes, "fields": created.fields,
              "definition": created.note_type(), "reviews": collection.db.scalar("SELECT count(*) FROM revlog")})
        assert json.loads(sys.stdin.readline())["continue"]
        prepare_with_lost_response(sidecar, client)
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        emit({"stage": "owned_material_refreshed", "fields": collection.get_note(created_id).fields})
        assert json.loads(sys.stdin.readline())["continue"]
        prepare_with_lost_response(sidecar, client)
        for _ in range(10):
            page = delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            if not page["changes"]:
                break
        else:
            raise AssertionError("Bulk creation deliveries did not drain")
        bulk = [collection.get_note(nid) for nid in collection.db.list("SELECT id FROM notes WHERE mid = ? ORDER BY id", created.mid)]
        assert len(bulk) == 4
        emit({"stage": "owned_bulk_created", "notes": [{"id": note.id, "fields": note.fields,
              "cards": [{"id": card.id, "queue": card.queue, "reps": card.reps} for card in note.cards()]} for note in bulk],
              "reviews": collection.db.scalar("SELECT count(*) FROM revlog")})
        assert json.loads(sys.stdin.readline())["continue"]
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
