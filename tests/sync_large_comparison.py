"""Resolve a source deletion using a large, retried Rails comparison."""

import json
from pathlib import Path
import sys

import support
from anki.buildinfo import version
from anki.collection import Collection
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from sync_http_roundtrip import authorize, emit


def run(config):
    assert version == "26.08.1"
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck_id = collection.decks.id("Large comparison")
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck_id)
        card_id = note.cards()[0].id
        sidecar = root / "sync.sqlite3"
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        snapshot.capture(collection, [deck_id])
        snapshot.upload()
        emit({"stage": "snapshot_uploaded", "card_id": card_id})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.remove_notes([note.id])
        changes = SyncChanges(sidecar, client)
        changes.capture(collection)
        changes.upload()
        try:
            changes.apply(collection)
        except SyncConflict as error:
            assert len(error.conflicts) == 1
            problem = error.conflicts[0]
            assert problem["code"] == "note_changed_before_deletion"
        else:
            raise AssertionError("Expected changed fields to require deletion confirmation")
        request = client._profile_request
        reads = []

        def lose_part(profile_id, path, payload, **options):
            result = request(profile_id, path, payload, **options)
            if "/comparisons/" in path:
                reads.append(path)
                assert len(json.dumps(result).encode()) <= client.MAX_RESPONSE_BYTES
                if len(reads) == 1:
                    raise ConnectionError("Lost an intermediate comparison part")
            return result

        client._profile_request = lose_part
        try:
            changes.card_conflict(problem)
        except ConnectionError:
            detail = changes.card_conflict(problem)
        else:
            raise AssertionError("Expected an interrupted comparison")
        finally:
            client._profile_request = request
        assert reads[0] == reads[1]
        assert detail["deletion"]["note"]["fields"] == ["<" * 1048576, ">" * 1048576]
        emit({"stage": "comparison_fetched", "part_reads": len(reads)})
        assert json.loads(sys.stdin.readline())["continue"]
        prepared = changes.prepare_deletion_resolution(collection, problem, detail)
        changes.resolve_deletion(collection, prepared)
        send = client.batch
        attempts = []

        def lose_acceptance(profile_id, payload):
            attempts.append(payload)
            result = send(profile_id, payload)
            if len(attempts) == 1:
                raise ConnectionError("Lost the accepted deletion response")
            return result

        client.batch = lose_acceptance
        try:
            changes.upload()
        except ConnectionError:
            changes.upload()
        else:
            raise AssertionError("Expected an interrupted deletion receipt")
        finally:
            client.batch = send
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        changes.apply(collection)
        assert collection.db.scalar("SELECT count(*) FROM cards") == 0
        assert collection.db.scalar("SELECT count(*) FROM notes") == 0
        emit({"stage": "deletion_reconciled"})
        assert json.loads(sys.stdin.readline())["continue"]
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
