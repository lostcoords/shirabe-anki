import unittest

import support
from test_authoring import Note, lookup, profile

from shirabe_addon.authoring import AuthoringError, Lookup, NoteSnapshot, escaped_text
from shirabe_addon.authoring_batch import Batch, BatchRow, LookupIdentity


def row(nid, expression="猫"):
    note = Note(note_id=nid)
    return BatchRow(NoteSnapshot.capture(note), note.note_type(), profile(), LookupIdentity(expression))


class EnrichmentBatch(unittest.TestCase):
    def test_duplicate_identities_share_one_request_but_remain_separate_notes(self):
        batch = Batch()
        batch.rows = [row(1), row(2)]
        calls = []
        batch.prepare(lambda identity: calls.append(identity) or [lookup()])
        self.assertEqual(len(calls), 1)
        self.assertEqual([r.status for r in batch.rows], ["ready", "ready"])
        self.assertEqual([r.snapshot.note_id for r in batch.rows], [1, 2])
        self.assertEqual(vars(calls[0]), {"expression": "猫", "reading": "", "entry": ""})

    def test_multiple_entries_wait_for_an_explicit_choice(self):
        batch = Batch()
        batch.rows = [row(1)]
        batch.prepare(lambda _: [lookup(), lookup(entry="jmdict:other")])
        self.assertEqual(batch.rows[0].status, "ambiguous")
        self.assertIsNone(batch.rows[0].selected)
        batch.rows[0].choose(1)
        self.assertEqual(batch.rows[0].status, "ready")
        self.assertEqual(batch.rows[0].candidates[batch.rows[0].selected].entry, "jmdict:other")

    def test_cancel_stops_the_next_request_and_resume_reuses_prepared_results(self):
        batch = Batch()
        batch.rows = [row(1), row(2, "犬")]
        calls = []

        def resolve(identity):
            calls.append(identity)
            batch.cancel()
            return [lookup()]

        batch.prepare(resolve)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r.status for r in batch.rows], ["cancelled", "cancelled"])
        batch.resume()
        batch.prepare(lambda identity: calls.append(identity) or [lookup()])
        self.assertEqual(len(calls), 2)
        self.assertEqual([r.status for r in batch.rows], ["ready", "ready"])

    def test_retry_does_not_repeat_successful_lookups(self):
        batch = Batch()
        batch.rows = [row(1), row(2, "犬")]

        def resolve(identity):
            if identity.expression == "犬":
                raise OSError("failed")
            return [lookup()]

        batch.prepare(resolve)
        self.assertEqual([r.status for r in batch.rows], ["ready", "failed"])
        retried = []
        batch.prepare(lambda identity: retried.append(identity.expression) or [])
        self.assertEqual(retried, ["犬"])
        self.assertEqual([r.status for r in batch.rows], ["ready", "unmatched"])

    def test_entry_identity_is_not_replaced_by_the_first_spelling_match(self):
        batch = Batch()
        target = row(1)
        target.identity = LookupIdentity("生", "せい", "jmdict:selected")
        batch.rows = [target]
        batch.prepare(lambda _: [Lookup(lemma="生", reading="なま", entry="jmdict:other")])
        self.assertEqual(target.status, "unmatched")

    def test_capture_refuses_a_whole_collection_sized_request_before_reading(self):
        with self.assertRaises(AuthoringError):
            Batch().capture(None, list(range(1, 102)), [])

    def test_choosing_a_profile_moves_an_ambiguous_mapping_to_a_lookup(self):
        note = Note({"Expression": "猫", "Reading": "ネコ", "Other": "never send this"})
        target = BatchRow(NoteSnapshot.capture(note), note.note_type(), status="choose-profile")
        target.set_profile(profile())
        self.assertEqual(target.status, "pending")
        self.assertEqual(target.identity, LookupIdentity("猫", "ねこ", ""))
        self.assertNotIn("never send this", repr(target.identity))

    def test_mapped_entry_keeps_opaque_source_id_characters_and_spacing(self):
        entry = "jmdict:語:Ａ/猫?x=1&y=2 "
        note = Note({"Expression": "猫", "Entry": escaped_text(entry)})
        target = BatchRow(NoteSnapshot.capture(note), note.note_type())
        target.set_profile(profile())
        self.assertEqual(target.identity.entry, entry)


if __name__ == "__main__":
    unittest.main()
