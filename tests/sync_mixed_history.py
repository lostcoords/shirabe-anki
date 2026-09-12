"""Swap disposable history fixtures through Rails, retaining both event identities."""

import copy
import json
from pathlib import Path
import sys

import support
from anki.buildinfo import version
from anki.collection import Collection
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import CARD_COLUMNS, REVLOG_COLUMNS, card_row, same_state
from shirabe_addon.sync_snapshot import SyncSnapshot, SnapshotConflict
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit, source_rows


def run(config):
    assert version == "26.08.1"
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck_id = collection.decks.id("Mixed history")
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
        changes = SyncChanges(sidecar, client)

        def sync():
            changes.capture(collection)
            changes.upload()
            changes.apply(collection)

        card = collection.get_card(card_id)
        card.start_timer()
        collection.sched.answerCard(card, 3)
        first = source_rows(collection, "revlog")[0]
        first_card = card_row(collection, card_id)
        sync()
        emit({"stage": "first_review", "card_id": card_id, "row": first})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.undo()
        sync()
        card = collection.get_card(card_id)
        card.start_timer()
        collection.sched.answerCard(card, 4)
        second = source_rows(collection, "revlog")[0]
        assert second["id"] != first["id"]
        removed = [second]
        if config.get("large"):
            extra = [{**second, "id": second["id"] + 1000 + index, "type": 4, "ease": 0} for index in range(400)]
            collection.db.executemany(f"INSERT INTO revlog ({','.join(REVLOG_COLUMNS)}) VALUES ({','.join('?' for _ in REVLOG_COLUMNS)})",
                                      [[row[key] for key in REVLOG_COLUMNS] for row in extra])
            removed.extend(extra)
        sync()
        emit({"stage": "second_review", "row": second})
        assert json.loads(sys.stdin.readline())["continue"]

        def swap_fixture_history():
            collection.db.executemany("DELETE FROM revlog WHERE id=?", [[row["id"]] for row in removed])
            collection.db.execute(f"INSERT INTO revlog ({','.join(REVLOG_COLUMNS)}) VALUES ({','.join('?' for _ in REVLOG_COLUMNS)})",
                                  *(first[key] for key in REVLOG_COLUMNS))
            columns = [key for key in CARD_COLUMNS if key != "id"]
            collection.db.execute(f"UPDATE cards SET {','.join(key + '=?' for key in columns)} WHERE id=?",
                                  *(first_card[key] for key in columns), card_id)

        collection.db.transact(swap_fixture_history)
        changes.capture(collection)
        with SyncState(sidecar) as state:
            payloads = [state.batch(key)["payload"] for key in state.get("changes")["keys"]]
        entry = next(entry for payload in payloads for entry in payload.get("cards", []))
        assert entry["removed_history"] == removed
        assert [item["row"] for item in entry["restored_history"]] == [first]
        send = client.batch

        def upload_with_lost_response():
            sent = []

            def lose_response(profile_id, payload):
                sent.append(payload)
                result = send(profile_id, payload)
                if len(sent) == 1:
                    raise ConnectionError("Lost mixed history response")
                return result

            client.batch = lose_response
            try:
                changes.upload()
            except ConnectionError:
                changes.upload()
            else:
                raise AssertionError("Expected a lost history response")
            finally:
                client.batch = send
            assert len(sent) == 2 and sent[0] == sent[1]

        upload_with_lost_response()
        if config["schedule"] == "automatic":
            changes.apply(collection)
        else:
            try:
                changes.apply(collection)
            except SyncConflict as error:
                assert len(error.conflicts) == 1 and error.conflicts[0]["code"] == "history_changed_before_undo"
                problem = error.conflicts[0]
            else:
                raise AssertionError("Expected the concurrent review to require a schedule choice")
            detail = changes.card_conflict(problem)
            assert detail["history_removal"]["restored"] == entry["restored_history"]
            altered = copy.deepcopy(detail)
            altered["history_removal"]["restored"][0]["removal_id"] += 1
            try:
                changes.prepare_history_removal(collection, problem, altered)
            except SnapshotConflict:
                pass
            else:
                raise AssertionError("Expected the changed restoration identity to be refused")
            prepared = changes.prepare_history_removal(collection, problem, detail)
            collection.db.execute("UPDATE revlog SET time=time+1 WHERE id=?", first["id"])
            try:
                changes.resolve_history_removal(collection, prepared, schedule=config["schedule"])
            except SnapshotConflict:
                pass
            else:
                raise AssertionError("Expected history changed after opening the chooser to be refused")
            with SyncState(sidecar) as state:
                assert state.replacement_for(problem["request_key"], "cards", card_id) is None
            collection.db.execute("UPDATE revlog SET time=? WHERE id=?", first["time"], first["id"])
            changes.resolve_history_removal(collection, prepared, schedule=config["schedule"])
            upload_with_lost_response()
            try:
                changes.apply(collection)
            except SyncConflict as error:
                raise AssertionError(error.conflicts) from error
        selected = card_row(collection, card_id)
        emit({"stage": "mixed_applied", "card": selected})
        assert json.loads(sys.stdin.readline())["continue"]
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        for _ in range(10):
            page = delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            if not page["changes"]:
                break
        else:
            raise AssertionError("Mixed history delivery did not drain")
        assert same_state(selected, card_row(collection, card_id))
        history = source_rows(collection, "revlog")
        assert first in history and not any(row["id"] == second["id"] for row in history)
        assert len(history) == (1 if config["schedule"] == "automatic" else 2)
        sync()
        emit({"stage": "mixed_synced", "history": history})
        assert json.loads(sys.stdin.readline())["continue"]
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
