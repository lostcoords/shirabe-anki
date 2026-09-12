"""Exercise simultaneous edits and reviews using disposable AnkiWeb records."""

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import time
import uuid

import support
from anki.buildinfo import version
from anki.collection import Collection
from anki.scheduler.v3 import CardAnswer
from ankiweb_live_probe import LiveAccountProbe
from native_sync_server import native_sync_server, full_sync
from study_corpus import add_type
from sync_competing_reviews import drain
from sync_http_roundtrip import authorize, emit
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import card_row, history_row, same_state
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState


def proceed():
    assert json.loads(sys.stdin.readline())["continue"]


def review_rows(collection, card_ids):
    identities = collection.db.list("SELECT id FROM revlog WHERE cid IN (?, ?) ORDER BY id", *card_ids)
    return [history_row(collection, identity) for identity in identities]


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
        emit({"stage": "progress", "phase": "Prepared desktop copy", "done": 0, "total": 6})
        receiver = probe.clone("receiver", source=config.get("ankiweb_source"))
        emit({"stage": "progress", "phase": "Prepared receiver copy", "done": 0, "total": 6})

        def sync(collection):
            probe.sync(collection)
            emit({"stage": "progress", "phase": "AnkiWeb conflict probe", "done": probe.syncs, "total": 6})

        prefix = "Shirabe Test Conflicts " + uuid.uuid4().hex[:12]
        deck_id = desktop.decks.id(prefix)
        model = add_type(desktop, prefix, ["Expression", "Meaning", "Hint"],
                         [("Recognition", "{{Expression}}", "{{Meaning}}", None)])
        notes = []
        for label in ("Independent edits", "Conflicting edits"):
            note = desktop.new_note(model)
            note.fields = [label, "original meaning", "original hint"]
            note.tags = ["shared", "remove_in_anki", "remove_in_shirabe"]
            desktop.add_note(note, deck_id)
            notes.append(note)
        card_ids = [item.cards()[0].id for item in notes]
        (root / "fixture.json").write_text(json.dumps({"deck": prefix, "deck_id": deck_id,
            "note_ids": [item.id for item in notes], "card_ids": card_ids, "note_type_id": model["id"],
            "live": not config.get("rehearsal")}, indent=2) + "\n")
        sync(desktop)
        sync(receiver)
        snapshot = SyncSnapshot(path, client)
        captured = snapshot.capture(desktop, [deck_id])
        profile = snapshot.upload()
        delivery = SyncDelivery(path, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        changes = SyncChanges(path, client)
        answered_at = int(time.time()) * 1000 + 500
        for index, item in enumerate(notes):
            remote = receiver.get_note(item.id)
            remote.fields[index] = "Anki expression" if index == 0 else "Anki meaning"
            remote.tags = ["shared", "remove_in_shirabe", "anki"]
            receiver.update_note(remote)
            card = receiver.get_card(card_ids[index])
            card.start_timer()
            states = receiver._backend.get_scheduling_states(card.id)
            answer = receiver.sched.build_answer(card=card, states=states, rating=CardAnswer.EASY)
            answer.answered_at_millis = answered_at
            receiver.sched.answer_card(answer)
        native_history = review_rows(receiver, card_ids)
        emit({"stage": "conflicts_offline", "profile_id": profile["id"], "note_ids": [item.id for item in notes],
              "card_ids": card_ids, "history": native_history,
              "cards": [card_row(receiver, identity) for identity in card_ids]})
        proceed()
        sync(receiver)
        sync(desktop)
        desktop.models._clear_cache()
        changes.capture(desktop)
        changes.upload()
        try:
            changes.apply(desktop)
        except SyncConflict as error:
            assert len(error.conflicts) == 1, error.conflicts
            problem = error.conflicts[0]
            assert problem["kind"] == "notes" and problem["id"] == notes[1].id, problem
            prepared = changes.prepare_note_resolution(desktop, problem, changes.note_conflict(problem))
            assert prepared["local"]["flds"].split("\x1f")[1] == "Anki meaning"
            assert prepared["detail"]["note"]["fields"][1] == "Shirabe meaning"
            changes.resolve_note(desktop, prepared, ["Conflicting edits", "Chosen meaning", "original hint"],
                                 ["anki", "shared", "shirabe"])
        else:
            raise AssertionError("The simultaneous field edit was overwritten without a choice")
        send, attempts = client.batch, []

        def lose_choice_response(profile_id, payload):
            result = send(profile_id, payload)
            if any(row.get("conflict_id") for row in payload.get("notes", [])):
                attempts.append(payload)
                if len(attempts) == 1:
                    raise ConnectionError("Lost committed conflict choice receipt")
            return result

        client.batch = lose_choice_response
        try:
            changes.upload()
        except ConnectionError:
            changes = SyncChanges(path, client)
        else:
            raise AssertionError("Expected a lost conflict choice receipt")
        changes.upload()
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        client.batch = send
        changes.apply(desktop)
        drain(delivery, desktop)
        changes.capture(desktop)
        changes.upload()
        changes.apply(desktop)
        drain(delivery, desktop)
        expected_fields = [["Anki expression", "Shirabe meaning", "original hint"],
                           ["Conflicting edits", "Chosen meaning", "original hint"]]
        for item, fields in zip(notes, expected_fields):
            current = desktop.get_note(item.id)
            assert current.fields == fields, current.fields
            assert sorted(current.tags) == ["anki", "shared", "shirabe"], current.tags
        history = review_rows(desktop, card_ids)
        assert len(history) == 4
        assert {row["id"] for row in native_history}.issubset(row["id"] for row in history)
        sync(desktop)
        sync(receiver)
        assert review_rows(receiver, card_ids) == review_rows(desktop, card_ids)
        for item, fields in zip(notes, expected_fields):
            assert receiver.get_note(item.id).fields == fields
            assert sorted(receiver.get_note(item.id).tags) == ["anki", "shared", "shirabe"]
        for identity in card_ids:
            assert same_state(card_row(desktop, identity), card_row(receiver, identity))
        with SyncState(path) as state:
            assert state.get("changes") is None and state.get("delivery") is None
        emit({"stage": "conflicts_reconciled", "fields": expected_fields, "history": review_rows(desktop, card_ids),
              "cards": [card_row(desktop, identity) for identity in card_ids]})
        proceed()
        result = probe.report(deck=prefix, deck_id=deck_id, note_ids=[item.id for item in notes], card_ids=card_ids,
                              note_type_id=model["id"], field_conflict=True, independent_fields=True,
                              concurrent_tags=True, concurrent_reviews=True, lost_choice_response=True, media_tested=False)
        emit({"stage": "conflicts_verified", **result})


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
