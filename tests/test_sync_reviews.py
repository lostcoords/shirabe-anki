from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import support
from shirabe_addon.sync_reviews import apply_review, card_row, history_row, ReviewConflict, same_state
from shirabe_addon.sync_state import SyncState

try:
    from anki.collection import Collection
    from anki.buildinfo import version
except ImportError:
    Collection = None
    version = None


@unittest.skipUnless(Collection and version == "26.08.1", "Requires the pinned Anki 26.08.1 Python runtime")
class SyncReviewsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.collection = Collection(str(root / "collection.anki2"))
        self.collection.set_config("fsrs", True)
        self.state = SyncState(root / "sync.sqlite3")
        note = self.collection.new_note(self.collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        self.collection.add_note(note, self.collection.decks.id("Reviews"))
        self.card = note.cards()[0]
        before = card_row(self.collection, self.card.id)
        after = copy.deepcopy(before)
        instant = datetime.now(timezone.utc)
        after.update(type=2, queue=2, due=3, ivl=3, reps=1, left=0,
                     data=json.dumps({"s": 3.12345, "d": 2.1181, "lrt": int(instant.timestamp())}))
        self.payload = {
            "sync_id": str(uuid.uuid4()), "anki_card_id": self.card.id,
            "rating": 3, "reviewed_at": instant.isoformat(),
            "anki": {"card_before": before, "card_after": after, "note_guid": note.guid, "anki_type_id": note.mid,
                     "revlog": {"cid": self.card.id, "usn": -1, "ease": 3, "ivl": 3, "lastIvl": 0, "factor": 200, "time": 123, "type": 0}},
        }

    def tearDown(self):
        self.state.close()
        self.collection.close()
        self.directory.cleanup()

    def test_disabling_fsrs_refuses_review_delivery_without_changing_anki_or_losing_the_review(self):
        self.collection.set_config("fsrs", False)
        before = card_row(self.collection, self.card.id)
        with self.assertRaisesRegex(ReviewConflict, "Enable FSRS"):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        self.assertFalse(self.collection.get_config("fsrs"))
        self.assertIsNone(self.state.review(self.payload["sync_id"]))
        self.collection.set_config("fsrs", True)
        apply_review(self.collection, self.state, self.payload)
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_card_and_history_are_written_once_and_retry_keeps_later_state(self):
        returned = apply_review(self.collection, self.state, self.payload)
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))
        self.assertTrue(same_state(card_row(self.collection, self.card.id), self.payload["anki"]["card_after"]))
        later = self.collection.get_card(self.card.id)
        later.reps += 1
        self.collection.update_card(later)
        self.assertEqual(returned, apply_review(self.collection, self.state, self.payload))
        self.assertEqual(2, self.collection.get_card(self.card.id).reps)
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))

    def test_history_only_review_cannot_apply_without_a_reconciled_schedule(self):
        self.payload["history_only"] = True
        before = card_row(self.collection, self.card.id)
        with self.assertRaisesRegex(ReviewConflict, "Reconcile the card's schedule"):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_delivered_new_review_consumes_the_native_daily_limit_once(self):
        deck_id = self.card.did
        preset = self.collection.decks.config_dict_for_deck_id(deck_id)
        preset["new"]["perDay"] = 3
        self.collection.decks.update_config(preset)
        self.collection.decks.select(deck_id)
        for index in range(4):
            note = self.collection.new_note(self.collection.models.by_name("Basic"))
            note.fields = [f"remaining {index}", "answer"]
            self.collection.add_note(note, deck_id)
        self.assertEqual(3, self.collection.sched.counts()[0])
        apply_review(self.collection, self.state, self.payload)
        self.assertEqual(2, self.collection.sched.counts()[0])
        self.assertFalse(self.collection.undo_status().undo)
        self.assertEqual([self.collection.sched.today, 1], self.collection.decks.get(deck_id)["newToday"])
        apply_review(self.collection, self.state, self.payload)
        self.assertEqual(2, self.collection.sched.counts()[0])

    def test_review_and_interday_learning_consume_review_budget_but_intraday_learning_does_not(self):
        for queue, card_type, expected in ((1, 1, 0), (2, 2, 1), (3, 1, 1)):
            with self.subTest(queue=queue):
                card = self.collection.get_card(self.card.id)
                card.queue, card.type = queue, card_type
                card.due = int(datetime.now().timestamp()) if queue == 1 else self.collection.sched.today
                self.collection.update_card(card)
                before = card_row(self.collection, card.id)
                payload = copy.deepcopy(self.payload)
                payload["sync_id"] = str(uuid.uuid4())
                payload["anki"]["card_before"] = before
                payload["anki"]["card_after"]["reps"] = before["reps"] + 1
                prior = self.collection.decks.get(card.did)
                apply_review(self.collection, self.state, payload)
                after = self.collection.decks.get(card.did)
                self.assertEqual(prior["newToday"][1], after["newToday"][1])
                self.assertEqual(prior["revToday"][1] + expected, after["revToday"][1])
                self.assertEqual(prior["timeToday"][1] + 123, after["timeToday"][1])

    def test_a_review_from_an_earlier_day_does_not_consume_todays_budget(self):
        self.payload["reviewed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        prior = self.collection.decks.get(self.card.did)
        apply_review(self.collection, self.state, self.payload)
        after = self.collection.decks.get(self.card.did)
        for field in ("newToday", "revToday", "timeToday"):
            self.assertEqual(prior[field], after[field])

    def test_a_failed_history_insert_rolls_back_the_public_card_update(self):
        before = card_row(self.collection, self.card.id)
        self.collection.db.execute("CREATE TEMP TRIGGER reject_history BEFORE INSERT ON revlog BEGIN SELECT RAISE(ABORT, 'injected history failure'); END")
        with self.assertRaises(Exception):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from revlog"))
        self.assertFalse(self.state.review(self.payload["sync_id"])["applied"])
        self.collection.db.execute("DROP TRIGGER reject_history")
        apply_review(self.collection, self.state, self.payload)
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))

    def test_a_failed_counter_update_rolls_back_card_history_and_parent_counters(self):
        self.collection.decks.rename(self.card.did, "Japanese::Reviews")
        parent_id = self.collection.decks.id("Japanese")
        before = card_row(self.collection, self.card.id)
        deck = self.collection.decks.get(self.card.did)
        parent = self.collection.decks.get(parent_id)
        update_stats = self.collection.sched.update_stats

        def fail(*args, **kwargs):
            update_stats(*args, **kwargs)
            raise RuntimeError("counter update interrupted")

        with patch.object(self.collection.sched, "update_stats", fail):
            with self.assertRaisesRegex(RuntimeError, "counter update interrupted"):
                apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))
        self.assertEqual(deck["newToday"], self.collection.decks.get(self.card.did)["newToday"])
        self.assertEqual(parent["newToday"], self.collection.decks.get(parent_id)["newToday"])
        apply_review(self.collection, self.state, self.payload)
        self.assertEqual(1, self.collection.decks.get(self.card.did)["newToday"][1])
        self.assertEqual(1, self.collection.decks.get(parent_id)["newToday"][1])

    def test_a_crash_after_anki_commit_reuses_the_reserved_id(self):
        with patch.object(self.state, "finish_review", side_effect=RuntimeError("sidecar unavailable")):
            with self.assertRaisesRegex(RuntimeError, "sidecar unavailable"):
                apply_review(self.collection, self.state, self.payload)
        planned = self.state.review(self.payload["sync_id"])
        self.assertFalse(planned["applied"])
        self.assertIsNotNone(history_row(self.collection, planned["row"]["id"]))
        self.state.close()
        self.collection.close()
        self.collection = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.state = SyncState(Path(self.directory.name) / "sync.sqlite3")
        returned = apply_review(self.collection, self.state, self.payload)
        self.assertEqual(planned["row"]["id"], returned["row"]["id"])
        self.assertTrue(self.state.review(self.payload["sync_id"])["applied"])
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))
        self.assertEqual(1, self.collection.decks.get(self.card.did)["newToday"][1])

    def test_an_existing_timestamp_is_not_mistaken_for_the_delivered_event(self):
        instant = datetime.fromisoformat(self.payload["reviewed_at"])
        source_id = int(instant.timestamp()) * 1000 + instant.microsecond // 1000
        row = self.payload["anki"]["revlog"]
        self.collection.db.execute("INSERT INTO revlog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                  source_id, row["cid"], row["usn"], row["ease"], row["ivl"], row["lastIvl"], row["factor"], row["time"], row["type"])
        returned = apply_review(self.collection, self.state, self.payload)
        self.assertEqual(source_id + 1, returned["row"]["id"])
        self.assertEqual(2, self.collection.db.scalar("select count(*) from revlog"))

    def test_changed_deliveries_and_missing_applied_rows_require_resolution(self):
        returned = apply_review(self.collection, self.state, self.payload)
        changed = copy.deepcopy(self.payload)
        changed["rating"] = 4
        with self.assertRaises(ReviewConflict):
            apply_review(self.collection, self.state, changed)
        self.collection.db.execute("DELETE FROM revlog WHERE id = ?", returned["row"]["id"])
        with self.assertRaisesRegex(ReviewConflict, "removed"):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(0, self.collection.db.scalar("select count(*) from revlog"))

    def test_an_ankiweb_usn_change_does_not_change_the_reserved_event_identity(self):
        returned = apply_review(self.collection, self.state, self.payload)
        self.collection.db.execute("UPDATE revlog SET usn = 17 WHERE id = ?", returned["row"]["id"])
        replay = apply_review(self.collection, self.state, self.payload)
        self.assertEqual(returned["row"]["id"], replay["row"]["id"])
        self.assertEqual(17, replay["row"]["usn"])
        self.assertEqual(1, self.collection.db.scalar("select count(*) from revlog"))

    def test_version_and_live_schema_are_checked_before_reserving_or_writing(self):
        before = card_row(self.collection, self.card.id)
        with patch("anki.buildinfo.version", "unsupported"):
            with self.assertRaises(ReviewConflict):
                apply_review(self.collection, self.state, self.payload)
        self.collection.db.execute("ALTER TABLE revlog ADD COLUMN unexpected INTEGER")
        with self.assertRaisesRegex(ReviewConflict, "schema"):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertIsNone(self.state.review(self.payload["sync_id"]))

    def test_independent_card_edits_do_not_get_overwritten(self):
        card = self.collection.get_card(self.card.id)
        card.queue = -1
        self.collection.update_card(card)
        before = card_row(self.collection, self.card.id)
        with self.assertRaisesRegex(ReviewConflict, "independently"):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from revlog"))

    def test_an_accepted_review_schedule_allows_its_missing_history_without_rewriting_the_card(self):
        from shirabe_addon.sync_cards import apply_card_change
        from shirabe_addon.sync_snapshot import SyncSnapshot

        apply_card_change(self.collection, self.payload)
        source = {**self.payload["anki"], "schedule_review": {"sync_id": self.payload["sync_id"], "reviewed_at": self.payload["reviewed_at"]}}
        self.state.remember_checkpoint(*SyncSnapshot.review_checkpoint(self.card.id, "accepted", source))
        before = card_row(self.collection, self.card.id)
        apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_a_confirmed_reset_keeps_its_schedule_when_earlier_review_history_arrives(self):
        from shirabe_addon.sync_snapshot import SyncSnapshot

        before = card_row(self.collection, self.card.id)
        self.payload["card"] = {"reset_generation": 0}
        self.state.remember_checkpoint(*SyncSnapshot.review_checkpoint(self.card.id, "reset", {"card_after": before}, reset_generation=1))
        apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(1, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_a_later_native_change_invalidates_a_confirmed_reset_proof(self):
        from shirabe_addon.sync_snapshot import SyncSnapshot

        before = card_row(self.collection, self.card.id)
        self.payload["card"] = {"reset_generation": 0}
        self.state.remember_checkpoint(*SyncSnapshot.review_checkpoint(self.card.id, "reset", {"card_after": before}, reset_generation=1))
        self.card.reps = 5
        self.collection.update_card(self.card)
        changed = card_row(self.collection, self.card.id)
        with self.assertRaisesRegex(ReviewConflict, "independently"):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(changed, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_a_stale_accepted_schedule_does_not_authorize_overwriting_a_later_local_change(self):
        from shirabe_addon.sync_cards import apply_card_change
        from shirabe_addon.sync_snapshot import SyncSnapshot

        apply_card_change(self.collection, self.payload)
        source = {**self.payload["anki"], "schedule_review": {"sync_id": self.payload["sync_id"], "reviewed_at": self.payload["reviewed_at"]}}
        self.state.remember_checkpoint(*SyncSnapshot.review_checkpoint(self.card.id, "accepted", source))
        card = self.collection.get_card(self.card.id)
        card.due += 1
        self.collection.update_card(card)
        before = card_row(self.collection, self.card.id)
        with self.assertRaises(ReviewConflict):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("SELECT count(*) FROM revlog"))

    def test_public_api_preserves_local_custom_data_and_flags(self):
        card = self.collection.get_card(self.card.id)
        card.custom_data = '{"custom":true}'
        card.flags = 3
        self.collection.update_card(card)
        apply_review(self.collection, self.state, self.payload)
        stored = self.collection.get_card(self.card.id)
        self.assertEqual(3, stored.flags)
        self.assertEqual({"custom": True}, json.loads(stored.custom_data))

    def test_unrecognized_auxiliary_data_is_not_silently_discarded(self):
        raw = json.dumps({"foreign": "preserve"})
        self.collection.db.execute("UPDATE cards SET data = ? WHERE id = ?", raw, self.card.id)
        before = card_row(self.collection, self.card.id)
        with self.assertRaisesRegex(ReviewConflict, "preserve"):
            apply_review(self.collection, self.state, self.payload)
        self.assertEqual(before, card_row(self.collection, self.card.id))
        self.assertEqual(0, self.collection.db.scalar("select count(*) from revlog"))


class StoragePrecisionTest(unittest.TestCase):
    def test_only_storage_precision_is_tolerated_and_review_timestamps_stay_exact(self):
        left = {"data": '{"s":3.1235,"d":2.118,"lrt":1780000000}'}
        self.assertTrue(same_state(left, {"data": '{"s":3.12345,"d":2.1181,"lrt":1780000000}'}))
        self.assertFalse(same_state(left, {"data": '{"s":3.12,"d":2.118,"lrt":1780000000}'}))
        self.assertFalse(same_state(left, {"data": '{"s":3.1235,"d":2.118,"lrt":1780000001}'}))
