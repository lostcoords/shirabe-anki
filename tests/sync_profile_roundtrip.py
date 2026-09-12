"""Run full-profile HTTP sync on a disposable backup, never the user's collection."""

from __future__ import annotations

import hashlib
import cProfile
import io
import json
from pathlib import Path
import sqlite3
import sys
import pstats
from time import monotonic

import support
from anki.collection import Collection
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from shirabe_addon.sync_reviews import CARD_COLUMNS, REVLOG_COLUMNS
from sync_http_roundtrip import authorize, emit


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def counts(collection):
    return {"notes": collection.note_count(), "cards": collection.card_count(),
            "note_types": collection.db.scalar("SELECT count(DISTINCT mid) FROM notes"),
            "history": collection.db.scalar("SELECT count(*) FROM revlog WHERE cid IN (SELECT id FROM cards)")}


def samples(collection):
    return [{"id": note.id, "type_id": note.mid, "guid": note.guid,
             "fields_digest": hashlib.sha256("\x1f".join(note.fields).encode()).hexdigest()}
            for source_id in collection.db.list("SELECT min(id) FROM notes GROUP BY mid ORDER BY mid")
            for note in [collection.get_note(source_id)]]


def fingerprints(collection):
    def fingerprint(rows):
        result = hashlib.sha256()
        for row in rows:
            result.update((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        return result.hexdigest()

    notes = SyncSnapshot._rows(collection, "notes", "1 = 1", ())
    cards = SyncSnapshot._rows(collection, "cards", "1 = 1", ())
    history = SyncSnapshot._rows(collection, "revlog", "cid IN (SELECT id FROM cards)", ())
    return {"notes": fingerprint([row["id"], row["guid"], row["mid"], row["flds"].split("\x1f"), sorted(row["tags"].split())] for row in notes),
            "cards": fingerprint([row[column] for column in CARD_COLUMNS] for row in cards),
            "history": fingerprint([row[column] for column in REVLOG_COLUMNS] for row in history)}


def run(config):
    scratch = Path(__file__).resolve().parents[2] / "tmp"
    source = Path(config["source_copy"]).resolve(strict=True)
    root = Path(config["directory"]).resolve(strict=True)
    destination = root / "collection.anki2"
    if not source.is_relative_to(scratch) or source.name != "collection.anki2" or not root.is_relative_to(scratch) or destination.exists():
        raise ValueError("Use an authorized source copy and a fresh destination under workspace tmp")
    original_digest = digest(source)
    origin = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        origin.backup(target)
    finally:
        target.close()
        origin.close()
    collection = Collection(str(destination))
    path = root / "sync.sqlite3"
    client = SyncLogin(path, config["origin"])
    decode = client._decode

    def decode_response(response):
        body = decode(response)
        if response.status >= 400:
            print(json.dumps({"status": response.status, "code": body.get("code"), "detail": body.get("detail")}), file=sys.stderr, flush=True)
        return body

    client._decode = decode_response
    measurements = {}
    try:
        authorize(client)
        selected = [item.id for item in collection.decks.all_names_and_ids() if not collection.decks.is_filtered(item.id)]
        snapshot = SyncSnapshot(path, client)
        start = monotonic()
        captured = snapshot.capture(collection, selected)
        measurements["capture_seconds"] = monotonic() - start

        def progress(phase):
            def report(done, total):
                if done % 10 == 0 or done == total:
                    emit({"stage": "progress", "phase": phase, "done": done, "total": total})
            return report

        start = monotonic()
        profile = snapshot.upload(progress=progress("initial_upload"))
        measurements["upload_seconds"] = monotonic() - start
        changes = SyncChanges(path, client)
        start = monotonic()
        cycle = changes.capture(collection)
        measurements["unchanged_scan_seconds"] = monotonic() - start
        assert not cycle["keys"], "Initial upload did not establish a stable source baseline"
        changes.upload()
        changes.apply(collection)
        if config.get("profile_scan"):
            profiler = cProfile.Profile()
            with profiler:
                assert not changes.capture(collection)["keys"]
            report = io.StringIO()
            pstats.Stats(profiler, stream=report).sort_stats("cumtime").print_stats(20)
            measurements["scan_profile"] = report.getvalue()
            changes.upload()
            changes.apply(collection)
        initial = counts(collection)
        representatives = samples(collection)
        review_samples = [{"card_id": card_id, "type_id": type_id} for type_id, card_id in collection.db.all(
            "SELECT n.mid, min(c.id) FROM cards c JOIN notes n ON n.id = c.nid "
            "WHERE c.queue >= 0 AND c.odid = 0 GROUP BY n.mid ORDER BY n.mid")]
        emit({"stage": "profile_uploaded", "counts": initial, "decks": len(captured["deck_ids"]), "samples": representatives,
              "review_samples": review_samples, "fingerprints": fingerprints(collection)})
        assert json.loads(sys.stdin.readline())["continue"]
        delivery = SyncDelivery(path, client, profile_id=profile["id"], key=captured["pair_request"]["key"])

        def receive_all():
            while True:
                page = delivery.receive()
                delivery.apply(collection)
                delivery.acknowledge()
                if not page["changes"]:
                    return

        receive_all()
        for item in representatives:
            note = collection.get_note(item["id"])
            assert "shirabe-profile-probe" in note.tags
            note.tags.append("anki-profile-probe")
            note.fields[-1] += "<!-- shirabe sync profile probe -->"
            collection.update_note(note)
        start = monotonic()
        changes.capture(collection)
        changes.upload(progress=progress("incremental_upload"))
        changes.apply(collection)
        receive_all()
        measurements["incremental_seconds"] = monotonic() - start
        assert counts(collection) == initial
        assert not changes.capture(collection)["keys"], "The reconciled collection queued another upload"
        changes.upload()
        changes.apply(collection)
        emit({"stage": "profile_fields_reconciled", "counts": counts(collection), "samples": samples(collection),
              "fingerprints": fingerprints(collection)})
        command = json.loads(sys.stdin.readline())
        assert command["continue"]
        expected_reviews = command["reviews"]
        assert {row["card_id"] for row in expected_reviews} == {row["card_id"] for row in review_samples}
        previous_history = {row["card_id"]: set(collection.db.list("SELECT id FROM revlog WHERE cid = ?", row["card_id"]))
                            for row in expected_reviews}
        receive_all()
        reviewed = []
        for expected in expected_reviews:
            card_id = expected["card_id"]
            card = collection.get_card(card_id)
            assert card.reps == expected["reps"], (card_id, card.reps, expected["reps"])
            delivered_history = set(collection.db.list("SELECT id FROM revlog WHERE cid = ?", card_id))
            assert len(delivered_history - previous_history[card_id]) == 1
            assert previous_history[card_id] <= delivered_history
            card.start_timer()
            collection.sched.answerCard(card, 3)
            native_history = set(collection.db.list("SELECT id FROM revlog WHERE cid = ?", card_id))
            assert len(native_history - delivered_history) == 1
            assert delivered_history <= native_history
            reviewed.append({"card_id": card_id, "delivered_history_id": (delivered_history - previous_history[card_id]).pop(),
                             "native_history_id": (native_history - delivered_history).pop(),
                             "reps": collection.get_card(card_id).reps})
        start = monotonic()
        changes.capture(collection)
        changes.upload(progress=progress("review_upload"))
        changes.apply(collection)
        receive_all()
        measurements["review_roundtrip_seconds"] = monotonic() - start
        assert counts(collection) == {**initial, "history": initial["history"] + 2 * len(expected_reviews)}
        assert not changes.capture(collection)["keys"], "Reconciled reviews queued another upload"
        changes.upload()
        changes.apply(collection)
        with SyncState(path) as state:
            pending = len(state.pending())
        client.sign_out()
        assert digest(source) == original_digest, "The authorized source copy changed"
        emit({"stage": "profile_roundtrip", "counts": counts(collection), "samples": samples(collection),
              "reviewed": reviewed,
              "edited_types": len(representatives), "pending_batches": pending, "source_unchanged": True,
              "measurements": measurements, "sidecar_bytes": path.stat().st_size, "fingerprints": fingerprints(collection)})
    finally:
        collection.close()
        assert digest(source) == original_digest, "The authorized source copy changed"


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
