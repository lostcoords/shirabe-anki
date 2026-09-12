"""Compare leech review and confirmed Undo in both apps over real HTTP."""

from contextlib import closing
import json
from pathlib import Path
import sys
import time

import support
from anki.cards_pb2 import FsrsMemoryState
from anki.collection import Collection
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import card_row
from shirabe_addon.sync_snapshot import SyncSnapshot
from sync_http_roundtrip import authorize, emit
from sync_sibling_burial_roundtrip import exchange


def run(config):
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    with closing(Collection(str(path))) as collection:
        collection.set_config("fsrs", True)
        cases, decks = [], []
        for action, tags in ((0, ["original"]), (1, ["original"]), (0, ["Leech", "original"])):
            label = f"Leech action {action} tags {len(tags)}"
            deck_id = collection.decks.id(label)
            decks.append(deck_id)
            preset = collection.decks.add_config(label)
            preset["lapse"].update(leechFails=3, leechAction=action, delays=[10])
            collection.decks.update_config(preset)
            deck = collection.decks.get(deck_id)
            deck["conf"] = preset["id"]
            collection.decks.save(deck)
            note = collection.new_note(collection.models.by_name("Basic"))
            note.fields, note.tags = [label, "answer"], tags
            collection.add_note(note, deck_id)
            card = note.cards()[0]
            card.type = card.queue = 2
            card.due = collection.sched.today
            card.ivl, card.reps, card.lapses = 20, 30, 2
            card.memory_state = FsrsMemoryState(stability=20, difficulty=5)
            card.last_review_time = int(time.time()) - 20 * 86400
            collection.update_card(card)
            card.start_timer()
            collection.sched.answerCard(card, 3)
            cases.append({"card_id": card.id, "note_id": note.id, "action": action,
                          "tags": collection.get_note(note.id).tags, "before": card_row(collection, card.id)})
        sidecar = root / "sync.sqlite3"
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        captured = snapshot.capture(collection, decks)
        profile = snapshot.upload()
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        changes = SyncChanges(sidecar, client)
        for sample in cases:
            emit({"stage": "leech_predecessor", "card_id": sample["card_id"]})
            assert json.loads(sys.stdin.readline())["continue"]
            delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            sample["before"] = card_row(collection, sample["card_id"])
            emit({"stage": "leech_ready", **sample})
            review_id = json.loads(sys.stdin.readline())["review_id"]
            delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            card = collection.get_card(sample["card_id"])
            assert card.queue == (-1 if sample["action"] == 0 else 1)
            assert card.lapses == 3
            tags = collection.get_note(card.nid).tags
            assert sum(tag.casefold() == "leech" for tag in tags) == 1, tags
            requested = client._profile_request(profile["id"], "/review_undos",
                                                {"review_id": review_id, "request_key": f"leech-{card.id}"})
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
            restored = card_row(collection, card.id)
            assert {key: value for key, value in restored.items() if key not in ("mod", "usn")} == \
                {key: value for key, value in sample["before"].items() if key not in ("mod", "usn")}, (sample, restored)
            assert collection.get_note(card.nid).tags == sample["tags"]
            delivery.acknowledge()
            exchange(collection, changes, delivery)
            assert collection.get_note(card.nid).tags == sample["tags"]
            emit({"stage": "leech_undone", "request_id": requested["id"]})
            assert json.loads(sys.stdin.readline())["continue"]
        client.sign_out()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
