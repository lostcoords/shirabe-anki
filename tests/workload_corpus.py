"""Measure Anki's review workload boundary in disposable collections."""

import json
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from fixture_deck import REPO, require_anki, save_json


def build(root):
    from anki.buildinfo import version
    from anki.collection import Collection
    from anki.import_export_pb2 import ExportAnkiPackageOptions
    from google.protobuf.json_format import MessageToDict

    path = str(root / "collection.anki2")
    col = Collection(path)
    try:
        col.set_config("fsrs", True)
        col._backend.set_load_balancer_enabled(True)
        presets = []
        for name in ("Shared", "Unrelated"):
            preset = col.decks.add_config(name)
            preset["new"].update(delays=[], perDay=9999)
            preset["rev"]["perDay"] = 9999
            col.decks.update_config(preset)
            presets.append(preset["id"])
        decks = []
        for name, preset in (("Target", presets[0]), ("Same preset", presets[0]), ("Other preset", presets[1])):
            did = col.decks.id(name)
            deck = col.decks.get(did)
            deck["conf"] = preset
            col.decks.save(deck)
            decks.append(did)
        targets = []
        pool = []
        for index in range(220):
            note = col.new_note(col.models.by_name("Basic"))
            note.fields = [f"Workload {index}", "answer"]
            col.add_note(note, decks[0] if index < 20 else decks[2])
            (targets if index < 20 else pool).append(note.cards()[0].id)
        today = col.sched.today
        now = int(time.time())
        for index, cid in enumerate(pool):
            offset = (6, 7, 9, 10)[index % 4]
            col.db.execute("update cards set type=2, queue=2, ivl=10, reps=3, due=? where id=?", today + offset, cid)
        col.decks.select(decks[0])
    finally:
        col.close()

    cases = []
    phases = (("other_preset", decks[2], 2, 2), ("same_preset", decks[1], 2, 2),
        ("same_deck", decks[0], 2, 2), ("suspended", decks[1], 2, -1),
        ("buried", decks[1], 2, -2), ("interday_learning", decks[1], 1, 3),
        ("interday_relearning", decks[1], 3, 3), ("intraday_learning", decks[1], 1, 1),
        ("new", decks[1], 0, 0),
        ("new_far_positions", decks[1], 0, 0),
        ("target_far_positions", decks[2], 2, 2),
        ("first_weekday_only", decks[2], 2, 2), ("second_weekday_only", decks[2], 2, 2))
    for name, did, kind, queue in phases:
        col = Collection(path)
        try:
            preset = col.decks.get_config(presets[0])
            preset["easyDaysPercentages"] = [1] * 7
            if name.endswith("weekday_only"):
                offset = 7 if name == "first_weekday_only" else 8
                normal = (datetime.fromtimestamp(col.sched.day_cutoff).date() + timedelta(days=offset)).weekday()
                preset["easyDaysPercentages"] = [int(day == normal) for day in range(7)]
            col.decks.update_config(preset)
            for index, cid in enumerate(targets):
                col.db.execute("update cards set due=? where id=?", index + 1 + (10000 if name == "target_far_positions" else 0), cid)
            for index, cid in enumerate(pool):
                offset = (6, 7, 9, 10)[index % 4]
                due = now + offset * 86400 if queue == 1 else today + offset
                if name == "new_far_positions":
                    due += 10000
                interval = 10 if kind in (2, 3) else 0
                reps = 0 if kind == 0 else 3
                col.db.execute("update cards set did=?, type=?, queue=?, ivl=?, reps=?, due=? where id=?", did, kind, queue, interval, reps, due, cid)
        finally:
            col.close()
        col = Collection(path)
        try:
            queued = col._backend.get_queued_cards(fetch_limit=1000, intraday_learning_only=False)
            cards = [MessageToDict(card, preserving_proto_field_name=True) for card in queued.cards if card.card.id in targets]
            assert len(cards) == len(targets), (name, len(cards))
            cases.append({"name": name, "cards": cards, "pool": col.db.all(
                "select type,queue,count(*) from cards where id not in (%s) group by type,queue" % ",".join(map(str, targets)))})
            col.export_anki_package(out_path=str(root / f"{name}.apkg"),
                options=ExportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True, legacy=True), limit=None)
        finally:
            col.close()
    report = {"anki_version": version, "timezone": os.environ["TZ"], "now": now, "cases": cases}
    save_json(root / "workload-expected.json", report)
    return report


if __name__ == "__main__":
    require_anki()
    os.environ["TZ"] = os.environ.get("ANKI_SCHEDULER_TIMEZONE", "Asia/Tokyo")
    time.tzset()
    root = Path(tempfile.mkdtemp(prefix="anki-workload-", dir=REPO / "tmp"))
    report = build(root)
    print(json.dumps({"artifacts": str(root), "anki_version": report["anki_version"],
        "easy_intervals": {case["name"]: [card["states"]["easy"]["normal"]["review"]["scheduled_days"]
            for card in case["cards"]] for case in report["cases"]}}, indent=2))
    check = subprocess.run(["bin/test", "unit", "test/models/anki/workload_corpus_test.rb"], cwd=REPO,
        env={**os.environ, "ANKI_WORKLOAD_CORPUS": str(root)}, capture_output=True, text=True)
    (root / "rails.log").write_text(check.stdout + check.stderr)
    print(check.stdout + check.stderr)
    sys.exit(check.returncode)
