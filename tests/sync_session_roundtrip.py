"""Run the sync dialog and Anki operation queues against the Rails test server."""

import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import weakref

import support
from gui_safety import offscreen_test_application
from sync_http_roundtrip import authorize, emit


def run(config):
    if config.get("retained_trial"):
        from study_linked_trial import configuration

        verified = configuration(config["directory"])
        assert all(config.get(key) == verified[key] for key in ("origin", "package", "package_sha256"))
    app = offscreen_test_application([])
    import aqt
    from anki.buildinfo import version
    from anki.collection import Collection
    from anki.lang import set_lang
    from aqt import gui_hooks
    from aqt.operations import CollectionOp
    from aqt.progress import ProgressManager
    from aqt.qt import QDialogButtonBox, QEventLoop, QTimer, QWidget, Qt
    from aqt.taskman import TaskManager
    from shirabe_addon.sync_session import session_for
    from shirabe_addon.sync_state import SyncState
    from shirabe_addon.sync_ui import StudySyncDialog

    assert version == "26.08.1", version
    set_lang("en_US")
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert config.get("retained_trial") or not path.exists(), "This probe only creates a new disposable collection"
    if config.get("offline_order"):
        from sync_offline_orders import prepare

        prepare(config, path)
    fresh_collection = not path.exists()

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.app = app
            self.col = Collection(str(path))
            if fresh_collection:
                self.col.set_config("fsrs", True)
            self.pm = SimpleNamespace(profileFolder=lambda: str(root))
            self.state = "deckBrowser"
            self.background_ops = self.undo_updates = 0
            self.progress = ProgressManager(self)
            self.taskman = TaskManager(self)
            self.setWindowTitle("Agent Tests study sync")

        def weakref(self):
            return weakref.proxy(self)

        def _increase_background_ops(self):
            self.background_ops += 1

        def _decrease_background_ops(self):
            self.background_ops -= 1

        def update_undo_actions(self):
            self.undo_updates += 1

    errors = []
    previous_exception_hook = sys.excepthook

    def record_exception(kind, error, traceback):
        errors.append(error)
        previous_exception_hook(kind, error, traceback)

    sys.excepthook = record_exception
    mw = Window()
    previous_mw, aqt.mw = aqt.mw, mw
    session = session_for(mw, config["origin"])
    session.timer.setInterval(25)
    operations = []

    def operation_finished(changes, initiator):
        assert threading.current_thread() is threading.main_thread()
        operations.append(initiator)
        session.request(changes, initiator)

    gui_hooks.operation_did_execute.append(operation_finished)

    def wait_until(predicate, timeout=20, *, allow_sync_error=False):
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(5)
        deadline = time.monotonic() + timeout

        def check():
            if errors or (session.error and not allow_sync_error) or predicate() or time.monotonic() >= deadline:
                loop.quit()

        timer.timeout.connect(check)
        timer.start()
        loop.exec()
        timer.stop()
        assert not errors, errors
        assert allow_sync_error or not session.error, session.error
        assert predicate(), f"Timed out: {session.status}"

    def idle_for(seconds):
        until = time.monotonic() + seconds
        wait_until(lambda: time.monotonic() >= until)

    try:
        if config.get("retained_trial"):
            from study_linked_trial import exercise

            exercise(mw, session, wait_until, config)
            return
        if config.get("mixed_pairing"):
            from sync_mixed_pairing_roundtrip import prepare

            note, selected_decks = prepare(mw.col, config["package"])
        elif config.get("offline_order"):
            selected_decks = [mw.col.decks.id("Offline order")]
            note = mw.col.get_note(mw.col.find_notes("")[0])
        else:
            deck_id = mw.col.decks.id("Japanese automatic sync")
            selected_decks = [deck_id]
            note = mw.col.new_note(mw.col.models.by_name("Basic"))
            note.fields = ["猫", "cat"]
            mw.col.add_note(note, deck_id)
        card_id = note.cards()[0].id
        if not config.get("mixed_pairing") and not config.get("offline_order"):
            for name in ("_sync-incoming.wav", "_sync-outgoing.wav", "_sync-delete.wav"):
                mw.col.media.write_data(name, b"initial media")
        if config.get("initial_resolution"):
            other = mw.col.new_note(note.note_type())
            other.fields = ["犬", "dog"]
            mw.col.add_note(other, deck_id)
            batch = session.login.batch
            intercepted = False

            def initial_notes(profile_id, payload):
                nonlocal intercepted
                if payload.get("notes") and not intercepted:
                    intercepted = True
                    emit({"stage": "initial_notes_ready", "notes": payload["notes"], "note_id": note.id})
                    assert json.loads(sys.stdin.readline())["continue"]
                return batch(profile_id, payload)

            session.login.batch = initial_notes
        if not config.get("browser_pairing"):
            authorize(session.login)
        dialog = StudySyncDialog(mw, config["origin"])
        wait_until(lambda: not session.busy)
        if config.get("browser_pairing"):
            assert dialog.sign_in.isEnabled() and not dialog.upload.isEnabled()
            dialog.sign_in.click()
            wait_until(lambda: not session.busy and dialog.verification_url is not None)
            pending = session.login.pending_login()
            emit({"stage": "authorize", "user_code": pending["user_code"], "url": dialog.verification_url})
            assert json.loads(sys.stdin.readline())["continue"]
            wait_until(lambda: not session.busy and bool(session.login.credentials()), timeout=30)
            assert not dialog.timer.isActive() and dialog.verification_url is None
        for index in range(dialog.deck_list.count()):
            item = dialog.deck_list.item(index)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in selected_decks else Qt.CheckState.Unchecked)
        assert dialog.upload.isEnabled()
        dialog.upload.click()
        wait_until(lambda: not session.busy and hasattr(dialog, "snapshot_preview"))
        with SyncState(session.path) as state:
            previewed = state.get("initial_snapshot")
            assert previewed["phase"] == "captured" and previewed["profile"] is None
            assert previewed["counts"]["notes"] > 0 and previewed["counts"]["cards"] > 0
        dialog.snapshot_preview.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        assert dialog.upload.isEnabled() and dialog.upload.text() == "Resume initial upload"
        dialog.upload.click()
        wait_until(lambda: not session.busy and dialog.snapshot_preview.isVisible())
        with SyncState(session.path) as state:
            assert state.get("initial_snapshot") == previewed
        dialog.snapshot_preview.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        wait_until(lambda: not session.busy, allow_sync_error=bool(config.get("initial_resolution")))
        if config.get("initial_resolution"):
            from sync_initial_resolution_roundtrip import exercise

            exercise(mw, session, dialog, wait_until, note.id, other.id)
            session.login.sign_out()
            return
        with SyncState(session.path) as state:
            assert state.get("initial_snapshot")["phase"] == "uploaded"
        if config.get("offline_order"):
            from sync_offline_orders import exercise

            exercise(mw, session, wait_until, config)
            session.login.sign_out()
            return
        if config.get("mixed_pairing"):
            from sync_mixed_pairing_roundtrip import exercise

            exercise(mw, session, dialog, wait_until, config)
            session.login.sign_out()
            return
        initial_operations = sum(initiator is session for initiator in operations)
        emit({"stage": "session_uploaded", "note_id": note.id, "card_id": card_id})
        assert json.loads(sys.stdin.readline())["continue"]
        if config.get("browser_pairing"):
            from sync_pairing_roundtrip import exercise

            exercise(mw, session, dialog, wait_until, note.id, card_id)
            session.login.sign_out()
            return
        if config.get("media_choices"):
            from sync_media_choice_roundtrip import exercise

            exercise(mw, session, dialog, wait_until)
            session.login.sign_out()
            return

        mw.state = "review"
        dialog.automatic.setChecked(True)
        local_edit = object()

        def edit(col):
            assert threading.current_thread() is not threading.main_thread()
            col.media.trash_files(["_sync-outgoing.wav"])
            col.media.write_data("_sync-outgoing.wav", b"later Anki media")
            edited = col.get_note(note.id)
            edited.fields[0] = "ねこ from Anki operation"
            return col.update_note(edited)

        CollectionOp(mw, edit).run_in_background(initiator=local_edit)
        wait_until(lambda: local_edit in operations)
        idle_for(0.15)
        assert not session.busy
        assert mw.col.get_note(note.id).fields[1] == "cat"
        assert initial_operations == sum(initiator is session for initiator in operations)
        mw.state = "deckBrowser"
        dialog.reject()
        assert dialog.closed.is_set() and not session.closed.is_set()
        wait_until(lambda: sum(initiator is session for initiator in operations) > initial_operations
                   and session.status == "Saved changes exchanged." and not session.busy)
        assert mw.col.get_note(note.id).fields == ["ねこ from Anki operation", "feline from Shirabe"], mw.col.get_note(note.id).fields
        media = Path(mw.col.media.dir())
        assert (media / "_sync-incoming.wav").read_bytes() == b"later Shirabe media"
        assert (media / "_sync-new.png").read_bytes() == b"new Shirabe image"
        assert (media / "_sync-outgoing.wav").read_bytes() == b"later Anki media"
        assert not (media / "_sync-delete.wav").exists()
        own_operations = sum(initiator is session for initiator in operations)
        assert own_operations >= initial_operations + 2, operations
        idle_for(0.2)
        assert own_operations == sum(initiator is session for initiator in operations)
        assert not session.busy
        assert not session.timer.isActive()
        emit({"stage": "session_automatic", "own_operations": own_operations})
        assert json.loads(sys.stdin.readline())["continue"]

        dialog = StudySyncDialog(mw, config["origin"])
        wait_until(lambda: not session.busy)
        assert dialog.session is session
        assert dialog.automatic.isChecked()
        assert dialog.sync.isEnabled()
        dialog.sync.click()
        assert session.busy
        dialog.reject()
        wait_until(lambda: not session.busy)
        assert session.status == "Saved changes exchanged."
        card = mw.col.get_card(card_id)
        assert card.reps == 1
        assert mw.col.db.scalar("select count(*) from revlog where cid = ?", card_id) == 1
        with SyncState(session.path) as state:
            assert state.get("delivery") is None
            assert state.get("changes") is None
            assert not state.pending()
        idle_for(0.2)
        assert not session.busy and not session.timer.isActive()
        assert mw.background_ops == 0
        assert mw.progress.busy() == 0
        assert mw.undo_updates == len(operations)
        session.set_automatic(False)
        added = []

        def add_note(col):
            other = col.new_note(col.get_note(note.id).note_type())
            other.fields = ["犬", "dog"]
            changes = col.add_note(other, deck_id)
            added.append(other.id)
            return changes

        assert session.run(add_note, lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        assert session.start()
        wait_until(lambda: not session.busy)
        emit({"stage": "session_conflict_ready", "other_note_id": added[0]})
        assert json.loads(sys.stdin.readline())["continue"]

        def competing_edits(col):
            edited = col.get_note(note.id)
            edited.fields[1] = "competing Anki meaning"
            col.update_note(edited)
            other = col.get_note(added[0])
            other.fields[0] = "犬 edited in Anki"
            return col.update_note(other)

        assert session.run(competing_edits, lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        own_operations = sum(initiator is session for initiator in operations)
        assert session.start()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert session.error and len(session.conflicts) == 1, session.conflicts
        assert session.conflicts[0]["id"] == note.id
        assert mw.col.get_note(note.id).fields[1] == "competing Anki meaning"
        assert mw.col.get_note(added[0]).fields == ["犬 edited in Anki", "merged Shirabe meaning"]
        assert sum(initiator is session for initiator in operations) == own_operations + 1
        assert mw.undo_updates == len(operations)
        def edit_while_paused(col):
            other = col.get_note(added[0])
            other.tags.append("edited_while_paused")
            return col.update_note(other)

        assert session.run(edit_while_paused, lambda _: None, mutating=True, preserve_status=True)
        wait_until(lambda: not session.busy, allow_sync_error=True)
        dialog = StudySyncDialog(mw, config["origin"])
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert dialog.conflicts.count() == 1
        assert "1 sync record needs attention" in dialog.status.text()
        dialog.conflicts.setCurrentRow(0)
        assert dialog.resolve.isEnabled()
        dialog.resolve.click()
        wait_until(lambda: hasattr(dialog, "note_resolution"))
        chooser = dialog.note_resolution
        chooser.fields[1].setPlainText("resolved meaning")
        chooser.tags.setText("resolved_from_anki")
        emit({"stage": "session_note_choice_open"})
        assert json.loads(sys.stdin.readline())["continue"]
        chooser.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert len(session.conflicts) == 1 and session.conflicts[0]["kind"] == "notes"
        dialog.conflicts.setCurrentRow(0)
        dialog.resolve.click()
        wait_until(lambda: dialog.note_resolution is not chooser and not session.busy)
        chooser = dialog.note_resolution
        chooser.fields[1].setPlainText("resolved meaning")
        chooser.tags.setText("resolved_from_anki")
        send_batch = session.login.batch
        resolution_requests = []

        def lose_resolution_response(profile_id, payload):
            receipt = send_batch(profile_id, payload)
            if any("conflict_id" in row for row in payload.get("notes", [])):
                resolution_requests.append(payload)
                if len(resolution_requests) == 1:
                    raise ConnectionError("Interrupted after the resolution reached Shirabe")
            return receipt

        session.login.batch = lose_resolution_response
        chooser.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert "Interrupted after the resolution reached Shirabe" in session.error
        assert mw.col.get_note(note.id).fields[1] == "competing Anki meaning"
        assert dialog.sync.isEnabled()
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        session.login.batch = send_batch
        assert len(resolution_requests) == 2 and resolution_requests[0] == resolution_requests[1]
        assert session.status == "Saved changes exchanged."
        assert not session.conflicts
        assert mw.col.get_note(note.id).fields[1] == "resolved meaning"
        assert mw.col.get_note(note.id).tags == ["resolved_from_anki"]
        assert "edited_while_paused" in mw.col.get_note(added[0]).tags
        with SyncState(session.path) as state:
            assert state.get("changes") is None
            assert state.get("delivery") is None
        acknowledging, continue_acknowledgment = threading.Event(), threading.Event()
        acknowledge = session.login.acknowledge

        def delayed_acknowledgment(*args, **kwargs):
            if not acknowledging.is_set():
                acknowledging.set()
                assert continue_acknowledgment.wait(10)
            return acknowledge(*args, **kwargs)

        session.login.acknowledge = delayed_acknowledgment
        assert not session.automatic
        assert session.start()
        try:
            wait_until(acknowledging.is_set)
            editing = object()

            def edit_during_manual_sync(col):
                other = col.get_note(added[0])
                other.tags.append("edited_during_manual_sync")
                return col.update_note(other)

            CollectionOp(mw, edit_during_manual_sync).run_in_background(initiator=editing)
            wait_until(lambda: editing in operations)
            assert session.busy and not session.timer.isActive()
        finally:
            continue_acknowledgment.set()
        wait_until(lambda: not session.busy)
        session.login.acknowledge = acknowledge
        assert "edited_during_manual_sync" in mw.col.get_note(added[0]).tags
        assert not session.automatic and not session.timer.isActive()
        emit({"stage": "session_card_conflict_ready"})
        assert json.loads(sys.stdin.readline())["continue"]

        def change_flag(col):
            current = col.get_card(card_id)
            current.flags = 3
            return col.update_card(current)

        assert session.run(change_flag, lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        assert session.start()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert len(session.conflicts) == 1 and session.conflicts[0]["kind"] == "cards", session.conflicts
        dialog.conflicts.setCurrentRow(0)
        assert dialog.resolve.isEnabled()
        dialog.resolve.click()
        wait_until(lambda: hasattr(dialog, "card_resolution") and not session.busy)
        chooser = dialog.card_resolution
        emit({"stage": "session_card_choice_open"})
        assert json.loads(sys.stdin.readline())["continue"]
        chooser.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert len(session.conflicts) == 1 and session.conflicts[0]["kind"] == "cards"
        dialog.conflicts.setCurrentRow(0)
        dialog.resolve.click()
        wait_until(lambda: dialog.card_resolution is not chooser and not session.busy)
        chooser = dialog.card_resolution
        chooser.choices["properties"].setCurrentIndex(1)
        resolution_requests = []

        def lose_card_resolution(profile_id, payload):
            response = send_batch(profile_id, payload)
            if any(row.get("conflict_id") for row in payload.get("cards", [])):
                resolution_requests.append(payload)
                if len(resolution_requests) == 1:
                    raise ConnectionError("Lost accepted card-resolution response")
            return response

        session.login.batch = lose_card_resolution
        chooser.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert session.error and "Lost accepted card-resolution" in session.error, session.error
        assert mw.col.get_card(card_id).flags == 3
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        session.login.batch = send_batch
        assert len(resolution_requests) == 2 and resolution_requests[0] == resolution_requests[1]
        assert mw.col.get_card(card_id).flags == 4
        assert mw.col.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 1
        assert not session.conflicts and session.status == "Saved changes exchanged."
        emit({"stage": "session_incoming_ready"})
        assert json.loads(sys.stdin.readline())["continue"]
        received, continue_delivery = threading.Event(), threading.Event()
        deliver = session.login.deliver

        def delayed_delivery(*args, **kwargs):
            page = deliver(*args, **kwargs)
            if not received.is_set():
                assert page["changes"]
                received.set()
                assert continue_delivery.wait(10)
            return page

        session.login.deliver = delayed_delivery
        assert session.start()
        try:
            wait_until(received.is_set)
            editing = object()

            def edit_before_delivery(col):
                edited = col.get_note(note.id)
                edited.fields[1] = "incoming Anki meaning"
                return col.update_note(edited)

            CollectionOp(mw, edit_before_delivery).run_in_background(initiator=editing)
            wait_until(lambda: editing in operations)
        finally:
            continue_delivery.set()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        session.login.deliver = deliver
        assert len(session.conflicts) == 1 and session.conflicts[0]["direction"] == "incoming", session.conflicts
        assert mw.col.get_note(note.id).fields[1] == "incoming Anki meaning"
        assert mw.col.get_note(added[0]).fields[1] == "incoming independent meaning"
        assert mw.undo_updates == len(operations)
        previous_chooser = dialog.note_resolution
        dialog.conflicts.setCurrentRow(0)
        assert dialog.resolve.isEnabled()
        dialog.resolve.click()
        wait_until(lambda: dialog.note_resolution is not previous_chooser and not session.busy)
        chooser = dialog.note_resolution
        chooser.fields[1].setPlainText("resolved incoming meaning")
        acknowledgments = []

        def lose_incoming_acknowledgment(*args, **kwargs):
            response = acknowledge(*args, **kwargs)
            acknowledgments.append((args, kwargs))
            if len(acknowledgments) == 1:
                raise ConnectionError("Lost incoming-choice acknowledgment")
            return response

        session.login.acknowledge = lose_incoming_acknowledgment
        chooser.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert session.error and "Lost incoming-choice acknowledgment" in session.error, session.error
        assert mw.col.get_note(note.id).fields[1] == "resolved incoming meaning"

        def edit_after_ack(col):
            edited = col.get_note(note.id)
            edited.fields[0] = "猫 after incoming ack"
            return col.update_note(edited)

        assert session.run(edit_after_ack, lambda _: None, mutating=True, preserve_status=True)
        wait_until(lambda: not session.busy, allow_sync_error=True)
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        session.login.acknowledge = acknowledge
        assert acknowledgments[0] == acknowledgments[1]
        assert mw.col.get_note(note.id).fields == ["猫 after incoming ack", "resolved incoming meaning"]
        assert not session.conflicts and session.status == "Saved changes exchanged."
        with SyncState(session.path) as state:
            assert state.get("delivery") is None and state.get("changes") is None
            assert state.db.execute("SELECT count(*) FROM delivery_choices").fetchone()[0] == 0
        emit({"stage": "session_incoming_card_ready"})
        assert json.loads(sys.stdin.readline())["continue"]
        received, continue_delivery = threading.Event(), threading.Event()
        session.login.deliver = delayed_delivery
        assert session.start()
        try:
            wait_until(received.is_set)
            editing = object()

            def flag_before_delivery(col):
                edited = col.get_card(card_id)
                edited.flags = 5
                return col.update_card(edited)

            CollectionOp(mw, flag_before_delivery).run_in_background(initiator=editing)
            wait_until(lambda: editing in operations)
        finally:
            continue_delivery.set()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        session.login.deliver = deliver
        assert len(session.conflicts) == 1 and session.conflicts[0]["direction"] == "incoming", session.conflicts
        assert session.conflicts[0]["kind"] == "cards"
        dialog.conflicts.setCurrentRow(0)
        assert dialog.resolve.isEnabled(), "Incoming card conflicts have no chooser"
        previous_chooser = dialog.card_resolution
        dialog.resolve.click()
        wait_until(lambda: dialog.card_resolution is not previous_chooser and not session.busy)
        chooser = dialog.card_resolution
        acknowledgments = []
        session.login.acknowledge = lose_incoming_acknowledgment
        chooser.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert session.error and "Lost incoming-choice acknowledgment" in session.error, session.error
        assert mw.col.get_card(card_id).flags == 5
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        session.login.acknowledge = acknowledge
        assert acknowledgments[0] == acknowledgments[1]
        assert mw.col.get_card(card_id).flags == 5
        assert mw.col.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 1
        assert not session.conflicts and session.status == "Saved changes exchanged."
        with SyncState(session.path) as state:
            assert state.get("delivery") is None and state.get("changes") is None
            assert state.checkpoint("source_card", card_id)["payload"]["flags"] == 5
        emit({"stage": "session_deletion_ready"})
        assert json.loads(sys.stdin.readline())["continue"]

        def edit_before_deletion(col):
            edited = col.get_note(note.id)
            edited.fields[1] = "Kept current Anki note"
            return col.update_note(edited)

        assert session.run(edit_before_deletion, lambda _: None, mutating=True, preserve_status=True)
        wait_until(lambda: not session.busy)
        dialog.sync.click()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        problem = next(item for item in session.conflicts if item.get("operation") == "delete")
        assert problem["direction"] == "incoming" and problem["kind"] == "notes", problem
        dialog.conflicts.setCurrentRow(session.conflicts.index(problem))
        assert dialog.resolve.isEnabled()
        dialog.resolve.click()
        wait_until(lambda: hasattr(dialog, "note_deletion_resolution") and not session.busy)
        acknowledgments = []
        session.login.acknowledge = lose_incoming_acknowledgment
        dialog.note_deletion_resolution.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert session.error and "Lost incoming-choice acknowledgment" in session.error, session.error
        assert acknowledgments[0][1]["kept_notes"] == [problem["cursor"]]
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        session.login.acknowledge = acknowledge
        assert acknowledgments[0] == acknowledgments[1]
        assert mw.col.get_note(note.id).fields[1] == "Kept current Anki note"
        assert mw.col.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 1
        assert not session.conflicts and session.status == "Saved changes exchanged."
        emit({"stage": "session_absence_ready"})
        assert json.loads(sys.stdin.readline())["continue"]
        assert session.run(session.delivery.receive, lambda _: None)
        wait_until(lambda: not session.busy)
        with SyncState(session.path) as state:
            state.begin_delivery()
        assert session.run(lambda col: col.remove_notes([note.id]), lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        dialog.sync.click()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert any(item.get("operation") == "delete" for item in session.conflicts), (session.error, session.conflicts)
        problem = next(item for item in session.conflicts if item.get("operation") == "delete")
        dialog.conflicts.setCurrentRow(session.conflicts.index(problem))
        chooser = dialog.note_deletion_resolution
        dialog.resolve.click()
        wait_until(lambda: dialog.note_deletion_resolution is not chooser and not session.busy)
        dialog.note_deletion_resolution.accept()
        wait_until(lambda: not session.busy)
        assert not session.conflicts
        assert not mw.col.db.scalar("SELECT count(*) FROM notes WHERE id = ?", note.id)
        assert session.run(lambda col: col.undo(), lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        assert mw.col.get_note(note.id).fields[1] == "Kept current Anki note"
        assert mw.col.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 1
        assert not session.conflicts and session.status == "Saved changes exchanged."
        emit({"stage": "session_return_deletion_ready"})
        assert json.loads(sys.stdin.readline())["continue"]
        returning, allow_return = threading.Event(), threading.Event()
        send_batch = session.login.batch

        def delay_deletion_return(profile_id, payload):
            if any(entry.get("deleted") for entry in payload.get("cards", [])):
                returning.set()
                assert allow_return.wait(15)
            return send_batch(profile_id, payload)

        session.login.batch = delay_deletion_return
        dialog.sync.click()
        wait_until(returning.is_set)
        emit({"stage": "session_deletion_return_waiting"})
        assert json.loads(sys.stdin.readline())["continue"]
        allow_return.set()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        session.login.batch = send_batch
        problem = session.conflicts[0]
        assert problem["direction"] == "returning" and problem["operation"] == "delete", problem
        dialog.conflicts.setCurrentRow(0)
        dialog.resolve.click()
        wait_until(lambda: hasattr(dialog, "source_deletion_resolution") and not session.busy)
        emit({"stage": "session_deletion_return_choice_open"})
        assert json.loads(sys.stdin.readline())["continue"]
        chooser = dialog.source_deletion_resolution
        chooser.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert len(session.conflicts) == 1 and session.conflicts[0]["direction"] == "returning"
        dialog.conflicts.setCurrentRow(0)
        dialog.resolve.click()
        wait_until(lambda: dialog.source_deletion_resolution is not chooser and not session.busy)
        deletion_choices = []

        def lose_deletion_choice(profile_id, payload):
            result = send_batch(profile_id, payload)
            if any(entry.get("deleted") and entry.get("conflict_id") for entry in payload.get("cards", [])):
                deletion_choices.append(payload)
                if len(deletion_choices) == 1:
                    raise ConnectionError("Lost accepted deletion choice")
            return result

        session.login.batch = lose_deletion_choice
        dialog.source_deletion_resolution.accept()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        assert "Lost accepted deletion choice" in session.error, session.error
        assert session.run(lambda col: col.undo(), lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        session.login.batch = send_batch
        assert len(deletion_choices) == 2 and deletion_choices[0] == deletion_choices[1]
        assert mw.col.get_note(note.id).fields[1] == "Kept current Anki note"
        assert mw.col.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 1
        assert not session.conflicts and session.status == "Saved changes exchanged."
        emit({"stage": "session_outgoing_deletion_ready"})
        assert json.loads(sys.stdin.readline())["continue"]
        assert session.run(lambda col: col.remove_notes([note.id]), lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        dialog.sync.click()
        wait_until(lambda: not session.busy, allow_sync_error=True)
        problem = session.conflicts[0]
        assert problem.get("direction") is None and problem["operation"] == "delete", problem
        emit({"stage": "session_outgoing_deleted_elsewhere"})
        assert json.loads(sys.stdin.readline())["continue"]
        dialog.conflicts.setCurrentRow(0)
        chooser = dialog.source_deletion_resolution
        dialog.resolve.click()
        wait_until(lambda: dialog.source_deletion_resolution is not chooser and not session.busy)
        dialog.source_deletion_resolution.accept()
        wait_until(lambda: not session.busy)
        assert not session.conflicts
        emit({"stage": "session_duplicate_deletion_settled"})
        assert json.loads(sys.stdin.readline())["continue"]
        assert session.run(lambda col: col.undo(), lambda _: None, mutating=True)
        wait_until(lambda: not session.busy)
        dialog.sync.click()
        wait_until(lambda: not session.busy)
        assert mw.col.get_note(note.id).fields[1] == "Kept current Anki note"
        assert mw.col.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card_id) == 1
        assert not session.conflicts and session.status == "Saved changes exchanged."
        dialog.reject()
        emit({"stage": "session_close_ready"})
        assert json.loads(sys.stdin.readline())["continue"]
        from shirabe_addon.sync_closing import unload_profile

        mw.state = "review"
        session.set_automatic(True)
        closing = []

        def save_editor(callback):
            def save(col):
                edited = col.get_note(note.id)
                edited.fields[0] = "Anki edit saved on close"
                return col.update_note(edited)

            CollectionOp(mw, save).success(lambda _: callback()).run_in_background()

        def native_close(callback):
            assert not session.busy
            assert mw.col.get_note(note.id).fields == ["Anki edit saved on close", "Shirabe edit received on close"]
            callback()

        mw.closeAllWindows = save_editor
        unload_profile(mw, lambda: closing.append(True), native_close)
        wait_until(lambda: closing == [True])
        emit({"stage": "session_close_synced", "fields": mw.col.get_note(note.id).fields})
        assert json.loads(sys.stdin.readline())["continue"]
        started, release = threading.Event(), threading.Event()
        callbacks = []

        def in_flight():
            started.set()
            assert release.wait(5), "Profile-close test did not release its worker"

        assert session.run(in_flight, callbacks.append)
        wait_until(started.is_set)
        session.close()
        release.set()
        wait_until(lambda: mw.background_ops == 0)
        assert not callbacks, "A completed worker called back into a closed profile"
        assert not session.request()
        assert not session.timer.isActive()
        session.login.sign_out()
        emit({"stage": "session_finished", "reps": card.reps, "operations": len(operations),
              "review_id": mw.col.db.scalar("select id from revlog where cid = ?", card_id)})
    finally:
        session.close()
        mw.taskman._collection_executor.shutdown(wait=True)
        mw.taskman._no_collection_executor.shutdown(wait=True)
        app.processEvents()
        gui_hooks.operation_did_execute.remove(operation_finished)
        mw.progress.clear()
        mw.col.close()
        aqt.mw = previous_mw
        sys.excepthook = previous_exception_hook
        mw.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
