from paths import SCRATCH
"""Compare actual queued card IDs for native display-order settings."""

from contextlib import redirect_stdout
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
import time

from queue_budget_oracle import capture, version


def configure(col, root, decks, *, options, siblings=False, child_options=None, tied=False, positions_same=False,
              memory=None):
    preset = col.decks.config_dict_for_deck_id(root)
    preset.update(newMix=1, interdayLearningMix=1, reviewOrder=9, newSortOrder=1)
    preset.update(options)
    col.decks.update_config(preset)
    if child_options:
        child = col.decks.add_config("Child", clone_from=preset)
        child.update(child_options)
        col.decks.update_config(child)
        for deck_id in decks:
            deck = col.decks.get(deck_id)
            deck["conf"] = child["id"]
            col.decks.update(deck)
    if siblings:
        for index in range(3):
            note = col.new_note(col.models.by_name("Basic (and reversed card)"))
            note.fields = [f"sibling {index}", "answer"]
            col.add_note(note, decks[0])
    for index, card_id in enumerate(col.db.list("SELECT id FROM cards ORDER BY id")):
        card = col.get_card(card_id)
        if card.queue == 0:
            card.due = 42 if positions_same else 100 - index
        elif card.queue in (2, 3):
            card.due = col.sched.today - index
            card.ivl = 10 + (index * 7) % 11
            card.factor = 1300 + (index * 11) % 17 * 100
            if card.queue == 3:
                card.type = 3
        if tied:
            card.queue = card.type = 2
            card.due = col.sched.today
            card.ivl = 10
            card.factor = 2500
            card.reps = 1
            card.left = 0
        if memory:
            card.factor = 1300 + (index * 7) % 17 * 100
            if memory != "tied":
                card.due = col.sched.today - (index * 13) % 23
                card.ivl = (index * 7) % 31
        col.update_card(card)
        if tied:
            col.db.execute("UPDATE cards SET mod = ? WHERE id = ?", 1700000000 + index * 123, card.id)
        if memory:
            data = {"s": 0.5 + (index * 11) % 41, "d": 1 + (index * 3) % 10,
                    "dr": 0.7 + (index % 3) * 0.1, "decay": 0.154 if index % 2 else 0.5,
                    "lrt": int(time.time()) - (index - 3) * 43200}
            if index % 5 == 0:
                data = {}
            elif index % 5 == 1:
                data.pop("s")
            elif index % 5 == 2:
                data.pop("dr")
            elif index % 5 == 3:
                data.pop("decay")
            if memory == "without_lrt":
                data.pop("lrt", None)
            elif memory == "tied":
                data = {"s": 15, "d": 5, "dr": 0.9, "decay": 0.5,
                        "lrt": int(time.time()) - 20 * 86400}
            col.db.execute("UPDATE cards SET data = ? WHERE id = ?", json.dumps(data), card.id)


def configure_names(col, root, decks, *, names, kind, mode):
    for index, deck_id in enumerate(decks):
        col.decks.rename(deck_id, f"Japanese::Original {index}")
    for deck_id, name in zip(decks, names):
        col.decks.rename(deck_id, f"Japanese::{name}")
    options = {"newGatherPriority": mode} if kind == "new" else {"reviewOrder": mode}
    configure(col, root, decks, options=options, tied=kind == "review", positions_same=True)
    if kind == "new":
        col.remove_notes(col.db.list("SELECT nid FROM cards WHERE queue != 0"))


def run():
    assert version == "26.08.1", version
    cases = {}
    for field, values in (("newMix", range(3)), ("interdayLearningMix", range(3)),
                          ("reviewOrder", range(13)),
                          ("newGatherPriority", range(6)), ("newSortOrder", range(5))):
        for value in values:
            cases[f"{field}_{value}"] = ({field: value}, field == "newSortOrder")
    with tempfile.TemporaryDirectory(prefix="anki-order-oracle-", dir=SCRATCH) as directory:
        captured = [
            capture(directory, name, new_limit=20, review_limit=100, interday=3, review_trace=True,
                    configure=partial(configure, options=options, siblings=siblings))
            for name, (options, siblings) in cases.items()]
        for selected_child in (False, True):
            captured.append(capture(directory, f"selected_child_{selected_child}", new_limit=20, review_limit=100,
                                    children=True, selected_child=selected_child,
                                    configure=partial(configure, options={"newMix": 2}, child_options={"newMix": 1})))
        for mode in range(6):
            captured.append(capture(directory, f"parent_gather_{mode}", new_limit=3, review_limit=100,
                                    children=True, configure=partial(configure, options={"newGatherPriority": mode})))
        for mode in (0, 1, 2, 3, 4, 5, 6, 8, 9, 10):
            captured.append(capture(directory, f"tied_review_{mode}", new_limit=20, review_limit=100,
                                    configure=partial(configure, options={"reviewOrder": mode}, siblings=True, tied=True)))
        for mode in range(3):
            captured.append(capture(directory, f"tied_position_{mode}", new_limit=20, review_limit=100,
                                    configure=partial(configure, options={"newGatherPriority": mode}, siblings=True, positions_same=True)))
        for days in (1, 7, 10000):
            for gather in (3, 4, 5):
                for sort in (2, 3, 4):
                    captured.append(capture(directory, f"random_{days}_{gather}_{sort}", new_limit=5, review_limit=100, days=days,
                                            configure=partial(configure, options={"newGatherPriority": gather, "newSortOrder": sort},
                                                              siblings=True)))
        for memory in ("mixed", "without_lrt", "tied"):
            for days in (0, 10000):
                for mode in (5, 6, 7, 11, 12):
                    captured.append(capture(directory, f"memory_{memory}_{days}_{mode}",
                                            new_limit=20, review_limit=100, review_trace=True, days=days,
                                            configure=partial(configure, options={"reviewOrder": mode}, siblings=True,
                                                              tied=True, memory=memory)))
        for new_count in range(6):
            for review_count in range(6):
                captured.append(capture(directory, f"mix_{new_count}_{review_count}", new_limit=new_count,
                                        review_limit=100, reviews=review_count, review_trace=True,
                                        configure=partial(configure, options={"newMix": 0})))
        names = [capture(directory, f"names_{index}_{kind}_{mode}", new_limit=3, review_limit=100,
                         children=True, review_trace=True,
                         configure=partial(configure_names, names=names, kind=kind, mode=mode))
                 for index, names in enumerate((("a", "B"), ("ä", "Ö"), ("ß", "TT"), ("a::child", "a child")))
                 for kind, mode in (("new", 0), ("new", 5), ("review", 1), ("review", 2))]
        return {"version": version, "cases": captured, "names": names}


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = run()
    print(json.dumps(report))
