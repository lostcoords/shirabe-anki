"""Resume an initial upload through the existing native note chooser."""

import json
import sys

from sync_http_roundtrip import emit


def exercise(mw, session, dialog, wait_until, note_id, other_id):
    from shirabe_addon.sync_state import SyncState
    from shirabe_addon.sync_ui import StudySyncDialog

    assert session.error and len(session.conflicts) == 1, session.conflicts
    problem = session.conflicts[0]
    assert problem["kind"] == "notes" and problem["id"] == note_id
    assert not dialog.resolve.isHidden() and not dialog.sync.isEnabled()
    with SyncState(session.path) as state:
        assert state.get("initial_snapshot")["phase"] == "resolving"
        original = state.batch(problem["request_key"])
    dialog.conflicts.setCurrentRow(0)
    dialog.resolve.click()
    wait_until(lambda: hasattr(dialog, "note_resolution") and not session.busy)
    dialog.note_resolution.reject()
    assert mw.col.get_note(note_id).fields == ["猫", "cat"]
    dialog.conflicts.setCurrentRow(0)
    dialog.resolve.click()
    wait_until(lambda: dialog.note_resolution.isVisible() and not session.busy)
    chooser = dialog.note_resolution
    chooser.fields[1].setPlainText("linked meaning chosen in Anki")
    chooser.tags.setText("linked")
    requests = []
    batch = session.login.batch

    def lose_resolution(profile_id, payload):
        result = batch(profile_id, payload)
        if any(row.get("conflict_id") for row in payload.get("notes", [])):
            requests.append(payload)
            if len(requests) == 1:
                raise ConnectionError("Lost initial resolution response")
        return result

    session.login.batch = lose_resolution
    chooser.accept()
    wait_until(lambda: not session.busy, allow_sync_error=True)
    assert session.error == "Lost initial resolution response", session.error
    assert mw.col.get_note(note_id).fields == ["猫", "cat"]
    with SyncState(session.path) as state:
        assert state.batch(problem["request_key"]) == original
    dialog.reject()
    dialog = StudySyncDialog(mw, session.login.api.removesuffix("/api/v1"))
    wait_until(lambda: not session.busy, allow_sync_error=True)
    assert dialog.upload.isEnabled() and dialog.upload.text() == "Resume initial upload"
    dialog.upload.click()
    wait_until(lambda: not session.busy)
    assert session.status == "Saved changes exchanged."
    assert len(requests) == 2 and requests[0] == requests[1]
    assert mw.col.get_note(note_id).fields == ["猫", "linked meaning chosen in Anki"]
    assert mw.col.get_note(other_id).fields == ["犬", "dog"]
    with SyncState(session.path) as state:
        assert state.get("initial_snapshot")["phase"] == "uploaded"
        assert state.get("changes") is None
        assert state.checkpoint("source_note", note_id)["payload"]["flds"] == "猫\x1flinked meaning chosen in Anki"
        assert not state.pending()
    emit({"stage": "initial_resolved", "note_id": note_id, "other_id": other_id,
          "original_key": problem["request_key"], "original_receipt": original["receipt"]})
    assert json.loads(sys.stdin.readline())["continue"]
    dialog.reject()


if __name__ == "__main__":
    from sync_session_roundtrip import run

    run({**json.loads(sys.stdin.readline()), "initial_resolution": True})
