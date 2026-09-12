import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from anki.collection import Collection
from shirabe_addon.sync_client import SyncError
from shirabe_addon.sync_media import SyncMedia, MediaConflict
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from test_sync_snapshot import SnapshotServer


class MediaServer(SnapshotServer):
    def __init__(self):
        super().__init__()
        self.files, self.version, self.downloads = {}, 0, []
        self.page_size = 200

    def replace(self, filename, data, *, deleted=False):
        previous = self.files.get(filename)
        self.version += 1
        self.media[filename] = data
        self.files[filename] = {"id": previous["id"] if previous else len(self.files) + 1,
                                "filename": filename, "digest": hashlib.sha256(data).hexdigest(), "bytes": len(data), "deleted": deleted}
        return copy.deepcopy(self.files[filename])

    def upload_media(self, profile_id, **payload):
        if payload["request_key"] in self.receipts:
            return copy.deepcopy(self.receipts[payload["request_key"]])
        previous = self.files.get(payload["filename"])
        current = previous["digest"] if previous and not previous["deleted"] else None
        if current not in (payload["base"], payload["digest"]):
            raise SyncError(409, "SYNC_CONFLICT")
        self.media_requests.append(copy.deepcopy(payload))
        result = self.replace(payload["filename"], Path(payload["source"]).read_bytes())
        self.receipts[payload["request_key"]] = result
        return copy.deepcopy(result)

    def delete_media(self, profile_id, *, key, request_key, id, base):
        if request_key in self.receipts:
            return copy.deepcopy(self.receipts[request_key])
        previous = next(row for row in self.files.values() if row["id"] == id)
        if previous["digest"] != base:
            raise SyncError(409, "SYNC_CONFLICT")
        result = self.replace(previous["filename"], self.media[previous["filename"]], deleted=True)
        self.receipts[request_key] = result
        return copy.deepcopy(result)

    def media_manifest(self, profile_id, *, key, after=0):
        files = sorted((copy.deepcopy(row) for row in self.files.values() if row["id"] > after), key=lambda row: row["id"])[:self.page_size]
        return {"files": files, "revision": self.version, "cursor": files[-1]["id"] if files else after}

    def download_media(self, profile_id, *, key, id, digest, bytes, destination):
        row = next(row for row in self.files.values() if row["id"] == id)
        if row["digest"] != digest:
            raise SyncError(409, "SYNC_CONFLICT")
        self.downloads.append(row["filename"])
        destination.write(self.media[row["filename"]])
        destination.seek(0)


class SyncMediaTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sync.sqlite3"
        self.collection = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        self.server = MediaServer()
        self.deck = self.collection.decks.id("Japanese")
        self.note = self.collection.new_note(self.collection.models.by_name("Basic"))
        self.note.fields = ["猫 [sound:cat.mp3]", "cat"]
        self.collection.add_note(self.note, self.deck)
        self.collection.media.write_data("cat.mp3", b"original")
        snapshot = SyncSnapshot(self.path, self.server)
        snapshot.capture(self.collection, [self.deck])
        snapshot.upload()
        self.sync = SyncMedia(self.path, self.server)

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def live(self, filename="cat.mp3"):
        return Path(self.collection.media.dir()) / filename

    def cycle(self):
        self.sync.capture(self.collection)
        self.sync.exchange()
        return self.sync.apply(self.collection)

    def test_disabling_fsrs_keeps_downloaded_media_pending_and_prevents_a_new_capture(self):
        from shirabe_addon.sync_reviews import ReviewConflict

        self.server.replace("cat.mp3", b"Shirabe replacement")
        self.sync.capture(self.collection)
        self.sync.exchange()
        self.collection.set_config("fsrs", False)
        with self.assertRaisesRegex(ReviewConflict, "Enable FSRS"):
            self.sync.capture(self.collection)
        with self.assertRaisesRegex(ReviewConflict, "Enable FSRS"):
            self.sync.apply(self.collection)
        self.assertEqual(b"original", self.live().read_bytes())
        self.assertFalse(self.collection.get_config("fsrs"))
        self.collection.set_config("fsrs", True)
        self.assertEqual(1, self.sync.apply(self.collection))
        self.assertEqual(b"Shirabe replacement", self.live().read_bytes())

    def test_later_anki_media_edits_upload_once_and_unchanged_scans_do_no_transfer(self):
        self.assertEqual(0, self.cycle())
        self.live().write_bytes(b"new Anki bytes")
        self.assertEqual(0, self.cycle())
        self.assertEqual(b"new Anki bytes", self.server.media["cat.mp3"])
        self.assertEqual(2, len(self.server.media_requests))
        self.assertEqual(0, self.cycle())
        self.assertEqual(2, len(self.server.media_requests))
        self.assertEqual([], self.server.downloads)

    def test_new_note_references_and_nested_assets_join_later_sync(self):
        self.collection.media.write_data("style.css", b'@import "style.css"; url(new.png); url(new.png)')
        self.collection.media.write_data("new.png", b"image")
        self.note.fields[1] = '<link href="style.css">'
        self.collection.update_note(self.note)
        self.cycle()
        self.assertEqual({"cat.mp3", "style.css", "new.png"}, set(self.server.media))

    def test_generated_latex_media_is_selected_from_rendered_fields_without_compilation(self):
        model = self.note.note_type()
        model["latexsvg"] = True
        model["tmpls"][0]["qfmt"] = "[$]{{Front}}[/$]"
        self.collection.models.save(model)
        self.note.fields = ["x^2", "cat"]
        self.collection.update_note(self.note)
        filename = self.collection._backend.extract_latex(text="[$]x^2[/$]", svg=True, expand_clozes=False).latex[0].filename
        self.collection.media.write_data(filename, b"generated equation")
        self.collection.media.write_data("latex-" + "a" * 40 + ".svg", b"unselected equation")
        with patch("anki.latex._save_latex_image", side_effect=AssertionError("LaTeX compiler ran")):
            self.cycle()
        self.assertEqual(b"generated equation", self.server.media[filename])
        self.assertNotIn("latex-" + "a" * 40 + ".svg", self.server.media)
        self.assertEqual(["x^2", "cat"], self.collection.get_note(self.note.id).fields)

    def test_ordinary_cards_do_not_invoke_native_latex_rendering(self):
        with patch.object(self.collection._backend, "render_existing_card", side_effect=AssertionError("Rendered an ordinary card")):
            self.cycle()

    def test_remote_new_files_and_replacements_use_anki_media_and_keep_original_references(self):
        self.server.replace("cat.mp3", b"Shirabe replacement")
        self.server.replace("_Remote.PNG", b"new image")
        with patch.object(self.collection.media, "write_data", wraps=self.collection.media.write_data) as write, \
             patch.object(self.collection.media, "trash_files", wraps=self.collection.media.trash_files) as trash:
            self.assertEqual(2, self.cycle())
            self.assertEqual(2, write.call_count)
            trash.assert_called_once_with(["cat.mp3"])
        self.assertEqual(b"Shirabe replacement", self.live().read_bytes())
        self.assertEqual(b"new image", self.live("_Remote.PNG").read_bytes())
        self.assertEqual(0, self.cycle())
        self.assertEqual(1, len(self.server.media_requests))

    def test_concurrent_edits_preserve_both_sides_and_the_pending_upload(self):
        self.server.replace("cat.mp3", b"remote edit")
        self.live().write_bytes(b"local edit")
        self.sync.capture(self.collection)
        with self.assertRaises(MediaConflict):
            self.sync.exchange()
        self.assertEqual(b"remote edit", self.server.media["cat.mp3"])
        self.assertEqual(b"local edit", self.live().read_bytes())
        with SyncState(self.path) as state:
            self.assertEqual(1, state.db.execute("SELECT count(*) FROM media_uploads WHERE receipt IS NULL").fetchone()[0])

    def test_local_edit_after_download_is_not_overwritten_and_remains_pending(self):
        self.server.replace("cat.mp3", b"remote edit")
        self.sync.capture(self.collection)
        self.sync.exchange()
        self.live().write_bytes(b"new local edit")
        with self.assertRaises(MediaConflict):
            self.sync.apply(self.collection)
        self.assertEqual(b"new local edit", self.live().read_bytes())
        with SyncState(self.path) as state:
            self.assertEqual(1, state.db.execute("SELECT count(*) FROM media_incoming").fetchone()[0])

    def test_native_write_completed_before_sidecar_failure_is_not_repeated(self):
        self.server.replace("cat.mp3", b"remote edit")
        self.sync.capture(self.collection)
        self.sync.exchange()
        with patch.object(self.sync, "_complete_incoming", side_effect=OSError("Sidecar failed")):
            with self.assertRaises(OSError):
                self.sync.apply(self.collection)
        self.assertEqual(b"remote edit", self.live().read_bytes())
        self.sync = SyncMedia(self.path, self.server)
        with patch.object(self.collection.media, "write_data", wraps=self.collection.media.write_data) as write, \
             patch.object(self.collection.media, "trash_files", wraps=self.collection.media.trash_files) as trash:
            self.assertEqual(1, self.sync.apply(self.collection))
            write.assert_not_called()
            trash.assert_not_called()
        self.assertEqual(0, self.cycle())

    def test_remote_tombstone_trashes_only_the_unchanged_file_and_keeps_a_checkpoint(self):
        self.server.replace("cat.mp3", b"original", deleted=True)
        self.assertEqual(1, self.cycle())
        self.assertFalse(self.live().exists())
        self.assertEqual(0, self.cycle())
        with SyncState(self.path) as state:
            self.assertEqual("deleted", state.db.execute("SELECT digest FROM media_checkpoints WHERE filename = 'cat.mp3'").fetchone()[0])
        self.collection.media.write_data("cat.mp3", b"restored in Anki")
        self.cycle()
        self.assertFalse(self.server.files["cat.mp3"]["deleted"])
        self.assertEqual(b"restored in Anki", self.server.media["cat.mp3"])

    def test_paginated_manifest_restarts_if_its_revision_changes(self):
        self.server.page_size = 1
        self.server.replace("new.png", b"first")
        original = self.server.media_manifest
        changed = False

        def manifest(*args, **kwargs):
            nonlocal changed
            if kwargs.get("after") and not changed:
                changed = True
                self.server.replace("new.png", b"second")
            return original(*args, **kwargs)

        with patch.object(self.server, "media_manifest", side_effect=manifest):
            self.assertEqual(1, self.cycle())
        self.assertTrue(changed)
        self.assertEqual(b"second", self.live("new.png").read_bytes())

    def test_an_incoming_file_cannot_overwrite_an_unlinked_local_file(self):
        self.collection.media.write_data("private.png", b"unlinked local")
        self.server.replace("private.png", b"server bytes")
        with self.assertRaises(MediaConflict):
            self.cycle()
        self.assertEqual(b"unlinked local", self.live("private.png").read_bytes())

    def test_failed_native_write_restores_the_original_and_retains_the_download(self):
        self.server.replace("cat.mp3", b"incoming")
        self.sync.capture(self.collection)
        self.sync.exchange()
        original = self.collection.media.write_data

        def write(filename, data):
            if data == b"incoming":
                raise OSError("Native write failed")
            return original(filename, data)

        with patch.object(self.collection.media, "write_data", side_effect=write), self.assertRaises(OSError):
            self.sync.apply(self.collection)
        self.assertEqual(b"original", self.live().read_bytes())
        self.assertEqual(1, self.sync.apply(self.collection))
        self.assertEqual(b"incoming", self.live().read_bytes())

    def test_interrupted_native_replacement_resumes_after_the_original_was_trashed(self):
        self.server.replace("cat.mp3", b"incoming")
        self.sync.capture(self.collection)
        self.sync.exchange()
        original = self.collection.media.trash_files

        def interrupted(names):
            original(names)
            raise KeyboardInterrupt("Stopped between native media operations")

        with patch.object(self.collection.media, "trash_files", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.sync.apply(self.collection)
        self.assertFalse(self.live().exists())
        self.sync = SyncMedia(self.path, self.server)
        self.assertEqual(1, self.sync.apply(self.collection))
        self.assertEqual(b"incoming", self.live().read_bytes())
        self.assertEqual(0, self.cycle())
        with SyncState(self.path) as state:
            self.assertEqual(0, state.db.execute("SELECT count(*) FROM media_replacements").fetchone()[0])

    def test_missing_media_without_a_started_replacement_requires_a_choice(self):
        self.server.replace("cat.mp3", b"incoming")
        self.sync.capture(self.collection)
        self.sync.exchange()
        self.collection.media.trash_files(["cat.mp3"])
        with self.assertRaises(MediaConflict):
            self.sync.apply(self.collection)
        self.assertFalse(self.live().exists())

    def test_later_local_bytes_survive_an_interrupted_replacement(self):
        self.server.replace("cat.mp3", b"incoming")
        self.sync.capture(self.collection)
        self.sync.exchange()
        with patch.object(self.collection.media, "write_data", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.sync.apply(self.collection)
        self.collection.media.write_data("cat.mp3", b"later local edit")
        self.sync = SyncMedia(self.path, self.server)
        with self.assertRaises(MediaConflict):
            self.sync.apply(self.collection)
        self.assertEqual(b"later local edit", self.live().read_bytes())
        problem = {"filename": "cat.mp3"}
        prepared = self.sync.prepare_resolution(self.collection, problem, self.sync.conflict_detail(problem))
        self.sync.resolve(self.collection, prepared, "anki")
        self.assertEqual(0, self.cycle())
        self.assertEqual(b"later local edit", self.server.media["cat.mp3"])
        with SyncState(self.path) as state:
            self.assertEqual(0, state.db.execute("SELECT count(*) FROM media_replacements").fetchone()[0])

    def test_a_changed_replacement_receipt_cannot_restore_missing_media(self):
        self.server.replace("cat.mp3", b"incoming")
        self.sync.capture(self.collection)
        self.sync.exchange()
        with patch.object(self.collection.media, "write_data", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.sync.apply(self.collection)
        with SyncState(self.path) as state, state.db:
            state.db.execute("UPDATE media_replacements SET target = ?", (hashlib.sha256(b"other").hexdigest(),))
        self.sync = SyncMedia(self.path, self.server)
        with self.assertRaisesRegex(MediaConflict, "replacement changed"):
            self.sync.apply(self.collection)
        self.assertFalse(self.live().exists())

    def test_a_later_local_edit_survives_an_incoming_checkpoint_failure(self):
        self.server.replace("cat.mp3", b"incoming")
        self.sync.capture(self.collection)
        self.sync.exchange()
        with patch.object(self.sync, "_complete_incoming", side_effect=OSError("Sidecar failed")), self.assertRaises(OSError):
            self.sync.apply(self.collection)
        self.live().write_bytes(b"later local edit")
        self.sync = SyncMedia(self.path, self.server)
        with self.assertRaises(MediaConflict):
            self.sync.apply(self.collection)
        self.assertEqual(b"later local edit", self.live().read_bytes())

    def test_corrupt_downloads_and_changed_epochs_never_trash_the_original(self):
        incoming = self.server.replace("cat.mp3", b"incoming")
        self.sync.capture(self.collection)
        self.sync.exchange()
        saved = self.path.with_suffix(".media") / incoming["digest"]
        saved.write_bytes(b"corrupt")
        with patch.object(self.collection.media, "trash_files") as trash, self.assertRaises(MediaConflict):
            self.sync.apply(self.collection)
        trash.assert_not_called()
        saved.write_bytes(b"incoming")
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            snapshot["pair_request"]["key"] = "new epoch"
            state.put("initial_snapshot", snapshot)
        with self.assertRaises(MediaConflict):
            self.sync.apply(self.collection)
        self.assertEqual(b"original", self.live().read_bytes())

    def test_missing_local_files_do_not_silently_delete_the_server_copy(self):
        self.collection.media.trash_files(["cat.mp3"])
        with self.assertRaisesRegex(MediaConflict, "missing"):
            self.cycle()
        self.assertEqual(b"original", self.server.media["cat.mp3"])
        self.assertFalse(self.server.files["cat.mp3"]["deleted"])

    def prepare_choice(self):
        problem = {"kind": "media", "id": "cat.mp3", "filename": "cat.mp3", "code": "media_conflict"}
        detail = self.sync.conflict_detail(problem)
        return self.sync.prepare_resolution(self.collection, problem, detail)

    def test_keep_anki_sends_the_current_file_without_rewriting_the_old_saved_upload(self):
        self.server.replace("cat.mp3", b"remote")
        self.live().write_bytes(b"saved local")
        self.sync.capture(self.collection)
        with self.assertRaises(MediaConflict):
            self.sync.exchange()
        self.live().write_bytes(b"current local")
        prepared = self.prepare_choice()
        self.sync.resolve(self.collection, prepared, "anki")
        self.cycle()
        self.assertEqual(b"current local", self.server.media["cat.mp3"])
        self.assertEqual(b"current local", self.live().read_bytes())
        self.assertEqual(0, self.cycle())

    def test_keep_shirabe_replaces_the_conflicting_local_file_only_after_confirmation(self):
        self.server.replace("cat.mp3", b"remote")
        self.live().write_bytes(b"local")
        self.sync.capture(self.collection)
        self.sync.resolve(self.collection, self.prepare_choice(), "shirabe")
        self.assertEqual(b"local", self.live().read_bytes())
        self.assertEqual(1, self.cycle())
        self.assertEqual(b"remote", self.live().read_bytes())
        self.assertEqual(0, self.cycle())

    def test_a_changed_local_file_invalidates_the_open_choice(self):
        self.server.replace("cat.mp3", b"remote")
        prepared = self.prepare_choice()
        self.live().write_bytes(b"later local")
        with self.assertRaises(MediaConflict):
            self.sync.resolve(self.collection, prepared, "shirabe")
        self.assertEqual(b"later local", self.live().read_bytes())

    def test_a_changed_remote_file_requires_a_new_choice_without_discarding_pending_work(self):
        self.server.replace("cat.mp3", b"remote")
        self.live().write_bytes(b"local")
        self.sync.capture(self.collection)
        self.sync.resolve(self.collection, self.prepare_choice(), "anki")
        self.server.replace("cat.mp3", b"later remote")
        with self.assertRaises(MediaConflict):
            self.cycle()
        self.assertEqual(b"local", self.live().read_bytes())
        self.assertEqual(b"later remote", self.server.media["cat.mp3"])
        self.sync.resolve(self.collection, self.prepare_choice(), "shirabe")
        self.cycle()
        self.assertEqual(b"later remote", self.live().read_bytes())

    def test_a_missing_local_file_can_be_restored_or_explicitly_deleted_on_shirabe(self):
        self.collection.media.trash_files(["cat.mp3"])
        self.sync.resolve(self.collection, self.prepare_choice(), "shirabe")
        self.cycle()
        self.assertEqual(b"original", self.live().read_bytes())
        self.collection.media.trash_files(["cat.mp3"])
        self.sync.resolve(self.collection, self.prepare_choice(), "anki")
        self.cycle()
        self.assertTrue(self.server.files["cat.mp3"]["deleted"])
        self.assertFalse(self.live().exists())
        self.assertEqual(0, self.cycle())

    def test_a_lost_confirmed_deletion_receipt_does_not_delete_a_later_remote_restoration(self):
        self.collection.media.trash_files(["cat.mp3"])
        self.sync.resolve(self.collection, self.prepare_choice(), "anki")
        original = self.server.delete_media

        def lose(*args, **kwargs):
            original(*args, **kwargs)
            raise ConnectionError("Lost deletion receipt")

        with patch.object(self.server, "delete_media", side_effect=lose), self.assertRaises(ConnectionError):
            self.cycle()
        self.server.replace("cat.mp3", b"later remote restoration")
        self.sync = SyncMedia(self.path, self.server)
        self.cycle()
        self.assertFalse(self.server.files["cat.mp3"]["deleted"])
        self.assertEqual(b"later remote restoration", self.live().read_bytes())

    def test_an_edit_after_keeping_shirabe_is_not_overwritten_by_the_saved_choice(self):
        self.server.replace("cat.mp3", b"remote")
        self.sync.resolve(self.collection, self.prepare_choice(), "shirabe")
        self.live().write_bytes(b"later local")
        with self.assertRaises(MediaConflict):
            self.cycle()
        self.assertEqual(b"later local", self.live().read_bytes())
