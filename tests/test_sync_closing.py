import importlib.util
from pathlib import Path
import tempfile
from threading import Event, get_ident
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import support
from gui_safety import offscreen_test_application


@unittest.skipUnless(importlib.util.find_spec("aqt"), "Requires Anki's Qt runtime")
class SyncClosingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = offscreen_test_application([])

    def setUp(self):
        from aqt.qt import QWidget
        from shirabe_addon.sync_session import session_for

        self.directory = tempfile.TemporaryDirectory()
        self.mw = QWidget()
        self.mw.col = SimpleNamespace(path=str(Path(self.directory.name) / "collection.anki2"))
        self.mw.pm = SimpleNamespace(profileFolder=lambda: self.directory.name)
        self.mw.taskman = SimpleNamespace(run_on_main=lambda callback: callback())
        self.mw.state = "review"
        self.mw.closeAllWindows = Mock()
        self.session = session_for(self.mw, "https://shirabe.example")
        self.session.automatic = True
        self.original = Mock()
        self.callback = Mock()

    def tearDown(self):
        if dialog := getattr(self.mw, "_shirabe_profile_close", None):
            dialog.reject()
        self.session.close()
        self.mw.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def close_profile(self):
        from shirabe_addon.sync_closing import unload_profile

        unload_profile(self.mw, self.callback, self.original)

    def settle(self):
        for _ in range(4):
            self.app.processEvents()

    def begin(self):
        self.close_profile()
        self.mw.closeAllWindows.call_args.args[0]()
        self.settle()
        return self.mw._shirabe_profile_close

    def start(self):
        self.session._syncing = True
        return True

    def test_close_saves_editors_then_syncs_before_native_shutdown(self):
        with patch.object(self.session, "start", side_effect=self.start) as start:
            self.close_profile()
            start.assert_not_called()
            self.original.assert_not_called()
            self.mw.closeAllWindows.call_args.args[0]()
            self.settle()
            start.assert_called_once_with()
            self.original.assert_not_called()
            self.session._finished()
            self.settle()
        self.original.assert_called_once_with(self.callback)

    def test_repeated_close_requests_do_not_start_two_syncs_or_close_twice(self):
        with patch.object(self.session, "start", side_effect=self.start) as start:
            self.begin()
            self.close_profile()
            self.assertEqual(1, self.mw.closeAllWindows.call_count)
            self.session._finished()
            self.settle()
            start.assert_called_once()
        self.original.assert_called_once_with(self.callback)

    def test_running_step_finishes_before_the_final_sync_starts(self):
        self.session._working = True
        with patch.object(self.session, "start", side_effect=self.start) as start:
            self.begin()
            start.assert_not_called()
            self.session._working = False
            self.session.changed.emit()
            self.settle()
            start.assert_called_once()

    def test_failure_keeps_profile_open_and_allows_retry(self):
        with patch.object(self.session, "start", side_effect=self.start) as start:
            dialog = self.begin()
            self.session._failed(ConnectionError("Offline"))
            self.settle()
            self.assertIn("Offline", dialog.status.text())
            self.original.assert_not_called()
            dialog.retry.click()
            self.settle()
            self.assertEqual(2, start.call_count)
            self.session.error = None
            self.session._finished()
            self.settle()
        self.original.assert_called_once_with(self.callback)

    def test_staying_in_anki_does_not_cancel_or_repeat_the_active_sync(self):
        with patch.object(self.session, "start", side_effect=self.start):
            dialog = self.begin()
            dialog.reject()
            self.assertFalse(self.session.closed.is_set())
            self.session._finished()
            self.settle()
        self.original.assert_not_called()
        self.assertIsNone(self.mw._shirabe_profile_close)

    def test_skip_waits_for_collection_work_before_closing(self):
        with patch.object(self.session, "start", side_effect=self.start):
            dialog = self.begin()
            self.session._working = True
            dialog.skip.click()
            self.settle()
            self.assertTrue(self.session.closed.is_set())
            self.original.assert_not_called()
            self.session._working = False
            self.session.changed.emit()
            self.settle()
        self.original.assert_called_once_with(self.callback)

    def test_automatic_sync_disabled_does_not_add_a_shutdown_sync(self):
        self.session.automatic = False
        self.close_profile()
        self.mw.closeAllWindows.assert_not_called()
        self.original.assert_called_once_with(self.callback)

    def test_startup_error_is_visible_instead_of_escaping_the_qt_callback(self):
        with patch.object(self.session, "start", side_effect=OSError("Sidecar unavailable")):
            dialog = self.begin()
            self.assertIn("Sidecar unavailable", dialog.status.text())
        self.original.assert_not_called()

    def test_refused_delivery_start_does_not_silently_close_the_profile(self):
        def refuse():
            self.session._failed(ValueError("The sync binding changed"))
            return False

        with patch.object(self.session, "start", side_effect=refuse):
            dialog = self.begin()
            self.original.assert_not_called()
            self.assertIn("The sync binding changed", dialog.status.text())

    def test_skipped_worker_failure_releases_shutdown_without_starting_more_work(self):
        with patch.object(self.session, "start", side_effect=self.start):
            dialog = self.begin()
            self.session._working = True
            dialog.skip.click()
            self.session._failed(ConnectionError("Cancelled request failed"))
            self.settle()
        self.original.assert_called_once_with(self.callback)

    def test_skip_cancels_a_rate_limit_wait_without_repeating_the_request(self):
        retry = Mock()
        self.session._retry_operation = (retry, Mock(), None)
        self.session.retry_timer.start(60000)
        dialog = self.begin()
        dialog.skip.click()
        self.settle()
        self.assertFalse(self.session.retry_timer.isActive())
        retry.assert_not_called()
        self.original.assert_called_once_with(self.callback)

    def test_unpaired_profile_does_not_prevent_shutdown(self):
        self.begin()
        self.original.assert_called_once_with(self.callback)

    def test_cancelled_editor_close_can_be_requested_again(self):
        with patch.object(self.session, "start", side_effect=self.start) as start:
            self.close_profile()
            stale = self.mw.closeAllWindows.call_args.args[0]
            self.close_profile()
            saved = self.mw.closeAllWindows.call_args.args[0]
            stale()
            self.settle()
            start.assert_not_called()
            saved()
            saved()
            self.settle()
            start.assert_called_once()

    def test_late_editor_callback_cannot_reopen_a_closed_profile(self):
        self.close_profile()
        saved = self.mw.closeAllWindows.call_args.args[0]
        self.session.close()
        saved()
        self.settle()
        self.assertIsNone(getattr(self.mw, "_shirabe_profile_close", None))
        self.original.assert_not_called()

    def test_real_native_unload_only_runs_after_the_final_sync(self):
        from aqt import AnkiQt

        events = []
        self.original = lambda callback: AnkiQt.unloadProfile(self.mw, callback)
        self.mw._unloadProfile = lambda: events.append("profile-unloaded")
        self.mw.unloadCollection = lambda callback: (events.append("collection-unloaded"), callback())
        self.callback = lambda: events.append("finished")
        with patch.object(self.session, "start", side_effect=self.start), \
                patch("aqt.gui_hooks.profile_will_close", side_effect=lambda: events.append("will-close")):
            self.begin()
            self.assertEqual([], events)
            self.session._finished()
            self.settle()
        self.assertEqual(["will-close", "collection-unloaded", "profile-unloaded", "finished"], events)

    def test_skipping_real_background_network_waits_for_the_worker_without_blocking_qt(self):
        import weakref
        from aqt.qt import QTimer
        from aqt.taskman import TaskManager

        self.mw.weakref = lambda: weakref.proxy(self.mw)
        self.mw.taskman = TaskManager(self.mw)
        self.mw._increase_background_ops = Mock()
        self.mw._decrease_background_ops = Mock()
        release, entered = Event(), Event()
        threads, heartbeats = [], []
        timer = QTimer(self.mw)
        timer.timeout.connect(lambda: heartbeats.append(get_ident()))
        timer.start(1)

        def network():
            threads.append(get_ident())
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release the network step")

        def start():
            self.session._syncing = True
            return self.session._run(network, lambda _: self.fail("Skipped worker continued sync"))

        try:
            with patch("aqt.mw", self.mw), patch.object(self.session, "start", side_effect=start):
                dialog = self.begin()
                deadline = monotonic() + 3
                while (not entered.is_set() or not heartbeats) and monotonic() < deadline:
                    self.app.processEvents()
                self.assertTrue(entered.is_set())
                self.assertTrue(heartbeats)
                self.assertNotEqual(get_ident(), threads[0])
                self.assertEqual({get_ident()}, set(heartbeats))
                dialog.skip.click()
                self.settle()
                self.original.assert_not_called()
                release.set()
                deadline = monotonic() + 3
                while not self.original.called and monotonic() < deadline:
                    self.app.processEvents()
                self.original.assert_called_once_with(self.callback)
        finally:
            release.set()
            timer.stop()
            self.mw.taskman._no_collection_executor.shutdown(wait=True)
            self.mw.taskman._collection_executor.shutdown(wait=True)
