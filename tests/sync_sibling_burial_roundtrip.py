"""Exchange automatic sibling burial and a confirmed Undo through real HTTP."""

from contextlib import closing
import json
from pathlib import Path
import sys

import support
from anki.collection import Collection
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit


def exchange(collection, changes, delivery):
    changes.capture(collection)
    changes.upload()
    changes.apply(collection)
    for _ in range(10):
        page = delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        if not page["changes"]:
            return
    raise AssertionError("Sibling changes did not drain")


def run(config):
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    with closing(Collection(str(path))) as collection:
        collection.set_config("fsrs", True)
        deck = collection.decks.id("Sibling burial")
        preset = collection.decks.config_dict_for_deck_id(deck)
        preset["new"]["bury"] = True
        collection.decks.update_config(preset)
        note = collection.new_note(collection.models.by_name("Basic (and reversed card)"))
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck)
        card_id, sibling_id = [card.id for card in note.cards()]
        sidecar = root / "sync.sqlite3"
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        captured = snapshot.capture(collection, [deck])
        profile = snapshot.upload()
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        changes = SyncChanges(sidecar, client)
        emit({"stage": "siblings_ready", "card_id": card_id, "sibling_id": sibling_id})
        review_id = json.loads(sys.stdin.readline())["review_id"]
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        assert collection.get_card(sibling_id).queue == -2
        assert collection.get_card(card_id).reps == 1
        requested = client._profile_request(profile["id"], "/review_undos",
                                            {"review_id": review_id, "request_key": "sibling-undo"})
        delivery.receive()
        try:
            delivery.apply(collection)
        except DeliveryConflict as error:
            problem = next(item for item in error.conflicts if item["kind"] == "review_undos")
        else:
            raise AssertionError("Undo applied without confirmation")
        prepared = delivery.prepare_review_undo_resolution(collection, problem)
        assert "undo_error" not in prepared, prepared
        assert len(prepared["siblings"]) == 1
        delivery.confirm_review_undo(collection, prepared)
        delivery.apply(collection)
        assert collection.get_card(sibling_id).queue == 0
        assert collection.get_card(card_id).reps == 0
        delivery.acknowledge()
        exchange(collection, changes, delivery)
        emit({"stage": "siblings_undone", "request_id": requested["id"]})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.decks.select(deck)
        collection.sched.reset()
        card = collection.sched.getCard()
        assert card.id == card_id
        card.start_timer()
        collection.sched.answerCard(card, 4)
        assert collection.get_card(sibling_id).queue == -2
        exchange(collection, changes, delivery)
        with SyncState(sidecar) as state:
            assert not state.pending()
        emit({"stage": "native_siblings_buried"})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        assert collection.get_card(sibling_id).queue == 0
        assert collection.get_card(card_id).reps == 0
        exchange(collection, changes, delivery)
        emit({"stage": "native_siblings_undone"})
        assert json.loads(sys.stdin.readline())["continue"]
        client.sign_out()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
