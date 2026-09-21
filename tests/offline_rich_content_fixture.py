"""API-shaped publisher content for native preparation and shared-card tests."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import support
from offline_fixture import identify, prepared
from shirabe_addon import offline


def source_tag():
    return {"name": "arch", "category": "register", "notes": "Publisher note", "order": 2, "score": -3.5}


def rich_entry():
    return {
        "dictionary": "publisher", "dictionaryName": "Publisher dictionary",
        "type": "word", "sourceId": "rich:1",
        "sourceHeadwords": [{"text": "猫", "reading": "ねこ", "tags": [source_tag()], "rules": ["n"], "score": 7.25}],
        "senses": [
            {"position": 3, "depth": 1, "label": "㊀①",
             "definitions": [{"lang": "ja", "text": "猫の説明", "title": "基本義"},
                             {"lang": "en", "text": "cat", "title": "Meaning"}],
             "usages": [{"lang": "ja", "text": "猫がいる。", "title": "使用例"},
                        {"lang": "en", "text": "There is a cat.", "title": "Usage"}],
             "tags": [{"category": "misc", "code": "uk", "label": "Usually kana", "scope": "entry"}],
             "sourceTags": [source_tag()], "notes": ["Retained note"],
             "related": [{"text": "子猫", "reading": "コネコ", "label": "子供の猫"}],
             "antonyms": [], "seeAlso": [{"text": "イエネコ", "reading": None}]},
            {"position": 7, "depth": 2, "label": "㊀①㋐", "definitions": [], "tags": [],
             "usages": [{"lang": "ja", "text": "猫が寝る。", "title": "用例"}]},
        ],
    }


def rich_candidate():
    return {
        "id": "猫", "dictionary": "publisher", "sourceId": "rich:1", "headword": "猫", "reading": "ネコ",
        "common": False, "name": False, "pitch": [],
        "forms": [{"text": "ねこ", "script": "kana", "common": False,
                   "tags": [{"category": "kanaTag", "code": "ik", "label": "Irregular kana", "scope": "group"}]}],
        "entries": [rich_entry()],
    }


def prepare_rich(value=None, *, texts=("猫",), **options):
    value = rich_candidate() if value is None else value
    def lookup(token):
        return {"words": [{"candidates": [copy.deepcopy(value)]}]} if token["lemma"] == "猫" else identify(token)
    return prepared(texts, identify=lookup, allowed_dictionaries={"publisher", "jmdict"},
                    preferences={"languages": ["ja", "en"], "dictionaryReveal": {"publisher:ja": "show"}}, **options)


def candidate_with_size(size):
    for count in range(16):
        value = rich_candidate()
        notes = ["x" * 2048 for _ in range(count)] + [""]
        value["entries"][0]["senses"][0]["notes"] = notes
        remaining = size - len(offline._json(value))
        if 0 <= remaining <= 2048:
            notes[-1] = "x" * remaining
            assert len(offline._json(value)) == size
            return value
    raise ValueError("Requested candidate size cannot fit the fixture's bounded notes")


def persist_fixture(*, expected_anki_version=None, expected_anki_buildinfo=None):
    from anki import buildinfo

    library = str(Path(buildinfo.__file__).resolve())
    if expected_anki_version is not None and buildinfo.version != expected_anki_version:
        raise RuntimeError(f"Expected Anki {expected_anki_version}, loaded {buildinfo.version} from {library}")
    if expected_anki_buildinfo is not None and library != str(Path(expected_anki_buildinfo).resolve()):
        raise RuntimeError(f"Expected Anki library {expected_anki_buildinfo}, loaded {library}")
    from anki.collection import Collection

    result = prepare_rich()
    with tempfile.TemporaryDirectory(prefix="shirabe-rich-content-") as directory:
        col = Collection(str(Path(directory) / "collection.anki2"))
        try:
            stored = offline.persist_snapshot(col.media, result)
            filename, encoded = offline.snapshot_file(result.snapshot)
            assert stored.filename == filename
            assert stored.bytes_used == len(encoded)
            assert (Path(col.media.dir()) / filename).read_bytes() == encoded
            snapshot = offline.read_snapshot(col.media, filename)
            assert snapshot == result.snapshot
            counts = {table: col.db.scalar(f"select count(*) from {table}") for table in ("notes", "cards", "revlog")}
            assert not any(counts.values())
            return {"anki_version": buildinfo.version, "anki_buildinfo": library, "python": sys.executable,
                    "snapshot": snapshot, "encoded_bytes": len(encoded), "decoded_bytes": result.bytes_used,
                    "sha256": hashlib.sha256(encoded).hexdigest(), "filename": filename, "rows": counts}
        finally:
            col.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--persist", action="store_true", help="Verify storage in a disposable real Anki collection")
    parser.add_argument("--expected-anki-version", help="Refuse persistence with another Anki library version")
    parser.add_argument("--expected-anki-buildinfo", help="Refuse persistence from another Anki library path")
    options = parser.parse_args()
    result = persist_fixture(expected_anki_version=options.expected_anki_version,
                             expected_anki_buildinfo=options.expected_anki_buildinfo) if options.persist else prepare_rich().snapshot
    print(json.dumps(result, ensure_ascii=False))
