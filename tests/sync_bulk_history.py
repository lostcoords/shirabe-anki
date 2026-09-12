"""Round-trip multi-batch history changes through the Rails sync endpoints."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import support
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
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
        deck_id = collection.decks.id("Bulk history")
        model = collection.models.by_name("Basic")
        for index in range(201):
            note = collection.new_note(model)
            note.fields = [f"猫 {index}", "cat"]
            collection.add_note(note, deck_id)
        card_ids = collection.db.list("SELECT id FROM cards ORDER BY id")
        rows = []
        for index, card_id in enumerate(card_ids):
            for event in range(401 if index == 0 else 1):
                rows.append((1_700_000_000_000 + index * 1000 + event, card_id, -1, 3, 1, 0, 2500, 100, 1))
        insert = "INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        collection.db.executemany(insert, rows)
        history = source_rows(collection, "revlog")
        client = SyncLogin(root / "sync.sqlite3", config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(root / "sync.sqlite3", client)
        snapshot.capture(collection, [deck_id])
        snapshot.upload()
        changes = SyncChanges(root / "sync.sqlite3", client)
        emit({"stage": "history_synced", "card_ids": card_ids, "history_ids": [row[0] for row in rows]})
        assert json.loads(sys.stdin.readline())["continue"]

        for operation in ("removed", "restored"):
            if operation == "removed":
                collection.db.execute("DELETE FROM revlog")
            else:
                collection.db.executemany(insert, rows)
            changes.capture(collection)
            with SyncState(root / "sync.sqlite3") as state:
                payloads = [state.batch(key)["payload"] for key in state.get("changes")["keys"]]
            assert [len(payload["cards"]) for payload in payloads] == [200, 1]
            entries = [entry for payload in payloads for entry in payload["cards"]]
            assert sum(len(entry[f"{operation}_history"]) for entry in entries) == 601
            assert len(entries[0][f"{operation}_history"]) == 401
            send = client.batch
            sent = []

            def lose_response(profile_id, payload):
                sent.append(payload)
                receipt = send(profile_id, payload)
                if len(sent) == 2:
                    raise ConnectionError("Lost second accepted history batch response")
                return receipt

            client.batch = lose_response
            try:
                changes.upload()
            except ConnectionError:
                changes = SyncChanges(root / "sync.sqlite3", client)
                changes.upload()
            else:
                raise AssertionError("Expected the second batch response to be lost")
            finally:
                client.batch = send
            assert len(sent) == 3 and sent[1] == sent[2]
            changes.apply(collection)
            with SyncState(root / "sync.sqlite3") as state:
                assert state.get("changes") is None
                checkpoints = state.checkpoint_digests("source_history")
                assert len(checkpoints) == 601
                assert all((digest == "deleted") == (operation == "removed") for digest in checkpoints.values())
            assert source_rows(collection, "revlog") == ([] if operation == "removed" else history)
            changes.capture(collection)
            with SyncState(root / "sync.sqlite3") as state:
                assert state.get("changes")["keys"] == []
            changes.upload()
            changes.apply(collection)
            emit({"stage": operation})
            assert json.loads(sys.stdin.readline())["continue"]
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
