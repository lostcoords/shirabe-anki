from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from shirabe_addon.sync_delivery import SyncDelivery, DeliveryConflict
from shirabe_addon.sync_reviews import card_row, history_row, ReviewConflict
from shirabe_addon.sync_state import SyncState

try:
    from anki.collection import Collection
    from anki.buildinfo import version
except ImportError:
    Collection = None
    version = None


class Server:
    api = "http://127.0.0.1:3000/api/v1"

    def __init__(self):
        self.page = {"cursor": 0, "changes": []}
        self.requests = []
        self.receipts = {}
        self.acknowledged = 0
        self.lose_batch_response = False
        self.lose_ack_response = False
        self.reject_history = False

    def deliver(self, profile_id, **payload):
        self.requests.append(("deliver", profile_id, payload))
        return copy.deepcopy(self.page)

    def batch(self, profile_id, payload):
        self.requests.append(("batch", profile_id, copy.deepcopy(payload)))
        receipt = {"history": [{"id": item["row"]["id"], "sync_id": item.get("sync_id"),
                                "status": "conflict" if self.reject_history else "accepted"} for item in payload.get("history", [])],
                   "cards": [{"id": item["row"]["id"], "status": "accepted", "source_deleted": True,
                              "snapshot": "deleted-card", "card": {"anki_id": item["row"]["id"], "deleted": True}}
                             for item in payload.get("cards", [])]}
        for entry, result in zip(payload.get("cards", []), receipt["cards"]):
            if entry.get("review_undo_cursor"):
                result.pop("source_deleted")
                result.update(review_undo_cursor=entry["review_undo_cursor"], removed_history=entry["removed_history"],
                    anki={"card_after": entry["row"], "history_removals": [{"id": row["id"], "removal_id": 41} for row in entry["removed_history"]]})
        self.receipts.setdefault(payload["request_key"], receipt)
        if self.lose_batch_response:
            self.lose_batch_response = False
            raise ConnectionError("Lost batch response")
        return copy.deepcopy(self.receipts[payload["request_key"]])

    def acknowledge(self, profile_id, **payload):
        self.requests.append(("acknowledge", profile_id, payload))
        self.acknowledged = payload["cursor"]
        if self.lose_ack_response:
            self.lose_ack_response = False
            raise ConnectionError("Lost acknowledgement response")


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sync.sqlite3"
        self.collection = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        self.note = self.collection.new_note(self.collection.models.by_name("Basic"))
        self.note.fields = ["猫", "cat"]
        self.collection.add_note(self.note, self.collection.decks.id("Japanese"))
        self.server = Server()
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        remote = {"anki_id": self.note.id, "guid": self.note.guid, "anki_type_id": self.note.mid,
                  "field_names": ["Front", "Back"], "fields": ["猫", "feline"], "tags": []}
        self.change = {"cursor": 1, "entity_kind": "note", "entity_id": 10, "operation": "upsert",
                       "payload": {"base": {"fields": ["猫", "cat"], "tags": []}, "note": remote, "snapshot": "note-digest"}}
        self.server.page = {"cursor": 1, "changes": [self.change]}

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def restart(self):
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")

    def test_delivery_records_started_application_before_any_collection_write(self):
        self.delivery.receive()
        with SyncState(self.path) as state:
            self.assertEqual("received", state.get("delivery")["phase"])

        def interrupted(collection, payload):
            with SyncState(self.path) as state:
                self.assertEqual("applying", state.get("delivery")["phase"])
            raise RuntimeError("Interrupted before the collection write")

        with patch("shirabe_addon.sync_delivery.apply_note_change", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "Interrupted"):
                self.delivery.apply(self.collection)
        self.assertEqual(["猫", "cat"], self.collection.get_note(self.note.id).fields)
        self.restart()
        self.delivery.receive()
        with SyncState(self.path) as state:
            self.assertEqual("applying", state.get("delivery")["phase"])
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(["猫", "feline"], self.collection.get_note(self.note.id).fields)

    def test_a_late_removed_review_receipt_keeps_the_removal_checkpoint_for_restoration(self):
        self.add_review()
        self.delivery.receive()
        self.delivery.apply(self.collection)
        with SyncState(self.path) as state:
            row = state.review("review-uuid")["row"]
            state.remember_checkpoint("source_history", row["id"], "deleted", row)
            state.remember_checkpoint("history_removal", row["id"], "41", row)
        send = self.server.batch

        def archived(profile_id, payload):
            receipt = send(profile_id, payload)
            for item in receipt["history"]:
                item["removed"] = True
            return receipt

        with patch.object(self.server, "batch", archived):
            self.delivery.acknowledge()
        with SyncState(self.path) as state:
            self.assertEqual("deleted", state.checkpoint_digest("source_history", row["id"]))
            self.assertEqual("41", state.checkpoint_digest("history_removal", row["id"]))
        self.assertEqual(row, history_row(self.collection, row["id"]))

    def test_disabling_fsrs_keeps_a_received_note_delivery_pending_without_applying_it(self):
        from shirabe_addon.sync_reviews import ReviewConflict

        self.delivery.receive()
        with SyncState(self.path) as state:
            pending = state.get("delivery")
        self.collection.set_config("fsrs", False)
        with self.assertRaisesRegex(ReviewConflict, "Enable FSRS"):
            self.delivery.apply(self.collection)
        self.assertEqual(["猫", "cat"], self.collection.get_note(self.note.id).fields)
        with SyncState(self.path) as state:
            self.assertEqual(pending, state.get("delivery"))
            self.assertIsNone(state.delivery_result(1))
        self.collection.set_config("fsrs", True)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(["猫", "feline"], self.collection.get_note(self.note.id).fields)
        self.assertEqual(1, self.server.acknowledged)

    def add_review(self):
        card = self.note.cards()[0]
        before = card_row(self.collection, card.id)
        after = {**before, "reps": before["reps"] + 1}
        payload = {"sync_id": "review-uuid", "anki_card_id": card.id, "rating": 3,
                   "snapshot": "review-card-digest", "card": {"anki_id": card.id},
                   "reviewed_at": datetime.now(timezone.utc).isoformat(),
                   "anki": {"card_before": before, "card_after": after, "note_guid": self.note.guid,
                            "anki_type_id": self.note.mid, "revlog": {"cid": card.id, "usn": -1, "ease": 3,
                            "ivl": 0, "lastIvl": 0, "factor": 200, "time": 100, "type": 0}}}
        self.server.page["changes"].append({"cursor": 2, "entity_kind": "review", "entity_id": 20,
                                             "operation": "upsert", "payload": payload})
        self.server.page["cursor"] = 2

    def test_received_and_applied_changes_survive_restart_without_reapplying_later_edits(self):
        self.delivery.receive()
        self.restart()
        self.delivery.receive()
        self.assertEqual(1, len(self.server.requests))
        self.delivery.apply(self.collection)
        self.assertEqual(["猫", "feline"], self.collection.get_note(self.note.id).fields)
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "cat"
        self.collection.update_note(note)
        self.restart()
        self.delivery.apply(self.collection)
        self.assertEqual(["猫", "cat"], self.collection.get_note(self.note.id).fields)
        self.delivery.acknowledge()
        self.assertEqual(1, self.server.acknowledged)
        with SyncState(self.path) as state:
            self.assertEqual(1, state.get("delivery_cursor"))
            self.assertIsNone(state.get("delivery"))
            self.assertEqual("note-digest", state.checkpoint("note", self.note.id)["digest"])

    def test_no_network_runs_during_collection_application(self):
        self.add_review()
        with ThreadPoolExecutor(max_workers=1) as network:
            network.submit(self.delivery.receive).result(timeout=5)
        with patch.object(self.server, "batch", side_effect=AssertionError("network on collection thread")), \
             patch.object(self.server, "acknowledge", side_effect=AssertionError("network on collection thread")):
            self.delivery.apply(self.collection)
        self.assertEqual(0, self.server.acknowledged)
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))
        with ThreadPoolExecutor(max_workers=1) as network:
            network.submit(self.delivery.acknowledge).result(timeout=5)
        self.assertEqual(2, self.server.acknowledged)
        self.assertEqual(["deliver", "batch", "acknowledge"], [item[0] for item in self.server.requests])
        with SyncState(self.path) as state:
            card_id = self.note.cards()[0].id
            source_id = self.collection.db.scalar("select id from revlog where cid = ?", card_id)
            self.assertEqual({source_id: history_row(self.collection, source_id)}, state.card_history(card_id))

    def test_review_undo_withdrawal_preserves_history_and_retries_acknowledgement(self):
        self.add_review()
        review = copy.deepcopy(self.server.page["changes"][-1])
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"phase": "uploaded", "api": self.server.api,
                "profile": {"id": 123}, "pair_request": {"key": "epoch"},
                "collection_path": str(Path(self.collection.path).resolve()), "deck_ids": [self.note.cards()[0].did],
                "root_deck_ids": [self.note.cards()[0].did]})
        change = {"cursor": 3, "entity_kind": "review", "entity_id": 20, "operation": "delete",
            "payload": {"sync_id": review["payload"]["sync_id"], "anki_card_id": review["payload"]["anki_card_id"],
                        "delivery_cursor": 2, "review": review["payload"]}}
        self.server.page = {"cursor": 3, "changes": [change]}
        self.delivery.receive()
        before = card_row(self.collection, review["payload"]["anki_card_id"])
        history = self.collection.db.all("SELECT * FROM revlog")
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertEqual("review_undos", failure.exception.conflicts[0]["kind"])
        prepared = self.delivery.prepare_review_undo_resolution(self.collection, failure.exception.conflicts[0])
        changed = copy.deepcopy(prepared)
        changed["change"]["payload"]["sync_id"] = "another-review"
        with self.assertRaisesRegex(ReviewConflict, "changed while"):
            self.delivery.withdraw_review_undo(self.collection, changed)
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            state.put("initial_snapshot", {**snapshot, "collection_path": "another-collection.anki2"})
        with self.assertRaisesRegex(DeliveryConflict, "collection, server or sync epoch"):
            self.delivery.withdraw_review_undo(self.collection, prepared)
        with SyncState(self.path) as state:
            state.put("initial_snapshot", snapshot)
        self.delivery.withdraw_review_undo(self.collection, prepared)
        self.restart()
        self.delivery.apply(self.collection)
        self.server.lose_ack_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.restart()
        self.delivery.acknowledge()
        self.assertEqual({"key": "epoch", "cursor": 3, "kept_reviews": [3]}, self.server.requests[-1][2])
        self.assertEqual(before, card_row(self.collection, before["id"]))
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        with self.assertRaises(DeliveryConflict):
            self.delivery.withdraw_review_undo(self.collection, prepared)

    def prepare_undo_request(self, *, burial=False, leech=False):
        if burial:
            model = self.collection.models.get(self.note.mid)
            template = self.collection.models.new_template("Reverse")
            template.update(qfmt="{{Back}}", afmt="{{Front}}")
            self.collection.models.add_template(model, template)
            self.collection.models.save(model)
        self.add_review()
        review = copy.deepcopy(self.server.page["changes"][-1])
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"phase": "uploaded", "api": self.server.api,
                "profile": {"id": 123}, "pair_request": {"key": "epoch"},
                "collection_path": str(Path(self.collection.path).resolve()), "deck_ids": [self.note.cards()[0].did],
                "root_deck_ids": [self.note.cards()[0].did]})
        change = {"cursor": 3, "entity_kind": "review", "entity_id": 20, "operation": "delete",
            "payload": {"sync_id": review["payload"]["sync_id"], "anki_card_id": review["payload"]["anki_card_id"],
                        "delivery_cursor": 2, "review": review["payload"], "snapshot": "undo-base", "card": review["payload"]["card"]}}
        if leech:
            note = self.collection.get_note(self.note.id)
            before = list(note.tags)
            note.tags.append("leech")
            self.collection.update_note(note)
            change["payload"]["leech_tag"] = {"before": before, "after": list(note.tags)}
        if burial:
            sibling = self.note.cards()[1]
            before = card_row(self.collection, sibling.id)
            sibling.queue = -2
            self.collection.update_card(sibling)
            change["payload"]["sibling_burials"] = [{"snapshot": "buried-sibling",
                "anki": {"card_before": card_row(self.collection, sibling.id), "card_after": before,
                         "note_guid": self.note.guid, "anki_type_id": self.note.mid}}]
        self.server.page = {"cursor": 3, "changes": [change]}
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        return self.delivery.prepare_review_undo_resolution(self.collection, failure.exception.conflicts[0])

    def test_confirmed_undo_restores_leech_tags_and_preserves_note_fields(self):
        prepared = self.prepare_undo_request(leech=True)
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "edited after the review"
        self.collection.update_note(note)
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        note = self.collection.get_note(self.note.id)
        self.assertEqual(prepared["change"]["payload"]["leech_tag"]["before"], note.tags)
        self.assertEqual("edited after the review", note.fields[1])
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        self.delivery.acknowledge()

    def test_leech_undo_accepts_anki_canonical_tag_capitalization(self):
        prepared = self.prepare_undo_request(leech=True)
        note = self.collection.get_note(self.note.id)
        self.collection.tags.rename("leech", "Leech")
        self.assertIn("Leech", self.collection.get_note(note.id).tags)
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        self.assertEqual(prepared["change"]["payload"]["leech_tag"]["before"], self.collection.get_note(note.id).tags)
        self.delivery.acknowledge()

    def test_confirmed_leech_undo_refuses_later_tag_edits_without_removing_history(self):
        prepared = self.prepare_undo_request(leech=True)
        self.delivery.confirm_review_undo(self.collection, prepared)
        note = self.collection.get_note(self.note.id)
        note.tags.append("later")
        self.collection.update_note(note)
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(sorted(note.tags), sorted(self.collection.get_note(note.id).tags))

    def test_leech_undo_output_failure_rolls_back_note_card_and_history(self):
        prepared = self.prepare_undo_request(leech=True)
        self.delivery.confirm_review_undo(self.collection, prepared)
        with patch.object(SyncState, "remember_review_undo_output", side_effect=OSError("output unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertEqual(prepared["change"]["payload"]["leech_tag"]["after"], self.collection.get_note(self.note.id).tags)
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(1, self.note.cards()[0].reps)

    def test_committed_leech_undo_retry_preserves_later_native_tags(self):
        prepared = self.prepare_undo_request(leech=True)
        self.delivery.confirm_review_undo(self.collection, prepared)
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("receipt unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        note = self.collection.get_note(self.note.id)
        self.assertEqual(prepared["change"]["payload"]["leech_tag"]["before"], note.tags)
        note.tags.append("later")
        self.collection.update_note(note)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(sorted(note.tags), sorted(self.collection.get_note(note.id).tags))
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))

    def test_reconciled_undo_refuses_missing_predecessor_proof(self):
        prepared = self.prepare_undo_request()
        change = copy.deepcopy(prepared["change"])
        original = change["payload"]["review"]["anki"]
        change["payload"]["anki"] = {**original, "card_before": original["card_after"], "card_after": original["card_before"]}
        with SyncState(self.path) as state:
            with self.assertRaisesRegex(ReviewConflict, "preceding review"):
                self.delivery._review_undo_state(self.collection, state, change)
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_reconciled_undo_cannot_restore_a_different_card_identity(self):
        prepared = self.prepare_undo_request()
        change = copy.deepcopy(prepared["change"])
        original = change["payload"]["review"]["anki"]
        change["payload"]["anki"] = {**original, "card_before": original["card_after"],
            "card_after": {**original["card_before"], "id": original["card_before"]["id"] + 1}}
        with SyncState(self.path) as state:
            with self.assertRaisesRegex(ReviewConflict, "identity changed"):
                self.delivery._review_undo_state(self.collection, state, change)
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_confirmed_undo_restores_automatic_sibling_burial(self):
        prepared = self.prepare_undo_request(burial=True)
        sibling_id = self.note.cards()[1].id
        self.assertEqual(-2, self.collection.get_card(sibling_id).queue)
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        self.assertEqual(0, self.collection.get_card(sibling_id).queue)
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        self.delivery.acknowledge()

    def test_confirmed_undo_rechecks_siblings_before_any_history_is_removed(self):
        prepared = self.prepare_undo_request(burial=True)
        self.delivery.confirm_review_undo(self.collection, prepared)
        sibling = self.note.cards()[1]
        sibling.queue = -1
        self.collection.update_card(sibling)
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(-1, self.collection.get_card(sibling.id).queue)

    def test_sibling_undo_output_failure_rolls_back_cards_history_and_counters(self):
        prepared = self.prepare_undo_request(burial=True)
        sibling_id = self.note.cards()[1].id
        self.delivery.confirm_review_undo(self.collection, prepared)
        with patch.object(SyncState, "remember_review_undo_output", side_effect=OSError("output unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(-2, self.collection.get_card(sibling_id).queue)
        self.assertEqual(1, self.note.cards()[0].reps)
        self.assertEqual(1, self.collection.decks.get(self.note.cards()[0].did)["newToday"][1])

    def test_completed_sibling_undo_retry_preserves_a_later_native_suspension(self):
        prepared = self.prepare_undo_request(burial=True)
        self.delivery.confirm_review_undo(self.collection, prepared)
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("receipt unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        sibling = self.note.cards()[1]
        self.assertEqual(0, sibling.queue)
        sibling.queue = -1
        self.collection.update_card(sibling)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(-1, self.collection.get_card(sibling.id).queue)
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(0, self.collection.decks.get(sibling.did)["newToday"][1])

    def test_confirmed_review_undo_removes_once_and_returns_its_archive_before_ack(self):
        prepared = self.prepare_undo_request()
        deck_id = self.note.cards()[0].did
        self.assertEqual(1, self.collection.decks.get(deck_id)["newToday"][1])
        self.assertNotIn("undo_error", prepared)
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(0, self.note.cards()[0].reps)
        self.assertEqual(0, self.collection.decks.get(deck_id)["newToday"][1])
        self.server.lose_batch_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.collection.decks.get(deck_id)["newToday"][1])
        sent = [payload for method, _, payload in self.server.requests if method == "batch" and payload.get("cards")]
        self.assertEqual(2, len(sent))
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(3, sent[0]["cards"][0]["review_undo_cursor"])
        with SyncState(self.path) as state:
            self.assertEqual("41", state.checkpoint_digest("history_removal", prepared["row"]["id"]))
            self.assertEqual("deleted", state.checkpoint_digest("source_history", prepared["row"]["id"]))
            self.assertFalse(state.pending_review_undos())

    def test_review_undo_checks_publication_and_changes_again_after_confirmation(self):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.collection.db.execute("UPDATE revlog SET usn = 1 WHERE id = ?", prepared["row"]["id"])
        before = card_row(self.collection, prepared["local"]["id"])
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertIn("native sync", failure.exception.conflicts[0]["code"])
        self.assertEqual(before, card_row(self.collection, before["id"]))
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        current = self.delivery.prepare_review_undo_resolution(self.collection, prepared["problem"])
        self.assertIn("native sync", current["undo_error"])

    def test_review_undo_resumes_after_native_commit_without_scanning_absence_as_another_removal(self):
        from shirabe_addon.sync_changes import SyncChanges
        from shirabe_addon.sync_snapshot import SnapshotConflict
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        with patch.object(SyncState, "finish_delivery_change", side_effect=RuntimeError("sidecar write failed")):
            with self.assertRaisesRegex(RuntimeError, "sidecar write failed"):
                self.delivery.apply(self.collection)
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        with self.assertRaisesRegex(SnapshotConflict, "pending review Undo"):
            SyncChanges(self.path, self.server).capture(self.collection)
        with self.assertRaisesRegex(ReviewConflict, "already removed"):
            self.delivery.withdraw_review_undo(self.collection, prepared)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.note.cards()[0].reps)

    def test_interrupted_review_undo_with_a_live_row_requires_new_confirmation(self):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.collection.db.execute("CREATE TEMP TRIGGER refuse_undo BEFORE DELETE ON revlog BEGIN SELECT RAISE(ABORT, 'native write failed'); END")
        with self.assertRaises(Exception):
            self.delivery.apply(self.collection)
        self.collection.db.execute("DROP TRIGGER refuse_undo")
        self.restart()
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertIn("live review", failure.exception.conflicts[0]["code"])
        refreshed = self.delivery.prepare_review_undo_resolution(self.collection, prepared["problem"])
        self.assertTrue(refreshed["prior_removal"])
        self.delivery.confirm_review_undo(self.collection, refreshed)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.note.cards()[0].reps)

    def test_interrupted_undo_returns_its_saved_result_without_overwriting_a_later_native_review(self):
        from shirabe_addon.sync_changes import SyncChanges
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        with patch.object(SyncState, "finish_delivery_change", side_effect=RuntimeError("sidecar write failed")):
            with self.assertRaisesRegex(RuntimeError, "sidecar write failed"):
                self.delivery.apply(self.collection)
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        latest = card_row(self.collection, card.id)
        history = self.collection.db.all("SELECT * FROM revlog")
        self.assertEqual(1, len(history))
        with SyncState(self.path) as state:
            saved_output = state.review_undo_output(prepared["change"]["cursor"])
        self.restart()
        self.delivery.apply(self.collection)
        self.assertEqual(latest, card_row(self.collection, card.id))
        with SyncState(self.path) as state:
            entries = [entry for key in state.review_undo_return_keys() for entry in state.batch(key)["payload"]["cards"]]
        self.assertEqual(1, len(entries))
        self.assertEqual(saved_output["cards"], entries)
        self.assertEqual(0, entries[0]["row"]["reps"])
        self.assertNotIn("last_review_id", entries[0])
        self.delivery.acknowledge()
        self.assertEqual(latest, card_row(self.collection, card.id))
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        cycle = SyncChanges(self.path, self.server).capture(self.collection)
        with SyncState(self.path) as state:
            rows = [entry["row"] for key in cycle["keys"] for entry in state.batch(key)["payload"].get("history", [])]
            self.assertEqual([history[0][0]], [row["id"] for row in rows])
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))

    def test_failed_undo_output_persistence_rolls_back_native_history_and_schedule(self):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        before = card_row(self.collection, prepared["local"]["id"])
        history = self.collection.db.all("SELECT * FROM revlog")
        counts = self.collection.decks.get(before["did"])["newToday"]
        with patch.object(SyncState, "remember_review_undo_output", side_effect=OSError("output unavailable")):
            with self.assertRaisesRegex(OSError, "output unavailable"):
                self.delivery.apply(self.collection)
        self.assertEqual(before, card_row(self.collection, prepared["local"]["id"]))
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        self.assertEqual(counts, self.collection.decks.get(before["did"])["newToday"])
        with SyncState(self.path) as state:
            self.assertIsNone(state.review_undo_output(prepared["change"]["cursor"]))
            self.assertIsNone(state.delivery_result(prepared["change"]["cursor"]))

    def test_saved_undo_output_does_not_prove_a_native_commit_that_rolled_back(self):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        record = SyncState.remember_review_undo_output

        def interrupted(state, *args):
            record(state, *args)
            raise RuntimeError("native commit interrupted")

        with patch.object(SyncState, "remember_review_undo_output", interrupted):
            with self.assertRaisesRegex(RuntimeError, "native commit interrupted"):
                self.delivery.apply(self.collection)
        self.restart()
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertIn("live review", failure.exception.conflicts[0]["code"])
        self.assertEqual(prepared["row"], history_row(self.collection, prepared["row"]["id"]))
        with SyncState(self.path) as state:
            stale = state.review_undo_output(prepared["change"]["cursor"])
            self.assertIsNotNone(stale)
            self.assertIsNone(state.delivery_result(prepared["change"]["cursor"]))
        card = self.note.cards()[0]
        card.flags = 3
        self.collection.update_card(card)
        fresh = self.delivery.prepare_review_undo_resolution(self.collection, prepared["problem"])
        self.delivery.confirm_review_undo(self.collection, fresh)
        self.delivery.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual(3, state.review_undo_output(prepared["change"]["cursor"])["cards"][0]["row"]["flags"])
        self.delivery.acknowledge()
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        with SyncState(self.path) as state:
            self.assertIsNone(state.review_undo_output(prepared["change"]["cursor"]))

    def test_review_undo_rechecks_deck_selection_after_confirmation(self):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.collection.decks.id("Japanese::New child")
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertIn("deck selection", failure.exception.conflicts[0]["code"])
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(1, self.note.cards()[0].reps)
        current = self.delivery.prepare_review_undo_resolution(self.collection, prepared["problem"])
        self.assertIn("deck selection", current["undo_error"])
        self.delivery.withdraw_review_undo(self.collection, current)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()

    def test_review_undo_rechecks_the_exact_card_after_confirmation(self):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        card = self.note.cards()[0]
        card.flags = 2
        self.collection.update_card(card)
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertIn("changed after confirmation", failure.exception.conflicts[0]["code"])
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        updated = self.delivery.prepare_review_undo_resolution(self.collection, prepared["problem"])
        self.delivery.confirm_review_undo(self.collection, updated)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(2, self.note.cards()[0].flags)
        self.assertEqual(0, self.note.cards()[0].reps)

    def test_restoration_after_interrupted_native_commit_needs_new_undo_consent(self):
        from shirabe_addon.sync_cards import apply_card_change
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        with patch.object(SyncState, "finish_delivery_change", side_effect=RuntimeError("lost sidecar")):
            with self.assertRaises(RuntimeError):
                self.delivery.apply(self.collection)
        source = prepared["change"]["payload"]["review"]["anki"]
        apply_card_change(self.collection, {"anki": source}, restored_history=[{"row": prepared["row"], "removal_id": 41}])
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertIn("live review", failure.exception.conflicts[0]["code"])
        self.assertIsNotNone(history_row(self.collection, prepared["row"]["id"]))
        self.delivery.withdraw_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, self.note.cards()[0].reps)

    def test_returned_review_undo_cannot_acknowledge_a_different_request(self):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        send = self.server.batch

        def mismatch(profile_id, payload):
            result = send(profile_id, payload)
            result["cards"][0]["review_undo_cursor"] += 1
            return result

        with patch.object(self.server, "batch", side_effect=mismatch):
            with self.assertRaisesRegex(DeliveryConflict, "not accepted the requested Undo"):
                self.delivery.acknowledge()
        self.assertEqual(2, self.server.acknowledged)
        with SyncState(self.path) as state:
            self.assertIsNotNone(state.get("delivery"))
            self.assertIsNone(state.checkpoint_digest("history_removal", prepared["row"]["id"]))

    def accepted_undo_result(self, **remote_changes):
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        send = self.server.batch

        def merged(profile_id, payload):
            result = send(profile_id, payload)
            result["cards"][0]["anki"]["card_after"].update(flags=3, **remote_changes)
            return result

        with patch.object(self.server, "batch", side_effect=merged):
            with self.assertRaises(DeliveryConflict) as failure:
                self.delivery.acknowledge()
        self.assertEqual(1, len(failure.exception.conflicts))
        return prepared, failure.exception.conflicts[0]

    def test_accepted_undo_result_can_reconcile_flags_without_removing_history_again(self):
        prepared, problem = self.accepted_undo_result()
        self.restart()
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        with patch("shirabe_addon.sync_cards.remove_unsynced_history") as remove_history:
            self.delivery.resolve_review_undo_result(self.collection, choice,
                {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
            self.assertEqual((), remove_history.call_args.args[2])
        self.delivery.acknowledge()
        self.assertEqual(3, self.note.cards()[0].flags)
        self.assertEqual(0, self.note.cards()[0].reps)
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(3, self.server.acknowledged)
        with SyncState(self.path) as state:
            self.assertFalse(state.pending_review_undos())
            self.assertEqual("41", state.checkpoint_digest("history_removal", prepared["row"]["id"]))

    def prepare_undo_schedule_conflict(self, schedule):
        from shirabe_addon.sync_changes import SyncChanges
        prepared = self.prepare_undo_request()
        self.delivery.confirm_review_undo(self.collection, prepared)
        self.delivery.apply(self.collection)
        send = self.server.batch

        def conflicted(profile_id, payload):
            if payload["cards"][0].get("conflict_id"):
                return send(profile_id, payload)
            return {"cards": [{"id": self.note.cards()[0].id, "status": "conflict", "conflict_id": 7,
                               "code": "history_changed_before_undo"}]}

        with patch.object(self.server, "batch", side_effect=conflicted):
            with self.assertRaises(DeliveryConflict) as failure:
                self.delivery.acknowledge()
        self.assertEqual(1, len(failure.exception.conflicts))
        problem = failure.exception.conflicts[0]
        self.assertEqual("cards", problem["kind"])
        source = prepared["change"]["payload"]["review"]["anki"]
        remote = {**source["card_before"], "reps": 2}
        detail = {"id": 7, "key": "epoch", "resolved": False, "snapshot": "concurrent-review",
                  "card": {"anki_id": remote["id"]}, "anki": {**source, "card_after": remote},
                  "history_removal": {"rows": [prepared["row"]]}}
        changes = SyncChanges(self.path, self.server)
        choice = changes.prepare_history_removal(self.collection, problem, detail)
        replacement = changes.resolve_history_removal(self.collection, choice, schedule=schedule)
        return prepared, problem, replacement

    def reconcile_undo_schedule_conflict(self, schedule):
        prepared, problem, replacement = self.prepare_undo_schedule_conflict(schedule)
        self.restart()
        if schedule == "shirabe":
            with self.assertRaises(DeliveryConflict) as accepted:
                self.delivery.acknowledge()
            result = self.delivery.prepare_review_undo_result_resolution(self.collection, accepted.exception.conflicts[0])
            self.assertTrue(result["schedule_confirmed"])
            with patch("shirabe_addon.sync_cards.remove_unsynced_history") as remove_history:
                self.delivery.resolve_review_undo_result(self.collection, result,
                    {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
                self.assertEqual((), remove_history.call_args.args[2])
        self.delivery.acknowledge()
        self.assertEqual(2 if schedule == "shirabe" else 0, self.note.cards()[0].reps)
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(3, self.server.acknowledged)
        with SyncState(self.path) as state:
            self.assertTrue(state.batch_applied(problem["request_key"]))
            self.assertTrue(state.batch_applied(replacement))
            self.assertEqual("41", state.checkpoint_digest("history_removal", prepared["row"]["id"]))

    def test_remote_undo_schedule_conflict_keeps_the_anki_schedule(self):
        self.reconcile_undo_schedule_conflict("anki")

    def test_remote_undo_schedule_conflict_applies_the_confirmed_shirabe_schedule(self):
        self.reconcile_undo_schedule_conflict("shirabe")

    def test_returned_undo_resolution_preserves_request_identity_and_removed_history(self):
        _, problem, replacement = self.prepare_undo_schedule_conflict("shirabe")
        with SyncState(self.path) as state:
            entry = state.batch(replacement)["payload"]["cards"][0]
            base = state.resolution_base(replacement)
            for changes in ({"review_undo_cursor": 999}, {"removed_history": []}, {"deleted": True}, {"restored_history": [{"id": 1}]}):
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "preserve its request"):
                    state.stage_resolution(problem["request_key"], "cards", problem["id"], {**entry, **changes}, base, returned=True)
            self.assertEqual(replacement, state.replacement_for(problem["request_key"], "cards", problem["id"]))

    def test_accepted_undo_schedule_must_match_the_confirmed_resolution(self):
        self.prepare_undo_schedule_conflict("shirabe")
        send = self.server.batch

        def changed(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["cards"][0]["anki"]["card_after"]["reps"] = 9
            return receipt

        with patch.object(self.server, "batch", side_effect=changed), self.assertRaises(DeliveryConflict) as failure:
            self.delivery.acknowledge()
        prepared = self.delivery.prepare_review_undo_result_resolution(self.collection, failure.exception.conflicts[0])
        self.assertFalse(prepared["schedule_confirmed"])
        self.assertFalse(prepared["schedule_matches"])
        with self.assertRaisesRegex(ReviewConflict, "different card schedules"):
            self.delivery.resolve_review_undo_result(self.collection, prepared,
                {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
        self.assertEqual(0, self.note.cards()[0].reps)
        self.assertEqual(2, self.server.acknowledged)

    def test_confirmed_undo_schedule_recovers_after_native_write_before_checkpoint(self):
        original, _, _ = self.prepare_undo_schedule_conflict("shirabe")
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.acknowledge()
        problem = failure.exception.conflicts[0]
        prepared = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        choices = {"deck": "anki", "lifecycle": "anki", "properties": "anki"}
        with patch.object(SyncState, "complete_entity", side_effect=RuntimeError("failed checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "failed checkpoint"):
                self.delivery.resolve_review_undo_result(self.collection, prepared, choices)
        self.assertEqual(2, self.note.cards()[0].reps)
        self.restart()
        prepared = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        self.delivery.resolve_review_undo_result(self.collection, prepared, choices)
        self.delivery.acknowledge()
        self.assertEqual(2, self.note.cards()[0].reps)
        self.assertIsNone(history_row(self.collection, original["row"]["id"]))
        self.assertEqual(3, self.server.acknowledged)

    def test_new_native_history_blocks_an_accepted_undo_schedule_without_losing_the_review(self):
        self.prepare_undo_schedule_conflict("shirabe")
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.acknowledge()
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = self.collection.db.all("SELECT * FROM revlog")
        with self.assertRaisesRegex(ReviewConflict, "changed review history"):
            self.delivery.prepare_review_undo_result_resolution(self.collection, failure.exception.conflicts[0])
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        self.assertEqual(1, len(history))
        self.assertEqual(2, self.server.acknowledged)

    def test_remote_undo_conflict_descendants_settle_only_after_the_latest_choice(self):
        from shirabe_addon.sync_changes import SyncChanges
        original, root, first = self.prepare_undo_schedule_conflict("anki")
        rejected = {"cards": [{"id": root["id"], "status": "conflict", "conflict_id": 8,
                                "code": "history_changed_before_undo"}]}
        with patch.object(self.server, "batch", return_value=rejected), self.assertRaises(DeliveryConflict) as failure:
            self.delivery.acknowledge()
        problem = failure.exception.conflicts[0]
        self.assertEqual(first, problem["request_key"])
        source = original["change"]["payload"]["review"]["anki"]
        detail = {"id": 8, "key": "epoch", "resolved": False, "snapshot": "later-review",
                  "card": {"anki_id": root["id"]}, "anki": {**source, "card_after": {**source["card_before"], "reps": 3}},
                  "history_removal": {"rows": [original["row"]]}}
        changes = SyncChanges(self.path, self.server)
        choice = changes.prepare_history_removal(self.collection, problem, detail)
        second = changes.resolve_history_removal(self.collection, choice, schedule="anki")
        with SyncState(self.path) as state:
            self.assertFalse(state.batch_applied(root["request_key"]))
            self.assertFalse(state.batch_applied(first))
        self.restart()
        self.delivery.acknowledge()
        with SyncState(self.path) as state:
            self.assertTrue(all(state.batch_applied(key) for key in (root["request_key"], first, second)))
            self.assertEqual(8, state.batch(second)["payload"]["cards"][0]["conflict_id"])
            self.assertEqual(7, state.batch(first)["payload"]["cards"][0]["conflict_id"])
        self.assertEqual(0, self.note.cards()[0].reps)
        self.assertIsNone(history_row(self.collection, original["row"]["id"]))

    def test_undo_result_choice_rechecks_local_state_and_kept_state_remains_uploadable(self):
        from shirabe_addon.sync_changes import SyncChanges
        _, problem = self.accepted_undo_result()
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        card = self.note.cards()[0]
        card.flags = 4
        self.collection.update_card(card)
        selected = {"deck": "anki", "lifecycle": "anki", "properties": "anki"}
        with self.assertRaisesRegex(ReviewConflict, "chooser was open"):
            self.delivery.resolve_review_undo_result(self.collection, choice, selected)
        current = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        self.delivery.resolve_review_undo_result(self.collection, current, selected)
        self.delivery.acknowledge()
        self.assertEqual(4, self.note.cards()[0].flags)
        cycle = SyncChanges(self.path, self.server).capture(self.collection)
        with SyncState(self.path) as state:
            entries = [entry for key in cycle["keys"] for entry in state.batch(key)["payload"].get("cards", [])]
            self.assertEqual(4, entries[0]["row"]["flags"])
            self.assertEqual("deleted-card", entries[0]["base"])

    def test_undo_result_ack_resumes_after_entity_completion_without_reopening_the_choice(self):
        _, problem = self.accepted_undo_result()
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        complete = SyncState.complete_entity

        def interrupted(state, *args, **kwargs):
            complete(state, *args, **kwargs)
            raise RuntimeError("lost completion response")

        with patch.object(SyncState, "complete_entity", interrupted):
            with self.assertRaisesRegex(RuntimeError, "lost completion response"):
                self.delivery.resolve_review_undo_result(self.collection, choice,
                    {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        self.restart()
        self.delivery.acknowledge()
        self.assertEqual(3, self.server.acknowledged)
        self.assertEqual(3, self.note.cards()[0].flags)

    def test_undo_result_choice_can_be_reopened_after_native_write_and_failed_checkpoint(self):
        prepared, problem = self.accepted_undo_result()
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        with patch.object(SyncState, "complete_entity", side_effect=RuntimeError("failed checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "failed checkpoint"):
                self.delivery.resolve_review_undo_result(self.collection, choice,
                    {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        self.assertEqual(3, self.note.cards()[0].flags)
        self.restart()
        current = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        self.delivery.resolve_review_undo_result(self.collection, current,
            {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
        self.delivery.acknowledge()
        self.assertIsNone(history_row(self.collection, prepared["row"]["id"]))

    def test_undo_result_schedule_disagreement_does_not_authorize_another_schedule(self):
        _, problem = self.accepted_undo_result(reps=2)
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        self.assertFalse(choice["schedule_matches"])
        before = card_row(self.collection, problem["id"])
        with self.assertRaisesRegex(ReviewConflict, "different card schedules"):
            self.delivery.resolve_review_undo_result(self.collection, choice,
                {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        self.assertEqual(before, card_row(self.collection, problem["id"]))
        self.assertEqual(2, self.server.acknowledged)

    def reconcile_unconfirmed_undo_schedule(self, schedule, *, interrupted=False):
        original, problem = self.accepted_undo_result(reps=2)
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        with SyncState(self.path) as state:
            receipt = state.batch(problem["request_key"])["receipt"]
        selected = {"deck": "anki", "lifecycle": "anki", "properties": "anki", "schedule": schedule}
        if interrupted:
            with patch.object(SyncState, "complete_entity", side_effect=RuntimeError("failed checkpoint")):
                with self.assertRaisesRegex(RuntimeError, "failed checkpoint"):
                    self.delivery.resolve_review_undo_result(self.collection, choice, selected)
            self.restart()
            choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        with patch("shirabe_addon.sync_cards.remove_unsynced_history") as remove_history:
            self.delivery.resolve_review_undo_result(self.collection, choice, selected)
            self.assertEqual((), remove_history.call_args.args[2])
        self.delivery.acknowledge()
        self.assertEqual(2 if schedule == "shirabe" else 0, self.note.cards()[0].reps)
        self.assertIsNone(history_row(self.collection, original["row"]["id"]))
        self.assertEqual(3, self.server.acknowledged)
        with SyncState(self.path) as state:
            self.assertEqual(receipt, state.batch(problem["request_key"])["receipt"])
            self.assertFalse(state.pending_review_undos())

    def test_accepted_undo_result_can_explicitly_keep_the_native_schedule(self):
        self.reconcile_unconfirmed_undo_schedule("anki")

    def test_accepted_undo_result_can_explicitly_choose_the_returned_schedule(self):
        self.reconcile_unconfirmed_undo_schedule("shirabe")

    def test_explicit_undo_result_schedule_resumes_after_native_write(self):
        self.reconcile_unconfirmed_undo_schedule("shirabe", interrupted=True)

    def test_native_schedule_edit_after_accepted_undo_can_be_explicitly_reconciled(self):
        _, problem = self.accepted_undo_result()
        card = self.note.cards()[0]
        expected = card.due
        card.due += 10
        self.collection.update_card(card)
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        self.assertTrue(choice["schedule_choice_required"])
        self.delivery.resolve_review_undo_result(self.collection, choice,
            {"deck": "anki", "lifecycle": "anki", "properties": "anki", "schedule": "shirabe"})
        self.delivery.acknowledge()
        self.assertEqual(expected, self.note.cards()[0].due)
        self.assertEqual(3, self.server.acknowledged)

    def test_accepted_undo_choice_is_durable_before_native_state_changes(self):
        _, problem = self.accepted_undo_result(reps=2)
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        before = card_row(self.collection, problem["id"])
        with patch.object(SyncState, "remember_review_undo_result_choice", side_effect=OSError("sidecar unavailable")):
            with self.assertRaisesRegex(OSError, "sidecar unavailable"):
                self.delivery.resolve_review_undo_result(self.collection, choice,
                    {"deck": "anki", "lifecycle": "anki", "properties": "anki", "schedule": "shirabe"})
        self.assertEqual(before, card_row(self.collection, problem["id"]))
        self.assertEqual(2, self.server.acknowledged)

    def test_new_history_after_interrupted_explicit_undo_choice_is_preserved(self):
        _, problem = self.accepted_undo_result(reps=2)
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        with patch.object(SyncState, "complete_entity", side_effect=RuntimeError("failed checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "failed checkpoint"):
                self.delivery.resolve_review_undo_result(self.collection, choice,
                    {"deck": "anki", "lifecycle": "anki", "properties": "anki", "schedule": "shirabe"})
        self.restart()
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = self.collection.db.all("SELECT * FROM revlog")
        self.assertEqual(1, len(history))
        with self.assertRaisesRegex(ReviewConflict, "changed review history"):
            self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        self.assertEqual(2, self.server.acknowledged)

    def test_reopening_undo_choice_invalidates_a_previously_prepared_confirmation(self):
        _, problem = self.accepted_undo_result(reps=2)
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        selected = {"deck": "anki", "lifecycle": "anki", "properties": "anki", "schedule": "anki"}
        with patch.object(SyncState, "complete_entity", side_effect=RuntimeError("failed checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "failed checkpoint"):
                self.delivery.resolve_review_undo_result(self.collection, choice, selected)
        before = card_row(self.collection, problem["id"])
        with self.assertRaisesRegex(ReviewConflict, "chooser was open"):
            self.delivery.resolve_review_undo_result(self.collection, choice, {**selected, "schedule": "shirabe"})
        self.assertEqual(before, card_row(self.collection, problem["id"]))

    def test_schedule_edit_after_failed_undo_checkpoint_requires_a_fresh_choice(self):
        _, problem = self.accepted_undo_result(reps=2)
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        selected = {"deck": "anki", "lifecycle": "anki", "properties": "anki", "schedule": "shirabe"}
        with patch.object(SyncState, "complete_entity", side_effect=RuntimeError("failed checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "failed checkpoint"):
                self.delivery.resolve_review_undo_result(self.collection, choice, selected)
        card = self.note.cards()[0]
        card.due += 20
        self.collection.update_card(card)
        fresh = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        self.delivery.resolve_review_undo_result(self.collection, fresh, {**selected, "schedule": "anki"})
        self.delivery.acknowledge()
        self.assertEqual(card.due, self.note.cards()[0].due)
        self.assertEqual(3, self.server.acknowledged)

    def test_restored_history_refuses_an_accepted_undo_result_choice(self):
        from shirabe_addon.sync_cards import apply_card_change
        prepared, problem = self.accepted_undo_result()
        choice = self.delivery.prepare_review_undo_result_resolution(self.collection, problem)
        source = prepared["change"]["payload"]["review"]["anki"]
        apply_card_change(self.collection, {"anki": source}, restored_history=[{"row": prepared["row"], "removal_id": 41}])
        with self.assertRaisesRegex(ReviewConflict, "exists again"):
            self.delivery.resolve_review_undo_result(self.collection, choice,
                {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        self.assertEqual(prepared["row"], history_row(self.collection, prepared["row"]["id"]))
        self.assertEqual(1, self.note.cards()[0].reps)

    def test_an_applied_receipt_supersedes_older_note_deliveries_but_not_newer_edits(self):
        self.note.fields[1] = "latest accepted meaning"
        self.collection.update_note(self.note)
        with SyncState(self.path) as state:
            batch = state.enqueue(key="epoch", notes=[{"id": self.note.id}])
            state.remember_receipt(batch["request_key"], {"cursor": 3, "notes": [{"id": self.note.id, "status": "accepted"}]})
            state.complete_entity(batch["request_key"], "notes", self.note.id,
                                  [("note", self.note.id, "latest-digest", {})], cursor=3)
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.assertEqual("latest accepted meaning", self.collection.get_note(self.note.id).fields[1])
        with SyncState(self.path) as state:
            self.assertEqual("latest-digest", state.checkpoint_digest("note", self.note.id))
            self.assertEqual({}, state.delivery_result(1))
        self.delivery.acknowledge()
        newer = copy.deepcopy(self.change)
        newer["cursor"] = 4
        newer["payload"]["base"]["fields"][1] = "latest accepted meaning"
        newer["payload"]["note"]["fields"][1] = "newer Shirabe meaning"
        self.server.page = {"cursor": 4, "changes": [newer]}
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.assertEqual("newer Shirabe meaning", self.collection.get_note(self.note.id).fields[1])

    def test_a_card_receipt_never_suppresses_review_history_delivery(self):
        self.add_review()
        card_id = self.note.cards()[0].id
        with SyncState(self.path) as state:
            batch = state.enqueue(key="epoch", cards=[{"row": {"id": card_id}}])
            state.remember_receipt(batch["request_key"], {"cursor": 3, "cards": [{"id": card_id, "status": "accepted"}]})
            state.complete_entity(batch["request_key"], "cards", card_id, [], cursor=3)
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog where cid = ?", card_id))
        self.assertEqual(1, self.collection.get_card(card_id).reps)

    def undo_delivery(self):
        from shirabe_addon.sync_reviews import history_row
        card = self.note.cards()[0]
        restored = card_row(self.collection, card.id)
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        reviewed = card_row(self.collection, card.id)
        review_id = self.collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", card.id)
        row = history_row(self.collection, review_id)
        self.server.page = {"cursor": 1, "changes": [{"cursor": 1, "entity_kind": "card", "entity_id": 30, "operation": "upsert",
            "payload": {"snapshot": "undo-card", "card": {"anki_id": card.id, "deleted": False},
                        "anki": {"card_before": reviewed, "card_after": restored, "note_guid": self.note.guid,
                                 "anki_type_id": self.note.mid, "removed_history": [row]}}}]}
        self.server.page["changes"][0]["payload"]["anki"]["history_removals"] = [{"id": review_id, "removal_id": 9}]
        return card, row, reviewed

    def test_received_undo_removes_unsynced_history_and_recovers_a_lost_local_receipt(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        self.delivery.receive()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            self.delivery.apply(self.collection)
        self.assertIsNone(history_row(self.collection, row["id"]))
        self.assertEqual(0, self.collection.get_card(card.id).reps)
        self.assertEqual(0, self.server.acknowledged)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.collection.get_card(card.id).reps)
        with SyncState(self.path) as state:
            self.assertEqual("deleted", state.checkpoint_digest("source_history", row["id"]))
            self.assertEqual("9", state.checkpoint_digest("history_removal", row["id"]))

    def test_received_undo_accepts_history_already_removed_by_native_undo(self):
        card, row, reviewed = self.undo_delivery()
        self.delivery.receive()
        self.collection.undo()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.collection.get_card(card.id).reps)
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_large_history_delivery_removes_and_restores_all_events_after_a_lost_local_receipt(self):
        from shirabe_addon.sync_reviews import REVLOG_COLUMNS
        card, row, reviewed = self.undo_delivery()
        rows = [row] + [{**row, "id": row["id"] - index - 1} for index in range(400)]
        self.collection.db.executemany(
            f"INSERT INTO revlog ({','.join(REVLOG_COLUMNS)}) VALUES ({','.join('?' for _ in REVLOG_COLUMNS)})",
            [tuple(item[key] for key in REVLOG_COLUMNS) for item in rows[1:]])
        source = self.server.page["changes"][0]["payload"]["anki"]
        source["removed_history"] = rows
        source["history_removals"] = [{"id": item["id"], "removal_id": index + 1} for index, item in enumerate(rows)]
        restored = source["card_after"]
        for cursor, count in ((1, 0), (2, 401)):
            with self.subTest(cursor=cursor):
                if cursor == 2:
                    self.server.page = {"cursor": 2, "changes": [{"cursor": 2, "entity_kind": "card", "entity_id": 30,
                        "operation": "upsert", "payload": {"snapshot": "redo-card", "card": {"anki_id": card.id, "deleted": False},
                            "anki": {"card_before": restored, "card_after": reviewed, "note_guid": self.note.guid,
                                     "anki_type_id": self.note.mid,
                                     "restored_history": [{"row": item, "removal_id": index + 1} for index, item in enumerate(rows)]}}}]}
                self.delivery.receive()
                with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
                    self.delivery.apply(self.collection)
                self.assertEqual(count, self.collection.db.scalar("SELECT count(*) FROM revlog"))
                self.restart()
                self.delivery.apply(self.collection)
                self.delivery.acknowledge()
                self.assertEqual(count, self.collection.db.scalar("SELECT count(*) FROM revlog"))
                self.assertEqual(cursor - 1, self.collection.get_card(card.id).reps)
                with SyncState(self.path) as state:
                    checkpoints = state.checkpoint_digests("source_history")
                    self.assertEqual(401, len(checkpoints))
                    self.assertTrue(all((digest == "deleted") == (count == 0) for digest in checkpoints.values()))
        self.assertEqual(sorted(rows, key=lambda item: item["id"]),
            [dict(zip(REVLOG_COLUMNS, values)) for values in self.collection.db.all("SELECT * FROM revlog ORDER BY id")])

    def mixed_history_delivery(self):
        card, removed, reviewed = self.undo_delivery()
        restored = {**removed, "id": removed["id"] - 100}
        source = self.server.page["changes"][0]["payload"]["anki"]
        source["card_after"] = copy.deepcopy(reviewed)
        source["restored_history"] = [{"row": restored, "removal_id": 10}]
        return card, removed, restored, reviewed

    def test_mixed_history_delivery_retries_both_operations_after_a_lost_local_receipt(self):
        card, removed, restored, reviewed = self.mixed_history_delivery()
        self.delivery.receive()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            self.delivery.apply(self.collection)
        self.assertIsNone(history_row(self.collection, removed["id"]))
        self.assertEqual(restored, history_row(self.collection, restored["id"]))
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual(0, self.server.acknowledged)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertIsNone(history_row(self.collection, removed["id"]))
        self.assertEqual(restored, history_row(self.collection, restored["id"]))
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        with SyncState(self.path) as state:
            self.assertEqual("deleted", state.checkpoint_digest("source_history", removed["id"]))
            self.assertEqual("9", state.checkpoint_digest("history_removal", removed["id"]))
            self.assertEqual(restored, state.checkpoint("source_history", restored["id"])["payload"])

    def test_mixed_history_delivery_rolls_back_both_lists_and_schedule_when_card_write_fails(self):
        card, removed, restored, reviewed = self.mixed_history_delivery()
        self.server.page["changes"][0]["payload"]["anki"]["card_after"]["due"] += 20
        self.delivery.receive()
        with patch.object(self.collection, "update_card", side_effect=RuntimeError("injected card failure")), self.assertRaisesRegex(RuntimeError, "injected card failure"):
            self.delivery.apply(self.collection)
        self.assertEqual(removed, history_row(self.collection, removed["id"]))
        self.assertIsNone(history_row(self.collection, restored["id"]))
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        with SyncState(self.path) as state:
            self.assertIsNotNone(state.history_removal(1))
            self.assertIsNone(state.delivery_result(1))
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("interrupted", caught.exception.conflicts[0]["code"])
        self.assertEqual(removed, history_row(self.collection, removed["id"]))
        self.assertIsNone(history_row(self.collection, restored["id"]))
        self.assertEqual(reviewed, card_row(self.collection, card.id))

    def test_mixed_history_delivery_validates_restorations_before_recording_removal_intent(self):
        from shirabe_addon.sync_reviews import REVLOG_COLUMNS
        card, removed, restored, reviewed = self.mixed_history_delivery()
        collision = {**restored, "ease": 1}
        self.collection.db.execute(
            f"INSERT INTO revlog ({','.join(REVLOG_COLUMNS)}) VALUES ({','.join('?' for _ in REVLOG_COLUMNS)})",
            *(collision[key] for key in REVLOG_COLUMNS))
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("different event", caught.exception.conflicts[0]["code"])
        self.assertEqual(removed, history_row(self.collection, removed["id"]))
        self.assertEqual(collision, history_row(self.collection, restored["id"]))
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        with SyncState(self.path) as state:
            self.assertIsNone(state.history_removal(1))

    def test_mixed_history_delivery_cannot_remove_published_history_or_restore_its_other_event(self):
        card, removed, restored, reviewed = self.mixed_history_delivery()
        self.collection.db.execute("UPDATE revlog SET usn=5 WHERE id=?", removed["id"])
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("native sync", caught.exception.conflicts[0]["code"])
        self.assertEqual({**removed, "usn": 5}, history_row(self.collection, removed["id"]))
        self.assertIsNone(history_row(self.collection, restored["id"]))
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        with SyncState(self.path) as state:
            self.assertIsNone(state.history_removal(1))

    def check_mixed_history_choice(self, side):
        card, removed, restored, reviewed = self.mixed_history_delivery()
        remote = self.server.page["changes"][0]["payload"]["anki"]["card_after"]
        remote["due"] += 20
        local = self.collection.get_card(card.id)
        local.due += 10
        self.collection.update_card(local)
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"api": self.server.api, "phase": "uploaded", "profile": {"id": 123},
                "pair_request": {"key": "epoch"}, "collection_path": str(Path(self.collection.path).resolve()), "deck_ids": [card.did]})
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        prepared = self.delivery.prepare_card_resolution(self.collection, caught.exception.conflicts[0])
        self.assertEqual([{"row": restored, "removal_id": 10}], prepared["entry"]["restored_history"])
        self.delivery.resolve_history_removal(self.collection, prepared, schedule=side)
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            self.delivery.apply(self.collection)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(local.due if side == "anki" else remote["due"], self.collection.get_card(card.id).due)
        self.assertIsNone(history_row(self.collection, removed["id"]))
        self.assertEqual(restored, history_row(self.collection, restored["id"]))

    def test_mixed_history_choice_keeps_the_anki_schedule_after_a_lost_receipt(self):
        self.check_mixed_history_choice("anki")

    def test_mixed_history_choice_keeps_the_incoming_schedule_after_a_lost_receipt(self):
        self.check_mixed_history_choice("shirabe")

    def test_received_undo_retry_does_not_remove_a_later_native_redo(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        self.delivery.receive()
        self.collection.undo()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            self.delivery.apply(self.collection)
        self.collection.redo()
        self.assertEqual(row, history_row(self.collection, row["id"]))
        current = card_row(self.collection, card.id)
        self.restart()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("interrupted", caught.exception.conflicts[0]["code"])
        self.assertEqual(row, history_row(self.collection, row["id"]))
        self.assertEqual(current, card_row(self.collection, card.id))

    def incoming_undo_choice(self):
        card, row, reviewed = self.undo_delivery()
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"api": self.server.api, "phase": "uploaded", "profile": {"id": 123},
                "pair_request": {"key": "epoch"}, "collection_path": str(Path(self.collection.path).resolve()), "deck_ids": [card.did]})
        self.delivery.receive()
        self.collection.undo()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            self.delivery.apply(self.collection)
        self.collection.redo()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        return self.delivery.prepare_card_resolution(self.collection, caught.exception.conflicts[0])

    def check_incoming_undo_choice(self, side):
        prepared = self.incoming_undo_choice()
        self.assertEqual(1, len(prepared["entry"]["removed_history"]))
        self.delivery.resolve_history_removal(self.collection, prepared, schedule=side)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        self.assertEqual(1 if side == "anki" else 0, self.note.cards()[0].reps)
        with SyncState(self.path) as state:
            self.assertEqual(0, state.checkpoint("source_card", self.note.cards()[0].id)["payload"]["reps"])

    def test_interrupted_incoming_undo_can_be_confirmed_with_the_anki_schedule(self):
        self.check_incoming_undo_choice("anki")

    def test_interrupted_incoming_undo_can_be_confirmed_with_the_incoming_schedule(self):
        self.check_incoming_undo_choice("shirabe")

    def test_incoming_undo_choice_refuses_a_new_review_after_the_dialog_opened(self):
        prepared = self.incoming_undo_choice()
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        current = card_row(self.collection, card.id)
        history = self.collection.db.all("SELECT * FROM revlog ORDER BY id")
        with self.assertRaises(DeliveryConflict):
            self.delivery.resolve_history_removal(self.collection, prepared, schedule="shirabe")
        self.assertEqual(current, card_row(self.collection, card.id))
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog ORDER BY id"))

    def test_incoming_undo_choice_refuses_a_new_review_after_confirmation(self):
        prepared = self.incoming_undo_choice()
        self.delivery.resolve_history_removal(self.collection, prepared, schedule="anki")
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        current = card_row(self.collection, card.id)
        history = self.collection.db.all("SELECT * FROM revlog ORDER BY id")
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertEqual(current, card_row(self.collection, card.id))
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog ORDER BY id"))

    def test_incoming_undo_confirmation_is_not_reusable_after_another_lost_receipt(self):
        from shirabe_addon.sync_reviews import REVLOG_COLUMNS
        prepared = self.incoming_undo_choice()
        row = prepared["entry"]["removed_history"][0]
        self.delivery.resolve_history_removal(self.collection, prepared, schedule="anki")
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            self.delivery.apply(self.collection)
        self.collection.db.execute("INSERT INTO revlog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", *(row[key] for key in REVLOG_COLUMNS))
        self.restart()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        refreshed = self.delivery.prepare_card_resolution(self.collection, caught.exception.conflicts[0])
        self.delivery.resolve_history_removal(self.collection, refreshed, schedule="shirabe")
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_incoming_undo_choice_cannot_remove_a_review_published_after_confirmation(self):
        prepared = self.incoming_undo_choice()
        self.delivery.resolve_history_removal(self.collection, prepared, schedule="shirabe")
        row = prepared["entry"]["removed_history"][0]
        self.collection.db.execute("UPDATE revlog SET usn = 3 WHERE id = ?", row["id"])
        current = card_row(self.collection, self.note.cards()[0].id)
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("native sync", caught.exception.conflicts[0]["code"])
        self.assertEqual(current, card_row(self.collection, self.note.cards()[0].id))
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_a_failed_confirmed_removal_requires_a_new_confirmation(self):
        prepared = self.incoming_undo_choice()
        self.delivery.resolve_history_removal(self.collection, prepared, schedule="shirabe")
        with patch.object(self.collection, "update_card", side_effect=RuntimeError("injected card failure")), self.assertRaises(RuntimeError):
            self.delivery.apply(self.collection)
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("interrupted", caught.exception.conflicts[0]["code"])
        refreshed = self.delivery.prepare_card_resolution(self.collection, caught.exception.conflicts[0])
        self.assertNotEqual(prepared["prior_removal"], refreshed["prior_removal"])
        self.delivery.resolve_history_removal(self.collection, refreshed, schedule="shirabe")
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_incoming_undo_choice_keeps_other_reviews_present_when_the_dialog_opens(self):
        prepared = self.incoming_undo_choice()
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        refreshed = self.delivery.prepare_card_resolution(self.collection, caught.exception.conflicts[0])
        removed_id = prepared["entry"]["removed_history"][0]["id"]
        remaining = self.collection.db.all("SELECT * FROM revlog WHERE id != ?", removed_id)
        self.delivery.resolve_history_removal(self.collection, refreshed, schedule="anki")
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(remaining, self.collection.db.all("SELECT * FROM revlog"))
        self.assertEqual(2, self.note.cards()[0].reps)

    def test_incoming_undo_schedule_choice_preserves_local_suspension_and_properties(self):
        prepared = self.incoming_undo_choice()
        card = self.note.cards()[0]
        card.queue, card.flags, card.custom_data = -1, 3, '{"source":"learner"}'
        self.collection.update_card(card)
        refreshed = self.delivery.prepare_card_resolution(self.collection, prepared["problem"])
        self.delivery.resolve_history_removal(self.collection, refreshed, schedule="shirabe")
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        card = self.note.cards()[0]
        self.assertEqual((-1, 3, 0), (card.queue, card.flags, card.reps))
        self.assertEqual('{"source":"learner"}', card.custom_data)
        self.assertEqual(refreshed["local"]["did"], card.did)

    def test_generic_card_choices_cannot_authorize_an_interrupted_history_removal(self):
        prepared = self.incoming_undo_choice()
        with self.assertRaisesRegex(DeliveryConflict, "Undo chooser"):
            self.delivery.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_choice(1))

    def test_received_undo_refuses_history_published_after_receiving_the_request(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        self.delivery.receive()
        self.collection.db.execute("UPDATE revlog SET usn = 0 WHERE id = ?", row["id"])
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("native sync", caught.exception.conflicts[0]["code"])
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual({**row, "usn": 0}, history_row(self.collection, row["id"]))
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))
            self.assertIsNone(state.checkpoint("history_removal", row["id"]))
        self.assertEqual(0, self.server.acknowledged)

    def test_received_undo_does_not_remove_changed_event_content(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        self.collection.db.execute("UPDATE revlog SET ease = 1 WHERE id = ?", row["id"])
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("different event", caught.exception.conflicts[0]["code"])
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual({**row, "ease": 1}, history_row(self.collection, row["id"]))

    def test_received_undo_keeps_history_when_a_later_native_review_changes_the_schedule(self):
        card, row, reviewed = self.undo_delivery()
        card = self.collection.get_card(card.id)
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        later = card_row(self.collection, card.id)
        history = self.collection.db.all("SELECT * FROM revlog ORDER BY id")
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertEqual(later, card_row(self.collection, card.id))
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog ORDER BY id"))

    def test_received_undo_rolls_back_card_changes_when_history_deletion_fails(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        self.collection.db.execute("CREATE TEMP TRIGGER reject_removal BEFORE DELETE ON revlog BEGIN SELECT RAISE(ABORT, 'injected removal failure'); END")
        self.delivery.receive()
        with self.assertRaisesRegex(Exception, "injected removal failure"):
            self.delivery.apply(self.collection)
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual(row, history_row(self.collection, row["id"]))
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))
            self.assertIsNotNone(state.history_removal(1))
        self.collection.db.execute("DROP TRIGGER reject_removal")
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("interrupted", caught.exception.conflicts[0]["code"])
        self.assertEqual(row, history_row(self.collection, row["id"]))

    def test_received_undo_removes_two_hundred_events_but_leaves_other_cards_alone(self):
        from shirabe_addon.sync_reviews import REVLOG_COLUMNS
        card, row, reviewed = self.undo_delivery()
        rows = [{**row, "id": row["id"] + index} for index in range(200)]
        self.collection.db.executemany("INSERT INTO revlog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                      [tuple(item[key] for key in REVLOG_COLUMNS) for item in rows[1:]])
        other = self.collection.new_note(self.collection.models.by_name("Basic"))
        other.fields = ["犬", "dog"]
        self.collection.add_note(other, card.did)
        other_card = other.cards()[0]
        other_card.start_timer()
        self.collection.sched.answerCard(other_card, 3)
        other_history = self.collection.db.all("SELECT * FROM revlog WHERE cid = ?", other_card.id)
        source = self.server.page["changes"][0]["payload"]["anki"]
        source["removed_history"] = rows
        source["history_removals"] = [{"id": item["id"], "removal_id": index + 1} for index, item in enumerate(rows)]
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id))
        self.assertEqual(other_history, self.collection.db.all("SELECT * FROM revlog WHERE cid = ?", other_card.id))
        self.assertEqual(0, self.collection.get_card(card.id).reps)
        with SyncState(self.path) as state:
            for index, item in enumerate(rows):
                self.assertEqual(str(index + 1), state.checkpoint_digest("history_removal", item["id"]))

    def test_received_undo_rolls_back_removed_history_when_the_card_write_fails(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        self.delivery.receive()
        with patch.object(self.collection, "update_card", side_effect=RuntimeError("injected card failure")), self.assertRaisesRegex(RuntimeError, "injected card failure"):
            self.delivery.apply(self.collection)
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual(row, history_row(self.collection, row["id"]))
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))
            self.assertIsNotNone(state.history_removal(1))

    def test_received_undo_validates_the_last_event_of_a_large_list_before_writing(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        source = self.server.page["changes"][0]["payload"]["anki"]
        source["removed_history"] = [{**row, "id": row["id"] + index} for index in range(201)]
        source["removed_history"][-1]["cid"] += 1
        source["history_removals"] = [{"id": item["id"], "removal_id": index + 1} for index, item in enumerate(source["removed_history"])]
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("identity does not match this card", caught.exception.conflicts[0]["code"])
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual(row, history_row(self.collection, row["id"]))
        with SyncState(self.path) as state:
            self.assertIsNone(state.history_removal(1))

    def test_received_undo_refuses_duplicate_event_ids(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        source = self.server.page["changes"][0]["payload"]["anki"]
        source["removed_history"].append(row)
        source["history_removals"].append({"id": row["id"], "removal_id": 10})
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("identity is invalid", caught.exception.conflicts[0]["code"])
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual(row, history_row(self.collection, row["id"]))

    def test_received_undo_refuses_removing_and_restoring_the_same_event(self):
        from shirabe_addon.sync_reviews import history_row
        card, row, reviewed = self.undo_delivery()
        self.server.page["changes"][0]["payload"]["anki"]["restored_history"] = [{"row": row, "removal_id": 8}]
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict) as caught:
            self.delivery.apply(self.collection)
        self.assertIn("both removed and restored", caught.exception.conflicts[0]["code"])
        self.assertEqual(reviewed, card_row(self.collection, card.id))
        self.assertEqual(row, history_row(self.collection, row["id"]))
        with SyncState(self.path) as state:
            self.assertIsNone(state.history_removal(1))

    def redo_delivery(self):
        from shirabe_addon.sync_reviews import history_row
        card = self.note.cards()[0]
        before = card_row(self.collection, card.id)
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        after = card_row(self.collection, card.id)
        review_id = self.collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", card.id)
        row = history_row(self.collection, review_id)
        self.collection.undo()
        self.server.page = {"cursor": 1, "changes": [{"cursor": 1, "entity_kind": "card", "entity_id": 30, "operation": "upsert",
            "payload": {"snapshot": "redo-card", "card": {"anki_id": card.id, "deleted": False},
                        "anki": {"card_before": before, "card_after": after, "note_guid": self.note.guid,
                                 "anki_type_id": self.note.mid, "restored_history": [{"row": row, "removal_id": 9}]}}}]}
        return card, row

    def test_received_redo_restores_native_history_and_retries_after_a_lost_local_receipt(self):
        from shirabe_addon.sync_reviews import history_row
        card, row = self.redo_delivery()
        review_id = row["id"]
        self.delivery.receive()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Lost local receipt")), self.assertRaises(OSError):
            self.delivery.apply(self.collection)
        self.assertEqual(row, history_row(self.collection, review_id))
        self.assertEqual(1, self.collection.get_card(card.id).reps)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        with SyncState(self.path) as state:
            self.assertEqual(row, state.checkpoint("source_history", review_id)["payload"])

    def test_received_redo_does_not_replace_an_event_with_the_same_native_id(self):
        from shirabe_addon.sync_reviews import history_row, REVLOG_COLUMNS
        card, row = self.redo_delivery()
        before = card_row(self.collection, card.id)
        conflicting = {**row, "ease": 1}
        self.collection.db.execute("INSERT INTO revlog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", *(conflicting[key] for key in REVLOG_COLUMNS))
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertEqual(conflicting, history_row(self.collection, row["id"]))
        self.assertEqual(before, card_row(self.collection, card.id))

    def test_received_redo_rolls_back_earlier_history_inserts_and_card_writes_on_failure(self):
        card, row = self.redo_delivery()
        before = card_row(self.collection, card.id)
        self.server.page["changes"][0]["payload"]["anki"]["restored_history"].append({"removal_id": 10, "row": {**row, "id": "invalid"}})
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        self.assertEqual(before, card_row(self.collection, card.id))

    def test_receiving_a_receipt_without_applying_it_does_not_suppress_delivery(self):
        with SyncState(self.path) as state:
            batch = state.enqueue(key="epoch", notes=[{"id": self.note.id}])
            state.remember_receipt(batch["request_key"], {"cursor": 3, "notes": [{"id": self.note.id, "status": "accepted"}]})
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.assertEqual("feline", self.collection.get_note(self.note.id).fields[1])

    def test_review_commit_before_delivery_receipt_recovers_without_duplicate_history(self):
        self.add_review()
        self.delivery.receive()
        finish = SyncState.finish_delivery_change

        def interrupted(state, cursor, result, **kwargs):
            if cursor == 2:
                raise OSError("Delivery receipt unavailable")
            return finish(state, cursor, result, **kwargs)

        with patch.object(SyncState, "finish_delivery_change", interrupted):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))
        self.assertEqual(2, self.server.acknowledged)

    def test_returned_review_keeps_its_delivery_cursor_across_a_lost_response(self):
        self.add_review()
        self.delivery.receive()
        self.delivery.apply(self.collection)
        with SyncState(self.path) as state:
            delivery = state.get("delivery")
            payload = state.batch(delivery["return_key"])["payload"]
            self.assertEqual(2, payload["history"][0]["delivery_cursor"])
        self.server.lose_batch_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.restart()
        self.delivery.acknowledge()
        requests = [request[2] for request in self.server.requests if request[0] == "batch"]
        self.assertEqual([payload, payload], requests)
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))
        self.assertEqual(2, self.server.acknowledged)

    def test_checkpoint_and_event_result_roll_back_together(self):
        self.delivery.receive()
        with patch.object(SyncState, "_remember_checkpoint", side_effect=OSError("Checkpoint unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))
            self.assertIsNone(state.checkpoint("note", self.note.id))
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(["猫", "feline"], self.collection.get_note(self.note.id).fields)

    def test_return_batch_and_delivery_link_roll_back_together(self):
        self.add_review()
        self.delivery.receive()
        put = SyncState._put

        def interrupted(state, key, value):
            if key == "delivery" and value["return_key"] is not None:
                raise OSError("Return link unavailable")
            return put(state, key, value)

        with patch.object(SyncState, "_put", interrupted):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual([], state.pending())
            self.assertIsNone(state.get("delivery")["return_key"])
            self.assertIsNotNone(state.delivery_result(2))
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, len(self.server.receipts))

    def test_a_field_conflict_keeps_the_page_available_for_resolution(self):
        self.delivery.receive()
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "kitten"
        self.collection.update_note(note)
        with self.assertRaises(DeliveryConflict) as error:
            self.delivery.apply(self.collection)
        self.assertEqual("incoming", error.exception.conflicts[0]["direction"])
        self.assertEqual("notes", error.exception.conflicts[0]["kind"])
        self.assertEqual(1, error.exception.conflicts[0]["cursor"])
        with self.assertRaises(DeliveryConflict):
            self.delivery.acknowledge()
        self.restart()
        self.assertEqual(self.server.page, self.delivery.receive())
        self.assertEqual(["猫", "kitten"], self.collection.get_note(self.note.id).fields)
        self.assertEqual(0, self.server.acknowledged)

    def incoming_note_choice(self):
        self.delivery.receive()
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "kitten"
        self.collection.update_note(note)
        with self.assertRaises(DeliveryConflict) as error:
            self.delivery.apply(self.collection)
        return self.delivery.prepare_note_resolution(self.collection, error.exception.conflicts[0])

    def test_a_saved_incoming_choice_survives_restart_and_a_lost_ack_without_overwriting_later_edits(self):
        prepared = self.incoming_note_choice()
        self.delivery.resolve_note(self.collection, prepared, ["猫", "chosen meaning"], ["chosen"])
        self.assertEqual("kitten", self.collection.get_note(self.note.id).fields[1])
        self.restart()
        self.delivery.apply(self.collection)
        self.assertEqual("chosen meaning", self.collection.get_note(self.note.id).fields[1])
        with SyncState(self.path) as state:
            self.assertEqual(self.server.page, state.get("delivery")["response"])
            self.assertEqual("猫\x1ffeline", state.checkpoint("source_note", self.note.id)["payload"]["flds"])
        self.server.lose_ack_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "edited after applying"
        self.collection.update_note(note)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual("edited after applying", self.collection.get_note(self.note.id).fields[1])
        self.assertEqual(["chosen"], self.collection.get_note(self.note.id).tags)
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_choice(1))

    def test_an_interruption_after_native_choice_application_retries_the_saved_intent(self):
        prepared = self.incoming_note_choice()
        self.delivery.resolve_note(self.collection, prepared, ["猫", "chosen meaning"], [])
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Sidecar unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertEqual("chosen meaning", self.collection.get_note(self.note.id).fields[1])
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual("chosen meaning", self.collection.get_note(self.note.id).fields[1])

    def test_an_incoming_choice_cannot_use_a_stale_local_comparison(self):
        prepared = self.incoming_note_choice()
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "edited while choosing"
        self.collection.update_note(note)
        with self.assertRaisesRegex(DeliveryConflict, "changed"):
            self.delivery.resolve_note(self.collection, prepared, ["猫", "chosen meaning"], [])
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_choice(1))

    def test_a_later_edit_can_be_chosen_again_without_dropping_the_incoming_change(self):
        prepared = self.incoming_note_choice()
        self.delivery.resolve_note(self.collection, prepared, ["猫", "first choice"], [])
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "later Anki edit"
        self.collection.update_note(note)
        with self.assertRaises(DeliveryConflict) as error:
            self.delivery.apply(self.collection)
        self.assertEqual("later Anki edit", self.collection.get_note(self.note.id).fields[1])
        refreshed = self.delivery.prepare_note_resolution(self.collection, error.exception.conflicts[0])
        self.delivery.resolve_note(self.collection, refreshed, ["猫", "second choice"], [])
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual("second choice", self.collection.get_note(self.note.id).fields[1])

    def test_a_conflict_preserves_later_changes_to_the_same_note_but_applies_an_independent_note(self):
        other = self.collection.new_note(self.note.note_type())
        other.fields = ["犬", "dog"]
        self.collection.add_note(other, self.note.cards()[0].did)
        repeated = copy.deepcopy(self.change)
        repeated["cursor"] = 2
        repeated["payload"]["base"]["fields"][1] = "feline"
        repeated["payload"]["note"]["fields"][1] = "cat"
        independent = copy.deepcopy(self.change)
        independent["cursor"] = 3
        independent["payload"]["base"]["fields"] = ["犬", "dog"]
        independent["payload"]["note"].update(anki_id=other.id, guid=other.guid, fields=["犬", "canine"])
        self.server.page = {"cursor": 3, "changes": [self.change, repeated, independent]}
        prepared = self.incoming_note_choice()
        self.assertEqual("canine", self.collection.get_note(other.id).fields[1])
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))
            self.assertIsNone(state.delivery_result(2))
            self.assertEqual({}, state.delivery_result(3))
        with self.assertRaises(DeliveryConflict):
            self.delivery.acknowledge()
        self.assertEqual(self.note.id, prepared["problem"]["id"])

    def test_lost_history_response_retries_the_same_batch_before_acknowledgement(self):
        self.add_review()
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.server.lose_batch_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.assertEqual(0, self.server.acknowledged)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        batches = [item[2] for item in self.server.requests if item[0] == "batch"]
        self.assertEqual(batches[0], batches[1])
        self.assertEqual(1, len(self.server.receipts))
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))

    def test_returned_history_checkpoint_failure_retries_saved_receipt_before_acknowledgement(self):
        self.add_review()
        self.delivery.receive()
        self.delivery.apply(self.collection)
        with patch.object(SyncState, "complete_batch", side_effect=OSError("checkpoint unavailable")):
            with self.assertRaisesRegex(OSError, "checkpoint unavailable"):
                self.delivery.acknowledge()
        self.assertEqual(0, self.server.acknowledged)
        card_id = self.note.cards()[0].id
        with SyncState(self.path) as state:
            self.assertEqual({}, state.card_history(card_id))
        self.restart()
        self.delivery.acknowledge()
        self.assertEqual(2, self.server.acknowledged)
        self.assertEqual(1, len([item for item in self.server.requests if item[0] == "batch"]))
        source_id = self.collection.db.scalar("select id from revlog where cid = ?", card_id)
        with SyncState(self.path) as state:
            self.assertEqual({source_id: history_row(self.collection, source_id)}, state.card_history(card_id))

    def incoming_card_choice(self, remote_changes=None, local_changes=None, selected_decks=()):
        card = self.note.cards()[0]
        before = card_row(self.collection, card.id)
        after = {**before, "flags": 2, **(remote_changes or {})}
        self.server.page = {"cursor": 1, "changes": [{"cursor": 1, "entity_kind": "card", "entity_id": 11,
            "operation": "upsert", "payload": {"snapshot": "incoming-card", "card": {"anki_id": card.id},
                "anki": {"card_before": before, "card_after": after, "note_guid": self.note.guid, "anki_type_id": self.note.mid}}}]}
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"api": self.server.api, "phase": "uploaded", "profile": {"id": 123},
                "pair_request": {"key": "epoch"}, "collection_path": str(Path(self.collection.path).resolve()), "deck_ids": [card.did, *selected_decks]})
        self.delivery.receive()
        card.flags = 3
        for key, value in (local_changes or {}).items():
            setattr(card, key, value)
        self.collection.update_card(card)
        with self.assertRaises(DeliveryConflict) as error:
            self.delivery.apply(self.collection)
        return self.delivery.prepare_card_resolution(self.collection, error.exception.conflicts[0])

    def test_incoming_card_choices_combine_selected_deck_lifecycle_and_properties(self):
        destination = self.collection.decks.id("Japanese::Second")
        prepared = self.incoming_card_choice(remote_changes={"did": destination, "queue": -1},
            local_changes={"queue": -2}, selected_decks=[destination])
        self.delivery.resolve_card(self.collection, prepared, {"deck": "shirabe", "lifecycle": "anki", "properties": "shirabe"})
        self.assertNotEqual(destination, self.note.cards()[0].did)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        card = self.note.cards()[0]
        self.assertEqual((destination, -2, 2, 0), (card.did, card.queue, card.flags, card.reps))

    def test_incoming_card_choice_cannot_bypass_a_changed_source_identity(self):
        prepared = self.incoming_card_choice()
        with SyncState(self.path) as state:
            page = state.get("delivery")
            page["response"]["changes"][0]["payload"]["anki"]["card_before"]["nid"] += 1
            state.put("delivery", page)
        with self.assertRaisesRegex(DeliveryConflict, "identity"):
            self.delivery.prepare_card_resolution(self.collection, prepared["problem"])

    def test_incoming_card_choice_survives_restart_and_lost_ack_preserving_server_checkpoint(self):
        prepared = self.incoming_card_choice()
        self.delivery.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
        self.assertEqual(3, self.note.cards()[0].flags)
        self.restart()
        self.delivery.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual(2, state.checkpoint("source_card", self.note.cards()[0].id)["payload"]["flags"])
        self.server.lose_ack_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        card = self.note.cards()[0]
        card.flags = 4
        self.collection.update_card(card)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(4, self.note.cards()[0].flags)
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_choice(1))

    def test_incoming_card_choice_recovers_after_native_write_before_sidecar_result(self):
        prepared = self.incoming_card_choice()
        self.delivery.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Sidecar unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertEqual(2, self.note.cards()[0].flags)
        self.restart()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(2, self.note.cards()[0].flags)

    def test_incoming_card_choice_preserves_a_review_made_after_staging(self):
        prepared = self.incoming_card_choice()
        self.delivery.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = self.collection.db.all("SELECT * FROM revlog")
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, self.note.cards()[0].reps)
        self.assertEqual(2, self.note.cards()[0].flags)
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))

    def test_incoming_card_choice_refuses_stale_chooser_and_divergent_schedules(self):
        prepared = self.incoming_card_choice()
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        choices = {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"}
        with self.assertRaisesRegex(DeliveryConflict, "changed"):
            self.delivery.resolve_card(self.collection, prepared, choices)
        refreshed = self.delivery.prepare_card_resolution(self.collection, prepared["problem"])
        self.assertFalse(refreshed["schedule_matches"])
        with self.assertRaisesRegex(DeliveryConflict, "review history"):
            self.delivery.resolve_card(self.collection, refreshed, choices)
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_choice(1))
        self.assertEqual(3, self.note.cards()[0].flags)

    def test_incoming_card_choice_refuses_a_changed_binding_or_unselected_deck(self):
        prepared = self.incoming_card_choice()
        with SyncState(self.path) as state:
            original = state.get("initial_snapshot")
        for replacement in ({"deck_ids": []}, {"pair_request": {"key": "another-epoch"}},
                            {"collection_path": "/another/collection.anki2"}, {"profile": {"id": 456}}):
            with self.subTest(replacement=replacement):
                with SyncState(self.path) as state:
                    state.put("initial_snapshot", {**original, **replacement})
                with self.assertRaises(DeliveryConflict):
                    self.delivery.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
                with SyncState(self.path) as state:
                    self.assertIsNone(state.delivery_choice(1))

    def test_an_incoming_card_edited_after_staging_can_be_chosen_again(self):
        prepared = self.incoming_card_choice()
        choices = {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"}
        self.delivery.resolve_card(self.collection, prepared, choices)
        card = self.note.cards()[0]
        card.flags = 4
        self.collection.update_card(card)
        with self.assertRaises(DeliveryConflict) as error:
            self.delivery.apply(self.collection)
        refreshed = self.delivery.prepare_card_resolution(self.collection, error.exception.conflicts[0])
        self.delivery.resolve_card(self.collection, refreshed, choices)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(2, self.note.cards()[0].flags)

    def test_lost_acknowledgement_response_does_not_repeat_native_writes_or_history_upload(self):
        self.add_review()
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.server.lose_ack_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.restart()
        with patch("shirabe_addon.sync_delivery.apply_review", side_effect=AssertionError("Repeated review")), \
             patch("shirabe_addon.sync_delivery.apply_note_change", side_effect=AssertionError("Repeated note")):
            self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, len([item for item in self.server.requests if item[0] == "batch"]))
        with SyncState(self.path) as state:
            self.assertEqual(2, state.get("delivery_cursor"))

    def test_history_conflict_is_retained_and_never_acknowledged_on_restart(self):
        self.add_review()
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.server.reject_history = True
        for _ in range(2):
            with self.assertRaises(DeliveryConflict):
                self.delivery.acknowledge()
            self.restart()
        self.assertEqual(0, self.server.acknowledged)
        self.assertEqual(1, len([item for item in self.server.requests if item[0] == "batch"]))
        with SyncState(self.path) as state:
            self.assertEqual({}, state.card_history(self.note.cards()[0].id))

    def test_unapplied_and_unsupported_changes_cannot_advance_the_cursor(self):
        self.server.page["changes"].append({"cursor": 2, "entity_kind": "media", "entity_id": 30,
                                             "operation": "delete", "payload": {}})
        self.server.page["cursor"] = 2
        self.delivery.receive()
        with self.assertRaises(DeliveryConflict):
            self.delivery.acknowledge()
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertEqual(["猫", "cat"], self.collection.get_note(self.note.id).fields)
        self.assertEqual(0, self.server.acknowledged)

    def test_cancelled_cursor_gaps_and_empty_pages_are_acknowledged(self):
        self.server.page = {"cursor": 5, "changes": []}
        self.delivery.receive()
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(5, self.server.acknowledged)
        self.server.page = {"cursor": 5, "changes": []}
        self.delivery.receive()
        self.assertEqual(5, self.server.requests[-1][2]["cursor"])

    def test_changed_server_profile_or_epoch_cannot_reuse_a_sidecar(self):
        self.delivery.receive()
        for profile_id, key in ((124, "epoch"), (123, "other-epoch")):
            with self.assertRaises(DeliveryConflict):
                SyncDelivery(self.path, self.server, profile_id=profile_id, key=key)
        self.server.api = "https://other.example/api/v1"
        with self.assertRaises(DeliveryConflict):
            self.restart()

    def test_invalid_cursor_order_is_rejected_before_persisting_or_applying(self):
        for page in ({"cursor": -1, "changes": []},
                     {"cursor": True, "changes": []},
                     {"cursor": 0, "changes": [self.change]},
                     {"cursor": 1, "changes": [self.change, self.change]}):
            self.server.page = page
            with self.assertRaises(DeliveryConflict):
                self.delivery.receive()
            with SyncState(self.path) as state:
                self.assertIsNone(state.get("delivery"))
