import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import support
from offline_fixture import prepared
from shirabe_addon import offline, offline_ui


def backend(**options):
    credentials = json.dumps({"access": "fixture-token", "expiresAt": 2000})
    return offline.DictionaryBackend("https://fixture.invalid", credentials, now=lambda: 1, **options)


def response(value, after_read=lambda: None):
    class Reply(io.BytesIO):
        headers = {}

        def read(self, size):
            data = super().read(size)
            after_read()
            return data
    return Reply(json.dumps(value).encode())


class PreparationAccess(unittest.TestCase):
    def test_signout_and_expiry_discard_in_flight_responses(self):
        for reason in ("signout", "expiry"):
            with self.subTest(reason=reason):
                source = backend()
                def revoke():
                    if reason == "signout":
                        offline.invalidate_preparations()
                    else:
                        source._now = lambda: 2
                source._opener = SimpleNamespace(open=lambda *_a, **_k: response({"words": []}, revoke))
                with self.assertRaises(offline.SnapshotError) as caught:
                    source.identify({"lemma": "猫", "surface": "猫"})
                self.assertEqual(caught.exception.code, "reconnect_required")

    def test_denied_access_blocks_subsequent_requests(self):
        for status in (401, 403):
            with self.subTest(status=status):
                source = backend()
                calls = []
                def denied(*_args, **_kwargs):
                    calls.append(status)
                    raise urllib.error.HTTPError("https://fixture.invalid", status, "denied", {}, io.BytesIO())
                source._opener = SimpleNamespace(open=denied)
                for _ in range(2):
                    with self.assertRaises(offline.SnapshotError) as caught:
                        source.dictionaries()
                    self.assertEqual(caught.exception.code, "reconnect_required")
                self.assertEqual(calls, [status])

    def test_dictionary_fingerprint_change_invalidates_the_backend(self):
        for initial in (None, "v1"):
            with self.subTest(initial=initial):
                source = backend(stack_fingerprint=initial)
                answers = iter([{"stackFingerprint": "v1"}, {"stackFingerprint": "v1"}, {"stackFingerprint": "v2"}])
                source._opener = SimpleNamespace(open=lambda *_a, **_k: response(next(answers)))
                source.parse("猫")
                source.parse("猫")
                with self.assertRaises(offline.SnapshotError) as caught:
                    source.parse("猫")
                self.assertEqual(caught.exception.code, "dictionary_changed")
                with self.assertRaises(offline.SnapshotError):
                    source.check_access()

    def test_changed_fingerprint_on_first_response_is_rejected(self):
        source = backend(stack_fingerprint="v1")
        source._opener = SimpleNamespace(open=lambda *_a, **_k: response({"stackFingerprint": "v2"}))
        with self.assertRaises(offline.SnapshotError) as caught:
            source.parse("猫")
        self.assertEqual(caught.exception.code, "dictionary_changed")

    def test_missing_or_invalid_fingerprint_cannot_use_a_pinned_dictionary_selection(self):
        for method in ("parse", "identify"):
            for fingerprint in ({}, {"stackFingerprint": None}, {"stackFingerprint": 7}, {"stackFingerprint": ""}):
                with self.subTest(method=method, fingerprint=fingerprint):
                    source = backend(stack_fingerprint="v1")
                    source._opener = SimpleNamespace(open=lambda *_a, **_k: response(fingerprint))
                    with self.assertRaises(offline.SnapshotError) as caught:
                        getattr(source, method)("猫" if method == "parse" else {"lemma": "猫", "surface": "猫"})
                    self.assertEqual(caught.exception.code, "server_update_required" if fingerprint.get("stackFingerprint") is None else "dictionary_changed")


@unittest.skipUnless(importlib.util.find_spec("anki"), "Anki libraries required; no GUI is opened")
class PreparationAccessStorage(unittest.TestCase):
    def setUp(self):
        from anki.collection import Collection

        self.directory = tempfile.TemporaryDirectory(prefix="shirabe-preparation-access-")
        self.addCleanup(self.directory.cleanup)
        self.col = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.addCleanup(self.col.close)
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"] = "猫"
        note["Back"] = "user's answer"
        self.col.add_note(note, 1)
        note["Back"] = "user's newer answer"
        self.col.update_note(note)
        self.note_id = note.id
        self.captured = offline_ui.capture_notes(self.col, [note.id])[0]
        self.batch = offline_ui.PreparedBatch(rows=[(self.captured, prepared(("猫",)))])
        self.cards = self.col.db.all("select * from cards order by id")
        self.source = backend()

    def assert_unchanged(self):
        note = self.col.get_note(self.note_id)
        self.assertEqual(offline_ui.note_revision(note, note.note_type()), self.captured.revision)
        self.assertEqual(note["Back"], "user's newer answer")
        self.assertNotIn("ShirabeLookup", note)
        self.assertEqual(self.col.db.all("select * from cards order by id"), self.cards)
        self.assertEqual(self.col.db.scalar("select count(*) from revlog"), 0)

    def test_signed_out_queued_save_does_not_write_media_or_notes(self):
        before = set(Path(self.col.media.dir()).iterdir())
        offline.invalidate_preparations()
        with self.assertRaises(offline.SnapshotError):
            offline_ui.apply_prepared(self.col, self.batch, {}, check_access=self.source.check_access)
        self.assert_unchanged()
        self.assertEqual(set(Path(self.col.media.dir()).iterdir()), before)

    def test_signout_during_save_undoes_only_the_preparation(self):
        update = self.col.update_notes
        def update_then_signout(notes):
            result = update(notes)
            offline.invalidate_preparations()
            return result
        with patch.object(self.col, "update_notes", update_then_signout):
            with self.assertRaises(offline.SnapshotError):
                offline_ui.apply_prepared(self.col, self.batch, {}, check_access=self.source.check_access)
        self.assert_unchanged()
        self.assertGreater(offline.snapshot_storage(self.col.media).files, 0)


if __name__ == "__main__":
    unittest.main()
