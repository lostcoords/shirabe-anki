from paths import SCRATCH
"""Capture sibling eligibility from disposable native Anki collections."""

from contextlib import redirect_stdout
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
import time

from queue_budget_oracle import capture as capture_collection, version

capture = partial(capture_collection, answer_trace=True, review_trace=True, limit_probe=True)


def configure(col, root, decks, *, queues, flags, future=False, second_flags=None):
    col.remove_notes(col.db.list("SELECT id FROM notes"))
    if future:
        col.set_config("collapseTime", 0)
    preset = col.decks.config_dict_for_deck_id(root)
    preset.update(newMix=2, interdayLearningMix=1, reviewOrder=0, newSortOrder=1)
    preset["new"]["bury"] = bool(flags & 1)
    preset["rev"]["bury"] = bool(flags & 2)
    preset["buryInterdayLearning"] = bool(flags & 4)
    col.decks.update_config(preset)
    if second_flags is not None:
        child = col.decks.add_config("Other burial settings", clone_from=preset)
        child["new"]["bury"] = bool(second_flags & 1)
        child["rev"]["bury"] = bool(second_flags & 2)
        child["buryInterdayLearning"] = bool(second_flags & 4)
        col.decks.update_config(child)
        deck = col.decks.get(decks[1])
        deck["conf"] = child["id"]
        col.decks.update(deck)
    for index in range(3):
        note = col.new_note(col.models.by_name("Basic (and reversed card)"))
        note.fields = [f"siblings {index}", "answer"]
        col.add_note(note, decks[0])
        for ordinal, (card, queue) in enumerate(zip(note.cards(), queues)):
            offset = index * 2 + ordinal
            card.queue = queue
            card.type = {0: 0, 1: 1, 2: 2, 3: 3}[queue]
            card.due = (offset + 1 if queue == 0 else
                        col._backend.sched_timing_today().next_day_at - 1 if queue == 1 and future else
                        int(time.time()) - 60 + offset if queue == 1 else
                        col.sched.today + 1 + index if future and ordinal == 0 else
                        col.sched.today - 10 + offset)
            if second_flags is not None:
                card.did = decks[ordinal]
            card.ivl = 10 if queue in (2, 3) else 0
            card.reps = int(queue != 0)
            card.left = 1001 if queue in (1, 3) else 0
            col.update_card(card)


def run():
    assert version == "26.08.1", version
    pairs = [(0, 0), (2, 2), (3, 3), (0, 2), (0, 3),
             (2, 3), (1, 2), (1, 3), (1, 0), (1, 1)]
    with tempfile.TemporaryDirectory(prefix="anki-bury-oracle-", dir=SCRATCH) as directory:
        cases = [
            capture(directory, f"bury_{left}_{right}_{flags}", new_limit=100, review_limit=100,
                    configure=partial(configure, queues=(left, right), flags=flags))
            for left, right in pairs for flags in range(8)]
        for other in (0, 2, 3):
            for flags in range(8):
                cases.append(capture(directory, f"future_1_{other}_{flags}", new_limit=100, review_limit=100,
                                     configure=partial(configure, queues=(1, other), flags=flags, future=True)))
        for future_queue in (2, 3):
            for other in (0, 1, 2, 3):
                for flags in range(8):
                    cases.append(capture(directory, f"future_{future_queue}_{other}_{flags}", new_limit=100, review_limit=100,
                                         configure=partial(configure, queues=(future_queue, other), flags=flags, future=True)))
        for queue in (0, 2):
            cases.append(capture(directory, f"limited_{queue}", new_limit=2, review_limit=2,
                                 configure=partial(configure, queues=(queue, queue), flags=7)))
        for queue in (0, 2, 3):
            for first, second in ((0, 7), (7, 0)):
                cases.append(capture(directory, f"split_{queue}_{first}_{second}", new_limit=100, review_limit=100,
                                     children=True, configure=partial(configure, queues=(queue, queue),
                                                                       flags=first, second_flags=second)))
        return {"version": version, "cases": cases}


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = run()
    print(json.dumps(report))
