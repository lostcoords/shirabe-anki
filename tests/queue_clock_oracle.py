"""Advance time inside a disposable native Anki process on macOS."""

from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo


def set_time(stamp):
    os.environ["SHIRABE_ORACLE_TIME"] = str(stamp)
    assert time.time() == stamp, "The process-local clock was not installed"


def run(directory, *, learning=False, invalidation=False):
    from anki.collection import Collection
    from anki.buildinfo import version

    assert version == "26.08.1", version
    stamp = int(datetime(2026, 9, 15, 12, tzinfo=timezone.utc).timestamp())
    set_time(stamp)
    with closing(Collection(str(directory / "clock.anki2"))) as col:
        col.set_config("fsrs", True)
        note = col.new_note(col.models.by_name("Basic"))
        note.fields = ["Clock", "answer"]
        col.add_note(note, 1)
        assert col.get_card(note.card_ids()[0]).mod == stamp, "Anki is not using the process-local clock"
        initial = col._backend.sched_timing_today()
        set_time(stamp + 86400)
        following = col._backend.sched_timing_today()
        assert following.days_elapsed == initial.days_elapsed + 1
    from queue_budget_oracle import capture
    from queue_learning_oracle import configure_session

    if learning:
        from queue_learning_oracle import run as learning_sessions

        set_time(stamp)
        return {**learning_sessions(), "clock": "process-local"}

    if invalidation:
        from queue_invalidation_oracle import run as queue_edits

        set_time(stamp)
        return {**queue_edits(), "clock": "process-local"}

    cases = []
    for mode in ("new", "learning", "relearning", "mixed"):
        for limit in (0, 600, 1200):
            set_time(stamp)
            cases.append(capture(directory, f"elapsed_{mode}_{limit}", new_limit=100, review_limit=100,
                                 configure=partial(configure_session, mode=mode, preset="ordinary", limit=limit),
                                 visit=partial(timeline, stamp=stamp)))
    calendars = [("UTC", "2026-09-15T03:59:30", 4), ("UTC", "2026-09-15T22:59:30", 23),
                 ("Asia/Tokyo", "2026-09-15T03:59:30", 4), ("Asia/Tokyo", "2026-09-15T11:59:30", 12),
                 ("America/New_York", "2026-03-08T01:59:30", 4), ("America/New_York", "2026-11-01T01:59:30", 4),
                 ("Europe/Madrid", "2026-03-29T01:59:30", 4), ("Europe/Madrid", "2026-10-25T02:59:30", 4)]
    for index, (zone, local, rollover) in enumerate(calendars):
        os.environ["TZ"] = zone
        time.tzset()
        stamp = int(datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone)).timestamp())
        set_time(stamp)
        case = capture(directory, f"calendar_{index}", new_limit=100, review_limit=100,
                       configure=partial(configure_calendar, rollover=rollover), visit=partial(timeline, stamp=stamp))
        case["timezone"] = zone
        cases.append(case)
    for zone, local, rollover in (calendars[1], calendars[3]):
        os.environ["TZ"] = zone
        time.tzset()
        stamp = int(datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone)).timestamp())
        for preset in ("short", "ordinary"):
            set_time(stamp)
            case = capture(directory, f"first_day_{rollover}_{preset}", new_limit=1, review_limit=100,
                           configure=partial(configure_calendar, rollover=rollover, mode="new", preset=preset),
                           visit=partial(timeline, stamp=stamp))
            case["timezone"] = zone
            cases.append(case)
    edges = []
    os.environ["TZ"] = "UTC"
    time.tzset()
    stamp = int(datetime(2026, 9, 15, 12, tzinfo=timezone.utc).timestamp())
    for kind in ("straddling", "empty", "new", "review"):
        for limit in (0, 1, 600, 1200):
            set_time(stamp)
            offsets = sorted({0, 1, 2, 59, 60, 61, max(0, limit - 1), limit, limit + 1})
            steps = [(offset, 0) for offset in offsets] + [(offsets[-1] + 1, 3)]
            edges.append(capture(directory, f"edge_{kind}_{limit}", new_limit=100, review_limit=100,
                                 configure=partial(configure_edges, kind=kind, limit=limit),
                                 visit=partial(timeline, stamp=stamp, steps=steps)))
    return {"version": version, "initial_day": initial.days_elapsed, "following_day": following.days_elapsed,
            "cases": cases, "edges": edges}


def configure_edges(col, root, decks, *, kind, limit):
    col.remove_notes(col.db.list("SELECT id FROM notes"))
    col.set_config("collapseTime", limit)
    offsets = sorted({-1, 0, 1, limit - 1, limit, limit + 1}) if kind == "straddling" else [limit + 1, limit + 2, limit + 60]
    for index, offset in enumerate(offsets):
        note = col.new_note(col.models.by_name("Basic"))
        note.fields = [f"learning boundary {index}", "answer"]
        col.add_note(note, root)
        card = note.cards()[0]
        card.type = card.queue = card.reps = 1
        card.left = 2
        card.due = int(time.time()) + offset
        col.update_card(card)
    if kind in ("new", "review"):
        note = col.new_note(col.models.by_name("Basic"))
        note.fields = ["held ordinary card", "answer"]
        col.add_note(note, root)
        if kind == "review":
            card = note.cards()[0]
            card.type = card.queue = 2
            card.reps = 1
            card.ivl = 10
            card.due = col.sched.today
            col.update_card(card)


def configure_calendar(col, root, decks, *, rollover, mode="mixed", preset="ordinary"):
    from queue_learning_oracle import configure_session

    col.set_config("rollover", rollover)
    col.close()
    col.reopen()
    configure_session(col, root, decks, mode=mode, preset=preset, limit=1200)


def timeline(col, *, stamp, steps=None):
    from queue_budget_oracle import rows

    events = []
    steps = steps or [(0, 0), (0, 1), (5, 0), (60, 0), (300, 2), (600, 3),
                      (900, 1), (1200, 3), (3600, 4), (86400, 0), (86400, 3)]
    for elapsed, rating in steps:
        set_time(stamp + elapsed)
        queued = col.sched.get_queued_cards(fetch_limit=100)
        event = {"now": time.time(), "rating": rating, "queued": [item.card.id for item in queued.cards],
                 "counts": {"new": queued.new_count, "learning": queued.learning_count, "review": queued.review_count}}
        if rating and queued.cards:
            card = col.get_card(queued.cards[0].card.id)
            card.start_timer()
            answer = col.sched.build_answer(card=card, states=queued.cards[0].states, rating=rating - 1)
            col.sched.answer_card(answer)
            event["after"] = next(row for row in rows(col, "cards") if row["id"] == card.id)
            answered = col.sched.get_queued_cards(fetch_limit=100)
            event["answered"] = {"queued": [item.card.id for item in answered.cards],
                                 "counts": {"new": answered.new_count, "learning": answered.learning_count, "review": answered.review_count}}
            history = rows(col, "revlog")
            col.undo()
            restored = col.sched.get_queued_cards(fetch_limit=100)
            event["undo"] = {"queued": [item.card.id for item in restored.cards],
                             "counts": {"new": restored.new_count, "learning": restored.learning_count, "review": restored.review_count}}
            assert col.undo_status().redo, "Inspecting the queue discarded native Redo"
            col.redo()
            assert next(row for row in rows(col, "cards") if row["id"] == card.id) == event["after"]
            assert rows(col, "revlog") == history, "Native Redo did not restore the original review history"
            redone = col.sched.get_queued_cards(fetch_limit=100)
            event["redo"] = {"queued": [item.card.id for item in redone.cards],
                             "counts": {"new": redone.new_count, "learning": redone.learning_count, "review": redone.review_count}}
            # Native Redo and a fresh answer can produce different live learning counts.
            col.undo()
            col.sched.answer_card(answer)
            reanswered = col.sched.get_queued_cards(fetch_limit=100)
            event["reanswered"] = {"queued": [item.card.id for item in reanswered.cards],
                                   "counts": {"new": reanswered.new_count, "learning": reanswered.learning_count, "review": reanswered.review_count}}
        events.append(event)
    return events


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        with redirect_stdout(sys.stderr):
            report = run(Path(sys.argv[2]), learning="--learning" in sys.argv,
                         invalidation="--invalidation" in sys.argv)
        print(json.dumps(report))
    else:
        assert sys.platform == "darwin", "The native clock oracle currently requires macOS"
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="anki-clock-oracle-", dir=root / "tmp") as directory:
            library = Path(directory) / "clock.dylib"
            subprocess.run(["clang", "-dynamiclib", "-Wall", "-Werror", "-O2",
                            str(Path(__file__).with_name("native_clock.c")), "-o", str(library)], check=True)
            environment = {**os.environ, "TZ": "UTC", "DYLD_INSERT_LIBRARIES": str(library)}
            mode = [argument for argument in sys.argv[1:] if argument in ("--learning", "--invalidation")]
            subprocess.run([sys.executable, __file__, "--child", directory, *mode], env=environment, check=True)
