"""Exchange disposable reviews through Rails and the explicitly authorized AnkiWeb account."""

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
from sync_http_roundtrip import authorize, emit
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import SyncDelivery, DeliveryConflict
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import history_row, same_state, card_row
from shirabe_addon.sync_snapshot import SyncSnapshot


def proceed():
    assert json.loads(sys.stdin.readline())["continue"]


def run(config):
    assert version == "26.08.1"
    root = Path(config["artifact_directory"])
    client = SyncLogin(root / "sidecar.sqlite3", config["origin"])
    authorize(client)
    with ExitStack() as stack:
        if config.get("rehearsal"):
            endpoint = stack.enter_context(native_sync_server(root))
            source = root / "rehearsal-source"
            source.mkdir()
            original = Collection(str(source / "collection.anki2"))
            original.set_config("fsrs", True)
            original_note = original.new_note(original.models.by_name("Basic"))
            original_note.fields = ["Existing note", "Must remain unchanged"]
            original.add_note(original_note, original.decks.id("Existing deck"))
            auth = original.sync_login("probe", "disposable", endpoint)
            original.sync_collection(auth, False)
            full_sync(original, auth, upload=True)
            original.close()
            probe = LiveAccountProbe(root / "copies", source, auth=auth)
        else:
            probe = LiveAccountProbe(root, config["ankiweb_profile"])
        stack.callback(probe.close)
        desktop, mobile = probe.clone("desktop"), probe.clone("mobile")
        deck_name = "Shirabe Test Sync " + uuid.uuid4().hex[:12]
        deck_id = desktop.decks.id(deck_name)
        model = desktop.models.by_name("Basic")
        assert model and len(model["tmpls"]) == 1
        note = desktop.new_note(model)
        note.fields = ["Shirabe disposable AnkiWeb test"] * len(model["flds"])
        note.tags = ["shirabe_ankiweb_test"]
        desktop.add_note(note, deck_id)
        assert len(note.cards()) == 1
        card_id = note.cards()[0].id
        (root / "fixture.json").write_text(json.dumps({"deck": deck_name, "deck_id": deck_id,
            "note_id": note.id, "card_id": card_id, "live": not config.get("rehearsal")}, indent=2) + "\n")
        probe.sync(desktop)
        probe.sync(mobile)
        assert mobile.get_note(note.id).fields == note.fields
        snapshot = SyncSnapshot(root / "sidecar.sqlite3", client)
        captured = snapshot.capture(desktop, [deck_id])
        profile = snapshot.upload()
        sync = SyncDelivery(root / "sidecar.sqlite3", client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        changes = SyncChanges(root / "sidecar.sqlite3", client)

        def exchange():
            changes.capture(desktop)
            changes.upload()
            changes.apply(desktop)
            sync.receive()
            sync.apply(desktop)
            sync.acknowledge()

        emit({"stage": "ankiweb_uploaded", "note_id": note.id, "card_id": card_id})
        proceed()
        sync.receive()
        sync.apply(desktop)
        sync.acknowledge()
        first_history = desktop.db.list("SELECT id FROM revlog WHERE cid = ?", card_id)
        assert len(first_history) == 1
        emit({"stage": "unpublished_review", "history_id": first_history[0]})
        proceed()
        sync.receive()
        try:
            sync.apply(desktop)
        except DeliveryConflict as error:
            prepared = sync.prepare_review_undo_resolution(desktop, error.conflicts[0])
            assert "undo_error" not in prepared
            sync.confirm_review_undo(desktop, prepared)
        else:
            raise AssertionError("Cross-app Undo did not request confirmation")
        sync.apply(desktop)
        sync.acknowledge()
        assert not desktop.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id)
        probe.sync(desktop)
        probe.sync(mobile)
        assert not mobile.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id)
        emit({"stage": "unpublished_undo_synced"})
        proceed()
        sync.receive()
        sync.apply(desktop)
        sync.acknowledge()
        review_id = desktop.db.scalar("SELECT id FROM revlog WHERE cid = ?", card_id)
        assert review_id
        probe.sync(desktop)
        probe.sync(mobile)
        assert history_row(mobile, review_id) == history_row(desktop, review_id)
        assert same_state(card_row(desktop, card_id), card_row(mobile, card_id))
        assert mobile.get_note(note.id).fields[-1] == "Edited in Shirabe"
        emit({"stage": "published_review", "history_id": review_id})
        proceed()
        delivered = sync.receive()
        assert delivered["changes"]
        try:
            sync.apply(desktop)
        except DeliveryConflict as error:
            prepared = sync.prepare_review_undo_resolution(desktop, error.conflicts[0])
            assert "native sync" in prepared["undo_error"]
        else:
            raise AssertionError("Published review Undo was not refused")
        assert history_row(desktop, review_id)
        emit({"stage": "published_undo_refused"})
        proceed()
        sync.withdraw_review_undo(desktop, prepared)
        sync.apply(desktop)
        sync.acknowledge()
        edited = mobile.get_note(note.id)
        edited.fields[0] = "Edited on the other Anki client"
        mobile.update_note(edited)
        card = mobile.get_card(card_id)
        card.start_timer()
        mobile.sched.answerCard(card, 3)
        probe.sync(mobile)
        probe.sync(desktop)
        exchange()
        history_ids = desktop.db.list("SELECT id FROM revlog WHERE cid = ? ORDER BY id", card_id)
        assert len(history_ids) == 2
        emit({"stage": "ankiweb_returned", "history_ids": history_ids})
        proceed()
        result = probe.report(deck=deck_name, deck_id=deck_id, note_id=note.id, card_id=card_id,
                              history_ids=history_ids, unpublished_undo=True, published_undo_refused=True,
                              media_tested=False)
        emit({"stage": "ankiweb_verified", **result})


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
