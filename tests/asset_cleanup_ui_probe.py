import argparse
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
import weakref

from gui_safety import gui_arguments

parser = argparse.ArgumentParser(description="Check an isolated Anki cleanup dialog.")
parser.add_argument("--font-size", type=int, default=13)
parser.add_argument("--completed-before-callback", action="store_true")
parser.add_argument("--output", type=Path)
args = gui_arguments(parser, allow_offscreen=True)
if args.output:
    args.output.mkdir(parents=True, exist_ok=True)

import support
from offline_fixture import prepared
from shirabe_addon import offline, offline_ui
from anki.collection import Collection
import aqt
from aqt.qt import QApplication, QDialogButtonBox, QFont, QLabel, QPoint, QPlainTextEdit, QScrollArea, QTimer, QWidget
from aqt.taskman import TaskManager


class CleanupWindow(QWidget):
    def __init__(self, col):
        super().__init__()
        self.col = col
        self.background_ops = 0
        self.progress = SimpleNamespace(start=lambda **_: None, finish=lambda: None)
        self.taskman = TaskManager(self)

    def weakref(self):
        return weakref.proxy(self)

    def _increase_background_ops(self):
        self.background_ops += 1

    def _decrease_background_ops(self):
        self.background_ops -= 1


app = QApplication(["shirabe-cleanup-probe"])
app.setFont(QFont("Arial", args.font_size))
app.setQuitOnLastWindowClosed(False)
with tempfile.TemporaryDirectory(prefix="shirabe-cleanup-ui-") as directory:
    col = Collection(str(Path(directory) / "collection.anki2"))
    parent = CleanupWindow(col)
    if args.completed_before_callback:
        submit = parent.taskman._collection_executor.submit

        def completed_submit(*args, **kwargs):
            future = submit(*args, **kwargs)
            future.result(timeout=5)
            return future

        parent.taskman._collection_executor.submit = completed_submit
    aqt.mw = parent
    body = b"old offline code"
    code = col.media.write_data(f"_shirabe_{hashlib.sha256(body).hexdigest()[:20]}_attach.js", body)
    snapshot = offline.persist_snapshot(col.media, prepared()).filename
    errors = []
    notices = []
    threads = []
    inspect = offline_ui.inspect_cleanup
    trash = offline_ui.trash_reviewed

    def inspection(collection):
        threads.append(threading.get_ident())
        return inspect(collection)

    def removal(collection, names):
        threads.append(threading.get_ident())
        return trash(collection, names)

    def fail(message):
        errors.append(str(message))
        dialog = app.activeModalWidget()
        if dialog:
            dialog.reject()
        app.exit(1)

    def run(mode):
        acted = []
        done = []
        def interact():
            if acted:
                return
            dialog = app.activeModalWidget()
            if not dialog or dialog.windowTitle() != "Review Shirabe storage":
                return
            acted.append(True)
            try:
                copy = "\n".join(label.text() for label in dialog.findChildren(QLabel))
                assert "Snapshots:" in copy and "Code bundles:" in copy and "Total:" in copy, copy
                assert "Undo" in copy and "media trash" in copy, copy
                listing = dialog.findChild(QPlainTextEdit).toPlainText().splitlines()
                buttons = dialog.findChild(QDialogButtonBox)
                content = dialog.findChild(QScrollArea)
                assert content.horizontalScrollBar().maximum() == 0, "Cleanup instructions require horizontal scrolling"
                screen = dialog.screen().availableGeometry()
                assert dialog.frameGeometry().height() <= screen.height(), "Cleanup dialog exceeds screen height"
                assert dialog.frameGeometry().width() <= screen.width(), "Cleanup dialog exceeds screen width"
                for button in buttons.buttons():
                    top_left = button.mapToGlobal(QPoint(0, 0))
                    bottom_right = top_left + QPoint(button.width() - 1, button.height() - 1)
                    assert screen.contains(top_left) and screen.contains(bottom_right), f"Button outside screen: {button.text()}"
                    assert button.width() >= button.minimumSizeHint().width(), f"Clipped button: {button.text()}"
                for label in dialog.findChildren(QLabel):
                    if label.isVisible() and label.wordWrap():
                        assert label.height() + 2 >= label.heightForWidth(label.width()), f"Clipped label: {label.text()}"
                    elif label.isVisible():
                        assert label.width() >= label.minimumSizeHint().width(), f"Clipped label: {label.text()}"
                if mode == "cancel" and args.output:
                    assert dialog.grab().save(str(args.output / f"cleanup-{args.font_size}pt-top.png"))
                    content.verticalScrollBar().setValue(content.verticalScrollBar().maximum())
                    app.processEvents()
                    assert dialog.grab().save(str(args.output / f"cleanup-{args.font_size}pt-bottom.png"))
                approve = buttons.button(QDialogButtonBox.StandardButton.Ok)
                if mode == "empty":
                    assert listing == [] and not approve.isEnabled(), listing
                else:
                    assert set(listing) == {snapshot, code}, listing
                    assert approve.isEnabled()
                if mode == "accept":
                    note = col.new_note(col.models.by_name("Basic"))
                    note["Front"] = code
                    col.add_note(note, 1)
                    approve.click()
                else:
                    buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
            except Exception as error:
                fail(error)
        def finished():
            done.append(True)
            app.quit()
        timer = QTimer()
        timer.timeout.connect(interact)
        timer.start(10)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(lambda: fail("Cleanup UI timed out"))
        deadline.start(10000)
        QTimer.singleShot(0, lambda: offline_ui.review_cleanup(parent, finished))
        app.exec()
        timer.stop()
        deadline.stop()
        assert done and acted and not errors, (done, acted, errors)
        assert parent.background_ops == 0

    try:
        with patch.object(offline_ui, "inspect_cleanup", inspection), patch.object(offline_ui, "trash_reviewed", removal), \
                patch.object(offline, "DictionaryBackend", side_effect=AssertionError("Cleanup must not require sign-in")), \
                patch("aqt.utils.showInfo", lambda message, **_: notices.append(message)), \
                patch("aqt.utils.showWarning", lambda message, **_: fail(message)):
            run("cancel")
            assert col.media.have(snapshot) and col.media.have(code)
            assert notices == []
            run("accept")
            assert not col.media.have(snapshot) and col.media.have(code)
            assert "Moved 1 of 2 reviewed files" in notices[-1], notices
            run("empty")
            assert not col.media.have(snapshot) and col.media.have(code)
        assert len(set(threads)) == 1 and threads[0] != threading.get_ident(), threads
        col.media.restore_trash()
        assert col.media.have(snapshot) and col.media.have(code)
        print("native cleanup UI, serialized QueryOp, cancel, recheck, and no-sign-in access passed")
    finally:
        parent.taskman._collection_executor.shutdown()
        parent.taskman._no_collection_executor.shutdown()
        aqt.mw = None
        col.close()
