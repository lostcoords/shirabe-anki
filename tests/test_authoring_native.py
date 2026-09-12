"""Run with Anki's Python to exercise its SQLite backend and actual undo stack."""

import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from shirabe_addon import authoring, collection

try:
    from anki.collection import Collection
except ImportError:
    Collection = None


@unittest.skipUnless(Collection, "Run with Anki's bundled Python for native collection tests")
class NativeAuthoring(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shirabe-authoring-")
        self.col = Collection(str(Path(self.temp.name) / "collection.anki2"))
        model = self.col.models.by_name("Basic (and reversed card)")
        for name in ("Reading", "Entry", "Sentence", "Other"):
            self.col.models.add_field(model, self.col.models.new_field(name))
        self.col.models.update_dict(model)
        self.model = model
        self.profile = authoring.FieldProfile("Japanese", model["id"], {
            "expression": "Front", "meanings": "Back", "reading": "Reading", "entry": "Entry", "sentence": "Sentence",
        }, tags=("shirabe",))
        self.lookup = authoring.Lookup(surface="猫", lemma="猫", reading="ねこ", entry="jmdict:1467640", meanings=("cat",))

    def tearDown(self):
        self.col.close()
        self.temp.cleanup()

    def add(self, **fields):
        note = self.col.new_note(self.model)
        note["Front"] = "猫"
        note["Other"] = "<b>mine</b>[sound:mine.mp3]"
        note.tags = ["mine"]
        for name, value in fields.items():
            note[name] = value
        self.col.add_note(note, 1)
        return self.col.get_note(note.id)

    def test_native_update_and_undo_restore_fields_tags_provenance_and_sibling_cards(self):
        note = self.add(Back="old")
        cards_before = self.col.db.all("select * from cards where nid = ? order by id", note.id)
        before = authoring.NoteSnapshot.capture(note)
        result = authoring.apply_previews(self.col, [authoring.build_preview(note, self.profile, self.lookup, policies={"meanings": "replace"})])
        self.assertEqual(result.applied, (note.id,))
        self.assertEqual(self.col.get_note(note.id)["Back"], "cat")
        self.assertEqual(self.col.get_note(note.id)["Other"], dict(before.fields)["Other"])
        self.assertEqual(self.col.get_note(note.id).tags, ["mine", "shirabe"])
        self.assertEqual(self.col.undo_status().undo, "Shirabe: enrich notes")
        self.assertIn("Back", authoring.provenance_for(self.col, note.id))
        self.assertEqual(self.col.db.all("select * from cards where nid = ? order by id", note.id), cards_before)
        self.col.undo()
        restored = self.col.get_note(note.id)
        self.assertEqual(tuple(zip(restored.keys(), restored.fields)), before.fields)
        self.assertEqual(tuple(restored.tags), before.tags)
        self.assertEqual(authoring.provenance_for(self.col, note.id), {})
        self.assertEqual(self.col.db.all("select * from cards where nid = ? order by id", note.id), cards_before)
        self.col.redo()
        self.assertEqual(self.col.get_note(note.id)["Back"], "cat")
        self.assertIn("Back", authoring.provenance_for(self.col, note.id))

    def test_conflicting_note_is_skipped_and_retry_does_not_repeat_an_append(self):
        first, second = self.add(), self.add(Back="my definition")
        previews = [authoring.build_preview(n, self.profile, self.lookup, policies={"meanings": "append"}) for n in (first, second)]
        first["Other"] = "edited during lookup"
        self.col.update_note(first)
        result = authoring.apply_previews(self.col, previews)
        self.assertEqual(result.conflicts, (first.id,))
        self.assertEqual(result.applied, (second.id,))
        self.assertEqual(self.col.get_note(second.id)["Back"], "my definition<br>cat")
        retry = authoring.apply_previews(self.col, previews)
        self.assertEqual(retry.applied, ())
        self.assertEqual(self.col.get_note(second.id)["Back"], "my definition<br>cat")

    def test_cancel_does_not_write_or_add_an_undo_entry(self):
        note = self.add()
        undo = self.col.undo_status()
        result = authoring.apply_previews(self.col, [authoring.build_preview(note, self.profile, self.lookup)], cancelled=lambda: True)
        self.assertEqual(result.applied, ())
        self.assertEqual(self.col.get_note(note.id)["Back"], "")
        self.assertEqual(self.col.undo_status(), undo)

    def test_duplicates_do_not_treat_example_mentions_or_different_entries_as_matches(self):
        exact = self.add(Entry=self.lookup.entry, Reading="ネコ")
        fallback = self.add(Reading="ねこ")
        self.add(Entry="jmdict:other", Reading="ねこ")
        self.add(Front="犬", Sentence="猫が好き。")
        found = collection.find_existing_notes(self.col, self.lookup, [self.profile])
        self.assertEqual([(n.note_id, n.match) for n in found], [(exact.id, "entry-reading"), (fallback.id, "expression-reading")])

    def test_expression_search_is_literal_even_for_search_operators_and_ruby(self):
        note = self.add(Front='<ruby>ア*イ<rt>あい</rt></ruby>')
        self.add(Front="アカイ")
        found = collection.find_existing_notes(self.col, authoring.Lookup(lemma="ア*イ"), [self.profile])
        self.assertEqual([n.note_id for n in found], [note.id])

    def test_duplicates_preserve_opaque_entry_identity_instead_of_normalizing_it(self):
        entry = "jmdict:語:Ａ/猫?x=1&y=2 "
        exact = self.add(Entry=authoring.escaped_text(entry))
        self.add(Entry=authoring.escaped_text(entry.replace("Ａ", "A")))
        other_type = self.col.new_note(self.col.models.by_name("Basic"))
        other_type["Front"] = authoring.escaped_text(entry)
        self.col.add_note(other_type, 1)
        lookup = authoring.Lookup.from_selection({"lemma": "old-spelling", "entry": entry})
        found = collection.find_existing_notes(self.col, lookup, [self.profile])
        self.assertEqual([(note.note_id, note.entry) for note in found], [(exact.id, entry)])

    def test_mapped_states_ignore_sentence_mentions_even_with_heuristic_enabled(self):
        self.add(Front="犬", Sentence="猫です。")
        self.assertEqual(collection.states_for(self.col, ["猫"], [self.profile], allow_heuristic=True), {"猫": "none"})
        self.add()
        self.assertEqual(collection.states_for(self.col, ["猫"], [self.profile]), {"猫": "new"})

    def test_stale_unrelated_profiles_do_not_break_valid_matches_or_reviewer_states(self):
        note = self.add()
        deleted = authoring.FieldProfile("Deleted", 999999, {"expression": "Front"})
        renamed = authoring.FieldProfile("Renamed", self.model["id"], {"expression": "Missing"})
        profiles = [deleted, renamed, self.profile]
        found = collection.find_existing_notes(self.col, self.lookup, profiles)
        self.assertEqual([n.note_id for n in found], [note.id])
        self.assertEqual(collection.states_for(self.col, ["猫"], profiles), {"猫": "new"})

    def test_bulk_provenance_does_not_overflow_ankis_undo_history(self):
        notes = [self.add(Front=f"猫{i}") for i in range(40)]
        previews = [authoring.build_preview(n, self.profile, self.lookup) for n in notes]
        self.assertEqual(len(authoring.apply_previews(self.col, previews).applied), 40)
        self.col.undo()
        self.assertTrue(all(self.col.get_note(n.id)["Back"] == "" for n in notes))
        self.assertTrue(all(authoring.provenance_for(self.col, n.id) == {} for n in notes))

    def test_native_preset_is_separate_and_undoable(self):
        before = [m["id"] for m in self.col.models.all()]
        result = authoring.create_preset(self.col)
        result.profile.validate(self.col.models.get(result.profile.note_type_id))
        self.assertEqual(result.profile.fields["sentence"], "Sentence")
        with self.assertRaises(authoring.AuthoringError):
            authoring.create_preset(self.col)
        self.col.undo()
        self.assertEqual([m["id"] for m in self.col.models.all()], before)

    def test_provenance_failure_rolls_back_only_this_update(self):
        note = self.add()
        preview = authoring.build_preview(note, self.profile, self.lookup)
        with patch.object(self.col, "set_config", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                authoring.apply_previews(self.col, [preview])
        self.assertEqual(self.col.get_note(note.id)["Back"], "")
        self.assertEqual(self.col.get_note(note.id).tags, ["mine"])
        self.assertEqual(self.col.undo_status().undo, "Add Note")

    def test_deleted_note_is_a_conflict_and_does_not_block_other_rows(self):
        deleted, kept = self.add(), self.add()
        previews = [authoring.build_preview(n, self.profile, self.lookup) for n in (deleted, kept)]
        self.col.remove_notes([deleted.id])
        result = authoring.apply_previews(self.col, previews)
        self.assertEqual(result.conflicts, (deleted.id,))
        self.assertEqual(result.applied, (kept.id,))

    def test_creation_tracks_only_fields_the_learner_did_not_change_before_save(self):
        draft = self.col.new_note(self.model)
        preview = authoring.build_preview(draft, self.profile, self.lookup)
        for name, value in preview.field_values().items():
            draft[name] = value
        draft["Back"] = "my own definition"
        self.col.add_note(draft, 1)
        step = self.col.undo_status().last_step
        authoring.record_created_note(self.col, draft.id, preview, undo_step=step)
        provenance = authoring.provenance_for(self.col, draft.id)
        self.assertIn("Front", provenance)
        self.assertNotIn("Back", provenance)
        self.assertEqual(self.col.undo_status().undo, "Add Note")
        self.col.undo()
        self.assertEqual(self.col.find_notes(""), [])
        self.assertEqual(authoring.provenance_for(self.col, draft.id), {})

    def test_creation_does_not_merge_into_another_actions_undo_entry(self):
        note = self.add()
        step = self.col.undo_status().last_step
        preview = authoring.build_preview(note, self.profile, self.lookup)
        self.add()
        authoring.record_created_note(self.col, note.id, preview, undo_step=step)
        self.assertEqual(authoring.provenance_for(self.col, note.id), {})

    def test_native_media_deduplicates_and_can_enter_the_audio_preview_with_attribution(self):
        from shirabe_addon.authoring_media import DownloadedMedia, store_selected_media
        downloaded = DownloadedMedia(b"ID3a recording", "mp3", "Speaker, corpus record", "CC BY 4.0")
        first, second = store_selected_media(self.col, downloaded), store_selected_media(self.col, downloaded)
        self.assertEqual(first.filename, second.filename)
        self.assertTrue(self.col.media.have(first.filename))
        profile = authoring.FieldProfile("With audio", self.model["id"], self.profile.fields | {"audio": "Other"})
        note = self.add(Other="")
        preview = authoring.build_preview(note, profile, self.lookup, media=first)
        self.assertEqual(preview.field_values()["Other"], first.field_html)
        self.assertIn("CC BY 4.0", preview.source)
        authoring.apply_previews(self.col, [preview])
        self.col.undo()
        self.assertEqual(self.col.get_note(note.id)["Other"], "")
        self.assertTrue(self.col.media.have(first.filename))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg generates the synthetic AAC fixture")
    def test_native_m4a_is_deduplicated_and_rendered_as_sound(self):
        from shirabe_addon.authoring_media import DownloadedMedia, store_selected_media
        path = Path(self.temp.name) / "tone.m4a"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=0.15", "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000",
                        str(path)], check=True, capture_output=True)
        data = path.read_bytes()
        downloaded = DownloadedMedia(data, "m4a", "Synthetic test tone", "CC0")
        first = store_selected_media(self.col, downloaded)
        self.assertEqual(store_selected_media(self.col, downloaded).filename, first.filename)
        self.assertTrue(first.filename.endswith(".m4a"))
        self.assertEqual(first.field_html, f"[sound:{first.filename}]")
        self.assertEqual((Path(self.col.media.dir()) / first.filename).read_bytes(), data)
        profile = authoring.FieldProfile("Audio", self.model["id"], {"audio": "Other", "source": "Back"})
        draft = self.add(Other="")
        preview = authoring.build_preview(draft, profile, self.lookup, media=first)
        self.assertEqual(preview.field_values()["Other"], first.field_html)
        self.assertIn("Synthetic test tone", preview.field_values()["Back"])
        authoring.apply_previews(self.col, [preview])
        self.assertEqual(self.col.get_note(draft.id)["Other"], first.field_html)
        self.col.undo()
        self.assertEqual(self.col.get_note(draft.id)["Other"], "")
        self.assertEqual((Path(self.col.media.dir()) / first.filename).read_bytes(), data)
        note = self.add(Back=first.field_html)
        self.assertEqual(note.cards()[0].answer_av_tags()[0].filename, first.filename)


if __name__ == "__main__":
    unittest.main()
