"""Measure selected-deck capture on a disposable backup of an authorized profile copy."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from time import monotonic

import support
from anki.collection import Collection
from shirabe_addon.sync_client import SyncClient
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState


def run(source):
    scratch = Path(__file__).resolve().parents[2] / "tmp"
    source = source.resolve(strict=True)
    if not source.is_relative_to(scratch) or source.name != "collection.anki2":
        raise ValueError("Use an authorized collection copy under the workspace's tmp directory")
    with source.open("rb") as stream:
        before = hashlib.file_digest(stream, "sha256").hexdigest()
    with tempfile.TemporaryDirectory(prefix="anki-sync-capture-", dir=scratch) as directory:
        root = Path(directory)
        origin = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(root / "collection.anki2")
        try:
            origin.backup(destination)
        finally:
            destination.close()
            origin.close()
        collection = Collection(str(root / "collection.anki2"))
        try:
            decks = [item.id for item in collection.decks.all_names_and_ids()
                     if not collection.decks.is_filtered(item.id)]
            snapshot = SyncSnapshot(root / "sync.sqlite3", SyncClient("http://127.0.0.1:1"))
            started = monotonic()
            captured = snapshot.capture(collection, decks)
            elapsed = monotonic() - started
            counts = {kind: 0 for kind in ("note_types", "notes", "cards", "history")}
            maximum = 0
            with SyncState(root / "sync.sqlite3") as state:
                keys = state.snapshot_batch_keys()
                for key in keys:
                    payload = state.batch(key)["payload"]
                    maximum = max(maximum, len(json.dumps(payload).encode()))
                    for kind in counts:
                        counts[kind] += len(payload.get(kind, []))
            assert counts["notes"] == collection.note_count()
            assert counts["cards"] == collection.card_count()
            assert counts["history"] == collection.db.scalar("SELECT count(*) FROM revlog WHERE cid IN (SELECT id FROM cards)")
            assert maximum <= snapshot.MAX_BYTES
            result = {"counts": counts, "batches": len(keys), "max_wire_bytes": maximum,
                      "capture_seconds": elapsed, "sidecar_bytes": (root / "sync.sqlite3").stat().st_size,
                      "selected_decks": len(captured["deck_ids"]), "unselected_siblings": captured["unselected_siblings"]}
        finally:
            collection.close()
    with source.open("rb") as stream:
        assert before == hashlib.file_digest(stream, "sha256").hexdigest(), "The source copy changed"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-copy", required=True, type=Path)
    print(json.dumps(run(parser.parse_args().source_copy)), flush=True)
