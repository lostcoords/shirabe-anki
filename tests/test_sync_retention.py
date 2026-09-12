from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from shirabe_addon.sync_state import SyncState


class SyncRetentionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sync.sqlite3"
        self.state = SyncState(self.path)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def completed_batch(self, fields="猫"):
        batch = self.state.enqueue(key="epoch", notes=[{"id": 12, "flds": fields}])
        self.state.remember_receipt(batch["request_key"], {"notes": [{"id": 12, "status": "accepted"}]})
        self.state.complete_batch(batch["request_key"], [("note", 12, "digest", {"fields": ["猫"]})])
        return batch["request_key"]

    def test_only_applied_payloads_are_removed_and_checkpoints_and_credentials_survive(self):
        finished = self.completed_batch()
        pending = self.state.enqueue(key="epoch", notes=[{"id": 13}])
        received = self.state.enqueue(key="epoch", notes=[{"id": 14}])
        self.state.remember_receipt(received["request_key"], {"notes": [{"id": 14, "status": "conflict"}]})
        self.state.put("native_login", {"refresh_token": "private"})
        self.assertEqual(1, self.state.prune())
        with self.assertRaises(ValueError):
            self.state.batch(finished)
        self.assertEqual(pending, self.state.batch(pending["request_key"])["payload"])
        self.assertIsNotNone(self.state.batch(received["request_key"])["receipt"])
        self.assertEqual({"fields": ["猫"]}, self.state.checkpoint("note", 12)["payload"])
        self.assertEqual({"refresh_token": "private"}, self.state.get("native_login"))
        self.assertEqual(0, self.state.prune())

    def test_snapshot_cycle_and_delivery_guards_keep_their_applied_receipts(self):
        key = self.completed_batch()
        for setting, value in (("initial_snapshot", {"phase": "captured"}),
                               ("changes", {"keys": [key]}), ("delivery", {"return_key": key})):
            with self.subTest(setting=setting):
                self.state.put(setting, value)
                self.assertEqual(0, self.state.prune())
                self.assertIsNotNone(self.state.batch(key)["receipt"])
                self.state.put(setting, None)
        self.assertEqual(1, self.state.prune())

    def test_completed_snapshot_indexes_are_retired_with_the_payloads(self):
        snapshot = {"pair_request": {"key": "epoch"}, "phase": "captured"}
        self.state.stage_snapshot(snapshot, [{"notes": [{"id": 12}]}])
        key = self.state.snapshot_batch_keys()[0]
        self.state.remember_receipt(key, {"notes": [{"id": 12, "status": "accepted"}]})
        self.state.complete_batch(key, [("source_note", 12, "source-digest", {"id": 12})])
        self.state.put("initial_snapshot", {**snapshot, "phase": "uploaded"})
        self.assertEqual(1, self.state.prune())
        self.assertEqual([], self.state.snapshot_batch_keys())
        self.assertEqual("source-digest", self.state.checkpoint_digest("source_note", 12))
        self.assertEqual("uploaded", self.state.get("initial_snapshot")["phase"])

    def test_per_entity_progress_survives_reopening_and_is_retired_with_its_batch(self):
        batch = self.state.enqueue(key="epoch", notes=[{"id": 12}])
        key = batch["request_key"]
        self.state.remember_receipt(key, {"notes": [{"id": 12, "status": "accepted"}]})
        self.state.complete_entity(key, "notes", 12, [("note", 12, "digest", {"fields": ["猫"]})], cursor=3)
        self.state.close()
        self.state = SyncState(self.path)
        self.assertTrue(self.state.entity_applied(key, "notes", 12))
        self.assertEqual(3, self.state.accepted_cursor("notes", 12))
        self.assertFalse(self.state.batch_applied(key))
        self.assertEqual(0, self.state.prune())
        self.state.complete_batch(key, [])
        self.assertEqual(0, self.state.prune())
        self.state.put("delivery_cursor", 3)
        self.assertEqual(1, self.state.prune())
        self.assertFalse(self.state.entity_applied(key, "notes", 12))
        self.assertEqual("digest", self.state.checkpoint_digest("note", 12))

    def test_entity_progress_and_checkpoints_roll_back_together(self):
        batch = self.state.enqueue(key="epoch", notes=[{"id": 12}])
        key = batch["request_key"]
        self.state.remember_receipt(key, {"notes": [{"id": 12, "status": "accepted"}]})
        write = self.state._remember_checkpoint

        def fail_after_write(*args):
            write(*args)
            raise OSError("Checkpoint interrupted")

        with patch.object(self.state, "_remember_checkpoint", side_effect=fail_after_write):
            with self.assertRaises(OSError):
                self.state.complete_entity(key, "notes", 12, [("note", 12, "digest", {})])
        self.assertFalse(self.state.entity_applied(key, "notes", 12))
        self.assertIsNone(self.state.checkpoint("note", 12))

    def test_review_return_payload_is_retired_only_after_acknowledgement_and_keeps_its_identity(self):
        row = {"id": 123, "cid": 45}
        self.state.prepare_review("review-uuid", {"rating": 3}, row)
        self.state.finish_review("review-uuid")
        self.state.remember_delivery(0, {"cursor": 1, "changes": [{"cursor": 1}]})
        self.state.finish_delivery_change(1, {"row": row, "sync_id": "review-uuid"})
        delivery = self.state.prepare_delivery_return("epoch")
        key = delivery["return_key"]
        self.state.remember_receipt(key, {"history": [{"id": 123, "sync_id": "review-uuid", "status": "accepted"}]})
        self.assertEqual(0, self.state.prune())
        self.state.complete_delivery(1)
        self.assertEqual(1, self.state.prune())
        self.assertEqual(1, self.state.get("delivery_cursor"))
        self.assertEqual("review-uuid", self.state.review_sync_id(123))
        self.assertTrue(self.state.review("review-uuid")["applied"])

    def test_compaction_reclaims_disk_space_without_changing_private_file_permissions(self):
        self.completed_batch("x" * (2 * 1024 * 1024))
        before = self.path.stat().st_size
        self.assertEqual(1, self.state.prune())
        self.assertLess(self.path.stat().st_size, before / 2)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual("ok", self.state.db.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual("digest", self.state.checkpoint_digest("note", 12))

    def test_compaction_failure_keeps_the_committed_checkpoints_and_can_be_retried(self):
        self.completed_batch("x" * (2 * 1024 * 1024))
        with patch.object(self.state, "compact", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.state.prune()
        self.assertEqual("digest", self.state.checkpoint_digest("note", 12))
        self.assertEqual(0, self.state.prune())
        self.assertEqual("ok", self.state.db.execute("PRAGMA integrity_check").fetchone()[0])
