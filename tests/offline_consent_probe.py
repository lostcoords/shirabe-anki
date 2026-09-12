"""Render only the consent dialog, without an Anki profile, collection or network."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

from gui_safety import gui_arguments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--font-sizes", type=int, nargs="+", default=[13, 20, 28])
    args = gui_arguments(parser, allow_offscreen=True)
    import support
    from shirabe_addon import offline, offline_ui
    from anki.buildinfo import version
    from aqt.qt import QApplication, QCheckBox, QDialogButtonBox, QFont, QLabel, QPoint, QPushButton, QScrollArea, QSpinBox, QTimer, QToolButton, qVersion
    output = args.output or Path(tempfile.mkdtemp(prefix="shirabe-offline-consent-"))
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication(["shirabe-isolated-consent-probe"])
    report = {"version": version, "qt": qVersion(), "platform": app.platformName(), "screenshots": [], "failures": [], "cases": []}
    exceptions = []
    def failed(kind, error, trace):
        exceptions.append(str(error))
        sys.__excepthook__(kind, error, trace)
        dialog = app.activeModalWidget()
        if dialog:
            dialog.reject()
    sys.excepthook = failed

    for font_size in args.font_sizes:
        app.setFont(QFont("Arial", font_size))
        for expanded in (False, True):
            case = {"font_pt": font_size, "expanded": expanded}
            def inspect(case=case):
                dialog = app.activeModalWidget()
                app.processEvents()
                screen = dialog.screen().availableGeometry()
                failures = []
                if dialog.frameGeometry().height() > screen.height() or dialog.frameGeometry().width() > screen.width():
                    failures.append("Dialog exceeds available screen")
                buttons = dialog.findChild(QDialogButtonBox)
                for scroll in dialog.findChildren(QScrollArea):
                    if scroll.parentWidget() == dialog and scroll.geometry().intersects(buttons.geometry()):
                        failures.append("Dictionary area overlaps action buttons")
                    if scroll.horizontalScrollBar().maximum():
                        failures.append("Consent text requires horizontal scrolling")
                start = buttons.button(QDialogButtonBox.StandardButton.Ok)
                cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
                for button in (start, cancel):
                    position = button.mapToGlobal(QPoint(0, 0))
                    if not screen.contains(position) or not screen.contains(position + QPoint(button.width() - 1, button.height() - 1)):
                        failures.append(f"Button outside available screen: {button.text()}")
                    if button.width() < button.minimumSizeHint().width():
                        failures.append(f"Clipped button: {button.text()}")
                for label in dialog.findChildren(QLabel):
                    if label.isVisible() and label.wordWrap() and label.height() + 2 < label.heightForWidth(label.width()):
                        failures.append(f"Clipped label: {label.text()[:70]}")
                boxes = dialog.findChildren(QCheckBox)
                for box in boxes:
                    if box.width() < box.minimumSizeHint().width():
                        failures.append(f"Clipped checkbox: {box.text()}")
                permission = next(box for box in boxes if box.text() == "Send visible card text to Shirabe")
                audio = next(box for box in boxes if box.text() == "Include Shirabe pronunciation")
                examples = next(box for box in boxes if box.text() == "Include Tatoeba examples")
                official = next(box for box in boxes if box.text().startswith("JMdict"))
                uploaded = next(box for box in boxes if box.text().startswith("My dictionary"))
                assert not permission.isChecked() and not audio.isChecked() and official.isChecked() and not uploaded.isChecked()
                assert not examples.isChecked() and examples.isEnabled()
                assert not start.isEnabled()
                limit = dialog.findChild(QSpinBox)
                assert limit.value() == 25 and limit.maximum() == 256
                copy = "\n".join(label.text() for label in dialog.findChildren(QLabel))
                assert "both sides" in copy and "cloze answers" in copy and "Anyone receiving the deck" in copy
                assert "Shirabe permits" in copy and "1 KiB in 1 file" in copy
                assert "Only selected-card excerpts, not full dictionaries." in copy and "CC BY 2.0 FR" in copy
                image = output / f"consent-{version}-{case['font_pt']}pt-{'expanded' if case['expanded'] else 'collapsed'}.png"
                assert dialog.grab().save(str(image))
                case.update({"size": [dialog.width(), dialog.height()], "screen": [screen.width(), screen.height()], "failures": failures,
                             "layout": [{"type": w.metaObject().className(), "text": w.text() if hasattr(w, "text") else "",
                                         "geometry": [w.x(), w.y(), w.width(), w.height()],
                                         "hint": [w.sizeHint().width(), w.sizeHint().height()],
                                         "height_for_width": w.heightForWidth(w.width())}
                                        for w in dialog.findChildren((QLabel, QDialogButtonBox, QScrollArea, QCheckBox, QPushButton, QSpinBox, QToolButton)) if w.isVisible()]})
                report["cases"].append(case)
                report["failures"].extend(f"{case['font_pt']}pt expanded={case['expanded']}: {failure}" for failure in failures)
                report["screenshots"].append(str(image))
                content = next((scroll for scroll in dialog.findChildren(QScrollArea) if scroll.parentWidget() == dialog), None)
                dictionary_scroll = dialog.findChild(QScrollArea, "shirabe-dictionaries")
                if content and content.verticalScrollBar().maximum():
                    content.ensureWidgetVisible(dictionary_scroll)
                    content.horizontalScrollBar().setValue(content.horizontalScrollBar().maximum())
                    dictionary_scroll.verticalScrollBar().setValue(dictionary_scroll.verticalScrollBar().maximum())
                    app.processEvents()
                    bottom = image.with_stem(image.stem + "-bottom")
                    assert dialog.grab().save(str(bottom))
                    report["screenshots"].append(str(bottom))
                    last_notice = next(label for label in dictionary_scroll.findChildren(QLabel) if label.text().startswith("Select only if"))
                    for widget in (uploaded, last_notice):
                        position = widget.mapTo(content.viewport(), QPoint(0, 0))
                        assert content.viewport().rect().contains(position + QPoint(widget.width() - 1, widget.height() - 1)), "Dictionary permission must be reachable by scrolling"
                permission.setChecked(True)
                assert start.isEnabled()
                audio.setChecked(True)
                examples.setChecked(True)
                start.click()
            def shown(expanded=expanded):
                dialog = app.activeModalWidget()
                if expanded:
                    dialog.findChild(QToolButton).click()
                QTimer.singleShot(150, inspect)
            QTimer.singleShot(150, shown)
            chosen = offline_ui._consent(None, 2, [
                {"slug": "jmdict", "name": "JMdict", "visibility": "official", "entryCount": 200000,
                 "source": {"license": "CC BY-SA 4.0", "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/"}},
                {"slug": "uploaded", "name": "My dictionary", "visibility": "personal"},
                {"slug": "tatoeba", "name": "Tatoeba", "type": "sentence", "visibility": "official",
                 "source": {"url": "https://tatoeba.org", "license": "CC BY 2.0 FR", "licenseUrl": "https://creativecommons.org/licenses/by/2.0/fr/"}},
            ], offline.SnapshotStorage(1024, 1), {})
            if chosen != ({"jmdict"}, 25, True, True):
                report["failures"].append(f"Unexpected consent selection: {chosen}")
    report["failures"].extend(exceptions)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report, "output": str(output)}))
    return bool(report["failures"])


if __name__ == "__main__":
    raise SystemExit(main())
