"""Exercise selected-deck changes and note creation across the native sync bridge."""

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import uuid

import support
from anki.buildinfo import version
from anki.collection import Collection
from ankiweb_live_probe import LiveAccountProbe
from native_sync_server import native_sync_server, full_sync
from study_corpus import add_type
from sync_http_roundtrip import authorize, emit
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_collection import SyncCollection
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import card_row, same_state
from shirabe_addon.sync_snapshot import SyncSnapshot


def proceed():
    assert json.loads(sys.stdin.readline())["continue"]


def run(config):
    assert version == "26.08.1"
    root = Path(config["artifact_directory"])
    path = root / "sidecar.sqlite3"
    client = SyncLogin(path, config["origin"])
    authorize(client)
    with ExitStack() as stack:
        if config.get("rehearsal"):
            endpoint = stack.enter_context(native_sync_server(root))
            source = root / "rehearsal-source"
            source.mkdir()
            original = Collection(str(source / "collection.anki2"))
            original.set_config("fsrs", True)
            note = original.new_note(original.models.by_name("Basic"))
            note.fields = ["Existing note", "Must remain unchanged"]
            original.add_note(note, original.decks.id("Existing deck"))
            auth = original.sync_login("probe", "disposable", endpoint)
            original.sync_collection(auth, False)
            full_sync(original, auth, upload=True)
            original.close()
            probe = LiveAccountProbe(root / "copies", source, auth=auth)
        else:
            probe = LiveAccountProbe(root, config["ankiweb_profile"])
        stack.callback(probe.close)
        desktop = probe.clone("desktop", source=config.get("ankiweb_source"))
        receiver = probe.clone("receiver", source=config.get("ankiweb_source"))
        prefix = "Shirabe Test Workflows " + uuid.uuid4().hex[:12]
        first = desktop.decks.id(prefix)
        second = desktop.decks.id(prefix + " destination")
        model = add_type(desktop, prefix, ["Expression", "Meaning"],
            [("Recognition", "{{Expression}}", "{{Meaning}}", None)])
        note = desktop.new_note(model)
        note.fields = ["猫", "cat"]
        note.tags = ["shirabe_ankiweb_test"]
        desktop.add_note(note, first)
        card_id = note.cards()[0].id
        (root / "fixture.json").write_text(json.dumps({"decks": [prefix, prefix + " destination"],
            "deck_ids": [first, second], "note_id": note.id, "card_id": card_id,
            "note_type_id": model["id"], "live": not config.get("rehearsal")}, indent=2) + "\n")
        probe.sync(desktop)
        probe.sync(receiver)
        snapshot = SyncSnapshot(path, client)
        captured = snapshot.capture(desktop, [first, second])
        profile = snapshot.upload()
        sync = SyncDelivery(path, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        changes = SyncChanges(path, client)
        emit({"stage": "workflows_uploaded", "profile_id": profile["id"],
            "note_id": note.id, "card_id": card_id, "first_deck": first, "second_deck": second})
        proceed()
        sync.receive()
        sync.apply(desktop)
        sync.acknowledge()
        assert desktop.get_card(card_id).queue == -1
        assert desktop.get_card(card_id).did == second
        created = desktop.find_notes(f"mid:{model['id']}")
        assert len(created) == 2
        created_id = next(identity for identity in created if identity != note.id)
        assert desktop.get_note(created_id).fields == ["犬", "dog from Shirabe"]
        probe.sync(desktop)
        probe.sync(receiver)
        assert receiver.get_note(created_id).fields == desktop.get_note(created_id).fields
        assert receiver.get_note(created_id).tags == ["created_in_shirabe"]
        assert same_state(card_row(desktop, card_id), card_row(receiver, card_id))
        assert receiver.get_card(card_id).queue == -1
        assert receiver.get_card(card_id).did == second
        emit({"stage": "workflows_outbound", "created_note_id": created_id})
        proceed()
        receiver.sched.unsuspend_cards([card_id])
        receiver.set_deck([card_id], first)
        receiver.models._clear_cache()
        changed = receiver.models.get(model["id"])
        changed["css"] = ".card { color: #234567; }"
        receiver.models.save(changed)
        added = receiver.new_note(changed)
        added.fields = ["鳥", "bird from Anki"]
        added.tags = ["created_in_anki"]
        receiver.add_note(added, second)
        probe.sync(receiver)
        probe.sync(desktop)
        desktop.models._clear_cache()
        assert same_state(card_row(desktop, card_id), card_row(receiver, card_id))
        assert desktop.get_card(card_id).queue == 0
        assert desktop.get_card(card_id).did == first
        changes.capture(desktop)
        changes.upload()
        changes.apply(desktop)
        sync.receive()
        sync.apply(desktop)
        sync.acknowledge()
        emit({"stage": "workflows_inbound", "added_note_id": added.id,
            "added_card_id": added.cards()[0].id, "css": changed["css"]})
        proceed()
        SyncCollection(path, client).prepare_publications()
        for _ in range(12):
            page = sync.receive()
            try:
                sync.apply(desktop)
            except DeliveryConflict as error:
                raise AssertionError(error.conflicts) from error
            sync.acknowledge()
            if not page["changes"]:
                break
        else:
            raise AssertionError("Owned publication did not finish")
        owned = [desktop.get_note(nid) for nid in desktop.find_notes(f'deck:"{prefix} destination" tag:owned_publication')]
        assert len(owned) == 3
        assert {item.note_type()["shirabe"]["kind"] for item in owned} == {"vocab", "kanji", "writing"}
        owned_ids = [item.id for item in owned]
        probe.sync(desktop)
        probe.sync(receiver)
        desktop.models._clear_cache()
        receiver.models._clear_cache()
        owned_rows = []
        for item in owned:
            remote = receiver.get_note(item.id)
            assert remote.fields == item.fields
            assert remote.note_type() == item.note_type()
            native_card = item.cards()[0]
            assert same_state(card_row(desktop, native_card.id), card_row(receiver, native_card.id))
            assert native_card.reps == 1
            owned_rows.append({"id": item.id, "kind": item.note_type()["shirabe"]["kind"],
                               "fields": remote.fields, "card_id": native_card.id})
        emit({"stage": "workflows_owned_outbound", "notes": owned_rows})
        proceed()
        for identity in owned_ids:
            remote = receiver.get_note(identity)
            remote["Hint"] = "Edited after AnkiWeb sync"
            receiver.update_note(remote)
        probe.sync(receiver)
        probe.sync(desktop)
        desktop.models._clear_cache()
        changes.capture(desktop)
        changes.upload()
        changes.apply(desktop)
        sync.receive()
        sync.apply(desktop)
        sync.acknowledge()
        emit({"stage": "workflows_owned_inbound", "note_ids": owned_ids})
        proceed()
        result = probe.report(decks=[prefix, prefix + " destination"], deck_ids=[first, second],
            note_ids=[note.id, created_id, added.id, *owned_ids], note_type_id=model["id"],
            owned_note_type_ids=[item.mid for item in owned], owned_publication=True,
            creation_both_sides=True, suspension=True, selected_deck_moves=True,
            anki_owned_css=True, media_tested=False)
        emit({"stage": "workflows_verified", **result})


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
