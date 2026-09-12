from __future__ import annotations

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import support
from shirabe_addon.sync_note_creations import apply_note_creation, CREATION_RECEIPT
from shirabe_addon.sync_delivery import SyncDelivery, DeliveryConflict
from shirabe_addon.sync_notes import NoteConflict
from shirabe_addon.sync_state import SyncState
from shirabe_addon.sync_changes import SyncChanges

try:
    from anki.collection import Collection
    from anki.buildinfo import version
except ImportError:
    Collection = None
    version = None


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncNoteCreationsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sync.sqlite3"
        self.collection_path = str(Path(self.directory.name) / "collection.anki2")
        self.collection = Collection(self.collection_path)
        self.collection.set_config("fsrs", True)
        self.deck_id = self.collection.decks.id("Japanese")
        self.model = self.collection.models.by_name("Basic (and reversed card)")
        self.change = self.creation(self.model, [" <ruby>猫<rt>ねこ</rt></ruby>\n", "<script>custom()</script>cat"], [0, 1])
        self.remember()

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def creation(self, model, fields, ordinals):
        return {"cursor": 1, "entity_kind": "note", "entity_id": 10, "operation": "upsert", "payload": {
            "creation": {"key": "note-creation:10", "anki_deck_id": self.deck_id},
            "note": {"anki_id": None, "guid": "creation-guid", "anki_type_id": model["id"],
                     "field_names": [field["name"] for field in model["flds"]], "fields": fields, "tags": ["Japanese", "new"]},
            "definition": copy.deepcopy(model),
            "cards": [{"card_id": 100 + ordinal, "ordinal": ordinal, "anki_deck_id": self.deck_id} for ordinal in ordinals]}}

    def remember(self):
        with SyncState(self.path) as state:
            state.bind_delivery({"api": "http://127.0.0.1/api/v1", "profile_id": 123, "key": "epoch"})
            state.remember_delivery(0, {"cursor": 1, "changes": [self.change]})

    def apply(self):
        with SyncState(self.path) as state:
            return apply_note_creation(self.collection, state, self.change, {self.deck_id})

    def restart_collection(self):
        self.collection.close()
        self.collection = Collection(self.collection_path)

    def delivery(self):
        client = SimpleNamespace(api="http://127.0.0.1/api/v1", batch=Mock(return_value={"history": [], "creations": []}), acknowledge=Mock())
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"phase": "uploaded", "api": client.api, "profile": {"id": 123},
                "pair_request": {"key": "epoch"}, "collection_path": str(Path(self.collection_path).resolve()),
                "root_deck_ids": [self.deck_id], "deck_ids": [self.deck_id]})
        return SyncDelivery(self.path, client, profile_id=123, key="epoch")

    def owned_creation(self):
        definition = copy.deepcopy(self.model)
        definition.update(id=None, name="Shirabe vocab", shirabe={"version": 1, "kind": "vocab"})
        self.change = self.creation(definition, ["猫", "<b>cat</b>"], [0, 1])
        self.change["payload"]["creation"]["owned_type_id"] = 42
        self.change["payload"]["note"]["note_type_id"] = 42
        with SyncState(self.path) as state:
            state.put("delivery", None)
        self.remember()

    def test_public_native_type_and_note_creation_roll_back_together(self):
        previous = {item.id for item in self.collection.models.all_names_and_ids()}
        def interrupted():
            definition = copy.deepcopy(self.model)
            definition.update(id=0, name="Atomic type probe")
            identity = self.collection.models.add_dict(definition).id
            note = self.collection.new_note(self.collection.models.get(identity))
            note.fields = ["猫", "cat"]
            self.collection.add_note(note, self.deck_id)
            raise RuntimeError("abort type and note")
        with self.assertRaisesRegex(RuntimeError, "abort type and note"):
            self.collection.db.transact(interrupted)
        self.restart_collection()
        self.assertEqual(previous, {item.id for item in self.collection.models.all_names_and_ids()})
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM notes"))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM cards"))

    def test_owned_definition_creates_native_type_and_returns_its_identity(self):
        self.owned_creation()
        previous = {item.id for item in self.collection.models.all_names_and_ids()}
        result = self.apply()["creation"]
        definition = result["definition"]
        self.assertEqual(42, result["owned_type_id"])
        self.assertEqual(definition["id"], result["note"]["mid"])
        self.assertNotIn(definition["id"], previous)
        self.assertEqual(definition, self.collection.models.get(definition["id"]))
        self.assertEqual(self.change["payload"]["definition"]["css"], definition["css"])
        self.assertEqual(self.change["payload"]["definition"]["shirabe"], definition["shirabe"])
        self.assertEqual(self.change["payload"]["note"]["fields"], self.collection.get_note(result["note"]["id"]).fields)
        self.assertEqual(result, self.apply()["creation"])
        self.assertEqual(len(previous) + 1, len(self.collection.models.all_names_and_ids()))

    def test_owned_creation_receipt_failure_rolls_back_type_as_well_as_note(self):
        self.owned_creation()
        previous = {item.id for item in self.collection.models.all_names_and_ids()}
        with patch.object(self.collection, "set_config", side_effect=RuntimeError("receipt interrupted")):
            with self.assertRaisesRegex(RuntimeError, "receipt interrupted"):
                self.apply()
        self.restart_collection()
        self.assertEqual(previous, {item.id for item in self.collection.models.all_names_and_ids()})
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM notes"))
        result = self.apply()["creation"]
        self.assertEqual(len(previous) + 1, len(self.collection.models.all_names_and_ids()))
        self.assertEqual(result["definition"]["id"], result["note"]["mid"])

    def test_owned_creation_recovery_never_creates_another_type(self):
        self.owned_creation()
        with patch.object(SyncState, "remember_note_creation_output", side_effect=RuntimeError("sidecar unavailable")):
            with self.assertRaisesRegex(RuntimeError, "sidecar unavailable"):
                self.apply()
        self.restart_collection()
        note_id = self.collection.db.scalar("SELECT id FROM notes")
        identity = self.collection.get_note(note_id).mid
        with patch.object(self.collection.models, "add_dict", side_effect=AssertionError("duplicate type")):
            result = self.apply()["creation"]
        self.assertEqual(note_id, result["note"]["id"])
        self.assertEqual(identity, result["definition"]["id"])

    def next_owned_creation(self):
        self.change = copy.deepcopy(self.change)
        self.change.update(cursor=2, entity_id=11)
        self.change["payload"]["creation"]["key"] = "note-creation:11"
        self.change["payload"]["note"]["guid"] = "second-owned-note"
        for card in self.change["payload"]["cards"]:
            card["card_id"] += 100
        with SyncState(self.path) as state:
            state.put("delivery", None)
            state.remember_delivery(0, {"cursor": 2, "changes": [self.change]})

    def test_two_pending_owned_notes_reuse_the_receipted_type(self):
        self.owned_creation()
        first = self.apply()["creation"]
        self.next_owned_creation()
        self.restart_collection()
        with patch.object(self.collection.models, "add_dict", side_effect=AssertionError("duplicate type")):
            second = self.apply()["creation"]
        self.assertEqual(first["definition"], second["definition"])
        self.assertNotEqual(first["note"]["id"], second["note"]["id"])
        self.assertEqual(2, self.collection.db.scalar("SELECT count(*) FROM notes"))

    def test_owned_creation_does_not_adopt_a_matching_name_or_marker(self):
        self.owned_creation()
        foreign = copy.deepcopy(self.change["payload"]["definition"])
        foreign["id"] = 0
        foreign_id = self.collection.models.add_dict(foreign).id
        before = copy.deepcopy(self.collection.models.get(foreign_id))
        created = self.apply()["creation"]
        self.assertNotEqual(foreign_id, created["definition"]["id"])
        self.assertEqual(before, self.collection.models.get(foreign_id))
        self.assertTrue(created["definition"]["name"].startswith("Shirabe vocab-"))

    def test_changed_owned_definition_is_not_overwritten_by_a_second_creation(self):
        self.owned_creation()
        first = self.apply()["creation"]
        model = self.collection.models.get(first["definition"]["id"])
        model["css"] += "\n.card { color: blue; }"
        self.collection.models.save(model)
        self.next_owned_creation()
        with self.assertRaisesRegex(NoteConflict, "owned note type changed"):
            self.apply()
        self.assertEqual(model["css"], self.collection.models.get(model["id"])["css"])
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM notes"))

    def test_owned_binding_failure_recovers_without_duplicate_type(self):
        self.owned_creation()
        with patch.object(SyncState, "remember_checkpoint", side_effect=RuntimeError("binding interrupted")):
            with self.assertRaisesRegex(RuntimeError, "binding interrupted"):
                self.apply()
        self.restart_collection()
        with patch.object(self.collection.models, "add_dict", side_effect=AssertionError("duplicate type")):
            result = self.apply()["creation"]
        with SyncState(self.path) as state:
            self.assertEqual(result["definition"], state.checkpoint("created_note_type", 42)["payload"])

    def test_wrong_owned_siblings_roll_back_the_type_and_retry_without_reopening(self):
        self.owned_creation()
        previous = {item.id for item in self.collection.models.all_names_and_ids()}
        original = self.collection.add_note
        def alter(note, deck_id):
            result = original(note, deck_id)
            self.collection.remove_cards_and_orphaned_notes([note.cards()[0].id])
            return result
        with patch.object(self.collection, "add_note", side_effect=alter):
            with self.assertRaisesRegex(NoteConflict, "siblings"):
                self.apply()
        self.assertEqual(previous, {item.id for item in self.collection.models.all_names_and_ids()})
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM notes"))
        created = self.apply()["creation"]
        self.assertEqual(2, len(created["cards"]))

    def test_owned_native_defaults_do_not_require_exported_native_field_ids(self):
        self.owned_creation()
        definition = self.change["payload"]["definition"]
        for field in definition["flds"]:
            for key in list(field):
                if key not in ("name", "ord", "font", "size", "rtl", "sticky"):
                    del field[key]
        for template in definition["tmpls"]:
            for key in list(template):
                if key not in ("name", "ord", "qfmt", "afmt", "bqfmt", "bafmt", "mod", "usn", "did"):
                    del template[key]
        with SyncState(self.path) as state:
            state.put("delivery", None)
        self.remember()
        result = self.apply()["creation"]
        self.assertEqual(["Front", "Back"], [field["name"] for field in result["definition"]["flds"]])
        self.assertEqual(result["definition"], self.collection.models.get(result["note"]["mid"]))
        self.assertIn("<b>cat</b>", self.collection.get_card(result["cards"][0]["id"]).answer())

    def test_owned_creation_rejects_a_missing_or_different_ownership_identity(self):
        self.owned_creation()
        self.change["payload"]["note"]["note_type_id"] = 43
        with SyncState(self.path) as state:
            state.put("delivery", None)
        self.remember()
        with self.assertRaisesRegex(NoteConflict, "Invalid owned"):
            self.apply()
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM notes"))

    def owned_receipt(self, created):
        return {"history": [], "creations": [{"status": "accepted", "key": created["key"],
            "delivery_cursor": created["delivery_cursor"],
            "note": {"id": created["note"]["id"], "snapshot": "accepted-note",
                "note": {"anki_id": created["note"]["id"], "anki_type_id": created["note"]["mid"]}},
            "cards": [{"id": card["id"], "snapshot": "accepted-card", "card": {"anki_id": card["id"]}}
                for card in created["cards"]],
            "note_type": {"id": created["definition"]["id"], "note_type_id": created["owned_type_id"],
                "revision": 0, "definition": created["definition"]}}]}

    def test_owned_type_acknowledgement_checkpoints_the_definition_for_the_next_scan(self):
        self.owned_creation()
        delivery = self.delivery()
        delivery.apply(self.collection)
        with SyncState(self.path) as state:
            created = state.delivery_result(1)["creation"]
        delivery.client.batch.return_value = self.owned_receipt(created)
        delivery.acknowledge()
        with SyncState(self.path) as state:
            checkpoint = state.checkpoint("note_type", created["definition"]["id"])
            self.assertIsNotNone(checkpoint)
            self.assertEqual(created["definition"], checkpoint["payload"])
            self.assertEqual("0", checkpoint["digest"])
        captured = SyncChanges(self.path, delivery.client).capture(self.collection)
        self.assertEqual([], captured["keys"])

    def test_owned_type_acknowledgement_refuses_a_different_definition(self):
        self.owned_creation()
        delivery = self.delivery()
        delivery.apply(self.collection)
        with SyncState(self.path) as state:
            created = state.delivery_result(1)["creation"]
        receipt = copy.deepcopy(self.owned_receipt(created))
        receipt["creations"][0]["note_type"]["definition"]["css"] += "changed"
        delivery.client.batch.return_value = receipt
        with self.assertRaisesRegex(DeliveryConflict, "note type"):
            delivery.acknowledge()
        delivery.client.acknowledge.assert_not_called()
        with SyncState(self.path) as state:
            self.assertIsNone(state.checkpoint("note", created["note"]["id"]))
            self.assertEqual(0, state.get("delivery_cursor", 0))

    def test_creation_returns_native_rows_and_preserves_fields_and_siblings(self):
        result = self.apply()
        creation = result["creation"]
        note = self.collection.get_note(creation["note"]["id"])
        self.assertEqual(self.change["payload"]["note"]["fields"], note.fields)
        self.assertEqual({"Japanese", "new"}, set(note.tags))
        self.assertEqual("creation-guid", note.guid)
        self.assertEqual([0, 1], sorted(card["ord"] for card in creation["cards"]))
        self.assertEqual({card.id for card in note.cards()}, {card["id"] for card in creation["cards"]})
        self.assertEqual("note-creation:10", creation["key"])
        self.assertEqual(1, creation["delivery_cursor"])
        self.assertIsNone(self.collection.get_config(CREATION_RECEIPT))
        self.assertEqual(result, self.apply())
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))

    def test_native_commit_before_sidecar_result_recovers_without_overwriting_later_edits(self):
        with patch.object(SyncState, "finish_delivery_change", side_effect=RuntimeError("lost sidecar write")):
            with self.assertRaisesRegex(RuntimeError, "lost sidecar"):
                self.apply()
        self.restart_collection()
        self.assertIsNotNone(self.collection.get_config(CREATION_RECEIPT))
        note_id = self.collection.db.scalar("select id from notes")
        note = self.collection.get_note(note_id)
        note.fields[1] = "later desktop edit"
        self.collection.update_note(note)
        with patch.object(self.collection, "add_note", side_effect=AssertionError("duplicate creation")):
            result = self.apply()
        self.assertEqual(note_id, result["creation"]["note"]["id"])
        self.assertEqual(self.change["payload"]["note"]["fields"], result["creation"]["note"]["flds"].split("\x1f"))
        self.assertEqual("later desktop edit", self.collection.get_note(note_id).fields[1])
        self.assertIsNone(self.collection.get_config(CREATION_RECEIPT))

    def test_completed_creation_preserves_one_step_native_undo(self):
        self.apply()
        self.collection.undo()
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from cards"))
        self.assertIsNone(self.collection.get_config(CREATION_RECEIPT))

    def test_matching_guid_without_native_creation_receipt_is_not_adopted(self):
        note = self.collection.new_note(self.model)
        note.guid = "creation-guid"
        note.fields = self.change["payload"]["note"]["fields"]
        self.collection.add_note(note, self.deck_id)

        with self.assertRaisesRegex(NoteConflict, "GUID"):
            self.apply()
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))
        with SyncState(self.path) as state:
            self.assertIsNone(state.delivery_result(1))

    def test_receipt_failure_rolls_back_native_note_and_cards_and_can_retry(self):
        original = self.collection.set_config

        def interrupt(key, value, **kwargs):
            original(key, value, **kwargs)
            if key == CREATION_RECEIPT:
                raise RuntimeError("interrupted native commit")

        with patch.object(self.collection, "set_config", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "interrupted native commit"):
                self.apply()
        self.restart_collection()
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from cards"))
        self.assertIsNone(self.collection.get_config(CREATION_RECEIPT))
        with SyncState(self.path) as state:
            self.assertIsNone(state.note_creation(1)["output"])
        self.apply()
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))
        self.assertEqual(2, self.collection.db.scalar("select count(*) from cards"))

    def test_native_undo_during_recovery_does_not_recreate_the_note(self):
        with patch.object(SyncState, "finish_delivery_change", side_effect=RuntimeError("lost sidecar write")):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.collection.undo()
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))
        self.assertIsNotNone(self.collection.get_config(CREATION_RECEIPT))
        with self.assertRaisesRegex(NoteConflict, "identity"):
            self.apply()
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))

    def test_sparse_cloze_numbers_are_native_ordinals(self):
        self.change = self.creation(self.collection.models.by_name("Cloze"), ["{{c1::猫}} {{c4::犬}}", ""], [0, 3])
        with SyncState(self.path) as state:
            state.put("delivery", None)
        self.remember()
        result = self.apply()
        self.assertEqual([0, 3], sorted(card["ord"] for card in result["creation"]["cards"]))

    def test_selected_template_override_uses_its_native_destination(self):
        reverse_deck = self.collection.decks.id("Japanese::Production")
        self.model["tmpls"][1]["did"] = reverse_deck
        self.collection.models.save(self.model)
        self.change = self.creation(self.model, ["猫", "cat"], [0, 1])
        self.change["payload"]["cards"][1]["anki_deck_id"] = reverse_deck
        with SyncState(self.path) as state:
            state.put("delivery", None)
        self.remember()
        with SyncState(self.path) as state:
            result = apply_note_creation(self.collection, state, self.change, {self.deck_id, reverse_deck})
        self.assertEqual({0: self.deck_id, 1: reverse_deck}, {card["ord"]: card["did"] for card in result["creation"]["cards"]})

    def test_sidecar_output_failure_recovers_native_commit_without_duplicate_creation(self):
        with patch.object(SyncState, "remember_note_creation_output", side_effect=RuntimeError("sidecar unavailable")):
            with self.assertRaisesRegex(RuntimeError, "sidecar unavailable"):
                self.apply()
        self.restart_collection()
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))
        self.assertIsNotNone(self.collection.get_config(CREATION_RECEIPT))
        note_id = self.collection.db.scalar("select id from notes")
        note = self.collection.get_note(note_id)
        note.fields[1] = "later desktop edit"
        self.collection.update_note(note)
        with patch.object(self.collection, "add_note", side_effect=AssertionError("duplicate creation")):
            result = self.apply()
        self.assertEqual(self.change["payload"]["note"]["fields"], result["creation"]["note"]["flds"].split("\x1f"))
        self.assertEqual("later desktop edit", self.collection.get_note(note_id).fields[1])
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))

    def test_native_undo_before_output_checkpoint_is_not_recreated(self):
        with patch.object(SyncState, "remember_note_creation_output", side_effect=RuntimeError("sidecar unavailable")):
            with self.assertRaisesRegex(RuntimeError, "sidecar unavailable"):
                self.apply()
        self.collection.undo()
        self.assertIsNotNone(self.collection.get_config(CREATION_RECEIPT))
        with self.assertRaisesRegex(NoteConflict, "identity"):
            self.apply()
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))
        with SyncState(self.path) as state:
            self.assertIsNone(state.note_creation(1)["output"])
            self.assertIsNone(state.delivery_result(1))

    def test_changed_native_receipt_is_not_used_to_recover_missing_sidecar_output(self):
        with patch.object(SyncState, "remember_note_creation_output", side_effect=RuntimeError("sidecar unavailable")):
            with self.assertRaisesRegex(RuntimeError, "sidecar unavailable"):
                self.apply()
        marker = self.collection.get_config(CREATION_RECEIPT)
        marker["output"]["creation"]["note"]["flds"] = "changed receipt"
        self.collection.set_config(CREATION_RECEIPT, marker)
        self.restart_collection()
        with self.assertRaisesRegex(NoteConflict, "changed receipt"):
            self.apply()
        with SyncState(self.path) as state:
            self.assertIsNone(state.note_creation(1)["output"])
            self.assertIsNone(state.delivery_result(1))
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))

    def test_failure_to_clear_receipt_retries_without_native_creation(self):
        set_config = self.collection.set_config
        def interrupt(key, value, **kwargs):
            if key == CREATION_RECEIPT and value is None:
                raise RuntimeError("cleanup interrupted")
            return set_config(key, value, **kwargs)
        with patch.object(self.collection, "set_config", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "cleanup interrupted"):
                self.apply()
        with SyncState(self.path) as state:
            result = state.delivery_result(1)
        self.assertIsNotNone(result)
        with patch.object(self.collection, "add_note", side_effect=AssertionError("duplicate creation")):
            self.assertEqual(result, self.apply())
        self.assertIsNone(self.collection.get_config(CREATION_RECEIPT))

    def test_a_foreign_receipt_cannot_be_consumed_or_overwritten(self):
        foreign = {"nonce": "another-attempt", "intent": "foreign", "output": "foreign"}
        self.collection.set_config(CREATION_RECEIPT, foreign)
        with self.assertRaisesRegex(NoteConflict, "Another native creation"):
            self.apply()
        self.assertEqual(foreign, self.collection.get_config(CREATION_RECEIPT))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))

    def test_recovery_refuses_replaced_source_identity_and_changed_prepared_output(self):
        with patch.object(SyncState, "finish_delivery_change", side_effect=RuntimeError("lost sidecar write")):
            with self.assertRaises(RuntimeError):
                self.apply()
        with SyncState(self.path) as state:
            output = state.note_creation(1)["output"]
            altered = copy.deepcopy(output)
            altered["creation"]["note"]["guid"] = "other-guid"
            with self.assertRaisesRegex(ValueError, "cannot change"):
                state.remember_note_creation_output(1, altered)
        note = self.collection.get_note(output["creation"]["note"]["id"])
        note.guid = "other-guid"
        self.collection.update_note(note)
        with self.assertRaisesRegex(NoteConflict, "identity"):
            self.apply()
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))

    def test_changed_definition_and_unselected_decks_cannot_create(self):
        with SyncState(self.path) as state:
            with self.assertRaisesRegex(NoteConflict, "selected"):
                apply_note_creation(self.collection, state, self.change, set())
        self.model["css"] += "\n.card { color: red; }"
        self.collection.models.save(self.model)
        with self.assertRaisesRegex(NoteConflict, "type changed"):
            self.apply()
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))

    def test_unexpected_native_siblings_roll_back_creation(self):
        self.change["payload"]["cards"].pop()
        with SyncState(self.path) as state:
            state.put("delivery", None)
        self.remember()
        with self.assertRaisesRegex(NoteConflict, "siblings"):
            self.apply()
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from cards"))

    def test_native_hook_mutation_cannot_change_the_frozen_request(self):
        original = self.collection.add_note
        request = copy.deepcopy(self.change)

        def alter(note, deck_id):
            note.fields[0] = "changed by another add-on"
            return original(note, deck_id)

        with patch.object(self.collection, "add_note", side_effect=alter):
            with self.assertRaisesRegex(NoteConflict, "content"):
                self.apply()
        self.assertEqual(request, self.change)
        self.assertEqual(0, self.collection.db.scalar("select count(*) from notes"))

    def test_delivery_does_not_acknowledge_unaccepted_creation_identities(self):
        delivery = self.delivery()
        delivery.apply(self.collection)
        delivery.client.batch.assert_not_called()
        with self.assertRaisesRegex(DeliveryConflict, "creation identity"):
            delivery.acknowledge()
        delivery.client.acknowledge.assert_not_called()
        with SyncState(self.path) as state:
            pending = state.get("delivery")
            batch = state.batch(pending["return_key"])
            self.assertEqual([state.delivery_result(1)["creation"]], batch["payload"]["creations"])
            self.assertEqual(0, state.get("delivery_cursor", 0))
        self.assertEqual(1, self.collection.db.scalar("select count(*) from notes"))

    def test_delivery_revisits_applied_creation_to_finish_native_receipt_cleanup(self):
        delivery = self.delivery()
        set_config = self.collection.set_config
        def interrupt(key, value, **kwargs):
            if key == CREATION_RECEIPT and value is None:
                raise RuntimeError("cleanup interrupted")
            return set_config(key, value, **kwargs)
        with patch.object(self.collection, "set_config", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "cleanup interrupted"):
                delivery.apply(self.collection)
        self.assertIsNotNone(self.collection.get_config(CREATION_RECEIPT))
        with patch.object(self.collection, "add_note", side_effect=AssertionError("duplicate creation")):
            delivery.apply(self.collection)
        self.assertIsNone(self.collection.get_config(CREATION_RECEIPT))
