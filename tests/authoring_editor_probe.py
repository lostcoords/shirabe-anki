"""Exercise Anki's save callback across delayed WebEngine Editor startup."""

import argparse
import json
from types import SimpleNamespace
from unittest.mock import patch

from gui_safety import gui_arguments

gui_arguments(argparse.ArgumentParser(description=__doc__), allow_offscreen=True)

import support
from aqt.editor import Editor
from aqt.qt import QApplication, QCoreApplication, QEvent, QEventLoop, QTimer, QWidget
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView

from shirabe_addon import authoring_ui
from shirabe_addon.authoring import FieldProfile


app = QApplication(["shirabe-editor-ready-test"])
errors = []


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            errors.append(message)


class Web(QWebEngineView):
    def evalWithCallback(self, script, callback):
        self.page().runJavaScript(script, callback)


def until(predicate):
    loop = QEventLoop()
    poll = QTimer()
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    poll.start(10)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    poll.stop()
    assert predicate(), "Timed out waiting for the Editor callback"


def js(web, script):
    results = []
    web.evalWithCallback(script, results.append)
    until(lambda: bool(results))
    return results[0]


def fixture():
    window = QWidget()
    window.web = Web(window)
    window.web.setPage(Page(window.web))
    loaded = []
    window.web.loadFinished.connect(loaded.append)
    window.web.setHtml("<main>Editor loading</main><script>window.saves = 0;</script>")
    until(lambda: bool(loaded))
    window.editor = SimpleNamespace(note=SimpleNamespace(id=0, fields=[""]), web=window.web, parentWindow=window)
    window.editor.call_after_note_saved = lambda callback: Editor.call_after_note_saved(window.editor, callback)
    return window


window = fixture()
filled, closed, failures = [], [], []
profile = FieldProfile("Japanese", 1, {"expression": "Front"})
note = SimpleNamespace(id=0, fields=["猫"])
window.set_note = lambda note, **_: (filled.append(note), setattr(window.editor, "note", note))
completion = authoring_ui._Completion(window, lambda: closed.append(True), failures.append)
with patch.object(authoring_ui.dialogs, "open", return_value=window):
    authoring_ui._open_add_draft(None, note, profile, completion, preview=None, config={}, save_config=lambda _: None)
    js(window.web, "true")
    assert not errors, errors
    assert not filled and not closed, "Fresh Add draft was accessed before the Editor loaded"
    js(window.web, "window.getNoteId = () => 0; window.saveNow = () => ++window.saves; true")
    until(lambda: bool(closed))
    assert filled == [note] and len(closed) == 1 and not failures, (filled, closed, failures)
    assert js(window.web, "window.saves") == 1

window.deleteLater()
app.processEvents()
window = fixture()
closed, failures = [], []
completion = authoring_ui._Completion(window, lambda: closed.append(True), failures.append)
with patch.object(authoring_ui.dialogs, "open", return_value=window):
    authoring_ui._open_add_draft(None, note, profile, completion, preview=None, config={}, save_config=lambda _: None)
    js(window.web, "true")
    window.editor.note = None
    window.web.deleteLater()
    until(lambda: bool(closed))
    assert len(closed) == 1 and not failures, (closed, failures)

window.deleteLater()
app.processEvents()
window = fixture()
window.editor.note.fields = ["My unfinished draft"]
filled, closed, warnings = [], [], []
window.set_note = lambda *_, **__: filled.append(True)
completion = authoring_ui._Completion(window, lambda: closed.append(True), failures.append)
with patch.object(authoring_ui.dialogs, "open", return_value=window), patch.object(authoring_ui, "showWarning", lambda text, **_: warnings.append(text)):
    authoring_ui._open_add_draft(None, note, profile, completion, preview=None, config={}, save_config=lambda _: None)
    js(window.web, "window.getNoteId = () => 0; window.saveNow = () => ++window.saves; true")
    until(lambda: bool(closed))
    assert not filled and warnings and window.editor.note.fields == ["My unfinished draft"]
    assert js(window.web, "window.saves") == 1

window.deleteLater()
app.processEvents()
window = fixture()
selections = []
window.web.selectedText = lambda: (selections.append(True), "猫")[1]
lookup = authoring_ui.EditorLookupDialog(window.editor, {}, lambda _: None)
js(window.web, "true")
assert not selections and not lookup.query.text(), "Selection was read before the Editor loaded"
js(window.web, "window.getNoteId = () => 0; window.saveNow = () => ++window.saves; true")
until(lambda: bool(selections))
assert lookup.query.text() == "猫"
lookup.close()
window.deleteLater()
app.processEvents()

window = fixture()
closed, failures = [], []
completion = authoring_ui._Completion(window, lambda: closed.append(True), failures.append)
with patch.object(authoring_ui.dialogs, "open", return_value=window), patch.object(authoring_ui, "monotonic", return_value=0):
    authoring_ui._open_add_draft(None, note, profile, completion, preview=None, config={}, save_config=lambda _: None)
    js(window.web, "true")
with patch.object(authoring_ui, "monotonic", return_value=16):
    until(lambda: bool(closed))
assert len(closed) == 1 and len(failures) == 1 and "still loading" in str(failures[0])
window.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
assert not errors, errors
print(json.dumps({"webengine": True, "nativeSave": True, "deferredAdd": True, "preservesDraft": True,
                  "deferredSelection": True, "closeReleasesGate": True, "boundedWait": True, "consoleErrors": errors}))
