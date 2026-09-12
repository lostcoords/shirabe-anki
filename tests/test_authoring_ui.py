import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import support
import test_authoring_native
from gui_safety import offscreen_test_application

from shirabe_addon.authoring import Example, FieldProfile, Lookup, NoteChoices, ROLE_ORDER, remember_profile


@unittest.skipUnless(importlib.util.find_spec("aqt"), "Run with Anki's Python to check native authoring dialogs")
class AuthoringDialogs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = offscreen_test_application([])

    def setUp(self):
        from aqt.qt import QWidget
        self.fixture = test_authoring_native.NativeAuthoring()
        self.fixture.setUp()
        self.mw = QWidget()
        self.mw.col = self.fixture.col
        self.mw.app = self.app

    def tearDown(self):
        self.mw.close()
        self.mw.deleteLater()
        self.app.processEvents()
        self.fixture.tearDown()

    def capture(self, dialog, name):
        dialog.show()
        self.app.processEvents()
        directory = os.environ.get("SHIRABE_AUTHORING_SCREENSHOTS")
        if directory:
            dialog.grab().save(str(Path(directory) / name))

    def test_mapping_suggests_familiar_fields_in_learning_order_without_saving(self):
        from shirabe_addon.authoring_ui import MappingDialog
        dialog = MappingDialog(self.mw, {}, note_type_id=self.fixture.model["id"])
        self.assertEqual(tuple(dialog.fields), ROLE_ORDER)
        self.assertEqual(dialog.fields["expression"].currentData(), "Front")
        self.assertEqual(dialog.fields["meanings"].currentData(), "Back")
        self.assertEqual(dialog.fields["reading"].currentData(), "Reading")
        self.assertIsNone(dialog.profile)
        self.capture(dialog, "mapping.png")
        dialog.close()

    def test_loading_a_custom_mapping_preserves_its_choices_including_unmapped_fields(self):
        from shirabe_addon.authoring_ui import MappingDialog
        profile = FieldProfile("My fields", self.fixture.model["id"], {"expression": "Other", "meanings": "Sentence"})
        dialog = MappingDialog(self.mw, remember_profile({}, profile))
        dialog.saved.setCurrentIndex(1)
        self.assertEqual(dialog.fields["expression"].currentData(), "Other")
        self.assertEqual(dialog.fields["meanings"].currentData(), "Sentence")
        self.assertIsNone(dialog.fields["reading"].currentData())
        dialog.close()

    def test_stale_mapping_explains_which_fields_need_repair(self):
        from aqt.qt import QLabel
        from shirabe_addon.authoring_ui import MappingDialog
        stale = FieldProfile("My fields", self.fixture.model["id"], {"expression": "Gone"})
        dialog = MappingDialog(self.mw, remember_profile({}, stale))
        dialog.saved.setCurrentIndex(1)
        self.assertTrue(any("Gone" in label.text() for label in dialog.findChildren(QLabel)))
        self.capture(dialog, "mapping-repair.png")
        dialog.close()

    def test_choosing_a_deleted_profile_opens_native_repair(self):
        from shirabe_addon import authoring_ui
        stale = FieldProfile("Deleted", 999999, {"expression": "Front"})
        config = remember_profile({}, stale)
        with patch.object(authoring_ui, "configure_profiles", return_value=self.fixture.profile) as repair:
            chosen = authoring_ui.choose_profile(self.mw, config, lambda _: None)
        self.assertEqual(chosen, self.fixture.profile)
        self.assertEqual(repair.call_args.kwargs["profile"], stale)

    def test_one_material_dialog_contains_the_actual_field_preview_and_example_choices(self):
        from shirabe_addon.authoring_ui import ChoiceDialog
        value = Lookup(surface="食べました", lemma="食べる", reading="たべる", entry="jmdict:1358280",
                       meanings=("to eat", "to live on"), sentence="昨日、寿司を食べました。",
                       examples=(Example("一緒に食べましょう。", "Let's eat together.", "Tatoeba", "tatoeba:123"),))
        note = self.fixture.col.new_note(self.fixture.model)
        dialog = ChoiceDialog(self.mw, value, self.fixture.profile, self.fixture.model, note=note, config={})
        self.assertEqual(dialog.preview.field_values()["Front"], "食べる")
        self.assertTrue(dialog.example_options.isHidden())
        dialog.headword.setCurrentIndex(dialog.headword.findData("surface"))
        self.assertEqual(dialog.preview.field_values()["Front"], "食べました")
        dialog.example.setCurrentIndex(dialog.example.findData(0))
        self.assertEqual(dialog.preview.field_values()["Sentence"], "一緒に食べましょう。")
        self.assertIn("Tatoeba", dialog.preview.source)
        self.assertTrue(dialog.number.isHidden())
        self.assertEqual(dialog.fields.table.horizontalHeaderItem(3).text(), "After")
        meaning_row = list(self.fixture.profile.fields).index("meanings")
        self.assertEqual(dialog.fields.table.item(meaning_row, 3).text(), "to eat\nto live on")
        dialog.fields.show_html.setChecked(True)
        self.assertEqual(dialog.fields.table.item(meaning_row, 3).text(), "to eat<br>to live on")
        dialog.fields.show_html.setChecked(False)
        self.capture(dialog, "material-and-fields.png")
        dialog.close()

    def test_qt_cloze_offsets_account_for_emoji(self):
        from shirabe_addon.authoring_ui import utf16_span
        self.assertEqual(utf16_span("😀猫です。", 2, 3), (1, 2))
        with self.assertRaises(ValueError):
            utf16_span("😀猫です。", 1, 3)

    def test_material_stays_compact_and_attribution_wraps_when_resized(self):
        from aqt.qt import QFont, QPushButton, QScrollArea
        from shirabe_addon.authoring_ui import ChoiceDialog
        value = Lookup(lemma="猫", reading="ねこ", meanings=("cat",), source="Current reading",
                       examples=(Example("猫が窓の外を見ています。", "A cat is looking out the window.",
                                         "A corpus source with a long title that needs wrapping in a narrow window.",
                                         "tatoeba:fixture-1"),))
        old_font = self.app.font()
        try:
            for font_size in (13, 20):
                self.app.setFont(QFont("Arial", font_size))
                for note in (None, self.fixture.col.new_note(self.fixture.model)):
                    dialog = ChoiceDialog(self.mw, value, self.fixture.profile, self.fixture.model, note=note, config={})
                    dialog.show()
                    more = next(button for button in dialog.findChildren(QPushButton) if button.text().startswith("Find more examples"))
                    more.setChecked(True)
                    self.app.processEvents()
                    dialog.example.setCurrentIndex(dialog.example.findData(0))
                    dialog.example_status.setText("Found 1 new example. Choose a sentence above.")
                    dialog.example_status.show()
                    more.setChecked(False)
                    for width, height in ((560, 480), (740, 640), (1000, 900)):
                        with self.subTest(font_size=font_size, fields=note is not None, width=width):
                            dialog.resize(width, height)
                            self.app.processEvents()
                            self.assertLessEqual(dialog.example_status.y() - more.geometry().bottom(), 20)
                            for label in (dialog.example_status, dialog.translation, dialog.source):
                                required = label.heightForWidth(label.width())
                                self.assertGreaterEqual(label.height(), required, label.text())
                                self.assertLessEqual(label.height(), required + 2, label.text())
                            self.assertEqual(dialog.findChild(QScrollArea).horizontalScrollBar().maximum(), 0)
                    dialog.close()
        finally:
            self.app.setFont(old_font)

    def test_native_editor_save_waits_for_async_ui_and_closing_releases_gate(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("authoring_editor_probe.py"))],
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"consoleErrors": []', result.stdout)

    def test_pending_update_keeps_gate_until_native_cancel_and_closes_once(self):
        from shirabe_addon import authoring_ui
        from aqt.qt import QDialog
        note = self.fixture.add()
        operations, events = [], []

        class Query:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def failure(self, callback):
                self.failed = callback
                return self
            def run_in_background(self):
                operations.append(self)

        with patch.object(authoring_ui, "QueryOp", Query), patch.object(authoring_ui, "ChoiceDialog") as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            authoring_ui.preview_update(self.mw, note.id, self.fixture.lookup, self.fixture.profile,
                                        on_closed=lambda: events.append("closed"), on_error=lambda e: events.append("error"))
            self.assertEqual(events, [])
            operation = operations[0]
            operation.kwargs["success"](operation.kwargs["op"](self.fixture.col))
            self.assertEqual(events, ["closed"])
            operation.failed(RuntimeError("late failure"))
            self.assertEqual(events.count("closed"), 1)

    def test_missing_credentials_offer_connect_without_starting_lookup(self):
        from types import SimpleNamespace
        from aqt.qt import QWidget
        from shirabe_addon.authoring_ui import EditorLookupDialog
        web = QWidget(self.mw)
        web.selectedText = lambda: "猫"
        web.evalWithCallback = lambda _, callback: callback(True)
        editor = SimpleNamespace(parentWindow=self.mw, mw=self.mw, note=self.fixture.add(),
                                 web=web)
        dialog = EditorLookupDialog(editor, {}, lambda _: None)
        with patch("shirabe_addon.authoring_ui.QueryOp") as query:
            dialog.search_word()
        query.assert_not_called()
        self.assertFalse(dialog.connect.isHidden())
        self.assertEqual(dialog.status.text(), "Connect to Shirabe, then search again.")
        dialog.close()

    def test_editor_lookup_keeps_long_definitions_reachable_with_large_text(self):
        from types import SimpleNamespace
        from aqt.qt import QDialogButtonBox, QFont, QLabel, QPoint, QScrollArea, QWidget
        from shirabe_addon.authoring_ui import EditorLookupDialog
        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 28))
            web = QWidget(self.mw)
            web.selectedText = lambda: "猫"
            web.evalWithCallback = lambda _, callback: callback(True)
            editor = SimpleNamespace(parentWindow=self.mw, mw=self.mw, note=self.fixture.add(), web=web)
            dialog = EditorLookupDialog(editor, {}, lambda _: None)
            meaning = "A long dictionary meaning that remains readable instead of being clipped at the bottom of the window."
            candidate = Lookup(lemma="猫", reading="ねこ", entry="jmdict:1", meanings=(meaning,) * 4)
            dialog.received(0, (candidate,))
            dialog.results.setCurrentIndex(1)
            dialog.show()
            self.app.processEvents()
            self.capture(dialog, "editor-lookup-large.png")
            screen = dialog.screen().availableGeometry()
            self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
            self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
            content = dialog.findChild(QScrollArea)
            self.assertIsNotNone(content)
            self.assertEqual(content.horizontalScrollBar().maximum(), 0)
            for label in dialog.findChildren(QLabel):
                if label.isVisible() and label.wordWrap():
                    self.assertGreaterEqual(label.height() + 2, label.heightForWidth(label.width()), label.text())
            content.ensureWidgetVisible(dialog.use)
            self.app.processEvents()
            use_top = dialog.use.mapTo(content.viewport(), QPoint(0, 0))
            use_bottom = use_top + QPoint(dialog.use.width() - 1, dialog.use.height() - 1)
            self.assertTrue(content.viewport().rect().contains(use_top))
            self.assertTrue(content.viewport().rect().contains(use_bottom))
            close = dialog.findChild(QDialogButtonBox)
            for button in close.buttons():
                top_left = button.mapToGlobal(QPoint(0, 0))
                bottom_right = top_left + QPoint(button.width() - 1, button.height() - 1)
                self.assertTrue(screen.contains(top_left) and screen.contains(bottom_right), button.text())
            self.capture(dialog, "editor-lookup-large-bottom.png")
            dialog.close()
            dialog.deleteLater()
        finally:
            self.app.setFont(original)

    def test_selected_notes_share_one_field_preview_with_individual_policy_choices(self):
        from shirabe_addon.authoring_batch import Batch
        from shirabe_addon.authoring_ui import BatchPreviewDialog
        one, two = self.fixture.add(Back="my definition"), self.fixture.add()
        batch = Batch()
        batch.rows = batch.capture(self.fixture.col, [one.id, two.id], [self.fixture.profile])
        batch.prepare(lambda _: [self.fixture.lookup])
        dialog = BatchPreviewDialog(self.mw, batch.rows)
        self.assertEqual(len(dialog.previews), 2)
        self.assertEqual(dialog.previews[0].field_values()["Back"], "my definition")
        self.assertEqual(dialog.previews[1].field_values()["Back"], "cat")
        self.capture(dialog, "batch-fields.png")
        dialog.close()

    def test_field_preview_windows_fit_the_screen_with_large_text(self):
        from aqt.qt import QDialogButtonBox, QFont, QPoint
        from shirabe_addon.authoring_batch import Batch
        from shirabe_addon.authoring_ui import BatchPreviewDialog, PreviewDialog

        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 28))
            note = self.fixture.add()
            single = PreviewDialog(self.mw, note, self.fixture.profile, self.fixture.lookup)
            batch = Batch()
            batch.rows = batch.capture(self.fixture.col, [note.id], [self.fixture.profile])
            batch.prepare(lambda _: [self.fixture.lookup])
            multiple = BatchPreviewDialog(self.mw, batch.rows)
            for name, dialog in (("field-preview-large.png", single), ("batch-preview-large.png", multiple)):
                with self.subTest(dialog=dialog.windowTitle()):
                    dialog.show()
                    self.app.processEvents()
                    self.capture(dialog, name)
                    screen = dialog.screen().availableGeometry()
                    self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
                    self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
                    buttons = dialog.findChild(QDialogButtonBox)
                    for button in buttons.buttons():
                        top_left = button.mapToGlobal(QPoint(0, 0))
                        bottom_right = top_left + QPoint(button.width() - 1, button.height() - 1)
                        self.assertTrue(screen.contains(top_left) and screen.contains(bottom_right), button.text())
                        self.assertGreaterEqual(button.width(), button.minimumSizeHint().width())
                    dialog.close()
                    dialog.deleteLater()
        finally:
            self.app.setFont(original)

    def test_browser_enrichment_passes_account_config_to_material_preview(self):
        from shirabe_addon import authoring_ui
        config = remember_profile({"apiBase": "https://dictionary.example", "cardToken": "fixture"}, self.fixture.profile)
        browser = type("Browser", (), {"mw": self.mw})()
        with patch("shirabe_addon.authoring_lookup.resolver_from_config"), patch.object(authoring_ui, "enrich_selected_notes") as enrich:
            authoring_ui.enrich_browser_selection(browser, config, lambda _: None)
        self.assertEqual(enrich.call_args.kwargs.get("config"), config)

    def test_batch_material_corpus_choice_changes_only_the_approved_row(self):
        from aqt.qt import QDialog, QPushButton
        from shirabe_addon import authoring_ui
        from shirabe_addon.authoring_batch import Batch
        one, two, unmatched = self.fixture.add(), self.fixture.add(Back="my definition"), self.fixture.add(Front="犬")
        unmapped = self.fixture.col.new_note(self.fixture.col.models.by_name("Basic"))
        unmapped["Front"] = "鳥"
        self.fixture.col.add_note(unmapped, 1)
        batch = Batch()
        batch.rows = batch.capture(self.fixture.col, [one.id, two.id, unmatched.id, unmapped.id], [self.fixture.profile])
        batch.prepare(lambda identity: [self.fixture.lookup] if identity.expression == "猫" else [])
        self.assertEqual([row.status for row in batch.rows], ["ready", "ready", "unmatched", "unmapped"])
        config = {"apiBase": "https://dictionary.example", "cardToken": "fixture"}
        dialog = authoring_ui.BatchPreviewDialog(self.mw, batch.rows, config=config)
        operations = []

        class Query:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def without_collection(self):
                return self
            def failure(self, callback):
                return self
            def run_in_background(self):
                operations.append(self)

        def choose(material):
            self.assertEqual(material.config, config)
            self.assertFalse(material.lookup.examples)
            more = next(button for button in material.findChildren(QPushButton) if button.text().startswith("Find more examples"))
            more.click()
            examples = (Example("猫がいます。", "There is a cat.", "Corpus", "corpus:1"),)
            with patch("shirabe_addon.authoring_examples.search_examples_from_config", return_value=examples) as search:
                material.search_examples_button.click()
                operations[-1].kwargs["success"](operations[-1].kwargs["op"](None))
            self.assertEqual(search.call_args.args, (config, self.fixture.lookup))
            self.assertEqual(material.sentence.toPlainText(), "")
            material.example.setCurrentIndex(material.example.findData(0))
            material.accept()
            return QDialog.DialogCode.Accepted

        with patch.object(authoring_ui, "QueryOp", Query), patch.object(authoring_ui.ChoiceDialog, "exec", new=choose):
            dialog.choose_material()
        self.assertEqual(dialog.previews[0].field_values()["Sentence"], "猫がいます。")
        self.assertIn("corpus:1", dialog.previews[0].source)
        self.assertEqual(dialog.previews[1].field_values()["Sentence"], "")
        self.assertEqual(dialog.previews[1].field_values()["Back"], "my definition")
        self.assertEqual(self.fixture.col.get_note(one.id)["Sentence"], "")
        self.assertEqual(len(dialog.previews), 2)
        self.capture(dialog, "batch-chosen-example.png")
        dialog.close()

    def test_completed_progress_is_not_user_cancellation(self):
        from aqt.qt import QProgressDialog
        dialog = QProgressDialog("Looking up selected words", "Cancel", 0, 1, self.mw)
        events = []
        dialog.canceled.connect(lambda: events.append("cancelled"))
        dialog.setValue(1)
        dialog.reset()
        self.assertEqual(events, [])

    def test_corpus_results_do_not_replace_the_selected_sentence_or_its_source(self):
        from shirabe_addon import authoring_ui
        note = self.fixture.col.new_note(self.fixture.model)
        value = Lookup(lemma="猫", sentence="私の猫です。", source="My book")
        dialog = authoring_ui.ChoiceDialog(self.mw, value, self.fixture.profile, self.fixture.model, note=note, config={})
        operations = []

        class Query:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def without_collection(self):
                return self
            def failure(self, callback):
                return self
            def run_in_background(self):
                operations.append(self)

        with patch.object(authoring_ui, "QueryOp", Query):
            dialog.search_examples()
            operations[0].kwargs["success"]((Example("猫がいます。", "There is a cat.", "Tatoeba", "tatoeba:1"),))
        self.assertEqual(dialog.sentence.toPlainText(), "私の猫です。")
        self.assertEqual(dialog.source.text(), "My book")
        self.assertEqual(dialog.preview.source, "My book")
        self.assertIn("1 new example", dialog.example_status.text())
        self.assertNotIn("1 new example choices", dialog.example_status.text())
        dialog.close()

    def test_audio_requires_mappings_and_never_uses_card_urls(self):
        from aqt.qt import QPushButton
        from shirabe_addon import authoring_ui
        value = Lookup(lemma="猫", reading="ねこ", entry="jmdict:word:cat-1",
                       audio_urls=("file:///untrusted-card.mp3",))
        profile = FieldProfile("With audio", self.fixture.model["id"], self.fixture.profile.fields | {"audio": "Other"})
        note = self.fixture.add()
        with patch.object(authoring_ui, "QueryOp") as query, patch("shirabe_addon.offline.DictionaryBackend") as backend, \
                patch("shirabe_addon.authoring_media.download_selected_media") as download:
            dialog = authoring_ui.ChoiceDialog(self.mw, value, profile, self.fixture.model, note=note, config={})
            query.assert_not_called()
            backend.assert_not_called()
            download.assert_not_called()
        self.assertIn("Source", dialog.audio_status.text())
        self.assertFalse(dialog.audio_choose.isEnabled())
        for policy in ("fill", "replace", "append", "refresh"):
            box = dialog.fields.actions["audio"]
            box.setCurrentIndex(box.findData(policy))
            self.assertEqual(dialog.preview.field_values()["Other"], note["Other"])
        self.capture(dialog, "audio-mapping-needed.png")
        dialog.close()

    def test_recording_picker_has_explicit_selection_retry_and_ignores_closed_callbacks(self):
        from shirabe_addon import authoring_ui, authoring_media
        operations, imports = [], []

        class Query:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def without_collection(self):
                return self
            def failure(self, callback):
                self.failed = callback
                return self
            def run_in_background(self):
                operations.append(self)

        recording = authoring_media.Recording("https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a", "ねこ", 1, "HL")
        mp3_recording = authoring_media.Recording(recording.url, "ねこ", 1, "HL", download_url=recording.url[:-4] + ".mp3")
        imported = authoring_media.ImportedMedia("shirabe_" + "b" * 64 + ".m4a", "ignored", "Shirabe", authoring_media.ANKI_PERMISSION)
        with patch.object(authoring_ui, "QueryOp", Query), patch.object(authoring_media, "import_recording", side_effect=lambda *a, **kw: imports.append(kw)):
            picker = authoring_ui.RecordingDialog(self.mw, self.fixture.lookup, {})
            operations[-1].kwargs["success"]((recording, mp3_recording))
            self.assertFalse(picker.download.isEnabled())
            self.assertIn(authoring_media.ANKI_PERMISSION, picker.permission.text())
            picker.recordings.setCurrentIndex(1)
            self.assertFalse(picker.download.isEnabled())
            self.assertIn("AnkiWeb", picker.format_notice.text())
            self.assertFalse(picker.allow_m4a.isChecked())
            picker.allow_m4a.click()
            self.assertTrue(picker.download.isEnabled())
            picker.recordings.setCurrentIndex(2)
            self.assertTrue(picker.download.isEnabled())
            self.assertEqual(picker.download.text(), "Download MP3")
            self.assertTrue(picker.allow_m4a.isHidden())
            self.assertFalse(picker.allow_m4a.isChecked())
            picker.recordings.setCurrentIndex(1)
            self.assertFalse(picker.download.isEnabled())
            picker.allow_m4a.click()
            picker.download.click()
            self.assertEqual(len(imports), 1)
            self.assertTrue(imports[-1]["allow_m4a"])
            imports[-1]["on_error"](RuntimeError("Connection interrupted"))
            self.assertTrue(picker.download.isEnabled())
            self.assertIn("Retry", picker.download.text())
            picker.download.click()
            picker.reject()
            self.assertTrue(imports[-1]["cancelled"]())
            imports[-1]["on_ready"](imported)
            self.assertIsNone(picker.imported)

            closed_picker = authoring_ui.RecordingDialog(self.mw, self.fixture.lookup, {})
            closed_picker.reject()
            operations[-1].kwargs["success"]((recording,))
            self.assertEqual(closed_picker.recordings.count(), 1)
            self.assertFalse(closed_picker.download.isEnabled())

    def test_recording_picker_keeps_large_text_and_actions_on_screen(self):
        from aqt.qt import QDialogButtonBox, QFont, QLabel, QPoint, QScrollArea
        from shirabe_addon import authoring_ui

        class Query:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def without_collection(self):
                return self
            def failure(self, callback):
                return self
            def run_in_background(self):
                pass

        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 28))
            with patch.object(authoring_ui, "QueryOp", Query):
                dialog = authoring_ui.RecordingDialog(self.mw, self.fixture.lookup, {})
            dialog.show()
            self.app.processEvents()
            self.capture(dialog, "recording-large.png")
            screen = dialog.screen().availableGeometry()
            self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
            self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
            content = dialog.findChild(QScrollArea)
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
            content.ensureWidgetVisible(dialog.status)
            self.app.processEvents()
            status_top = dialog.status.mapTo(content.viewport(), QPoint(0, 0))
            status_bottom = status_top + QPoint(dialog.status.width() - 1, dialog.status.height() - 1)
            self.assertTrue(content.viewport().rect().contains(status_top))
            self.assertTrue(content.viewport().rect().contains(status_bottom))
            self.capture(dialog, "recording-large-bottom.png")
            dialog.close()
            dialog.deleteLater()
        finally:
            self.app.setFont(original)

    def test_cancelled_media_import_cannot_write_when_collection_work_is_queued(self):
        from threading import Event
        from shirabe_addon import authoring_media
        operations = []

        class Query:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def without_collection(self):
                return self
            def failure(self, callback):
                return self
            def run_in_background(self):
                operations.append(self)

        closed = Event()
        recording = authoring_media.Recording("https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a", "ねこ", 1, "HL")
        ready, errors = [], []
        with patch("aqt.operations.QueryOp", Query), patch.object(authoring_media, "store_selected_media") as store:
            authoring_media.import_recording(self.mw, recording, allow_m4a=True, on_ready=ready.append, on_error=errors.append, cancelled=closed.is_set)
            downloaded = authoring_media.DownloadedMedia(b"unused fixture", "m4a", "Shirabe", authoring_media.ANKI_PERMISSION)
            operations[0].kwargs["success"](downloaded)
            self.assertEqual(len(operations), 2)
            closed.set()
            operations[1].kwargs["success"](operations[1].kwargs["op"](self.fixture.col))
            store.assert_not_called()
            self.assertEqual(ready, [])
            self.assertEqual(errors, [])

    def test_downloaded_recording_enters_preview_without_replacing_existing_audio(self):
        from shirabe_addon import authoring_ui, authoring_media
        profile = FieldProfile("Audio", self.fixture.model["id"], {"audio": "Other", "source": "Sentence"})
        note = self.fixture.add(Sentence="My source")
        imported = authoring_media.ImportedMedia("shirabe_" + "b" * 64 + ".m4a", "ignored", "Shirabe", authoring_media.ANKI_PERMISSION)
        dialog = authoring_ui.ChoiceDialog(self.mw, self.fixture.lookup, profile, self.fixture.model, note=note, config={})
        dialog.set_recording(imported)
        self.assertEqual(dialog.preview.field_values()["Other"], note["Other"])
        box = dialog.fields.actions["audio"]
        box.setCurrentIndex(box.findData("append"))
        self.assertIn(imported.filename, dialog.preview.field_values()["Other"])
        self.assertIn("My source", dialog.preview.field_values()["Sentence"])
        self.assertIn(authoring_media.ANKI_PERMISSION, dialog.preview.field_values()["Sentence"])
        source = dialog.fields.actions["source"]
        source.setCurrentIndex(source.findData("keep"))
        from aqt.qt import QDialogButtonBox
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled())
        self.assertIn("attribution", dialog.problem.text())
        source.setCurrentIndex(source.findData("append"))
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled())
        dialog.reject()
        self.assertEqual(self.fixture.col.get_note(note.id)["Other"], note["Other"])

    def test_mapping_and_material_remain_scrollable_with_large_fonts_and_dark_palette(self):
        from aqt.qt import QColor, QDialogButtonBox, QFont, QLabel, QPalette, QScrollArea, QPoint
        from shirabe_addon.authoring_ui import ChoiceDialog, MappingDialog
        old_font, old_palette = self.app.font(), self.app.palette()
        try:
            self.app.setFont(QFont("Arial", 28))
            palette = QPalette(old_palette)
            for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
                palette.setColor(role, QColor("#242424"))
            for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
                palette.setColor(role, QColor("#eeeeee"))
            self.app.setPalette(palette)
            mapping = MappingDialog(self.mw, {}, note_type_id=self.fixture.model["id"])
            self.capture(mapping, "mapping-large-dark.png")
            self.assertGreaterEqual(mapping.name.width(), mapping.name.fontMetrics().horizontalAdvance(mapping.name.text()) + 24)
            value = Lookup(lemma="食べる", reading="たべる", sentence="ご飯を食べます。", meanings=("to eat",))
            material = ChoiceDialog(self.mw, value, self.fixture.profile, self.fixture.model,
                                    note=self.fixture.col.new_note(self.fixture.model), config={})
            self.capture(material, "material-large-dark.png")
            for dialog in (mapping, material):
                available = dialog.screen().availableGeometry()
                self.assertLessEqual(dialog.frameGeometry().width(), available.width())
                self.assertLessEqual(dialog.frameGeometry().height(), available.height())
                scroll = dialog.findChild(QScrollArea)
                self.assertIsNotNone(scroll)
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                buttons = dialog.findChild(QDialogButtonBox)
                top = buttons.mapTo(dialog, QPoint()).y()
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(top + buttons.height(), dialog.height())
                for label in dialog.findChildren(QLabel):
                    if label.isVisible() and label.wordWrap():
                        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()), label.text())
            mapping.close()
            material.close()
        finally:
            self.app.setFont(old_font)
            self.app.setPalette(old_palette)


if __name__ == "__main__":
    unittest.main()
