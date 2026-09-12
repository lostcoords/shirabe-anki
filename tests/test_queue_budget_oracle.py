from contextlib import closing
from pathlib import Path
import tempfile
import unittest

try:
    from anki.collection import Collection
    from queue_budget_oracle import rows
except ImportError:
    Collection = None


@unittest.skipUnless(Collection, "Anki's Python package is required")
class QueueBudgetOracleTests(unittest.TestCase):
    def test_inspection_preserves_native_review_undo(self):
        with tempfile.TemporaryDirectory() as directory, closing(Collection(str(Path(directory) / "collection.anki2"))) as col:
            col.set_config("fsrs", True)
            note = col.new_note(col.models.by_name("Basic"))
            note.fields = ["猫", "cat"]
            col.add_note(note, 1)
            card = col.sched.getCard()
            before = col.get_card(card.id).due
            col.sched.answerCard(card, 1)
            undo = col.undo_status()
            self.assertTrue(undo.undo)
            self.assertEqual(card.id, rows(col, "cards")[0]["id"])
            self.assertEqual(undo, col.undo_status())
            col.undo()
            self.assertEqual(before, col.get_card(card.id).due)


if __name__ == "__main__":
    unittest.main()
