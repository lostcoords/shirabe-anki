"""Capture real Anki source, rendering, mutations and bounded reconciliation scans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import resource
import sqlite3
import os
import subprocess
import sys
import time
import tracemalloc

from fixture_deck import AUDIO, IMAGE, REPO, save_json


def table_rows(col, table):
    columns = [row[1] for row in col.db.all(f"pragma table_info({table})")]
    return [dict(zip(columns, row)) for row in col.db.all(f"select * from {table} order by id")]


def snapshot(col):
    notes = []
    for row in table_rows(col, "notes"):
        note = col.get_note(row["id"])
        notes.append({**row, "fields": note.fields, "tags": note.tags})
    cards = []
    for row in table_rows(col, "cards"):
        card = col.get_card(row["id"])
        cards.append({**row, "question": card.question(), "answer": card.answer()})
    return {
        "collection": {"creation_time": col.crt, "today": col.sched.today, "config": col.all_config()},
        "note_types": sorted(col.models.all(), key=lambda model: model["id"]),
        "notes": notes,
        "cards": cards,
        "reviews": table_rows(col, "revlog"),
        "decks": [col.decks.get(item.id) for item in col.decks.all_names_and_ids()],
        "presets": col.decks.all_config(),
        "media": {
            path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            for path in sorted(Path(col.media.dir()).iterdir()) if path.is_file()
        },
    }


def add_type(col, name, fields, templates, *, cloze=False, css=""):
    model = col.models.new("Shirabe study " + name)
    model["type"] = int(cloze)
    model["css"] = css
    for name in fields:
        col.models.add_field(model, col.models.new_field(name))
    for name, question, answer, deck in templates:
        template = col.models.new_template(name)
        template.update(qfmt=question, afmt=answer, did=deck)
        col.models.add_template(model, template)
    return col.models.get(col.models.add_dict(model).id)


def configure_settings(col, selected):
    preset = col.decks.add_config("Study corpus custom")
    preset.update(
        name="Study corpus custom", desiredRetention=0.87, sm2Retention=0.83,
        newMix=2, interdayLearningMix=1, newSortOrder=3, reviewOrder=7,
        newGatherPriority=5, buryInterdayLearning=True,
        easyDaysPercentages=[1, 0.8, 1, 0.9, 1, 0.7, 0.5],
        autoplay=False, maxTaken=73, timer=1, replayq=False, stopTimerOnAnswer=True,
        secondsToShowQuestion=4.5, secondsToShowAnswer=6.25,
        questionAction=1, answerAction=3, waitForAudio=False,
        weightSearch="deck:Study*", ignoreRevlogsBeforeDate="2025-01-02",
        customPreset={"preserve": [1, "two"]},
    )
    preset["new"].update(delays=[0.5, 3, 20], perDay=37, initialFactor=2700,
                         ints=[2, 7, 0], order=0, bury=True, customNew="preserve")
    preset["rev"].update(perDay=451, maxIvl=12345, ease4=1.45,
                         hardFactor=1.15, ivlFct=0.95, bury=True, customRev="preserve")
    preset["lapse"].update(delays=[2, 15], mult=0.25, minInt=3,
                           leechAction=0, leechFails=9, customLapse="preserve")
    col.decks.update_config(preset)
    day = col.sched.today
    deck = col.decks.get(selected)
    deck.update(
        conf=preset["id"], desc="<b>Source description</b>", md=True, collapsed=True, browserCollapsed=True,
        extendNew=17, extendRev=43, reviewLimit=123, newLimit=19,
        reviewLimitToday={"limit": 67, "today": day},
        newLimitToday={"limit": 8, "today": day}, desiredRetention=87,
        newToday=[day, -2], revToday=[day, 11], lrnToday=[day, -3],
        timeToday=[day, 123456], customDeck={"preserve": True},
    )
    col.decks.save(deck)
    filtered = col.decks.get(col.decks.new_filtered("Study corpus filtered"))
    filtered.update(
        resched=False, terms=[["tag:study_corpus", 42, 3], ["is:due", 17, 6]],
        delays=[1.5, 8], previewDelay=13, previewAgainSecs=73,
        previewHardSecs=617, previewGoodSecs=31, customFiltered="preserve",
    )
    col.decks.save(filtered)


def build(root, source_manifest, sizes):
    from anki.buildinfo import version
    from anki.collection import Collection
    from anki.import_export_pb2 import ExportAnkiPackageOptions

    root.mkdir()
    col = Collection(str(root / "collection.anki2"))
    try:
        selected = col.decks.id("Study corpus::Selected")
        outside = col.decks.id("Study corpus::Outside")
        media_root = Path(source_manifest["collection"]).with_suffix(".media")
        for name in (IMAGE, AUDIO):
            col.media.write_data(name, (media_root / name).read_bytes())
        fields = ["Prompt", "Answer", "Optional"]
        basic = add_type(col, "basic", fields, [("Recognition", "{{Prompt}}", '{{FrontSide}}<hr id="answer">{{Answer}}', None)])
        single = add_type(col, "single-field", ["Only"], [("Only", "{{Only}}", "{{Only}}", None)])
        reverse = add_type(col, "reverse", fields, [
            ("Recognition", "{{Prompt}}", "{{Answer}}", None),
            ("Production", "{{Answer}}", "{{Prompt}}", None),
        ])
        multiple = add_type(col, "multiple", fields, [
            ("First", "{{Prompt}}", "{{Answer}}", None),
            ("Second", "{{Answer}}", "{{Prompt}}", None),
            ("Optional", "{{#Optional}}{{Optional}}{{/Optional}}", "{{Prompt}}", None),
        ])
        cloze = add_type(col, "cloze", ["Text", "Extra"], [
            ("Cloze", "{{cloze:Text}}", "{{cloze:Text}}<hr>{{Extra}}", None),
        ], cloze=True)
        cross = add_type(col, "cross-deck", fields, [
            ("Selected", "{{Prompt}}", "{{Answer}}", None),
            ("Outside", "{{Answer}}", "{{Prompt}}", outside),
        ])
        typed = add_type(col, "typed", fields, [("Typed", "{{Prompt}}{{type:Answer}}", "{{FrontSide}}<hr>{{type:Answer}}", None)])
        rich = add_type(col, "rich", fields, [
            ("Rich", '<section id="prompt">{{Prompt}}</section><script>document.getElementById("prompt").dataset.ready="yes"</script>', "{{FrontSide}}<hr>{{text:Answer}}", None),
        ], css='.card { color: #123456; } ruby rt { font-size: 0.5em; }')
        cases = [
            ("basic", basic, ["猫", "cat", ""]),
            ("reverse", reverse, ["食べる", "to eat", ""]),
            ("multiple", multiple, ["水", "water", "みず"]),
            ("conditional-empty", multiple, ["火", "fire", ""]),
            ("empty-note", basic, ["", "", ""]),
            ("empty-single-field", single, [""]),
            ("cloze-sparse-repeated", cloze, ["{{c1::猫}}と{{c4::犬::動物}}、また{{c1::猫}}", "extra"]),
            ("cloze-nested", cloze, ["{{c1::外{{c2::内}}}}", "nested"]),
            ("cross-deck", cross, ["本", "book", ""]),
            ("typed", typed, ["山", "mountain", ""]),
            ("rich-css-script", rich, ['<ruby>日本語<rt>にほんご</rt></ruby>', "<b>Japanese</b>", ""]),
            ("media", basic, [f'<img src="{IMAGE}">[sound:{AUDIO}]', "media", ""]),
        ]
        identities = {}
        for name, model, values in cases:
            note = col.new_note(model)
            note.fields = values
            note.tags = ["study_corpus", f"case::{name}", "preserve"]
            col.add_note(note, selected)
            identities[name] = note.id
        for name in ("reverse", "cloze-sparse-repeated"):
            for index, card in enumerate(col.get_note(identities[name]).cards()):
                card.start_timer()
                col.sched.answerCard(card, 3 if index == 0 else 4)

        configure_settings(col, selected)
        initial = snapshot(col)
        save_json(root / "initial.json", initial)
        apkg = root / "study-corpus.apkg"
        col.export_anki_package(out_path=str(apkg), options=ExportAnkiPackageOptions(
            with_scheduling=True, with_deck_configs=True, with_media=True, legacy=True,
        ), limit=None)
        checks = {
            "reverse_keeps_two_schedules": len(col.get_note(identities["reverse"]).cards()) == 2,
            "sparse_cloze_ordinals": sorted(card.ord for card in col.get_note(identities["cloze-sparse-repeated"]).cards()) == [0, 3],
            "conditional_card_omitted": len(col.get_note(identities["conditional-empty"]).cards()) == 2,
            "empty_note_has_fallback_card": len(col.get_note(identities["empty-note"]).cards()) == 1,
            "deck_override_creates_outside_sibling": {card.did for card in col.get_note(identities["cross-deck"]).cards()} == {selected, outside},
            "distinct_sibling_review_streams": len({row["cid"] for row in initial["reviews"]}) == 4,
        }
        mutations = []

        def capture(name, operation):
            before = snapshot(col)
            operation()
            after = snapshot(col)
            save_json(root / f"{name}.json", {"before": before, "after": after})
            mutations.append(name)
            return before, after

        def edit_note(name, field, value):
            note = col.get_note(identities[name])
            note[field] = value
            col.update_note(note)

        capture("field-edit", lambda: edit_note("reverse", "Prompt", "食べます"))
        checks["field_edit_persisted"] = col.get_note(identities["reverse"])["Prompt"] == "食べます"

        def edit_tags():
            note = col.get_note(identities["reverse"])
            note.tags = ["study_corpus", "added"]
            col.update_note(note)

        capture("tag-edit", edit_tags)
        checks["tag_edit_persisted"] = set(col.get_note(identities["reverse"]).tags) == {"study_corpus", "added"}

        def edit_template():
            model = col.models.get(basic["id"])
            model["tmpls"][0]["afmt"] += "<footer>changed</footer>"
            col.models.update_dict(model)

        capture("template-edit", edit_template)
        checks["template_edit_changes_rendering"] = "<footer>changed</footer>" in col.get_note(identities["basic"]).cards()[0].answer()
        capture("card-generation", lambda: edit_note("conditional-empty", "Optional", "ひ"))
        checks["field_edit_generates_missing_card"] = len(col.get_note(identities["conditional-empty"]).cards()) == 3
        card_id = col.get_note(identities["reverse"]).cards()[0].id
        capture("move", lambda: col.set_deck([card_id], outside))
        checks["card_move_persisted"] = col.get_card(card_id).did == outside
        before_suspend = next(row for row in table_rows(col, "cards") if row["id"] == card_id)
        capture("suspend", lambda: col.sched.suspend_cards([card_id]))
        checks["suspended_queue"] = col.get_card(card_id).queue == -1
        capture("resume", lambda: col.sched.unsuspend_cards([card_id]))
        after_resume = next(row for row in table_rows(col, "cards") if row["id"] == card_id)
        checks["resume_preserves_schedule"] = all(before_suspend[key] == after_resume[key] for key in ("type", "queue", "due", "ivl", "factor", "reps", "lapses", "left"))

        def review():
            card = col.get_card(card_id)
            card.start_timer()
            col.sched.answerCard(card, 3)

        before, after = capture("review", review)
        checks["review_appends_history"] = len(after["reviews"]) == len(before["reviews"]) + 1
        base = col.get_note(identities["reverse"]).fields[:]
        capture("conflict", lambda: edit_note("reverse", "Prompt", "Anki edit"))
        local = base[:]
        local[0] = "Shirabe edit"
        save_json(root / "conflict-input.json", {"note_id": identities["reverse"], "base": base, "anki": col.get_note(identities["reverse"]).fields, "shirabe": local})
        deleted = identities["multiple"]
        deleted_cards = [card.id for card in col.get_note(deleted).cards()]
        capture("delete-note", lambda: col.remove_notes([deleted]))
        checks["note_deletion_removes_siblings"] = not col.find_cards("nid:" + str(deleted))
        save_json(root / "deletions.json", {"notes": [deleted], "cards": deleted_cards})
        report = {"anki": version, "collection": str(root / "collection.anki2"), "apkg": str(apkg), "initial": str(root / "initial.json"), "cases": identities, "selected_decks": [selected], "mutations": mutations, "checks": checks}
    finally:
        col.close()
    report["benchmarks"] = [benchmark(root, size) for size in sizes]
    report["passed"] = all(checks.values()) and all(row["indexed_cards"] == row["cards"] for row in report["benchmarks"])
    save_json(root / "report.json", report)
    probe = subprocess.run(
        [str(REPO / "bin/test"), "unit", "test/models/anki/study_corpus_test.rb", "test/models/anki/collection_source_test.rb"],
        cwd=REPO, env={**os.environ, "ANKI_STUDY_CORPUS": str(root)},
        capture_output=True, text=True,
    )
    (root / "shirabe-import.log").write_text(probe.stdout + probe.stderr)
    report["shirabe_import"] = {"passed": probe.returncode == 0, "log": str(root / "shirabe-import.log")}
    if probe.returncode == 0:
        from study_export import verify
        report["shirabe_export"] = verify(root)
        report["passed"] = report["passed"] and report["shirabe_export"]["passed"]
    save_json(root / "report.json", report)
    return report


def benchmark(root, size):
    from anki.collection import AddNoteRequest, Collection

    folder = root / f"benchmark-{size}"
    folder.mkdir()
    col = Collection(str(folder / "collection.anki2"))
    started = time.perf_counter()
    try:
        deck = col.decks.id("Selected benchmark")
        model = col.models.by_name("Basic")
        for offset in range(0, size, 1000):
            requests = []
            for index in range(offset, min(offset + 1000, size)):
                note = col.new_note(model)
                note.fields = [f"単語 {index}", f"Meaning {index}"]
                requests.append(AddNoteRequest(note=note, deck_id=deck))
            col.add_notes(requests)
        build_seconds = time.perf_counter() - started
        tracemalloc.start()
        started = time.perf_counter()
        indexed = 0
        with sqlite3.connect(folder / "index.sqlite3") as index:
            index.execute("create table entities (kind text not null, id integer not null, digest text not null, primary key (kind, id))")
            for kind, table in (("card", "cards"), ("note", "notes"), ("review", "revlog")):
                after = 0
                while rows := col.db.all(f"select * from {table} where id > ? order by id limit 1000", after):
                    index.executemany("insert into entities values (?, ?, ?)", [
                        (kind, row[0], hashlib.sha256(json.dumps(row, ensure_ascii=False).encode()).hexdigest()) for row in rows
                    ])
                    after = rows[-1][0]
                    if kind == "card":
                        indexed += len(rows)
            note_types = json.dumps(col.models.all(), ensure_ascii=False).encode()
            index.execute("insert into entities values (?, ?, ?)", ("note_types", 0, hashlib.sha256(note_types).hexdigest()))
            decks = json.dumps([col.decks.get(item.id) for item in col.decks.all_names_and_ids()], ensure_ascii=False).encode()
            index.execute("insert into entities values (?, ?, ?)", ("decks", 0, hashlib.sha256(decks).hexdigest()))
            presets = json.dumps(col.decks.all_config(), ensure_ascii=False).encode()
            index.execute("insert into entities values (?, ?, ?)", ("presets", 0, hashlib.sha256(presets).hexdigest()))
        seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"cards": size, "indexed_cards": indexed, "build_seconds": build_seconds, "reconcile_seconds": seconds, "python_peak_bytes": peak, "process_peak_bytes": rss if sys.platform == "darwin" else rss * 1024, "index_bytes": (folder / "index.sqlite3").stat().st_size, "scope": "selected collection rows, note types, decks and presets; this new-card fixture has no media or review history; no GUI or network"}
    finally:
        col.close()
