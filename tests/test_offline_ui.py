import json
import os
from pathlib import Path
import subprocess
import unittest

import support
from check_against_anki import anki_python
from offline_fixture import prepared, parse, identify
from shirabe_addon import offline, offline_ui


class BatchTests(unittest.TestCase):
    def test_storage_labels_show_small_sizes_and_file_counts(self):
        for size, expected in ((0, "0 B"), (512, "512 B"), (1024, "1 KiB"), (1536, "1.5 KiB"), (1024 * 1024, "1 MiB")):
            self.assertEqual(offline_ui.format_bytes(size), expected)
        self.assertEqual(offline_ui.storage_summary(offline.SnapshotStorage(1024, 1)), "1 KiB in 1 file")
        self.assertEqual(offline_ui.storage_summary(offline.SnapshotStorage(0, 0)), "0 B in 0 files")

    def test_approved_dictionary_selection_preserves_stack_order_without_adding_unapproved_dictionaries(self):
        preferences = {"dictionaries": ["private:ja", "second:es", "first:en", "second:en"]}
        dictionaries = [{"slug": "first"}, {"slug": "second"}, {"slug": "third"}, {"slug": "private"}]
        self.assertEqual(offline_ui.approved_dictionary_slugs(preferences, {"first", "second", "third"}, dictionaries), ["second", "first", "third"])
        self.assertEqual(offline_ui.approved_dictionary_slugs(preferences, {"private"}, dictionaries), ["private"])
        self.assertEqual(offline_ui.approved_dictionary_slugs(preferences, set(), dictionaries), [])

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_consent_requires_upload_permission_and_defaults_uploaded_dictionaries_off(self):
        probe = '''
import sys
sys.path.insert(0, sys.argv[1])
import support
from shirabe_addon import offline, offline_ui
from aqt.qt import QApplication, QCheckBox, QDialogButtonBox, QLabel, QScrollArea, QSpinBox, QTimer, QToolButton
app = QApplication(["shirabe-consent-probe"])
sys.excepthook = lambda kind, error, trace: (sys.__excepthook__(kind, error, trace), app.activeModalWidget().reject())
def approve():
    dialog = app.activeModalWidget()
    labels = dialog.findChildren(QLabel)
    copy = "\\n".join(label.text() for label in labels if label.isVisible())
    assert "both sides" in copy and "cloze answers" in copy and "export" in copy, copy
    assert "1 KiB in 1 file" in copy and "0.00 MiB" not in copy, copy
    assert "ShirabeLookup" in copy and "128 KiB" not in copy, copy
    assert dialog.findChild(QScrollArea, "shirabe-dictionaries").height() < 240
    details = dialog.findChild(QToolButton)
    assert not details.isChecked()
    details.click()
    assert any(label.isVisible() and "128 KiB" in label.text() for label in labels)
    boxes = dialog.findChildren(QCheckBox)
    assert len(boxes) == 5
    assert [box.isChecked() for box in boxes] == [False, False, False, True, False]
    assert boxes[1].text() == "Include Shirabe pronunciation"
    assert boxes[2].text() == "Include Tatoeba examples" and not boxes[2].isEnabled()
    assert "Only selected-card excerpts, not full dictionaries." in copy
    assert "entries unknown" in boxes[4].text()
    start = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
    assert not start.isEnabled()
    limit = dialog.findChild(QSpinBox)
    assert limit.value() == 25 and limit.maximum() == 256
    boxes[0].setChecked(True)
    assert start.isEnabled()
    start.click()
QTimer.singleShot(0, approve)
chosen = offline_ui._consent(None, 2, [
    {"slug":"jmdict", "name":"JMdict", "visibility":"official", "entryCount":200000, "source":{"license":"CC BY-SA 4.0"}},
    {"slug":"uploaded", "name":"My dictionary", "visibility":"personal"},
], offline.SnapshotStorage(1024, 1), {})
assert chosen == ({"jmdict"}, 25, False, False), chosen
print("native consent passed")
'''
        result = subprocess.run([str(anki_python()), "-c", probe, str(Path(__file__).parent)], capture_output=True, text=True, timeout=15,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native consent passed", result.stdout)

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_consent_large_font_layout_buttons_and_scrolling(self):
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("offline_consent_probe.py"))],
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["cases"]), 6)
        self.assertEqual(report["failures"], [])

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_progress_window_keeps_large_text_and_stop_action_visible(self):
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("offline_progress_ui_probe.py"))],
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("offline progress UI passed", result.stdout)

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_real_webengine_capture_preserves_visible_runs_without_deck_requests(self):
        sdk = Path(__file__).resolve().parents[1]
        build = '''import {build} from "esbuild";
const result=await build({stdin:{contents:`import {collectReadableRuns} from "@shirabe-org/card/word-card/wrap";globalThis.ShirabeCard={collectReadableRuns};`,resolveDir:process.cwd()},bundle:true,format:"iife",write:false});
process.stdout.write(result.outputFiles[0].text);'''
        bundle = subprocess.run(["node", "--input-type=module", "-e", build],
                                cwd=sdk, capture_output=True, text=True)
        self.assertEqual(bundle.returncode, 0, bundle.stderr)
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("offline_webengine_probe.py"))],
                                input=bundle.stdout, capture_output=True, text=True, timeout=25,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"qtWebEngine": true', result.stdout)

    def test_batch_fingerprints_preferences_allowlist_and_bounds_network_work(self):
        preferences = {"stackFingerprint": "stack-v1", "dictionaries": ["jmdict:en"], "languages": ["en"], "uiLocale": "en"}
        self.assertNotEqual(offline_ui.content_version(preferences, {"jmdict"}), offline_ui.content_version(preferences, {"jmdict", "uploaded"}))
        class Backend:
            parse = staticmethod(parse)
            identify = staticmethod(identify)

            def check_access(self):
                pass
        notes = [offline_ui.CapturedNote(i, "revision", (), None, None) for i in range(3)]
        progress = []
        result = offline_ui.prepare_batch(notes, {0: ["猫"], 1: ["犬"], 2: None}, Backend(), preferences=preferences,
                                           approved={"jmdict"}, progress=lambda *args: progress.append(args))
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.skipped, 1)
        self.assertFalse(result.stopped)
        self.assertEqual(progress, [(1, 3), (2, 3)])
        self.assertEqual(result.rows[0][1].snapshot["contentVersion"], offline_ui.content_version(preferences, {"jmdict"}))
        cancelled = offline_ui.prepare_batch(notes, {0: ["猫"]}, Backend(), preferences=preferences, approved={"jmdict"}, cancelled=lambda: True)
        self.assertTrue(cancelled.stopped)
        self.assertEqual(cancelled.rows, [])
        class Oversized(Backend):
            def identify(self, _):
                raise offline.SnapshotError("Too big", code="entry_limit")
        oversized = offline_ui.prepare_batch(notes[:1], {0: ["猫"]}, Oversized(), preferences=preferences, approved={"jmdict"})
        self.assertEqual(oversized.issue_notes, {"entry_limit": 1})
        self.assertEqual(oversized.skipped, 1)
        static = offline_ui.prepare_batch(notes[:1], {0: ["猫"]}, Backend(), preferences=preferences, approved={"jmdict"}, static_only={0})
        self.assertFalse(static.rows[0][1].complete)
        self.assertEqual(static.issue_notes, {"static_text_only": 1})
        for value in (0, 257, "100", True):
            with self.assertRaises(offline.SnapshotError):
                offline_ui.storage_budget({"offlineStorageMiB": value})

    def test_capture_script_has_no_embedded_credentials_or_network_api(self):
        script = offline_ui.capture_script('<ruby>猫<rt>ねこ</rt></ruby>', 1)
        self.assertIn("collectReadableRuns", script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("cardToken", script)
        with self.assertRaises(offline.SnapshotError):
            offline_ui.capture_script("猫" * offline_ui.MAX_RENDER_BYTES, 1)

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_field_template_refs_conflicts_partial_refresh_and_undo(self):
        probe = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import support
from offline_fixture import prepared
from shirabe_addon import offline, offline_ui
from anki.collection import Collection
with tempfile.TemporaryDirectory(prefix="shirabe-offline-ui-") as directory:
    col = Collection(str(Path(directory) / "collection.anki2"))
    try:
        model = col.models.by_name("Basic (and reversed card)")
        before_templates = [(t["qfmt"], t["afmt"]) for t in model["tmpls"]]
        note_ids = []
        for word in ("猫", "犬", "鳥"):
            note = col.new_note(model)
            note["Front"] = '<ruby>' + word + '<rt>ねこ</rt></ruby>'
            note["Back"] = '<b>my answer</b>[sound:mine.mp3]'
            note.tags = ["mine"]
            col.add_note(note, 1)
            note_ids.append(note.id)
        captures = offline_ui.capture_notes(col, note_ids[:2])
        assert len(captures[0].sides) == 4
        assert any("<ruby>猫" in html for _, html in captures[0].sides)
        original = [(n, list(col.get_note(n).fields), list(col.get_note(n).tags)) for n in note_ids]
        cards = col.db.all("select * from cards order by id")
        changed = col.get_note(note_ids[1])
        changed["Back"] = "edited during preparation"
        col.update_note(changed)
        batch = offline_ui.PreparedBatch(rows=[(capture, prepared()) for capture in captures])
        result = offline_ui.apply_prepared(col, batch, {})
        assert (result.saved, result.conflicts) == (1, 1), result
        updated = col.get_note(note_ids[0])
        filename = offline.snapshot_filename(updated["ShirabeLookup"])
        assert updated["ShirabeLookup"] == offline.snapshot_field(filename)
        assert offline.MEDIA_NAME.fullmatch(filename)
        assert offline.read_snapshot(col.media, filename)
        assert list(updated.fields)[:2] == original[0][1] and updated.tags == ["mine"]
        assert col.get_note(note_ids[1])["Back"] == "edited during preparation"
        assert col.get_note(note_ids[1])["ShirabeLookup"] == ""
        assert col.get_note(note_ids[2])["ShirabeLookup"] == ""
        updated_model = updated.note_type()
        assert all("{{text:ShirabeLookup}}" in t["afmt"] for t in updated_model["tmpls"])
        assert all("shirabe:question" in t["qfmt"] for t in updated_model["tmpls"])
        assert col.db.all("select * from cards order by id") == cards
        assert col.undo_status().undo == "Prepare Shirabe offline lookup", col.undo_status()
        col.undo()
        assert "ShirabeLookup" not in col.get_note(note_ids[0])
        assert [(t["qfmt"], t["afmt"]) for t in col.models.get(model["id"])["tmpls"]] == before_templates
        assert col.get_note(note_ids[1])["Back"] == "edited during preparation"
        assert col.db.all("select * from cards order by id") == cards
        col.redo()
        assert offline.snapshot_filename(col.get_note(note_ids[0])["ShirabeLookup"]) == filename
        legacy = col.get_note(note_ids[0])
        legacy["ShirabeLookup"] = filename
        col.update_note(legacy)
        current = offline_ui.capture_notes(col, [note_ids[0]])[0]
        assert current.previous_name == filename and current.previous
        partial = prepared(budget=offline.Budget(max_requests=2))
        refreshed = offline_ui.apply_prepared(col, offline_ui.PreparedBatch(rows=[(current, partial)]), {})
        assert refreshed.kept_previous == 1
        assert offline.snapshot_filename(col.get_note(note_ids[0])["ShirabeLookup"]) == filename
        assert col.get_note(note_ids[0])["ShirabeLookup"] == offline.snapshot_field(filename)
        assert filename not in offline_ui.unreferenced_snapshots(col)
        basic = col.models.by_name("Basic")
        another = col.new_note(basic)
        another["Front"] = "別"
        col.add_note(another, 1)
        rollback_captures = offline_ui.capture_notes(col, [note_ids[0], another.id])
        original_update = col.models.update_dict
        called = 0
        def fail_second(model, **kwargs):
            global called
            called += 1
            if called == 2:
                model["tmpls"][0]["afmt"] = "{{MissingField}}"
            return original_update(model, **kwargs)
        col.models.update_dict = fail_second
        try:
            offline_ui.apply_prepared(col, offline_ui.PreparedBatch(rows=[(c, prepared()) for c in rollback_captures]), {})
            raise AssertionError("invalid template was accepted")
        except Exception as error:
            assert not isinstance(error, AssertionError), error
        finally:
            col.models.update_dict = original_update
        assert "ShirabeLookup" not in col.get_note(another.id)
        assert "{{MissingField}}" not in col.models.get(basic["id"])["tmpls"][0]["afmt"]
        for capture in rollback_captures:
            restored = col.get_note(capture.note_id)
            assert offline_ui.note_revision(restored, restored.note_type()) == capture.revision
        from shirabe_addon import template_ui
        target = template_ui.TemplateTarget.capture(col, col.models.get(basic["id"]))
        original_sides = [(t["qfmt"], t["afmt"]) for t in target.model["tmpls"]]
        template_ui.apply_install(col, target, template_ui.TemplateOptions(answer_only=False, online_parsing=True, excluded_fields=("Back",)), {})
        current = offline_ui.capture_notes(col, [another.id])[0]
        offline_ui.apply_prepared(col, offline_ui.PreparedBatch(rows=[(current, prepared())]), {})
        settings = col.get_config(template_ui.settings_key(basic["id"]))
        assert settings["answerOnly"] is False and settings["onlineParsing"] is True and settings["excludedFields"] == ["Back"]
        installed = col.models.get(basic["id"])
        assert "{{text:ShirabeLookup}}" in installed["tmpls"][0]["qfmt"]
        template_ui.apply_remove(col, (template_ui.TemplateTarget.capture(col, installed),))
        assert [(t["qfmt"], t["afmt"]) for t in col.models.get(basic["id"])["tmpls"]] == original_sides
        print(json.dumps({"nativeUndo": True, "conflictSkipped": True, "scheduleUnchanged": True, "bytes": result.bytes_used}))
    finally:
        col.close()
'''
        result = subprocess.run([str(anki_python()), "-c", probe, str(Path(__file__).parent)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"nativeUndo": true', result.stdout)


if __name__ == "__main__":
    unittest.main()
