#!/usr/bin/env python3
"""Exercise saved pronunciation and source details in an isolated AnkiQt reviewer."""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import traceback
import zipfile

from fixture_deck import output_directory, require_anki, save_json
from gui_safety import gui_arguments

ADDON = "shirabe_phase0"
PROFILE = "Saved audio"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate(manifest_path: Path) -> tuple[dict, Path]:
    manifest = json.loads(manifest_path.read_text())
    recording = manifest.get("prepared_recording")
    package = manifest_path.with_name("shirabe-ankiweb-prepared-audio.apkg")
    expected_note = next((row for row in manifest.get("notes", [])
                          if row.get("id") == manifest.get("prepared_audio_note_id")), None)
    if (not manifest.get("deck", "").startswith("Shirabe Test ")
            or expected_note is None or expected_note.get("fields", [None])[0] != "猫がいます。"
            or not isinstance(recording, dict)
            or recording.get("filename") not in manifest.get("media", {})
            or manifest["media"][recording["filename"]].get("sha256") != recording.get("sha256")
            or recording.get("mimeType") not in {"audio/mp4", "audio/mpeg"}
            or recording.get("license") != "Permitted for Anki deck synchronization and export"
            or not package.is_file() or digest(package) != manifest.get("apkg_sha256")):
        raise ValueError("Expected the unchanged prepared pronunciation candidate.")
    return manifest, package


def addon_package(root: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "src"
    defaults = json.loads((source / "config.json").read_text())
    if any(defaults.get(key) for key in ("cardToken", "accessToken", "refreshToken", "installId")):
        raise ValueError("Refusing to package runtime credentials or installation identity in defaults")
    path = root / "shirabe-phase0.ankiaddon"
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source.rglob("*")):
            if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc" or file.name == "meta.json":
                continue
            archive.writestr(str(file.relative_to(source)), file.read_bytes())
    return path


def prepare(root: Path, manifest: dict, package: Path) -> Path:
    from anki.collection import Collection
    import anki.lang
    from anki.import_export_pb2 import ImportAnkiPackageRequest
    from aqt.profiles import ProfileManager

    source = root / "source"
    source.mkdir()
    col = Collection(str(source / "collection.anki2"))
    try:
        col.import_anki_package(ImportAnkiPackageRequest(package_path=str(package)))
        expected_ids = {row["id"] for row in manifest["notes"]}
        if set(col.find_notes("")) != expected_ids:
            raise ValueError("Unexpected notes in the prepared pronunciation package.")
        for row in manifest["notes"]:
            note = col.get_note(row["id"])
            if note.guid != row["guid"] or note.fields != row["fields"]:
                raise ValueError(f"Prepared note {row['id']} changed.")
            if [card.id for card in note.cards()] != row["cards"]:
                raise ValueError(f"Prepared card identities changed for note {row['id']}.")
        deck = col.decks.by_name(manifest["deck"])
        if deck is None or any(card.did != deck["id"] for row in manifest["notes"] for card in col.get_note(row["id"]).cards()):
            raise ValueError("Prepared notes left the bounded test deck.")
        col.decks.select(deck["id"])
        media = Path(col.media.dir())
        actual_media = {path.name: {"bytes": path.stat().st_size, "sha256": digest(path)}
                        for path in media.iterdir() if path.is_file()}
        if actual_media != manifest["media"]:
            raise ValueError("Prepared media differs from the candidate manifest.")
    finally:
        col.close()

    base = root / "profile"
    base.mkdir()
    shutil.copytree(source, base / PROFILE)
    anki.lang.set_lang("en")
    pm = ProfileManager(base)
    pm.setupMeta()
    pm.create(PROFILE)
    pm.load(PROFILE)
    pm.meta.update(firstRun=False, defaultLang="en", updates=False, check_for_updates=False,
                   check_for_addon_updates=False)
    pm.profile.update(autoSync=False, syncKey=None, syncMedia=False)
    pm.save()
    pm.db.close()

    archive = addon_package(root)
    installed = base / "addons21" / ADDON
    installed.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(installed)
    config = json.loads((installed / "config.json").read_text())
    config.update(enabled=True, apiBase="https://shirabe.org", reportUsage=False, onlineParsing=False,
                  answerOnly=True, useCollectionStates=False, cardToken="", accessToken="",
                  refreshToken="", installId="")
    save_json(installed / "meta.json", {"disabled": False, "config": config})
    return base


class Driver:
    def __init__(self, app, root: Path, manifest: dict):
        import aqt
        from aqt.webview import AnkiWebPage

        self.app, self.mw, self.root, self.manifest = app, aqt.mw, root, manifest
        self.report = {
            "checks": {},
            "scope": "Current saved-audio APKG in an isolated installed AnkiQt reviewer; real Qt mouse gestures and decoded M4A playback",
            "profile": str(root / "profile"),
            "candidate": {
                "deck": manifest["deck"],
                "note_id": manifest["prepared_audio_note_id"],
                "recording": manifest["prepared_recording"],
                "sdk_sha256": manifest["sdk_sha256"],
                "apkg_sha256": manifest["apkg_sha256"],
            },
        }
        self.console_errors = []
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

    def settle(self, seconds=0.5):
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

    def screenshot(self, name):
        self.settle(0.25)
        path = self.root / f"{name}.png"
        if not self.mw.grab().save(str(path)):
            raise RuntimeError(f"Screenshot failed: {path}")

    def shadow(self, selector, text=None):
        return self.js("""(() => {
          for (const host of document.querySelectorAll('*')) {
            if (!host.shadowRoot) continue;
            for (const element of host.shadowRoot.querySelectorAll(SELECTOR)) {
              if (TEXT !== null && element.textContent.trim() !== TEXT && element.getAttribute('aria-label') !== TEXT) continue;
              element.scrollIntoView({block:'nearest', inline:'nearest'});
              const rect = element.getBoundingClientRect();
              return {x:rect.x+rect.width/2,y:rect.y+rect.height/2,text:element.textContent.trim(),
                visible:!!(rect.width&&rect.height),open:element.parentElement?.open??null};
            }
          }
          return null;
        })()""".replace("SELECTOR", json.dumps(selector)).replace("TEXT", json.dumps(text)))

    def click_shadow(self, selector, text=None):
        from aqt.qt import QPoint, Qt
        from PyQt6.QtTest import QTest

        point = self.shadow(selector, text)
        if not point or not point["visible"]:
            raise AssertionError(f"Missing visible shadow element: {selector} {text or ''}")
        self.settle(0.2)
        point = self.shadow(selector, text)
        target = self.mw.reviewer.web.focusProxy() or self.mw.reviewer.web
        QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(point["x"]), int(point["y"])))

    def popup(self):
        return self.js("""Array.from(document.querySelectorAll('*')).flatMap(host => host.shadowRoot
          ? [...host.shadowRoot.querySelectorAll('.sb-popup')].map(popup => ({text:popup.innerText,
              visible:popup.classList.contains('sb-visible')&&getComputedStyle(popup).visibility==='visible'})) : [])""")

    def run(self):
        from anki.buildinfo import version
        from aqt.qt import PYQT_VERSION_STR, QT_VERSION_STR

        self.wait(lambda: self.mw.col is not None, "isolated profile opened", 40)
        self.report.update(anki=version, qt=QT_VERSION_STR, pyqt=PYQT_VERSION_STR,
                           addon=str(sys.modules[ADDON].__file__))
        self.check("isolated_collection", Path(self.mw.col.path).resolve().is_relative_to(self.root))
        self.check("no_sync_account", not self.mw.pm.profile.get("syncKey") and not self.mw.pm.profile.get("autoSync"))
        config = sys.modules[ADDON].config()
        self.check("offline_click_configuration", config.get("answerOnly") is True
                   and config.get("onlineParsing") is False and not config.get("cardToken"))
        bundle = Path(sys.modules[ADDON].__file__).with_name("web") / "shirabe-card.js"
        self.check("current_sdk_matches_candidate", digest(bundle) == self.manifest["sdk_sha256"])

        note_id = self.manifest["prepared_audio_note_id"]
        expected = next(row for row in self.manifest["notes"] if row["id"] == note_id)
        card_id = expected["cards"][0]
        note = self.mw.col.get_note(note_id)
        before = {
            "fields": note.fields[:],
            "tags": note.tags[:],
            "cards": self.mw.col.db.all("select * from cards where nid=? order by id", note_id),
            "revlog": self.mw.col.db.all("select * from revlog order by id"),
        }

        self.mw.resize(1050, 850)
        self.mw.moveToState("review")
        self.wait(lambda: self.mw.reviewer.card is not None, "real reviewer initialized")
        self.settle(1.5)
        self.mw.reviewer.card = self.mw.col.get_card(card_id)
        self.mw.reviewer.card.start_timer()
        for _ in range(3):
            self.mw.reviewer._showQuestion()
            self.settle(1)
            if self.mw.reviewer.state == "question" and self.js("document.querySelectorAll('.sb-word').length") == 0:
                break
        self.check("question_has_no_lookup_tokens", self.js("document.querySelectorAll('.sb-word').length") == 0)
        self.mw.reviewer._showAnswer()
        self.wait(lambda: self.js("document.querySelectorAll('.sb-word').length") > 0,
                  "answer-side saved token")
        self.check("answer_wraps_saved_cat", self.js("Array.from(document.querySelectorAll('.sb-word')).some(e=>e.textContent==='猫')"))

        self.js("""(() => {
          const OriginalAudio = window.Audio;
          const probe = {events:[],instances:[],original:OriginalAudio};
          document.addEventListener('click', event => {
            const path = event.composedPath();
            if (path.some(element => element?.getAttribute?.('aria-label') === 'Play saved pronunciation')) {
              probe.events.push({type:'play-click',trusted:event.isTrusted,
                active:navigator.userActivation?.isActive??null,hasBeenActive:navigator.userActivation?.hasBeenActive??null});
            }
          }, true);
          probe.observer = new Proxy(OriginalAudio, {construct(target,args){
            const audio = new target(...args), id = probe.instances.push(audio)-1;
            const source = () => ({srcPrefix:audio.src.slice(0,40),srcLength:audio.src.length});
            const originalPlay = audio.play.bind(audio);
            audio.play = () => {
              const result = originalPlay();
              result?.catch(error => probe.events.push({id,type:'play-rejected',name:error.name,message:error.message,
                active:navigator.userActivation?.isActive??null,hasBeenActive:navigator.userActivation?.hasBeenActive??null}));
              return result;
            };
            probe.events.push({id,type:'created',...source(),active:navigator.userActivation?.isActive??null,
              hasBeenActive:navigator.userActivation?.hasBeenActive??null});
            for (const type of ['playing','ended','pause','error']) audio.addEventListener(type, () => probe.events.push({
              id,type,currentTime:audio.currentTime,duration:audio.duration,error:audio.error?.code??null,...source()
            }));
            return audio;
          }});
          window.__shirabeDesktopAudioProbe=probe;
          window.Audio=probe.observer;
          return true;
        })()""")
        self.check("no_audio_before_lookup", self.js("window.__shirabeDesktopAudioProbe.instances.length") == 0)
        self.click_word()
        self.wait(lambda: any(row["visible"] and "cat" in row["text"] for row in self.popup()),
                  "saved cat popup")
        self.screenshot("desktop-saved-audio-popup")
        popup_text = next(row["text"] for row in self.popup() if row["visible"])
        self.check("saved_definition_visible", "ねこ" in popup_text and "cat" in popup_text)
        self.check("saved_audio_button_visible", bool(self.shadow("button", "Play saved pronunciation")))

        self.click_shadow("summary", "Source details")
        self.wait(lambda: bool(self.js("""Array.from(document.querySelectorAll('*')).some(host => host.shadowRoot &&
          [...host.shadowRoot.querySelectorAll('details')].some(details => details.open && details.textContent.includes('Sources saved with this card:')))""")),
                  "source details opened")
        details = self.js("""Array.from(document.querySelectorAll('*')).flatMap(host => host.shadowRoot
          ? [...host.shadowRoot.querySelectorAll('details[open]')].map(details => ({
              text: details.innerText,
              links: [...details.querySelectorAll('.sb-example-reference-link')].map(link => ({
                text: link.textContent, href: link.href, target: link.target, rel: link.rel
              }))
            })) : [])""")
        expected_links = [
            {"text": "Tatoeba sentence 10098762", "href": "https://tatoeba.org/en/sentences/show/10098762",
             "target": "_blank", "rel": "noopener noreferrer"},
            {"text": "Tatoeba sentence 9032775", "href": "https://tatoeba.org/en/sentences/show/9032775",
             "target": "_blank", "rel": "noopener noreferrer"},
            {"text": "Tatoeba", "href": "https://tatoeba.org/", "target": "_blank", "rel": "noopener noreferrer"},
            {"text": "CC BY 2.0 FR", "href": "https://creativecommons.org/licenses/by/2.0/fr/",
             "target": "_blank", "rel": "noopener noreferrer"},
        ]
        self.check("source_details_visible", any("Sources saved with this card:" in row["text"]
                                                and "https://" not in row["text"]
                                                and row["links"] == expected_links for row in details))
        self.screenshot("desktop-saved-source-details")
        self.check("no_audio_before_play", self.js("window.__shirabeDesktopAudioProbe.instances.length") == 0)

        self.click_shadow("button", "Play saved pronunciation")
        self.wait(lambda: self.js("window.__shirabeDesktopAudioProbe.events.some(e=>e.type==='ended')"),
                  "decoded saved playback ended", 12)
        events = self.js("window.__shirabeDesktopAudioProbe.events")
        self.report["audio_events"] = events
        completed = [event for event in events if event["type"] == "ended" and event["error"] is None
                     and event["duration"] > 0 and event["currentTime"] >= event["duration"]]
        self.check("real_saved_playback_completed", len(completed) == 1)
        created = [event for event in events if event["type"] == "created"]
        self.check("verified_data_url_used", len(created) == 1 and created[0]["srcPrefix"].startswith(
            f"data:{self.manifest['prepared_recording']['mimeType']};base64,"))
        self.check("no_playback_error", not any(event["type"] == "error" for event in events))

        current = self.mw.col.get_note(note_id)
        self.check("note_unchanged", current.fields == before["fields"] and current.tags == before["tags"])
        self.check("card_rows_unchanged", before["cards"] == self.mw.col.db.all(
            "select * from cards where nid=? order by id", note_id))
        self.check("revlog_unchanged", before["revlog"] == self.mw.col.db.all("select * from revlog order by id"))
        self.report["console_errors"] = self.console_errors
        self.check("no_console_errors", not self.console_errors)
        self.report["passed"] = all(self.report["checks"].values())
        save_json(self.root / "desktop-saved-audio-report.json", self.report)
        return self.report["passed"]

    def click_word(self):
        from aqt.qt import QPoint, Qt
        from PyQt6.QtTest import QTest

        rect = self.js("""(() => {const element=[...document.querySelectorAll('.sb-word')].find(e=>e.textContent==='猫');
          if(!element)return null;const r=element.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()""")
        if not rect:
            raise AssertionError("No saved cat token in the real reviewer")
        target = self.mw.reviewer.web.focusProxy() or self.mw.reviewer.web
        QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(rect["x"]), int(rect["y"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-open", action="store_true")
    args = gui_arguments(parser)
    require_anki()
    manifest_path = args.manifest or (Path(value) if (value := os.environ.get("SHIRABE_SAVED_AUDIO_MANIFEST")) else None)
    if manifest_path is None:
        parser.error("Pass --manifest or SHIRABE_SAVED_AUDIO_MANIFEST.")
    sys.argv = ["anki"]
    manifest, package = candidate(manifest_path.resolve(strict=True))
    root = output_directory(args.output)
    base = prepare(root, manifest, package)

    import aqt
    from aqt.qt import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    aqt.AnkiApp.KEY = "shirabe-saved-audio-" + root.name
    faulthandler.register(signal.SIGUSR1)
    print(json.dumps({"root": str(root), "base": str(base), "single_instance_key": aqt.AnkiApp.KEY}), flush=True)
    sys.argv = ["anki", "-b", str(base), "-p", PROFILE, "-l", "en"]
    app = aqt._run(sys.argv, exec=False)
    if app is None:
        raise RuntimeError("Isolated Anki did not start")
    try:
        app.appMsg.disconnect(aqt.mw.onAppMsg)
    except TypeError:
        pass
    driver = Driver(app, root, manifest)
    passed = False
    try:
        passed = driver.run()
    except Exception:
        driver.report.update(passed=False, error=traceback.format_exc(), console_errors=driver.console_errors)
        try:
            driver.report["failure_dom"] = driver.js("""({words:[...document.querySelectorAll('.sb-word')].map(e=>e.textContent),
              shadows:[...document.querySelectorAll('*')].filter(e=>e.shadowRoot).map(e=>e.shadowRoot.textContent.slice(-4000)),
              audio:window.__shirabeDesktopAudioProbe?.events??[]})""")
            driver.screenshot("desktop-saved-audio-failure")
        except Exception:
            driver.report["failure_capture_error"] = traceback.format_exc()
        save_json(root / "desktop-saved-audio-report.json", driver.report)
        traceback.print_exc()
    print(json.dumps({"root": str(root), "passed": passed,
                      "report": str(root / "desktop-saved-audio-report.json")}), flush=True)
    if args.keep_open:
        app.exec()
    else:
        aqt.mw.close()
        driver.settle(1)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
