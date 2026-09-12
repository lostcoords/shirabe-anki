import copy
from pathlib import Path
import tempfile
import unittest

import support
from shirabe_addon.sync_collection import SyncCollection
from shirabe_addon.sync_snapshot import SyncSnapshot, SnapshotConflict
from shirabe_addon.sync_state import SyncState
from test_sync_snapshot import Collection, SnapshotServer, version


class SettingsServer(SnapshotServer):
    def __init__(self):
        super().__init__()
        self.settings = []
        self.settings_receipts = {}
        self.lose_settings = False
        self.publication_requests = []
        self.publication_receipts = {}
        self.lose_publication = False

    def prepare_publications(self, profile_id, payload):
        self.publication_requests.append(copy.deepcopy(payload))
        receipt = self.publication_receipts.setdefault(payload["request_key"],
            {"request_key": payload["request_key"], "after": 100 if payload["after"] == 0 else 101,
             "through": 101, "complete": payload["after"] != 0})
        if self.lose_publication:
            self.lose_publication = False
            raise ConnectionError("Lost publication response")
        return receipt

    def collection_settings(self, profile_id, payload):
        self.settings.append(copy.deepcopy(payload))
        receipt = self.settings_receipts.setdefault(payload["request_key"],
            {"request_key": payload["request_key"], "revision": payload["base_revision"] + 1})
        if self.lose_settings:
            self.lose_settings = False
            raise ConnectionError("Lost settings response")
        return receipt


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned native Anki runtime")
class SyncCollectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.collection = Collection(str(self.root / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        self.path = self.root / "sync.sqlite3"
        self.server = SettingsServer()
        self.deck_id = self.collection.decks.id("Japanese")
        note = self.collection.new_note(self.collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        self.collection.add_note(note, self.deck_id)
        snapshot = SyncSnapshot(self.path, self.server)
        snapshot.capture(self.collection, [self.deck_id])
        snapshot.upload()
        self.settings = SyncCollection(self.path, self.server)

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def test_publication_pages_resume_the_same_request_after_a_lost_response(self):
        self.server.lose_publication = True
        with self.assertRaises(ConnectionError):
            self.settings.prepare_publications()
        self.settings = SyncCollection(self.path, self.server)
        self.settings.prepare_publications()
        requests = self.server.publication_requests
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(100, requests[2]["after"])
        self.assertEqual(101, requests[2]["through"])
        self.assertNotEqual(requests[1]["request_key"], requests[2]["request_key"])
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("publications"))

    def test_publication_does_not_depend_on_changed_native_settings(self):
        self.settings.capture(self.collection)
        self.settings.upload()
        self.assertIsNone(self.settings.capture(self.collection))
        self.settings.prepare_publications()
        self.settings.prepare_publications()
        self.assertEqual([0, 100, 0, 100], [request["after"] for request in self.server.publication_requests])

    def test_cancellation_between_pages_retains_the_next_page_and_its_upper_bound(self):
        with self.assertRaises(SnapshotConflict):
            self.settings.prepare_publications(cancelled=lambda: len(self.server.publication_requests) == 1)
        self.settings.prepare_publications()
        self.assertEqual([0, 100], [request["after"] for request in self.server.publication_requests])

    def test_invalid_publication_receipts_cannot_advance_saved_progress(self):
        original = self.server.prepare_publications
        for change in ({"request_key": "different"}, {"after": -1}, {"after": 0},
                       {"after": 102}, {"through": "101"}, {"complete": 1}):
            self.server.prepare_publications = lambda profile_id, payload: {**original(profile_id, payload), **change}
            with self.assertRaises(SnapshotConflict):
                self.settings.prepare_publications()
            with SyncState(self.path) as state:
                self.assertEqual(0, state.get("publications")["request"]["after"])

    def test_publication_refuses_a_changed_sync_epoch(self):
        self.server.lose_publication = True
        with self.assertRaises(ConnectionError):
            self.settings.prepare_publications()
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            snapshot["pair_request"]["key"] = "changed"
            state.put("initial_snapshot", snapshot)
        with self.assertRaises(SnapshotConflict):
            self.settings.prepare_publications()
        self.assertEqual(1, len(self.server.publication_requests))

    def test_zero_leech_threshold_is_normalized_before_settings_are_captured(self):
        from anki.deck_config_pb2 import UpdateDeckConfigsRequest

        preset = self.collection.decks.config_dict_for_deck_id(self.deck_id)
        preset["lapse"]["leechFails"] = 0
        self.collection.decks.update_config(preset)
        self.assertEqual(8, self.collection.decks.config_dict_for_deck_id(self.deck_id)["lapse"]["leechFails"])
        settings = self.collection.decks.get_deck_configs_for_update(self.deck_id)
        config = next(item.config for item in settings.all_config if item.config.id == preset["id"])
        config.config.leech_threshold = 0
        self.collection.decks.update_deck_configs(UpdateDeckConfigsRequest(target_deck_id=self.deck_id, configs=[config], fsrs=True))
        pending = self.settings.capture(self.collection)
        captured = pending["request"]["source_metadata"]["dconf"][str(preset["id"])]
        self.assertEqual(8, captured["lapse"]["leechFails"])

    def test_missing_leech_action_defaults_to_tag_only(self):
        preset = self.collection.decks.config_dict_for_deck_id(self.deck_id)
        del preset["lapse"]["leechAction"]
        self.collection.decks.update_config(preset)
        pending = self.settings.capture(self.collection)
        captured = pending["request"]["source_metadata"]["dconf"][str(preset["id"])]
        self.assertEqual(1, captured["lapse"]["leechAction"])

    def test_disabling_fsrs_preserves_pending_settings_and_the_sync_epoch(self):
        from shirabe_addon.sync_reviews import ReviewConflict

        pending = self.settings.capture(self.collection)
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
        self.collection.set_config("fsrs", False)
        with self.assertRaisesRegex(ReviewConflict, "Enable FSRS"):
            self.settings.capture(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual(pending, state.get("collection_settings"))
            self.assertEqual(snapshot, state.get("initial_snapshot"))
        self.assertEqual([], self.server.settings)
        self.collection.set_config("fsrs", True)
        self.assertEqual(pending, self.settings.capture(self.collection))
        self.settings.upload()
        self.assertEqual([pending["request"]], self.server.settings)

    def test_lost_response_keeps_the_frozen_snapshot_then_captures_later_native_settings(self):
        first = self.settings.capture(self.collection)
        self.server.lose_settings = True
        with self.assertRaises(ConnectionError):
            self.settings.upload()
        deck = self.collection.decks.get(self.deck_id)
        deck["newLimit"] = 7
        self.collection.decks.update(deck)
        self.settings = SyncCollection(self.path, self.server)
        self.assertEqual(first, self.settings.capture(self.collection))
        self.settings.upload()
        self.assertEqual(self.server.settings[0], self.server.settings[1])
        second = self.settings.capture(self.collection)
        self.assertEqual(1, second["request"]["base_revision"])
        self.assertEqual(7, second["request"]["source_metadata"]["decks"][str(self.deck_id)]["newLimit"])
        self.settings.upload()
        self.assertIsNone(self.settings.capture(self.collection))

    def test_the_acknowledged_prefix_is_captured_even_when_no_native_settings_changed(self):
        self.settings.capture(self.collection)
        self.settings.upload()
        self.assertIsNone(self.settings.capture(self.collection))
        with SyncState(self.path) as state:
            state.put("delivery", {"cursor": 3})
        with self.assertRaisesRegex(SnapshotConflict, "pending delivery"):
            self.settings.capture(self.collection)
        with SyncState(self.path) as state:
            state.put("delivery", None)
            state.put("delivery_cursor", 3)
        captured = self.settings.capture(self.collection)
        self.assertEqual(3, captured["request"]["source_metadata"]["capture"]["acknowledged_cursor"])

    def test_saved_settings_cannot_be_sent_to_another_epoch(self):
        self.settings.capture(self.collection)
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            snapshot["pair_request"]["key"] = "different"
            state.put("initial_snapshot", snapshot)
        with self.assertRaisesRegex(SnapshotConflict, "another sync epoch"):
            self.settings.upload()
        self.assertEqual([], self.server.settings)

    def test_an_untouched_delivery_allows_capture_but_started_application_does_not(self):
        with SyncState(self.path) as state:
            state.remember_delivery(0, {"cursor": 3, "changes": []})
        captured = self.settings.capture(self.collection)
        self.assertEqual(0, captured["request"]["source_metadata"]["capture"]["acknowledged_cursor"])
        with SyncState(self.path) as state:
            state.begin_delivery()
        with self.assertRaisesRegex(SnapshotConflict, "pending delivery"):
            self.settings.capture(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual(captured, state.get("collection_settings"))

    def test_a_changed_receipt_does_not_discard_the_pending_capture(self):
        captured = self.settings.capture(self.collection)
        self.server.settings_receipts[captured["request"]["request_key"]] = {"request_key": "different", "revision": 1}
        with self.assertRaisesRegex(SnapshotConflict, "different collection settings receipt"):
            self.settings.upload()
        with SyncState(self.path) as state:
            self.assertEqual(captured, state.get("collection_settings"))
            self.assertIsNone(state.get("collection_revision"))
