"""Consume actual Rails endpoint responses supplied by OfflineSnapshotContractTest."""

import io
import json
import sys
from urllib.parse import parse_qs, urlsplit

import support
from shirabe_addon import offline, offline_ui

captured = json.load(sys.stdin)
text = captured.get("text", "猫")
calls = []


class CapturedResponses:
    def open(self, request, *, timeout):
        path = urlsplit(request.full_url).path.removeprefix("/api/v1")
        body = json.loads(request.data) if request.data else None
        if body is not None:
            assert not parse_qs(urlsplit(request.full_url).query)
            assert request.headers["Accept-language"] == captured["/me"]["preferences"]["uiLocale"]
            if path == "/parse":
                assert body == {"texts": [text]}
            else:
                assert path == "/words/identify" and "examples" not in body["include"]
                assert body["dictionaries"] == ["personal-cat", "jmdict"], body
                assert "relations" in body["include"]
        calls.append((request.get_method(), path))
        data = json.dumps(captured[path], ensure_ascii=False).encode()
        assert len(data) < offline.MAX_RESPONSE_BYTES
        result = io.BytesIO(data)
        result.headers = {"Content-Length": str(len(data))}
        return result


def backend(**kwargs):
    result = offline.DictionaryBackend("https://fixture.invalid", json.dumps({"access": "fixture", "expiresAt": 2000}),
                                       now=lambda: 1, **kwargs)
    result._opener = CapturedResponses()
    return result


metadata = backend()
preferences = metadata.me()["preferences"]
dictionaries = metadata.dictionaries()["dictionaries"]
approved = {"personal-cat", "jmdict"}
selection = offline_ui.approved_dictionary_slugs(preferences, approved, dictionaries)
source = backend(locale=preferences["uiLocale"], dictionaries=selection, stack_fingerprint=preferences["stackFingerprint"])
notes = [offline_ui.CapturedNote(i, "fixture", (), None, None) for i in (1, 2)]
batch = offline_ui.prepare_batch(notes, {1: [text], 2: [text]}, source,
                                 preferences=preferences, approved=approved,
                                 sources=[item for item in dictionaries if item["slug"] in approved])
assert len(batch.rows) == 2 and not batch.issue_notes, batch
snapshot = batch.rows[0][1].snapshot
assert batch.rows[1][1].snapshot == snapshot
assert offline.validate_snapshot(snapshot) == snapshot and snapshot["complete"]
for raw, token in zip(captured["/parse"]["results"][0]["tokens"], snapshot["segments"][0]["tokens"]):
    inflection = raw.get("inflection") or {}
    if inflection:
        assert token.get("inflection") == [form["label"] for form in inflection["forms"]], (token, inflection)
    assert token.get("inflectionBase") == inflection.get("base"), (token, inflection)
assert snapshot["preferences"]["dictionaryReveal"] == preferences["dictionaryReveal"]
assert next(item for item in snapshot["sources"] if item["slug"] == "jmdict")["source"]["license"] == "EDRDG"
assert next(item for item in snapshot["sources"] if item["slug"] == "personal-cat")["attribution"] == "Fixture attribution"
assert calls == [("GET", "/me"), ("GET", "/dictionary-stack"), ("GET", "/dictionaries"),
                 ("POST", "/parse"), ("POST", "/words/identify"), ("POST", "/parse")], calls
for reason, changed in captured.get("drift", {}).items():
    source = backend(locale=preferences["uiLocale"], dictionaries=selection, stack_fingerprint=preferences["stackFingerprint"])
    original_parse = captured["/parse"]
    start = len(calls)
    def progress(completed, _total):
        if completed == 1:
            captured["/parse"] = changed
    try:
        offline_ui.prepare_batch(notes, {1: [text], 2: [text]}, source, preferences=preferences, approved=approved, progress=progress)
        raise AssertionError(f"Dictionary {reason} change was accepted")
    except offline.SnapshotError as error:
        assert error.code == "dictionary_changed", error
    finally:
        captured["/parse"] = original_parse
    assert calls[start:] == [("POST", "/parse"), ("POST", "/words/identify"), ("POST", "/parse")], calls[start:]
print(json.dumps({"snapshot": snapshot, "identify": captured["/words/identify"], "parse": captured["/parse"]}, ensure_ascii=False))
