#!/usr/bin/env python3
"""Smoke native authoring in installed Anki with an isolated profile and loopback API."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import desktop_check
from fixture_deck import output_directory, require_anki, save_json
from offline_fixture import candidate

RECORDING_URLS = ["https://cdn.shirabe.org/audio/words/" + digit * 64 + ".m4a" for digit in ("a", "b")]
LIVE_RECORDING_URL = "https://cdn.shirabe.org/audio/words/231d52edbc4efb2b35e7603c18c0050f0150846e17b28208e10219a0b63dd448.mp3"
LIVE_RECORDING_SHA256 = "429847003510fe7eac3c569ea31138a2dbc61cdbe473b3061113e4cf8083d201"
LIVE_RECORDING_BYTES = 10700


def import_context():
    context = []
    frame = sys._getframe(1)
    try:
        while frame:
            if frame.f_globals.get("__name__", "").startswith("aqt.") and frame.f_code.co_name in ("import_file", "importFile", "handleImport", "onAppMsg"):
                values = {name: frame.f_locals[name] for name in ("path", "file", "buf") if isinstance(frame.f_locals.get(name), str)}
                context.append({"source": frame.f_code.co_filename, "line": frame.f_lineno,
                                "function": frame.f_code.co_name, **values})
            frame = frame.f_back
    finally:
        del frame
    return context


def audio_fixture(root, extension):
    codec = {"m4a": "aac", "mp3": "libmp3lame"}[extension]
    tone = root / f"synthetic-tone.{extension}"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.15",
                    "-c:a", codec, "-b:a", "96k", "-ac", "1", "-ar", "48000", str(tone)], check=True)
    return tone.read_bytes()


class RecordingDownload:
    def __init__(self, media, root, audio_format="m4a", live_test_recording=False):
        self.live = live_test_recording
        self.audio_format = "mp3" if self.live else audio_format
        self.urls = [LIVE_RECORDING_URL] if self.live else [url[:-4] + "." + self.audio_format for url in RECORDING_URLS]
        self.bytes = None if self.live else audio_fixture(root, self.audio_format)
        self.requests = []
        self.completed = []
        self.download = media.download_selected_media

    def __call__(self, selection, **kwargs):
        if selection.url not in self.urls:
            raise AssertionError("Unexpected recording URL")
        if self.live and "opener" in kwargs:
            raise AssertionError("Live recording must use the production transport")
        self.requests.append(selection.url)
        if self.live:
            kwargs["max_bytes"] = min(kwargs.get("max_bytes", 1_048_576), 1_048_576)
        else:
            transport = self
            class FixtureOpener:
                def open(_, request, timeout):
                    if len(transport.requests) == 1:
                        raise OSError("Synthetic connection interrupted")
                    response = io.BytesIO(transport.bytes)
                    response.headers = {"Content-Type": "audio/mpeg" if transport.audio_format == "mp3" else "audio/mp4",
                                        "Content-Length": str(len(transport.bytes))}
                    return response
            kwargs["opener"] = FixtureOpener()
        downloaded = self.download(selection, **kwargs)
        digest = hashlib.sha256(downloaded.data).hexdigest()
        if self.live and (downloaded.extension != "mp3" or len(downloaded.data) != LIVE_RECORDING_BYTES or digest != LIVE_RECORDING_SHA256):
            raise AssertionError("Downloaded bytes do not match the approved MP3")
        self.bytes = downloaded.data
        self.completed.append({"url": selection.url, "bytes": len(downloaded.data), "sha256": digest, "extension": downloaded.extension})
        return downloaded


def fixture_api(audio_format="m4a", live_test_recording=False):
    server, requests = desktop_check.fixture_api()
    server.fixture_audio_format = "mp3" if live_test_recording else audio_format
    server.live_test_recording = live_test_recording

    class Handler(server.RequestHandlerClass):
        def handle_request(self):
            path = urlsplit(self.path).path
            if self.command == "GET" and (path.startswith("/api/v1/words/") or path == "/api/v1/sentences"):
                requests.append({"method": self.command, "path": self.path, "body": {}})
                example = {"id": "tatoeba:fixture-1", "japanese": "猫が窓の外を見ています。", "source": "Synthetic corpus fixture",
                           "translations": [{"lang": "en", "text": "A cat is looking out the window."}]}
                if path == "/api/v1/sentences":
                    self.respond({"sentences": [example]})
                else:
                    detail = candidate(source_id="word:cat-1")
                    detail["examples"] = [example]
                    detail["pitch"] = [{"downstep": 1, "pattern": "HL", "audioUrl": RECORDING_URLS[0]},
                                       {"downstep": 0, "pattern": "LH", "audioUrl": RECORDING_URLS[1]}]
                    if live_test_recording:
                        detail["reading"] = "ねこ"
                        detail["pitch"] = [{"downstep": 1, "pattern": "atamadaka", "audioUrl": LIVE_RECORDING_URL[:-4] + ".m4a",
                                            "audioDownloadUrl": LIVE_RECORDING_URL}]
                    elif audio_format == "mp3":
                        for row in detail["pitch"]:
                            row["audioDownloadUrl"] = row["audioUrl"][:-4] + ".mp3"
                    self.respond(detail)
            else:
                super().handle_request()

    server.RequestHandlerClass = Handler
    return server, requests


class Driver(desktop_check.Driver):
    def modal_tick(self):
        from aqt.qt import QDialog, QLabel, QMessageBox, QProgressDialog, QTimer
        dialog = self.app.activeModalWidget()
        if isinstance(dialog, QMessageBox):
            self.report.setdefault("messages", []).append(dialog.text())
            self.report.setdefault("message_contexts", []).append({"message": dialog.text(), "imports": import_context(),
                "checks_completed": len(self.report["checks"]), "media_requests": list(getattr(self, "media_requests", []))})
            dialog.accept()
        elif type(dialog).__name__ == "RecordingDialog":
            if not getattr(dialog, "fixture_approval", False):
                dialog.fixture_approval = True
                QTimer.singleShot(0, lambda: self.approve_recording(dialog))
            return
        elif isinstance(dialog, QDialog) and not isinstance(dialog, QProgressDialog) and type(dialog).__name__ != "ChoiceDialog":
            self.report.setdefault("unexpected_dialogs", []).append({"title": dialog.windowTitle(),
                "text": "\n".join(label.text() for label in dialog.findChildren(QLabel))})
            dialog.reject()
        if type(dialog).__name__ != "ChoiceDialog" or getattr(self, "approving", False):
            return
        self.approving = True
        approve = getattr(self, "material_action", self.approve_dialog)
        QTimer.singleShot(0, lambda: approve(dialog))

    def approve_dialog(self, dialog):
        from aqt.qt import QDialogButtonBox, QPushButton, QScrollArea
        try:
            self.check("current_sentence_before_search", dialog.sentence.toPlainText() == "現在の文には猫がいます。")
            more = next(button for button in dialog.findChildren(QPushButton) if button.text().startswith("Find more examples"))
            more.click()
            dialog.search_examples_button.click()
            self.wait(lambda: bool(dialog.lookup.examples), "native corpus choices")
            self.check("search_preserves_current_sentence", dialog.sentence.toPlainText() == "現在の文には猫がいます。")
            self.check("corpus_deduplicates_entry_and_surface_results", len(dialog.lookup.examples) == 1)
            dialog.example.setCurrentIndex(dialog.example.findData(0))
            self.check("explicit_example_selection", dialog.sentence.toPlainText() == "猫が窓の外を見ています。")
            self.check("example_attribution_preserved", "Synthetic corpus fixture" in dialog.source.text() and "tatoeba:fixture-1" in dialog.source.text())
            self.check("audio_has_no_automatic_selection", dialog.media is None)
            self.recording_attempt = 0
            self.retry_audio_download = not self.server.live_test_recording
            dialog.audio_choose.click()
            self.check("audio_picker_cancel_does_not_select_or_download", dialog.media is None and not self.media_requests)
            dialog.audio_choose.click()
            if self.report.get("audio_approval_error"):
                raise AssertionError(self.report["audio_approval_error"])
            self.check("audio_selection_is_in_field_preview", dialog.media is not None and dialog.media.filename in dialog.preview.field_values()["Audio"])
            self.report["audio_outcome"] = dialog.audio_status.text()
            self.report["approved_fields"] = dialog.preview.field_values()
            more.setChecked(False)
            scroll = dialog.findChild(QScrollArea)
            for width, height in ((560, 480), (1000, 900), (740, 640)):
                dialog.resize(width, height)
                self.settle(0.1)
                self.check(f"material_wraps_at_{width}", scroll.horizontalScrollBar().maximum() == 0 and
                           all(label.heightForWidth(label.width()) <= label.height() <= label.heightForWidth(label.width()) + 2
                               for label in (dialog.example_status, dialog.translation, dialog.source)))
                if width == 560:
                    scroll.ensureWidgetVisible(dialog.source)
                    self.settle(0.1)
                    self.screenshot("authoring-material-narrow", dialog)
            scroll.verticalScrollBar().setValue(0)
            self.settle(0.1)
            self.report["material_layout"] = {
                "example_gap": dialog.example_status.y() - more.geometry().bottom(),
                "source_height": dialog.source.height(),
                "source_required_height": dialog.source.heightForWidth(dialog.source.width()),
            }
            self.screenshot("authoring-material-preview", dialog)
            self.check("collapsed_examples_leave_no_blank_panel", self.report["material_layout"]["example_gap"] <= 20)
            self.check("attribution_has_room_for_every_line", dialog.source.height() >= dialog.source.heightForWidth(dialog.source.width()))
            scroll.ensureWidgetVisible(dialog.fields)
            self.settle(0.1)
            self.screenshot("authoring-field-preview", dialog)
            source_row = list(dialog.profile.fields).index("source")
            dialog.fields.table.scrollToItem(dialog.fields.table.item(source_row, 3))
            self.settle(0.1)
            self.screenshot("authoring-audio-field-preview", dialog)
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        except Exception:
            self.report["approval_error"] = traceback.format_exc()
            dialog.reject()

    def approve_recording(self, dialog):
        try:
            selected_index = 1 if self.server.live_test_recording else 2
            self.wait(lambda: dialog.recordings.count() == selected_index + 1, "verified recording choices")
            self.check("audio_requires_individual_selection", not dialog.download.isEnabled())
            self.recording_attempt += 1
            if self.recording_attempt == 1:
                dialog.reject()
                return
            self.check("audio_permission_is_scoped_to_anki", dialog.permission.text() == "Shirabe\nPermitted for Anki deck synchronization and export")
            accent = 1 if self.server.live_test_recording else 0
            self.check("audio_labels_include_reading_and_accent", "ねこ" in dialog.recordings.itemText(selected_index)
                       and f"accent {accent}" in dialog.recordings.itemText(selected_index))
            dialog.recordings.setCurrentIndex(selected_index)
            if self.audio_format == "m4a":
                self.check("m4a_requires_explicit_format_override", not dialog.allow_m4a.isChecked() and not dialog.download.isEnabled())
                self.check("m4a_explains_ankiweb_limitation", dialog.allow_m4a.isVisible() and "AnkiWeb" in dialog.format_notice.text())
                self.screenshot("authoring-audio-m4a-warning", dialog)
                dialog.allow_m4a.click()
                self.check("m4a_format_override_enables_download", dialog.download.isEnabled() and dialog.allow_m4a.isChecked())
            else:
                self.check("verified_mp3_needs_no_format_override", dialog.download.isEnabled() and not dialog.allow_m4a.isVisible()
                           and not dialog.allow_m4a.isChecked() and "MP3" in dialog.download.text())
            self.screenshot("authoring-audio-selection", dialog)
            dialog.download.click()
            if self.retry_audio_download:
                self.wait(lambda: dialog.download.isEnabled() and "Retry" in dialog.download.text(), "native audio download retry")
                self.check("audio_download_failure_is_retryable", "interrupted" in dialog.status.text())
                dialog.download.click()
        except Exception:
            self.report["audio_approval_error"] = traceback.format_exc()
            dialog.reject()

    def editor_js(self, editor, source):
        results = []
        editor.web.evalWithCallback(source, results.append)
        self.wait(lambda: bool(results), "Editor JavaScript callback")
        return results[0]

    def approve_audio_update(self, dialog):
        from aqt.qt import QDialogButtonBox, QScrollArea
        try:
            dialog.audio_choose.click()
            if self.report.get("audio_approval_error"):
                raise AssertionError(self.report["audio_approval_error"])
            self.check("update_audio_keeps_existing_by_default", dialog.preview.field_values()["Audio"] == self.update_audio_before)
            audio = dialog.fields.actions["audio"]
            audio.setCurrentIndex(audio.findData("append"))
            self.check("update_audio_can_append_selected_recording", self.update_audio_before in dialog.preview.field_values()["Audio"] and
                       dialog.media.filename in dialog.preview.field_values()["Audio"])
            source = dialog.fields.actions["source"]
            source.setCurrentIndex(source.findData("keep"))
            save = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
            self.check("audio_attribution_required_before_save", not save.isEnabled() and "attribution" in dialog.problem.text())
            source.setCurrentIndex(source.findData("append"))
            dialog.findChild(QScrollArea).ensureWidgetVisible(dialog.fields)
            source_row = list(dialog.profile.fields).index("source")
            dialog.fields.table.scrollToItem(dialog.fields.table.item(source_row, 3))
            self.settle(0.1)
            self.screenshot("authoring-audio-update-preview", dialog)
            save.click()
        except Exception:
            self.report["audio_update_error"] = traceback.format_exc()
            dialog.reject()

    def check_audio_update(self, addon, authoring, profile, model, lookup, audio_bytes):
        existing = self.mw.col.media.write_data(f"existing-tone.{self.audio_format}", audio_bytes)
        note = self.mw.col.new_note(model)
        for name, value in {"Front": "猫", "Back": "My definition", "Audio": f"[sound:{existing}]", "Source": "My source"}.items():
            note[name] = value
        self.mw.col.add_note(note, profile.deck_id)
        before = tuple(note.fields)
        self.update_audio_before = note["Audio"]
        card = note.cards()[0]
        card.queue, card.type, card.ivl, card.reps = 2, 2, 3, 1
        card.due = self.mw.col.sched.today + 3
        self.mw.col.update_card(card)
        self.mw.col.db.execute("insert into revlog values (?, ?, -1, 3, 3, 1, 2500, 1000, 1)", card.id, card.id)
        cards = self.mw.col.db.all("select * from cards where nid = ? order by id", note.id)
        reviews = self.mw.col.db.all("select * from revlog where cid = ? order by id", card.id)
        done, closed, errors = [], [], []
        self.approving, self.material_action = False, self.approve_audio_update
        self.recording_attempt, self.retry_audio_download = 1, False
        authoring.preview_update(self.mw, note.id, lookup, profile, config=addon.config(), save_config=addon.save_config,
                                 on_done=done.append, on_closed=lambda: closed.append(True), on_error=lambda error: errors.append(str(error)))
        self.wait(lambda: bool(closed), "native audio update finished")
        if self.report.get("audio_update_error"):
            raise AssertionError(self.report["audio_update_error"])
        self.check("native_update_gate_closed_once", closed == [True] and not errors and len(done) == 1)
        saved = self.mw.col.get_note(note.id)
        self.report["audio_update_saved_fields"] = dict(saved.items())
        self.check("native_update_saved_audio_and_source", self.update_audio_before in saved["Audio"] and saved["Audio"].count("[sound:") == 2 and
                   "My source" in saved["Source"] and "Permitted for Anki deck synchronization and export" in saved["Source"] and saved["Back"] == "My definition")
        self.check("native_update_preserves_card_history", cards == self.mw.col.db.all("select * from cards where nid = ? order by id", note.id) and
                   reviews == self.mw.col.db.all("select * from revlog where cid = ? order by id", card.id))
        self.mw.onUndo()
        self.wait(lambda: tuple(self.mw.col.get_note(note.id).fields) == before, "Undo restored existing audio and source")
        self.check("native_update_undo_restores_audio_source", not authoring.provenance_for(self.mw.col, note.id))

    def approve_batch_material(self, dialog):
        from aqt.qt import QPushButton
        try:
            self.check("batch_unpinned_lookup_starts_without_examples", not dialog.lookup.examples)
            more = next(button for button in dialog.findChildren(QPushButton) if button.text().startswith("Find more examples"))
            more.click()
            dialog.search_examples_button.click()
            self.wait(lambda: bool(dialog.lookup.examples), "batch corpus choices")
            self.check("batch_corpus_search_requires_explicit_choice", not dialog.sentence.toPlainText())
            dialog.example.setCurrentIndex(dialog.example.findData(0))
            more.setChecked(False)
            self.settle(0.1)
            self.screenshot("authoring-batch-material", dialog)
            dialog.accept()
        except Exception:
            self.report["batch_approval_error"] = traceback.format_exc()
            dialog.reject()

    def check_batch_examples(self, addon, authoring, profile, model):
        from aqt.qt import QCoreApplication, QDialog, QDialogButtonBox, QEvent
        batch_module = importlib.import_module(desktop_check.ADDON + ".authoring_batch")
        lookup_module = importlib.import_module(desktop_check.ADDON + ".authoring_lookup")
        ui = importlib.import_module(desktop_check.ADDON + ".authoring_ui")
        notes = []
        for values in ({"Front": "猫"}, {"Front": "猫", "Back": "My definition", "Sentence": "My sentence", "Source": "My source"},
                       {"Front": "猫", "Reading": "ちがう"}):
            note = self.mw.col.new_note(model)
            for name, value in values.items():
                note[name] = value
            self.mw.col.add_note(note, profile.deck_id)
            notes.append(note)
        unmapped = self.mw.col.new_note(self.mw.col.models.by_name("Basic"))
        unmapped["Front"] = "鳥"
        self.mw.col.add_note(unmapped, profile.deck_id)
        notes.append(unmapped)
        before = {note.id: tuple(note.fields) for note in notes}
        batch = batch_module.Batch()
        batch.rows = batch.capture(self.mw.col, list(before), [profile])
        batch.prepare(lookup_module.resolver_from_config(addon.config()))
        self.check("batch_mixed_selection_is_retained", [row.status for row in batch.rows] == ["ready", "ready", "unmatched", "unmapped"])
        preview = ui.BatchPreviewDialog(self.mw, batch.rows, config=addon.config())
        preview.show()
        self.approving, self.material_action = False, self.approve_batch_material
        preview.choose_material()
        if self.report.get("batch_approval_error"):
            raise AssertionError(self.report["batch_approval_error"])
        chosen, untouched = preview.previews
        self.check("batch_chosen_example_retains_attribution", chosen.field_values()["Sentence"] == "猫が窓の外を見ています。" and
                   "tatoeba:fixture-1" in chosen.field_values()["Source"])
        self.check("batch_other_row_keeps_its_sentence", untouched.field_values()["Sentence"] == "My sentence" and
                   untouched.field_values()["Source"] == "My source" and untouched.field_values()["Back"] == "My definition")
        self.check("batch_material_approval_does_not_save", all(tuple(self.mw.col.get_note(nid).fields) == fields for nid, fields in before.items()))
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.settle(0.1)
        self.screenshot("authoring-batch-preview", preview)
        preview.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.check("batch_requires_native_field_approval", preview.result() == QDialog.DialogCode.Accepted)
        result = authoring.apply_previews(self.mw.col, preview.previews)
        self.check("batch_applies_only_ready_notes", set(result.applied) == {notes[0].id, notes[1].id} and
                   all(tuple(self.mw.col.get_note(note.id).fields) == before[note.id] for note in notes[2:]))
        self.check("batch_saved_selected_example", self.mw.col.get_note(notes[0].id)["Sentence"] == "猫が窓の外を見ています。")
        self.mw.onUndo()
        self.wait(lambda: all(tuple(self.mw.col.get_note(nid).fields) == fields for nid, fields in before.items()), "Undo restored mixed batch fields")
        self.check("batch_undo_restores_all_selected_fields", True)
        preview.close()

    def run(self):
        from anki.buildinfo import version
        self.wait(lambda: self.mw.col is not None and self.mw.state != "profileManager", "isolated profile loaded")
        self.settle()
        addon = importlib.import_module(desktop_check.ADDON)
        authoring = importlib.import_module(desktop_check.ADDON + ".authoring")
        media = importlib.import_module(desktop_check.ADDON + ".authoring_media")
        self.audio_format = self.server.fixture_audio_format
        transport = RecordingDownload(media, self.root, self.audio_format, self.server.live_test_recording)
        self.media_requests = transport.requests
        self.report["recording_downloads"] = transport.completed
        media.download_selected_media = transport
        prepared = next(note for note in self.manifest["notes"] if note["case"] == "prepared")
        model = self.mw.col.models.get(prepared["notetype"])
        for name in ("Sentence", "Translation", "Source", "Audio"):
            self.mw.col.models.add_field(model, self.mw.col.models.new_field(name))
        self.mw.col.models.update_dict(model)
        profile = authoring.FieldProfile("Native authoring smoke", model["id"], {
            "expression": "Front", "reading": "Reading", "meanings": "Back", "sentence": "Sentence",
            "translation": "Translation", "source": "Source", "audio": "Audio", "entry": "Entry",
        }, deck_id=prepared["deck"], tags=("native_authoring_smoke",))
        lookup = authoring.Lookup(lemma="猫", surface="猫", reading="ねこ", entry="jmdict:word:cat-1",
                                  meanings=("cat",), sentence="現在の文には猫がいます。", source="Current reading",
                                  audio_urls=("file:///untrusted-card.mp3",))
        before = set(self.mw.col.find_notes(""))
        closed, failures = [], []
        transport_scope = "approved public ねこ MP3 only, production HTTPS downloader with 1 MiB limit" if transport.live else "synthetic audio transport"
        self.report.update(scope=f"Real AnkiQt Add, update, Undo and mixed batch; {transport_scope}; dictionary and corpus metadata are loopback fixtures; isolated profile, no sync account",
                           anki=version, qt_platform=self.app.platformName(), audio_format=self.audio_format,
                           live_test_recording=transport.live)
        add = authoring.open_add_note(self.mw, lookup, profile, config=addon.config(), save_config=addon.save_config,
                                     on_closed=lambda: closed.append(True), on_error=lambda error: failures.append(str(error)))
        if self.report.get("approval_error"):
            raise AssertionError(self.report["approval_error"])
        self.wait(lambda: bool(closed), "fresh Add editor handoff")
        self.check("add_handoff_closed_once", closed == [True] and not failures)
        audio_bytes = transport.bytes
        self.check("approved_recording_download_completed", audio_bytes is not None and len(transport.completed) == 1)
        self.check("add_is_draft_until_native_save", set(self.mw.col.find_notes("")) == before)
        self.check("fresh_editor_contains_approved_example", add.editor.note["Sentence"] == "猫が窓の外を見ています。")
        self.wait(lambda: self.editor_js(add.editor, """(() => {
            const roots = [document];
            for (const root of roots) for (const element of root.querySelectorAll('*')) {
                if (element.shadowRoot) roots.push(element.shadowRoot);
                if (element.isContentEditable && element.textContent.includes('猫が窓の外を見ています。')) return true;
            }
            return false;
        })()"""), "approved sentence painted in native Editor")
        self.check("native_editor_fields_rendered", True)
        self.settle(0.1)
        self.screenshot("authoring-native-add", add)
        add.addButton.click()
        self.wait(lambda: self.mw.col.note_count() == len(before) + 1, "native Add saved note")
        ids = set(self.mw.col.find_notes("")) - before
        self.check("native_add_saved_one_note", len(ids) == 1)
        nid = next(iter(ids))
        self.wait(lambda: bool(authoring.provenance_for(self.mw.col, nid)), "native creation provenance")
        saved = self.mw.col.get_note(nid)
        self.report["saved_fields"] = dict(saved.items())
        self.check("saved_example_and_attribution", saved["Sentence"] == "猫が窓の外を見ています。" and "tatoeba:fixture-1" in saved["Source"])
        self.check("saved_opaque_entry_identity", saved["Entry"] == "jmdict:word:cat-1")
        audio_name = saved["Audio"].removeprefix("[sound:").removesuffix("]")
        audio_path = Path(self.mw.col.media.dir()) / audio_name
        self.check("native_save_keeps_selected_audio_and_permission", audio_path.read_bytes() == audio_bytes and
                   "Shirabe" in saved["Source"] and media.ANKI_PERMISSION in saved["Source"])
        self.report["saved_audio"] = {"path": str(audio_path), "bytes": audio_path.stat().st_size,
                                      "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest()}
        self.check("only_selected_verified_recording_was_downloaded", self.media_requests == [transport.urls[-1]] * (1 if transport.live else 2))
        self.check("saved_recording_extension_matches_selected_format", audio_name.endswith("." + self.audio_format))
        self.report["undo_label"] = self.mw.col.undo_status().undo
        self.mw.onUndo()
        self.wait(lambda: self.mw.col.note_count() == len(before), "native Undo removed authored note")
        self.check("native_undo_restores_note_count", set(self.mw.col.find_notes("")) == before)
        self.report["audio_after_undo"] = "Downloaded file remains as unused Anki media" if audio_path.exists() else "Removed"
        self.check("native_undo_retains_downloaded_bytes", audio_path.read_bytes() == audio_bytes)
        self.report["audio_sha256_after_undo"] = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        self.settle()
        add.close()
        self.settle()
        self.check_audio_update(addon, authoring, profile, model, lookup, audio_bytes)
        self.check_batch_examples(addon, authoring, profile, model)
        self.check("no_editor_console_errors", not self.console_errors)
        self.report.update(requests=self.requests[:], media_requests=self.media_requests, console_errors=self.console_errors, passed=True)


def main():
    invocation = sys.argv[:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--startup-only", action="store_true", help="Capture startup imports without opening authoring dialogs")
    parser.add_argument("--audio-format", choices=("m4a", "mp3"), default="m4a", help="Exercise explicit M4A override or verified MP3 download")
    parser.add_argument("--live-test-recording", action="store_true", help="Download only the approved public ねこ MP3 with the production transport; metadata stays synthetic, implies --audio-format mp3")
    args = desktop_check.gui_arguments(parser)
    require_anki()
    root = output_directory(args.output)
    server, requests = fixture_api(audio_format=args.audio_format, live_test_recording=args.live_test_recording)
    from anki.collection import Collection
    add_note = Collection.add_note
    def add_counted_fixture_note(col, note, deck_id):
        add_note(col, note, deck_id)
        return SimpleNamespace(count=len(note.cards()))
    with patch.object(Collection, "add_note", add_counted_fixture_note):
        base, manifest = desktop_check.prepare(root, f"http://127.0.0.1:{server.server_port}")
    import aqt
    from aqt.qt import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    aqt.AnkiApp.KEY = "shirabe-authoring-smoke-" + root.name
    sys.argv = ["anki", "-b", str(base), "-p", desktop_check.PROFILE, "-l", "en"]
    app = aqt._run(sys.argv, exec=False)
    if app is None:
        raise RuntimeError("Isolated Anki did not start")
    driver = Driver(app, root, manifest, requests, server)
    driver.report["launch"] = {"harness_argv": invocation, "anki_argv": sys.argv[:], "qt_arguments": app.arguments()}
    try:
        if args.startup_only:
            driver.wait(lambda: driver.mw.col is not None and driver.mw.state == "deckBrowser", "isolated profile loaded")
            driver.settle(2)
            driver.report.update(scope="Startup import diagnostics only; no authoring or audio actions", passed=True)
        else:
            driver.run()
    except Exception:
        driver.report.update(passed=False, error=traceback.format_exc(), requests=requests[:], console_errors=driver.console_errors)
        traceback.print_exc()
    finally:
        save_json(root / "authoring-desktop-report.json", driver.report)
        print(json.dumps({"report": str(root / "authoring-desktop-report.json"), "passed": driver.report.get("passed", False)}), flush=True)
        driver.timer.stop()
        aqt.mw.close()
        driver.settle(1)
        server.shutdown()
        server.server_close()
    return 0 if driver.report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
