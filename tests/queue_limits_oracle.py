"""Read new-card limits from a disposable pinned Anki collection."""

from contextlib import closing, redirect_stdout
import json
from pathlib import Path
import tempfile
import time
import sys

from anki.buildinfo import version
from anki.collection import Collection


def run():
    assert version == "26.08.1", version
    root = Path(__file__).resolve().parents[2] / "tmp"
    with tempfile.TemporaryDirectory(prefix="anki-limit-oracle-", dir=root) as directory:
        with closing(Collection(str(Path(directory) / "collection.anki2"))) as collection:
            deck_id = collection.decks.id("Limits")
            collection.decks.select(deck_id)
            preset = collection.decks.config_dict_for_deck_id(deck_id)
            preset["new"]["perDay"] = 3
            preset["rev"]["perDay"] = 100
            collection.decks.update_config(preset)
            for index in range(5):
                note = collection.new_note(collection.models.by_name("Basic"))
                note.fields = [f"card {index}", "answer"]
                collection.add_note(note, deck_id)
            today = collection.sched.today
            cases = []

            def capture(name, limit=None):
                new_count, _, _ = collection.sched.counts()
                cases.append({"name": name, "new": new_count, "now": time.time(),
                              "limit": limit,
                              "metadata": {"crt": collection.crt, "conf": collection.all_config(),
                                           "decks": {str(deck_id): collection.decks.get(deck_id)},
                                           "dconf": {str(preset["id"]): collection.decks.get_config(preset["id"])}}})

            for name, limit, daily in (
                ("preset", None, None),
                ("deck", 2, None),
                ("zero", 0, None),
                ("today", 2, {"today": today, "limit": 1}),
                ("today_zero", 2, {"today": today, "limit": 0}),
                ("expired", 2, {"today": today - 1, "limit": 1}),
            ):
                deck = collection.decks.get(deck_id)
                deck.update(newLimit=limit, newLimitToday=daily)
                collection.decks.update(deck)
                collection.sched.reset()
                capture(name)
            deck = collection.decks.get(deck_id)
            deck.update(newLimit=None, newLimitToday=None)
            collection.decks.update(deck)
            collection.sched.reset()
            card = collection.sched.getCard()
            card.start_timer()
            collection.sched.answerCard(card, 4)
            capture("native_review", limit=3)
            preset["new"]["perDay"] = 2
            collection.decks.update_config(preset)
            capture("changed_preset_after_review", limit=2)
            return {"version": version, "cases": cases}


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = run()
    print(json.dumps(report))
