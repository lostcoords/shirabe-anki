#!/usr/bin/env python3

import argparse
import os
from pathlib import Path
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from gui_safety import offscreen_test_application
from aqt.qt import QFont, QLabel, QPushButton, QWidget
from shirabe_addon import offline_ui


class Browser(QWidget):
    def __init__(self, app):
        super().__init__()
        self.mw = self
        self.app = app
        self.col = object()

    def selected_notes(self):
        return [1]


class Query:
    def __init__(self, *args, **kwargs):
        pass

    def without_collection(self):
        return self

    def failure(self, callback):
        return self

    def run_in_background(self):
        return self


class Web(QWidget):
    def cleanup(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    app = offscreen_test_application(["shirabe-progress-ui-probe"])
    app.setFont(QFont("Arial", 28))
    browser = Browser(app)
    backend = type("Backend", (), {})()
    with patch.object(offline_ui.offline, "DictionaryBackend", return_value=backend), \
            patch("aqt.operations.QueryOp", Query), patch("aqt.webview.AnkiWebView", Web):
        offline_ui.prepare_selected(browser, {}, lambda _: None)
    workflow = browser._shirabe_offline
    app.processEvents()
    available = workflow.screen().availableGeometry()
    assert workflow.frameGeometry().width() <= available.width()
    assert workflow.frameGeometry().height() <= available.height()
    label = workflow.findChild(QLabel)
    assert label.wordWrap()
    assert label.height() >= label.heightForWidth(label.width())
    stop = workflow.findChild(QPushButton)
    assert stop.isVisible()
    assert stop.y() >= 0 and stop.y() + stop.height() <= workflow.height()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        assert workflow.grab().save(str(args.output))
    workflow.close_work()
    browser.close()
    print("offline progress UI passed")


if __name__ == "__main__":
    main()
