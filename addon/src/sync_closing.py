from anki.buildinfo import version
from aqt.qt import QDialog, QDialogButtonBox, QLabel, Qt, QTimer, QVBoxLayout


def unload_profile(mw, onsuccess, original):
    session = getattr(mw, "_shirabe_study_sync", None)
    if getattr(mw, "_shirabe_profile_close", None):
        return
    if not session or session.closed.is_set() or not session.automatic or version != "26.08.1":
        original(onsuccess)
        return
    request = object()
    mw._shirabe_profile_close_request = request

    def saved():
        if mw._shirabe_profile_close_request is not request:
            return
        mw._shirabe_profile_close_request = None
        mw._shirabe_profile_close = ProfileClose(mw, session, lambda: original(onsuccess))
        mw._shirabe_profile_close.open()

    mw.closeAllWindows(saved)


class ProfileClose(QDialog):
    def __init__(self, mw, session, finish):
        super().__init__(mw)
        self.mw, self.session, self.finish = mw, session, finish
        self.started = self.skipping = self.finished = False
        self.setWindowTitle("Finish Shirabe sync")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        layout = QVBoxLayout(self)
        self.status = QLabel("Waiting for the current sync step…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.retry = buttons.addButton("Retry sync", QDialogButtonBox.ButtonRole.ActionRole)
        self.retry.clicked.connect(self.retry_sync)
        self.stay = buttons.addButton("Stay in Anki", QDialogButtonBox.ButtonRole.RejectRole)
        self.stay.clicked.connect(self.reject)
        self.skip = buttons.addButton("Close without finishing sync", QDialogButtonBox.ButtonRole.ActionRole)
        self.skip.clicked.connect(self.skip_sync)
        layout.addWidget(buttons)
        self.retry.setEnabled(False)
        self.check_timer = QTimer(self)
        self.check_timer.setSingleShot(True)
        self.check_timer.timeout.connect(self.check)
        self.session.changed.connect(self.schedule_check)
        self.schedule_check()

    def schedule_check(self):
        self.check_timer.start(0)

    def check(self):
        if self.finished:
            return
        if self.skipping:
            if not self.session.busy:
                self.complete()
            return
        if self.session.closed.is_set():
            self.reject()
            return
        self.status.setText(self.session.status or "Finishing Shirabe sync…")
        if self.session.busy:
            return
        if not self.started:
            self.started = True
            try:
                started = self.session.start()
            except Exception as error:
                self.session._failed(error)
                return
            if not started and not self.session.error:
                self.complete()
        elif self.session.error:
            self.status.setText(f"Shirabe sync did not finish: {self.session.error}\nSaved progress will be kept for the next sync.")
            self.retry.setEnabled(True)
        else:
            self.complete()

    def retry_sync(self):
        self.retry.setEnabled(False)
        self.started = False
        self.schedule_check()

    def skip_sync(self):
        self.skipping = True
        self.retry.setEnabled(False)
        self.stay.setEnabled(False)
        self.skip.setEnabled(False)
        self.status.setText("Stopping sync after the current step. Saved progress will be kept.")
        self.session.close()
        self.schedule_check()

    def release(self):
        self.finished = True
        self.check_timer.stop()
        self.session.changed.disconnect(self.schedule_check)
        self.mw._shirabe_profile_close = None

    def complete(self):
        self.release()
        super().accept()
        self.finish()

    def reject(self):
        if self.skipping or self.finished:
            return
        self.release()
        super().reject()
