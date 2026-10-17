from paths import SCRATCH
"""Capture daily-limit cases from disposable pinned native collections."""

from contextlib import closing, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time

from anki.buildinfo import version
from anki.collection import Collection


def rows(col, table):
    names = col.db.list("SELECT name FROM pragma_table_info(?)", table)
    return [dict(zip(names, row)) for row in col.db.all(f"SELECT * FROM {table} ORDER BY id")]


def capture(directory, name, *, review_limit=3, new_limit=3, reviews=4, interday=0,
            intraday=0, studied_new=0, studied_review=0, ignore=False, top=False,
            children=False, selected_child=False, today_override=None, minimum=0, configure=None, review_trace=False,
            answer_trace=False, days=0, limit_probe=False, session_ratings=None, visit=None):
    with closing(Collection(str(Path(directory) / f"{name}.anki2"))) as col:
        if days:
            col.crt -= days * 86400
            col.close()
            col.reopen()
        col.set_config("fsrs", True)
        root = col.decks.id("Japanese")
        decks = [col.decks.id("Japanese::A"), col.decks.id("Japanese::B")] if children else [root]
        preset = col.decks.config_dict_for_deck_id(root)
        preset["new"]["perDay"] = new_limit
        preset["rev"]["perDay"] = review_limit
        preset["newPerDayMinimum"] = minimum
        col.decks.update_config(preset)
        col.set_config("newCardsIgnoreReviewLimit", ignore)
        col.set_config("applyAllParentLimits", top)
        today = col.sched.today
        for deck_id in [root, *decks] if children else decks:
            deck = col.decks.get(deck_id)
            deck["newToday"] = [today, studied_new]
            deck["revToday"] = [today, studied_review]
            if deck_id != root:
                deck.update(newLimit=10, reviewLimit=10)
            if today_override is not None:
                deck["reviewLimitToday"] = {"today": today, "limit": today_override}
            col.decks.update(deck)
        for deck_id in decks:
            for queue, count in ((0, 5), (2, reviews), (3, interday), (1, intraday)):
                for index in range(count):
                    note = col.new_note(col.models.by_name("Basic"))
                    note.fields = [f"{deck_id}-{queue}-{index}", "answer"]
                    col.add_note(note, deck_id)
                    if queue:
                        card = note.cards()[0]
                        card.queue = queue
                        card.type = 2 if queue == 2 else 1
                        card.due = int(time.time()) - 60 if queue == 1 else today
                        card.ivl = 10 if queue == 2 else 0
                        card.reps = 1
                        card.left = 1001 if queue in (1, 3) else 0
                        col.update_card(card)
        if configure:
            configure(col, root, decks)
        selected = decks[0] if selected_child else root
        col.decks.select(selected)
        col.sched.reset()
        queued = col.sched.get_queued_cards(fetch_limit=100)
        remaining = col.sched.congratulations_info()
        timing = col._backend.sched_timing_today()
        assert timing.days_elapsed == days, (name, timing.days_elapsed, days)
        result = {"name": name, "now": time.time(), "selected": selected,
                "remaining": {"new": remaining.new_remaining, "review": remaining.review_remaining,
                              "learning": remaining.learn_remaining},
                "counts": {"new": queued.new_count, "learning": queued.learning_count,
                           "review": queued.review_count},
                "queued": [item.card.id for item in queued.cards],
                "metadata": {"crt": col.crt, "conf": col.all_config(),
                             "decks": {str(item.id): col.decks.get(item.id) for item in col.decks.all_names_and_ids()},
                             "dconf": {str(config["id"]): config for config in col.decks.all_config()},
                             "capture": {"captured_at": datetime.now(timezone.utc).isoformat(),
                                         "days_elapsed": timing.days_elapsed, "next_day_at": timing.next_day_at,
                                         "acknowledged_cursor": 0}},
                "models": {str(model["id"]): model for model in col.models.all()},
                "notes": rows(col, "notes"), "cards": rows(col, "cards")}
        if limit_probe and not queued.cards:
            try:
                for preset in deepcopy(result["metadata"]["dconf"]).values():
                    preset["new"]["perDay"] = preset["rev"]["perDay"] = 1_000_000
                    col.decks.update_config(preset)
                for deck in deepcopy(result["metadata"]["decks"]).values():
                    if not deck["dyn"]:
                        deck.update(newLimit=1_000_000, reviewLimit=1_000_000)
                        deck.pop("newLimitToday", None)
                        deck.pop("reviewLimitToday", None)
                        col.decks.update(deck)
                col.sched.reset()
                result["limit_backlog"] = bool(col.sched.get_queued_cards(fetch_limit=1).cards)
            finally:
                for preset in result["metadata"]["dconf"].values():
                    col.decks.update_config(deepcopy(preset))
                for deck in result["metadata"]["decks"].values():
                    col.decks.update(deepcopy(deck))
                col.sched.reset()
            assert rows(col, "cards") == result["cards"], "The limit probe changed cards"
        if answer_trace and queued.cards:
            card = col.sched.getCard()
            card.start_timer()
            col.sched.answerCard(card, 4)
            result["answer"] = {"card_id": card.id,
                                "cards": [{"id": row["id"], "queue": col.get_card(row["id"]).queue} for row in result["cards"]],
                                "queued": [item.card.id for item in col.sched.get_queued_cards(fetch_limit=100).cards]}
            col.undo()
            result["answer"]["undo_cards"] = [{"id": row["id"], "queue": col.get_card(row["id"]).queue} for row in result["cards"]]
        if review_trace:
            result["reviewed_order"] = []
            for _ in range(len(result["cards"])):
                card = col.sched.getCard()
                if card is None:
                    break
                result["reviewed_order"].append(card.id)
                card.start_timer()
                col.sched.answerCard(card, 4)
        if session_ratings is not None:
            from google.protobuf.json_format import MessageToDict

            result["session"] = []
            for rating in session_ratings:
                card = col.sched.getCard()
                if card is None:
                    break
                states = col._backend.get_scheduling_states(card.id)
                stamp = time.time()
                step = {"card_id": card.id, "rating": rating, "now": stamp,
                        "states": MessageToDict(states, preserving_proto_field_name=True)}
                card.start_timer()
                answer = col.sched.build_answer(card=card, states=states, rating=rating - 1)
                answer.answered_at_millis = int(stamp * 1000)
                col.sched.answer_card(answer)
                step["after"] = dict(zip(result["cards"][0], col.db.first("SELECT * FROM cards WHERE id = ?", card.id)))
                result["session"].append(step)
            following = col.sched.get_queued_cards(fetch_limit=1)
            result["session_next"] = [item.card.id for item in following.cards]
        if visit:
            result["timeline"] = visit(col)
        return result


def run():
    assert version == "26.08.1", version
    cases = {
        "review_cap": {},
        "spare_review_capacity": {"reviews": 1},
        "review_zero": {"review_limit": 0},
        "ignore_review_cap": {"ignore": True},
        "interday_cap": {"interday": 4, "reviews": 0},
        "interday_with_reviews": {"interday": 2, "reviews": 2},
        "intraday_outside_cap": {"intraday": 2, "review_limit": 0},
        "captured_reviews": {"studied_review": 2},
        "captured_new": {"studied_new": 2, "reviews": 0},
        "captured_new_with_reviews": {"studied_new": 2},
        "captured_new_ignore": {"studied_new": 2, "ignore": True},
        "captured_both": {"studied_review": 2, "studied_new": 1},
        "review_today_override": {"today_override": 1},
        "minimum_new": {"minimum": 2},
        "parent_session": {"children": True},
        "child_session": {"children": True, "selected_child": True},
        "child_from_top": {"children": True, "selected_child": True, "top": True},
        "parent_new": {"children": True, "reviews": 0},
        "child_new": {"children": True, "selected_child": True, "reviews": 0},
        "child_new_from_top": {"children": True, "selected_child": True, "top": True, "reviews": 0},
    }
    with tempfile.TemporaryDirectory(prefix="anki-queue-budget-", dir=SCRATCH) as directory:
        return {"version": version, "cases": [capture(directory, name, limit_probe=True, **options) for name, options in cases.items()]}


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = run()
    print(json.dumps(report))
