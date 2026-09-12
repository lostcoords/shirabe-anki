import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from offline_fixture import candidate, parse, prepared
from shirabe_addon import offline, offline_ui

try:
    from anki.collection import Collection
except ModuleNotFoundError as error:
    if error.name != "anki":
        raise
    Collection = None


def pinned_preparation(texts=("猫犬鳥",)):
    def pinned(text):
        answer = parse(text)
        for token in answer["tokens"]:
            token["entry"] = {"dictionary": "jmdict", "sourceId": token["surface"]}
        return answer

    return prepared(texts, parse=pinned,
                    identify=lambda query: {"words": [{"candidates": [candidate(query["surface"], query["surface"])]}]},
                    preferences={"languages": ["en", "es"], "dictionaryReveal": {"jmdict:en": "show"}},
                    sources=[{"slug": "jmdict", "name": "JMdict"}])


def partial(preparation, *indexes):
    snapshot = copy.deepcopy(preparation.snapshot)
    snapshot["complete"] = False
    for segment in snapshot["segments"]:
        segment["results"] = [result for result in segment["results"] if result["tokenIndex"] in indexes]
    return with_snapshot(preparation, snapshot)


def with_snapshot(preparation, snapshot):
    return replace(preparation, snapshot=snapshot, coverage=offline.coverage(snapshot),
                   bytes_used=len(offline._json(snapshot)), issues=("byte_limit",))


@unittest.skipIf(Collection is None, "Run with Anki's Python for native refresh persistence")
class OfflineRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="shirabe-offline-refresh-")
        self.addCleanup(self.directory.cleanup)
        self.col = Collection(str(Path(self.directory.name) / "collection.anki2"))
        self.addCleanup(self.col.close)
        self.full = pinned_preparation()

    def refresh(self, previous, proposed):
        before = copy.deepcopy((previous.snapshot, proposed.snapshot))
        first = offline.persist_snapshot(self.col.media, previous)
        stored = offline.persist_snapshot(self.col.media, proposed, previous_name=first.filename)
        self.assertEqual((previous.snapshot, proposed.snapshot), before)
        self.assertEqual(offline.read_snapshot(self.col.media, first.filename), previous.snapshot)
        for table in ("notes", "cards", "revlog"):
            self.assertEqual(self.col.db.scalar(f"select count(*) from {table}"), 0)
        return first, stored

    def test_partial_refresh_keeps_saved_definition_even_when_total_coverage_increases(self):
        previous, proposed = partial(self.full, 0), partial(self.full, 1, 2)
        first, stored = self.refresh(previous, proposed)
        self.assertTrue(stored.kept_previous)
        self.assertEqual(stored.filename, first.filename)
        self.assertEqual(stored.bytes_used, 0)
        self.assertEqual(offline.snapshot_storage(self.col.media).files, 1)

    def test_refresh_can_add_definitions_and_update_existing_glosses(self):
        previous, proposed = partial(self.full, 0), partial(self.full, 0, 1)
        proposed.snapshot["createdAt"] += 1000
        proposed.snapshot["segments"][0]["results"][0]["candidates"][0]["entries"][0]["senses"][0]["definitions"][0]["text"] = "updated cat"
        first, stored = self.refresh(previous, proposed)
        self.assertFalse(stored.kept_previous)
        self.assertNotEqual(stored.filename, first.filename)
        self.assertEqual(offline.read_snapshot(self.col.media, stored.filename), proposed.snapshot)

    def test_native_batch_reports_retention_and_preserves_note_fields_and_schedule(self):
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"], note["Back"] = "猫犬鳥", "my definition"
        self.col.add_note(note, 1)
        captured = offline_ui.capture_notes(self.col, [note.id])[0]
        initial = offline_ui.PreparedBatch(rows=[(captured, partial(self.full, 0))])
        offline_ui.apply_prepared(self.col, initial, {})
        before = list(self.col.get_note(note.id).fields)
        cards = self.col.db.all("select * from cards order by id")
        previous = offline_ui.capture_notes(self.col, [note.id])[0]
        batch = offline_ui.PreparedBatch(rows=[(previous, partial(self.full, 1, 2))])
        result = offline_ui.apply_prepared(self.col, batch, {})
        self.assertEqual((result.kept_previous, result.partial, result.bytes_used), (1, 1, 0))
        self.assertEqual(result.prepared, 1)
        self.assertEqual(list(self.col.get_note(note.id).fields), before)
        self.assertEqual(self.col.db.all("select * from cards order by id"), cards)
        self.assertEqual(self.col.db.scalar("select count(*) from revlog"), 0)

    def test_selected_definition_saved_as_another_tokens_alternative_is_protected(self):
        previous, proposed = partial(self.full, 0), partial(self.full, 0)
        previous.snapshot["segments"][0]["results"][0]["candidates"].append(
            copy.deepcopy(self.full.snapshot["segments"][0]["results"][1]["candidates"][0]))
        first, stored = self.refresh(previous, proposed)
        self.assertTrue(stored.kept_previous)
        self.assertEqual(stored.filename, first.filename)

    def test_same_segment_can_supply_saved_definition_from_a_different_result(self):
        previous, proposed = partial(self.full, 0), partial(self.full, 1)
        proposed.snapshot["segments"][0]["results"][0]["candidates"].append(
            copy.deepcopy(previous.snapshot["segments"][0]["results"][0]["candidates"][0]))
        first, stored = self.refresh(previous, proposed)
        self.assertFalse(stored.kept_previous)
        self.assertNotEqual(stored.filename, first.filename)

    def test_moving_definition_to_another_side_does_not_preserve_front_lookup(self):
        full = pinned_preparation(("猫", "犬"))
        previous, proposed = partial(full, 0), partial(full, 0)
        previous.snapshot["segments"][1]["results"] = []
        proposed.snapshot["segments"][1]["results"][0]["candidates"].append(
            copy.deepcopy(previous.snapshot["segments"][0]["results"][0]["candidates"][0]))
        proposed.snapshot["segments"][0]["results"] = []
        first, stored = self.refresh(previous, proposed)
        self.assertTrue(stored.kept_previous)
        self.assertEqual(stored.filename, first.filename)

    def test_support_from_another_visible_segment_is_not_lost(self):
        full = pinned_preparation(("猫", "犬", "鳥"))
        previous, proposed = partial(full, 0), partial(full, 0)
        cat = copy.deepcopy(full.snapshot["segments"][0]["results"][0]["candidates"][0])
        for item in (previous, proposed):
            item.snapshot["segments"][0]["results"] = []
        previous.snapshot["segments"][1]["results"][0]["candidates"].append(cat)
        proposed.snapshot["segments"][2]["results"][0]["candidates"].append(copy.deepcopy(cat))
        first, stored = self.refresh(previous, proposed)
        self.assertTrue(stored.kept_previous)
        self.assertEqual(stored.filename, first.filename)

    def test_matching_entry_id_with_wrong_surface_is_not_a_callable_definition(self):
        previous, proposed = partial(self.full, 0), partial(self.full, 1)
        wrong = copy.deepcopy(previous.snapshot["segments"][0]["results"][0]["candidates"][0])
        wrong["headword"] = "別"
        proposed.snapshot["segments"][0]["results"][0]["candidates"].append(wrong)
        first, stored = self.refresh(previous, proposed)
        self.assertTrue(stored.kept_previous)
        self.assertEqual(stored.filename, first.filename)

    def test_unpinned_alternative_loss_does_not_freeze_refresh(self):
        previous, proposed = partial(self.full, 0), partial(self.full, 1, 2)
        for item in (previous, proposed):
            del item.snapshot["segments"][0]["tokens"][0]["entry"]
        _, stored = self.refresh(previous, proposed)
        self.assertFalse(stored.kept_previous)

    def test_refresh_does_not_retain_data_across_changed_scope(self):
        mutations = {
            "version": lambda value: value.update(contentVersion="dictionary-v2"),
            "languages": lambda value: value["preferences"].update(languages=["es", "en"]),
            "reveal": lambda value: value["preferences"].update(dictionaryReveal={"jmdict:en": "hover"}),
            "approved sources": lambda value: value.update(sources=[]),
            "reading": lambda value: value["segments"][0]["tokens"][0].update(reading="ビョウ"),
            "pin": lambda value: value["segments"][0]["tokens"][0].update(entry="jmdict:other"),
            "lemma": lambda value: value["segments"][0]["tokens"][0].update(lemma="ねこ"),
            "pos": lambda value: value["segments"][0]["tokens"][0].update(pos="noun"),
            "inflection": lambda value: value["segments"][0]["tokens"][0].update(inflection=["past"], inflectionBase="猫"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                previous, proposed = partial(self.full, 0), partial(self.full, 1, 2)
                mutate(proposed.snapshot)
                _, stored = self.refresh(previous, proposed)
                self.assertFalse(stored.kept_previous)
                self.assertEqual(offline.read_snapshot(self.col.media, stored.filename), proposed.snapshot)
        _, stored = self.refresh(partial(self.full, 0), partial(pinned_preparation(("犬鳥",)), 0))
        self.assertFalse(stored.kept_previous)


if __name__ == "__main__":
    unittest.main()
