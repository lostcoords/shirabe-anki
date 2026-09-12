import copy
import json
from pathlib import Path
import tempfile
import unittest

import support
from shirabe_addon.sync_cards import apply_card_change, CardConflict
from shirabe_addon.sync_reviews import card_row

try:
    from anki.collection import Collection
    from anki.buildinfo import version
except ImportError:
    Collection = None
    version = None


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncCardsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.collection = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        note = self.collection.new_note(self.collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        self.collection.add_note(note, self.collection.decks.id("Japanese"))
        self.card = note.cards()[0]
        self.before = card_row(self.collection, self.card.id)
        self.after = copy.deepcopy(self.before)
        self.source = {"card_before": self.before, "card_after": self.after, "note_guid": note.guid, "anki_type_id": note.mid}

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def apply(self):
        return apply_card_change(self.collection, {"anki": self.source})

    def test_remote_deck_move_preserves_local_suspension_and_retries_without_writing(self):
        destination = self.collection.decks.id("Moved")
        self.after["did"] = destination
        self.card.queue = -1
        self.collection.update_card(self.card)
        self.apply()
        current = card_row(self.collection, self.card.id)
        self.assertEqual(destination, current["did"])
        self.assertEqual(-1, current["queue"])
        self.apply()
        self.assertEqual(current, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from revlog"))

    def test_remote_suspension_preserves_an_independent_local_schedule(self):
        self.after["queue"] = -1
        self.card.type, self.card.queue, self.card.due, self.card.ivl, self.card.reps = 2, 2, 5, 5, 1
        self.collection.update_card(self.card)
        self.apply()
        current = card_row(self.collection, self.card.id)
        self.assertEqual((2, -1, 5, 5, 1), tuple(current[key] for key in ("type", "queue", "due", "ivl", "reps")))

    def test_remote_schedule_keeps_local_auxiliary_data(self):
        self.after.update(type=2, queue=2, due=4, ivl=4, reps=1, data='{"s":3.12345,"d":2.1181,"lrt":1780000000}')
        self.card.custom_data = '{"custom":true}'
        self.card.flags = 3
        self.collection.update_card(self.card)
        self.apply()
        current = self.collection.get_card(self.card.id)
        self.assertEqual(4, current.ivl)
        self.assertEqual(3, current.flags)
        self.assertEqual({"custom": True}, json.loads(current.custom_data))
        self.apply()

    def test_auxiliary_changes_use_the_public_card_fields(self):
        self.after["data"] = '{"pos":9,"dr":0.91,"decay":0.5,"cd":"{\\"custom\\":true}"}'
        self.after["flags"] = 4
        self.apply()
        current = self.collection.get_card(self.card.id)
        self.assertEqual(9, current.original_position)
        self.assertAlmostEqual(0.91, current.desired_retention, places=6)
        self.assertEqual(0.5, current.decay)
        self.assertEqual(4, current.flags)
        self.assertEqual({"custom": True}, json.loads(current.custom_data))

    def test_incoming_card_edit_can_be_undone_and_redone_without_losing_local_edits(self):
        self.card.flags = 3
        self.collection.update_card(self.card)
        self.after["queue"] = -1
        self.apply()
        self.assertEqual(-1, self.collection.get_card(self.card.id).queue)
        self.assertTrue(self.collection.undo_status().undo)
        self.collection.undo()
        current = self.collection.get_card(self.card.id)
        self.assertEqual(self.before["queue"], current.queue)
        self.assertEqual(3, current.flags)
        self.collection.redo()
        current = self.collection.get_card(self.card.id)
        self.assertEqual(-1, current.queue)
        self.assertEqual(3, current.flags)
        self.assertEqual(0, self.collection.db.scalar("select count(*) from revlog"))
        self.collection.undo()
        self.assertTrue(self.collection.undo_status().undo)
        self.collection.undo()
        self.assertEqual(self.before["flags"], self.collection.get_card(self.card.id).flags)

    def test_unchanged_card_receipt_preserves_the_previous_native_undo(self):
        self.card.flags = 3
        self.collection.update_card(self.card)
        self.apply()
        self.assertTrue(self.collection.undo_status().undo)
        self.collection.undo()
        self.assertEqual(self.before["flags"], self.collection.get_card(self.card.id).flags)

    def test_competing_schedules_remain_unchanged_for_resolution(self):
        self.after["due"] += 2
        self.card.due += 3
        self.collection.update_card(self.card)
        original = card_row(self.collection, self.card.id)
        with self.assertRaisesRegex(CardConflict, "schedule"):
            self.apply()
        self.assertEqual(original, card_row(self.collection, self.card.id))

    def test_unknown_source_data_rolls_back_instead_of_disappearing(self):
        self.after["data"] = '{"foreign":"keep"}'
        self.after["queue"] = -1
        with self.assertRaisesRegex(CardConflict, "preserve"):
            self.apply()
        self.assertEqual(self.before, card_row(self.collection, self.card.id))

    def test_note_identity_changes_and_malformed_rows_are_rejected_before_writes(self):
        for field, value in (("note_guid", "other"), ("anki_type_id", 999)):
            original = self.source[field]
            self.source[field] = value
            with self.assertRaises(CardConflict):
                self.apply()
            self.source[field] = original
        self.after["queue"] = True
        with self.assertRaises(CardConflict):
            self.apply()
        self.assertEqual(self.before, card_row(self.collection, self.card.id))
