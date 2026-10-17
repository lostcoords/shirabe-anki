#!/usr/bin/env python3
"""Exercise installed AnkiQt, the real add-on, and its reviewer in a new profile."""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit
import zipfile

from fixture_deck import HERE, create_fixture, output_directory, require_anki, save_json
from gui_safety import gui_arguments

ADDON = "shirabe_phase0"
PROFILE = "Phase0"


def fixture_api():
    from offline_fixture import candidate, parse

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_OPTIONS(self):
            self.respond({}, 204)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def respond(self, body, status=200):
            encoded = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body) if body else {}
            requests.append({"method": self.command, "path": self.path, "body": data})
            path = urlsplit(self.path).path
            if getattr(self.server, "fixture_offline", False):
                self.respond({"title": "Fixture dictionary is offline"}, 503)
                return
            if path == "/api/v1/me":
                self.respond({"credential": {"scopes": ["user:account:read", "user:dictionary:read", "user:study:read"]},
                              "preferences": {"uiLocale": "en"}})
            elif path == "/api/v1/preferences":
                self.respond({"preferences": {"uiLocale": "en"}})
            elif path == "/api/v1/dictionary-stack":
                self.respond({"dictionaryStack": {"fingerprint": "phase0-fixture", "sources": [
                    {"dictionary": "jmdict", "languages": ["en"], "reveal": "show"},
                ]}})
            elif path == "/api/v1/dictionaries":
                self.respond({"dictionaries": [{"slug": "jmdict", "name": "Phase0 synthetic dictionary", "kind": "definition",
                                               "languages": ["en"], "visibility": "official", "entryCount": 16,
                                               "capabilities": ["search", "definitions", "offline"],
                                               "attribution": {"source": {"name": "Deterministic test data", "url": "https://example.test"},
                                                               "license": {"name": "CC0-1.0"}}}]})
            elif path == "/api/v1/parse":
                self.respond({**parse(data["texts"][0]), "stackFingerprint": "phase0-fixture"})
            elif path == "/api/v1/words/identify":
                self.respond({"words": [{"candidates": [candidate(token.get("lemma") or token.get("surface") or "猫")]} for token in data["tokens"]],
                              "stackFingerprint": "phase0-fixture"})
            elif path == "/api/v1/srs/states/lookup":
                self.respond({"states": []})
            else:
                self.respond({"title": "No fixture for this request"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


def package(root):
    source = HERE.parent / "src"
    defaults = json.loads((source / "config.json").read_text())
    if any(defaults.get(key) for key in ("cardToken", "accessToken", "refreshToken", "installId")):
        raise ValueError("Refusing to package runtime credentials or installation identity in defaults")
    path = root / "shirabe-phase0.ankiaddon"
    files = {}
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source.rglob("*")):
            if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc" or file.name == "meta.json":
                continue
            relative = str(file.relative_to(source))
            data = file.read_bytes()
            archive.writestr(relative, data)
            files[relative] = hashlib.sha256(data).hexdigest()
    save_json(root / "package-manifest.json", {"package": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "files": files})
    return path


def prepare(root, api_base):
    from anki.collection import Collection
    import anki.lang
    from aqt.profiles import ProfileManager
    from shirabe_addon.authoring import FieldProfile, remember_profile

    manifest = create_fixture(root, portable=True, prepared=True)
    base = root / "profile"
    base.mkdir()
    shutil.copytree(root / "source", base / PROFILE)
    prepared = next(note for note in manifest["notes"] if note["case"] == "prepared")
    col = Collection(str(base / PROFILE / "collection.anki2"))
    model = col.models.get(prepared["notetype"])
    for name in ("Reading", "Entry"):
        col.models.add_field(model, col.models.new_field(name))
    col.models.update_dict(model)
    col.decks.select(prepared["deck"])
    col.close()

    anki.lang.set_lang("en")
    pm = ProfileManager(base)
    pm.setupMeta()
    pm.create(PROFILE)
    pm.load(PROFILE)
    pm.meta.update(firstRun=False, defaultLang="en", updates=False, check_for_updates=False, check_for_addon_updates=False)
    pm.profile.update(autoSync=False, syncKey=None, syncMedia=False)
    pm.save()
    pm.db.close()

    archive = package(root)
    installed = base / "addons21" / ADDON
    installed.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(installed)
    config = json.loads((installed / "config.json").read_text())
    config.update(apiBase=api_base, reportUsage=False, onlineParsing=False, answerOnly=True,
                  cardToken=json.dumps({"shirabe.link": json.dumps({"access": "phase0-local-fixture-not-a-real-token", "expiresAt": int(time.time() * 1000) + 86_400_000})}))
    config = remember_profile(config, FieldProfile("Phase0", model["id"], {
        "expression": "Front", "meanings": "Back", "reading": "Reading", "entry": "Entry",
    }, deck_id=prepared["deck"], tags=("phase0_authored",)))
    save_json(installed / "meta.json", {"disabled": False, "config": config})
    return base, manifest


class Driver:
    def __init__(self, app, root, manifest, requests, server):
        import aqt
        from aqt.qt import QTimer
        self.app, self.mw, self.root = app, aqt.mw, root
        self.manifest, self.requests = manifest, requests
        self.server = server
        self.report = {"checks": {}, "scope": "Installed AnkiQt reviewer and loaded add-on; deterministic loopback dictionary API", "profile": str(root / "profile")}
        self.modal_action = None
        self.prepare_consent_pending = False
        self.timer = QTimer()
        self.timer.timeout.connect(self.modal_tick)
        self.timer.start(100)
        self.console_errors = []
        from aqt.webview import AnkiWebPage
        original_console = AnkiWebPage.javaScriptConsoleMessage
        def console(page, level, message, line, source):
            if level.name == "ErrorMessageLevel":
                self.console_errors.append({"message": message, "line": line, "source": source})
            original_console(page, level, message, line, source)
        AnkiWebPage.javaScriptConsoleMessage = console

    def wait(self, predicate, label, seconds=15):
        from aqt.qt import QEventLoop
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            result = predicate()
            if result:
                return result
            time.sleep(0.01)
        raise TimeoutError(label)

    def settle(self, seconds=0.7):
        until = time.monotonic() + seconds
        self.wait(lambda: time.monotonic() >= until, "settle", seconds + 2)

    def js(self, source):
        results = []
        self.mw.reviewer.web.evalWithCallback(source, results.append)
        self.wait(lambda: bool(results), "JavaScript callback")
        return results[0]

    def check(self, name, actual):
        self.report["checks"][name] = bool(actual)
        print(json.dumps({"check": name, "passed": bool(actual)}), flush=True)
        if not actual:
            raise AssertionError(name)

    def screenshot(self, name, widget=None):
        path = self.root / f"{name}.png"
        if not (widget or self.mw).grab().save(str(path)):
            raise RuntimeError(f"Screenshot failed: {path}")

    def popup(self):
        return self.js("Array.from(document.querySelectorAll('*')).flatMap(e=>e.shadowRoot?[...e.shadowRoot.querySelectorAll('.sb-popup')].map(p=>({text:p.innerText,visible:p.classList.contains('sb-visible')&&getComputedStyle(p).visibility==='visible'})):[])")

    def modal_tick(self):
        from aqt.qt import QCheckBox, QDialogButtonBox, QMessageBox
        dialog = self.app.activeModalWidget()
        if not dialog:
            return
        if type(dialog).__name__ == "ChoiceDialog" and self.modal_action:
            name, self.modal_action = self.modal_action, None
            self.screenshot(name, dialog)
            self.report[name] = {"window": dialog.windowTitle(), "fields": dialog.preview.field_values(), "tags": list(dialog.preview.tags)}
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        elif dialog.windowTitle() == "Prepare for offline lookup" and self.prepare_consent_pending:
            self.prepare_consent_pending = False
            permission = next(box for box in dialog.findChildren(QCheckBox) if box.text() == "Send visible card text to Shirabe")
            buttons = dialog.findChild(QDialogButtonBox)
            start = buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.report["native_prepare_consent"] = {
                "permission_checked_initially": permission.isChecked(), "start_enabled_initially": start.isEnabled(),
                "requests_before_consent": self.requests[self.prepare_request_start:],
            }
            self.screenshot("desktop-native-prepare-consent", dialog)
            permission.setChecked(True)
            start.click()
        elif isinstance(dialog, QMessageBox):
            self.report.setdefault("messages", []).append(dialog.text())
            dialog.accept()

    def prepare_native(self):
        import aqt
        from anki.collection import Collection, NoteIdsLimit
        from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest
        from aqt.qt import QAction
        from importlib import import_module
        offline = import_module(ADDON + ".offline")
        addon = sys.modules[ADDON]
        targets = [n for n in self.manifest["notes"] if n["case"] in ("basic", "ruby-inline", "cloze")]
        ids = [n["id"] for n in targets]
        originals = {nid: {"fields": dict(self.mw.col.get_note(nid).items()), "tags": self.mw.col.get_note(nid).tags[:],
                           "guid": self.mw.col.get_note(nid).guid} for nid in ids}
        original_cards = self.mw.col.db.all("select * from cards order by id")
        original_revlog = self.mw.col.db.all("select * from revlog order by id")
        self.check("native_prepare_starts_without_snapshot_field", all("ShirabeLookup" not in value["fields"] for value in originals.values()))
        self.prepare_request_start = len(self.requests)
        self.prepare_consent_pending = True
        browser = aqt.dialogs.open("Browser", self.mw)
        self.settle()
        browser.search_for("nid:" + ",".join(map(str, ids)))
        browser.table.select_all()
        self.wait(lambda: set(browser.selected_notes()) == set(ids), "select native preparation targets")
        action = next(action for action in browser.findChildren(QAction) if action.text() == "Prepare selected notes for offline lookup…")
        action.trigger()
        workflow = self.wait(lambda: getattr(browser, "_shirabe_offline", None), "native preparation window")
        self.wait(lambda: workflow.finished_work, "native offline preparation finished", 55)
        self.settle()
        self.report["native_prepare_collected_runs"] = workflow.texts
        self.report["native_prepare_requests"] = self.requests[self.prepare_request_start:]
        self.check("native_prepare_requires_explicit_consent", not self.report["native_prepare_consent"]["permission_checked_initially"] and not self.report["native_prepare_consent"]["start_enabled_initially"])
        self.check("native_prepare_metadata_only_before_consent", all(r["path"] in ("/api/v1/me", "/api/v1/dictionary-stack", "/api/v1/dictionaries") for r in self.report["native_prepare_consent"]["requests_before_consent"]))
        snapshots = {}
        for target in targets:
            note = self.mw.col.get_note(target["id"])
            self.check("native_prepare_wrote_" + target["case"], "ShirabeLookup" in note and bool(note["ShirabeLookup"]))
            raw = note["ShirabeLookup"]
            names = __import__("re").findall(r"_shirabe_snapshot_v[12]_[a-f0-9]{64}\.json", raw)
            self.check("native_prepare_reference_" + target["case"], len(set(names)) == 1)
            filename = names[0]
            snapshot = offline.read_snapshot(self.mw.col.media, filename)
            self.check("native_prepare_valid_snapshot_" + target["case"], snapshot is not None)
            snapshots[target["case"]] = {"note_id": note.id, "field": raw, "filename": filename, "snapshot": snapshot}
            original = originals[note.id]
            self.check("native_prepare_preserves_" + target["case"], all(note[name] == value for name, value in original["fields"].items()) and note.tags == original["tags"] and note.guid == original["guid"])
        self.report["native_prepared_snapshots"] = snapshots
        self.check("native_prepare_preserves_card_rows", original_cards == self.mw.col.db.all("select * from cards order by id"))
        self.check("native_prepare_preserves_revlog", original_revlog == self.mw.col.db.all("select * from revlog order by id"))
        self.report["native_prepare_undo_label"] = self.mw.col.undo_status().undo
        self.check("native_prepare_named_undo", self.mw.col.undo_status().undo == "Prepare Shirabe offline lookup")
        apkg = self.root / "native-prepared.apkg"
        self.mw.col.export_anki_package(out_path=str(apkg), options=ExportAnkiPackageOptions(with_media=True, legacy=True), limit=NoteIdsLimit(ids))
        imported_dir = self.root / "native-roundtrip"
        imported_dir.mkdir()
        imported = Collection(str(imported_dir / "collection.anki2"))
        try:
            imported.import_anki_package(ImportAnkiPackageRequest(package_path=str(apkg)))
            self.check("native_prepared_apkg_note_count", imported.note_count() == len(ids))
            for key, value in snapshots.items():
                self.check("native_prepared_apkg_media_" + key, offline.read_snapshot(imported.media, value["filename"]) == value["snapshot"])
        finally:
            imported.close()
        self.report["native_prepared_apkg"] = str(apkg)
        browser.close()
        self.settle()
        addon.save_config({**addon.config(), "cardToken": "", "onlineParsing": False, "answerOnly": True})
        self.server.fixture_offline = True
        request_count = len(self.requests)
        self.mw.reviewer._initWeb()
        self.settle()
        self.show("basic")
        self.wait(lambda: self.js("document.querySelectorAll('.sb-word').length") > 0, "native prepared tokens in fresh reviewer")
        self.click_word()
        self.screenshot("desktop-native-prepared-offline-popup")
        self.report["native_prepared_popup"] = self.popup()
        self.report["native_prepared_offline_trace"] = self.js("ShirabePhase0.events")
        self.check("native_prepared_popup_without_api", len(self.requests) == request_count)

    def show(self, case, answer=True):
        note = next(n for n in self.manifest["notes"] if n["case"] == case)
        self.mw.reviewer.card = self.mw.col.get_card(note["cards"][0]["id"])
        self.mw.reviewer.card.start_timer()
        self.mw.reviewer._showQuestion()
        self.settle()
        if answer:
            self.mw.reviewer._showAnswer()
            self.settle()
        return note

    def click_word(self):
        from aqt.qt import QPoint, Qt
        from PyQt6.QtTest import QTest
        rect = self.js("(() => {const e=document.querySelector('.sb-word');if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
        if not rect:
            raise AssertionError("No wrapped word in real reviewer")
        target = self.mw.reviewer.web.focusProxy() or self.mw.reviewer.web
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=QPoint(int(rect["x"]), int(rect["y"])))
        self.wait(lambda: any(p["visible"] and "cat" in p["text"] for p in self.popup()), "offline popup")
        self.settle(0.3)

    def raw_point(self, text):
        return self.js("""(() => {const root=document.getElementById('qa'), w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);for(let n=w.nextNode();n;n=w.nextNode()){if(n.parentElement.closest('script,style,[hidden],rt'))continue;const i=n.textContent.indexOf(__TEXT__);if(i<0)continue;const r=document.createRange();r.setStart(n,i);r.setEnd(n,i+1);const b=r.getBoundingClientRect();return {x:b.x+b.width/4,y:b.y+b.height/2}}return null})()""".replace("__TEXT__", json.dumps(text)))

    def raw_mouse(self, text, click=False):
        from aqt.qt import QPoint, Qt
        from PyQt6.QtTest import QTest
        rect = self.raw_point(text)
        if not rect:
            raise AssertionError("Raw text is missing: " + text)
        target = self.mw.reviewer.web.focusProxy() or self.mw.reviewer.web
        point = QPoint(int(rect["x"]), int(rect["y"]))
        if click:
            QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=point)
        else:
            QTest.mouseMove(target, point)

    def run(self, prepare_offline=False):
        import aqt
        from anki.buildinfo import version
        from aqt.qt import QT_VERSION_STR, PYQT_VERSION_STR
        self.wait(lambda: self.mw.col is not None, "isolated profile opened", 40)
        self.report.update(anki=version, qt=QT_VERSION_STR, pyqt=PYQT_VERSION_STR,
                           addon=str(sys.modules[ADDON].__file__), reviewer_class=type(self.mw.reviewer).__name__)
        self.check("isolated_collection", Path(self.mw.col.path).resolve().is_relative_to(self.root))
        self.check("no_sync_account", not self.mw.pm.profile.get("syncKey") and not self.mw.pm.profile.get("autoSync"))
        config = sys.modules[ADDON].config()
        self.check("only_loopback_fixture_api", urlsplit(config["apiBase"]).hostname == "127.0.0.1")
        self.check("analytics_disabled", config.get("reportUsage") is False)
        self.check("only_fixture_credential", json.loads(json.loads(config["cardToken"])["shirabe.link"])["access"] == "phase0-local-fixture-not-a-real-token")
        self.mw.resize(1050, 850)
        self.mw.moveToState("review")
        self.wait(lambda: self.mw.reviewer.card is not None, "real reviewer initialized")
        self.settle(1.5)
        self.check("webview_storage_is_ephemeral", self.mw.reviewer.web.page().profile().isOffTheRecord())
        self.check("question_no_api_requests", not self.requests)
        prepared = self.show("prepared")
        self.wait(lambda: self.js("document.querySelectorAll('.sb-word').length") == 3, "prepared wrapped tokens")
        self.click_word()
        self.screenshot("desktop-offline-popup")
        self.report["offline_popup"] = self.popup()
        self.check("offline_definition_no_api_requests", not self.requests)
        self.check("popup_has_no_field_chooser", all("Copy and choose fields" not in p["text"] and "Add Anki note" not in p["text"] for p in self.report["offline_popup"]))

        self.show("basic")
        self.settle(1)
        self.check("default_click_only_no_background_api", not self.requests)
        self.report["default_readable_runs"] = self.js("ShirabeCard.collectReadableRuns(document.getElementById('qa'))")
        self.check("default_unwrapped", self.js("document.querySelectorAll('.sb-word').length") == 0)
        self.report["requests_before_online_consent"] = self.requests[:]
        self.raw_mouse("猫")
        self.settle()
        self.check("hover_no_api_requests", not self.requests)
        self.raw_mouse("猫", click=True)
        self.wait(lambda: any(p["visible"] and "cat" in p["text"] for p in self.popup()), "clicked online definition")
        self.settle(0.3)
        self.screenshot("desktop-click-online-popup")
        self.report["clicked_requests"] = self.requests[:]
        self.check("click_fetches_account_on_demand", any(r["path"] == "/api/v1/me" for r in self.requests))
        self.check("click_identifies_word", any(r["path"].startswith("/api/v1/words/identify") for r in self.requests))
        self.check("click_does_not_upload_answer", "ANSWER_ONLY" not in json.dumps(self.requests))
        request_start = len(self.requests)
        addon = sys.modules[ADDON]
        addon.save_config({**addon.config(), "onlineParsing": True, "answerOnly": False})
        self.js("window.__phase0Attach=[];window.__phase0OriginalAttach=ShirabeCard.attach;ShirabeCard.attach=function(o){__phase0Attach.push({parse:o.parse,opacity:getComputedStyle(o.root).opacity,runs:ShirabeCard.collectReadableRuns(o.root)});return __phase0OriginalAttach(o)}")
        self.show("cloze", answer=False)
        self.report["cloze_settings"] = self.js("window.__shirabe&&({onlineParsing:__shirabe.onlineParsing,native:__shirabe.native,apiBase:__shirabe.apiBase,cardId:__shirabe.cardId,stored:!!__shirabe.stored})")
        self.report["cloze_attachment_observations"] = self.js("({atAttach:__phase0Attach,after:ShirabeCard.collectReadableRuns(document.getElementById('qa')),opacity:getComputedStyle(document.getElementById('qa')).opacity})")
        self.settle(1)
        auto_parsed = any(urlsplit(r["path"]).path == "/api/v1/parse" for r in self.requests[request_start:])
        self.report["checks"]["online_consent_parses_on_render"] = auto_parsed
        if not auto_parsed:
            self.report["diagnostic_only"] = "Original render did not parse. Reopened the same loader after visibility settled to test the opacity hypothesis; this does not turn the failing render check into a pass."
            self.js("ShirabeAnki.open(window.__shirabe);true")
        self.wait(lambda: any(urlsplit(r["path"]).path == "/api/v1/parse" for r in self.requests[request_start:]), "explicit online parse")
        self.settle(1)
        parse_texts = [text for r in self.requests[request_start:] if urlsplit(r["path"]).path == "/api/v1/parse"
                       for text in r["body"].get("texts", [])]
        self.report["opted_in_cloze_parse_texts"] = parse_texts
        self.check("online_consent_causes_parse", bool(parse_texts))
        self.check("hidden_active_cloze_not_uploaded", all("寿司" not in t and "CLOZE_EXTRA_ONLY" not in t for t in parse_texts))
        self.screenshot("desktop-cloze-online-consent")
        addon.save_config({**addon.config(), "onlineParsing": False, "answerOnly": True})
        self.show("prepared")
        self.click_word()
        self.screenshot("desktop-final-offline-popup")
        if prepare_offline:
            self.prepare_native()
        self.report["checks"]["native_add_no_saveNow_error"] = not any("saveNow is not defined" in error["message"] for error in self.console_errors)
        self.report["requests"] = self.requests[:]
        self.report["console_errors"] = self.console_errors
        self.report["passed"] = all(self.report["checks"].values())
        save_json(self.root / "desktop-report.json", self.report)
        return self.report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--prepare-offline", action="store_true", help="Run Browser consent, real text capture/preparation, APKG roundtrip and a fresh signed-out offline reviewer.")
    args = gui_arguments(parser)
    require_anki()
    root = output_directory(args.output)
    server, requests = fixture_api()
    base, manifest = prepare(root, f"http://127.0.0.1:{server.server_port}")
    import aqt
    from aqt.qt import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    aqt.AnkiApp.KEY = "shirabe-phase0-" + root.name
    faulthandler.register(signal.SIGUSR1)
    print(json.dumps({"root": str(root), "base": str(base), "single_instance_key": aqt.AnkiApp.KEY}), flush=True)
    sys.argv = ["anki", "-b", str(base), "-p", PROFILE, "-l", "en"]
    app = aqt._run(sys.argv, exec=False)
    if app is None:
        raise RuntimeError("Isolated Anki did not start")
    driver = Driver(app, root, manifest, requests, server)
    passed = False
    try:
        passed = driver.run(prepare_offline=args.prepare_offline)
    except Exception:
        driver.report.update(passed=False, error=traceback.format_exc(), requests=requests[:], console_errors=driver.console_errors)
        driver.report["failure_dom"] = driver.js("({popup:Array.from(document.querySelectorAll('*')).filter(e=>e.shadowRoot).map(e=>e.shadowRoot.textContent.slice(-3000)),words:Array.from(document.querySelectorAll('.sb-word')).map(e=>({text:e.textContent,rect:e.getBoundingClientRect().toJSON()}))})")
        save_json(root / "desktop-report.json", driver.report)
        driver.screenshot("desktop-failure")
        traceback.print_exc()
    print(json.dumps({"root": str(root), "passed": passed, "report": str(root / "desktop-report.json")}), flush=True)
    if args.keep_open:
        app.exec()
    else:
        driver.timer.stop()
        aqt.mw.close()
        driver.settle(1)
    server.shutdown()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
