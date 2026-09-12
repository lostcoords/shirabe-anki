from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import support
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_notes import NoteConflict
from shirabe_addon.sync_note_deletions import DELETION_RECEIPT
from shirabe_addon.sync_snapshot import SnapshotConflict, SyncSnapshot, source_digest
from shirabe_addon.sync_state import SyncState
from test_sync_delivery import Collection, Server, version


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncNoteDeletionsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sync.sqlite3"
        self.collection = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        self.deck = self.collection.decks.id("Japanese")
        self.outside = self.collection.decks.id("Outside")
        self.note = self.collection.new_note(self.collection.models.by_name("Basic (and reversed card)"))
        self.note.fields = ["猫", "cat"]
        self.collection.add_note(self.note, self.deck)
        self.other = self.collection.new_note(self.collection.models.by_name("Basic"))
        self.other.fields = ["犬", "dog"]
        self.collection.add_note(self.other, self.outside)
        self.server = Server()
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"phase": "uploaded", "api": self.server.api, "profile": {"id": 123},
                "pair_request": {"key": "epoch"}, "collection_path": str(Path(self.collection.path).resolve()),
                "root_deck_ids": [self.deck], "deck_ids": [self.deck]})
        entries = []
        for row in SyncSnapshot._rows(self.collection, "cards", "nid = ?", (self.note.id,), with_last_event=True):
            last_event = row.pop("last_event_id")
            entries.append({"row": row, "last_event_id": last_event})
        self.change = {"cursor": 1, "entity_kind": "note", "entity_id": 10, "operation": "delete", "payload": {
            "note": {"anki_id": self.note.id, "guid": self.note.guid, "anki_type_id": self.note.mid,
                "field_names": [field["name"] for field in self.note.note_type()["flds"]],
                "fields": self.note.fields[:], "tags": self.note.tags[:]},
            "source_note": next(SyncSnapshot._rows(self.collection, "notes", "id = ?", (self.note.id,))),
            "definition": copy.deepcopy(self.note.note_type()), "cards": entries}}
        self.server.page = {"cursor": 1, "changes": [self.change]}
        with SyncState(self.path) as state:
            state.remember_checkpoint("note", self.note.id, "note-base", self.change["payload"]["note"])
            for entry in entries:
                row = entry["row"]
                state.remember_checkpoint("source_card", row["id"], source_digest(row), row)
                state.remember_checkpoint("card", row["id"], "card-base", {"anki_id": row["id"]})

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def exists(self):
        return bool(self.collection.db.scalar("SELECT count(*) FROM notes WHERE id = ?", self.note.id))

    def apply(self):
        self.delivery.receive()
        self.delivery.apply(self.collection)

    def test_deletes_exactly_the_selected_note_and_preserves_native_undo(self):
        self.apply()
        self.assertFalse(self.exists())
        self.assertEqual("犬", self.collection.get_note(self.other.id).fields[0])
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM cards WHERE nid = ?", self.note.id))
        self.delivery.acknowledge()
        self.assertEqual(1, self.server.acknowledged)
        self.assertTrue(self.collection.undo_status().undo)
        self.collection.undo()
        self.assertEqual(["猫", "cat"], self.collection.get_note(self.note.id).fields)
        self.assertEqual(2, len(self.collection.get_note(self.note.id).cards()))

    def test_native_undo_before_acknowledgement_still_publishes_deletion_then_restoration(self):
        self.apply()
        self.collection.undo()
        self.delivery.acknowledge()
        deleted = [entry for method, _, payload in self.server.requests if method == "batch"
                   for entry in payload.get("cards", []) if entry.get("deleted")]
        self.assertEqual({card.id for card in self.note.cards()}, {entry["row"]["id"] for entry in deleted})
        with SyncState(self.path) as state:
            for card in self.note.cards():
                self.assertEqual("deleted", state.checkpoint_digest("source_card", card.id))
            snapshot = state.get("initial_snapshot")
            batches = list(SyncChanges(self.path, self.server)._batches(self.collection, state, snapshot))
        restored = [entry for batch in batches for entry in batch.get("cards", [])]
        self.assertEqual({card.id for card in self.note.cards()}, {entry["row"]["id"] for entry in restored})
        self.assertTrue(all(not entry.get("deleted") and entry["base"] == "deleted-card" for entry in restored))

    def test_refuses_a_sibling_outside_the_selected_decks(self):
        card = self.note.cards()[1]
        card.did = self.outside
        self.collection.update_card(card)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("outside the selected decks", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())
        self.assertEqual(2, len(self.collection.get_note(self.note.id).cards()))
        self.assertEqual(0, self.server.acknowledged)

    def test_omitting_an_unselected_sibling_from_the_request_does_not_authorize_deletion(self):
        card = self.note.cards()[1]
        card.did = self.outside
        self.collection.update_card(card)
        self.change["payload"]["cards"] = self.change["payload"]["cards"][:1]
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("complete sibling card set", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())

    def test_note_edits_refuse_without_mutating_any_card(self):
        self.note.fields[1] = "feline"
        self.collection.update_note(self.note)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("fields or tags changed", failure.exception.conflicts[0]["code"])
        self.assertEqual("feline", self.collection.get_note(self.note.id).fields[1])
        self.assertEqual(2, len(self.collection.get_note(self.note.id).cards()))

    def test_new_reviews_refuse_without_losing_history(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("new review history", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id))

    def test_template_edits_refuse_even_when_sibling_ids_stay_the_same(self):
        model = self.note.note_type()
        model["tmpls"][0]["qfmt"] += " changed"
        self.collection.models.save(model)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("note type changed", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())

    def test_selection_changes_refuse_before_deletion(self):
        self.collection.decks.id("Japanese::New")
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("changed deck selection", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())

    def test_another_collection_binding_refuses_deletion(self):
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            state.put("initial_snapshot", {**snapshot, "collection_path": str(Path(self.directory.name) / "other.anki2")})
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("linked collection", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())

    def test_lost_local_receipt_resumes_without_repeating_native_deletion(self):
        self.delivery.receive()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Sidecar unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertFalse(self.exists())
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        with patch.object(self.collection, "remove_notes", side_effect=AssertionError("Repeated deletion")):
            self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, self.server.acknowledged)

    def test_native_undo_before_a_lost_receipt_is_not_deleted_again(self):
        self.delivery.receive()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Sidecar unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.collection.undo()
        with self.assertRaises(DeliveryConflict):
            self.delivery.apply(self.collection)
        self.assertTrue(self.exists())
        self.assertEqual(0, self.server.acknowledged)

    def test_keep_choice_survives_restart_and_lost_ack_without_deleting_current_note(self):
        self.note.fields[1] = "feline"
        self.collection.update_note(self.note)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        problem = failure.exception.conflicts[0]
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        self.assertEqual("feline", prepared["local"]["flds"].split("\x1f")[1])
        self.delivery.keep_note(self.collection, prepared)
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        with patch.object(self.collection, "remove_notes", side_effect=AssertionError("Keep choice deleted a note")):
            self.delivery.apply(self.collection)
            self.server.lose_ack_response = True
            with self.assertRaises(ConnectionError):
                self.delivery.acknowledge()
            self.delivery.apply(self.collection)
            self.delivery.acknowledge()
        self.assertEqual("feline", self.collection.get_note(self.note.id).fields[1])
        self.assertEqual([1], self.server.requests[-1][2]["kept_notes"])

    def test_keep_choice_recovers_native_undo_after_lost_local_receipt(self):
        self.delivery.receive()
        with patch.object(SyncState, "finish_delivery_change", side_effect=OSError("Sidecar unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.collection.undo()
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, failure.exception.conflicts[0])
        self.assertTrue(prepared["attempted"])
        self.delivery.keep_note(self.collection, prepared)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertTrue(self.exists())
        self.assertEqual(2, len(self.collection.get_note(self.note.id).cards()))
        self.assertEqual([1], self.server.requests[-1][2]["kept_notes"])

    def test_keep_choice_refuses_a_changed_note_identity_or_a_completed_delivery(self):
        self.delivery.receive()
        problem = {"direction": "incoming", "kind": "notes", "id": self.note.id, "cursor": 1}
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        self.note.guid = "changed-guid"
        self.collection.update_note(self.note)
        with self.assertRaises(DeliveryConflict):
            self.delivery.keep_note(self.collection, prepared)
        self.note.guid = self.change["payload"]["note"]["guid"]
        self.collection.update_note(self.note)
        self.delivery.apply(self.collection)
        with self.assertRaises(DeliveryConflict):
            self.delivery.keep_note(self.collection, prepared)

    def test_keep_choice_must_be_reopened_after_a_concurrent_field_edit(self):
        self.delivery.receive()
        problem = {"direction": "incoming", "kind": "notes", "id": self.note.id, "cursor": 1}
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        self.note.fields[1] = "changed while choosing"
        self.collection.update_note(self.note)
        with self.assertRaisesRegex(DeliveryConflict, "changed while this chooser"):
            self.delivery.keep_note(self.collection, prepared)
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_choice(1))

    def test_missing_notes_and_another_collection_cannot_be_reported_as_kept(self):
        self.delivery.receive()
        problem = {"direction": "incoming", "kind": "notes", "id": self.note.id, "cursor": 1}
        with SyncState(self.path) as state:
            original = state.get("initial_snapshot")
            state.put("initial_snapshot", {**original, "collection_path": "another-collection"})
        with self.assertRaisesRegex(DeliveryConflict, "linked collection"):
            self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        with SyncState(self.path) as state:
            state.put("initial_snapshot", original)
        self.collection.remove_notes([self.note.id])
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        with self.assertRaisesRegex(DeliveryConflict, "note is absent"):
            self.delivery.keep_note(self.collection, prepared)

    def test_confirm_absence_preserves_unuploaded_history_and_survives_undo_and_lost_response(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history_id = self.collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", card.id)
        self.collection.remove_notes([self.note.id])
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, failure.exception.conflicts[0])
        self.assertIsNone(prepared["local"])
        with patch.object(self.collection, "remove_notes", side_effect=AssertionError("Repeated deletion")):
            self.delivery.confirm_note_absence(self.collection, prepared)
            self.collection.undo()
            self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
            self.delivery.apply(self.collection)
            self.server.lose_batch_response = True
            with self.assertRaises(ConnectionError):
                self.delivery.acknowledge()
            self.assertEqual(0, self.server.acknowledged)
            self.delivery.acknowledge()
        uploads = [payload for method, _, payload in self.server.requests if method == "batch"]
        self.assertEqual(uploads[0], uploads[1])
        self.assertEqual(history_id, uploads[0]["history"][0]["row"]["id"])
        self.assertEqual(2, len(uploads[2]["cards"]))
        self.assertTrue(self.exists())
        self.assertNotIn("kept_notes", self.server.requests[-1][2])
        with SyncState(self.path) as state:
            self.assertEqual("deleted", state.checkpoint_digest("source_card", card.id))
            self.assertIsNotNone(state.checkpoint_digest("source_history", history_id))

    def test_confirm_absence_refuses_native_undo_while_the_chooser_is_open(self):
        self.delivery.receive()
        self.collection.remove_notes([self.note.id])
        problem = {"direction": "incoming", "kind": "notes", "id": self.note.id, "cursor": 1}
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        self.collection.undo()
        with self.assertRaisesRegex(DeliveryConflict, "changed while this chooser"):
            self.delivery.confirm_note_absence(self.collection, prepared)
        self.assertTrue(self.exists())
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))

    def test_confirm_absence_refuses_a_requested_card_id_reassigned_to_another_note(self):
        source_id = self.note.cards()[0].id
        self.delivery.receive()
        self.collection.remove_notes([self.note.id])
        self.collection.db.execute("UPDATE cards SET id = ? WHERE nid = ?", source_id, self.other.id)
        problem = {"direction": "incoming", "kind": "notes", "id": self.note.id, "cursor": 1}
        with self.assertRaisesRegex(NoteConflict, "requested card exists"):
            self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))

    def test_failed_absence_storage_rolls_back_all_return_batches(self):
        self.delivery.receive()
        self.collection.remove_notes([self.note.id])
        problem = {"direction": "incoming", "kind": "notes", "id": self.note.id, "cursor": 1}
        prepared = self.delivery.prepare_note_deletion_resolution(self.collection, problem)
        with SyncState(self.path) as state:
            state.db.execute("CREATE TRIGGER reject_absence BEFORE INSERT ON delivery_results BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "disk failure"):
            self.delivery.confirm_note_absence(self.collection, prepared)
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))
            self.assertEqual([], state.pending())
            state.db.execute("DROP TRIGGER reject_absence")
        self.delivery.confirm_note_absence(self.collection, prepared)
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()

    def test_unaccepted_source_deletion_cannot_acknowledge_the_request(self):
        self.apply()
        response = {"cards": [{"id": entry["row"]["id"], "status": "conflict"} for entry in self.change["payload"]["cards"]]}
        with patch.object(self.server, "batch", return_value=response):
            with self.assertRaisesRegex(DeliveryConflict, "not accepted the source deletion"):
                self.delivery.acknowledge()
        self.assertEqual(0, self.server.acknowledged)
        with SyncState(self.path) as state:
            with self.assertRaisesRegex(ValueError, "source deletion has not been accepted"):
                state.complete_delivery(1)

    def test_lost_checkpoint_write_reuses_the_accepted_absence_receipt_after_undo(self):
        self.apply()
        with patch.object(SyncState, "complete_entity", side_effect=OSError("Checkpoint unavailable")):
            with self.assertRaises(OSError):
                self.delivery.acknowledge()
        self.assertEqual(0, self.server.acknowledged)
        self.collection.undo()
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertEqual(1, sum(method == "batch" for method, _, _ in self.server.requests))
        self.assertTrue(self.exists())
        with SyncState(self.path) as state:
            self.assertTrue(all(state.checkpoint_digest("source_card", card.id) == "deleted" for card in self.note.cards()))

    def test_already_accepted_source_deletion_does_not_delete_a_later_native_undo(self):
        self.delivery.receive()
        self.collection.remove_notes([self.note.id])
        with SyncState(self.path) as state:
            for entry in self.change["payload"]["cards"]:
                row = entry["row"]
                batch = state.enqueue(key="epoch", cards=[{"row": row, "deleted": True}])
                state.remember_receipt(batch["request_key"], {"cursor": 2})
                state.complete_entity(batch["request_key"], "cards", row["id"],
                                      [("source_card", row["id"], "deleted", row)], cursor=2)
        self.collection.undo()
        self.delivery.apply(self.collection)
        self.assertTrue(self.exists())
        self.delivery.acknowledge()
        self.assertEqual(1, self.server.acknowledged)
        self.assertEqual(0, sum(method == "batch" for method, _, _ in self.server.requests))

    def test_older_source_absence_cannot_fulfill_a_new_deletion_request(self):
        self.delivery.receive()
        with SyncState(self.path) as state:
            for entry in self.change["payload"]["cards"]:
                row = entry["row"]
                state.remember_checkpoint("source_card", row["id"], "deleted", row)
        self.delivery.apply(self.collection)
        self.assertFalse(self.exists())
        self.delivery.acknowledge()
        self.assertEqual(2, sum(len(payload.get("cards", [])) for method, _, payload in self.server.requests if method == "batch"))

    def test_partially_accepted_deletion_never_deletes_restored_siblings_automatically(self):
        self.delivery.receive()
        with SyncState(self.path) as state:
            row = self.change["payload"]["cards"][0]["row"]
            batch = state.enqueue(key="epoch", cards=[{"row": row, "deleted": True}])
            state.remember_receipt(batch["request_key"], {"cursor": 2})
            state.complete_entity(batch["request_key"], "cards", row["id"],
                                  [("source_card", row["id"], "deleted", row)], cursor=2)
        with self.assertRaises(DeliveryConflict) as failure:
            self.delivery.apply(self.collection)
        self.assertIn("Some requested cards", failure.exception.conflicts[0]["code"])
        self.assertEqual(2, len(self.collection.get_note(self.note.id).cards()))
        self.assertEqual(0, self.server.acknowledged)

    def test_note_with_more_than_200_cards_returns_bounded_deletion_batches(self):
        model = self.note.note_type()
        for index in range(203):
            template = self.collection.models.new_template(f"Sibling {index}")
            template.update(qfmt="{{Front}} " + str(index), afmt="{{Back}}")
            model["tmpls"].append(template)
        self.collection.models.save(model)
        self.change["payload"]["definition"] = copy.deepcopy(self.note.note_type())
        entries = []
        with SyncState(self.path) as state:
            for row in SyncSnapshot._rows(self.collection, "cards", "nid = ?", (self.note.id,), with_last_event=True):
                entries.append({"last_event_id": row.pop("last_event_id"), "row": row})
                state.remember_checkpoint("source_card", row["id"], source_digest(row), row)
                state.remember_checkpoint("card", row["id"], "card-base", {"anki_id": row["id"]})
        self.change["payload"]["cards"] = entries
        self.assertEqual(205, len(entries))
        self.apply()
        self.delivery.acknowledge()
        batches = [payload["cards"] for method, _, payload in self.server.requests if method == "batch"]
        self.assertEqual([200, 5], [len(batch) for batch in batches])
        self.assertEqual(205, len({entry["row"]["id"] for batch in batches for entry in batch}))
        self.collection.undo()
        self.assertEqual(205, len(self.collection.get_note(self.note.id).cards()))

    def returned_deletion_conflict(self, *, status="conflict"):
        self.apply()
        first = self.change["payload"]["cards"][0]["row"]
        send = self.server.batch

        def conflict(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["cards"][0] = {"id": first["id"], "status": status, "conflict_id": 81, "code": "card_changed_before_deletion"}
            return receipt

        with patch.object(self.server, "batch", side_effect=conflict):
            with self.assertRaises(DeliveryConflict) as failure:
                self.delivery.acknowledge()
        self.assertEqual(1, len(failure.exception.conflicts))
        problem = failure.exception.conflicts[0]
        self.assertEqual("returning", problem["direction"])
        self.assertEqual("delete", problem["operation"])
        detail = {"id": 81, "key": "epoch", "resolved": False, "snapshot": "current-card",
                  "card": {"anki_id": first["id"]}, "anki": {"card_after": {**first, "flags": 2},
                  "note_guid": self.note.guid, "anki_type_id": self.note.mid},
                  "deletion": {"note_base": "current-note", "note": self.change["payload"]["note"]}}
        return problem, detail

    def test_rejected_deletion_return_retries_its_saved_absence_without_repeating_native_deletion(self):
        problem, _ = self.returned_deletion_conflict(status="rejected")
        with SyncState(self.path) as state:
            source = self.change["payload"]["source_note"]
            state.remember_checkpoint("source_note", self.note.id, source_digest(source), source)
            original = state.batch(problem["request_key"])
        changes = SyncChanges(self.path, self.server)
        prepared = changes.prepare_rejected_retry(self.collection, problem)
        replacement = changes.retry_rejected(self.collection, prepared)
        self.collection.undo()
        self.server.lose_batch_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        with patch.object(self.collection, "remove_notes", side_effect=AssertionError("Repeated native deletion")):
            self.delivery.apply(self.collection)
            self.delivery.acknowledge()
        self.assertTrue(self.exists())
        with SyncState(self.path) as state:
            self.assertEqual(original, state.batch(problem["request_key"]))
            self.assertTrue(state.entity_applied(problem["request_key"], "cards", problem["id"]))
            self.assertEqual(original["payload"]["cards"][:1], state.batch(replacement)["payload"]["cards"])

    def test_returned_deletion_conflict_can_be_confirmed_without_repeating_deletion_or_losing_its_receipt(self):
        problem, detail = self.returned_deletion_conflict()
        first = self.change["payload"]["cards"][0]["row"]
        changes = SyncChanges(self.path, self.server)
        with patch.object(self.server, "card_conflict", return_value=detail, create=True):
            fetched = changes.card_conflict(problem)
        prepared = changes.prepare_deletion_resolution(self.collection, problem, fetched)
        self.assertIsNone(prepared["local"])
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        changes.resolve_deletion(self.collection, prepared)
        self.collection.undo()
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        self.server.lose_batch_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.assertEqual(0, self.server.acknowledged)
        with patch.object(self.collection, "remove_notes", side_effect=AssertionError("Repeated deletion")):
            self.delivery.apply(self.collection)
            self.delivery.acknowledge()
        self.assertTrue(self.exists())
        with SyncState(self.path) as state:
            self.assertEqual(original, state.batch(problem["request_key"]))
            self.assertEqual("deleted", state.checkpoint_digest("source_card", first["id"]))
        uploads = [payload for method, _, payload in self.server.requests if method == "batch"]
        self.assertEqual(3, len(uploads))
        self.assertEqual(uploads[1], uploads[2])
        choice = uploads[1]["cards"][0]
        self.assertEqual(81, choice["conflict_id"])
        self.assertEqual("current-note", choice["note_base"])
        self.assertEqual("current-card", choice["base"])

    def test_native_undo_while_choosing_requires_reopening_the_source_deletion_comparison(self):
        problem, detail = self.returned_deletion_conflict()
        changes = SyncChanges(self.path, self.server)
        prepared = changes.prepare_deletion_resolution(self.collection, problem, detail)
        self.collection.undo()
        with self.assertRaisesRegex(SnapshotConflict, "changed while this chooser"):
            changes.resolve_deletion(self.collection, prepared)
        reopened = changes.prepare_deletion_resolution(self.collection, problem, detail)
        self.assertIsNotNone(reopened["local"])
        changes.resolve_deletion(self.collection, reopened)
        self.assertTrue(self.exists())

    def test_failed_deletion_choice_storage_preserves_the_original_receipt_without_orphan_batches(self):
        problem, detail = self.returned_deletion_conflict()
        changes = SyncChanges(self.path, self.server)
        prepared = changes.prepare_deletion_resolution(self.collection, problem, detail)
        with SyncState(self.path) as state:
            keys = state.deletion_return_keys()
            original = state.batch(problem["request_key"])
            state.db.execute("CREATE TRIGGER reject_deletion_choice BEFORE INSERT ON batch_resolutions BEGIN SELECT RAISE(ABORT, 'choice unavailable'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "choice unavailable"):
            changes.resolve_deletion(self.collection, prepared)
        with SyncState(self.path) as state:
            self.assertEqual(keys, state.deletion_return_keys())
            self.assertEqual([], state.pending())
            self.assertEqual(original, state.batch(problem["request_key"]))
            state.db.execute("DROP TRIGGER reject_deletion_choice")
        changes.resolve_deletion(self.collection, prepared)
        self.delivery.acknowledge()

    def test_deletion_choice_refuses_changed_native_identity_and_wrong_batch_owner(self):
        problem, detail = self.returned_deletion_conflict()
        changes = SyncChanges(self.path, self.server)
        with self.assertRaisesRegex(SnapshotConflict, "no longer waiting"):
            changes.prepare_deletion_resolution(self.collection, {**problem, "direction": "outgoing"}, detail)
        self.collection.undo()
        note = self.collection.get_note(self.note.id)
        note.guid = "new-guid"
        self.collection.update_note(note)
        with self.assertRaisesRegex(SnapshotConflict, "Anki identity changed"):
            changes.prepare_deletion_resolution(self.collection, problem, detail)
        with SyncState(self.path) as state:
            self.assertIsNone(state.replacement_for(problem["request_key"], "cards", problem["id"]))

    def test_lost_network_acknowledgement_does_not_delete_an_undone_note_again(self):
        self.apply()
        self.server.lose_ack_response = True
        with self.assertRaises(ConnectionError):
            self.delivery.acknowledge()
        self.collection.undo()
        self.delivery = SyncDelivery(self.path, self.server, profile_id=123, key="epoch")
        self.delivery.apply(self.collection)
        self.delivery.acknowledge()
        self.assertTrue(self.exists())
        self.assertEqual(1, self.server.acknowledged)

    def test_a_card_flag_edit_refuses_deletion(self):
        card = self.note.cards()[0]
        card.flags = 3
        self.collection.update_card(card)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("sibling card changed", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())

    def test_a_tag_edit_refuses_deletion(self):
        self.note.tags = ["keep"]
        self.collection.update_note(self.note)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("fields or tags changed", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())

    def test_source_note_metadata_changes_refuse_deletion(self):
        self.collection.db.execute("UPDATE notes SET data = ? WHERE id = ?", "new metadata", self.note.id)
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("note metadata changed", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())
        self.assertEqual("new metadata", self.collection.db.scalar("SELECT data FROM notes WHERE id = ?", self.note.id))

    def test_pending_outgoing_changes_refuse_deletion(self):
        with SyncState(self.path) as state:
            state.put("changes", {"keys": ["pending"]})
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("saved outgoing sync", failure.exception.conflicts[0]["code"])
        self.assertTrue(self.exists())

    def test_later_note_uploads_do_not_silently_consume_a_deletion_request(self):
        with SyncState(self.path) as state:
            batch = state.enqueue(key="epoch", notes=[{"id": self.note.id}])
            state.remember_receipt(batch["request_key"], {"cursor": 2, "notes": [{"id": self.note.id, "status": "accepted"}]})
            state.complete_entity(batch["request_key"], "notes", self.note.id, [], cursor=2)
        self.apply()
        self.assertFalse(self.exists())

    def test_failure_to_save_the_attempt_cannot_delete_the_note(self):
        self.delivery.receive()
        with patch.object(SyncState, "prepare_note_deletion", side_effect=OSError("Sidecar unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertTrue(self.exists())

    def test_failure_before_the_native_call_can_retry_without_a_native_commit_receipt(self):
        self.delivery.receive()
        with patch.object(self.collection, "remove_notes", side_effect=OSError("Collection unavailable")):
            with self.assertRaises(OSError):
                self.delivery.apply(self.collection)
        self.assertTrue(self.exists())
        self.delivery.apply(self.collection)
        self.assertFalse(self.exists())

    def test_receipt_failure_rolls_back_deletion_and_can_retry(self):
        set_config = self.collection.set_config
        def interrupt(key, value, **kwargs):
            result = set_config(key, value, **kwargs)
            if key == DELETION_RECEIPT and value is not None:
                raise OSError("receipt unavailable")
            return result
        with patch.object(self.collection, "set_config", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "receipt unavailable"):
                self.apply()
        path = self.collection.path
        self.collection.close()
        self.collection = Collection(path)
        self.assertTrue(self.exists())
        self.assertEqual(2, len(self.collection.get_note(self.note.id).cards()))
        self.assertIsNone(self.collection.get_config(DELETION_RECEIPT))
        self.apply()
        self.assertFalse(self.exists())

    def test_receipt_cleanup_failure_preserves_undo_and_does_not_repeat_deletion(self):
        set_config = self.collection.set_config
        def interrupt(key, value, **kwargs):
            if key == DELETION_RECEIPT and value is None:
                raise OSError("cleanup unavailable")
            return set_config(key, value, **kwargs)
        with patch.object(self.collection, "set_config", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "cleanup unavailable"):
                self.apply()
        self.assertFalse(self.exists())
        self.assertIsNotNone(self.collection.get_config(DELETION_RECEIPT))
        self.collection.undo()
        with patch.object(self.collection, "remove_notes", side_effect=AssertionError("Repeated deletion")):
            self.apply()
        self.assertTrue(self.exists())
        self.assertIsNone(self.collection.get_config(DELETION_RECEIPT))
        self.delivery.acknowledge()
        self.assertEqual(1, self.server.acknowledged)

    def test_foreign_deletion_receipt_cannot_be_consumed_or_overwritten(self):
        self.collection.set_config(DELETION_RECEIPT, "other-deletion")
        with self.assertRaises(DeliveryConflict):
            self.apply()
        self.assertTrue(self.exists())
        self.assertEqual("other-deletion", self.collection.get_config(DELETION_RECEIPT))

    def test_retry_after_rollback_rechecks_later_edits(self):
        with patch.object(self.collection, "remove_notes", side_effect=OSError("Collection unavailable")):
            with self.assertRaises(OSError):
                self.apply()
        self.note.fields[0] = "later edit"
        self.collection.update_note(self.note)
        with self.assertRaises(DeliveryConflict):
            self.apply()
        self.assertEqual("later edit", self.collection.get_note(self.note.id).fields[0])

    def test_an_already_missing_note_is_not_assumed_to_be_our_deletion(self):
        self.collection.remove_notes([self.note.id])
        with self.assertRaises(DeliveryConflict) as failure:
            self.apply()
        self.assertIn("disappeared before deletion", failure.exception.conflicts[0]["code"])
        self.assertEqual(0, self.server.acknowledged)

    def test_deletion_keeps_review_history_and_never_calls_the_network(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        entries = []
        for row in SyncSnapshot._rows(self.collection, "cards", "nid = ?", (self.note.id,), with_last_event=True):
            last_event = row.pop("last_event_id")
            entries.append({"row": row, "last_event_id": last_event})
        self.change["payload"]["cards"] = entries
        history = self.collection.db.all("SELECT * FROM revlog ORDER BY id")
        self.assertTrue(history)
        self.delivery.receive()
        with patch.object(self.server, "batch", side_effect=AssertionError("Network during deletion")), \
                patch.object(self.server, "acknowledge", side_effect=AssertionError("Network during deletion")):
            self.delivery.apply(self.collection)
        self.assertFalse(self.exists())
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog ORDER BY id"))
