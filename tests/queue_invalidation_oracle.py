from paths import SCRATCH
"""Capture edits to live learning queues in disposable native collections."""

from contextlib import redirect_stdout
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
import time

from queue_budget_oracle import capture, version
from queue_bury_oracle import configure as configure_burial
from queue_learning_oracle import configure_session


def timeline(col, *, action, position):
    ids = col.db.list("SELECT id FROM cards ORDER BY id")
    card = col.sched.getCard()
    assert card.id == ids[0]
    card.start_timer()
    col.sched.answerCard(card, 1)
    before = col.sched.get_queued_cards(fetch_limit=100)
    assert before.cards[0].card.id == ids[1]
    now = time.time()
    target = col.get_card(before.cards[position].card.id)
    if action == "note_edit":
        note = target.note()
        note.fields[0] = "edited while studying"
        col.update_note(note)
    elif action == "due_edit":
        target.due += 10
        col.update_card(target)
    elif action == "suspend":
        col.sched.suspend_cards([target.id])
    elif action == "bury":
        col.sched.bury_cards([target.id], manual=True)
    elif action == "remove":
        col.remove_notes([target.nid])
    elif action == "flag":
        col.set_user_flag_for_cards(3, [target.id])
    else:
        raise ValueError(action)
    following = col.sched.get_queued_cards(fetch_limit=100)
    return {"now": now, "first": ids[0], "target": target.id, "action": action,
            "before": [item.card.id for item in before.cards],
            "queued": [item.card.id for item in following.cards],
            "counts": {"new": following.new_count, "learning": following.learning_count,
                       "review": following.review_count}}


def configure_siblings(col, root, decks, *, other, mode, staggered, flags):
    configure_burial(col, root, decks, queues=(1, other), flags=flags)
    config = col.decks.config_dict_for_deck_id(root)
    config["new"]["delays"] = [1, 10]
    config["lapse"]["delays"] = [1, 10]
    col.decks.update_config(config)
    for index, card_id in enumerate(col.db.list("SELECT id FROM cards WHERE queue = 1 ORDER BY id")):
        card = col.get_card(card_id)
        card.type = 3 if mode == "relearning" else 1
        card.left = 2
        card.ivl = 2 if mode == "relearning" else 0
        if staggered:
            card.due = int(time.time()) + (index + 1) * 150
        col.update_card(card)


def queue_state(col):
    queue = col.sched.get_queued_cards(fetch_limit=100)
    return {"queued": [item.card.id for item in queue.cards],
            "counts": {"new": queue.new_count, "learning": queue.learning_count, "review": queue.review_count},
            "buried": col.db.list("SELECT id FROM cards WHERE queue = -2 ORDER BY id")}


def burial_timeline(col):
    events = []
    for rating in [1, 2, 1, 3, 2, 3, 1, 3, 3, 2, 3, 3] + [4] * 12:
        before = queue_state(col)
        if not before["queued"]:
            break
        card = col.sched.getCard()
        card.start_timer()
        states = col._backend.get_scheduling_states(card.id)
        answer = col.sched.build_answer(card=card, states=states, rating=rating - 1)
        event = {"now": time.time(), "card_id": card.id, "rating": rating, "before": before}
        col.sched.answer_card(answer)
        event["after"] = queue_state(col)
        col.undo()
        event["undo"] = queue_state(col)
        col.sched.answer_card(answer)
        event["reanswered"] = queue_state(col)
        events.append(event)
    return events


def run():
    assert version == "26.08.1", version
    with tempfile.TemporaryDirectory(prefix="anki-queue-invalidation-", dir=SCRATCH) as directory:
        cases = [capture(directory, f"{mode}_{action}_{position}", new_limit=100, review_limit=100,
                         configure=partial(configure_session, mode=mode, preset="ordinary", limit=1200, staggered=True),
                         visit=partial(timeline, action=action, position=position))
                 for mode in ("learning", "relearning")
                 for action in ("note_edit", "due_edit", "suspend", "bury", "remove", "flag")
                 for position in range(3)]
        siblings = [capture(directory, f"siblings_{mode}_{other}_{int(staggered)}_{flags}", new_limit=100, review_limit=100,
                            configure=partial(configure_siblings, other=other, mode=mode, staggered=staggered, flags=flags),
                            visit=burial_timeline)
                    for mode in ("learning", "relearning") for other in (0, 1, 2, 3)
                    for staggered in (False, True) for flags in (0, 7)]
    return {"version": version, "cases": cases, "siblings": siblings}


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = run()
    print(json.dumps(report))
