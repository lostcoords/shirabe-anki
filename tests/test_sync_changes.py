from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_snapshot import SyncSnapshot, SnapshotConflict
from shirabe_addon.sync_state import SyncState
from shirabe_addon.sync_delivery import SyncDelivery
from test_sync_delivery import Server
from test_sync_snapshot import Collection, version, SnapshotServer


class ChangesServer(SnapshotServer):
    def __init__(self, collection):
        super().__init__()
        self.collection = collection
        self.remote_back = None
        self.suspend = False
        self.hidden = False

    def batch(self, profile_id, payload):
        receipt = super().batch(profile_id, payload)
        for item, result in zip(payload.get("note_types", []), receipt["note_types"]):
            result["revision"] = item.get("base_revision", -1) + 1
        for item, result in zip(payload.get("notes", []), receipt["notes"]):
            result["note"]["field_names"] = [field["name"] for field in self.collection.models.get(item["mid"])["flds"]]
            if self.remote_back:
                result["note"]["fields"][1] = self.remote_back
        for item, result in zip(payload.get("cards", []), receipt["cards"]):
            row = item["row"]
            result["card"]["deleted"] = self.hidden
            note = self.collection.get_note(row["nid"])
            result["anki"] = {"card_before": row, "card_after": copy.deepcopy(row),
                              "note_guid": note.guid, "anki_type_id": note.mid}
            if self.suspend:
                result["anki"]["card_after"]["queue"] = -1
        for item, result in zip(payload.get("history", []), receipt["history"]):
            if item.get("sync_id"):
                result["sync_id"] = item["sync_id"]
        return receipt


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncChangesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sync.sqlite3"
        self.collection = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        self.server = ChangesServer(self.collection)
        self.deck_id = self.collection.decks.id("Japanese")
        self.note = self.collection.new_note(self.collection.models.by_name("Basic"))
        self.note.fields = ["猫", "cat"]
        self.collection.add_note(self.note, self.deck_id)
        snapshot = SyncSnapshot(self.path, self.server)
        snapshot.capture(self.collection, [self.deck_id])
        snapshot.upload()
        self.changes = SyncChanges(self.path, self.server)
        self.server.batches.clear()

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def payloads(self):
        with SyncState(self.path) as state:
            return [state.batch(key)["payload"] for key in state.get("changes")["keys"]]

    def legacy_type_rename(self):
        with SyncState(self.path) as state:
            checkpoint = state.checkpoint("note_type", self.note.mid)
            for group in ("flds", "tmpls"):
                for item in checkpoint["payload"][group]:
                    item.pop("id", None)
            state.remember_checkpoint("note_type", self.note.mid, checkpoint["digest"], checkpoint["payload"])
        model = self.collection.models.get(self.note.mid)
        self.collection.models.rename_field(model, model["flds"][0], "Expression")
        model["tmpls"][0]["name"] = "Recognition"
        self.collection.models.save(model)
        with self.assertRaises(SyncConflict) as error:
            self.changes.capture(self.collection)
        problem = error.exception.conflicts[0]
        self.assertEqual("mapping_required", problem["status"])
        self.assertEqual("note_types", problem["kind"])
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertEqual([], state.pending())
        self.assertEqual([], self.server.batches)
        return problem

    def test_legacy_rename_can_be_mapped_explicitly_and_survives_reopening(self):
        problem = self.legacy_type_rename()
        prepared = self.changes.prepare_type_mapping(self.collection, problem)
        maps = {"field_map": [0, 1], "template_map": [0]}
        before = self.collection.get_note(self.note.id).fields
        self.changes.resolve_type_mapping(self.collection, prepared, maps)
        self.assertEqual(before, self.collection.get_note(self.note.id).fields)
        self.changes = SyncChanges(self.path, self.server)
        self.changes.capture(self.collection)
        entry = next(batch["note_types"][0] for batch in self.payloads() if "note_types" in batch)
        self.assertEqual([0, 1], entry["field_map"])
        self.assertEqual([0], entry["template_map"])
        self.assertEqual(0, entry["base_revision"])
        self.changes.upload()
        self.changes.apply(self.collection)
        self.changes.capture(self.collection)
        self.assertEqual([], self.payloads())

    def test_mapping_confirmation_refuses_changed_definitions_and_invalid_choices(self):
        problem = self.legacy_type_rename()
        prepared = self.changes.prepare_type_mapping(self.collection, problem)
        for mapping in ([0, 0], [0], [0, 2], [True, 1], ["0", 1]):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(SnapshotConflict, "mapping"):
                self.changes.resolve_type_mapping(self.collection, prepared, {"field_map": mapping, "template_map": [0]})
        model = self.collection.models.get(self.note.mid)
        model["css"] += "\n.card { color: red; }"
        self.collection.models.save(model)
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.resolve_type_mapping(self.collection, prepared, {"field_map": [0, 1], "template_map": [0]})
        with SyncState(self.path) as state:
            self.assertIsNone(state.checkpoint("type_mapping", self.note.mid))

    def test_saved_mapping_is_not_reused_for_a_later_definition(self):
        problem = self.legacy_type_rename()
        prepared = self.changes.prepare_type_mapping(self.collection, problem)
        self.changes.resolve_type_mapping(self.collection, prepared, {"field_map": [0, 1], "template_map": [0]})
        model = self.collection.models.get(self.note.mid)
        self.collection.models.rename_field(model, model["flds"][0], "Word")
        self.collection.models.save(model)
        with self.assertRaises(SyncConflict) as error:
            self.changes.capture(self.collection)
        self.assertEqual("mapping_required", error.exception.conflicts[0]["status"])

    def test_explicit_maps_cannot_override_stable_identity(self):
        previous = [{"id": 10, "name": "Front"}, {"id": 20, "name": "Back"}]
        current = [{"id": 20, "name": "Answer"}, {"id": 10, "name": "Prompt"}]
        for mapping in ([0, 1], [None, 0]):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(SnapshotConflict, "identity"):
                self.changes.validate_mapping(previous, current, mapping)
        self.changes.validate_mapping(previous, current, [1, 0])

    def test_rejected_type_can_map_current_data_without_replacing_the_original_receipt(self):
        problem = self.legacy_type_rename()
        prepared = self.changes.prepare_type_mapping(self.collection, problem)
        self.changes.resolve_type_mapping(self.collection, prepared, {"field_map": [0, 1], "template_map": [0]})
        self.changes.capture(self.collection)
        send = self.server.batch

        def reject(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["note_types"] = [{"id": row["definition"]["id"], "status": "rejected", "code": "invalid_definition"}
                                     for row in payload.get("note_types", [])]
            return receipt

        with patch.object(self.server, "batch", reject):
            self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        problem = next(item for item in error.exception.conflicts if item["kind"] == "note_types")
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        model = self.collection.models.get(self.note.mid)
        self.collection.models.rename_field(model, model["flds"][0], "Word")
        self.collection.models.save(model)
        retry = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertTrue(retry["mapping_required"])
        prepared = self.changes.prepare_type_mapping(self.collection, problem)
        replacement = self.changes.resolve_type_mapping(self.collection, prepared, {"field_map": [0, 1], "template_map": [0]})
        with SyncState(self.path) as state:
            entry = state.batch(replacement)["payload"]["note_types"][0]
            self.assertEqual([0, 1], entry["field_map"])
            self.assertEqual("Word", entry["definition"]["flds"][0]["name"])
            self.assertEqual(original, state.batch(problem["request_key"]))
        self.changes.upload()
        self.changes.apply(self.collection)

    def test_choices_for_multiple_legacy_types_accumulate_before_capture_succeeds(self):
        extra = self.collection.new_note(self.collection.models.by_name("Basic (and reversed card)"))
        extra.fields = ["犬", "dog"]
        self.collection.add_note(extra, self.deck_id)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        for note in (self.note, extra):
            with SyncState(self.path) as state:
                checkpoint = state.checkpoint("note_type", note.mid)
                for item in checkpoint["payload"]["flds"]:
                    item.pop("id", None)
                state.remember_checkpoint("note_type", note.mid, checkpoint["digest"], checkpoint["payload"])
            model = self.collection.models.get(note.mid)
            self.collection.models.rename_field(model, model["flds"][0], "Expression")
            self.collection.models.save(model)
        resolved = []
        for _ in range(2):
            with self.assertRaises(SyncConflict) as error:
                self.changes.capture(self.collection)
            problem = error.exception.conflicts[0]
            self.assertNotIn(problem["id"], resolved)
            prepared = self.changes.prepare_type_mapping(self.collection, problem)
            templates = list(range(len(prepared["current"]["tmpls"])))
            self.changes.resolve_type_mapping(self.collection, prepared, {"field_map": [0, 1], "template_map": templates})
            resolved.append(problem["id"])
            self.changes = SyncChanges(self.path, self.server)
        self.changes.capture(self.collection)
        self.assertEqual(2, sum(len(batch.get("note_types", [])) for batch in self.payloads()))

    def test_unchanged_collection_sends_nothing(self):
        self.changes.capture(self.collection)
        self.assertEqual([], self.payloads())
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual([], self.server.batches)
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))

    def test_equivalent_receipt_stamps_and_json_do_not_keep_a_sync_cycle_running(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        self.changes.capture(self.collection)
        send = self.server.batch

        def normalize(profile_id, payload):
            result = send(profile_id, payload)
            for item in result["cards"]:
                row = item["anki"]["card_after"]
                row["mod"] += 60
                row["data"] = json.dumps(json.loads(row["data"]), indent=2)
            return result

        with patch.object(self.server, "batch", normalize):
            self.changes.upload()
        self.changes.apply(self.collection)
        self.changes.capture(self.collection)
        self.assertEqual([], self.payloads())

    def test_a_changed_receipt_reports_the_written_native_state_once_then_stops(self):
        card = self.note.cards()[0]
        card.flags = 1
        self.collection.update_card(card)
        self.changes.capture(self.collection)
        self.server.suspend = True
        self.changes.upload()
        self.changes.apply(self.collection)
        self.changes.capture(self.collection)
        rows = [entry["row"] for batch in self.payloads() for entry in batch.get("cards", [])]
        self.assertEqual(list(SyncSnapshot._rows(self.collection, "cards", "id = ?", (card.id,))), rows)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.changes.capture(self.collection)
        self.assertEqual([], self.payloads())

    def test_receipt_checkpoint_does_not_hide_an_independent_native_flag_edit(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        self.changes.capture(self.collection)
        self.changes.upload()
        card = self.collection.get_card(card.id)
        card.flags = 3
        self.collection.update_card(card)
        self.changes.apply(self.collection)
        self.changes.capture(self.collection)
        entry = next(batch["cards"][0] for batch in self.payloads() if "cards" in batch)
        self.assertEqual(3, entry["row"]["flags"])

    def test_card_receipt_only_covers_its_applied_cursor_and_rejects_invalid_bounds(self):
        card = self.note.cards()[0]
        card.flags = 1
        self.collection.update_card(card)
        self.changes.capture(self.collection)
        payload = next(item for item in self.payloads() if "cards" in item)
        send, saved = self.server.batch, []

        def receipt(profile_id, payload):
            result = send(profile_id, payload)
            result["cursor"] = 10
            for item in result["cards"]:
                item["applied_cursor"] = 2
            saved.append(copy.deepcopy(result))
            return result

        with patch.object(self.server, "batch", receipt):
            self.changes.upload()
        self.changes.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual(2, state.accepted_cursor("cards", card.id))
        for value in (-1, 11, True, "2"):
            with self.subTest(cursor=value):
                invalid = copy.deepcopy(saved[0])
                invalid["cards"][0]["applied_cursor"] = value
                with self.assertRaisesRegex(SnapshotConflict, "applied entity cursor"):
                    list(self.changes._entries(payload, invalid))

    def test_native_undo_sends_the_removed_event_with_the_restored_card(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = next(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,)))
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.collection.undo()
        self.changes.capture(self.collection)
        entry = next(batch["cards"][0] for batch in self.payloads() if "cards" in batch)
        self.assertEqual([history], entry["removed_history"])
        self.assertEqual(0, entry["row"]["reps"])
        self.assertNotIn("last_review_id", entry)

    def test_undo_receipt_must_accept_the_exact_removed_history_before_checkpointing(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.collection.undo()
        self.changes.capture(self.collection)
        with self.assertRaisesRegex(SnapshotConflict, "removed history"):
            self.changes.upload()

    def undo_conflict(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        reviewed = next(SyncSnapshot._rows(self.collection, "cards", "id = ?", (card.id,)))
        history = next(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,)))
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.collection.undo()
        self.changes.capture(self.collection)
        send = self.server.batch

        def conflict(profile_id, payload):
            receipt = send(profile_id, payload)
            for result in receipt["cards"]:
                result.update(status="conflict", code="history_changed_before_undo", conflict_id=456)
            return receipt

        with patch.object(self.server, "batch", conflict):
            self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        with SyncState(self.path) as state:
            key = state.get("changes")["binding"]["key"]
        detail = {"id": 456, "key": key, "resolved": False, "snapshot": "current-card", "code": "history_changed_before_undo",
                  "card": {"anki_id": card.id}, "history_removal": {"rows": [history]},
                  "anki": {"card_after": reviewed, "note_guid": self.note.guid, "anki_type_id": self.note.mid}}
        return error.exception.conflicts[0], detail

    def test_undo_resolution_keeps_the_exact_removed_events_and_native_comparison_base(self):
        problem, detail = self.undo_conflict()
        prepared = self.changes.prepare_history_removal(self.collection, problem, detail)
        replacement = self.changes.resolve_history_removal(self.collection, prepared, schedule="shirabe")
        with SyncState(self.path) as state:
            entry = state.batch(replacement)["payload"]["cards"][0]
            self.assertEqual(detail["history_removal"]["rows"], entry["removed_history"])
            self.assertEqual(detail["anki"]["card_after"], entry["row"])
            self.assertEqual(detail["snapshot"], entry["base"])
            self.assertEqual(prepared["local"], state.resolution_base(replacement)["card"])
            self.assertFalse(state.entity_applied(problem["request_key"], "cards", problem["id"]))

    def test_undo_resolution_refuses_a_native_review_made_after_the_dialog_opened(self):
        problem, detail = self.undo_conflict()
        prepared = self.changes.prepare_history_removal(self.collection, problem, detail)
        card = self.collection.get_card(problem["id"])
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        with self.assertRaises(SnapshotConflict):
            self.changes.resolve_history_removal(self.collection, prepared, schedule="anki")
        with SyncState(self.path) as state:
            self.assertIsNone(state.replacement_for(problem["request_key"], "cards", problem["id"]))

    def rejected_note(self):
        self.note.fields[1] = "saved rejected edit"
        self.collection.update_note(self.note)
        self.changes.capture(self.collection)
        send = self.server.batch

        def reject(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["notes"] = [{"id": row["id"], "status": "rejected", "code": "invalid_fields"} for row in payload.get("notes", [])]
            return receipt

        with patch.object(self.server, "batch", reject):
            self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        return error.exception.conflicts[0]

    def test_rejected_retry_keeps_its_receipt_and_settles_only_after_acceptance(self):
        problem = self.rejected_note()
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        replacement = self.changes.retry_rejected(self.collection, prepared)
        with SyncState(self.path) as state:
            self.assertEqual(original, state.batch(problem["request_key"]))
            self.assertFalse(state.entity_applied(problem["request_key"], "notes", self.note.id))
            self.assertEqual(original["payload"]["notes"], state.batch(replacement)["payload"]["notes"])
        self.changes = SyncChanges(self.path, self.server)
        self.changes.upload()
        self.changes.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual(original["receipt"], state.batch(problem["request_key"])["receipt"])
            self.assertTrue(state.entity_applied(problem["request_key"], "notes", self.note.id))
            self.assertIsNone(state.get("changes"))

    def test_rejected_retry_refuses_a_changed_identity_without_staging_anything(self):
        problem = self.rejected_note()
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.note.guid = "different-native-note"
        self.collection.update_note(self.note)
        with SyncState(self.path) as state:
            before = state.get("changes")
        with self.assertRaisesRegex(SnapshotConflict, "identity"):
            self.changes.retry_rejected(self.collection, prepared)
        with SyncState(self.path) as state:
            self.assertEqual(before, state.get("changes"))
            self.assertIsNone(state.replacement_for(problem["request_key"], "notes", self.note.id))

    def test_rejected_retry_refuses_source_edits_while_its_confirmation_is_open(self):
        problem = self.rejected_note()
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.note.fields[1] = "changed while confirming"
        self.collection.update_note(self.note)
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.retry_rejected(self.collection, prepared)
        with SyncState(self.path) as state:
            self.assertIsNone(state.replacement_for(problem["request_key"], "notes", self.note.id))

    def test_rejected_retry_preserves_newer_native_fields_for_the_next_capture(self):
        problem = self.rejected_note()
        self.note.fields[0] = "newer Anki edit"
        self.collection.update_note(self.note)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.changes.retry_rejected(self.collection, prepared)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual("newer Anki edit", self.collection.get_note(self.note.id).fields[0])
        self.changes.capture(self.collection)
        self.assertEqual("newer Anki edit", self.payloads()[0]["notes"][0]["flds"].split("\x1f")[0])

    def test_current_note_retry_keeps_the_original_base_and_sends_corrected_fields(self):
        problem = self.rejected_note()
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        self.note.fields[1] = "corrected Anki meaning"
        self.collection.update_note(self.note)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        replacement = self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            row = state.batch(replacement)["payload"]["notes"][0]
            self.assertEqual(original["payload"]["notes"][0]["base"], row["base"])
            self.assertEqual("猫\x1fcorrected Anki meaning", row["flds"])
            self.assertEqual(original, state.batch(problem["request_key"]))
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual("corrected Anki meaning", self.collection.get_note(self.note.id).fields[1])

    def test_current_note_retry_requires_its_current_field_layout_to_be_synced(self):
        problem = self.rejected_note()
        model = self.collection.get_note(self.note.id).note_type()
        self.collection.models.add_field(model, self.collection.models.new_field("Extra"))
        self.collection.models.save(model)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "note type"):
            self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            self.assertIsNone(state.replacement_for(problem["request_key"], "notes", self.note.id))

    def test_current_note_retry_does_not_overwrite_an_edit_made_during_upload(self):
        problem = self.rejected_note()
        self.note.fields[1] = "corrected value"
        self.collection.update_note(self.note)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.changes.retry_rejected(self.collection, prepared, source="current")
        self.note.fields[1] = "later Anki edit"
        self.collection.update_note(self.note)
        self.server.remote_back = "competing server edit"
        self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        self.assertEqual("local_conflict", error.exception.conflicts[0]["status"])
        self.assertEqual("later Anki edit", self.collection.get_note(self.note.id).fields[1])
        with SyncState(self.path) as state:
            self.assertFalse(state.entity_applied(problem["request_key"], "notes", self.note.id))

    def test_current_note_retry_refuses_an_unsynced_reorder_with_the_same_field_count(self):
        problem = self.rejected_note()
        model = self.collection.get_note(self.note.id).note_type()
        self.collection.models.reposition_field(model, model["flds"][0], 1)
        self.collection.models.save(model)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "note type"):
            self.changes.retry_rejected(self.collection, prepared, source="current")

    def test_current_type_retry_rebuilds_maps_without_changing_its_comparison_revision(self):
        model = self.collection.get_note(self.note.id).note_type()
        model["css"] = ".card { color: red; }"
        self.collection.models.save(model)
        self.changes.capture(self.collection)
        send = self.server.batch

        def reject(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["note_types"] = [{"id": row["definition"]["id"], "status": "rejected", "code": "invalid_definition"} for row in payload.get("note_types", [])]
            return receipt

        with patch.object(self.server, "batch", reject):
            self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        problem = error.exception.conflicts[0]
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        model = self.collection.get_note(self.note.id).note_type()
        self.collection.models.add_field(model, self.collection.models.new_field("Extra"))
        model["css"] = ".card { color: blue; }"
        self.collection.models.save(model)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        replacement = self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            row = state.batch(replacement)["payload"]["note_types"][0]
            self.assertEqual(original["payload"]["note_types"][0]["base_revision"], row["base_revision"])
            self.assertEqual([0, 1, None], row["field_map"])
            self.assertEqual(".card { color: blue; }", row["definition"]["css"])
            self.assertEqual(original, state.batch(problem["request_key"]))
        self.changes.upload()
        self.changes.apply(self.collection)

    def test_rejected_retry_does_not_reclassify_an_accepted_receipt(self):
        self.note.fields[0] = "accepted edit"
        self.collection.update_note(self.note)
        self.changes.capture(self.collection)
        self.changes.upload()
        with SyncState(self.path) as state:
            key = state.get("changes")["keys"][0]
        problem = {"request_key": key, "kind": "notes", "id": self.note.id, "status": "rejected"}
        with self.assertRaisesRegex(SnapshotConflict, "Only a rejected"):
            self.changes.prepare_rejected_retry(self.collection, problem)

    def test_rejected_history_retry_refuses_a_native_undo(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.changes.capture(self.collection)
        send = self.server.batch

        def reject(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["history"] = [{"id": entry["row"]["id"], "status": "rejected", "code": "invalid_history"} for entry in payload.get("history", [])]
            return receipt

        with patch.object(self.server, "batch", reject):
            self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        problem = error.exception.conflicts[0]
        self.assertEqual("history", problem["kind"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.collection.undo()
        with self.assertRaisesRegex(SnapshotConflict, "history changed"):
            self.changes.retry_rejected(self.collection, prepared)
        with SyncState(self.path) as state:
            self.assertIsNone(state.replacement_for(problem["request_key"], "history", problem["id"]))

    def rejected_history(self, *, sync_id=None):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        if sync_id:
            row = next(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,)))
            with SyncState(self.path) as state:
                state.prepare_review(sync_id, {"rating": 3}, row)
                state.finish_review(sync_id)
        self.changes.capture(self.collection)
        send = self.server.batch

        def reject(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["history"] = [{"id": entry["row"]["id"], "status": "rejected", "code": "invalid_history"} for entry in payload.get("history", [])]
            return receipt

        with patch.object(self.server, "batch", reject):
            self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        return next(problem for problem in error.exception.conflicts if problem["kind"] == "history")

    def test_current_history_retry_sends_corrected_content_without_replacing_receipts(self):
        problem = self.rejected_history()
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        self.collection.db.execute("UPDATE revlog SET time = 1234 WHERE id = ?", problem["id"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        replacement = self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            self.assertEqual(1234, state.batch(replacement)["payload"]["history"][0]["row"]["time"])
            self.assertEqual(original, state.batch(problem["request_key"]))
        self.changes.upload()
        self.changes.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertEqual(original["receipt"], state.batch(problem["request_key"])["receipt"])
            self.assertTrue(state.entity_applied(problem["request_key"], "history", problem["id"]))
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        self.assertEqual(1234, self.collection.db.scalar("SELECT time FROM revlog WHERE id = ?", problem["id"]))

    def test_saved_history_retry_refuses_content_that_was_corrected_in_anki(self):
        problem = self.rejected_history()
        self.collection.db.execute("UPDATE revlog SET time = 1234 WHERE id = ?", problem["id"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertIsNotNone(prepared["current_entry"])
        with self.assertRaisesRegex(SnapshotConflict, "Choose current Anki data"):
            self.changes.retry_rejected(self.collection, prepared)

    def test_current_history_retry_refuses_replacing_a_previously_accepted_event(self):
        problem = self.rejected_history()
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])["payload"]["history"][0]["row"]
            state.remember_checkpoint("source_history", problem["id"], "accepted", original)
        self.collection.db.execute("UPDATE revlog SET time = 1234 WHERE id = ?", problem["id"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "already synced"):
            self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            self.assertIsNone(state.replacement_for(problem["request_key"], "history", problem["id"]))

    def test_current_history_retry_refuses_reassigned_card_identity(self):
        problem = self.rejected_history()
        self.collection.db.execute("UPDATE revlog SET cid = cid + 1 WHERE id = ?", problem["id"])
        with self.assertRaisesRegex(SnapshotConflict, "history changed"):
            self.changes.prepare_rejected_retry(self.collection, problem)

    def test_current_history_retry_refuses_a_reused_card_with_a_different_note(self):
        problem = self.rejected_history()
        other = self.collection.new_note(self.collection.models.by_name("Basic"))
        other.fields = ["犬", "dog"]
        self.collection.add_note(other, self.deck_id)
        self.collection.db.execute("UPDATE cards SET nid = ? WHERE id = ?", other.id, self.note.cards()[0].id)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "card identity"):
            self.changes.retry_rejected(self.collection, prepared, source="current")

    def test_current_history_retry_preserves_returned_shirabe_identity(self):
        problem = self.rejected_history(sync_id="shirabe-review-identity")
        self.collection.db.execute("UPDATE revlog SET usn = 7 WHERE id = ?", problem["id"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        replacement = self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            self.assertEqual("shirabe-review-identity", state.batch(replacement)["payload"]["history"][0]["sync_id"])
        self.changes.upload()
        self.changes.apply(self.collection)

    def test_current_history_retry_refuses_changed_content_of_a_delivered_shirabe_review(self):
        problem = self.rejected_history(sync_id="delivered-review")
        self.collection.db.execute("UPDATE revlog SET time = 1234 WHERE id = ?", problem["id"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "already synced"):
            self.changes.retry_rejected(self.collection, prepared, source="current")

    def test_current_history_retry_rechecks_content_before_confirmation(self):
        problem = self.rejected_history()
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.collection.db.execute("UPDATE revlog SET time = 1234 WHERE id = ?", problem["id"])
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.retry_rejected(self.collection, prepared, source="current")

    def rejected_card(self):
        card = self.note.cards()[0]
        card.flags = 2
        self.collection.update_card(card)
        self.changes.capture(self.collection)
        send = self.server.batch

        def reject(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["cards"] = [{"id": entry["row"]["id"], "status": "rejected", "code": "invalid_card"} for entry in payload.get("cards", [])]
            return receipt

        with patch.object(self.server, "batch", reject):
            self.changes.upload()
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        return error.exception.conflicts[0]

    def test_current_card_retry_stages_new_reviews_before_card_and_preserves_base(self):
        problem = self.rejected_card()
        card = self.collection.get_card(problem["id"])
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = self.collection.db.all("SELECT * FROM revlog")
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        replacement = self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            keys = state.get("changes")["keys"]
            dependency = state.batch(keys[-2])["payload"]["history"][0]
            entry = state.batch(replacement)["payload"]["cards"][0]
            self.assertEqual(keys[-1], replacement)
            self.assertEqual(original["payload"]["cards"][0]["base"], entry["base"])
            self.assertEqual(dependency["row"]["id"], entry["last_review_id"])
            self.assertEqual(entry["last_review_id"], entry["last_event_id"])
            self.assertEqual(1, entry["row"]["reps"])
            self.assertEqual(original, state.batch(problem["request_key"]))
        self.changes = SyncChanges(self.path, self.server)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        self.assertEqual(1, self.collection.get_card(card.id).reps)
        with SyncState(self.path) as state:
            self.assertTrue(state.entity_applied(problem["request_key"], "cards", card.id))
            self.assertIsNone(state.get("changes"))

    def rejected_redo(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = next(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,)))
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.collection.undo()
        with SyncState(self.path) as state:
            state.remember_checkpoint("source_history", history["id"], "deleted", history)
            state.remember_checkpoint("history_removal", history["id"], "9", history)
        self.collection.redo()
        return self.rejected_card(), history

    def test_current_card_retry_keeps_redo_in_the_card_transaction_without_history_dependencies(self):
        problem, history = self.rejected_redo()
        self.collection.db.execute("UPDATE revlog SET usn = 5 WHERE id = ?", history["id"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertIsNotNone(prepared["current_entry"], prepared["current_error"])
        self.assertEqual([], prepared["dependencies"])
        expected = [{"row": {**history, "usn": 5}, "removal_id": 9}]
        self.assertEqual(expected, prepared["current_entry"]["restored_history"])
        replacement = self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            self.assertEqual(expected, state.batch(replacement)["payload"]["cards"][0]["restored_history"])

    def test_current_card_retry_refuses_redo_history_changed_after_capture(self):
        problem, history = self.rejected_redo()
        self.collection.db.execute("UPDATE revlog SET time = time + 1 WHERE id = ?", history["id"])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertIsNone(prepared["current_entry"])
        self.assertIn("restoration", prepared["current_error"])

    def test_current_card_retry_rechecks_review_history_before_confirmation(self):
        problem = self.rejected_card()
        card = self.collection.get_card(problem["id"])
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertIsNotNone(prepared["current_entry"])
        self.collection.db.execute("UPDATE revlog SET time = time + 1 WHERE cid = ?", card.id)
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            self.assertIsNone(state.replacement_for(problem["request_key"], "cards", card.id))

    def test_current_card_retry_refuses_undo_of_already_synced_history(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        problem = self.rejected_card()
        self.collection.undo()
        self.collection.undo()
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id))
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "history changed or was undone"):
            self.changes.retry_rejected(self.collection, prepared, source="current")

    def test_current_card_retry_does_not_resend_accepted_history(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        problem = self.rejected_card()
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertEqual([], prepared["dependencies"])
        self.changes.retry_rejected(self.collection, prepared, source="current")
        self.server.batches.clear()
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(1, len(self.server.batches))
        self.assertNotIn("history", self.server.batches[0])

    def test_current_card_retry_accepts_history_sync_metadata_changes(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        problem = self.rejected_card()
        self.collection.db.execute("UPDATE revlog SET usn = 5 WHERE cid = ?", card.id)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertIsNotNone(prepared["current_entry"], prepared["current_error"])
        self.assertEqual(5, prepared["dependencies"][0]["history"][0]["row"]["usn"])
        self.changes.retry_rejected(self.collection, prepared, source="current")
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_current_card_retry_refuses_edits_to_synced_review_content(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        problem = self.rejected_card()
        self.collection.db.execute("UPDATE revlog SET time = time + 1 WHERE cid = ?", card.id)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "history changed or was undone"):
            self.changes.retry_rejected(self.collection, prepared, source="current")

    def test_current_card_retry_preserves_an_anki_review_made_during_upload(self):
        problem = self.rejected_card()
        card = self.collection.get_card(problem["id"])
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.changes.retry_rejected(self.collection, prepared, source="current")
        card = self.collection.get_card(card.id)
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = self.collection.db.all("SELECT * FROM revlog")
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        self.assertEqual(2, self.collection.get_card(card.id).reps)
        self.changes.capture(self.collection)
        payloads = self.payloads()
        self.assertEqual(1, sum(len(payload.get("history", [])) for payload in payloads))
        entry = next(payload["cards"][0] for payload in payloads if "cards" in payload)
        self.assertEqual(2, entry["row"]["reps"])

    def test_current_card_retry_staging_rolls_back_history_if_card_cannot_be_saved(self):
        problem = self.rejected_card()
        card = self.collection.get_card(problem["id"])
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with SyncState(self.path) as state:
            before = state.get("changes")
            count = state.db.execute("SELECT count(*) FROM batches").fetchone()[0]
        enqueue = SyncState._enqueue

        def interrupted(state, key, entities):
            if "cards" in entities:
                raise OSError("Disk full while staging card")
            return enqueue(state, key, entities)

        with patch.object(SyncState, "_enqueue", interrupted), self.assertRaisesRegex(OSError, "Disk full"):
            self.changes.retry_rejected(self.collection, prepared, source="current")
        with SyncState(self.path) as state:
            self.assertEqual(before, state.get("changes"))
            self.assertEqual(count, state.db.execute("SELECT count(*) FROM batches").fetchone()[0])
            self.assertIsNone(state.replacement_for(problem["request_key"], "cards", card.id))

    def test_current_card_retry_batches_more_than_two_hundred_new_history_rows(self):
        problem = self.rejected_card()
        card = self.collection.get_card(problem["id"])
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        row = list(self.collection.db.first("SELECT * FROM revlog WHERE cid = ?", card.id))
        for offset in range(1, 202):
            self.collection.db.execute("INSERT INTO revlog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row[0] - offset, *row[1:])
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        self.assertEqual([200, 2], [len(batch["history"]) for batch in prepared["dependencies"]])
        self.changes.retry_rejected(self.collection, prepared, source="current")
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(202, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_current_card_retry_refuses_history_with_an_unsettled_receipt(self):
        problem = self.rejected_card()
        card = self.collection.get_card(problem["id"])
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        row = next(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,)))
        with SyncState(self.path) as state:
            cycle = state.get("changes")
            batch = state.enqueue(key=cycle["binding"]["key"], history=[{"row": row}])
            state.remember_receipt(batch["request_key"], {"history": [{"id": row["id"], "status": "rejected", "code": "invalid_history"}]})
            cycle["keys"].append(batch["request_key"])
            state.put("changes", cycle)
        prepared = self.changes.prepare_rejected_retry(self.collection, problem)
        with self.assertRaisesRegex(SnapshotConflict, "pending review history"):
            self.changes.retry_rejected(self.collection, prepared, source="current")

    def test_unchanged_scan_does_not_decode_full_checkpoint_payloads(self):
        with patch.object(SyncState, "checkpoint", side_effect=AssertionError("Decoded an unchanged checkpoint payload")):
            cycle = self.changes.capture(self.collection)
        self.assertEqual([], cycle["keys"])

    def test_sparse_edits_share_a_batch_even_across_source_read_pages(self):
        notes = []
        for index in range(201):
            note = self.collection.new_note(self.note.note_type())
            note.fields = [f"note {index}", "meaning"]
            self.collection.add_note(note, self.deck_id)
            notes.append(note)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        for note in (notes[0], notes[-1]):
            note.fields[1] = "changed"
            self.collection.update_note(note)
        self.changes.capture(self.collection)
        batches = [batch["notes"] for batch in self.payloads() if "notes" in batch]
        self.assertEqual(1, len(batches))
        self.assertEqual([notes[0].id, notes[-1].id], [row["id"] for row in batches[0]])

    def test_merged_reply_preserves_an_edit_made_during_upload_for_the_next_cycle(self):
        self.note.fields[0] = "ねこ"
        self.collection.update_note(self.note)
        self.changes.capture(self.collection)
        frozen = self.payloads()
        self.note.fields[0] = "ねこ, edited while uploading"
        self.collection.update_note(self.note)
        self.changes = SyncChanges(self.path, self.server)
        self.changes.capture(self.collection)
        self.assertEqual(frozen, self.payloads())
        self.server.remote_back = "feline from Shirabe"
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(["ねこ, edited while uploading", "feline from Shirabe"], self.collection.get_note(self.note.id).fields)
        with SyncState(self.path) as state:
            self.assertEqual("ねこ\x1ffeline from Shirabe", state.checkpoint("source_note", self.note.id)["payload"]["flds"])
        self.changes.capture(self.collection)
        row = next(batch["notes"][0] for batch in self.payloads() if "notes" in batch)
        self.assertEqual("ねこ, edited while uploading\x1ffeline from Shirabe", row["flds"])
        self.assertEqual("note-digest", row["base"])

    def test_lost_response_retries_frozen_request_and_receipt_is_not_yet_applied(self):
        self.note.fields[0] = "ねこ"
        self.collection.update_note(self.note)
        self.changes.capture(self.collection)
        self.server.lose_batch = True
        with self.assertRaises(ConnectionError):
            self.changes.upload()
        self.changes = SyncChanges(self.path, self.server)
        self.changes.upload()
        self.assertEqual(self.server.batches[0], self.server.batches[1])
        with SyncState(self.path) as state:
            key = state.get("changes")["keys"][0]
            self.assertIsNotNone(state.batch(key)["receipt"])
            self.assertFalse(state.batch_applied(key))
        self.changes.apply(self.collection)
        self.changes.capture(self.collection)
        self.assertEqual([], self.payloads())

    def test_native_review_sends_history_before_card_with_its_base(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.changes.capture(self.collection)
        payloads = self.payloads()
        kinds = [next(kind for kind in ("note_types", "notes", "history", "cards") if kind in batch) for batch in payloads]
        self.assertEqual(["history", "cards"], kinds)
        self.assertEqual(payloads[0]["history"][0]["row"]["id"], payloads[1]["cards"][0]["last_review_id"])
        self.assertEqual("card-digest", payloads[1]["cards"][0]["base"])
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(1, self.collection.get_card(card.id).reps)

    def test_resuming_a_merged_suspension_back_to_the_submitted_state_is_still_an_edit(self):
        card = self.note.cards()[0]
        card.flags = 3
        self.collection.update_card(card)
        self.changes.capture(self.collection)
        self.server.suspend = True
        self.changes.upload()
        self.changes.apply(self.collection)
        card = self.collection.get_card(card.id)
        self.assertEqual(-1, card.queue)
        card.queue = 0
        self.collection.update_card(card)
        self.changes.capture(self.collection)
        self.assertEqual(0, next(batch["cards"][0]["row"]["queue"] for batch in self.payloads() if "cards" in batch))

    def test_review_made_during_upload_survives_a_received_suspension_and_remains_pending(self):
        card = self.note.cards()[0]
        card.flags = 3
        self.collection.update_card(card)
        self.changes.capture(self.collection)
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        self.server.suspend = True
        self.changes.upload()
        self.changes.apply(self.collection)
        card = self.collection.get_card(card.id)
        self.assertEqual(-1, card.queue)
        self.assertEqual(1, card.reps)
        self.changes.capture(self.collection)
        payloads = self.payloads()
        self.assertEqual(1, next(batch["cards"][0]["row"]["reps"] for batch in payloads if "cards" in batch))
        self.assertEqual(1, sum(len(batch.get("history", [])) for batch in payloads))

    def test_reverting_a_received_note_field_is_not_mistaken_for_an_unchanged_source(self):
        server = Server()
        remote = {"anki_id": self.note.id, "guid": self.note.guid, "anki_type_id": self.note.mid,
                  "field_names": ["Front", "Back"], "fields": ["猫", "feline"], "tags": []}
        server.page = {"cursor": 1, "changes": [{"cursor": 1, "entity_kind": "note", "operation": "upsert",
                       "payload": {"base": {"fields": ["猫", "cat"], "tags": []}, "snapshot": "received-note", "note": remote}}]}
        delivery = SyncDelivery(self.path, server, profile_id=123, key="epoch")
        delivery.receive()
        delivery.apply(self.collection)
        delivery.acknowledge()
        with SyncState(self.path) as state:
            self.assertEqual("猫\x1ffeline", state.checkpoint("source_note", self.note.id)["payload"]["flds"])
        note = self.collection.get_note(self.note.id)
        note.fields[1] = "cat"
        self.collection.update_note(note)
        self.changes.capture(self.collection)
        row = next(batch["notes"][0] for batch in self.payloads() if "notes" in batch)
        self.assertEqual("猫\x1fcat", row["flds"])
        self.assertEqual("received-note", row["base"])

    def test_native_undo_of_a_delivered_card_edit_is_captured_against_the_received_state(self):
        from shirabe_addon.sync_reviews import card_row

        card = self.note.cards()[0]
        before = card_row(self.collection, card.id)
        after = {**before, "queue": -1}
        server = Server()
        server.page = {"cursor": 1, "changes": [{"cursor": 1, "entity_kind": "card", "operation": "upsert",
            "payload": {"snapshot": "received-card", "card": {"anki_id": card.id, "deleted": False},
                        "anki": {"card_before": before, "card_after": after,
                                 "note_guid": self.note.guid, "anki_type_id": self.note.mid}}}]}
        delivery = SyncDelivery(self.path, server, profile_id=123, key="epoch")
        delivery.receive()
        delivery.apply(self.collection)
        delivery.acknowledge()
        self.assertEqual(-1, self.collection.get_card(card.id).queue)
        self.collection.undo()
        self.changes.capture(self.collection)
        entry = next(batch["cards"][0] for batch in self.payloads() if "cards" in batch)
        self.assertEqual(before["queue"], entry["row"]["queue"])
        self.assertEqual("received-card", entry["base"])
        self.assertEqual(0, sum(len(batch.get("history", [])) for batch in self.payloads()))

    def test_layout_edit_forces_unchanged_note_values_and_maps_stable_ids(self):
        model = self.note.note_type()
        model["flds"][0]["name"] = "Expression"
        model["tmpls"][0]["qfmt"] = "{{Expression}}"
        model["tmpls"][0]["afmt"] = "{{FrontSide}}<hr>{{Back}}"
        self.collection.models.save(model)
        self.changes.capture(self.collection)
        payloads = self.payloads()
        definition = payloads[0]["note_types"][0]
        self.assertEqual([0, 1], definition["field_map"])
        self.assertEqual([0], definition["template_map"])
        self.assertEqual(0, definition["base_revision"])
        row = next(batch["notes"][0] for batch in payloads if "notes" in batch)
        self.assertEqual(1, row["type_revision"])
        self.assertEqual("猫\x1fcat", row["flds"])
        self.changes.upload()
        self.changes.apply(self.collection)

    def test_owned_type_stamp_echo_keeps_the_bound_note_schema_revision(self):
        with SyncState(self.path) as state:
            definition = state.checkpoint("note_type", self.note.mid)["payload"]
            state.remember_checkpoint("created_note_type", 100, "owned-creation", definition)
            previous = copy.deepcopy(definition)
            previous["usn"] = -10
            state.remember_checkpoint("source_note_type", self.note.mid, "before-native-sync", previous)
        self.note.fields[1] = "changed after native sync"
        self.collection.update_note(self.note)

        self.changes.capture(self.collection)

        payloads = self.payloads()
        definition = next(batch["note_types"][0] for batch in payloads if "note_types" in batch)
        row = next(batch["notes"][0] for batch in payloads if "notes" in batch)
        self.assertEqual(0, definition["base_revision"])
        self.assertEqual(0, row["type_revision"])
        self.assertEqual("猫\x1fchanged after native sync", row["flds"])

    def test_selection_removal_refuses_without_staging_a_partial_cycle(self):
        card = self.note.cards()[0]
        card.did = self.collection.decks.id("Outside")
        self.collection.update_card(card)
        with self.assertRaisesRegex(SnapshotConflict, "deleted|moved"):
            self.changes.capture(self.collection)
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertEqual([], state.pending())

    def test_a_deleted_card_stages_its_last_card_and_note_checkpoints(self):
        card = self.note.cards()[0]
        with SyncState(self.path) as state:
            original = state.checkpoint("source_card", card.id)["payload"]
            card_base = state.checkpoint_digest("card", card.id)
            note_base = state.checkpoint_digest("note", self.note.id)
        self.collection.remove_notes([self.note.id])
        self.changes.capture(self.collection)
        deletion = next(item for batch in self.payloads() for item in batch.get("cards", []) if item.get("deleted"))
        self.assertEqual(original, deletion["row"])
        self.assertEqual(card_base, deletion["base"])
        self.assertEqual(note_base, deletion["note_base"])

    def test_a_locally_hidden_card_keeps_its_live_source_checkpoint(self):
        from shirabe_addon.sync_snapshot import source_digest

        card = self.note.cards()[0]
        card.flags = 2
        self.collection.update_card(card)
        self.server.hidden = True
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        row = next(SyncSnapshot._rows(self.collection, "cards", "id = ?", (card.id,)))
        with SyncState(self.path) as state:
            self.assertEqual(source_digest(row), state.checkpoint_digest("source_card", card.id))
            self.assertTrue(state.checkpoint("card", card.id)["payload"]["deleted"])
        self.changes.capture(self.collection)
        self.assertEqual([], self.payloads())
        self.changes.upload()
        self.changes.apply(self.collection)
        self.collection.remove_notes([self.note.id])
        self.changes.capture(self.collection)
        deletion = next(item for batch in self.payloads() for item in batch.get("cards", []) if item.get("deleted"))
        self.assertEqual(card.id, deletion["row"]["id"])

    def test_reading_a_deleted_collection_preserves_anki_undo(self):
        from shirabe_addon.sync_reviews import verify_platform

        self.collection.remove_notes([self.note.id])
        undo = self.collection.undo_status().undo
        self.assertTrue(undo)
        verify_platform(self.collection)
        self.assertEqual(undo, self.collection.undo_status().undo, "Schema inspection cleared undo")
        self.changes.capture(self.collection)
        self.assertEqual(undo, self.collection.undo_status().undo, "Source capture cleared undo")
        self.collection.undo()
        self.assertEqual("猫", self.collection.get_note(self.note.id).fields[0])

    def test_capture_failure_rolls_back_all_staged_batches(self):
        self.note.fields[0] = "ねこ"
        self.collection.update_note(self.note)
        enqueue = SyncState._enqueue

        def fail_after_enqueue(state, key, entities):
            enqueue(state, key, entities)
            raise OSError("Disk unavailable")

        with patch.object(SyncState, "_enqueue", fail_after_enqueue):
            with self.assertRaises(OSError):
                self.changes.capture(self.collection)
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertEqual([], state.pending())

    def test_added_subdeck_is_not_silently_omitted(self):
        self.collection.decks.id("Japanese::New")
        with self.assertRaisesRegex(SnapshotConflict, "subdecks"):
            self.changes.capture(self.collection)

    def test_paused_upload_resumes_without_recapture(self):
        self.note.fields[0] = "ねこ"
        self.collection.update_note(self.note)
        captured = self.changes.capture(self.collection)
        with self.assertRaisesRegex(SnapshotConflict, "paused"):
            self.changes.upload(cancelled=lambda: True)
        self.assertEqual([], self.server.batches)
        self.assertEqual(captured, SyncChanges(self.path, self.server).capture(self.collection))
        self.changes.upload()
        self.changes.apply(self.collection)

    def test_decorated_rows_still_obey_the_wire_byte_limit(self):
        for index in range(3):
            note = self.collection.new_note(self.note.note_type())
            note.fields = ["日" * 1000, str(index)]
            self.collection.add_note(note, self.deck_id)
        with patch.object(SyncSnapshot, "MAX_BYTES", 16384):
            self.changes.capture(self.collection)
        payloads = self.payloads()
        self.assertGreater(sum("notes" in batch for batch in payloads), 1)
        self.assertTrue(all(len(json.dumps(batch).encode()) <= 16384 for batch in payloads))

    def test_oversized_history_removal_leaves_no_partial_capture_or_deleted_checkpoints(self):
        from shirabe_addon.sync_reviews import REVLOG_COLUMNS
        card = self.note.cards()[0]
        rows = [(1_700_000_000_000 + index, card.id, -1, 3, 1, 0, 2500, 100, 1) for index in range(401)]
        self.collection.db.executemany(
            f"INSERT INTO revlog ({','.join(REVLOG_COLUMNS)}) VALUES ({','.join('?' for _ in REVLOG_COLUMNS)})", rows)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        with SyncState(self.path) as state:
            before = state.checkpoint_digests("source_history")
        self.assertEqual(401, len(before))
        self.collection.db.execute("DELETE FROM revlog")
        self.note.fields[0] = "ねこ"
        self.collection.update_note(self.note)
        with patch.object(SyncSnapshot, "MAX_BYTES", 16384), self.assertRaisesRegex(SnapshotConflict, "cards record exceeds the sync byte limit"):
            self.changes.capture(self.collection)
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertEqual([], state.pending())
            self.assertEqual(before, state.checkpoint_digests("source_history"))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_applied_review_identity_is_carried_into_incremental_history(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        row = next(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,)))
        with SyncState(self.path) as state:
            state.prepare_review("explicit-review-uuid", {"rating": 3}, row)
            state.finish_review("explicit-review-uuid")
        self.changes.capture(self.collection)
        entry = next(batch["history"][0] for batch in self.payloads() if "history" in batch)
        self.assertEqual("explicit-review-uuid", entry["sync_id"])
        self.changes.upload()
        self.changes.apply(self.collection)

    def test_network_worker_does_not_use_the_collection(self):
        self.note.fields[0] = "ねこ"
        self.collection.update_note(self.note)
        self.changes.capture(self.collection)
        payload = self.payloads()[0]
        receipt = self.server.batch(123, payload)
        with patch.object(self.server, "batch", return_value=receipt):
            with ThreadPoolExecutor(max_workers=1) as network:
                network.submit(self.changes.upload).result(timeout=5)
        with patch.object(self.server, "batch", side_effect=AssertionError("Network in collection operation")):
            self.changes.apply(self.collection)

    def test_unaccepted_receipt_keeps_the_cycle_and_source_baseline(self):
        self.note.fields[0] = "ねこ"
        self.collection.update_note(self.note)
        self.changes.capture(self.collection)
        self.server.reject_notes = True
        self.changes.upload()
        with self.assertRaises(SnapshotConflict):
            self.changes.apply(self.collection)
        with SyncState(self.path) as state:
            self.assertIsNotNone(state.get("changes"))
            self.assertEqual("猫\x1fcat", state.checkpoint("source_note", self.note.id)["payload"]["flds"])

    def two_changed_notes(self):
        other = self.collection.new_note(self.note.note_type())
        other.fields = ["犬", "dog"]
        self.collection.add_note(other, self.deck_id)
        self.changes.capture(self.collection)
        self.changes.upload()
        self.changes.apply(self.collection)
        for note in (self.note, other):
            note.fields[0] += " edited in Anki"
            self.collection.update_note(note)
        self.changes.capture(self.collection)
        return other

    def test_a_remote_conflict_does_not_hold_up_accepted_notes_or_replay_them_after_restart(self):
        other = self.two_changed_notes()
        send = self.server.batch
        self.server.remote_back = "merged in Shirabe"

        def mixed_receipt(profile_id, payload):
            receipt = send(profile_id, payload)
            for result in receipt["notes"]:
                if result["id"] == other.id:
                    result.update(status="conflict", conflict_id=456, code="field_conflict")
            return receipt

        with patch.object(self.server, "batch", side_effect=mixed_receipt):
            self.changes.upload()
        frozen = self.payloads()
        with self.assertRaises(SnapshotConflict):
            self.changes.apply(self.collection)
        accepted = self.collection.get_note(self.note.id)
        self.assertEqual("merged in Shirabe", accepted.fields[1])
        self.assertEqual("dog", self.collection.get_note(other.id).fields[1])
        with SyncState(self.path) as state:
            self.assertEqual("猫 edited in Anki\x1fmerged in Shirabe", state.checkpoint("source_note", self.note.id)["payload"]["flds"])
            self.assertEqual("犬\x1fdog", state.checkpoint("source_note", other.id)["payload"]["flds"])
        accepted.fields[1] = "edited again after acceptance"
        self.collection.update_note(accepted)
        self.changes = SyncChanges(self.path, self.server)
        with patch.object(self.server, "batch", side_effect=AssertionError("Replayed a stored receipt")):
            self.changes.upload()
        from shirabe_addon.sync_changes import apply_note_change
        with patch("shirabe_addon.sync_changes.apply_note_change", wraps=apply_note_change) as apply:
            with self.assertRaises(SnapshotConflict):
                self.changes.apply(self.collection)
            apply.assert_not_called()
        self.assertEqual(frozen, self.payloads())
        self.assertEqual("edited again after acceptance", self.collection.get_note(self.note.id).fields[1])

    def test_an_in_flight_local_conflict_keeps_later_accepted_rows_and_retries_only_the_unfinished_row(self):
        other = self.two_changed_notes()
        self.server.remote_back = "merged in Shirabe"
        self.changes.upload()
        self.note.fields[1] = "independent Anki edit"
        self.collection.update_note(self.note)
        with self.assertRaises(SnapshotConflict):
            self.changes.apply(self.collection)
        self.assertEqual("merged in Shirabe", self.collection.get_note(other.id).fields[1])
        with SyncState(self.path) as state:
            self.assertEqual("猫\x1fcat", state.checkpoint("source_note", self.note.id)["payload"]["flds"])
            self.assertEqual("犬 edited in Anki\x1fmerged in Shirabe", state.checkpoint("source_note", other.id)["payload"]["flds"])
        self.note.fields[1] = "cat"
        self.collection.update_note(self.note)
        self.changes = SyncChanges(self.path, self.server)
        from shirabe_addon.sync_changes import apply_note_change
        with patch("shirabe_addon.sync_changes.apply_note_change", wraps=apply_note_change) as apply:
            self.changes.apply(self.collection)
            self.assertEqual([self.note.id], [call.args[1]["note"]["anki_id"] for call in apply.call_args_list])
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))

    def test_a_malformed_receipt_is_refused_before_any_accepted_row_is_applied(self):
        other = self.two_changed_notes()
        send = self.server.batch
        self.server.remote_back = "merged in Shirabe"

        def wrong_identity(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["notes"][-1]["id"] = other.id + 100
            return receipt

        with patch.object(self.server, "batch", side_effect=wrong_identity):
            with self.assertRaises(SnapshotConflict):
                self.changes.upload()
        with self.assertRaises(SnapshotConflict):
            self.changes.apply(self.collection)
        self.assertEqual("cat", self.collection.get_note(self.note.id).fields[1])

    def test_a_card_conflict_does_not_hold_up_other_accepted_card_states(self):
        other = self.two_changed_notes()
        self.changes.upload()
        self.changes.apply(self.collection)
        cards = [self.note.cards()[0], other.cards()[0]]
        for card in cards:
            card.flags = 1
            self.collection.update_card(card)
        self.changes.capture(self.collection)
        send = self.server.batch

        def different_flags(profile_id, payload):
            receipt = send(profile_id, payload)
            for result in receipt["cards"]:
                result["anki"]["card_after"]["flags"] = 2
            return receipt

        with patch.object(self.server, "batch", side_effect=different_flags):
            self.changes.upload()
        cards[0].flags = 3
        self.collection.update_card(cards[0])
        with self.assertRaises(SnapshotConflict):
            self.changes.apply(self.collection)
        self.assertEqual(3, self.collection.get_card(cards[0].id).flags)
        self.assertEqual(2, self.collection.get_card(cards[1].id).flags)
        cards[0].flags = 1
        self.collection.update_card(cards[0])
        self.changes = SyncChanges(self.path, self.server)
        from shirabe_addon.sync_changes import apply_card_change
        with patch("shirabe_addon.sync_changes.apply_card_change", wraps=apply_card_change) as apply:
            self.changes.apply(self.collection)
            self.assertEqual([cards[0].id], [call.args[1]["id"] for call in apply.call_args_list])

    def note_resolution(self):
        self.note.fields[1] = "Anki meaning"
        self.collection.update_note(self.note)
        self.changes.capture(self.collection)
        send = self.server.batch

        def conflict(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["notes"][0].update(status="conflict", conflict_id=456)
            return receipt

        with patch.object(self.server, "batch", side_effect=conflict):
            self.changes.upload()
        with self.assertRaises(SnapshotConflict) as error:
            self.changes.apply(self.collection)
        problem = error.exception.conflicts[0]
        with SyncState(self.path) as state:
            key = state.get("changes")["binding"]["key"]
        detail = {"id": 456, "key": key, "resolved": False, "snapshot": "current-note",
                  "note": {"anki_id": self.note.id, "guid": self.note.guid, "anki_type_id": self.note.mid,
                           "field_names": ["Front", "Back"], "type_revision": 0,
                           "fields": ["猫", "Shirabe meaning"], "tags": []}}
        return self.changes.prepare_note_resolution(self.collection, problem, detail)

    def card_resolution(self):
        from shirabe_addon.sync_reviews import card_row
        card = self.note.cards()[0]
        card.flags = 3
        self.collection.update_card(card)
        self.changes.capture(self.collection)
        send = self.server.batch

        def conflict(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["cards"][0].update(status="conflict", conflict_id=789)
            return receipt

        with patch.object(self.server, "batch", side_effect=conflict):
            self.changes.upload()
        with self.assertRaises(SnapshotConflict) as error:
            self.changes.apply(self.collection)
        problem = error.exception.conflicts[0]
        with SyncState(self.path) as state:
            key = state.get("changes")["binding"]["key"]
        remote = {**card_row(self.collection, card.id), "flags": 2, "queue": -1}
        detail = {"id": 789, "key": key, "resolved": False, "snapshot": "current-card", "card": {"anki_id": card.id},
                  "anki": {"note_guid": self.note.guid, "anki_type_id": self.note.mid, "card_after": remote}}
        return self.changes.prepare_card_resolution(self.collection, problem, detail)

    def test_card_choices_retry_after_restart_without_rewriting_history_or_original_receipts(self):
        prepared = self.card_resolution()
        original_key = prepared["problem"]["request_key"]
        with SyncState(self.path) as state:
            original = state.batch(original_key)
        self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        frozen = self.payloads()
        entry = frozen[-1]["cards"][0]
        self.assertEqual(2, entry["row"]["flags"])
        self.assertEqual(0, entry["row"]["queue"])
        self.assertEqual(789, entry["conflict_id"])
        self.assertEqual("current-card", entry["base"])
        self.assertEqual(3, self.note.cards()[0].flags)
        self.server.lose_batch = True
        with self.assertRaises(ConnectionError):
            self.changes.upload()
        self.changes = SyncChanges(self.path, self.server)
        self.assertEqual(frozen, self.payloads())
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(2, self.note.cards()[0].flags)
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertEqual(original, state.batch(original_key))
            self.assertTrue(state.entity_applied(original_key, "cards", self.note.cards()[0].id))

    def test_new_native_reviews_after_an_accepted_receipt_can_be_uploaded_without_rewriting_that_receipt(self):
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        self.changes.capture(self.collection)
        send = self.server.batch

        def competing(profile_id, payload):
            result = send(profile_id, payload)
            for item in result.get("cards", []):
                item["anki"]["card_after"]["ivl"] += 1
                item["anki"]["card_after"]["due"] += 1
                item["anki"]["schedule_review"] = {"sync_id": "pending-local", "reviewed_at": "2026-09-16T12:00:00Z"}
            return result

        with patch.object(self.server, "batch", competing):
            self.changes.upload()
        card = self.collection.get_card(card.id)
        card.start_timer()
        self.collection.sched.answerCard(card, 4)
        history = list(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,)))
        with self.assertRaises(SyncConflict) as error:
            self.changes.apply(self.collection)
        problem = error.exception.conflicts[0]
        prepared = self.changes.prepare_card_resolution(self.collection, problem, self.changes.card_conflict(problem))
        self.assertEqual([history[-1]], prepared["pending_reviews"])
        self.collection.db.execute("UPDATE revlog SET time = time + 1 WHERE id = ?", history[0]["id"])
        changed = self.changes.prepare_card_resolution(self.collection, problem, self.changes.card_conflict(problem))
        self.assertEqual([], changed["pending_reviews"])
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
        self.collection.db.execute("UPDATE revlog SET time = ? WHERE id = ?", history[0]["time"], history[0]["id"])
        self.collection.db.execute("UPDATE revlog SET time = time + 1 WHERE id = ?", history[-1]["id"])
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
        self.collection.db.execute("UPDATE revlog SET time = ? WHERE id = ?", history[-1]["time"], history[-1]["id"])
        with SyncState(self.path) as state:
            original = state.batch(problem["request_key"])
        self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
        batches = self.payloads()
        self.assertEqual(history[-1], batches[-2]["history"][0]["row"])
        self.assertEqual(history[-1]["id"], batches[-1]["cards"][0]["last_event_id"])
        self.server.lose_batch = True
        with self.assertRaises(ConnectionError):
            self.changes.upload()
        self.changes = SyncChanges(self.path, self.server)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(history, list(SyncSnapshot._rows(self.collection, "revlog", "cid = ?", (card.id,))))
        with SyncState(self.path) as state:
            self.assertEqual(original, state.batch(problem["request_key"]))
            self.assertIsNone(state.get("changes"))

    def test_card_edits_after_opening_the_chooser_refuse_stale_choices(self):
        prepared = self.card_resolution()
        card = self.note.cards()[0]
        card.flags = 4
        self.collection.update_card(card)
        before = self.payloads()
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        self.assertEqual(before, self.payloads())

    def test_card_edits_during_resolution_upload_remain_a_local_conflict(self):
        prepared = self.card_resolution()
        self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        card = self.note.cards()[0]
        card.flags = 4
        self.collection.update_card(card)
        self.changes.upload()
        with self.assertRaises(SnapshotConflict) as error:
            self.changes.apply(self.collection)
        self.assertEqual("local_conflict", error.exception.conflicts[0]["status"])
        self.assertEqual(4, self.note.cards()[0].flags)

    def test_card_chooser_cannot_replace_divergent_review_schedules(self):
        prepared = self.card_resolution()
        prepared["detail"]["anki"]["card_after"]["reps"] += 1
        before = self.payloads()
        with self.assertRaisesRegex(SnapshotConflict, "review history"):
            self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        self.assertEqual(before, self.payloads())

    def test_a_review_during_card_resolution_upload_is_preserved_for_the_next_capture(self):
        prepared = self.card_resolution()
        self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        card = self.note.cards()[0]
        card.start_timer()
        self.collection.sched.answerCard(card, 3)
        history = self.collection.db.all("SELECT * FROM revlog")
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(1, self.note.cards()[0].reps)
        self.assertEqual(2, self.note.cards()[0].flags)
        self.assertEqual(history, self.collection.db.all("SELECT * FROM revlog"))
        self.changes.capture(self.collection)
        self.assertEqual(1, sum(len(batch.get("history", [])) for batch in self.payloads()))

    def test_a_server_schedule_change_during_card_resolution_stays_pending(self):
        prepared = self.card_resolution()
        self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        send = self.server.batch

        def reviewed(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["cards"][0]["anki"]["card_after"]["reps"] += 1
            return receipt

        with patch.object(self.server, "batch", side_effect=reviewed):
            self.changes.upload()
        with self.assertRaises(SnapshotConflict) as error:
            self.changes.apply(self.collection)
        self.assertIn("review history", error.exception.conflicts[0]["code"])
        self.assertEqual(0, self.note.cards()[0].reps)
        self.assertEqual(3, self.note.cards()[0].flags)

    def test_an_accepted_card_receipt_with_a_new_local_conflict_can_be_chosen_again(self):
        prepared = self.card_resolution()
        self.changes.resolve_card(self.collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        card = self.note.cards()[0]
        card.flags = 4
        self.collection.update_card(card)
        self.changes.upload()
        with self.assertRaises(SnapshotConflict) as error:
            self.changes.apply(self.collection)
        problem = error.exception.conflicts[0]
        detail = self.changes.card_conflict(problem)
        self.assertIsNone(detail["id"])
        second = self.changes.prepare_card_resolution(self.collection, problem, detail)
        self.changes.resolve_card(self.collection, second, {"deck": "anki", "lifecycle": "anki", "properties": "shirabe"})
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual(2, self.note.cards()[0].flags)
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertTrue(state.entity_applied(prepared["problem"]["request_key"], "cards", card.id))

    def test_card_choices_refuse_changed_epochs_identities_and_unselected_decks(self):
        prepared = self.card_resolution()
        for change in ({"key": "another-epoch"}, {"id": 790}, {"resolved": True}):
            with self.assertRaises(SnapshotConflict):
                self.changes.prepare_card_resolution(self.collection, prepared["problem"], {**prepared["detail"], **change})
        wrong = copy.deepcopy(prepared["detail"])
        wrong["anki"]["card_after"]["nid"] += 1
        with self.assertRaisesRegex(SnapshotConflict, "identity"):
            self.changes.prepare_card_resolution(self.collection, prepared["problem"], wrong)
        outside = copy.deepcopy(prepared["detail"])
        outside["anki"]["card_after"]["did"] = self.collection.decks.id("Unselected")
        changed = self.changes.prepare_card_resolution(self.collection, prepared["problem"], outside)
        before = self.payloads()
        with self.assertRaisesRegex(SnapshotConflict, "outside"):
            self.changes.resolve_card(self.collection, changed, {"deck": "shirabe", "lifecycle": "anki", "properties": "shirabe"})
        self.assertEqual(before, self.payloads())

    def test_a_note_resolution_keeps_the_original_receipt_and_resumes_after_restart(self):
        prepared = self.note_resolution()
        with SyncState(self.path) as state:
            original_key = state.get("changes")["keys"][0]
            original = state.batch(original_key)
        self.changes.resolve_note(self.collection, prepared, ["猫", "Shirabe meaning"], ["chosen"])
        frozen = self.payloads()
        self.assertEqual(2, len(frozen))
        self.assertEqual(456, frozen[-1]["notes"][0]["conflict_id"])
        self.assertEqual("current-note", frozen[-1]["notes"][0]["base"])
        self.assertEqual("Anki meaning", self.collection.get_note(self.note.id).fields[1])
        self.changes = SyncChanges(self.path, self.server)
        self.changes.capture(self.collection)
        self.assertEqual(frozen, self.payloads())
        self.server.lose_batch = True
        with self.assertRaises(ConnectionError):
            self.changes.upload()
        self.changes = SyncChanges(self.path, self.server)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual("Shirabe meaning", self.collection.get_note(self.note.id).fields[1])
        self.assertEqual(["chosen"], self.collection.get_note(self.note.id).tags)
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertEqual(original, state.batch(original_key))
            self.assertTrue(state.entity_applied(original_key, "notes", self.note.id))

    def test_editing_anki_after_opening_a_note_chooser_refuses_to_stage_the_stale_choice(self):
        prepared = self.note_resolution()
        self.note.fields[1] = "edited while chooser open"
        self.collection.update_note(self.note)
        before = self.payloads()
        with self.assertRaisesRegex(SnapshotConflict, "changed"):
            self.changes.resolve_note(self.collection, prepared, ["猫", "Shirabe meaning"], [])
        self.assertEqual(before, self.payloads())

    def test_editing_the_chosen_field_during_resolution_upload_is_not_overwritten(self):
        prepared = self.note_resolution()
        self.changes.resolve_note(self.collection, prepared, ["猫", "Shirabe meaning"], [])
        self.note.fields[1] = "edited during upload"
        self.collection.update_note(self.note)
        self.changes.upload()
        with self.assertRaises(SnapshotConflict) as error:
            self.changes.apply(self.collection)
        self.assertEqual("edited during upload", self.collection.get_note(self.note.id).fields[1])
        self.assertEqual(1, len(error.exception.conflicts))
        self.assertEqual("local_conflict", error.exception.conflicts[0]["status"])

    def test_a_second_server_conflict_can_be_resolved_without_abandoning_the_original(self):
        prepared = self.note_resolution()
        self.changes.resolve_note(self.collection, prepared, ["猫", "first choice"], [])
        send = self.server.batch

        def conflict_again(profile_id, payload):
            receipt = send(profile_id, payload)
            receipt["notes"][0].update(status="conflict", conflict_id=457)
            return receipt

        with patch.object(self.server, "batch", side_effect=conflict_again):
            self.changes.upload()
        with self.assertRaises(SnapshotConflict) as error:
            self.changes.apply(self.collection)
        self.assertEqual(1, len(error.exception.conflicts))
        detail = copy.deepcopy(prepared["detail"])
        detail.update(id=457, snapshot="newer-comparison")
        detail["note"]["fields"][1] = "newer Shirabe edit"
        second = self.changes.prepare_note_resolution(self.collection, error.exception.conflicts[0], detail)
        self.changes.resolve_note(self.collection, second, ["猫", "second choice"], [])
        self.changes = SyncChanges(self.path, self.server)
        self.changes.upload()
        self.changes.apply(self.collection)
        self.assertEqual("second choice", self.collection.get_note(self.note.id).fields[1])
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("changes"))
            self.assertTrue(state.entity_applied(prepared["problem"]["request_key"], "notes", self.note.id))

    def test_resolution_staging_failure_leaves_no_orphan_request_or_link(self):
        prepared = self.note_resolution()
        before = self.payloads()
        put = SyncState._put

        def interrupted(state, key, value):
            put(state, key, value)
            raise OSError("Resolution interrupted")

        with patch.object(SyncState, "_put", interrupted):
            with self.assertRaises(OSError):
                self.changes.resolve_note(self.collection, prepared, ["猫", "chosen"], [])
        self.assertEqual(before, self.payloads())
        with SyncState(self.path) as state:
            self.assertEqual(0, state.db.execute("SELECT count(*) FROM batch_resolutions").fetchone()[0])
