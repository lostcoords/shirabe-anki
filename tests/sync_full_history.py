"""Reconcile native full sync with archived and concurrent reviews."""

import json
from pathlib import Path
import sys

import support
from anki.buildinfo import version
from anki.collection import Collection
from anki.errors import SyncError
from native_sync_server import native_sync_server, full_sync
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import card_row, same_state, STATE_COLUMNS
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit, source_rows


def run(config):
    assert version == "26.08.1"
    root = Path(config["directory"])
    recovery = config.get("recovery", "download")
    assert recovery in ("download", "upload")
    path = root / "collection.anki2"
    assert not path.exists()
    with native_sync_server(root) as endpoint:
        collection = Collection(str(path))
        collection.set_config("fsrs", True)
        try:
            deck_id = collection.decks.id("Full history recovery")
            note = collection.new_note(collection.models.by_name("Basic"))
            note.fields = ["猫", "cat"]
            collection.add_note(note, deck_id)
            card_id = note.cards()[0].id
            initial = card_row(collection, card_id)
            sidecar = root / "sync.sqlite3"
            client = SyncLogin(sidecar, config["origin"])
            authorize(client)
            snapshot = SyncSnapshot(sidecar, client)
            captured = snapshot.capture(collection, [deck_id])
            profile = snapshot.upload()
            changes = SyncChanges(sidecar, client)
            delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=captured["pair_request"]["key"])

            def sync():
                changes.capture(collection)
                changes.upload()
                changes.apply(collection)

            def drain():
                for _ in range(10):
                    page = delivery.receive()
                    delivery.apply(collection)
                    delivery.acknowledge()
                    if not page["changes"]:
                        return
                raise AssertionError("Review deliveries did not drain")

            emit({"stage": "awaiting_review", "card_id": card_id})
            assert json.loads(sys.stdin.readline())["continue"]
            drain()
            assert collection.db.scalar("SELECT count(*) FROM revlog") == 1
            card = collection.get_card(card_id)
            card.start_timer()
            collection.sched.answerCard(card, 3)
            assert collection.db.scalar("SELECT count(*) FROM revlog") == 2
            auth = collection.sync_login("probe", "disposable", endpoint)
            assert auth.endpoint == endpoint
            collection.sync_collection(auth, False)
            full_sync(collection, auth, upload=True)
            sync()
            history = source_rows(collection, "revlog")
            reviewed = card_row(collection, card_id)
            assert all(row["usn"] >= 0 for row in history)
            published_mod = collection.db.scalar("SELECT mod FROM col")
            emit({"stage": "published", "history": history, "card": reviewed})
            assert json.loads(sys.stdin.readline())["continue"]

            def remove_fixture_history():
                collection.db.execute("DELETE FROM revlog WHERE cid = ?", card_id)
                card = collection.get_card(card_id)
                for key in STATE_COLUMNS:
                    setattr(card, key, initial[key])
                card.memory_state = None
                card.last_review_time = None
                collection.update_card(card)

            collection.db.transact(remove_fixture_history)
            sync()
            assert same_state(card_row(collection, card_id), initial)
            emit({"stage": "removed"})
            assert json.loads(sys.stdin.readline())["continue"]
            # Anki skips sync when millisecond modification stamps match the server.
            collection.db.execute("UPDATE col SET mod = max(mod, ?) + 1", published_mod)
            try:
                collection.sync_collection(auth, False)
            except SyncError as error:
                assert "one-way sync" in str(error), error
            else:
                raise AssertionError("Published review deletion must require a native full sync")
            full_sync(collection, auth, upload=recovery == "upload")
            if recovery == "upload":
                assert source_rows(collection, "revlog") == []
                assert same_state(card_row(collection, card_id), initial)
                sync()
            else:
                assert source_rows(collection, "revlog") == history
                assert same_state(card_row(collection, card_id), reviewed)
                changes.capture(collection)
                send = client.batch
                sent = []

                def lose_response(profile_id, payload):
                    sent.append(payload)
                    result = send(profile_id, payload)
                    if payload.get("cards") and sum(bool(item.get("cards")) for item in sent) == 1:
                        raise ConnectionError("Lost full-download restoration response")
                    return result

                client.batch = lose_response
                try:
                    changes.upload()
                except ConnectionError:
                    changes = SyncChanges(sidecar, client)
                    changes.upload()
                else:
                    raise AssertionError("Expected a lost restoration response")
                finally:
                    client.batch = send
                card_batches = [item for item in sent if item.get("cards")]
                assert len(card_batches) == 2 and card_batches[0] == card_batches[1]
                assert len(card_batches[0]["cards"][0]["restored_history"]) == 2
                changes.apply(collection)
            drain()
            sync()
            collection.sync_collection(auth, False)
            sync()
            final_history = source_rows(collection, "revlog")
            restored_ids = {row["id"] for row in history}
            assert [row for row in final_history if row["id"] in restored_ids] == (history if recovery == "download" else [])
            assert len(final_history) == (2 if recovery == "download" else 0) + int(config.get("concurrent", False))
            if not config.get("concurrent"):
                assert same_state(card_row(collection, card_id), reviewed if recovery == "download" else initial)
            receiver = Collection(str(root / "receiver.anki2"))
            try:
                receiver.sync_collection(auth, False)
                full_sync(receiver, auth, upload=False)
                assert source_rows(receiver, "revlog") == final_history
                assert same_state(card_row(receiver, card_id), card_row(collection, card_id))
            finally:
                receiver.close()
            with SyncState(sidecar) as state:
                assert state.get("changes") is None
                checkpoints = state.checkpoint_digests("source_history")
                assert all((checkpoints[source_id] == "deleted") == (recovery == "upload") for source_id in restored_ids)
            emit({"stage": "recovered", "history": final_history, "card": card_row(collection, card_id)})
            assert json.loads(sys.stdin.readline())["continue"]
        finally:
            collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
