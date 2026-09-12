"""Retry a large native upload after losing an intermediate receipt part."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import support
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import SyncDelivery
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
        deck_id = collection.decks.id("Large receipt")
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck_id)
        card_id = note.cards()[0].id
        data = json.dumps({"custom": "a" * (3 * 1024 * 1024)}, separators=(",", ":"))
        collection.db.execute("UPDATE cards SET data=? WHERE id=?", data, card_id)
        original = source_rows(collection, "cards")
        client = SyncLogin(root / "sync.sqlite3", config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(root / "sync.sqlite3", client)
        captured = snapshot.capture(collection, [deck_id])
        send = client._profile_request
        reads = []
        lost = False
        lost_delivery = False

        def lose_part(profile_id, path, payload, **options):
            nonlocal lost, lost_delivery
            result = send(profile_id, path, payload, **options)
            if "/batches/" in path:
                reads.append(path)
                assert len(json.dumps(result).encode()) < client.MAX_RESPONSE_BYTES
                if not lost:
                    lost = True
                    raise ConnectionError("Lost an intermediate receipt part")
            if "/deliveries/" in path and not lost_delivery:
                lost_delivery = True
                raise ConnectionError("Lost an intermediate delivery part")
            return result

        client._profile_request = lose_part
        try:
            profile = snapshot.upload()
        except ConnectionError:
            with SyncState(root / "sync.sqlite3") as state:
                pending = [key for key in state.snapshot_batch_keys() if not state.batch_applied(key)]
                assert len(pending) == 1
                request_key = pending[0]
                assert state.batch(request_key)["receipt"] is None
            snapshot = SyncSnapshot(root / "sync.sqlite3", client)
            profile = snapshot.upload()
        else:
            raise AssertionError("Expected the receipt transfer to be interrupted")
        assert lost and reads[0] == reads[1]
        assert source_rows(collection, "cards") == original
        with SyncState(root / "sync.sqlite3") as state:
            assert all(state.batch_applied(key) for key in state.snapshot_batch_keys())
            receipt = state.batch(request_key)["receipt"]
            result = receipt["cards"][0]
            assert result["status"] == "accepted"
            assert result["anki"]["card_before"]["data"] == data
            assert result["anki"]["card_after"]["data"] == data
        emit({"stage": "receipt_resumed", "card_id": card_id, "request_key": request_key,
              "data_sha256": hashlib.sha256(data.encode()).hexdigest(), "receipt_reads": len(reads)})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.db.execute("UPDATE cards SET flags=3 WHERE id=?", card_id)
        original[0]["flags"] = 3
        changes = SyncChanges(root / "sync.sqlite3", client)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        delivery = SyncDelivery(root / "sync.sqlite3", client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        for _ in range(2):
            try:
                delivery.receive()
            except ConnectionError:
                delivery = SyncDelivery(root / "sync.sqlite3", client, profile_id=profile["id"], key=captured["pair_request"]["key"])
                delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
        assert lost_delivery
        assert collection.get_note(note.id).fields == ["<" * (1024 * 1024), ">" * (1024 * 1024)]
        assert source_rows(collection, "cards") == original
        emit({"stage": "large_delivery_applied"})
        assert json.loads(sys.stdin.readline())["continue"]
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
