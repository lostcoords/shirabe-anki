from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from shirabe_addon.sync_snapshot import SyncSnapshot, SnapshotConflict
from shirabe_addon.sync_client import SyncError
from shirabe_addon.sync_state import SyncState

try:
    from anki.collection import Collection
    from anki.buildinfo import version
except ImportError:
    Collection = None
    version = None


class SnapshotServer:
    api = "http://127.0.0.1:3000/api/v1"

    def __init__(self):
        self.pairings, self.links, self.batches = [], [], []
        self.receipts = {}
        self.finished = False
        self.lose_pair = False
        self.lose_batch = False
        self.reject_notes = False
        self.definitions = {}
        self.media, self.media_requests = {}, []
        self.lose_media = False

    def upload_media(self, profile_id, **payload):
        data = Path(payload["source"]).read_bytes()
        self.media_requests.append({**payload, "data": data})
        self.media[payload["filename"]] = data
        receipt = {"id": 1, "filename": payload["filename"], "digest": hashlib.sha256(data).hexdigest(), "bytes": len(data), "deleted": False}
        if self.lose_media:
            self.lose_media = False
            raise ConnectionError("Lost media receipt")
        return receipt

    def pair(self, **payload):
        self.pairings.append(copy.deepcopy(payload))
        if self.lose_pair:
            self.lose_pair = False
            raise ConnectionError("Lost pairing response")
        return {"id": 123, "collection_key": payload["collection_key"], "installation_id": payload["installation_id"],
                "snapshot_key": payload["key"], "sync_state": "snapshotting"}

    def select_deck(self, profile_id, **payload):
        self.links.append(payload["anki_id"])
        return {"id": len(self.links), "anki_id": payload["anki_id"], "deck_id": 321}

    def batch(self, profile_id, payload):
        self.batches.append(copy.deepcopy(payload))
        receipt = {kind: [] for kind in ("note_types", "notes", "history", "cards")}
        for kind in receipt:
            for item in payload.get(kind, []):
                row = item.get("definition", item.get("row", item))
                result = {"id": row["id"], "status": "accepted"}
                if kind == "notes":
                    result.update(status="conflict" if self.reject_notes else "accepted", snapshot="note-digest",
                                  note={"anki_id": row["id"], "guid": row["guid"], "anki_type_id": row["mid"],
                                        "field_names": [field["name"] for field in self.definitions[row["mid"]]["flds"]], "type_revision": 0,
                                        "fields": row["flds"].split("\x1f"), "tags": row["tags"].split()})
                elif kind == "cards":
                    result.update(snapshot="card-digest", card={"anki_id": row["id"]})
                elif kind == "note_types":
                    self.definitions[row["id"]] = row
                    result.update(revision=0, definition=row)
                receipt[kind].append(result)
        self.receipts.setdefault(payload["request_key"], receipt)
        if self.lose_batch:
            self.lose_batch = False
            raise ConnectionError("Lost batch response")
        return copy.deepcopy(self.receipts[payload["request_key"]])

    def finish_snapshot(self, profile_id, **payload):
        self.finished = True
        return {"id": profile_id, "snapshot_key": payload["key"], "sync_state": "ready"}


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sync.sqlite3"
        self.collection = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        self.server = SnapshotServer()
        self.snapshot = SyncSnapshot(self.path, self.server)
        self.parent = self.collection.decks.id("Japanese")
        self.child = self.collection.decks.id("Japanese::Vocabulary")
        self.other = self.collection.decks.id("Unselected")
        self.note = self.collection.new_note(self.collection.models.by_name("Basic (and reversed card)"))
        self.note.fields = ["猫", "<b>cat</b>"]
        self.collection.add_note(self.note, self.child)
        cards = self.note.cards()
        cards[1].did = self.other
        self.collection.update_card(cards[1])
        cards[0].start_timer()
        self.collection.sched.answerCard(cards[0], 3)
        cards[1].start_timer()
        self.collection.sched.answerCard(cards[1], 4)
        self.selected_card = cards[0].id

    def test_fsrs_is_required_before_a_snapshot_is_staged(self):
        from shirabe_addon.sync_reviews import ReviewConflict

        self.collection.set_config("fsrs", False)
        with self.assertRaisesRegex(ReviewConflict, "Enable FSRS"):
            self.snapshot.capture(self.collection, [self.parent])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.collection.get_config("fsrs"))

    def test_disabling_fsrs_pauses_snapshot_resume_without_discarding_it(self):
        from shirabe_addon.sync_reviews import ReviewConflict

        original = self.snapshot.capture(self.collection, [self.parent])
        self.collection.set_config("fsrs", False)
        with self.assertRaisesRegex(ReviewConflict, "Enable FSRS"):
            self.snapshot.capture(self.collection, [self.parent])
        with SyncState(self.path) as state:
            self.assertEqual(original, state.get("initial_snapshot"))
        self.collection.set_config("fsrs", True)
        self.assertEqual(original, self.snapshot.capture(self.collection, [self.parent]))

    def tearDown(self):
        self.collection.close()
        self.directory.cleanup()

    def payloads(self):
        with SyncState(self.path) as state:
            return [state.batch(key)["payload"] for key in state.snapshot_batch_keys()]

    def test_latex_references_survive_restart_without_following_later_or_unselected_cards(self):
        model = self.note.note_type()
        model["tmpls"][0]["qfmt"] = "[latex]{{Front}}[/latex]"
        model["tmpls"][0]["afmt"] = "{{FrontSide}}"
        model["tmpls"][1]["qfmt"] = "[latex]{{Back}}[/latex]"
        self.collection.models.save(model)
        self.note.fields = ["x", "y"]
        self.collection.update_note(self.note)
        names = {}
        for value in ("x", "y", "z"):
            names[value] = self.collection._backend.extract_latex(text=f"[latex]{value}[/latex]", svg=False, expand_clozes=False).latex[0].filename
            self.collection.media.write_data(names[value], value.encode())
        with patch("shirabe_addon.sync_media.SyncMedia.capture_snapshot", side_effect=OSError("Interrupted before media")):
            with self.assertRaises(OSError):
                self.snapshot.capture(self.collection, [self.parent])
        self.note.fields[0] = "z"
        self.collection.update_note(self.note)
        with patch("anki.latex._save_latex_image", side_effect=AssertionError("LaTeX compiler ran")):
            self.snapshot.capture(self.collection, [self.parent])
            self.snapshot.upload()
        self.assertEqual({names["x"]: b"x"}, self.server.media)

    def test_snapshot_uploads_referenced_and_shared_assets_with_nested_dependencies(self):
        media = self.collection.media
        files = {"猫.mp3": b"sound", "image.png": b"image", "style.css": b'@import "nested.css"; .card { background: url(image.png) }',
                 "nested.css": b'@import "style.css"; @font-face { src: url(font.woff2) }', "font.woff2": b"font", "_shared.js": b'const image = "image.png"'}
        for name, data in {**files, "unrelated.png": b"unselected"}.items():
            media.write_data(name, data)
        self.note.fields = ['猫 [sound:猫.mp3]', '<link href="style.css"><img src="missing.png">']
        self.collection.update_note(self.note)
        captured = self.snapshot.capture(self.collection, [self.parent])
        self.assertTrue(captured["media_captured"])
        self.assertIn("missing.png", captured["missing_media"])
        self.assertEqual({"note_types": 1, "notes": 1, "cards": 1, "history": 1,
                          "media_files": len(files), "media_bytes": sum(map(len, files.values()))}, captured["counts"])
        self.assertEqual([], self.server.pairings)
        self.assertEqual({}, self.server.media)
        self.snapshot.upload()
        self.assertEqual(captured["counts"]["media_bytes"], self.server.pairings[0]["media_bytes"])
        self.assertEqual(files, self.server.media)
        self.assertTrue(self.server.finished)
        with SyncState(self.path) as state:
            self.assertEqual(len(files), state.db.execute("SELECT count(*) FROM media_checkpoints").fetchone()[0])
            self.assertEqual(0, state.db.execute("SELECT count(*) FROM media_uploads WHERE receipt IS NULL").fetchone()[0])
        self.assertEqual([], list(self.path.with_suffix(".media").iterdir()))

    def test_lost_media_response_retries_the_saved_bytes_after_reopening(self):
        self.collection.media.write_data("cat.mp3", b"original")
        self.note.fields[0] = "[sound:cat.mp3]"
        self.collection.update_note(self.note)
        self.snapshot.capture(self.collection, [self.parent])
        self.server.lose_media = True
        with self.assertRaises(ConnectionError):
            self.snapshot.upload()
        self.assertFalse(self.server.finished)
        (Path(self.collection.media.dir()) / "cat.mp3").write_bytes(b"edited while offline")
        self.snapshot = SyncSnapshot(self.path, self.server)
        self.snapshot.upload()
        self.assertEqual([b"original", b"original"], [item["data"] for item in self.server.media_requests])
        self.assertEqual(self.server.media_requests[0]["request_key"], self.server.media_requests[1]["request_key"])
        self.assertEqual(b"edited while offline", (Path(self.collection.media.dir()) / "cat.mp3").read_bytes())

    def test_storage_preflight_preserves_the_saved_upload_without_sending_entities(self):
        self.collection.media.write_data("_saved.bin", b"saved bytes")
        captured = self.snapshot.capture(self.collection, [self.parent])
        with patch.object(self.server, "pair", side_effect=SyncError(413, "MEDIA_QUOTA_EXCEEDED")) as pairing:
            with self.assertRaisesRegex(SnapshotConflict, "remaining Shirabe storage"):
                self.snapshot.upload()
            self.assertEqual(11, pairing.call_args.kwargs["media_bytes"])
        self.assertEqual([], self.server.links)
        self.assertEqual([], self.server.batches)
        self.assertEqual({}, self.server.media)
        with SyncState(self.path) as state:
            self.assertEqual(captured, state.get("initial_snapshot"))
        (Path(self.collection.media.dir()) / "_saved.bin").write_bytes(b"changed after capture")
        self.snapshot = SyncSnapshot(self.path, self.server)
        self.snapshot.upload()
        self.assertEqual(11, self.server.pairings[0]["media_bytes"])
        self.assertEqual(b"saved bytes", self.server.media["_saved.bin"])

    def test_media_capture_never_follows_links_outside_the_collection(self):
        secret = Path(self.directory.name) / "private.txt"
        secret.write_bytes(b"not media")
        (Path(self.collection.media.dir()) / "linked.mp3").symlink_to(secret)
        self.note.fields[0] = "[sound:linked.mp3]"
        self.collection.update_note(self.note)
        captured = self.snapshot.capture(self.collection, [self.parent])
        self.assertIn("linked.mp3", captured["missing_media"])
        self.snapshot.upload()
        self.assertEqual({}, self.server.media)

    def test_capture_includes_descendants_and_whole_notes_but_only_selected_cards_and_history(self):
        captured = self.snapshot.capture(self.collection, [self.parent])
        self.assertEqual([self.parent, self.child], captured["deck_ids"])
        entities = {kind: [row for batch in self.payloads() for row in batch.get(kind, [])]
                    for kind in ("note_types", "notes", "history", "cards")}
        self.assertEqual([self.note.id], [row["id"] for row in entities["notes"]])
        self.assertEqual("猫\x1f<b>cat</b>", entities["notes"][0]["flds"])
        self.assertEqual(2, len(entities["note_types"][0]["definition"]["tmpls"]))
        self.assertEqual([self.selected_card], [row["row"]["id"] for row in entities["cards"]])
        self.assertEqual([self.selected_card], [row["row"]["cid"] for row in entities["history"]])
        self.assertEqual(entities["history"][0]["row"]["id"], entities["cards"][0]["last_review_id"])
        self.assertEqual(1, captured["unselected_siblings"])
        self.assertEqual([], self.server.pairings)
        self.assertIn("next_day_at", captured["pair_request"]["source_metadata"]["capture"])

    def test_preview_counts_describe_the_saved_copy_when_resuming_after_local_edits(self):
        self.collection.media.write_data("cat.mp3", b"original")
        self.note.fields[0] = "[sound:cat.mp3]"
        self.collection.update_note(self.note)
        captured = self.snapshot.capture(self.collection, [self.parent])
        self.collection.media.write_data("later.mp3", b"later media")
        self.note.fields[0] = "[sound:later.mp3]"
        self.collection.update_note(self.note)
        extra = self.collection.new_note(self.collection.models.by_name("Basic"))
        extra.fields = ["犬", "dog"]
        self.collection.add_note(extra, self.child)
        resumed = SyncSnapshot(self.path, self.server).capture(self.collection, [self.parent])
        self.assertEqual({"note_types": 1, "notes": 1, "cards": 1, "history": 1,
                          "media_files": 1, "media_bytes": 8}, resumed["counts"])
        self.assertEqual(captured, resumed)
        self.assertEqual([], self.server.pairings)

    def test_empty_deck_preview_has_zero_counts(self):
        empty = self.collection.decks.id("Empty")
        captured = self.snapshot.capture(self.collection, [empty])
        self.assertEqual({"note_types": 0, "notes": 0, "cards": 0, "history": 0,
                          "media_files": 0, "media_bytes": 0}, captured["counts"])
        self.assertEqual(0, captured["unselected_siblings"])

    def test_accepted_template_diagnostics_are_saved_separately_from_definitions(self):
        captured = self.snapshot.capture(self.collection, [self.parent])
        send = self.server.batch
        issues = [{"template": 0, "side": "question", "code": "unknown_filter", "detail": "edit"}]

        def checked(profile_id, payload):
            result = send(profile_id, payload)
            for entry in result["note_types"]:
                entry["template_diagnostics"] = issues
            return result

        with patch.object(self.server, "batch", checked):
            self.snapshot.upload()
        with SyncState(self.path) as state:
            self.assertEqual("uploaded", state.get("initial_snapshot")["phase"])
            report = state.checkpoint("template_diagnostics", self.note.mid)
            self.assertEqual(issues, report["payload"])
            self.assertEqual("0", report["digest"])
            self.assertNotIn("template_diagnostics", state.checkpoint("note_type", self.note.mid)["payload"])
        self.assertEqual(issues, self.snapshot.template_diagnostics()[0]["issues"])
        with SyncState(self.path) as state:
            definition = state.checkpoint("note_type", self.note.mid)["payload"]
            state.remember_checkpoint("note_type", self.note.mid, "1", definition)
        self.assertIsNone(self.snapshot.template_diagnostics()[0]["issues"])
        with SyncState(self.path) as state:
            state.remember_checkpoint("template_diagnostics", self.note.mid, "1", [])
        self.assertEqual([], self.snapshot.template_diagnostics()[0]["issues"])
        self.assertEqual(captured["root_deck_ids"], [self.parent])

    def test_capture_distinguishes_a_review_from_a_later_manual_reset(self):
        review_id = self.collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", self.selected_card)
        self.collection.sched.schedule_cards_as_new([self.selected_card], reset_counts=True)
        latest = self.collection.db.first("SELECT id, ease, type FROM revlog WHERE cid = ? ORDER BY id DESC LIMIT 1", self.selected_card)
        self.assertGreater(latest[0], review_id)
        self.assertEqual(0, latest[1])
        self.snapshot.capture(self.collection, [self.parent])
        entry = next(row for batch in self.payloads() for row in batch.get("cards", []))
        self.assertEqual(review_id, entry["last_review_id"])
        self.assertEqual(latest[0], entry["last_event_id"])
        self.assertNotEqual(entry["last_review_id"], entry["last_event_id"])
        self.assertEqual((0, 0), (entry["row"]["type"], entry["row"]["reps"]))

    def test_capture_distinguishes_a_review_from_a_manual_due_date_change(self):
        review_id = self.collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", self.selected_card)
        self.collection.sched.set_due_date([self.selected_card], "8")
        self.snapshot.capture(self.collection, [self.parent])
        entry = next(row for batch in self.payloads() for row in batch.get("cards", []))
        self.assertEqual(review_id, entry["last_review_id"])
        self.assertGreater(entry["last_event_id"], review_id)
        self.assertEqual(0, self.collection.db.scalar("SELECT ease FROM revlog WHERE id = ?", entry["last_event_id"]))

    def test_capture_names_the_review_when_it_is_the_latest_scheduling_event(self):
        self.snapshot.capture(self.collection, [self.parent])
        entry = next(row for batch in self.payloads() for row in batch.get("cards", []))
        self.assertEqual(entry["last_review_id"], entry["last_event_id"])

    def test_filtered_cards_belong_to_their_original_deck(self):
        filtered = self.collection.decks.new_filtered("Filtered")
        card = self.collection.get_card(self.selected_card)
        card.odid, card.did = card.did, filtered
        self.collection.update_card(card)
        captured = self.snapshot.capture(self.collection, [self.parent])
        rows = [item["row"] for batch in self.payloads() for item in batch.get("cards", [])]
        self.assertEqual([self.selected_card], [row["id"] for row in rows])
        self.assertEqual(self.child, rows[0]["odid"])
        self.assertNotIn(filtered, captured["deck_ids"])

    def test_history_read_pages_do_not_repeat_the_selection_for_each_wire_batch(self):
        for _ in range(205):
            card = self.collection.get_card(self.selected_card)
            card.start_timer()
            self.collection.sched.answerCard(card, 1)
        with patch.object(self.collection.db, "all", wraps=self.collection.db.all) as reads:
            rows = list(self.snapshot._rows(self.collection, "revlog", "cid = ?", (self.selected_card,)))
        queries = [call.args[0] for call in reads.call_args_list if call.args[0].startswith("SELECT revlog.*")]
        self.assertGreater(len(rows), 200)
        self.assertEqual(len(rows), self.collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", self.selected_card))
        self.assertEqual(sorted({row["id"] for row in rows}), [row["id"] for row in rows])
        self.assertEqual(2, len(queries))
        batches = list(self.snapshot._batches("history", ({"row": row} for row in rows), "epoch"))
        self.assertTrue(all(len(batch["history"]) <= 200 for batch in batches))

    def test_paged_collection_scanning_grows_linearly_when_the_collection_doubles(self):
        from anki.collection import AddNoteRequest

        counts = []
        for offset in (0, 1000):
            notes = []
            for index in range(offset, offset + 1000):
                note = self.collection.new_note(self.collection.models.by_name("Basic"))
                note.fields = [f"Word {index}", f"Meaning {index}"]
                notes.append(AddNoteRequest(note=note, deck_id=self.child))
            self.collection.add_notes(notes)
            with patch.object(self.collection.db, "all", wraps=self.collection.db.all) as queries:
                entities = {kind: list(rows) for kind, rows in self.snapshot._source_entities(self.collection, json.dumps([self.child]), None)}
            self.assertEqual(offset + 1001, len(entities["notes"]))
            self.assertEqual(offset + 1001, len(entities["cards"]))
            steps = 0

            def tick():
                nonlocal steps
                steps += 100
                return 0

            with closing(sqlite3.connect(Path(self.collection.path).as_uri() + "?mode=ro", uri=True)) as database:
                database.set_progress_handler(tick, 100)
                for call in queries.call_args_list:
                    if call.args[0].startswith(("SELECT notes.*", "SELECT cards.*", "SELECT revlog.*")):
                        database.execute(call.args[0], call.args[1:]).fetchall()
            counts.append(steps)
        self.assertGreater(counts[0], 0)
        self.assertLess(counts[1], counts[0] * 2.6, counts)

    def test_restart_reuses_frozen_pairing_and_batches_despite_later_anki_edits(self):
        captured = self.snapshot.capture(self.collection, [self.parent])
        payloads = self.payloads()
        self.server.lose_pair = True
        with self.assertRaises(ConnectionError):
            self.snapshot.upload()
        self.note.fields[0] = "changed while offline"
        self.collection.update_note(self.note)
        self.snapshot = SyncSnapshot(self.path, self.server)
        self.assertEqual(captured, self.snapshot.capture(self.collection, [self.parent]))
        self.assertEqual(payloads, self.payloads())
        self.server.lose_batch = True
        with self.assertRaises(ConnectionError):
            self.snapshot.upload()
        self.snapshot = SyncSnapshot(self.path, self.server)
        self.snapshot.upload()
        self.assertEqual(self.server.pairings[0], self.server.pairings[1])
        self.assertEqual(self.server.batches[0], self.server.batches[1])
        self.assertTrue(self.server.finished)
        self.assertEqual([self.parent, self.child], self.server.links)
        count = len(self.server.batches)
        self.snapshot.upload()
        self.assertEqual(count, len(self.server.batches))
        with SyncState(self.path) as state:
            self.assertEqual(["猫", "<b>cat</b>"], state.checkpoint("note", self.note.id)["payload"]["fields"])
            self.assertIsNotNone(state.checkpoint("source_note", self.note.id))

    def test_initial_note_conflict_uses_the_normal_chooser_and_preserves_its_original_receipt(self):
        from shirabe_addon.sync_changes import SyncChanges, SyncConflict

        self.snapshot.capture(self.collection, [self.parent])
        self.server.reject_notes = True
        with self.assertRaises(SyncConflict) as paused:
            self.snapshot.upload()
        problem = paused.exception.conflicts[0]
        with SyncState(self.path) as state:
            self.assertEqual("resolving", state.get("initial_snapshot")["phase"])
            self.assertEqual([problem["request_key"]], state.get("changes")["keys"])
            receipt = state.batch(problem["request_key"])["receipt"]
            self.assertFalse(state.batch_applied(problem["request_key"]))
            key = state.get("initial_snapshot")["pair_request"]["key"]
        self.assertFalse(self.server.finished)
        changes = SyncChanges(self.path, self.server)
        detail = {"key": key, "id": None, "resolved": False, "snapshot": "a" * 64,
                  "note": receipt["notes"][0]["note"]}
        prepared = changes.prepare_note_resolution(self.collection, problem, detail)
        choice = changes.resolve_note(self.collection, prepared, ["猫", "chosen meaning"], ["chosen"])
        self.server.reject_notes = False
        changes = SyncChanges(self.path, self.server)
        changes.upload()
        changes.apply(self.collection)
        self.snapshot = SyncSnapshot(self.path, self.server)
        self.snapshot.upload()
        self.assertTrue(self.server.finished)
        self.assertEqual(["猫", "chosen meaning"], self.collection.get_note(self.note.id).fields)
        with SyncState(self.path) as state:
            self.assertEqual("uploaded", state.get("initial_snapshot")["phase"])
            self.assertIsNone(state.get("changes"))
            self.assertEqual(receipt, state.batch(problem["request_key"])["receipt"])
            self.assertTrue(state.batch_applied(problem["request_key"]))
            self.assertTrue(state.batch_applied(choice))
            self.assertEqual("猫\x1fchosen meaning", state.checkpoint("source_note", self.note.id)["payload"]["flds"])
        self.assertEqual(1, sum(batch["request_key"] == problem["request_key"] for batch in self.server.batches))

    def test_initial_conflict_cannot_be_bypassed_by_resuming_the_upload(self):
        from shirabe_addon.sync_changes import SyncConflict

        self.snapshot.capture(self.collection, [self.parent])
        self.server.reject_notes = True
        with self.assertRaises(SyncConflict):
            self.snapshot.upload()
        sent = copy.deepcopy(self.server.batches)
        with self.assertRaisesRegex(SnapshotConflict, "Resolve"):
            SyncSnapshot(self.path, self.server).upload()
        self.assertEqual(sent, self.server.batches)
        self.assertFalse(self.server.finished)

    def test_rejected_rows_prevent_finishing_even_after_restart(self):
        self.snapshot.capture(self.collection, [self.parent])
        self.server.reject_notes = True
        for _ in range(2):
            with self.assertRaises(SnapshotConflict):
                self.snapshot.upload()
            self.snapshot = SyncSnapshot(self.path, self.server)
        self.assertFalse(self.server.finished)
        self.assertEqual(2, len(self.server.batches))

    def test_pausing_after_one_batch_keeps_the_upload_resumable(self):
        self.snapshot.capture(self.collection, [self.parent])
        progress = []
        with self.assertRaisesRegex(SnapshotConflict, "paused"):
            self.snapshot.upload(cancelled=lambda: bool(self.server.batches), progress=lambda done, total: progress.append((done, total)))
        self.assertFalse(self.server.finished)
        self.assertEqual(1, len(self.server.batches))
        self.snapshot = SyncSnapshot(self.path, self.server)
        self.snapshot.upload(progress=lambda done, total: progress.append((done, total)))
        self.assertTrue(self.server.finished)
        self.assertEqual(len(self.server.receipts), len(self.server.batches))
        self.assertEqual(progress[-1][0], progress[-1][1])

    def test_capture_failure_does_not_leave_a_partial_snapshot_or_pending_batches(self):
        original = SyncState._enqueue
        calls = 0

        def fail(state, key, entities):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Disk full")
            return original(state, key, entities)

        with patch.object(SyncState, "_enqueue", fail):
            with self.assertRaises(OSError):
                self.snapshot.capture(self.collection, [self.parent])
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("initial_snapshot"))
            self.assertEqual([], state.pending())
        self.snapshot.capture(self.collection, [self.parent])

    def test_batches_obey_count_and_wire_byte_limits(self):
        model = self.collection.models.by_name("Basic")
        for index in range(205):
            note = self.collection.new_note(model)
            note.fields = [f"単語{index}", "猫" * 10000]
            self.collection.add_note(note, self.child)
        self.snapshot.capture(self.collection, [self.parent])
        batches = self.payloads()
        self.assertGreater(len([batch for batch in batches if batch.get("notes")]), 2)
        for batch in batches:
            self.assertLessEqual(len(json.dumps(batch).encode()), 4 * 1024 * 1024)
            self.assertLessEqual(len(batch.get("note_types", [])), 20)
            for kind in ("notes", "cards", "history"):
                self.assertLessEqual(len(batch.get(kind, [])), 200)

    def test_empty_missing_filtered_and_changed_selections_are_rejected(self):
        filtered = self.collection.decks.new_filtered("Filtered")
        for ids in ([], [True], [-1], [999], [filtered]):
            with self.assertRaises(SnapshotConflict):
                self.snapshot.capture(self.collection, ids)
        self.snapshot.capture(self.collection, [self.parent])
        with self.assertRaises(SnapshotConflict):
            self.snapshot.capture(self.collection, [self.other])
        self.server.api = "https://another.example/api/v1"
        with self.assertRaises(SnapshotConflict):
            self.snapshot.upload()
