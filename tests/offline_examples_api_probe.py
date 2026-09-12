import io
import json
import sys
from urllib.parse import parse_qs, urlsplit

import support
from shirabe_addon import offline, offline_ui

captured = json.load(sys.stdin)
calls = []


class CapturedResponses:
    def open(self, request, *, timeout):
        url = urlsplit(request.full_url)
        path = url.path.removeprefix("/api/v1")
        body = json.loads(request.data) if request.data else None
        if body is not None:
            assert body["dictionaries"] == ["jmdict"], body
        elif path == "/sentences":
            assert parse_qs(url.query) == {"query": ["猫"], "entry": ["jmdict:offline-cat"], "take": ["2"], "locale": ["es"]}
        calls.append((request.get_method(), path))
        raw = json.dumps(captured[path], ensure_ascii=False).encode()
        result = io.BytesIO(raw)
        result.headers = {"Content-Length": str(len(raw))}
        return result


backend = offline.DictionaryBackend("https://fixture.invalid", json.dumps({"access": "fixture", "expiresAt": 2000}),
                                    dictionaries=["jmdict"], locale="es", now=lambda: 1)
backend._opener = CapturedResponses()
preferences = backend.me()["preferences"]
sources = backend.dictionaries()["dictionaries"]
batch = offline_ui.prepare_batch([offline_ui.CapturedNote(1, "fixture", (), None, None)], {1: ["猫"]}, backend,
                                 preferences=preferences, approved={"jmdict"}, sources=sources, include_examples=True)
assert len(batch.rows) == 1 and not batch.issue_notes, batch
item = batch.rows[0][1]
assert item.requests == 3 and item.complete
assert offline.validate_snapshot(item.snapshot) == item.snapshot
assert calls == [("GET", "/me"), ("GET", "/dictionaries"), ("POST", "/parse"), ("POST", "/words/identify"), ("GET", "/sentences")], calls
print(json.dumps({"snapshot": item.snapshot}, ensure_ascii=False))
