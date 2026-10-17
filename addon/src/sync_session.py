"""One native sync cycle per open Anki profile, shared with the settings dialog."""

from pathlib import Path
from threading import Event

from anki.buildinfo import version
from anki.collection import OpChanges
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import QObject, QTimer, Qt, pyqtSignal

from .sync_changes import SyncChanges
from .sync_client import SyncError
from .sync_collection import SyncCollection
from .sync_delivery import SyncDelivery
from .sync_login import SyncLogin, private_sync_path
from .sync_media import SyncMedia
from .sync_snapshot import SyncSnapshot
from .sync_state import SyncState


def session_for(mw, origin):
    existing = getattr(mw, "_shirabe_study_sync", None)
    path = str(Path(mw.col.path).resolve())
    if existing and not existing.closed.is_set() and existing.collection_path == path:
        return existing
    if existing:
        existing.close()
    mw._shirabe_study_sync = SyncSession(mw, origin)
    return mw._shirabe_study_sync


class SyncSession(QObject):
    changed = pyqtSignal()

    def __init__(self, mw, origin):
        super().__init__(mw)
        self.mw = mw
        self.collection_path = str(Path(mw.col.path).resolve())
        self.path = private_sync_path(mw.pm.profileFolder())
        with SyncState(self.path) as state:
            bound = state.get("initial_snapshot") or state.get("native_login")
            self.automatic = bool(state.get("automatic_sync", False))
        if bound:
            origin = bound["api"].removesuffix("/api/v1")
        self.login = SyncLogin(self.path, origin)
        self.snapshot = SyncSnapshot(self.path, self.login)
        self.changes = SyncChanges(self.path, self.login)
        self.collection_settings = SyncCollection(self.path, self.login)
        self.media = SyncMedia(self.path, self.login)
        self.closed = Event()
        self._working = self._syncing = self._pending = self._paused = False
        self._automatic_run = self._send_after_receive = False
        self._fresh_scan_needed = False
        self._initial_resume = False
        self._publications_ready = False
        self.status = ""
        self.error = None
        self.conflicts = []
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(lambda: self.start(automatic=True))
        self._retry_operation = None
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.retry_timer.timeout.connect(self._retry)

    @property
    def busy(self):
        return self._working or self._syncing or self._retry_operation is not None

    def set_automatic(self, enabled):
        self.automatic = bool(enabled)
        with SyncState(self.path) as state:
            state.put("automatic_sync", self.automatic)
        if self.automatic:
            self.request()
        else:
            self._pending = False
            self.timer.stop()
        self.changed.emit()

    def request(self, changes=None, initiator=None):
        if (not self.automatic and not self._syncing) or self.closed.is_set() or self._paused or initiator is self or version != "26.08.1":
            return False
        if changes is not None and not any(getattr(changes, field, False) for field in ("card", "note", "notetype", "deck", "deck_config", "config", "tag")):
            return False
        self._pending = True
        self._schedule()
        return True

    def _schedule(self):
        if self._pending and self.automatic and not self.busy and not self._paused and not self.closed.is_set():
            self.timer.start()

    def _status(self, message):
        self.status = message
        self.changed.emit()

    def run(self, operation, success, *, collection=False, mutating=False, preserve_status=False, cancelled=None):
        if self.busy:
            return False
        if not preserve_status:
            self.error = None
            self.status = ""
        return self._run(operation, success, collection=collection, mutating=mutating, cancelled=cancelled)

    def _run(self, operation, success, *, collection=False, mutating=False, cancelled=None):
        if self.closed.is_set() or self._working:
            return False
        self._working = True
        self.changed.emit()

        def ready(result):
            self._working = False
            self.changed.emit()
            if self.closed.is_set():
                return
            success(result)
            self._schedule()

        def work(col):
            if self.closed.is_set():
                raise ValueError("The Anki profile closed before this sync step started")
            if cancelled and cancelled():
                raise ValueError("Sync cancelled. Saved progress is retained.")
            if collection or mutating:
                if str(Path(col.path).resolve()) != self.collection_path:
                    raise ValueError("The open Anki profile changed. Reopen study sync in that profile.")
                return operation(col)
            return operation()

        def failed(error):
            if (not collection and not mutating and not self.closed.is_set() and not (cancelled and cancelled())
                    and isinstance(error, SyncError) and (error.status, error.code) in
                    ((429, "RATE_LIMIT_EXCEEDED"), (503, "MEDIA_NOT_READY"))
                    and type(error.retry_after) is int and 1 <= error.retry_after <= 86400):
                self._working = False
                self._retry_operation = (operation, success, cancelled)
                self.retry_timer.start(error.retry_after * 1000)
                reason = "Preparing note media" if error.code == "MEDIA_NOT_READY" else "Shirabe request limit reached"
                self._status(f"{reason}. Resuming in {error.retry_after} seconds…")
            else:
                self._failed(error)

        if mutating:
            CollectionOp(self.mw, work).success(ready).failure(self._failed).run_in_background(initiator=self)
        else:
            query = QueryOp(parent=self.mw, op=work, success=ready).failure(failed)
            if not collection:
                query.without_collection()
            query.run_in_background()
        return True

    def _retry(self):
        pending, self._retry_operation = self._retry_operation, None
        self.retry_timer.stop()
        if pending and not self.closed.is_set():
            operation, success, cancelled = pending
            self._run(operation, success, cancelled=cancelled)

    def cancel_wait(self):
        if self._retry_operation is not None:
            _, _, cancelled = self._retry_operation
            if cancelled and cancelled():
                self._failed(ValueError("Sync cancelled. Saved progress is retained."))

    def _failed(self, error):
        self.retry_timer.stop()
        self._retry_operation = None
        self._working = self._syncing = self._pending = False
        if self.closed.is_set():
            self.changed.emit()
            return
        self._paused = True
        self.error = str(error)
        self.conflicts = getattr(error, "conflicts", [])
        self.timer.stop()
        self._status(f"Sync paused: {error}")

    def start(self, *, automatic=False):
        if self.closed.is_set() or self.busy or version != "26.08.1":
            return False
        if automatic and (not self.automatic or self._paused):
            return False
        if automatic and getattr(self.mw, "state", None) == "review":
            self.request()
            return False
        with SyncState(self.path) as state:
            captured = state.get("initial_snapshot")
            pending_delivery = state.get("delivery")
            initial_cycle = state.get("changes")
        if not captured or captured["phase"] not in ("uploaded", "resolving") or not self.login.credentials():
            return False
        if automatic and captured["phase"] == "resolving":
            return False
        try:
            delivery = SyncDelivery(self.path, self.login, profile_id=captured["profile"]["id"], key=captured["pair_request"]["key"])
        except Exception as error:
            self._failed(error)
            return False
        self.timer.stop()
        self._syncing = True
        self.error = None
        self.conflicts = []
        self._pending = self._paused = False
        self._automatic_run = automatic
        self._send_after_receive = bool(pending_delivery)
        self._fresh_scan_needed = False
        self._initial_resume = captured["phase"] == "resolving"
        self._publications_ready = False
        self.delivery = delivery
        if self._initial_resume and not initial_cycle:
            self._resume_initial()
        elif pending_delivery and pending_delivery.get("phase") != "received" and not self._initial_resume:
            self._receive()
        else:
            self._capture()
        return True

    def _capture(self):
        self._pending = False
        self._status("Preparing Anki changes or resuming the saved sync…")
        def capture(col):
            settings = self.collection_settings.capture(col) if not self._initial_resume else None
            return {"changes": self.changes.capture(col), "settings": bool(settings)}

        self._run(capture, self._upload, collection=True)

    def _upload(self, captured):
        self._fresh_scan_needed = bool(captured["changes"]["keys"] or captured["settings"])

        def upload():
            if captured["settings"]:
                self.collection_settings.upload()
            return self.changes.upload(cancelled=self.closed.is_set, progress=self._progress)

        self._run(upload, self._apply_outgoing)

    def _progress(self, done, total, *, kind="batch"):
        def update():
            if not self.closed.is_set():
                self._status(f"Uploading {kind} {done} of {total}…")
        self.mw.taskman.run_on_main(update)

    def _defer_review(self):
        if self._automatic_run and getattr(self.mw, "state", None) == "review":
            self._syncing = False
            self._pending = True
            self._status("Sync will resume after this study session.")
            self._schedule()
            return True
        return False

    def _apply_outgoing(self, _):
        if self._defer_review():
            return
        problems = []

        def apply(col):
            try:
                self.changes.apply(col)
            except Exception as error:
                problems.append(error)
            return OpChanges(card=True, note=True, study_queues=True, browser_table=True)

        def applied(_):
            if problems:
                self._failed(problems[0])
            elif self._initial_resume:
                self._resume_initial()
            elif self._fresh_scan_needed:
                self._capture()
            else:
                self._receive()

        self._run(apply, applied, mutating=True)

    def _resume_initial(self):
        self._status("Resuming the initial upload…")

        def uploaded(_):
            self._syncing = False
            self.start()

        self._run(lambda: self.snapshot.upload(cancelled=self.closed.is_set, progress=self._progress,
                                               media_progress=lambda done, total: self._progress(done, total, kind="media file")), uploaded)

    def _receive(self):
        if not self._publications_ready and not self._initial_resume:
            with SyncState(self.path) as state:
                pending = state.get("delivery")
            if not pending:
                self._status("Preparing Shirabe note changes…")
                self._run(lambda: self.collection_settings.prepare_publications(cancelled=self.closed.is_set), self._publications_prepared)
                return
        self._status("Receiving Shirabe changes…")
        self._run(self.delivery.receive, self._apply_delivery)

    def _publications_prepared(self, _):
        self._publications_ready = True
        self._receive()

    def _apply_delivery(self, page):
        if self._defer_review():
            return
        problems = []

        def apply(col):
            try:
                self.delivery.apply(col)
            except Exception as error:
                problems.append(error)
            return OpChanges(card=True, note=True, study_queues=True, browser_table=True)

        def applied(_):
            if problems:
                self._failed(problems[0])
                return
            self._fresh_scan_needed |= bool(page["changes"])
            self._run(self.delivery.acknowledge, acknowledged)

        def acknowledged(_):
            if any(change.get("entity_kind") == "note" and "creation" in change.get("payload", {}) for change in page["changes"]):
                self._send_after_receive = False
                self._capture()
            elif page["changes"]:
                self._receive()
            elif self._send_after_receive:
                self._send_after_receive = False
                self._capture()
            elif self._fresh_scan_needed or self._pending:
                self._capture()
            else:
                self._capture_media()

        self._run(apply, applied, mutating=True)

    def _capture_media(self):
        if not self._publications_ready:
            self._receive()
            return
        self._status("Checking Anki media changes…")
        self._run(self.media.capture, self._exchange_media, collection=True)

    def _exchange_media(self, outgoing):
        self._media_outgoing = outgoing
        self._status("Exchanging media files…")
        self._run(lambda: self.media.exchange(cancelled=self.closed.is_set,
                                             progress=lambda done, total: self._progress(done, total, kind="media file")), self._apply_media)

    def _apply_media(self, _):
        if self._defer_review():
            return
        problems, counts = [], []

        def apply(col):
            try:
                counts.append(self.media.apply(col, cancelled=self.closed.is_set))
            except Exception as error:
                problems.append(error)
            return OpChanges(note=True, browser_table=True)

        def applied(_):
            if problems:
                self._failed(problems[0])
            elif self._media_outgoing or counts[0] or self._pending:
                self._capture()
            else:
                self._finished()

        self._run(apply, applied, mutating=True)

    def _finished(self):
        self._syncing = False
        self._status("Saved changes exchanged.")
        self._schedule()

    def close(self):
        self.closed.set()
        self.retry_timer.stop()
        self._retry_operation = None
        self._pending = self._syncing = False
        self.timer.stop()
