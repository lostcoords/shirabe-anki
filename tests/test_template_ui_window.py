from copy import deepcopy
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from gui_safety import offscreen_test_application


@unittest.skipUnless(importlib.util.find_spec('aqt'), "Run with Anki's bundled Python for native Qt")
class TemplateWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = offscreen_test_application(['shirabe-template-tests'])

    def setUp(self):
        from shirabe_addon import template_ui
        self.ui = template_ui
        self.targets = tuple(template_ui.TemplateTarget({
            'id': mid, 'name': name,
            'flds': [{'name': field} for field in ('Expression', 'Meaning', 'Sentence', 'Notes')],
            'tmpls': [{'name': 'Recognition', 'ord': 0, 'qfmt': '{{Expression}}', 'afmt': '{{Meaning}} {{Sentence}} {{Notes}}'}],
        }, None) for mid, name in ((1, 'Japanese vocabulary'), (2, 'Japanese sentences')))
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def setup_dialog(self, config=None):
        window = self.ui.build_install_dialog(None, self.targets, config or {})
        self.windows.append(window)
        return window

    def test_default_is_answer_only_and_online_permission_is_not_inherited(self):
        dialog = self.setup_dialog({'answerOnly': False, 'onlineParsing': True})
        self.assertTrue(dialog.options.answer_only)
        self.assertFalse(dialog.options.online_parsing)
        self.assertEqual(dialog.options.excluded_fields, ())
        self.assertIn('Sends visible Japanese text to https://shirabe.org', dialog.online.accessibleDescription())
        self.assertEqual(dialog.note_type.accessibleName(), 'Note type')
        self.assertEqual(dialog.fields.accessibleName(), 'Fields excluded from lookup')

    def test_field_choices_and_permission_belong_to_each_note_type(self):
        from aqt.qt import Qt
        dialog = self.setup_dialog()
        dialog.fields.item(3).setCheckState(Qt.CheckState.Checked)
        dialog.online.setChecked(True)
        dialog.side.setCurrentIndex(1)
        dialog.note_type.setCurrentIndex(1)
        self.assertEqual(dialog.options, self.ui.TemplateOptions())
        dialog.fields.item(1).setCheckState(Qt.CheckState.Checked)
        dialog.note_type.setCurrentIndex(0)
        self.assertEqual(dialog.options, self.ui.TemplateOptions(False, True, ('Notes',)))
        dialog.note_type.setCurrentIndex(1)
        self.assertEqual(dialog.options, self.ui.TemplateOptions(excluded_fields=('Meaning',)))

    def test_saved_preferences_load_for_only_their_note_type(self):
        target = self.targets[0]
        self.targets = (self.ui.TemplateTarget(target.model, {'answerOnly': False, 'onlineParsing': True, 'excludedFields': ['Notes']}), self.targets[1])
        dialog = self.setup_dialog()
        self.assertEqual(dialog.options, self.ui.TemplateOptions(False, True, ('Notes',)))
        dialog.note_type.setCurrentIndex(1)
        self.assertEqual(dialog.options, self.ui.TemplateOptions())

    def test_unsafe_template_is_explained_in_the_same_dialog(self):
        from aqt.qt import QDialog, Qt
        model = deepcopy(self.targets[0].model)
        model['tmpls'][0]['afmt'] = '<img src="{{Meaning}}">'
        self.targets = (self.ui.TemplateTarget(model, None),)
        dialog = self.setup_dialog()
        dialog.fields.item(1).setCheckState(Qt.CheckState.Checked)
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.error.isHidden())
        self.assertIn('Meaning', dialog.error.text())

    def test_remove_requires_checked_targets_and_cancel_preserves_selection_snapshot(self):
        from aqt.qt import QDialog, Qt
        before = deepcopy(self.targets)
        dialog = self.ui.build_remove_dialog(None, self.targets)
        self.windows.append(dialog)
        self.assertFalse(dialog.remove.isEnabled())
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.note_types.item(1).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(dialog.targets, (self.targets[1],))
        self.assertTrue(dialog.remove.isEnabled())
        dialog.reject()
        self.assertEqual(self.targets, before)

    def test_installed_anki_light_dark_and_large_text_screenshots(self):
        import aqt
        from aqt.profiles import Theme, WidgetStyle
        from aqt.qt import QFont, QLabel, QScrollArea, QStyleFactory, Qt
        from aqt.theme import ThemeManager

        artifact_dir = os.environ.get('SHIRABE_TEMPLATE_SCREENSHOTS')
        if artifact_dir:
            Path(artifact_dir).mkdir(parents=True, exist_ok=True)
        original_palette = self.app.palette()
        original_stylesheet = self.app.styleSheet()
        original_style = self.app.style().objectName()
        original_font = self.app.font()
        try:
            for dark, large in ((False, False), (True, False), (False, True)):
                manager = ThemeManager()
                profile = SimpleNamespace(theme=lambda: Theme.DARK if dark else Theme.LIGHT,
                                          get_widget_style=lambda: WidgetStyle.ANKI)
                with patch.object(aqt, 'mw', SimpleNamespace(app=self.app, pm=profile)):
                    manager.apply_style()
                font = QFont(original_font)
                if large:
                    font.setPointSize(28)
                self.app.setFont(font)
                setup = self.setup_dialog()
                setup.fields.item(3).setCheckState(Qt.CheckState.Checked)
                remove = self.ui.build_remove_dialog(None, self.targets)
                self.windows.append(remove)
                remove.note_types.item(0).setCheckState(Qt.CheckState.Checked)
                for name, window in (('install', setup), ('remove', remove)):
                    window.show()
                    self.app.processEvents()
                    shot = window.grab()
                    self.assertFalse(shot.isNull())
                    if artifact_dir:
                        variant = 'large' if large else 'dark' if dark else 'light'
                        self.assertTrue(shot.save(str(Path(artifact_dir) / f'{name}-{variant}.png')))
                    self.assertLess(window.width(), 900)
                    self.assertLess(window.height(), 900)
                    available = window.screen().availableGeometry()
                    self.assertLessEqual(window.frameGeometry().height(), available.height())
                    self.assertLessEqual(window.frameGeometry().width(), available.width())
                    for scroll in window.findChildren(QScrollArea):
                        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    for label in window.findChildren(QLabel):
                        if not label.isVisible():
                            continue
                        required = label.heightForWidth(label.width())
                        self.assertGreaterEqual(label.height(), required, label.text())
                    window.hide()
        finally:
            self.app.setFont(original_font)
            self.app.setStyle(QStyleFactory.create(original_style))
            self.app.setPalette(original_palette)
            self.app.setStyleSheet(original_stylesheet)


if __name__ == '__main__':
    unittest.main()
