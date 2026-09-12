from __future__ import annotations

import copy
import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import support
from shirabe_addon.sync_reviews import apply_review, card_row, history_row, same_state, STATE_COLUMNS
from shirabe_addon.sync_delivery import SyncDelivery, DeliveryConflict
from shirabe_addon.sync_state import SyncState
from test_sync_delivery import Server
from native_sync_server import native_sync_server, full_sync

try:
    from anki.collection import Collection
    from anki.buildinfo import version
    from anki.errors import SyncError
except ImportError:
    Collection = None
    version = None


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncHistoryProtocolTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shirabe-history-protocol-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.endpoint = self.enterContext(native_sync_server(root))
        self.collection = Collection(str(root / "desktop.anki2"))
        self.collection.set_config("fsrs", True)
        self.addCleanup(self.collection.close)
        self.receiver = Collection(str(root / "receiver.anki2"))
        self.addCleanup(self.receiver.close)
        self.sidecar = root / "sidecar.sqlite3"
        self.state = SyncState(self.sidecar)
        self.addCleanup(self.state.close)
        note = self.collection.new_note(self.collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        self.collection.add_note(note, self.collection.decks.id("Protocol probe"))
        self.card_id = note.cards()[0].id
        self.auth = self.collection.sync_login("probe", "disposable", self.endpoint)
        self.assertEqual(self.endpoint, self.auth.endpoint)
        self.collection.sync_collection(self.auth, False)
        self.full_sync(self.collection, upload=True)
        self.receiver.sync_collection(self.auth, False)
        self.full_sync(self.receiver, upload=False)

    def full_sync(self, collection, *, upload):
        full_sync(collection, self.auth, upload=upload)

    def deliver_review(self):
        before = card_row(self.collection, self.card_id)
        after = copy.deepcopy(before)
        instant = datetime.now(timezone.utc)
        after.update(type=2, queue=2, due=3, ivl=3, reps=1, left=0,
                     data=json.dumps({"s": 3.12345, "d": 2.1181, "lrt": int(instant.timestamp())}))
        note = self.collection.get_note(before["nid"])
        payload = {
            "sync_id": str(uuid.uuid4()), "anki_card_id": self.card_id,
            "rating": 3, "reviewed_at": instant.isoformat(),
            "anki": {"card_before": before, "card_after": after, "note_guid": note.guid, "anki_type_id": note.mid,
                     "revlog": {"cid": self.card_id, "usn": -1, "ease": 3, "ivl": 3, "lastIvl": 0,
                                "factor": 200, "time": 123, "type": 0}},
        }
        result = apply_review(self.collection, self.state, payload)
        self.before_review = before
        self.after_review = card_row(self.collection, self.card_id)
        return result["row"]["id"]

    def delete_review_and_restore_card(self, review_id):
        def remove():
            self.collection.db.execute("DELETE FROM revlog WHERE id = ?", review_id)
            card = self.collection.get_card(self.card_id)
            for key in STATE_COLUMNS:
                setattr(card, key, self.before_review[key])
            card.memory_state = None
            card.last_review_time = None
            self.collection.update_card(card)

        self.collection.db.transact(remove)
        self.assertTrue(same_state(card_row(self.collection, self.card_id), self.before_review))
        # Anki skips sync when millisecond modification stamps match the server.
        remote_mod = self.receiver.db.scalar("SELECT mod FROM col")
        self.collection.db.execute("UPDATE col SET mod = max(mod, ?) + 1", remote_mod)

    def receive_undo(self, review_id):
        row = history_row(self.collection, review_id)
        note = self.collection.get_note(self.before_review["nid"])
        server = Server()
        server.page = {"cursor": 1, "changes": [{"cursor": 1, "entity_kind": "card", "entity_id": 30, "operation": "upsert",
            "payload": {"snapshot": "undo", "card": {"anki_id": self.card_id, "reset_generation": 1},
                "anki": {"card_before": self.after_review, "card_after": self.before_review, "note_guid": note.guid,
                         "anki_type_id": note.mid, "removed_history": [row],
                         "history_removals": [{"id": row["id"], "removal_id": 9}]}}}]}
        delivery = SyncDelivery(self.sidecar, server, profile_id=123, key="epoch")
        delivery.receive()
        return delivery, server

    def test_addon_undo_before_native_sync_keeps_both_collections_consistent(self):
        review_id = self.deliver_review()
        delivery, server = self.receive_undo(review_id)
        delivery.apply(self.collection)
        delivery.acknowledge()
        self.assertEqual(1, server.acknowledged)
        self.assertIsNone(history_row(self.collection, review_id))
        self.collection.sync_collection(self.auth, False)
        self.receiver.sync_collection(self.auth, False)
        self.assertIsNone(history_row(self.receiver, review_id))
        self.assertTrue(same_state(card_row(self.receiver, self.card_id), self.before_review))

    def test_addon_undo_refuses_a_review_published_after_the_request_arrived(self):
        review_id = self.deliver_review()
        delivery, server = self.receive_undo(review_id)
        self.collection.sync_collection(self.auth, False)
        self.receiver.sync_collection(self.auth, False)
        published = history_row(self.collection, review_id)
        self.assertGreaterEqual(published["usn"], 0)
        with self.assertRaises(DeliveryConflict) as caught:
            delivery.apply(self.collection)
        self.assertIn("native sync", caught.exception.conflicts[0]["code"])
        self.assertEqual(0, server.acknowledged)
        self.assertEqual(published, history_row(self.collection, review_id))
        self.assertTrue(same_state(card_row(self.collection, self.card_id), self.after_review))
        self.collection.sync_collection(self.auth, False)
        self.receiver.sync_collection(self.auth, False)
        self.assertIsNotNone(history_row(self.receiver, review_id))

    def confirm_interrupted_undo_in_dialog(self, side):
        from gui_safety import offscreen_test_application
        application = offscreen_test_application([])
        from aqt.qt import QDialogButtonBox
        from shirabe_addon.sync_conflict_ui import HistoryRemovalDialog

        review_id = self.deliver_review()
        delivery, server = self.receive_undo(review_id)
        self.state.put("initial_snapshot", {"api": server.api, "phase": "uploaded", "profile": {"id": 123},
            "pair_request": {"key": "epoch"}, "collection_path": str(Path(self.collection.path).resolve()), "deck_ids": [self.before_review["did"]]})
        with patch.object(self.collection, "update_card", side_effect=RuntimeError("injected card failure")), self.assertRaises(RuntimeError):
            delivery.apply(self.collection)
        with self.assertRaises(DeliveryConflict) as caught:
            delivery.apply(self.collection)
        prepared = delivery.prepare_card_resolution(self.collection, caught.exception.conflicts[0])
        dialog = HistoryRemovalDialog(None, prepared, lambda schedule: delivery.resolve_history_removal(self.collection, prepared, schedule=schedule))
        dialog.show()
        application.processEvents()
        dialog.schedule.setCurrentIndex(dialog.schedule.findData(side))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        self.assertIsNotNone(self.state.delivery_choice(1))
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            delivery.apply(self.collection)
        delivery.apply(self.collection)
        delivery.acknowledge()
        self.assertEqual(1, server.acknowledged)
        self.assertIsNone(history_row(self.collection, review_id))
        self.collection.sync_collection(self.auth, False)
        self.receiver.sync_collection(self.auth, False)
        self.assertIsNone(history_row(self.receiver, review_id))
        expected = self.after_review if side == "anki" else self.before_review
        self.assertTrue(same_state(card_row(self.receiver, self.card_id), expected))
        dialog.deleteLater()
        application.processEvents()

    @unittest.skipUnless(importlib.util.find_spec("aqt"), "Requires the pinned offscreen Anki Qt runtime")
    def test_incoming_schedule_dialog_recovers_interrupted_undo_and_native_sync(self):
        self.confirm_interrupted_undo_in_dialog("shirabe")

    @unittest.skipUnless(importlib.util.find_spec("aqt"), "Requires the pinned offscreen Anki Qt runtime")
    def test_anki_schedule_dialog_recovers_interrupted_undo_and_native_sync(self):
        self.confirm_interrupted_undo_in_dialog("anki")

    def test_normal_sync_clears_native_undo(self):
        card = self.collection.get_card(self.card_id)
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.assertTrue(self.collection.undo_status().undo)
        self.collection.undo()
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        self.collection.redo()
        self.assertTrue(self.collection.undo_status().undo)
        self.collection.sync_collection(self.auth, False)
        self.assertFalse(self.collection.undo_status().undo)
        self.assertFalse(self.collection.undo_status().redo)

    def test_deleting_an_unsynced_delivered_review_does_not_publish_it(self):
        review_id = self.deliver_review()
        self.assertFalse(self.collection.undo_status().undo)
        self.assertEqual(-1, self.collection.db.scalar("SELECT usn FROM revlog WHERE id = ?", review_id))
        self.delete_review_and_restore_card(review_id)
        self.collection.sync_collection(self.auth, False)
        self.receiver.sync_collection(self.auth, False)
        self.assertEqual(0, self.receiver.db.scalar("SELECT count(*) FROM revlog"))
        self.assertTrue(same_state(card_row(self.receiver, self.card_id), self.before_review))

    def test_deleting_a_synced_delivered_review_requires_full_sync_and_download_restores_it(self):
        review_id = self.deliver_review()
        self.collection.sync_collection(self.auth, False)
        self.receiver.sync_collection(self.auth, False)
        self.assertGreaterEqual(self.collection.db.scalar("SELECT usn FROM revlog WHERE id = ?", review_id), 0)
        self.assertEqual(1, self.receiver.db.scalar("SELECT count(*) FROM revlog WHERE id = ?", review_id))
        self.delete_review_and_restore_card(review_id)
        with self.assertRaises(SyncError) as failure:
            self.collection.sync_collection(self.auth, False)
        self.assertIn("one-way sync", str(failure.exception))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog WHERE id = ?", review_id))
        self.assertEqual(1, self.receiver.db.scalar("SELECT count(*) FROM revlog WHERE id = ?", review_id))
        self.assertTrue(same_state(card_row(self.receiver, self.card_id), self.after_review))
        self.full_sync(self.collection, upload=False)
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog WHERE id = ?", review_id))
        self.assertTrue(same_state(card_row(self.collection, self.card_id), self.after_review))


if __name__ == "__main__":
    unittest.main()
