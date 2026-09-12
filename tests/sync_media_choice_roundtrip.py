"""Confirmed media choices through real Qt operations and Rails HTTP."""

import json
from pathlib import Path
import sys

import support
from sync_http_roundtrip import emit


def exercise(mw, session, dialog, wait_until):
    from anki.collection import OpChanges
    from aqt.qt import QDialogButtonBox

    filename = "_sync-incoming.wav"
    media_directory = Path(mw.col.media.dir())

    def local(data, name=filename):
        def change(col):
            col.media.trash_files([name])
            if data is not None:
                col.media.write_data(name, data)
            return OpChanges(note=True)

        assert session.run(change, lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)

    def paused(name=filename):
        dialog.sync.click()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert session.error and any(row.get("filename") == name for row in session.conflicts), session.error

    def open_choice(name=filename):
        index = next(index for index, row in enumerate(session.conflicts) if row.get("filename") == name)
        dialog.conflicts.setCurrentRow(index)
        previous = getattr(dialog, "media_resolution", None)
        assert dialog.resolve.isEnabled()
        dialog.resolve.click()
        wait_until(lambda: getattr(dialog, "media_resolution", None) is not previous and not session.busy)
        chooser = dialog.media_resolution
        assert not chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        return chooser

    def confirm(chooser, side, *, fails=False):
        chooser.choice.setCurrentIndex(chooser.choice.findData(side))
        chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        wait_until(lambda: not session.busy, allow_sync_error=fails)
        assert bool(session.error) == fails, session.error

    local(b"Anki conflict")
    paused()
    chooser = open_choice()
    chooser.reject()
    assert (media_directory / filename).read_bytes() == b"Anki conflict"
    chooser = open_choice()
    local(b"later Anki while choosing")
    confirm(chooser, "shirabe", fails=True)
    assert (media_directory / filename).read_bytes() == b"later Anki while choosing"
    confirm(open_choice(), "anki")
    emit({"stage": "media_anki_kept"})
    assert json.loads(sys.stdin.readline())["continue"]

    local(b"second Anki conflict")
    paused()
    confirm(open_choice(), "shirabe")
    assert (media_directory / filename).read_bytes() == b"second Shirabe conflict"
    emit({"stage": "media_shirabe_kept"})
    assert json.loads(sys.stdin.readline())["continue"]

    local(None)
    paused()
    confirm(open_choice(), "shirabe")
    assert (media_directory / filename).read_bytes() == b"second Shirabe conflict"
    local(None)
    paused()
    original = session.login.delete_media

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("Lost confirmed deletion response")

    session.login.delete_media = lose_response
    try:
        confirm(open_choice(), "anki", fails=True)
    finally:
        session.login.delete_media = original
    assert not (media_directory / filename).exists()
    emit({"stage": "media_deletion_response_lost"})
    assert json.loads(sys.stdin.readline())["continue"]
    dialog.sync.click()
    wait_until(lambda: not session.busy)
    assert (media_directory / filename).read_bytes() == b"restored after lost receipt"
    local(None, "_sync-delete.wav")
    paused("_sync-delete.wav")
    confirm(open_choice("_sync-delete.wav"), "anki")
    assert not (media_directory / "_sync-delete.wav").exists()
    assert session.status == "Saved changes exchanged."
    assert not session.conflicts
    emit({"stage": "media_choices_finished"})


if __name__ == "__main__":
    from sync_session_roundtrip import run

    run({**json.loads(sys.stdin.readline()), "media_choices": True})
