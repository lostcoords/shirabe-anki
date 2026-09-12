"""Shared executable fixture: Python preparation consumed by the TypeScript host."""

import json

import support
from shirabe_addon.offline import prepare_snapshot


def candidate(word="猫", source_id="1"):
    return {
        "id": word, "dictionary": "jmdict", "sourceId": source_id,
        "headword": word, "reading": "ネコ", "common": True, "name": False,
        "pitch": [{"downstep": 1, "pattern": "HL", "audioUrl": "https://private.test/audio?token=secret"}],
        "forms": [], "furigana": [{"text": word, "ruby": "ねこ"}],
        "entries": [{"order": 0, "dictionary": "jmdict", "sourceId": source_id, "type": "word",
                     "senses": [{"position": 3, "definitions": [{"lang": "en", "text": "cat"}, {"lang": "es", "text": "gato"}],
                                 "tags": [], "study": {"cardId": 42}}]}],
        "study": {"state": "due", "decks": ["private"]}, "access": "secret",
    }


def parse(text):
    position = 0
    tokens = []
    for char in text:
        length = len(char.encode("utf-16-le")) // 2
        tokens.append({"surface": char, "lemma": char, "reading": "ネコ", "position": position,
                       "length": length, "kind": "symbol" if char in ("😀", "は") else "word", "state": "due"})
        position += length
    return {"tokens": tokens, "credentials": "secret"}


def identify(token):
    word = token["lemma"]
    return {"words": [{"candidates": [candidate(word, "1" if word == "猫" else "2")]}]}


def prepared(texts=("😀猫は猫", "犬"), **kwargs):
    return prepare_snapshot(texts, parse=kwargs.pop("parse", parse), identify=kwargs.pop("identify", identify),
                            content_version=kwargs.pop("content_version", "dictionary-v1-en-es"),
                            allowed_dictionaries=kwargs.pop("allowed_dictionaries", {"jmdict"}),
                            now=lambda: 1_700_000_000, **kwargs)


def inflected(texts=("食べました", "食べない"), **kwargs):
    def parsed(text):
        return {"tokens": [{"surface": text, "position": 0, "length": len(text), "lemma": "食べる",
                            "reading": "タベマシタ" if text == "食べました" else "タベナイ", "pos": "verb", "kind": "word",
                            "entry": {"dictionary": "jmdict", "sourceId": "1"},
                            "inflection": {"labels": ["past", "polite"] if text == "食べました" else ["negative"],
                                           "base": "食べる", "access": "secret"}}]}
    word = candidate("食べる")
    word["reading"] = "タベル"
    word["furigana"] = [{"text": "食", "ruby": "た"}, {"text": "べる"}]
    return prepared(texts, parse=kwargs.pop("parse", parsed), identify=lambda _: {"words": [{"candidates": [word]}]}, **kwargs)


if __name__ == "__main__":
    print(json.dumps(prepared().snapshot, ensure_ascii=False, separators=(",", ":")))
