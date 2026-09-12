import importlib.util
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import support
from gui_safety import offscreen_test_application


@unittest.skipUnless(importlib.util.find_spec("aqt"), "Run with Anki's Python to check Qt widgets")
class SettingsWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = offscreen_test_application([])

    def test_sign_in_saves_preferences_and_opens_the_sdk_window(self):
        from aqt.qt import QPushButton
        from shirabe_addon.settings import SettingsDialog
        calls = []
        dialog = SettingsDialog(None, {}, lambda _: calls.append("save"), lambda: None,
                                lambda: calls.append("sign_in"))
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "Sign in").click()
        self.assertEqual(calls, ["save", "sign_in"])
        dialog.deleteLater()

    def test_pending_approval_has_a_continue_button_not_a_connected_label(self):
        from aqt.qt import QLabel, QPushButton
        from shirabe_addon.settings import SettingsDialog
        config = {"cardToken": json.dumps({"shirabe.pending": json.dumps({"expiresAt": time.time() * 1000 + 60000})})}
        dialog = SettingsDialog(None, config, lambda _: None, lambda: None, lambda: None)
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertIn("<b>Waiting for approval</b>", labels)
        self.assertNotIn("<b>Connected</b>", labels)
        self.assertIn("Continue sign-in", [button.text() for button in dialog.findChildren(QPushButton)])
        dialog.deleteLater()

    def test_opening_note_settings_saves_and_closes_the_old_preferences(self):
        from aqt.qt import QPushButton, QDialog
        from shirabe_addon.settings import SettingsDialog
        calls = []
        dialog = SettingsDialog(None, {}, lambda _: calls.append("save"), lambda: None,
                                lambda: None, on_note_settings=lambda: calls.append("mapping"))
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "Note fields, deck and tags…").click()
        self.assertEqual(calls, ["save", "mapping"])
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.deleteLater()

    def test_advanced_settings_expand_without_a_qt_callback_error(self):
        from aqt.qt import QGroupBox
        from shirabe_addon.settings import SettingsDialog
        dialog = SettingsDialog(None, {}, lambda _: None, lambda: None, lambda: None)
        advanced = next(box for box in dialog.findChildren(QGroupBox) if box.title() == "Advanced")
        with patch("sys.excepthook") as errors:
            advanced.setChecked(True)
            advanced.setChecked(False)
        errors.assert_not_called()
        dialog.deleteLater()

    def test_connected_account_can_reconnect_without_manually_removing_its_session(self):
        from aqt.qt import QPushButton
        from shirabe_addon.settings import SettingsDialog
        config = {"cardToken": json.dumps({"shirabe.link": json.dumps({"access": "expired"})})}
        calls = []
        dialog = SettingsDialog(None, config, lambda _: calls.append("save"), lambda: calls.append("out"),
                                lambda: calls.append("reconnect"))
        button = next((b for b in dialog.findChildren(QPushButton) if b.text() == "Reconnect…"), None)
        self.assertIsNotNone(button)
        button.click()
        self.assertEqual(calls, ["save", "reconnect"])
        dialog.deleteLater()

    def test_large_text_settings_fit_the_screen_and_keep_actions_visible(self):
        from aqt.qt import QCheckBox, QDialogButtonBox, QFont, QGroupBox, QLabel, QPoint, QScrollArea
        from shirabe_addon.settings import SettingsDialog
        original = self.app.font()
        screenshots = os.environ.get("SHIRABE_SETTINGS_SCREENSHOTS")
        if screenshots:
            Path(screenshots).mkdir(parents=True, exist_ok=True)
        try:
            connected = {"cardToken": json.dumps({"shirabe.link": json.dumps({"access": "current"})})}
            for font_size, config, name in ((13, {}, "default"), (20, connected, "connected"),
                                            (28, {"apiBase": "https://preview.example"}, "advanced")):
                with self.subTest(font_size=font_size, state=name):
                    self.app.setFont(QFont("Arial", font_size))
                    dialog = SettingsDialog(None, config, lambda _: None, lambda: None, lambda: None,
                                            on_note_settings=lambda: None)
                    dialog.show()
                    self.app.processEvents()
                    screen = dialog.screen().availableGeometry()
                    self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
                    self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
                    content = dialog.findChild(QScrollArea)
                    self.assertIsNotNone(content)
                    self.assertEqual(content.horizontalScrollBar().maximum(), 0)
                    buttons = dialog.findChild(QDialogButtonBox)
                    for button in buttons.buttons():
                        top_left = button.mapToGlobal(QPoint(0, 0))
                        bottom_right = top_left + QPoint(button.width() - 1, button.height() - 1)
                        self.assertTrue(screen.contains(top_left) and screen.contains(bottom_right), button.text())
                        self.assertGreaterEqual(button.width(), button.minimumSizeHint().width())
                    for label in dialog.findChildren(QLabel):
                        if label.isVisible() and label.wordWrap():
                            self.assertGreaterEqual(label.height() + 2, label.heightForWidth(label.width()), label.text())
                    for box in dialog.findChildren(QCheckBox):
                        self.assertGreaterEqual(box.width(), box.minimumSizeHint().width(), box.text())
                    if screenshots:
                        self.assertTrue(dialog.grab().save(str(Path(screenshots) / f"settings-{font_size}pt-{name}.png")))
                    if name == "advanced":
                        content.ensureWidgetVisible(dialog.api_base)
                        self.app.processEvents()
                        advanced = dialog.findChild(QGroupBox)
                        server = next(label for label in dialog.findChildren(QLabel) if label.text() == "Server")
                        self.assertGreaterEqual(server.mapTo(advanced, QPoint(0, 0)).y(), advanced.fontMetrics().height())
                        top_left = dialog.api_base.mapTo(content.viewport(), QPoint(0, 0))
                        bottom_right = top_left + QPoint(dialog.api_base.width() - 1, dialog.api_base.height() - 1)
                        self.assertTrue(content.viewport().rect().contains(top_left))
                        self.assertTrue(content.viewport().rect().contains(bottom_right))
                        if screenshots:
                            self.assertTrue(dialog.grab().save(str(Path(screenshots) / "settings-28pt-advanced-bottom.png")))
                    dialog.close()
                    dialog.deleteLater()
        finally:
            self.app.setFont(original)
