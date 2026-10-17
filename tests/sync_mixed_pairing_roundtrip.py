from paths import SCRATCH
"""Review copied Japanese cards in the browser while their native collection is closed."""

import hashlib
import json
from pathlib import Path
import sys

from sync_http_roundtrip import emit


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def prepare(collection, filename):
    from anki.import_export_pb2 import ImportAnkiPackageRequest

    package = Path(filename).resolve(strict=True)
    scratch = SCRATCH
    if not package.is_relative_to(scratch) or package.suffix != ".apkg":
        raise ValueError("Use an authorized trial package under workspace tmp")
    before = digest(package)
    collection.import_anki_package(ImportAnkiPackageRequest(package_path=str(package)))
    assert digest(package) == before
    assert collection.note_count() == 37 and collection.card_count() == 38
    assert collection.db.scalar("SELECT count(*) FROM revlog") == 0
    selected = collection.db.list("SELECT DISTINCT did FROM cards ORDER BY did")
    assert all(not collection.decks.is_filtered(deck) for deck in selected)
    for deck_id in selected:
        deck = collection.decks.get(deck_id)
        deck.update(newLimit=100, newLimitToday=None)
        collection.decks.update(deck)
    return collection.get_note(collection.db.scalar("SELECT min(id) FROM notes")), selected


def exercise(mw, session, dialog, wait_until, config):
    from anki.collection import Collection
    from shirabe_addon.sync_state import SyncState
    from sync_profile_roundtrip import fingerprints

    package = Path(config["package"]).resolve(strict=True)
    before = digest(package)
    collection_path = mw.col.path
    card_ids = mw.col.db.list("SELECT id FROM cards ORDER BY id")
    media = {file.name: digest(file) for file in Path(mw.col.media.dir()).iterdir()
             if file.is_file() and not file.name.startswith(".")}
    initial = fingerprints(mw.col)
    mw.col.close()
    emit({"stage": "mixed_uploaded", "collection_closed": True, "card_ids": card_ids,
          "fingerprints": initial, "media": media})
    try:
        assert json.loads(sys.stdin.readline())["continue"]
    finally:
        mw.col = Collection(collection_path)
    assert mw.col.db.scalar("SELECT count(*) FROM revlog") == 0
    dialog.sync.click()
    wait_until(lambda: not session.busy, timeout=60)
    assert session.status == "Saved changes exchanged."
    delivered = mw.col.db.all("SELECT cid, id FROM revlog ORDER BY cid")
    assert [row[0] for row in delivered] == card_ids
    assert all(mw.col.get_card(card_id).reps == 1 for card_id in card_ids)

    def native_reviews(col):
        for card_id in card_ids:
            card = col.get_card(card_id)
            card.start_timer()
            changes = col.sched.answerCard(card, 3)
        return changes

    assert session.run(native_reviews, lambda _: None, mutating=True)
    wait_until(lambda: not session.busy, timeout=60)
    assert all(mw.col.get_card(card_id).reps == 2 for card_id in card_ids)
    dialog.sync.click()
    wait_until(lambda: not session.busy, timeout=60)
    assert session.status == "Saved changes exchanged."
    with SyncState(session.path) as state:
        assert not state.pending()
        assert state.get("delivery") is None and state.get("changes") is None
    assert {file.name: digest(file) for file in Path(mw.col.media.dir()).iterdir()
            if file.is_file() and not file.name.startswith(".")} == media
    assert digest(package) == before
    emit({"stage": "mixed_reviewed", "fingerprints": fingerprints(mw.col),
          "delivered": delivered, "history": mw.col.db.scalar("SELECT count(*) FROM revlog"),
          "source_unchanged": True})
    assert json.loads(sys.stdin.readline())["continue"]


if __name__ == "__main__":
    from sync_session_roundtrip import run

    run({**json.loads(sys.stdin.readline()), "browser_pairing": True, "mixed_pairing": True})
