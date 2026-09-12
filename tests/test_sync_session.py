import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from gui_safety import offscreen_test_application
from shirabe_addon.sync_state import SyncState


@unittest.skipUnless(importlib.util.find_spec("aqt"), "Requires Anki's Qt runtime")
class SyncSessionTest(unittest.TestCase):
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
        self.mw.state = "deckBrowser"
        self.session = session_for(self.mw, "https://shirabe.example")

    def tearDown(self):
        self.session.close()
        self.mw.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def ready(self):
        from unittest.mock import Mock

        with SyncState(self.session.path) as state:
            state.put("initial_snapshot", {"phase": "uploaded", "profile": {"id": 123}, "pair_request": {"key": "epoch"}})
        self.session.login.credentials = lambda: {"access_token": "native"}
        self.session.collection_settings.capture = Mock(return_value=None)
        self.session.collection_settings.upload = Mock()
        self.session.collection_settings.prepare_publications = Mock()
        self.session.media.capture = Mock(return_value=0)
        self.session.media.exchange = Mock()
        self.session.media.apply = Mock(return_value=0)

    def test_one_session_is_shared_by_manual_and_automatic_callers(self):
        from shirabe_addon.sync_session import session_for
        self.assertIs(self.session, session_for(self.mw, "https://changed.example"))

    def test_publications_run_once_before_the_first_fresh_delivery(self):
        self.ready()
        from unittest.mock import Mock
        self.session.delivery = SimpleNamespace(receive=Mock(return_value={"changes": []}))
        self.session._apply_delivery = Mock()
        operations = []

        def run(operation, success, **options):
            operations.append(options)
            success(operation())

        with patch.object(self.session, "_run", side_effect=run):
            self.session._receive()
            self.session._receive()
        self.session.collection_settings.prepare_publications.assert_called_once()
        self.assertEqual(2, self.session.delivery.receive.call_count)
        self.assertEqual([{}, {}, {}], operations)

    def test_an_interrupted_native_delivery_is_recovered_before_publication(self):
        self.ready()
        from unittest.mock import Mock
        self.session.delivery = SimpleNamespace(receive=Mock(return_value={"changes": []}))
        self.session._apply_delivery = Mock()
        with SyncState(self.session.path) as state:
            state.put("delivery", {"phase": "applying"})
        with patch.object(self.session, "_run", side_effect=lambda operation, success: success(operation())):
            self.session._receive()
            self.session.collection_settings.prepare_publications.assert_not_called()
            with SyncState(self.session.path) as state:
                state.put("delivery", None)
            self.session._receive()
        self.session.collection_settings.prepare_publications.assert_called_once()

    def test_automatic_sync_requires_opt_in_and_ignores_its_own_operations(self):
        self.assertFalse(self.session.request())
        self.session.set_automatic(True)
        self.assertTrue(self.session.timer.isActive())
        self.session.timer.stop()
        self.assertFalse(self.session.request(initiator=self.session))
        self.assertFalse(self.session.timer.isActive())
        self.assertTrue(self.session.request(SimpleNamespace(note=True)))
        with SyncState(self.session.path) as state:
            self.assertTrue(state.get("automatic_sync"))

    def test_automatic_sync_waits_until_the_learner_leaves_the_reviewer(self):
        self.ready()
        self.session.set_automatic(True)
        self.mw.state = "review"
        with patch.object(self.session, "_run") as run:
            self.assertFalse(self.session.start(automatic=True))
            run.assert_not_called()
        self.assertTrue(self.session.timer.isActive())

    def test_initial_resolution_requires_a_manual_resume(self):
        self.ready()
        with SyncState(self.session.path) as state:
            captured = state.get("initial_snapshot")
            captured["phase"] = "resolving"
            state.put("initial_snapshot", captured)
        self.session.automatic = True
        with patch.object(self.session, "_run") as run:
            self.assertFalse(self.session.start(automatic=True))
            run.assert_not_called()

    def test_initial_resume_recovers_after_the_choice_was_applied_before_finishing_the_snapshot(self):
        self.ready()
        with SyncState(self.session.path) as state:
            captured = state.get("initial_snapshot")
            captured["phase"] = "resolving"
            state.put("initial_snapshot", captured)
        with patch.object(self.session, "_resume_initial") as resume, patch.object(self.session, "_capture") as capture:
            self.assertTrue(self.session.start())
            resume.assert_called_once_with()
            capture.assert_not_called()

    def test_sync_cycle_orders_collection_and_network_work_and_marks_its_own_mutations(self):
        self.ready()
        steps = []

        def run(operation, success, *, collection=False, mutating=False):
            steps.append("mutate" if mutating else "capture" if collection else "network")
            result = operation(self.mw.col) if collection or mutating else operation()
            success(result)
            return True

        with patch.object(self.session, "_run", side_effect=run), \
             patch.object(self.session.changes, "capture", return_value={"keys": []}), \
             patch.object(self.session.changes, "upload"), patch.object(self.session.changes, "apply"), \
             patch("shirabe_addon.sync_session.SyncDelivery") as delivery:
            delivery.return_value.receive.return_value = {"changes": []}
            self.assertTrue(self.session.start())
        self.assertEqual(["capture", "network", "mutate", "network", "network", "mutate", "network", "capture", "network", "mutate"], steps)
        self.assertFalse(self.session.busy)

    def test_media_changes_are_followed_by_a_fresh_capture_until_both_sides_are_quiet(self):
        self.ready()
        self.session.media.capture.side_effect = [1, 0, 0]
        self.session.media.apply.side_effect = [0, 1, 0]

        def run(operation, success, *, collection=False, mutating=False):
            success(operation(self.mw.col) if collection or mutating else operation())

        with patch.object(self.session, "_run", side_effect=run), \
             patch.object(self.session.changes, "capture", return_value={"keys": []}) as capture, \
             patch.object(self.session.changes, "upload"), patch.object(self.session.changes, "apply"), \
             patch("shirabe_addon.sync_session.SyncDelivery") as delivery:
            delivery.return_value.receive.return_value = {"changes": []}
            self.assertTrue(self.session.start())
        self.assertEqual(3, capture.call_count)
        self.assertEqual(3, self.session.media.exchange.call_count)
        self.assertFalse(self.session.busy)

    def test_incoming_media_waits_when_the_automatic_reviewer_opens(self):
        self.ready()
        self.session._automatic_run = self.session._syncing = True
        self.mw.state = "review"
        with patch.object(self.session, "_run") as run:
            self.session._apply_media(None)
            run.assert_not_called()
        self.session.media.apply.assert_not_called()

    def test_media_write_failure_notifies_anki_before_pausing(self):
        from anki.collection import OpChanges
        self.ready()
        self.session._syncing = True
        self.session.media.apply.side_effect = OSError("Media checkpoint failed")

        def run(operation, success, **options):
            self.assertTrue(options["mutating"])
            changes = operation(self.mw.col)
            self.assertIsInstance(changes, OpChanges)
            self.assertTrue(changes.note)
            success(changes)

        with patch.object(self.session, "_run", side_effect=run):
            self.session._apply_media(None)
        self.assertFalse(self.session.busy)
        self.assertEqual("Media checkpoint failed", self.session.error)

    def test_resuming_saved_work_keeps_capturing_until_later_source_edits_are_caught_up(self):
        self.ready()

        def run(operation, success, *, collection=False, mutating=False):
            success(operation(self.mw.col) if collection or mutating else operation())

        with patch.object(self.session, "_run", side_effect=run), \
             patch.object(self.session.changes, "capture", side_effect=[{"keys": ["saved"]}, {"keys": ["later-edit"]}, {"keys": []}]) as capture, \
             patch.object(self.session.changes, "upload"), patch.object(self.session.changes, "apply"), \
             patch("shirabe_addon.sync_session.SyncDelivery") as delivery:
            delivery.return_value.receive.return_value = {"changes": []}
            self.assertTrue(self.session.start())
        self.assertEqual(3, capture.call_count)
        self.assertEqual("Saved changes exchanged.", self.session.status)

    def test_incoming_changes_are_followed_by_a_fresh_source_capture(self):
        self.ready()

        def run(operation, success, *, collection=False, mutating=False):
            success(operation(self.mw.col) if collection or mutating else operation())

        with patch.object(self.session, "_run", side_effect=run), \
             patch.object(self.session.changes, "capture", return_value={"keys": []}) as capture, \
             patch.object(self.session.changes, "upload"), patch.object(self.session.changes, "apply"), \
             patch("shirabe_addon.sync_session.SyncDelivery") as delivery:
            delivery.return_value.receive.side_effect = [{"changes": [{"cursor": 1}]}, {"changes": []}, {"changes": []}]
            self.assertTrue(self.session.start())
        self.assertEqual(2, capture.call_count)

    def test_applied_outgoing_batches_are_rescanned_before_receiving_deferred_reviews(self):
        self.session._fresh_scan_needed = True

        def run(operation, success, **options):
            success(operation(self.mw.col))

        with patch.object(self.session, "_run", side_effect=run), \
             patch.object(self.session.changes, "apply"), \
             patch.object(self.session, "_capture") as capture, \
             patch.object(self.session, "_receive") as receive:
            self.session._apply_outgoing(None)
        capture.assert_called_once()
        receive.assert_not_called()

    def test_resumed_creation_captures_native_progress_before_receiving_deferred_reviews(self):
        self.ready()
        with SyncState(self.session.path) as state:
            state.put("delivery", {"response": {"changes": [{"entity_kind": "note", "payload": {"creation": {}}}]}})
        steps = []
        pages = iter([{"changes": [{"entity_kind": "note", "payload": {"creation": {}}}]}, {"changes": []}])

        def run(operation, success, *, collection=False, mutating=False):
            success(operation(self.mw.col) if collection or mutating else operation())

        def capture(_):
            steps.append("capture")
            return {"keys": []}

        def receive():
            steps.append("receive")
            return next(pages)

        def acknowledge():
            steps.append("acknowledge")
            with SyncState(self.session.path) as state:
                state.put("delivery", None)

        with patch.object(self.session, "_run", side_effect=run), \
             patch.object(self.session.changes, "capture", side_effect=capture), \
             patch.object(self.session.changes, "upload"), patch.object(self.session.changes, "apply"), \
             patch("shirabe_addon.sync_session.SyncDelivery") as delivery:
            delivery.return_value.receive.side_effect = receive
            delivery.return_value.acknowledge.side_effect = acknowledge
            self.assertTrue(self.session.start())
        self.assertEqual(["receive", "acknowledge", "capture", "receive", "acknowledge"], steps)
        self.assertFalse(self.session.busy)

    def test_received_delivery_uploads_local_edits_first_but_started_writes_resume_first(self):
        self.ready()
        for phase in ("received", "applying"):
            with self.subTest(phase=phase):
                self.session._syncing = False
                with SyncState(self.session.path) as state:
                    state.put("delivery", {"phase": phase, "response": {"changes": []}})
                with patch.object(self.session, "_capture") as capture, patch.object(self.session, "_receive") as receive:
                    self.assertTrue(self.session.start())
                    self.assertEqual(phase == "received", capture.called)
                    self.assertEqual(phase == "applying", receive.called)

    def test_manual_sync_tracks_edits_during_its_cycle_without_enabling_automatic_sync(self):
        self.assertFalse(self.session.automatic)
        self.session._syncing = True
        self.assertTrue(self.session.request(SimpleNamespace(note=True)))
        self.assertTrue(self.session._pending)
        self.assertFalse(self.session.timer.isActive())

    def test_an_external_edit_during_sync_requests_one_followup_cycle(self):
        self.session.set_automatic(True)
        self.session.timer.stop()
        self.session._syncing = True
        for _ in range(3):
            self.session.request(SimpleNamespace(card=True))
        self.assertFalse(self.session.timer.isActive())
        self.session._finished()
        self.assertTrue(self.session.timer.isActive())

    def test_manual_work_cannot_overlap_an_active_sync_cycle(self):
        self.session._syncing = True
        with patch.object(self.session, "_run") as run:
            self.assertFalse(self.session.run(lambda: None, lambda _: None))
            run.assert_not_called()

    def test_collection_mutations_identify_the_session_to_anki_hooks(self):
        with patch("shirabe_addon.sync_session.CollectionOp") as operation:
            self.session.run(lambda col: None, lambda _: None, mutating=True)
            operation.return_value.success.return_value.failure.return_value.run_in_background.assert_called_once_with(initiator=self.session)

    def test_opening_the_reviewer_during_upload_defers_collection_writes(self):
        self.session.set_automatic(True)
        self.session._automatic_run = self.session._syncing = True
        self.mw.state = "review"
        with patch.object(self.session, "_run") as run:
            self.session._apply_outgoing(None)
            run.assert_not_called()
        self.assertFalse(self.session.busy)
        self.assertTrue(self.session.timer.isActive())

    def test_failure_pauses_automatic_retries_until_manual_attention(self):
        self.session.set_automatic(True)
        self.session._failed(ValueError("Resolve the note conflict"))
        self.assertFalse(self.session.timer.isActive())
        self.assertFalse(self.session.request())
        self.assertIn("Resolve the note conflict", self.session.status)

    def test_rate_limit_wait_keeps_the_network_step_busy_then_resumes_it(self):
        from shirabe_addon.sync_client import SyncError
        from unittest.mock import Mock

        operation, completed = Mock(), Mock()
        error = SyncError(429, "RATE_LIMIT_EXCEEDED")
        error.retry_after = 7
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            self.session.run(operation, completed)
            query.return_value.failure.call_args.args[0](error)
            self.assertTrue(self.session.busy)
            self.assertIsNone(self.session.error)
            self.assertFalse(self.session.run(operation, completed))
            self.assertEqual(7000, self.session.retry_timer.interval())
            self.assertEqual(1, query.call_count)
            self.session.retry_timer.timeout.emit()
            self.assertEqual(2, query.call_count)
            query.call_args.kwargs["op"](None)
            query.call_args.kwargs["success"]("accepted")
        operation.assert_called_once_with()
        completed.assert_called_once_with("accepted")
        self.assertFalse(self.session.busy)

    def test_profile_close_cancels_a_rate_limit_wait(self):
        from shirabe_addon.sync_client import SyncError

        error = SyncError(429, "RATE_LIMIT_EXCEEDED")
        error.retry_after = 7
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            self.session.run(lambda: None, lambda _: self.fail("Closed profile resumed"))
            query.return_value.failure.call_args.args[0](error)
            self.assertTrue(self.session.busy)
            self.session.close()
            self.assertFalse(self.session.retry_timer.isActive())
            self.assertFalse(self.session.busy)
            self.session.retry_timer.timeout.emit()
            self.assertEqual(1, query.call_count)

    def test_pending_media_wait_retries_the_same_network_step(self):
        from shirabe_addon.sync_client import SyncError
        from unittest.mock import Mock

        operation, completed = Mock(), Mock()
        error = SyncError(503, "MEDIA_NOT_READY", retry_after=5)
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            self.session.run(operation, completed)
            query.return_value.failure.call_args.args[0](error)
            self.assertTrue(self.session.busy)
            self.assertIsNone(self.session.error)
            self.assertEqual(5000, self.session.retry_timer.interval())
            self.session.retry_timer.timeout.emit()
            self.assertEqual(2, query.call_count)
            query.call_args.kwargs["op"](None)
            query.call_args.kwargs["success"]("accepted")
        operation.assert_called_once_with()
        completed.assert_called_once_with("accepted")
        self.assertFalse(self.session.busy)

    def test_dialog_cancellation_stops_its_wait_but_not_an_independent_sync(self):
        from shirabe_addon.sync_client import SyncError
        from threading import Event

        cancelled = Event()
        error = SyncError(429, "RATE_LIMIT_EXCEEDED", retry_after=7)
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            self.session.run(lambda: None, lambda _: None, cancelled=cancelled.is_set)
            query.return_value.failure.call_args.args[0](error)
            self.session.cancel_wait()
            self.assertTrue(self.session.busy)
            cancelled.set()
            self.session.cancel_wait()
            self.assertFalse(self.session.busy)
            self.assertFalse(self.session.retry_timer.isActive())
            self.session.retry_timer.timeout.emit()
            self.assertEqual(1, query.call_count)

    def test_a_response_after_dialog_close_does_not_start_a_wait(self):
        from shirabe_addon.sync_client import SyncError
        from threading import Event

        cancelled = Event()
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            self.session.run(lambda: None, lambda _: None, cancelled=cancelled.is_set)
            cancelled.set()
            query.return_value.failure.call_args.args[0](SyncError(429, "RATE_LIMIT_EXCEEDED", retry_after=7))
        self.assertFalse(self.session.busy)
        self.assertFalse(self.session.retry_timer.isActive())

    def test_other_failures_and_collection_operations_do_not_retry(self):
        from shirabe_addon.sync_client import SyncError

        for error, collection in ((SyncError(429, "RATE_LIMIT_EXCEEDED"), False),
                                  (SyncError(409, "CONFLICT", retry_after=7), False),
                                  (SyncError(429, "OTHER_LIMIT", retry_after=7), False),
                                  (SyncError(429, "RATE_LIMIT_EXCEEDED", retry_after=7), True)):
            with self.subTest(error=error, collection=collection), patch("shirabe_addon.sync_session.QueryOp") as query:
                self.session.run(lambda: None, lambda _: None, collection=collection)
                query.return_value.failure.call_args.args[0](error)
                self.assertFalse(self.session.busy)
                self.assertFalse(self.session.retry_timer.isActive())
                self.assertEqual(str(error), self.session.error)

    def test_partial_acceptance_notifies_anki_before_pausing_the_cycle(self):
        from anki.collection import OpChanges
        from shirabe_addon.sync_changes import SyncConflict
        problem = {"id": 123, "kind": "notes", "status": "conflict", "code": "field_conflict"}

        def run(operation, success, **options):
            self.assertTrue(options["mutating"])
            changes = operation(self.mw.col)
            self.assertIsInstance(changes, OpChanges)
            self.assertTrue(changes.note)
            success(changes)

        self.session._syncing = True
        with patch.object(self.session.changes, "apply", side_effect=SyncConflict([problem])), \
             patch.object(self.session, "_run", side_effect=run), patch.object(self.session, "_receive") as receive:
            self.session._apply_outgoing(None)
            receive.assert_not_called()
        self.assertFalse(self.session.busy)
        self.assertEqual([problem], self.session.conflicts)
        self.assertIn("1 sync record needs", self.session.error)

    def test_failed_collection_applies_notify_anki_even_when_a_later_sidecar_write_fails(self):
        from anki.collection import OpChanges
        from shirabe_addon.sync_delivery import DeliveryConflict
        problem = {"direction": "incoming", "kind": "notes", "id": 123, "cursor": 1, "code": "field conflict"}
        self.session.delivery = SimpleNamespace(apply=lambda _: None, acknowledge=lambda: self.fail("Failed delivery was acknowledged"))

        def run(operation, success, **options):
            self.assertTrue(options["mutating"])
            changes = operation(self.mw.col)
            self.assertIsInstance(changes, OpChanges)
            self.assertTrue(changes.note)
            success(changes)

        for outgoing in (True, False):
            failures = [OSError("Sidecar unavailable")] if outgoing else [OSError("Sidecar unavailable"), DeliveryConflict("Incoming conflict", [problem])]
            for error in failures:
                with self.subTest(outgoing=outgoing, error=error):
                    self.session._syncing = True
                    target = self.session.changes if outgoing else self.session.delivery
                    with patch.object(target, "apply", side_effect=error), patch.object(self.session, "_run", side_effect=run):
                        if outgoing:
                            self.session._apply_outgoing(None)
                        else:
                            self.session._apply_delivery({"changes": [{"cursor": 1}]})
                    self.assertEqual(str(error), self.session.error)
                    self.assertEqual(getattr(error, "conflicts", []), self.session.conflicts)
                    self.assertFalse(self.session.busy)

    def test_profile_close_stops_scheduling_and_late_callbacks(self):
        self.session.set_automatic(True)
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            self.session.run(lambda: {}, lambda _: self.fail("Callback ran after profile close"))
            completed = query.call_args.kwargs["success"]
            self.session.close()
            completed({})
        self.assertFalse(self.session.timer.isActive())
        self.assertFalse(self.session.request())

    def test_queued_collection_work_checks_profile_identity_before_running(self):
        with patch("shirabe_addon.sync_session.QueryOp") as query:
            self.session.run(lambda col: self.fail("Wrong collection used"), lambda _: None, collection=True)
            work = query.call_args.kwargs["op"]
            with self.assertRaisesRegex(ValueError, "profile changed"):
                work(SimpleNamespace(path="/different/collection.anki2"))
