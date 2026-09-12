"""Compare both reconnect orders from byte-identical disposable Anki collections."""

import hashlib
import json
from pathlib import Path
import shutil
import sys

from anki.collection import Collection
from anki.scheduler.v3 import CardAnswer
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_state import SyncState
from study_corpus import snapshot
from sync_http_roundtrip import emit, source_rows


def prepare(config, destination):
    seed = Path(config["offline_seed"])
    if not seed.exists():
        collection = Collection(str(seed))
        try:
            collection.set_config("fsrs", True)
            deck = collection.decks.id("Offline order")
            for front in ("猫", "犬"):
                note = collection.new_note(collection.models.by_name("Basic"))
                note.fields = [front, "Original meaning"]
                note.tags = ["original"]
                collection.add_note(note, deck)
            collection.media.write_data("_offline.css", b".card { color: black; }")
        finally:
            collection.close()
    shutil.copyfile(seed, destination)
    shutil.copytree(seed.with_suffix(".media"), destination.with_suffix(".media"))


def exercise(mw, session, wait_until, config):
    seed = Path(config["offline_seed"])
    seed_digest = hashlib.sha256(seed.read_bytes()).hexdigest()
    for index, row in enumerate(source_rows(mw.col, "notes")):
        note = mw.col.get_note(row["id"])
        note.fields[0] = "Anki edited " + note.fields[0]
        note.tags.append("anki")
        mw.col.update_note(note)
        card = note.cards()[0]
        card.start_timer()
        states = mw.col._backend.get_scheduling_states(card.id)
        answer = mw.col.sched.build_answer(card=card, states=states, rating=CardAnswer.EASY)
        answer.answered_at_millis = config["answered_at"] + index
        answer.milliseconds_taken = 1200
        mw.col.sched.answer_card(answer)
    offline = {"stage": "offline_edits", "notes": source_rows(mw.col, "notes"),
               "history": source_rows(mw.col, "revlog"), "seed_digest": seed_digest}
    path = mw.col.path
    mw.col.close()
    mw.col = None
    emit(offline)
    assert json.loads(sys.stdin.readline())["continue"]
    mw.col = Collection(path)
    if config["offline_order"] == "pull_first":
        with SyncState(session.path) as state:
            saved = state.get("initial_snapshot")
        delivery = SyncDelivery(session.path, session.login, profile_id=saved["profile"]["id"], key=saved["pair_request"]["key"])
        page = delivery.receive()
        assert any(change["entity_kind"] == "review" for change in page["changes"])
    assert session.start()
    wait_until(lambda: not session.busy, allow_sync_error=True)
    assert not session.error, (config["offline_order"], session.error, session.conflicts)
    assert session.start()
    wait_until(lambda: not session.busy)
    assert not session.error, session.conflicts
    with SyncState(session.path) as state:
        assert state.get("changes") is None and state.get("delivery") is None
    result = snapshot(mw.col)
    assert len(result["reviews"]) == 4
    assert hashlib.sha256(seed.read_bytes()).hexdigest() == seed_digest
    emit({"stage": "offline_reconciled", "graph": result, "seed_digest": seed_digest})
