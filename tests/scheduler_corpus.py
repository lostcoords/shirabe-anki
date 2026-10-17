from paths import SHIRABE
"""Capture learning-step decisions from a disposable installed-Anki collection."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from fixture_deck import REPO, require_anki, save_json


def card_graph(col):
    return [[*row[:-1], json.loads(row[-1] or "{}")]
            for row in col.db.all("SELECT id, did, type, queue, due, ivl, reps, left, data FROM cards ORDER BY id")]


def capture_answers(col, cases):
    from anki.scheduler_pb2 import CardAnswer
    from google.protobuf.json_format import MessageToDict

    baseline = card_graph(col)
    assert {sample["card_id"] for sample in cases} <= {row[0] for row in baseline}
    for sample in cases:
        card_id = sample["card_id"]
        answers = {}
        captured_states = {}
        reviewed_at = {}
        undo_tags = {}
        for name, rating in (("again", CardAnswer.AGAIN), ("hard", CardAnswer.HARD),
                             ("good", CardAnswer.GOOD), ("easy", CardAnswer.EASY)):
            card = col.get_card(card_id)
            col.decks.select(1)
            col.decks.select(card.did)
            # Anki only load-balances scheduling states after building a study queue.
            col.sched.get_queued_cards(fetch_limit=1)
            reviewed_at[name] = int(time.time())
            states = col._backend.get_scheduling_states(card_id)
            serialized = MessageToDict(states, preserving_proto_field_name=True)
            captured_states[name] = serialized[name]
            card.start_timer()
            answer = col.sched.build_answer(card=card, states=states, rating=rating)
            # The answer API requires the queue head when a queue is present.
            col.decks.select(1)
            col.sched.answer_card(answer)
            answered = col.get_card(card_id)
            answers[name] = {"type": answered.type, "queue": answered.queue, "left": answered.left,
                             "ivl": answered.ivl, "due": answered.due, "did": answered.did,
                             "odid": answered.odid, "odue": answered.odue, "lapses": answered.lapses,
                             "tags": col.get_note(answered.nid).tags}
            col.undo()
            undo_tags[name] = col.get_note(answered.nid).tags
        sample.update(states=captured_states, answers=answers, reviewed_at=reviewed_at, undo_tags=undo_tags)
        current = card_graph(col)
        assert current == baseline, (sample["name"], [(before, after) for before, after in zip(baseline, current) if before != after])


def load_balance_cases(col, cases, now):
    for label, percentages, spacing, bury in (
        ("mixed-dense", [1, 0.5, 0, 1, 0.5, 0, 1], 1, False),
        ("mixed-sparse", [1, 0.5, 0, 1, 0.5, 0, 1], 3, False),
        ("minimum", [0] * 7, 1, False),
        ("minimum-sparse", [0] * 7, 3, False),
        ("fractional", [1, 0.25, 0, 0.75, 1, 0, 1], 1, False),
        ("cutoff", [1] * 7, 1, False),
        ("siblings", [1] * 7, 1, True),
        ("siblings-disabled", [1] * 7, 1, False),
        ("siblings-other-preset", [1] * 7, 1, True),
    ):
        preset = col.decks.add_config(label)
        preset["easyDaysPercentages"] = percentages
        preset["rev"]["bury"] = bury
        col.decks.update_config(preset)
        did = col.decks.id(label)
        deck = col.decks.get(did)
        deck["conf"] = preset["id"]
        col.decks.save(deck)
        sibling_did = did
        if label == "siblings-other-preset":
            sibling_preset = col.decks.add_config(f"{label}/other")
            sibling_did = col.decks.id(f"{label}/other")
            sibling_deck = col.decks.get(sibling_did)
            sibling_deck["conf"] = sibling_preset["id"]
            col.decks.save(sibling_deck)
        for offset in range(1, 90, spacing):
            for index in range(1 + offset % 3):
                note = col.new_note(col.models.by_name("Basic"))
                note.fields = [f"{label}/workload/{offset}/{index}", "answer"]
                col.add_note(note, did)
                card = note.cards()[0]
                card.type = card.queue = 2
                card.due = col.sched.today + offset
                card.ivl = 20
                card.reps = 10
                col.update_card(card)
        for index in range(16):
            note = col.new_note(col.models.by_name("Basic (and reversed card)" if label.startswith("siblings") else "Basic"))
            note.fields = [f"{label}/review/{index}", "answer"]
            col.add_note(note, did)
            siblings = note.cards()
            for ordinal, card in enumerate(siblings):
                card.did = sibling_did if ordinal else did
                card.type = card.queue = 2
                card.due = col.sched.today + (20 + index % 5 if ordinal else 0)
                card.ivl = 10
                card.reps = 10 + index
                col.update_card(card)
                col.db.execute("update cards set data=? where id=?",
                    json.dumps({"s": 10, "d": 5, "lrt": now - 864000}), card.id)
            if label == "cutoff":
                target = 90.01 + index * 0.06
                lower, upper = 0.001, 36500.0
                for _ in range(32):
                    stability = (lower + upper) / 2
                    col.db.execute("update cards set data=? where id=?",
                        json.dumps({"s": stability, "d": 5, "lrt": now - 864000}), siblings[0].id)
                    memory = col._backend.get_scheduling_states(siblings[0].id).good.normal.review.memory_state
                    if abs(memory.stability - target) < 0.0001:
                        break
                    if memory.stability < target:
                        lower = stability
                    else:
                        upper = stability
                assert abs(memory.stability - target) < 0.0001, (target, memory.stability)
            cases.append({"name": note.fields[0], "card_id": siblings[0].id, "remaining": 0})


def filtered_home_cases(col, cases, now):
    filtered_ids = []
    for reschedule in (False, True):
        label = f"filtered-home/{reschedule}"
        preset = col.decks.add_config(label)
        preset["desiredRetention"] = 0.95
        preset["rev"]["maxIvl"] = 9
        col.decks.update_config(preset)
        did = col.decks.id(label)
        deck = col.decks.get(did)
        deck["conf"] = preset["id"]
        col.decks.save(deck)
        filtered_id = col.decks.new_filtered(f"{label}/session")
        filtered_ids.append(filtered_id)
        for state, offset in ((0, 1), (2, -1), (2, 5)):
            note = col.new_note(col.models.by_name("Basic"))
            note.fields = [f"{label}/{state}/{offset}", "answer"]
            col.add_note(note, did)
            card = note.cards()[0]
            if state:
                card.type = card.queue = 2
                card.due = col.sched.today + offset
                card.ivl = 20
                card.reps = 10
                col.update_card(card)
                col.db.execute("update cards set data=? where id=?",
                    json.dumps({"s": 20, "d": 5, "lrt": now - 864000}), card.id)
            cases.append({"name": note.fields[0], "card_id": card.id, "remaining": 0,
                          "filtered_deck_id": filtered_id, "home_deck_id": did})
        filtered = col.decks.get(filtered_id)
        filtered.update(resched=reschedule, terms=[[f'deck:"{label}"', 100, 0]])
        col.decks.save(filtered)
        assert col.sched.rebuild_filtered_deck(filtered_id).count == 3
    return filtered_ids


def leech_cases(col, cases, now):
    for threshold, lapses in ((0, [0, 7, 8]), (1, [0, 1, 2]), (3, [1, 2, 3, 4, 5, 6]), (8, [6, 7, 8, 10, 11, 12, 15])):
        for action in (0, 1):
            label = f"Leech threshold {threshold} action {action}"
            preset = col.decks.add_config(label)
            preset["lapse"].update(leechFails=threshold, leechAction=action, delays=[10])
            col.decks.update_config(preset)
            did = col.decks.id(label)
            deck = col.decks.get(did)
            deck["conf"] = preset["id"]
            col.decks.save(deck)
            for state, previous in [(2, count) for count in lapses] + [(1, 7), (3, 7)]:
                name = f"{label}/state={state}/lapses={previous}"
                note = col.new_note(col.models.by_name("Basic"))
                note.fields = [name, "answer"]
                note.tags = ["original"]
                col.add_note(note, did)
                card = note.cards()[0]
                card.type = state
                card.queue = 2 if state == 2 else 1
                card.due = col.sched.today if state == 2 else now - 1
                card.ivl = 20 if state != 1 else 0
                card.reps = 30
                card.lapses = previous
                card.left = 0 if state == 2 else 1001
                col.update_card(card)
                col.db.execute("UPDATE cards SET data=? WHERE id=?",
                    json.dumps({"s": 20, "d": 5, "lrt": now - 20 * 86400}), card.id)
                cases.append({"name": name, "card_id": card.id, "state": state,
                              "remaining": 0 if state == 2 else 1})


def build(root):
    from anki.buildinfo import version
    from anki.collection import Collection
    from anki.import_export_pb2 import ExportAnkiPackageOptions
    from google.protobuf.json_format import MessageToDict

    col = Collection(str(root / "collection.anki2"))
    cases = []
    now = int(time.time())
    try:
        col.set_config("fsrs", True)
        for steps, maximum in (([1, 10], 36500), ([0.5, 3, 20], 36500), ([2], 36500), ([], 36500), ([], 3)):
            preset = col.decks.add_config(f"Steps {steps}, max {maximum}")
            preset["new"]["delays"] = steps
            preset["lapse"]["delays"] = steps
            preset["rev"]["maxIvl"] = maximum
            col.decks.update_config(preset)
            did = col.decks.id(f"Steps {steps}, max {maximum}")
            deck = col.decks.get(did)
            deck["conf"] = preset["id"]
            col.decks.save(deck)
            positions = [(0, 0, 20, 0), (2, 0, 20, 20)] + [
                (state, left, 20, 0) for state in (1, 3) for left in range(len(steps), 0, -1)
            ]
            if not steps and maximum == 36500:
                positions.extend((state, 0, stability, elapsed)
                    for stability in (0.001, 0.04999999, 0.05, 0.4999, 0.5, 0.5001, 2, 100, 36500, 1e8)
                    for state in (1, 2, 3) for elapsed in (0, 1, 30))
            if maximum == 3:
                positions *= 12
            positions = [(*position, True) for position in positions]
            if not steps and maximum == 36500:
                positions.extend((state, 0, 20, elapsed, False)
                    for state in (1, 2, 3) for elapsed in (0, 1, 30))
            positions = [(*position, -1) for position in positions]
            positions.extend((state, remaining, 20, 0, True, ahead)
                for state in (1, 3) for remaining in (range(len(steps), 0, -1) or (0,)) for ahead in (300, 900))
            for index, (state, remaining, stability, elapsed, timestamp, ahead) in enumerate(positions):
                name = f"{steps}/{state}/{remaining}/s={stability}/elapsed={elapsed}/lrt={timestamp}/max={maximum}/ahead={ahead}/case={index}"
                note = col.new_note(col.models.by_name("Basic"))
                note.fields = [name, "answer"]
                col.add_note(note, did)
                card = note.cards()[0]
                card.type = state
                card.queue = {0: 0, 1: 1, 2: 2, 3: 1}[state]
                card.due = col.sched.today + 20 - elapsed if state == 2 else now + ahead
                card.left = remaining * 1001
                card.ivl = 20 if state in (2, 3) else 0
                card.reps = 10 if state else 0
                col.update_card(card)
                if state:
                    data = {"s": stability, "d": 5}
                    if timestamp:
                        data["lrt"] = now - 86400 * elapsed
                    col.db.execute("update cards set data=? where id=?",
                        json.dumps(data), card.id)
                cases.append({"name": name, "card_id": card.id, "steps": steps, "maximum": maximum,
                              "state": state, "remaining": remaining})
        load_balance_cases(col, cases, now)
        leech_cases(col, cases, now)
        filtered_ids = filtered_home_cases(col, cases, now)
        col.update_cards([col.get_card(cid) for cid in col.db.list("select id from cards")])
        col.export_collection_package(str(root / "scheduler-corpus.colpkg"), include_media=False, legacy=True)
        col.reopen()
        for filtered_id in filtered_ids:
            col.sched.empty_filtered_deck(filtered_id)
        capture_answers(col, cases)
        report = {"anki_version": version, "now": now, "timezone": os.environ["TZ"], "config": col.all_config(),
                  "card_ids": [row[0] for row in card_graph(col)], "cases": cases}
        queued = []
        for deck in col.decks.all_names_and_ids():
            col.decks.select(deck.id)
            result = col._backend.get_queued_cards(fetch_limit=1000, intraday_learning_only=False)
            queued.append(MessageToDict(result, preserving_proto_field_name=True))
        save_json(root / "queued.json", queued)
        save_json(root / "scheduler-expected.json", report)
        col.export_anki_package(out_path=str(root / "scheduler-corpus.apkg"),
            options=ExportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True, legacy=True), limit=None)
        return report
    finally:
        col.close()


if __name__ == "__main__":
    require_anki()
    os.environ["TZ"] = os.environ.get("ANKI_SCHEDULER_TIMEZONE", "Asia/Tokyo")
    time.tzset()
    root = Path(tempfile.mkdtemp(prefix="anki-scheduler-", dir=REPO / "tmp"))
    report = build(root)
    print(json.dumps({"artifacts": str(root), "anki_version": report["anki_version"], "cases": len(report["cases"])}))
    check = subprocess.run(["bin/test", "unit", "test/models/anki/learning_corpus_test.rb"], cwd=SHIRABE,
        env={**os.environ, "ANKI_SCHEDULER_CORPUS": str(root)}, capture_output=True, text=True)
    (root / "rails.log").write_text(check.stdout + check.stderr)
    print(check.stdout + check.stderr)
    sys.exit(check.returncode)
