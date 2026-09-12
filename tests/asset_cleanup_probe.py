import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from offline_fixture import prepared
from shirabe_addon import assets, offline, offline_ui, template_ui
from anki.collection import Collection


class NativeCleanup(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="shirabe-cleanup-")
        self.addCleanup(self.directory.cleanup)
        self.col = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.addCleanup(self.col.close)
        self.media = self.col.media

    def bundle(self, body, suffix="attach.js"):
        data = body.encode()
        name = f"_shirabe_{hashlib.sha256(data).hexdigest()[:20]}_{suffix}"
        self.assertEqual(self.media.write_data(name, data), name)
        return name

    def note(self, text):
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"] = text
        self.col.add_note(note, 1)
        return note

    def test_snapshot_only_in_saved_removal_baseline_is_not_unused(self):
        snapshot = offline.persist_snapshot(self.media, prepared()).filename
        self.col.set_config(template_ui.settings_key(999999), {
            "templates": [{"before": {"afmt": snapshot}, "after": {"afmt": "{{Front}}"}}],
        })
        self.assertNotIn(snapshot, offline_ui.unreferenced_snapshots(self.col))

    def test_real_media_trash_preserves_current_templates_fields_baselines_and_other_media(self):
        old_attach = self.bundle("old attach")
        old_card = self.bundle("old card", "shirabe-card.js")
        current = assets.install(self.media)
        field_bundle = self.bundle("from note")
        question_bundle = self.bundle("from question")
        css_bundle = self.bundle("from CSS")
        before_bundle = self.bundle("saved before")
        after_bundle = self.bundle("saved after")
        snapshot = offline.persist_snapshot(self.media, prepared()).filename
        used_snapshot = offline.persist_snapshot(self.media, prepared(content_version="used")).filename
        note = self.note(f'<img src="{field_bundle}">{used_snapshot}')
        model = self.col.models.get(note.mid)
        model["tmpls"][0]["qfmt"] += f'<script src="{question_bundle}"></script>'
        model["tmpls"][0]["afmt"] += "".join(f'<script src="{name}"></script>' for name in current.values())
        model["css"] += f'\n/* {css_bundle} */'
        self.col.models.update_dict(model)
        self.col.set_config(template_ui.settings_key(999999), {
            "templates": [{"before": {"qfmt": before_bundle}, "after": {"afmt": after_bundle}}],
        })
        corrupt = self.media.write_data("_shirabe_" + "0" * 20 + "_attach.js", b"wrong digest")
        other = [self.media.write_data(name, b"mine") for name in (
            "photo.png", "_shirabe_custom.js", "_shirabe_" + "f" * 20 + "_other.js", "attach.js",
        )]
        media_path = Path(self.media.dir())
        originals = {path.name: path.read_bytes() for path in media_path.iterdir() if path.is_file()}
        cards = self.col.db.all("select * from cards")
        fields = list(self.col.get_note(note.id).fields)
        review = offline_ui.inspect_cleanup(self.col)
        self.assertEqual(set(review.names), {old_attach, old_card, snapshot})
        self.assertEqual(review.snapshots.files, 2)
        self.assertEqual(review.code.files, 10)
        self.assertEqual(review.code.bytes_used, sum(len(data) for name, data in originals.items() if assets.BUNDLE_NAME.fullmatch(name)))
        self.assertTrue(self.media.have(old_attach), "inspection must not delete")
        undo_before = self.col.undo_status()
        removed = offline_ui.trash_reviewed(self.col, review.names)
        self.assertEqual(set(removed), set(review.names))
        self.assertEqual(self.col.undo_status(), undo_before)
        self.assertEqual(self.col.db.all("select * from cards"), cards)
        self.assertEqual(self.col.get_note(note.id).fields, fields)
        self.assertTrue(all(self.media.have(name) for name in [*current.values(), field_bundle, question_bundle, css_bundle, before_bundle, after_bundle, used_snapshot, corrupt, *other]))
        self.assertTrue(all(not self.media.have(name) for name in removed))
        self.media.restore_trash()
        self.assertEqual({name: (media_path / name).read_bytes() for name in originals}, originals)

    def test_trash_rechecks_new_references_and_hashes_and_never_expands_reviewed_list(self):
        names = [self.bundle(f"old {i}") for i in range(6)]
        review = offline_ui.inspect_cleanup(self.col)
        self.assertEqual(set(review.names), set(names))
        note = self.note(names[0])
        model = self.col.models.get(note.mid)
        model["tmpls"][0]["afmt"] += f'<script src="{names[1]}"></script>'
        self.col.models.update_dict(model)
        self.col.set_config(template_ui.settings_key(note.mid), {"templates": [{"before": {"qfmt": names[2]}}]})
        (Path(self.media.dir()) / names[3]).write_bytes(b"changed after review")
        target = Path(self.directory.name) / "untouched.js"
        target.write_bytes(b"old 4")
        (Path(self.media.dir()) / names[4]).unlink()
        (Path(self.media.dir()) / names[4]).symlink_to(target)
        unreviewed = self.bundle("not reviewed")
        self.assertEqual(offline_ui.trash_reviewed(self.col, (*review.names, "../untouched.js", "photo.png", names[5])), [names[5]])
        self.assertTrue(all(self.media.have(name) for name in names[:5]))
        self.assertTrue(self.media.have(unreviewed))
        self.assertEqual(target.read_bytes(), b"old 4")
        self.assertTrue((Path(self.media.dir()) / names[4]).is_symlink())

    def test_each_review_is_bounded_without_hiding_other_generations_from_storage_totals(self):
        names = {self.bundle(f"version {i}") for i in range(5)}
        with patch.object(offline_ui, "MAX_CLEANUP_FILES", 2):
            review = offline_ui.inspect_cleanup(self.col)
            self.assertEqual(len(review.names), 2)
            self.assertEqual(review.code.files, 5)
            self.assertEqual(set(offline_ui.trash_reviewed(self.col, review.names)), set(review.names))
            following = offline_ui.inspect_cleanup(self.col)
            self.assertEqual(len(following.names), 2)
            self.assertTrue(set(following.names).isdisjoint(review.names))
            self.assertEqual(following.code.files, 3)
            self.assertEqual(set(offline_ui.trash_reviewed(self.col, following.names)), set(following.names))
            final = offline_ui.inspect_cleanup(self.col)
            self.assertEqual(set(final.names), names - set(review.names) - set(following.names))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_reviewed_snapshot_replaced_by_pipe_is_never_opened(self):
        name = offline.persist_snapshot(self.media, prepared()).filename
        path = Path(self.media.dir()) / name
        path.unlink()
        os.mkfifo(path)
        with patch.object(Path, "open", side_effect=AssertionError("A non-regular snapshot must not be opened")):
            self.assertIsNone(offline.read_snapshot(self.media, name))
            self.assertEqual(offline_ui.trash_reviewed(self.col, [name]), [])


if __name__ == "__main__":
    unittest.main()
