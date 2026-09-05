import importlib.util
import json
import time
import unittest

import support


@unittest.skipUnless(importlib.util.find_spec("aqt"), "Run with Anki's Python to check Qt widgets")
class SettingsWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aqt.qt import QApplication
        cls.app = QApplication.instance() or QApplication([])

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
