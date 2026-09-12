"""Pair through browser consent, then exchange browser and native reviews."""

import json
import sys

from sync_http_roundtrip import emit


def exercise(mw, session, dialog, wait_until, note_id, card_id):
    from shirabe_addon.sync_state import SyncState

    assert session.run(lambda col: col.set_config("fsrs", False), lambda _: None, mutating=True)
    wait_until(lambda: not session.busy)
    dialog.sync.click()
    wait_until(lambda: not session.busy, allow_sync_error=True)
    assert session.status.startswith("Sync paused: Enable FSRS"), session.status
    assert mw.col.get_card(card_id).reps == 0
    assert mw.col.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 0
    assert mw.col.get_config("fsrs") is False
    assert session.run(lambda col: col.set_config("fsrs", True), lambda _: None, mutating=True)
    wait_until(lambda: not session.busy)
    dialog.sync.click()
    wait_until(lambda: not session.busy)
    assert session.status == "Saved changes exchanged."
    assert mw.col.get_card(card_id).reps == 1
    assert mw.col.db.scalar("select count(*) from revlog where cid = ?", card_id) == 1
    deck_id = mw.col.get_card(card_id).did
    assert mw.col.decks.get(deck_id)["newToday"] == [mw.col.sched.today, 1]

    def review_and_edit(col):
        preset = col.decks.config_dict_for_deck_id(deck_id)
        preset["new"]["perDay"] = 7
        col.decks.update_config(preset)
        note = col.get_note(note_id)
        note.fields[1] = "feline edited in Anki"
        col.update_note(note)
        card = col.get_card(card_id)
        card.start_timer()
        return col.sched.answerCard(card, 3)

    assert session.run(review_and_edit, lambda _: None, mutating=True)
    wait_until(lambda: not session.busy)
    assert mw.col.get_card(card_id).reps == 2
    dialog.sync.click()
    wait_until(lambda: not session.busy)
    assert session.status == "Saved changes exchanged."
    with SyncState(session.path) as state:
        assert not state.pending()
        assert state.get("delivery") is None and state.get("changes") is None
        assert state.get("collection_settings") is None
        revision = state.get("collection_revision")
        cursor = state.get("delivery_cursor")
    counts = mw.col.decks.get(deck_id)
    assert counts["newToday"] == [mw.col.sched.today, 1]
    assert counts["timeToday"][1] == mw.col.db.scalar("SELECT sum(time) FROM revlog WHERE cid = ?", card_id)
    emit({"stage": "pairing_reviewed", "fsrs_pause_verified": True, "reps": mw.col.get_card(card_id).reps,
          "history": mw.col.db.scalar("select count(*) from revlog where cid = ?", card_id),
          "new_studied": counts["newToday"][1], "collection_revision": revision,
          "acknowledged_cursor": cursor, "deck_id": deck_id, "counters": {
              field: counts[field] for field in ("newToday", "revToday", "timeToday")}})
    assert json.loads(sys.stdin.readline())["continue"]


if __name__ == "__main__":
    from sync_session_roundtrip import run

    run({**json.loads(sys.stdin.readline()), "browser_pairing": True})
