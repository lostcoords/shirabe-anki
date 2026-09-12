#!/usr/bin/env python3
"""Serve a dummy device-authorization flow and build its isolated AnkiDroid deck."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

from fixture_deck import HERE, PROBE, output_directory, probe_block, require_anki, save_json

ACCESS = "phase0-access-only-dummy"
REFRESH = "phase0-refresh-only-dummy"
DEVICE = "phase0-device-code-only-dummy"
CODE = "PHASE0"
SCOPES = {"READ_ACCOUNT", "READ_DICTIONARY", "READ_SRS"}


def serve(root):
    from offline_fixture import candidate, parse
    state = {"approved": False, "issued": 0, "requests": [], "scope": []}
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, body, status=200, html=False):
            data = body.encode() if html else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.respond({}, 204)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if raw and "application/json" in self.headers.get("Content-Type", ""):
                data = json.loads(raw)
            else:
                data = {key: value[0] for key, value in parse_qs(raw.decode()).items()}
            path = urlsplit(self.path).path
            if path == "/evidence":
                with lock:
                    self.respond(state)
                return
            with lock:
                state["requests"].append({"at": time.time(), "method": self.command, "path": path, "body": data,
                                          "dummy_bearer": self.headers.get("Authorization") == "Bearer " + ACCESS})
                try:
                    self.route(path, data)
                finally:
                    save_json(root / "oauth-server-report.json", state)

        def route(self, path, data):
            if path == "/approve" and self.command == "GET":
                self.respond('''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phase0 fixture authorization</title><body style="font:20px sans-serif;padding:24px;background:#fff;color:#222">
<h1>Fixture sign-in only</h1><p>No real account is used. This issues a dummy read-only token to the isolated AnkiDroid app.</p>
<form action="/approve" method="post"><input type="hidden" name="code" value="PHASE0">
<button style="font:inherit;padding:16px;cursor:pointer">Approve fixture sign-in</button></form></body>''', html=True)
            elif path == "/approve" and self.command == "POST" and data.get("code") == CODE:
                state["approved"] = True
                self.respond('''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fixture approved</title><body style="font:20px sans-serif;padding:24px"><h1>Fixture approved</h1>
<p>Return to AnkiDroid. No real account was connected.</p></body>''', html=True)
            elif path == "/api/v1/oauth/device_authorization":
                requested = set(data.get("scope", "").split())
                if data.get("client_id") != "shirabe-anki-card" or not requested <= SCOPES:
                    self.respond({"error": "invalid_scope"}, 400)
                    return
                state["scope"] = sorted(requested)
                self.respond({"device_code": DEVICE, "user_code": CODE, "verification_uri": self.server.fixture_base + "/approve",
                              "verification_uri_complete": self.server.fixture_base + "/approve?code=" + CODE,
                              "interval": 2, "expires_in": 600})
            elif path == "/api/v1/oauth/token":
                valid = data.get("device_code") == DEVICE or data.get("refresh_token") == REFRESH
                if not valid:
                    self.respond({"error": "invalid_grant"}, 400)
                elif not state["approved"]:
                    self.respond({"error": "authorization_pending"}, 400)
                else:
                    state["issued"] += 1
                    self.respond({"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer", "expires_in": 3600,
                                  "scope": " ".join(state["scope"])})
            elif path.startswith("/api/v1/") and self.headers.get("Authorization") != "Bearer " + ACCESS:
                self.respond({"title": "Dummy fixture token required"}, 401)
            elif path == "/api/v1/me":
                self.respond({"credential": {"scopes": state["scope"]}, "preferences": {
                    "languages": ["en"], "uiLocale": "en", "dictionaries": ["jmdict"], "stackFingerprint": "phase0-oauth"}})
            elif path == "/api/v1/parse":
                self.respond(parse(data["text"]))
            elif path == "/api/v1/words/identify":
                self.respond({"words": [{"candidates": [candidate(token.get("lemma") or token.get("surface") or "猫")]} for token in data["tokens"]]})
            elif path == "/api/v1/srs/states/lookup":
                self.respond({"states": []})
            else:
                self.respond({"title": "No such fixture endpoint"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fixture_base = f"http://127.0.0.1:{server.server_port}"
    return server


def deck(root, api_base):
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions
    import support
    from shirabe_addon import assets, template
    source = root / "source"
    source.mkdir()
    col = Collection(str(source / "collection.anki2"))
    try:
        did = col.decks.id("Shirabe Phase0 OAuth")
        model = col.models.by_name("Basic")
        model["name"] = "Shirabe Phase0 OAuth"
        installed = assets.install(col.media)
        col.media.write_data(PROBE, (HERE / "phase0_probe.js").read_bytes())
        model["css"] = ".card{font:28px sans-serif;text-align:left} [data-phase0-controls]{font-size:14px;margin-top:30px}"
        model["tmpls"][0]["qfmt"] = template.with_question_guard('<div>{{Front}}</div>' + probe_block())
        model["tmpls"][0]["afmt"] = template.with_block('{{FrontSide}}<hr id="answer"><div>{{Back}}</div>', api_base,
            template.CLIENT_ID, "click", assets=installed)
        col.models.update_dict(model)
        note = col.new_note(model)
        note["Front"], note["Back"] = "猫がいます。", "Dummy OAuth handoff fixture. No real account."
        note.tags = ["phase0_oauth"]
        col.add_note(note, did)
        apkg = root / "phase0-oauth.apkg"
        col.export_anki_package(out_path=str(apkg), options=ExportAnkiPackageOptions(with_media=True, legacy=True), limit=DeckIdLimit(did))
        manifest = {"api_base": api_base, "apkg": str(apkg), "deck_id": did, "note_id": note.id,
                    "cards": [card.id for card in note.cards()], "assets": installed}
        save_json(root / "oauth-fixture.json", manifest)
        return manifest
    finally:
        col.close()


def main():
    require_anki()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = output_directory(args.output)
    server = serve(root)
    manifest = deck(root, server.fixture_base)
    print(json.dumps({"root": str(root), "port": server.server_port, **manifest}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
