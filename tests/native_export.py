"""Build Shirabe native notes and verify their package with the installed Anki backend."""

import json
from html import unescape
import math
import os
from pathlib import Path
import re
import subprocess
import struct
import sys
import tempfile
import zipfile

from fixture_deck import REPO, require_anki, save_json


def imported_memory(data):
    # Anki's storage rounds f32 stability to 4 places and difficulty to 3:
    # https://github.com/ankitects/anki/blob/25.09.4/rslib/src/storage/card/data.rs
    data = json.loads(data)
    for key, places in (("s", 4), ("d", 3)):
        if key in data:
            value = struct.unpack("f", struct.pack("f", data[key]))[0]
            scaled = struct.unpack("f", struct.pack("f", value * 10**places))[0]
            data[key] = math.floor(scaled + 0.5) / 10**places
    return data


def verify_cloned(root):
    from anki.collection import Collection
    from anki.import_export_pb2 import ImportAnkiPackageRequest, ImportAnkiPackageOptions
    from study_corpus import snapshot

    expected = json.loads((root / "cloned-expected.json").read_text())
    results = {}
    with tempfile.TemporaryDirectory(prefix="cloned-", dir=root) as scratch:
        col = Collection(str(Path(scratch) / "collection.anki2"))
        try:
            col.import_anki_package(ImportAnkiPackageRequest(package_path=str(root / "cloned-export.apkg"),
                options=ImportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True)))
            actual = snapshot(col)
            save_json(root / "cloned-imported.json", actual)
            notes = {note["guid"]: note for note in actual["notes"]}
            types = {model["id"]: model for model in actual["note_types"]}
            results["note_count"] = len(notes) == len(expected["notes"])
            results["card_count"] = len(actual["cards"]) == len(expected["cards"])
            results["fresh_schedules"] = all(card["type"] == card["queue"] == card["reps"] == card["lapses"] == 0 for card in actual["cards"])
            results["no_curator_history"] = actual["reviews"] == []
            omitted = {"style.css", "nested.css", "font.woff2"}
            results["template_only_media_omitted_by_anki"] = expected["media"].keys() - actual["media"].keys() == omitted
            results["referenced_and_underscore_media"] = {name: row["sha256"] for name, row in actual["media"].items()} == {
                name: digest for name, digest in expected["media"].items() if name not in omitted}
            for row in expected["notes"]:
                note = notes[row["guid"]]
                definition = expected["note_types"][str(row["mid"])]
                model = types[note["mid"]]
                key = row["guid"]
                results[f"{key}_fields"] = note["fields"] == row["flds"].split("\x1f")
                results[f"{key}_templates"] = (
                    model["css"] == definition["css"]
                    and [field["name"] for field in model["flds"]] == [field["name"] for field in definition["flds"]]
                    and [(template["qfmt"], template["afmt"]) for template in model["tmpls"]]
                    == [(template["qfmt"], template["afmt"]) for template in definition["tmpls"]])
                results[f"{key}_siblings"] = sorted(card["ord"] for card in actual["cards"] if card["nid"] == note["id"]) == sorted(
                    card["ord"] for card in expected["cards"] if card["nid"] == row["id"])
        finally:
            col.close()
    return results


def verify(root):
    from anki.buildinfo import version
    from anki.collection import Collection
    from anki.import_export_pb2 import ImportAnkiPackageRequest, ImportAnkiPackageOptions
    from study_corpus import snapshot

    expected = json.loads((root / "native-expected.json").read_text())
    package = root / "native-export.apkg"
    results = {}
    options = {}
    counter_days = {}
    with tempfile.TemporaryDirectory(prefix="collections-", dir=root) as scratch:
        for mode in ("opened", "imported", "imported_fsrs"):
            folder = Path(scratch) / mode
            folder.mkdir()
            path = folder / "collection.anki2"
            if mode == "opened":
                with zipfile.ZipFile(package) as archive:
                    path.write_bytes(archive.read("collection.anki2"))
            col = Collection(str(path))
            try:
                if mode == "imported_fsrs":
                    col.set_config("fsrs", True)
                    col.crt -= 200 * 86400
                if mode != "opened":
                    col.import_anki_package(ImportAnkiPackageRequest(package_path=str(package),
                        options=ImportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True)))
                else:
                    with zipfile.ZipFile(package) as archive:
                        for index, name in json.loads(archive.read("media")).items():
                            assert col.media.write_data(name, archive.read(index)) == name
                actual = snapshot(col)
                options[mode] = {"fsrs": bool(actual["collection"]["config"].get("fsrs", False))}
                counter_days[mode] = {}
                save_json(root / f"native-{mode}.json", actual)
                notes = {note["guid"]: note for note in actual["notes"]}
                types = {model["id"]: model for model in actual["note_types"]}
                expected_cards = {card["nid"]: card for card in expected["cards"]}
                cards = {card["nid"]: card for card in actual["cards"]}
                checks = {
                    "note_count": len(notes) == 4,
                    "card_count": len(actual["cards"]) == 4,
                    "history": actual["reviews"] == expected["reviews"],
                    "media": {name: row["sha256"] for name, row in actual["media"].items()} == expected["media"],
                    "collection_options": options[mode]["fsrs"] == (mode != "imported"),
                }
                for row in expected["notes"]:
                    note = notes[row["guid"]]
                    definition = expected["note_types"][str(row["mid"])]
                    kind = definition["shirabe"]["kind"]
                    expression = note["fields"][0]
                    case = f"{kind}/{expression}"
                    checks[f"{case}_fields"] = note["fields"] == row["flds"].split("\x1f")
                    model = types[note["mid"]]
                    checks[f"{case}_definition"] = (
                        model["css"] == definition["css"]
                        and [field["name"] for field in model["flds"]] == [field["name"] for field in definition["flds"]]
                        and model["tmpls"][0]["qfmt"] == definition["tmpls"][0]["qfmt"]
                        and model["tmpls"][0]["afmt"] == definition["tmpls"][0]["afmt"]
                        and model.get("shirabe") == definition["shirabe"])
                    card = cards[note["id"]]
                    original = expected_cards[row["id"]]
                    checks[f"{case}_schedule"] = all(card[key] == original[key] for key in
                        ("type", "queue", "ivl", "factor", "reps", "lapses", "left", "flags"))
                    checks[f"{case}_memory"] = json.loads(card["data"]) == (
                        imported_memory(original["data"]) if mode != "opened" else json.loads(original["data"]))
                    due = card["due"]
                    expected_due = original["due"]
                    if card["queue"] in (2, 3):
                        due += actual["collection"]["creation_time"] // 86400
                        expected_due += expected["collection"]["crt"] // 86400
                    checks[f"{case}_due"] = due == expected_due
                    deck = next(deck for deck in actual["decks"] if deck["id"] == card["did"])
                    expected_deck = expected["collection"]["decks"][str(original["did"])]
                    checks[f"{case}_deck"] = deck["name"] == expected_deck["name"]
                    checks[f"{case}_daily_counts"] = all(
                        deck[key][1] == expected_deck[key][1] for key in ("newToday", "revToday", "lrnToday", "timeToday"))
                    checks[f"{case}_stored_counter_days"] = all(
                        deck[key][0] == expected_deck[key][0] for key in ("newToday", "revToday", "lrnToday", "timeToday"))
                    counter_days[mode][deck["name"]] = {
                        "source_day": expected_deck["newToday"][0], "received_day": deck["newToday"][0],
                        "receiving_today": actual["collection"]["today"],
                        "same_study_day": deck["newToday"][0] + actual["collection"]["creation_time"] // 86400
                        == expected_deck["newToday"][0] + expected["collection"]["crt"] // 86400,
                    }
                    preset = next(preset for preset in actual["presets"] if preset["id"] == deck["conf"])
                    expected_preset = expected["collection"]["dconf"][str(expected_deck["conf"])]
                    checks[f"{case}_preset"] = (
                        preset["desiredRetention"] == expected_preset["desiredRetention"]
                        and preset["new"]["perDay"] == expected_preset["new"]["perDay"]
                        and preset["rev"]["maxIvl"] == expected_preset["rev"]["maxIvl"]
                        and preset["new"]["delays"] == expected_preset["new"]["delays"]
                        and preset["lapse"]["delays"] == expected_preset["lapse"]["delays"]
                        and preset["fsrsParams6"] == expected_preset["fsrsParams6"])
                    checks[f"{case}_easy_days"] = preset["easyDaysPercentages"] == expected_preset["easyDaysPercentages"]
                    checks[f"{case}_retention_override"] = deck.get("desiredRetention") is None
                    question, answer = card["question"], card["answer"]
                    if kind == "writing":
                        label = "Write the kana" if expression == "あ" else "Write the kanji"
                        checks[f"{case}_prompt"] = label in question and note["fields"][2] in question and expression not in question and expression in answer
                    elif kind == "kanji":
                        checks["kanji_faces"] = "山" in question and "mountain" in answer
                    else:
                        answer_text = unescape(re.sub(r"<[^>]+>", "", re.sub(r"<rt\b[^>]*>.*?</rt>", "", answer, flags=re.S)))
                        checks["vocab_faces"] = all(text in answer_text for text in
                            ("猫", "ねこ", "cat", "feline", "Native document", "犬と猫。", "A dog and a cat."))
                        checks["vocab_portable_documents"] = "<details><summary>My dictionary</summary>" in answer
                        checks["vocab_sentence_lookup"] = 'data-study-lookup="猫" data-study-reading="ねこ"' in answer
                        checks["vocab_dictionary_details"] = all(text in answer_text for text in
                            ("Dictionary usage note", "Dictionary example")) and 'class="sense-details"' in answer
                        checks["vocab_audio"] = len(col.get_card(card["id"]).answer_av_tags()) == 4
                        checks["vocab_accent_groups"] = re.findall(r'class="pronunciation" data-study-accent="(\d+)"', answer) == ["0", "1"]
                        checks["vocab_image"] = 'alt="Standalone image"' in answer
                        checks["vocab_furigana"] = "<ruby>猫<rt>ねこ</rt></ruby>" in answer
                        target = re.search(r'<a[^>]*class="study-target"[^>]*>(.*?)</a>', question, flags=re.S)
                        checks["vocab_question_hides_target_reading"] = bool(target and "猫" in target[1] and "<rt>" not in target[1])
                        checks["vocab_question_keeps_context_reading"] = "<ruby>犬<rt>いぬ</rt></ruby>" in question
                        checks["vocab_plain_identity_fields"] = note["fields"][:2] == ["猫", "ねこ"]
                    checks[f"{case}_metadata_hidden"] = "sense_overrides" not in question + answer
                results[mode] = checks
            finally:
                col.close()
    results["cloned_import"] = verify_cloned(root)
    report = {"anki_version": version, "checks": results, "collection_options": options, "counter_days": counter_days,
        "limitations": ["APKG import retains raw daily-counter days instead of translating them to the receiver's collection clock.",
            "Anki APKG import drops the cloned fixture's template-only style.css, nested.css and font.woff2, despite their presence in the package. Underscore-prefixed static assets survive."],
        "passed": all(all(checks.values()) for checks in results.values())}
    save_json(root / "native-report.json", report)
    return report


if __name__ == "__main__":
    require_anki()
    root = Path(tempfile.mkdtemp(prefix="anki-native-", dir=REPO / "tmp"))
    print(f"Artifacts: {root}", flush=True)
    probe = subprocess.run(["bin/test", "unit", "test/models/anki/native_export_test.rb", "test/models/official_deck_cloner_test.rb"], cwd=REPO,
        env={**os.environ, "ANKI_NATIVE_EXPORT_ROOT": str(root)}, capture_output=True, text=True)
    (root / "rails.log").write_text(probe.stdout + probe.stderr)
    if probe.returncode:
        print(probe.stdout + probe.stderr)
        sys.exit(probe.returncode)
    report = verify(root)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)
