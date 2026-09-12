"""Native study pairing and collection-worker handoffs, separate from card bridges."""

from __future__ import annotations

from threading import Event

from anki.buildinfo import version
from aqt.qt import QCheckBox, QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QTimer, QVBoxLayout, QWidget, Qt
from aqt.utils import openLink

from .sync_session import session_for
from .sync_state import SyncState


def label(text):
    widget = QLabel()
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setText(text)
    return widget


class SnapshotPreviewDialog(QDialog):
    def __init__(self, parent, captured):
        super().__init__(parent)
        self.setWindowTitle("Review initial copy")
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = QVBoxLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        counts = captured["counts"]
        content.addWidget(label("This saved copy contains:"))
        content.addWidget(label(f"Decks (including subdecks): {len(captured['deck_ids']):,}\n"
                                f"Notes: {counts['notes']:,}\nCards: {counts['cards']:,}\n"
                                f"Note types: {counts['note_types']:,}\nHistory entries: {counts['history']:,}\n"
                                f"Media files: {counts['media_files']:,} ({counts['media_bytes']:,} bytes)"))
        if captured["unselected_siblings"]:
            content.addWidget(label(f"{captured['unselected_siblings']:,} cards outside the selected decks share these notes. "
                                    "Editing a shared note in either app also changes those cards. "
                                    "Only selected cards' schedules are copied."))
        if captured["missing_media"]:
            content.addWidget(label(f"Missing referenced files: {len(captured['missing_media']):,}. "
                                    "These files cannot be uploaded; affected cards may have missing images, sound or styling."))
        content.addWidget(label("Media includes referenced files and underscore-prefixed shared assets. "
                                "The counts describe the saved copy, not later edits in Anki. "
                                "Resuming skips items already accepted by Shirabe. Cancel keeps the local copy for later."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Upload this copy")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        available = self.screen().availableGeometry()
        self.resize(min(560, available.width() - 40), min(620, available.height() - 40))


class StudySyncDialog(QDialog):
    def __init__(self, mw, origin):
        super().__init__(mw)
        self.mw = mw
        self.session = session_for(mw, origin)
        self.path = self.session.path
        self.login = self.session.login
        self.snapshot = self.session.snapshot
        origin = self.login.api.removesuffix("/api/v1")
        self.closed = Event()
        self.verification_url = None
        self.setWindowTitle("Shirabe study sync")
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        layout.addWidget(label("Experimental study sync. Upload selected decks, then use Sync now to exchange edits and reviews. "
                               "Referenced media and later file edits are included. Conflicting files and local deletions need your confirmation. "
                               "Changed deck selections and some deletion and scheduling conflicts still need support. "
                               "Some custom card scripts may not work in Shirabe."))
        layout.addWidget(label("Selecting a deck includes its subdecks and the complete fields of its notes. "
                               "Editing a shared note can change cards outside the selected decks. Only selected cards' schedules are copied."))
        layout.addWidget(label(f"Study account: {origin}. This login is separate from the dictionary popup and stays on this computer."))
        self.status = label("")
        layout.addWidget(self.status)
        self.conflicts = QListWidget()
        self.conflicts.setMaximumHeight(160)
        self.conflicts.hide()
        layout.addWidget(self.conflicts)
        self.resolve = QPushButton("Resolve selected conflict…")
        self.resolve.clicked.connect(self._resolve_selected)
        self.conflicts.currentItemChanged.connect(lambda *_: self._controls())
        layout.addWidget(self.resolve)
        self.template_checks = QPushButton("Template checks…")
        self.template_checks.clicked.connect(lambda: self._background(self.snapshot.template_diagnostics, self._show_template_checks))
        layout.addWidget(self.template_checks)
        self.sign_in = QPushButton("Sign in for study sync")
        self.sign_in.clicked.connect(lambda: self._background(self.login.start_login, self._waiting))
        layout.addWidget(self.sign_in)
        self.approve = QPushButton("Open approval page")
        self.approve.clicked.connect(self._open_approval)
        self.approve.hide()
        layout.addWidget(self.approve)
        self.sign_out = QPushButton("Sign out of study sync")
        self.sign_out.clicked.connect(lambda: self._background(self.login.sign_out, self._signed_out))
        layout.addWidget(self.sign_out)
        self.deck_list = QListWidget()
        layout.addWidget(self.deck_list)
        self.upload = QPushButton("Upload selected decks")
        self.upload.clicked.connect(self._capture)
        layout.addWidget(self.upload)
        self.sync = QPushButton("Sync now")
        self.sync.clicked.connect(self._sync)
        layout.addWidget(self.sync)
        self.automatic = QCheckBox("Sync automatically")
        self.automatic.setChecked(self.session.automatic)
        self.automatic.toggled.connect(self.session.set_automatic)
        layout.addWidget(self.automatic)
        layout.addWidget(label("Automatic sync waits until you leave the reviewer. Anki may show its progress indicator while applying changes. "
                               "Conflicts pause automatic sync until you return here."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)
        self.finished.connect(self._closed)
        self.session.changed.connect(self._session_changed)
        available = self.screen().availableGeometry()
        self.resize(min(640, available.width() - 40), min(740, available.height() - 40))
        self._controls()
        if version != "26.08.1":
            self.status.setText(f"Study sync requires the verified Anki 26.08.1 build. This computer is running {version}.")
        else:
            self._background(self._deck_choices, self._show_decks, collection=True, preserve_status=True)

    def _controls(self):
        authenticated = bool(self.login.credentials())
        with SyncState(self.path) as state:
            captured = state.get("initial_snapshot")
        ready = bool(captured and captured["phase"] == "uploaded")
        enabled = not self.busy and version == "26.08.1"
        selected = self.conflicts.currentItem()
        problem = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        resolvable = bool(problem) and problem["kind"] in ("notes", "cards", "review_undos", "review_undo_results", "media")
        retryable = bool(problem) and problem["kind"] in ("note_types", "notes", "history", "cards") and \
            problem.get("status") == "rejected" and problem.get("direction") != "incoming"
        mapping = bool(problem) and problem["kind"] == "note_types" and problem.get("status") == "mapping_required"
        self.resolve.setEnabled(enabled and (mapping or retryable or resolvable and problem.get("status") != "rejected"))
        self.resolve.setText("Match fields and templates…" if mapping else "Review rejected record…" if retryable else "Resolve selected conflict…")
        self.resolve.setVisible(bool(self.session.conflicts))
        self.template_checks.setEnabled(not self.busy and bool(captured))
        self.sign_in.setEnabled(enabled and not authenticated)
        self.sign_out.setEnabled(not self.busy and authenticated)
        self.deck_list.setEnabled(enabled and not captured)
        self.upload.setEnabled(enabled and authenticated and not ready)
        self.upload.setText("Initial copy uploaded" if ready else "Resume initial upload" if captured else "Upload selected decks")
        self.sync.setEnabled(enabled and authenticated and ready)
        self.automatic.setEnabled(enabled and authenticated and ready)

    @property
    def busy(self):
        return self.session.busy

    def _session_changed(self):
        if self.closed.is_set():
            return
        self._controls()
        if self.session.error:
            self.timer.stop()
        if self.session.status:
            self.status.setText(self.session.status)
        self.conflicts.clear()
        for conflict in self.session.conflicts:
            item = QListWidgetItem(f"{conflict['kind']} {conflict['id']}: {conflict['code']}")
            item.setData(Qt.ItemDataRole.UserRole, conflict)
            self.conflicts.addItem(item)
        self.conflicts.setVisible(bool(self.session.conflicts))

    def _background(self, operation, success, *, collection=False, mutating=False, preserve_status=False):
        if self.closed.is_set() or self.busy:
            return
        def ready(result):
            if self.closed.is_set():
                return
            self._controls()
            success(result)
        self.session.run(operation, ready, collection=collection, mutating=mutating, preserve_status=preserve_status,
                         cancelled=self.closed.is_set)

    @staticmethod
    def _deck_choices(col):
        return [(item.id, item.name) for item in col.decks.all_names_and_ids() if not col.decks.is_filtered(item.id)]

    def _show_decks(self, choices):
        with SyncState(self.path) as state:
            captured = state.get("initial_snapshot")
        selected = captured["root_deck_ids"] if captured else []
        for source_id, name in choices:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, source_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if source_id in selected else Qt.CheckState.Unchecked)
            self.deck_list.addItem(item)
        pending = self.login.pending_login()
        if pending and not self.login.credentials():
            self._waiting(pending)

    def _waiting(self, pending):
        self.verification_url = pending["verification_uri_complete"]
        self.status.setText(f"Approve code {pending['user_code']} at {pending['verification_uri']}, then return here.")
        self.approve.show()
        self.timer.start()

    def _open_approval(self):
        if self.verification_url:
            openLink(self.verification_url, parent=self)

    def _poll(self):
        def polled(connected):
            if connected:
                self.timer.stop()
                self.approve.hide()
                self.verification_url = None
                self.status.setText("Study sync signed in. Choose decks for the initial copy.")

        self._background(self.login.poll_login, polled)

    def _signed_out(self, _):
        self.timer.stop()
        self.approve.hide()
        self.verification_url = None
        self.status.setText("Study sync signed out. Your copied decks and sync checkpoints are retained.")

    def _capture(self):
        selected = [self.deck_list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.deck_list.count())
                    if self.deck_list.item(index).checkState() == Qt.CheckState.Checked]
        self.status.setText("Preparing the selected decks or resuming their saved upload…")
        self._background(lambda col: self.snapshot.capture(col, selected), self._upload, collection=True)

    def _upload(self, captured):
        if self.closed.is_set():
            return
        if captured["phase"] == "resolving":
            self._sync()
            return
        self.status.setText("Review the saved copy before uploading.")
        self.snapshot_preview = SnapshotPreviewDialog(self, captured)
        self.snapshot_preview.accepted.connect(lambda: self._send_snapshot(captured))
        self.snapshot_preview.rejected.connect(lambda: self.status.setText("Upload paused. The saved copy is ready to resume."))
        self.snapshot_preview.open()

    def _send_snapshot(self, captured):
        if self.closed.is_set():
            return
        self.status.setText(f"Uploading {len(captured['deck_ids'])} decks. {captured['unselected_siblings']} unselected sibling cards share these notes.")

        self._background(lambda: self.snapshot.upload(cancelled=self.closed.is_set, progress=self._progress,
                                                      media_progress=lambda done, total: self._progress(done, total, kind="media file")), lambda _: self._sync())

    def _progress(self, done, total, *, kind="batch"):
        def update():
            if not self.closed.is_set():
                self.status.setText(f"Uploading {kind} {done} of {total}. Closing this window pauses after the current request.")
        self.mw.taskman.run_on_main(update)

    def _sync(self):
        self.session.start()

    def _resolve_selected(self):
        selected = self.conflicts.currentItem()
        if selected is None:
            return
        problem = selected.data(Qt.ItemDataRole.UserRole)
        if problem["kind"] == "note_types" and problem.get("status") == "mapping_required":
            self._background(lambda col: self.session.changes.prepare_type_mapping(col, problem), self._show_type_mapping, collection=True)
            return
        if problem["kind"] == "media":
            def fetched(remote):
                self._background(lambda col: self.session.media.prepare_resolution(col, problem, remote),
                                 self._show_media_resolution, collection=True)

            self._background(lambda: self.session.media.conflict_detail(problem), fetched)
            return
        if problem.get("direction") == "returning" and problem["kind"] == "review_undo_results":
            self._background(lambda col: self.session.delivery.prepare_review_undo_result_resolution(col, problem),
                             self._show_review_undo_result_resolution, collection=True)
            return
        if problem.get("status") == "rejected" and problem.get("direction") != "incoming":
            self._background(lambda col: self.session.changes.prepare_rejected_retry(col, problem),
                             self._show_rejected_retry, collection=True)
            return
        if problem.get("direction") == "incoming":
            if problem["kind"] == "review_undos":
                self._background(lambda col: self.session.delivery.prepare_review_undo_resolution(col, problem),
                                 self._show_review_undo_resolution, collection=True)
            elif problem["kind"] == "notes" and problem.get("operation") == "delete":
                self._background(lambda col: self.session.delivery.prepare_note_deletion_resolution(col, problem),
                                 self._show_note_deletion_resolution, collection=True)
            elif problem["kind"] == "notes":
                self._background(lambda col: self.session.delivery.prepare_note_resolution(col, problem),
                                 self._show_note_resolution, collection=True)
            elif problem["kind"] == "cards":
                self._background(lambda col: self.session.delivery.prepare_card_resolution(col, problem),
                                 self._show_card_resolution, collection=True)
            return
        if problem["kind"] == "cards":
            self._resolve_card(problem)
            return

        def fetched(detail):
            self._background(lambda col: self.session.changes.prepare_note_resolution(col, problem, detail),
                             self._show_note_resolution, collection=True)

        self._background(lambda: self.session.changes.note_conflict(problem), fetched)

    def _show_type_mapping(self, prepared):
        from .sync_conflict_ui import TypeMappingDialog

        def submit(mappings):
            self._background(lambda col: self.session.changes.resolve_type_mapping(col, prepared, mappings),
                             lambda _: self._sync(), collection=True)

        self.type_mapping = TypeMappingDialog(self, prepared, submit)
        self.type_mapping.open()

    def _show_template_checks(self, reports):
        from .sync_conflict_ui import TemplateChecksDialog

        self.template_checks_dialog = TemplateChecksDialog(self, reports)
        self.template_checks_dialog.open()

    def _show_media_resolution(self, prepared):
        from .sync_conflict_ui import MediaConflictDialog

        def submit(choice):
            self._background(lambda col: self.session.media.resolve(col, prepared, choice),
                             lambda _: self._sync(), collection=True)

        self.media_resolution = MediaConflictDialog(self, prepared, submit)
        self.media_resolution.open()

    def _show_review_undo_resolution(self, prepared):
        from .sync_conflict_ui import ReviewUndoDialog

        def submit(action):
            operation = self.session.delivery.confirm_review_undo if action == "undo" else self.session.delivery.withdraw_review_undo
            self._background(lambda col: operation(col, prepared),
                             lambda _: self._sync(), collection=True)

        self.review_undo_resolution = ReviewUndoDialog(self, prepared, submit)
        self.review_undo_resolution.open()

    def _show_rejected_retry(self, prepared):
        from .sync_conflict_ui import RejectedRecordDialog

        def submit(source):
            if source == "mapping":
                self._background(lambda col: self.session.changes.prepare_type_mapping(col, prepared["problem"]),
                                 self._show_type_mapping, collection=True)
                return
            self._background(lambda col: self.session.changes.retry_rejected(col, prepared, source=source),
                             lambda _: self._sync(), collection=True)

        self.rejected_retry = RejectedRecordDialog(self, prepared, submit)
        self.rejected_retry.open()

    def _resolve_card(self, problem):
        def fetched(detail):
            if detail.get("history_removal"):
                self._background(lambda col: self.session.changes.prepare_history_removal(col, problem, detail),
                                 self._show_history_removal, collection=True)
            elif problem.get("operation") == "delete":
                self._background(lambda col: self.session.changes.prepare_deletion_resolution(col, problem, detail),
                                 self._show_source_deletion_resolution, collection=True)
            else:
                self._background(lambda col: self.session.changes.prepare_card_resolution(col, problem, detail),
                                 self._show_card_resolution, collection=True)

        self._background(lambda: self.session.changes.card_conflict(problem), fetched)

    def _show_history_removal(self, prepared):
        from .sync_conflict_ui import HistoryRemovalDialog

        def submit(schedule):
            changes = self.session.delivery if prepared["problem"].get("direction") == "incoming" else self.session.changes
            self._background(lambda col: changes.resolve_history_removal(col, prepared, schedule=schedule),
                             lambda _: self._sync(), collection=True)

        self.history_removal_resolution = HistoryRemovalDialog(self, prepared, submit)
        self.history_removal_resolution.open()

    def _show_source_deletion_resolution(self, prepared):
        from .sync_conflict_ui import SourceDeletionDialog

        def submit():
            self._background(lambda col: self.session.changes.resolve_deletion(col, prepared),
                             lambda _: self._sync(), collection=True)

        self.source_deletion_resolution = SourceDeletionDialog(self, prepared, submit)
        self.source_deletion_resolution.open()

    def _show_card_resolution(self, prepared):
        if prepared.get("entry", {}).get("removed_history"):
            return self._show_history_removal(prepared)
        from .sync_conflict_ui import CardConflictDialog

        def submit(choices):
            changes = self.session.delivery if prepared["problem"].get("direction") == "incoming" else self.session.changes
            self._background(lambda col: changes.resolve_card(col, prepared, choices),
                             lambda _: self._sync(), collection=True)

        self.card_resolution = CardConflictDialog(self, prepared, submit)
        self.card_resolution.open()

    def _show_review_undo_result_resolution(self, prepared):
        from anki.collection import OpChanges
        from .sync_conflict_ui import CardConflictDialog

        def submit(choices):
            def apply(col):
                self.session.delivery.resolve_review_undo_result(col, prepared, choices)
                return OpChanges(card=True, study_queues=True, browser_table=True)

            self._background(apply, lambda _: self._sync(), mutating=True)

        self.review_undo_result_resolution = CardConflictDialog(self, prepared, submit)
        self.review_undo_result_resolution.open()

    def _show_note_resolution(self, prepared):
        from .sync_conflict_ui import NoteConflictDialog

        def submit(fields, tags):
            changes = self.session.delivery if prepared["problem"].get("direction") == "incoming" else self.session.changes
            self._background(lambda col: changes.resolve_note(col, prepared, fields, tags),
                             lambda _: self._sync(), collection=True)

        self.note_resolution = NoteConflictDialog(self, prepared, submit)
        self.note_resolution.open()

    def _show_note_deletion_resolution(self, prepared):
        from .sync_conflict_ui import NoteDeletionDialog

        def submit():
            action = self.session.delivery.confirm_note_absence if prepared["local"] is None else self.session.delivery.keep_note
            self._background(lambda col: action(col, prepared), lambda _: self._sync(), collection=True)

        self.note_deletion_resolution = NoteDeletionDialog(self, prepared, submit)
        self.note_deletion_resolution.open()

    def _closed(self, *_):
        if not self.closed.is_set():
            self.session.changed.disconnect(self._session_changed)
        self.closed.set()
        if hasattr(self, "snapshot_preview"):
            self.snapshot_preview.reject()
        self.session.cancel_wait()
        self.timer.stop()
