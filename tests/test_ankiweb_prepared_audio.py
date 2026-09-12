import copy
import unittest

import support
from ankiweb_prepared_audio_probe import payload_sizes, prepare_cat
from offline_fixture import prepared
from shirabe_addon import authoring_media, offline, offline_audio
from test_offline_audio import M4A
from test_offline_examples import CORPUS, response, sentence


class AnkiWebPreparedFixture(unittest.TestCase):
    def setUp(self):
        self.original = prepared(("\n猫がいます。\n",)).snapshot
        self.recording = authoring_media.Recording("https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a", "ねこ", 1, "HL")
        self.downloaded = authoring_media.DownloadedMedia(M4A, "m4a", "Shirabe", authoring_media.ANKI_PERMISSION)
        self.fixture = {"corpus": CORPUS, "response": response(sentence(), sentence(456, "猫がいます。"), more=True)}

    def test_public_examples_use_production_preparation_with_one_cat_token_and_audio(self):
        before = copy.deepcopy(self.original)
        item = prepare_cat(self.original, self.recording, self.downloaded, self.fixture)
        self.assertTrue(item.complete)
        self.assertEqual(item.requests, 5)
        self.assertEqual(item.coverage.prepared, 1)
        segment = item.snapshot["segments"][0]
        self.assertEqual(segment["text"], "\n猫がいます。\n")
        self.assertEqual([(t["surface"], t["start"]) for t in segment["tokens"]], [("猫", 1)])
        self.assertEqual(len(segment["results"][0]["candidates"][0]["examples"]), 2)
        self.assertEqual(len(offline_audio.recordings(item.snapshot)), 1)
        self.assertEqual(self.original, before)
        self.assertEqual(offline.validate_snapshot(item.snapshot), item.snapshot)

    def test_size_accounting_counts_attribution_utf8_references_and_unique_audio(self):
        item = prepare_cat(self.original, self.recording, self.downloaded, self.fixture)
        reference = offline.snapshot_field("_shirabe_snapshot_v1_" + "a" * 64 + ".json", tuple(item.media))
        sizes = payload_sizes(item, reference)
        self.assertEqual(sizes["examples"], 2)
        self.assertEqual(sizes["snapshot_json_bytes"], len(offline._json(item.snapshot)))
        self.assertEqual(sizes["snapshot_media_bytes"], len(offline.snapshot_file(item.snapshot)[1]))
        self.assertEqual(sizes["pronunciation_bytes"], len(M4A))
        self.assertEqual(sizes["snapshot_reference_bytes"], len(reference.encode("utf-8")))
        self.assertEqual(sizes["payload_with_reference_bytes"], sizes["snapshot_media_bytes"] + len(M4A) + len(reference.encode("utf-8")))
        self.assertGreater(sizes["examples_and_attribution_bytes"], 0)
        self.assertLessEqual(sizes["examples_and_attribution_bytes"], offline.MAX_EXAMPLE_BYTES)

    def test_audio_only_stays_available_without_public_example_fixture(self):
        item = prepare_cat(self.original, self.recording, self.downloaded)
        self.assertEqual(item.requests, 4)
        self.assertEqual(payload_sizes(item, "")["examples_and_attribution_bytes"], 0)

    def test_incomplete_or_unverified_example_fixture_is_rejected(self):
        for corpus, answer in (({**CORPUS, "visibility": "personal"}, self.fixture["response"]),
                               (CORPUS, response()), (CORPUS, response({**sentence(), "id": "private:1"}))):
            with self.subTest(corpus=corpus["visibility"], answer=answer):
                with self.assertRaises(ValueError):
                    prepare_cat(self.original, self.recording, self.downloaded, {"corpus": corpus, "response": answer})


if __name__ == "__main__":
    unittest.main()
