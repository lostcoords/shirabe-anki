"""Capture learning-ahead queues from disposable native Anki collections."""

from contextlib import redirect_stdout
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
import time

from queue_budget_oracle import capture, version


def configure(col, root, decks, *, limit, ordinary, due):
    col.remove_notes(col.db.list("SELECT id FROM notes"))
    col.set_config("collapseTime", limit)
    now = int(time.time())
    queues = [(1, now + delay) for delay in ((-60,) if due else ()) + (300, 900, 1800)]
    if ordinary in ("new", "both"):
        queues.extend([(0, 1), (0, 2)])
    if ordinary in ("review", "both"):
        queues.extend([(2, col.sched.today), (2, col.sched.today)])
    for index, (queue, scheduled) in enumerate(queues):
        note = col.new_note(col.models.by_name("Basic"))
        note.fields = [f"learning {index}", "answer"]
        col.add_note(note, root)
        card = note.cards()[0]
        card.queue = queue
        card.type = queue
        card.due = scheduled
        card.ivl = 10 if queue == 2 else 0
        card.reps = int(queue != 0)
        card.left = 1001 if queue == 1 else 0
        col.update_card(card)


def run():
    assert version == "26.08.1", version
    with tempfile.TemporaryDirectory(prefix="anki-learning-oracle-", dir=Path(__file__).resolve().parents[2] / "tmp") as directory:
        cases = [capture(directory, f"ahead_{limit}_{ordinary}_{int(due)}", new_limit=100, review_limit=100,
                         configure=partial(configure, limit=limit, ordinary=ordinary, due=due), review_trace=True)
                 for limit in (0, 600, 1200) for ordinary in ("none", "new", "review", "both") for due in (False, True)]
        cases.extend(capture(directory, f"attempted_first_{delay}", new_limit=100, review_limit=100,
                             configure=partial(configure_priority, delay=delay), review_trace=True)
                     for delay in (-100, 300))
        sessions = [capture(directory, f"session_{mode}_{preset}_{limit}", new_limit=100, review_limit=100,
                            configure=partial(configure_session, mode=mode, preset=preset, limit=limit),
                            session_ratings=[1, 2, 1, 3, 2, 3, 1, 3, 3, 2, 3, 3] + [4] * 20)
                    for mode in ("new", "learning", "relearning", "mixed")
                    for preset in ("short", "ordinary", "interday") for limit in (0, 600, 1200)]
        sessions.extend(capture(directory, f"staggered_{mode}_{preset}_{limit}", new_limit=100, review_limit=100,
                                configure=partial(configure_session, mode=mode, preset=preset, limit=limit, staggered=True),
                                session_ratings=[1, 1, 2, 1, 3, 2, 3] + [4] * 12)
                        for mode in ("learning", "relearning")
                        for preset in ("short", "ordinary", "interday") for limit in (600, 1200))
        return {"version": version, "cases": cases, "sessions": sessions}


def configure_session(col, root, decks, *, mode, preset, limit, staggered=False):
    col.remove_notes(col.db.list("SELECT id FROM notes"))
    col.set_config("collapseTime", limit)
    config = col.decks.config_dict_for_deck_id(root)
    steps = {"short": [0.1, 0.2], "ordinary": [1, 10], "interday": [1, 10, 1440]}[preset]
    config["new"]["delays"] = steps
    config["lapse"]["delays"] = steps
    col.decks.update_config(config)
    kinds = [0, 1, 2, 3] if mode == "mixed" else [{"new": 0, "learning": 1, "relearning": 3}[mode]] * 3
    now = int(time.time())
    for index, kind in enumerate(kinds):
        note = col.new_note(col.models.by_name("Basic"))
        note.fields = [f"{mode} {index}", "answer"]
        col.add_note(note, root)
        card = note.cards()[0]
        card.type = kind
        card.queue = 1 if kind == 3 else kind
        card.due = now - 60 + index if kind in (1, 3) else (col.sched.today if kind == 2 else index + 1)
        if staggered:
            card.due = now + (index + 1) * 300
        card.ivl = 10 if kind == 2 else (2 if kind == 3 else 0)
        card.left = len(steps) if kind in (1, 3) else 0
        card.reps = int(kind != 0)
        col.update_card(card)


def configure_priority(col, root, decks, *, delay):
    configure_session(col, root, decks, mode="learning", preset="ordinary", limit=1200, staggered=True)
    for index, card_id in enumerate(col.db.list("SELECT id FROM cards ORDER BY id")):
        card = col.get_card(card_id)
        card.due = int(time.time()) + delay + index
        card.reps = int(index != 0)
        col.update_card(card)


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = run()
    print(json.dumps(report))
