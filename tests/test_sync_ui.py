import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from gui_safety import offscreen_test_application
from shirabe_addon.sync_state import SyncState


@unittest.skipUnless(importlib.util.find_spec("aqt"), "Run with Anki's Python to check Qt widgets")
class StudySyncWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = offscreen_test_application([])

    def setUp(self):
        from aqt.qt import QWidget
        self.directory = tempfile.TemporaryDirectory()
        self.mw = QWidget()
        self.mw.col = SimpleNamespace(path=str(Path(self.directory.name) / "collection.anki2"))
        self.mw.pm = SimpleNamespace(profileFolder=lambda: self.directory.name)
        self.mw.taskman = SimpleNamespace(run_on_main=lambda callback: callback())

    def tearDown(self):
        self.mw.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def dialog(self):
        from shirabe_addon.sync_ui import StudySyncDialog
        with patch("shirabe_addon.sync_ui.version", "unsupported"):
            dialog = StudySyncDialog(self.mw, "https://shirabe.example")
        dialog.show()
        self.app.processEvents()
        return dialog

    def test_undo_dialog_selects_a_schedule_without_discarding_other_history(self):
        from aqt.qt import QDialogButtonBox
        from shirabe_addon.sync_conflict_ui import HistoryRemovalDialog

        prepared = {"entry": {"removed_history": [{"id": 10, "cid": 20}]}, "local": {"reps": 0},
                    "detail": {"anki": {"card_after": {"reps": 2}}}}
        for side in ("shirabe", "anki"):
            submitted = []
            dialog = HistoryRemovalDialog(self.mw, prepared, submitted.append)
            dialog.show()
            self.app.processEvents()
            self.assertEqual("shirabe", dialog.schedule.currentData())
            dialog.schedule.setCurrentIndex(dialog.schedule.findData(side))
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            self.assertEqual([side], submitted)
            dialog.deleteLater()

    def test_template_checks_are_plain_text_advisory_and_do_not_resolve_sync(self):
        from aqt.qt import QDialogButtonBox, QLabel, QPlainTextEdit

        dialog = self.dialog()
        reports = [{"id": 42, "name": "<script>Japanese</script>", "revision": 3,
                    "issues": [{"template": 0, "side": "question", "code": "unknown_filter", "detail": "edit"}]},
                   {"id": 43, "name": "Scripted", "revision": 1, "issues": []},
                   {"id": 44, "name": "No report", "revision": 0, "issues": None}]
        with patch.object(dialog, "_sync") as sync:
            dialog._show_template_checks(reports)
            checks = dialog.template_checks_dialog
            view = checks.findChild(QPlainTextEdit)
            self.assertTrue(view.isReadOnly())
            for text in ("<script>Japanese</script>", "revision 3", "unknown_filter", "edit",
                         "No source issues reported", "No report for this revision"):
                self.assertIn(text, view.toPlainText())
            scope = " ".join(widget.text() for widget in checks.findChildren(QLabel))
            self.assertIn("not run JavaScript", scope)
            self.assertIn("do not block sync", scope)
            checks.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close).click()
            sync.assert_not_called()
        dialog.close()

    def test_type_mapping_requires_unique_choices_and_shows_removed_items(self):
        from aqt.qt import QDialogButtonBox, QLabel
        from shirabe_addon.sync_conflict_ui import TypeMappingDialog

        prepared = {"previous": {"name": "Japanese", "flds": [{"name": "Front"}, {"name": "Back"}],
                                 "tmpls": [{"name": "Old card"}]},
                    "current": {"name": "Japanese", "flds": [{"name": "Word"}, {"name": "Meaning"}],
                                "tmpls": [{"name": "Recognition"}]}}
        submitted = []
        dialog = TypeMappingDialog(self.mw, prepared, submitted.append)
        buttons = dialog.findChild(QDialogButtonBox)
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(confirm.isEnabled())
        fields, templates = dialog.choices["field_map"], dialog.choices["template_map"]
        fields[0].setCurrentIndex(fields[0].findData(0))
        fields[1].setCurrentIndex(fields[1].findData(0))
        templates[0].setCurrentIndex(templates[0].findData(0))
        self.assertFalse(confirm.isEnabled())
        fields[1].setCurrentIndex(fields[1].findData(None))
        self.assertTrue(confirm.isEnabled())
        self.assertIn("Back", dialog.removals.text())
        self.assertTrue(any("retire" in widget.text() for widget in dialog.findChildren(QLabel)))
        fields[1].setCurrentIndex(fields[1].findData(1))
        self.assertTrue(confirm.isEnabled())
        self.assertNotIn("Back", dialog.removals.text())
        confirm.click()
        self.assertEqual([{"field_map": [0, 1], "template_map": [0]}], submitted)
        dialog.deleteLater()

    def test_mapping_conflict_routes_to_the_native_chooser_and_cancel_keeps_it_pending(self):
        from aqt.qt import QDialogButtonBox
        from unittest.mock import Mock
        from shirabe_addon.sync_changes import SyncConflict

        problem = {"kind": "note_types", "id": 42, "status": "mapping_required", "code": "ambiguous_mapping"}
        definition = {"name": "Japanese", "flds": [{"name": "Front"}], "tmpls": [{"name": "Card"}]}
        prepared = {"problem": problem, "previous": definition, "current": definition}
        dialog = self.dialog()
        dialog.session.changes = Mock()
        dialog.session.changes.prepare_type_mapping.return_value = prepared
        dialog.session._failed(SyncConflict([problem]))
        dialog.conflicts.setCurrentRow(0)
        self.assertTrue(dialog.resolve.isEnabled())
        jobs = []
        with patch.object(dialog, "_background", side_effect=lambda op, success, **options: jobs.append((op, success, options))):
            dialog.resolve.click()
            self.assertEqual({"collection": True}, jobs[0][2])
            jobs[0][1](jobs[0][0](self.mw.col))
            chooser = dialog.type_mapping
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
            dialog.session.changes.resolve_type_mapping.assert_not_called()
            self.assertEqual(1, len(jobs))
            dialog._show_type_mapping(prepared)
            dialog.type_mapping.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            self.assertEqual({"collection": True}, jobs[1][2])
            jobs[1][0](self.mw.col)
            dialog.session.changes.resolve_type_mapping.assert_called_once_with(
                self.mw.col, prepared, {"field_map": [0], "template_map": [0]})
        dialog.close()

    def test_rejected_type_opens_mapping_before_retrying_current_data(self):
        from aqt.qt import QDialogButtonBox
        from unittest.mock import Mock

        problem = {"kind": "note_types", "id": 42, "status": "rejected", "code": "invalid_mapping"}
        prepared = {"problem": problem, "entry": {}, "current": {}, "mapping_required": True}
        dialog = self.dialog()
        dialog.session.changes = Mock()
        jobs = []
        with patch.object(dialog, "_background", side_effect=lambda op, success, **options: jobs.append((op, success, options))):
            dialog._show_rejected_retry(prepared)
            chooser = dialog.rejected_retry
            index = chooser.source.findData("mapping")
            self.assertGreaterEqual(index, 0)
            chooser.source.setCurrentIndex(index)
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            self.assertEqual({"collection": True}, jobs[0][2])
            jobs[0][0](self.mw.col)
            dialog.session.changes.prepare_type_mapping.assert_called_once_with(self.mw.col, problem)
            dialog.session.changes.retry_rejected.assert_not_called()
        dialog.close()

    def test_type_mapping_confirmation_remains_reachable_with_large_text(self):
        from aqt.qt import QDialogButtonBox, QFont
        from shirabe_addon.sync_conflict_ui import TypeMappingDialog

        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 22))
            prepared = {"previous": {"flds": [{"name": f"Previous field {index}"} for index in range(12)],
                                     "tmpls": [{"name": "Previous template"}]},
                        "current": {"name": "Japanese", "flds": [{"name": f"Renamed field {index}"} for index in range(12)],
                                    "tmpls": [{"name": "Renamed template"}]}}
            dialog = TypeMappingDialog(self.mw, prepared, lambda _: None)
            dialog.show()
            self.app.processEvents()
            screen = dialog.screen().availableGeometry()
            self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
            self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
            buttons = dialog.findChild(QDialogButtonBox)
            self.assertLessEqual(buttons.geometry().bottom(), dialog.height())
            screenshots = os.environ.get("SHIRABE_SYNC_SCREENSHOTS")
            if screenshots:
                Path(screenshots).mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(Path(screenshots) / "type-mapping-large-text.png"))
            dialog.close()
        finally:
            self.app.setFont(original)

    def test_mixed_history_dialog_shows_restored_events_before_confirmation(self):
        from aqt.qt import QDialogButtonBox, QTabWidget
        from shirabe_addon.sync_conflict_ui import HistoryRemovalDialog

        restored = [{"row": {"id": 11, "cid": 20}, "removal_id": 9}]
        prepared = {"entry": {"removed_history": [{"id": 10, "cid": 20}], "restored_history": restored},
                    "problem": {"direction": "incoming"}, "local": {"reps": 1},
                    "detail": {"anki": {"card_after": {"reps": 2}}}}
        submitted = []
        dialog = HistoryRemovalDialog(self.mw, prepared, submitted.append)
        dialog.show()
        self.app.processEvents()
        tabs = dialog.findChild(QTabWidget)
        self.assertEqual(4, tabs.count())
        self.assertEqual("Restored history", tabs.tabText(3))
        self.assertEqual(restored, json.loads(tabs.widget(3).toPlainText()))
        self.assertTrue(tabs.widget(3).isReadOnly())
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        self.assertEqual([], submitted)
        dialog.deleteLater()

    def test_media_dialog_requires_an_explicit_choice_and_cancel_keeps_it_pending(self):
        from aqt.qt import QDialogButtonBox
        from shirabe_addon.sync_conflict_ui import MediaConflictDialog

        prepared = {"filename": "猫.mp3", "local": None,
                    "remote": {"digest": "a" * 64, "bytes": 123, "deleted": False}}
        submitted = []
        for choice in (None, "anki", "shirabe"):
            dialog = MediaConflictDialog(self.mw, prepared, submitted.append)
            dialog.show()
            self.app.processEvents()
            buttons = dialog.findChild(QDialogButtonBox)
            self.assertFalse(buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            self.assertEqual("Confirm deletion from Shirabe", dialog.choice.itemText(dialog.choice.findData("anki")))
            if choice is None:
                buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
                self.assertEqual([], submitted)
            else:
                dialog.choice.setCurrentIndex(dialog.choice.findData(choice))
                self.assertTrue(buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
                buttons.button(QDialogButtonBox.StandardButton.Ok).click()
                self.assertEqual(choice, submitted[-1])
            dialog.deleteLater()

    def test_media_confirmation_is_saved_on_the_collection_worker_before_sync(self):
        from aqt.qt import QDialogButtonBox
        from unittest.mock import Mock

        prepared = {"filename": "cat.mp3", "local": None,
                    "remote": {"digest": "a" * 64, "bytes": 123, "deleted": False}}
        dialog = self.dialog()
        dialog.session.media = Mock()
        jobs = []
        with patch.object(dialog, "_background", side_effect=lambda operation, success, **options: jobs.append((operation, success, options))):
            dialog._show_media_resolution(prepared)
            chooser = dialog.media_resolution
            chooser.choice.setCurrentIndex(chooser.choice.findData("anki"))
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            self.assertEqual(1, len(jobs))
            operation, success, options = jobs[0]
            self.assertEqual({"collection": True}, options)
            operation(self.mw.col)
            dialog.session.media.resolve.assert_called_once_with(self.mw.col, prepared, "anki")
            with patch.object(dialog, "_sync") as sync:
                success(None)
                sync.assert_called_once_with()
        dialog.close()

    def test_review_undo_dialog_withdraws_only_after_confirmation(self):
        from aqt.qt import QDialogButtonBox
        from unittest.mock import Mock

        prepared = {"problem": {"direction": "incoming", "kind": "review_undos"},
                    "change": {"payload": {"anki_card_id": 42}}}
        dialog = self.dialog()
        dialog.session.delivery = Mock()
        jobs = []
        with patch.object(dialog, "_background", side_effect=lambda operation, success, **kwargs: jobs.append((operation, success))):
            dialog._show_review_undo_resolution(prepared)
            chooser = dialog.review_undo_resolution
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
            self.assertEqual([], jobs)
            dialog._show_review_undo_resolution(prepared)
            chooser = dialog.review_undo_resolution
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            jobs[0][0](self.mw.col)
            dialog.session.delivery.withdraw_review_undo.assert_called_once_with(self.mw.col, prepared)
            dialog._show_review_undo_resolution(prepared)
            chooser = dialog.review_undo_resolution
            chooser.action.setCurrentIndex(chooser.action.findData("undo"))
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            jobs[1][0](self.mw.col)
            dialog.session.delivery.confirm_review_undo.assert_called_once_with(self.mw.col, prepared)
            dialog._show_review_undo_resolution({**prepared, "undo_error": "Published history cannot be removed"})
            chooser = dialog.review_undo_resolution
            self.assertEqual(-1, chooser.action.findData("undo"))
            chooser.reject()
        dialog.close()

    def test_accepted_undo_result_choice_uses_a_mutating_collection_operation(self):
        from aqt.qt import QDialogButtonBox, QLabel
        from unittest.mock import Mock

        local = {"did": 1, "queue": 0, "flags": 0, "data": "{}"}
        prepared = {"problem": {"direction": "returning", "kind": "review_undo_results"},
                    "undo_accepted": True, "schedule_matches": True, "local": local,
                    "decks": {1: "Japanese"}, "detail": {"anki": {"card_after": {**local, "flags": 3}}}}
        dialog = self.dialog()
        dialog.session.delivery = Mock()
        jobs = []
        with patch.object(dialog, "_background", side_effect=lambda operation, success, **kwargs: jobs.append((operation, success, kwargs))):
            dialog._show_review_undo_result_resolution(prepared)
            chooser = dialog.review_undo_result_resolution
            self.assertTrue(any("will not remove any review history" in label.text() for label in chooser.findChildren(QLabel)))
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
            self.assertEqual([], jobs)
            dialog._show_review_undo_result_resolution(prepared)
            chooser = dialog.review_undo_result_resolution
            chooser.choices["properties"].setCurrentIndex(chooser.choices["properties"].findData("shirabe"))
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            self.assertEqual(1, len(jobs))
            operation, success, options = jobs[0]
            self.assertEqual({"mutating": True}, options)
            changes = operation(self.mw.col)
            self.assertTrue(changes.card and changes.study_queues and changes.browser_table)
            dialog.session.delivery.resolve_review_undo_result.assert_called_once_with(
                self.mw.col, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
            with patch.object(dialog, "_sync") as sync:
                success(changes)
                sync.assert_called_once_with()
            dialog._show_review_undo_result_resolution({**prepared, "schedule_matches": False})
            chooser = dialog.review_undo_result_resolution
            self.assertFalse(chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            chooser.reject()
            self.assertEqual(1, len(jobs))
            dialog._show_review_undo_result_resolution({**prepared, "schedule_matches": False, "schedule_confirmed": True})
            chooser = dialog.review_undo_result_resolution
            self.assertTrue(chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            self.assertTrue(any("schedule you confirmed" in label.text() for label in chooser.findChildren(QLabel)))
            chooser.reject()
            self.assertEqual(1, len(jobs))
        dialog.close()

    def test_accepted_undo_schedule_requires_a_selection_and_cancel_leaves_it_pending(self):
        from aqt.qt import QDialogButtonBox, QLabel
        from shirabe_addon.sync_conflict_ui import CardConflictDialog

        local = {"did": 1, "queue": 0, "flags": 0, "data": "{}", "due": 1}
        prepared = {"undo_accepted": True, "schedule_matches": False, "schedule_choice_required": True,
                    "local": local, "decks": {1: "Japanese"}, "detail": {"anki": {"card_after": {**local, "due": 10}}}}
        for side in ("anki", "shirabe"):
            submitted = []
            chooser = CardConflictDialog(self.mw, prepared, submitted.append)
            button = chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
            self.assertFalse(button.isEnabled())
            self.assertIsNone(chooser.choices["schedule"].currentData())
            self.assertTrue(any("not a live view of Shirabe" in label.text() for label in chooser.findChildren(QLabel)))
            chooser.choices["schedule"].setCurrentIndex(chooser.choices["schedule"].findData(side))
            self.assertTrue(button.isEnabled())
            button.click()
            self.assertEqual([{"schedule": side, "deck": "anki", "lifecycle": "anki", "properties": "anki"}], submitted)
            chooser.deleteLater()
        submitted = []
        chooser = CardConflictDialog(self.mw, prepared, submitted.append)
        chooser.choices["schedule"].setCurrentIndex(1)
        chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        self.assertEqual([], submitted)
        chooser.deleteLater()

    def test_incoming_undo_dialog_labels_the_recorded_schedule_and_routes_to_delivery(self):
        from aqt.qt import QDialogButtonBox, QLabel, QTabWidget
        from unittest.mock import Mock

        prepared = {"problem": {"direction": "incoming"}, "entry": {"removed_history": [{"id": 10, "cid": 20}]},
                    "local": {"reps": 1}, "detail": {"anki": {"card_after": {"reps": 0}}}}
        dialog = self.dialog()
        dialog.session.delivery = Mock()
        dialog.session.changes = Mock()
        jobs = []
        with patch.object(dialog, "_background", side_effect=lambda operation, success, **kwargs: jobs.append((operation, success))):
            dialog._show_card_resolution(prepared)
            chooser = dialog.history_removal_resolution
            self.assertIn("incoming", chooser.schedule.itemText(0).lower())
            tabs = chooser.findChild(QTabWidget)
            self.assertEqual("Incoming schedule", tabs.tabText(1))
            self.assertTrue(any("one removal attempt" in label.text() for label in chooser.findChildren(QLabel)))
            chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            jobs[0][0](self.mw.col)
            dialog.session.delivery.resolve_history_removal.assert_called_once_with(self.mw.col, prepared, schedule="shirabe")
            dialog.session.changes.resolve_history_removal.assert_not_called()
        dialog.close()

    def test_unsupported_runtime_disables_collection_sync_without_opening_a_browser(self):
        with patch("shirabe_addon.sync_ui.openLink") as browser:
            dialog = self.dialog()
            self.assertFalse(dialog.upload.isEnabled())
            self.assertFalse(dialog.sync.isEnabled())
            self.assertIn("26.08.1", dialog.status.text())
            browser.assert_not_called()
            dialog.close()

    def test_approval_only_opens_on_the_explicit_button_and_does_not_show_device_secret(self):
        dialog = self.dialog()
        with patch("shirabe_addon.sync_ui.openLink") as browser:
            dialog._waiting({"user_code": "ABCD", "device_code": "secret-device-code",
                             "verification_uri": "https://shirabe.example/link", "verification_uri_complete": "https://shirabe.example/link?code=ABCD"})
            browser.assert_not_called()
            self.assertNotIn("secret-device-code", dialog.status.text())
            dialog.approve.click()
            browser.assert_called_once_with("https://shirabe.example/link?code=ABCD", parent=dialog)
        dialog.close()
        self.assertTrue(dialog.closed.is_set())
        self.assertFalse(dialog.timer.isActive())

    def test_existing_pairing_keeps_its_server_when_dictionary_settings_change(self):
        from shirabe_addon.sync_login import private_sync_path
        path = private_sync_path(self.directory.name)
        with SyncState(path) as state:
            state.put("initial_snapshot", {"api": "https://paired.example/api/v1", "phase": "uploaded"})
        dialog = self.dialog()
        self.assertEqual("https://paired.example/api/v1", dialog.login.api)
        self.assertEqual("Initial copy uploaded", dialog.upload.text())
        dialog.close()

    def test_network_and_collection_jobs_use_separate_anki_operation_modes(self):
        dialog = self.dialog()
        modes = []
        jobs = []

        class Query:
            def __init__(self, parent, op, success):
                jobs.append((op, success))
                self.mode = "collection"

            def failure(self, _):
                return self

            def without_collection(self):
                self.mode = "network"
                return self

            def run_in_background(self):
                modes.append(self.mode)

        with patch("shirabe_addon.sync_session.QueryOp", Query):
            dialog._background(lambda: "response", lambda _: None)
            operation, success = jobs[-1]
            success(operation(None))
            dialog._background(lambda col: col.path, lambda _: None, collection=True)
            operation, success = jobs[-1]
            success(operation(self.mw.col))
        self.assertEqual(["network", "collection"], modes)
        dialog.close()

    def test_initial_copy_waits_for_confirmation_after_showing_counts_and_warnings(self):
        from aqt.qt import QDialogButtonBox, QLabel

        captured = {"phase": "captured", "deck_ids": [1, 2], "unselected_siblings": 3,
                    "missing_media": ["absent.mp3"],
                    "counts": {"note_types": 2, "notes": 4, "cards": 5, "history": 7,
                               "media_files": 6, "media_bytes": 12345}}
        for confirm in (False, True):
            with self.subTest(confirm=confirm):
                dialog = self.dialog()
                with patch.object(dialog, "_background") as background:
                    dialog._capture()
                    completed = background.call_args.args[1]
                    background.reset_mock()
                    completed(captured)
                    background.assert_not_called()
                    preview = dialog.snapshot_preview
                    text = "\n".join(widget.text() for widget in preview.findChildren(QLabel))
                    for expected in ("Decks (including subdecks): 2", "Notes: 4", "Cards: 5", "Note types: 2",
                                     "History entries: 7", "Media files: 6", "12,345 bytes", "3 cards outside",
                                     "Missing referenced files: 1", "Only selected cards' schedules"):
                        self.assertIn(expected, text)
                    buttons = preview.findChild(QDialogButtonBox)
                    buttons.button(QDialogButtonBox.StandardButton.Ok if confirm else QDialogButtonBox.StandardButton.Cancel).click()
                    if confirm:
                        self.assertEqual(1, background.call_count)
                        with patch.object(dialog.snapshot, "upload") as upload:
                            background.call_args.args[0]()
                            upload.assert_called_once()
                    else:
                        background.assert_not_called()
                        self.assertIn("Upload paused", dialog.status.text())
                dialog.close()

    def test_closing_settings_dismisses_the_preview_without_starting_upload(self):
        from aqt.qt import QDialogButtonBox

        dialog = self.dialog()
        captured = {"phase": "captured", "deck_ids": [1], "unselected_siblings": 0, "missing_media": [],
                    "counts": {"note_types": 0, "notes": 0, "cards": 0, "history": 0,
                               "media_files": 0, "media_bytes": 0}}
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            dialog._upload(captured)
            preview = dialog.snapshot_preview
            self.assertTrue(preview.isVisible())
            dialog.close()
            self.assertFalse(preview.isVisible())
            preview.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            query.assert_not_called()

    def test_snapshot_preview_keeps_confirmation_reachable_with_large_text(self):
        from aqt.qt import QDialogButtonBox, QFont, QScrollArea
        from shirabe_addon.sync_ui import SnapshotPreviewDialog

        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 22))
            captured = {"deck_ids": [1, 2], "unselected_siblings": 3, "missing_media": ["absent.mp3"],
                        "counts": {"note_types": 2, "notes": 100000, "cards": 200000, "history": 3000000,
                                   "media_files": 100000, "media_bytes": 9876543210}}
            preview = SnapshotPreviewDialog(self.mw, captured)
            preview.show()
            self.app.processEvents()
            screen = preview.screen().availableGeometry()
            self.assertLessEqual(preview.frameGeometry().height(), screen.height())
            self.assertLessEqual(preview.frameGeometry().width(), screen.width())
            self.assertEqual(0, preview.findChild(QScrollArea).horizontalScrollBar().maximum())
            buttons = preview.findChild(QDialogButtonBox)
            self.assertLessEqual(buttons.geometry().bottom(), preview.height())
            screenshots = os.environ.get("SHIRABE_SYNC_SCREENSHOTS")
            if screenshots:
                Path(screenshots).mkdir(parents=True, exist_ok=True)
                preview.grab().save(str(Path(screenshots) / "snapshot-preview-large-text.png"))
            preview.close()
        finally:
            self.app.setFont(original)

    def test_closed_dialog_does_not_start_the_next_background_step(self):
        dialog = self.dialog()
        dialog.close()
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            dialog._background(lambda: None, lambda _: None)
            query.assert_not_called()

    def test_sync_action_uses_the_shared_profile_cycle(self):
        dialog = self.dialog()
        with patch.object(dialog.session, "start") as start:
            dialog._sync()
            start.assert_called_once_with()
        self.assertEqual("Sync now", dialog.sync.text())
        dialog.close()

    def test_reopening_settings_keeps_the_paused_conflicts_visible(self):
        from shirabe_addon.sync_changes import SyncConflict
        from shirabe_addon.sync_ui import StudySyncDialog
        dialog = self.dialog()
        session = dialog.session
        dialog.close()
        session._failed(SyncConflict([{"kind": "notes", "id": 123, "code": "field_conflict"}]))
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            reopened = StudySyncDialog(self.mw, "https://shirabe.example")
            query.call_args.kwargs["success"]([])
        self.assertIn("1 sync record needs attention", reopened.status.text())
        self.assertEqual(1, reopened.conflicts.count())
        self.assertIn("123", reopened.conflicts.item(0).text())
        self.assertIn("field_conflict", reopened.conflicts.item(0).text())
        self.assertFalse(session.request())
        reopened.close()

    def test_note_chooser_copies_source_text_and_submits_only_on_apply(self):
        from shirabe_addon.sync_conflict_ui import NoteConflictDialog
        submitted = []
        prepared = {"local": {"flds": "猫\x1fAnki meaning", "tags": " anki "},
                    "detail": {"note": {"field_names": ["Front", "Back"],
                               "fields": ["猫", "<script>not executed</script>"], "tags": ["shirabe"]}}}
        dialog = NoteConflictDialog(self.mw, prepared, lambda fields, tags: submitted.append((fields, tags)))
        self.assertEqual([], submitted)
        dialog.use_shirabe[1].click()
        self.assertEqual("<script>not executed</script>", dialog.fields[1].toPlainText())
        dialog.fields[0].setPlainText("ねこ")
        dialog.tags.setText("chosen Japanese")
        dialog.accept()
        self.assertEqual([(["ねこ", "<script>not executed</script>"], ["chosen", "Japanese"])], submitted)

    def test_rejected_record_confirmation_shows_saved_source_and_sends_only_on_confirm(self):
        from aqt.qt import QDialogButtonBox, QTabWidget
        from shirabe_addon.sync_conflict_ui import RejectedRecordDialog
        prepared = {"problem": {"kind": "notes", "id": 123, "code": "invalid_fields"},
                    "entry": {"flds": "<script>saved source</script>"}, "current": {"flds": "newer Anki edit"}}
        submitted = []
        dialog = RejectedRecordDialog(self.mw, prepared, lambda source: submitted.append((prepared, source)))
        self.assertEqual([], submitted)
        tabs = dialog.findChild(QTabWidget)
        sources = [tabs.widget(index).toPlainText() for index in range(tabs.count())]
        self.assertIn("<script>saved source</script>", sources[0])
        self.assertIn("newer Anki edit", sources[1])
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertEqual("Retry saved record", buttons.button(QDialogButtonBox.StandardButton.Ok).text())
        dialog.accept()
        self.assertEqual([(prepared, "saved")], submitted)

    def test_rejected_record_current_data_requires_an_explicit_choice(self):
        from aqt.qt import QDialogButtonBox
        from shirabe_addon.sync_conflict_ui import RejectedRecordDialog
        prepared = {"problem": {"kind": "notes", "id": 123, "code": "invalid_fields"},
                    "entry": {"flds": "saved"}, "current": {"flds": "corrected"}, "current_entry": {"flds": "corrected"}}
        submitted = []
        dialog = RejectedRecordDialog(self.mw, prepared, submitted.append)
        self.assertEqual("saved", dialog.source.currentData())
        dialog.source.setCurrentIndex(dialog.source.findData("current"))
        self.assertEqual([], submitted)
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertEqual("Send current Anki data", buttons.button(QDialogButtonBox.StandardButton.Ok).text())
        dialog.accept()
        self.assertEqual(["current"], submitted)

    def test_rejected_type_selection_offers_retry_but_incoming_rejections_do_not(self):
        dialog = self.dialog()
        with patch("shirabe_addon.sync_ui.version", "26.08.1"):
            dialog.session.conflicts = [{"kind": "note_types", "id": 123, "code": "invalid_mapping", "status": "rejected"}]
            dialog._session_changed()
            dialog.conflicts.setCurrentRow(0)
            self.assertTrue(dialog.resolve.isEnabled())
            self.assertEqual("Review rejected record…", dialog.resolve.text())
            with patch.object(dialog, "_background") as background:
                dialog.resolve.click()
                self.assertTrue(background.call_args.kwargs["collection"])
                self.assertEqual(dialog._show_rejected_retry, background.call_args.args[1])
            dialog.session.conflicts[0]["direction"] = "incoming"
            dialog._session_changed()
            dialog.conflicts.setCurrentRow(0)
            self.assertFalse(dialog.resolve.isEnabled())
        dialog.close()

    def test_current_card_retry_explains_the_additional_review_upload(self):
        from aqt.qt import QLabel
        from shirabe_addon.sync_conflict_ui import RejectedRecordDialog
        prepared = {"problem": {"kind": "cards", "id": 123, "code": "invalid_card"},
                    "entry": {}, "current": {}, "current_entry": {}, "dependencies": [{"history": [{"row": {}}, {"row": {}}]}]}
        submitted = []
        dialog = RejectedRecordDialog(self.mw, prepared, submitted.append)
        self.assertIn("Current Anki data will also upload 2 new or updated review history records before this card.",
                      [label.text() for label in dialog.findChildren(QLabel)])
        dialog.source.setCurrentIndex(dialog.source.findData("current"))
        dialog.reject()
        self.assertEqual([], submitted)

    def test_deletion_chooser_keeps_plain_source_and_submits_only_an_explicit_keep(self):
        from aqt.qt import QDialogButtonBox, QTabWidget
        from shirabe_addon.sync_conflict_ui import NoteDeletionDialog
        prepared = {"fields": ["Expression", "Meaning"], "local": {"flds": "猫\x1f<script>not executed</script>"},
                    "card_count": 2, "attempted": True}
        submitted = []
        dialog = NoteDeletionDialog(self.mw, prepared, lambda: submitted.append("keep"))
        self.assertEqual([], submitted)
        tabs = dialog.findChild(QTabWidget)
        fields = [tabs.widget(index) for index in range(tabs.count())]
        self.assertEqual(["猫", "<script>not executed</script>"], [field.toPlainText() for field in fields])
        self.assertTrue(all(field.isReadOnly() for field in fields))
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertEqual("Keep note in Anki", buttons.button(QDialogButtonBox.StandardButton.Ok).text())
        dialog.reject()
        self.assertEqual([], submitted)
        dialog = NoteDeletionDialog(self.mw, prepared, lambda: submitted.append("keep"))
        dialog.accept()
        self.assertEqual(["keep"], submitted)

    def test_absent_note_chooser_confirms_deletion_without_offering_to_keep_it(self):
        from aqt.qt import QDialogButtonBox, QPlainTextEdit
        from shirabe_addon.sync_conflict_ui import NoteDeletionDialog
        prepared = {"fields": [], "local": None, "card_count": 0, "attempted": False}
        submitted = []
        dialog = NoteDeletionDialog(self.mw, prepared, lambda: submitted.append("absent"))
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertEqual("Confirm deletion", buttons.button(QDialogButtonBox.StandardButton.Ok).text())
        self.assertEqual([], dialog.findChildren(QPlainTextEdit))
        dialog.reject()
        self.assertEqual([], submitted)
        dialog = NoteDeletionDialog(self.mw, prepared, lambda: submitted.append("absent"))
        dialog.accept()
        self.assertEqual(["absent"], submitted)

    def test_source_deletion_conflict_shows_current_shirabe_source_and_requires_confirmation(self):
        from aqt.qt import QDialogButtonBox, QPlainTextEdit
        from shirabe_addon.sync_conflict_ui import SourceDeletionDialog
        prepared = {"local": None, "detail": {"card": {"schedule": {"reps": 5}},
                    "deletion": {"note": {"field_names": ["Expression", "Meaning"],
                    "fields": ["猫", "<script>not executed</script>"], "tags": ["edited"]}}}}
        submitted = []
        dialog = SourceDeletionDialog(self.mw, prepared, lambda: submitted.append("delete"))
        fields = dialog.findChildren(QPlainTextEdit)
        self.assertIn("<script>not executed</script>", [field.toPlainText() for field in fields])
        self.assertTrue(all(field.isReadOnly() for field in fields))
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertEqual("Apply source deletion", buttons.button(QDialogButtonBox.StandardButton.Ok).text())
        dialog.reject()
        self.assertEqual([], submitted)
        dialog = SourceDeletionDialog(self.mw, prepared, lambda: submitted.append("delete"))
        dialog.accept()
        self.assertEqual(["delete"], submitted)

    def test_card_chooser_submits_independent_choices_and_blocks_divergent_schedules(self):
        from aqt.qt import QDialogButtonBox
        from shirabe_addon.sync_conflict_ui import CardConflictDialog
        submitted = []
        local = {"did": 1, "queue": 0, "flags": 3, "data": "{}"}
        prepared = {"local": local, "detail": {"anki": {"card_after": {**local, "flags": 2, "queue": -1}}},
                    "decks": {1: "Japanese"}, "schedule_matches": True}
        dialog = CardConflictDialog(self.mw, prepared, submitted.append)
        dialog.choices["properties"].setCurrentIndex(1)
        self.assertEqual([], submitted)
        dialog.accept()
        self.assertEqual([{"deck": "anki", "lifecycle": "anki", "properties": "shirabe"}], submitted)
        dialog = CardConflictDialog(self.mw, {**prepared, "schedule_matches": False}, submitted.append)
        self.assertFalse(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        dialog.reject()
        self.assertEqual(1, len(submitted))
        dialog = CardConflictDialog(self.mw, {**prepared, "schedule_matches": False, "pending_reviews": [{"id": 123}]}, submitted.append)
        confirm = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        self.assertTrue(confirm.isEnabled())
        self.assertIn("reviews", confirm.text())
        dialog.reject()

    def test_card_chooser_keeps_apply_reachable_with_large_text(self):
        from aqt.qt import QDialogButtonBox, QFont
        from shirabe_addon.sync_conflict_ui import CardConflictDialog
        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 22))
            local = {"did": 1, "queue": 0, "flags": 3, "data": "{}"}
            prepared = {"local": local, "detail": {"anki": {"card_after": {**local, "flags": 2}}},
                        "decks": {1: "Japanese"}, "schedule_matches": True}
            dialog = CardConflictDialog(self.mw, prepared, lambda _: None)
            dialog.show()
            self.app.processEvents()
            screen = dialog.screen().availableGeometry()
            self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
            self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
            buttons = dialog.findChild(QDialogButtonBox)
            self.assertLessEqual(buttons.geometry().bottom(), dialog.height())
            screenshots = os.environ.get("SHIRABE_SYNC_SCREENSHOTS")
            if screenshots:
                Path(screenshots).mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(Path(screenshots) / "card-resolution-large-text.png"))
            dialog.close()
        finally:
            self.app.setFont(original)

    def test_note_chooser_keeps_apply_reachable_with_large_text(self):
        from aqt.qt import QDialogButtonBox, QFont
        from shirabe_addon.sync_conflict_ui import NoteConflictDialog
        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 22))
            prepared = {"local": {"flds": "猫\x1fAnki meaning", "tags": " Japanese "},
                        "detail": {"note": {"field_names": ["Expression", "Meaning"],
                                   "fields": ["猫", "Shirabe meaning"], "tags": ["Japanese", "shirabe"]}}}
            dialog = NoteConflictDialog(self.mw, prepared, lambda *_: None)
            dialog.show()
            self.app.processEvents()
            screen = dialog.screen().availableGeometry()
            self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
            self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
            buttons = dialog.findChild(QDialogButtonBox)
            self.assertLessEqual(buttons.geometry().bottom(), dialog.height())
            screenshots = os.environ.get("SHIRABE_SYNC_SCREENSHOTS")
            if screenshots:
                Path(screenshots).mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(Path(screenshots) / "note-resolution-large-text.png"))
            dialog.close()
        finally:
            self.app.setFont(original)

    def test_completion_after_closing_does_not_run_the_next_step(self):
        dialog = self.dialog()
        with patch("shirabe_addon.sync_session.QueryOp") as query, patch.object(dialog, "_upload") as next_step:
            dialog._background(lambda: {}, next_step)
            completed = query.call_args.kwargs["success"]
            self.assertTrue(dialog.busy)
            dialog.close()
            completed({})
            next_step.assert_not_called()
            self.assertFalse(dialog.busy)

    def test_closing_the_initial_upload_dialog_cancels_its_rate_limit_wait(self):
        from shirabe_addon.sync_client import SyncError

        dialog = self.dialog()
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            dialog._background(lambda: {}, lambda _: self.fail("Closed dialog resumed"))
            query.return_value.failure.call_args.args[0](SyncError(429, "RATE_LIMIT_EXCEEDED", retry_after=60))
            self.assertTrue(dialog.busy)
            dialog.close()
            self.assertFalse(dialog.busy)
            self.assertFalse(dialog.session.retry_timer.isActive())
            dialog.session.retry_timer.timeout.emit()
            self.assertEqual(1, query.call_count)

    def test_main_settings_can_open_study_sync_without_reusing_card_sign_in(self):
        from aqt.qt import QPushButton
        from shirabe_addon.settings import SettingsDialog
        calls = []
        dialog = SettingsDialog(self.mw, {}, lambda _: calls.append("save"), lambda: None,
                                lambda: calls.append("card_login"), on_study_sync=lambda: calls.append("study_sync"))
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "Study sync…").click()
        self.assertEqual(["save", "study_sync"], calls)
        dialog.close()

    def test_large_text_keeps_close_accessible_and_content_scrollable(self):
        from aqt.qt import QDialogButtonBox, QFont, QScrollArea
        original = self.app.font()
        try:
            self.app.setFont(QFont("Arial", 22))
            dialog = self.dialog()
            screen = dialog.screen().availableGeometry()
            self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
            self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
            scroll = dialog.findChild(QScrollArea)
            self.assertEqual(0, scroll.horizontalScrollBar().maximum())
            buttons = dialog.findChild(QDialogButtonBox)
            self.assertLessEqual(buttons.geometry().bottom(), dialog.height())
            screenshots = os.environ.get("SHIRABE_SYNC_SCREENSHOTS")
            if screenshots:
                Path(screenshots).mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(Path(screenshots) / "study-sync-large-text.png"))
            dialog.close()
        finally:
            self.app.setFont(original)
