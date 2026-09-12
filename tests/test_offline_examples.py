import base64
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import support
from check_against_anki import anki_python
from offline_fixture import candidate, identify, parse, prepared
from shirabe_addon import authoring_media, offline, offline_audio, offline_ui
from test_offline_audio import M4A, PreparationBackend


CORPUS = {"slug": "tatoeba", "name": "Tatoeba", "type": "sentence", "visibility": "official",
          "languages": ["en", "es", "ja"], "attribution": "Tatoeba (CC BY 2.0 FR)",
          "source": {"name": "Tatoeba (sense links: jmdict-simplified)", "url": "https://tatoeba.org",
                     "license": "CC BY 2.0 FR", "licenseUrl": "https://creativecommons.org/licenses/by/2.0/fr/"}}


def sentence(number=123, japanese="猫が好きです。"):
    return {"id": f"tatoeba:tatoeba_{number}", "japanese": japanese,
            "translations": [{"lang": "en", "text": "I like cats."}, {"lang": "es", "text": "Me gustan los gatos."}],
            "tokens": [{"surface": "猫", "study": "PRIVATE"}], "audioUrl": "https://private.invalid/secret"}


def response(*rows, more=False):
    return {"sentences": list(rows), "pagination": {"hasMore": more, "cursor": "private-cursor" if more else None}}


def candidates(snapshot):
    return [c for s in snapshot["segments"] for r in s["results"] for c in r["candidates"]]


def examples(snapshot):
    return [e for c in candidates(snapshot) for e in c.get("examples", [])]


class PreparedExamples(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fetch(self, word):
        self.calls.append(word["sourceId"])
        return response(sentence())

    def prepare(self, texts=("猫",), **options):
        return prepared(texts, examples=options.pop("examples", self.fetch),
                        sources=options.pop("sources", [CORPUS]),
                        preferences={"languages": ["es", "en"], "dictionaryReveal": {}}, **options)

    def test_default_off_ignores_unrequested_even_oversized_examples(self):
        raw = candidate()
        raw["examples"] = [{"japanese": "PRIVATE" * 100000}]
        item = prepared(("猫",), identify=lambda _: {"words": [{"candidates": [raw]}]})
        self.assertEqual(item.coverage.prepared, 1)
        self.assertTrue(item.complete)
        self.assertEqual(item.requests, 2)
        self.assertFalse(examples(item.snapshot))
        self.assertNotIn("PRIVATE", offline._json(item.snapshot).decode())

    def test_saves_frozen_sdk_sentence_shape_ordered_translation_and_license_links(self):
        item = self.prepare()
        self.assertTrue(item.complete, item.issues)
        self.assertEqual(item.requests, 3)
        self.assertEqual(examples(item.snapshot), [{"japanese": "猫が好きです。", "translations": [{"lang": "es", "text": "Me gustan los gatos."}]}])
        notice = next(row for row in item.snapshot["sources"] if row["slug"] == "tatoeba")
        self.assertEqual(notice["source"]["licenseUrl"], CORPUS["source"]["licenseUrl"])
        self.assertIn("https://tatoeba.org/en/sentences/show/123", notice["attribution"])
        self.assertIn("Tatoeba contributors", notice["attribution"])
        self.assertEqual(offline.validate_snapshot(item.snapshot), item.snapshot)
        self.assertNotIn("PRIVATE", offline._json(item.snapshot).decode())
        self.assertNotIn("private.invalid", offline._json(item.snapshot).decode())

    def test_requires_verified_official_corpus_metadata_before_any_request(self):
        for key, value in (("visibility", "personal"), ("type", "definition"), ("slug", "uploaded"), ("source", {})):
            with self.subTest(key=key):
                item = self.prepare(sources=[{**CORPUS, key: value}])
                self.assertIn("examples_unavailable", item.issues)
                self.assertEqual(item.requests, 2)
                self.assertFalse(examples(item.snapshot))
        for key, value in (("url", "https://private.invalid"), ("license", "Unknown"), ("licenseUrl", "http://creativecommons.org/licenses/by/2.0/fr/")):
            item = self.prepare(sources=[{**CORPUS, "source": {**CORPUS["source"], key: value}}])
            self.assertFalse(examples(item.snapshot))
        self.assertEqual(self.calls, [])

    def test_bad_or_private_sibling_never_blocks_valid_public_sentence(self):
        for bad in ({**sentence(), "id": "uploaded:123", "japanese": "PRIVATE" * 100000},
                    {**sentence(), "id": "tatoeba:../secret"}, {**sentence(), "japanese": "猫" * 513},
                    {**sentence(), "translations": "bad"}, None):
            with self.subTest(bad_type=type(bad)):
                item = self.prepare(examples=lambda _: response(bad, sentence(456)))
                self.assertEqual(len(examples(item.snapshot)), 1)
                self.assertIn("examples_unavailable", item.issues)
                self.assertEqual(item.coverage.prepared, 1)
                self.assertNotIn("PRIVATE", offline._json(item.snapshot).decode())

    def test_entry_note_request_and_byte_limits_never_drop_definitions(self):
        item = self.prepare(examples=lambda _: response(sentence(123), sentence(456), sentence(789)))
        self.assertFalse(examples(item.snapshot))
        self.assertIn("examples_limit", item.issues)
        item = self.prepare(("猫犬鳥魚馬牛羊豚虎",))
        self.assertLessEqual(len(examples(item.snapshot)), 8)
        self.assertLessEqual(len(self.calls), 8)
        self.assertIn("examples_limit", item.issues)
        self.assertEqual(item.coverage.prepared, 9)
        for maximum in (2, 3):
            item = self.prepare(budget=offline.Budget(max_requests=maximum))
            self.assertEqual(item.requests, maximum)
            self.assertEqual(bool(examples(item.snapshot)), maximum == 3)
            self.assertEqual(item.coverage.prepared, 1)
        baseline = prepared(("猫",), preferences={"languages": ["es", "en"], "dictionaryReveal": {}})
        item = self.prepare(budget=offline.Budget(max_bytes=baseline.bytes_used + 20))
        self.assertFalse(examples(item.snapshot))
        self.assertLessEqual(item.bytes_used, baseline.bytes_used + 20)
        self.assertEqual(item.coverage.prepared, 1)
        self.assertIn("examples_limit", item.issues)

    def test_distinct_entry_cache_and_no_pagination_or_fallback(self):
        item = self.prepare(("猫猫",))
        self.assertEqual(self.calls, ["1"])
        self.assertEqual(len(examples(item.snapshot)), 2)
        self.assertEqual(item.requests, 4)
        empty = self.prepare(examples=lambda _: response())
        self.assertTrue(empty.complete, empty.issues)
        unavailable = self.prepare(examples=lambda _: (_ for _ in ()).throw(OSError("secret")))
        self.assertIn("examples_unavailable", unavailable.issues)
        self.assertEqual(unavailable.coverage.prepared, 1)

    def test_requested_example_page_is_complete_even_when_corpus_has_more(self):
        def fetch(word):
            self.calls.append(word["sourceId"])
            return response(sentence(), sentence(456, "猫がいます。"), more=True)
        item = self.prepare(examples=fetch)
        self.assertTrue(item.complete, item.issues)
        self.assertEqual(len(examples(item.snapshot)), 2)
        self.assertEqual(item.requests, 3)
        self.assertEqual(self.calls, ["1"])

    def test_prior_example_disappearance_marks_refresh_partial(self):
        previous = self.prepare(static_text_only=True)
        item = self.prepare(previous=previous.snapshot, examples=lambda _: response())
        self.assertIn("examples_unavailable", item.issues)
        self.assertFalse(item.complete)

    def test_batch_default_off_and_explicit_selected_dictionary_calls(self):
        backend = offline.DictionaryBackend("https://fixture.invalid", json.dumps({"access": "fixture", "expiresAt": 2000}),
                                            now=lambda: 1, locale="es", dictionaries=["jmdict"])
        calls = []
        def request(path, fields=None):
            calls.append((path, fields))
            if path.startswith("/parse?"):
                return parse(fields["text"])
            if path.startswith("/words/identify?"):
                return identify(fields["tokens"][0])
            self.assertTrue(path.startswith("/sentences?"), path)
            self.assertIsNone(fields)
            self.assertEqual(parse_qs(urlsplit(path).query), {"entry": ["jmdict:1"], "query": ["猫"], "take": ["2"], "locale": ["es"]})
            return response(sentence())
        backend._request = request
        note = offline_ui.CapturedNote(1, "revision", (), None, None)
        preferences = {"languages": ["es", "en"], "dictionaries": ["jmdict:es"], "uiLocale": "es", "stackFingerprint": "v1"}
        options = dict(preferences=preferences, approved={"jmdict"}, sources=[CORPUS])
        for enabled in (False, True):
            calls.clear()
            batch = offline_ui.prepare_batch([note], {1: ["猫"]}, backend, include_examples=enabled, **options)
            self.assertEqual(len(batch.rows), 1)
            self.assertEqual(len(calls), 3 if enabled else 2)
            self.assertTrue(all(fields["dictionaries"] == ["jmdict"] for _, fields in calls if fields))
            self.assertEqual(bool(examples(batch.rows[0][1].snapshot)), enabled)

    def test_sentence_response_header_is_bounded_before_body_read(self):
        class TooLarge(io.BytesIO):
            headers = {"Content-Length": str(offline.MAX_RESPONSE_BYTES + 1)}
            def read(self, *args):
                raise AssertionError("Oversized response body must not be read")
        backend = offline.DictionaryBackend("https://fixture.invalid", json.dumps({"access": "fixture", "expiresAt": 2000}), now=lambda: 1)
        with patch.object(backend._opener, "open", return_value=TooLarge()):
            with self.assertRaises(offline.SnapshotError) as caught:
                backend.examples(candidate())
        self.assertEqual(caught.exception.code, "response_limit")

    def test_example_byte_allowance_and_entry_cap_include_notices(self):
        def long_examples(_):
            row = sentence(japanese="猫" * 512)
            row["translations"] = [{"lang": "es", "text": "猫" * 512}]
            return response(row)
        def unique(token):
            return {"words": [{"candidates": [candidate(token["lemma"], token["lemma"])]}]}
        base = prepared(("猫犬鳥魚馬",), identify=unique, preferences={"languages": ["es", "en"], "dictionaryReveal": {}})
        item = self.prepare(("猫犬鳥魚馬",), identify=unique, examples=long_examples)
        self.assertGreater(len(examples(item.snapshot)), 0)
        self.assertLess(item.bytes_used - base.bytes_used, 8 * 1024)
        self.assertIn("examples_limit", item.issues)
        raw = candidate()
        senses = raw["entries"][0]["senses"]
        senses[0]["definitions"] = [{"lang": "en", "text": "a" * 4000} for _ in range(6)]
        item = self.prepare(identify=lambda _: {"words": [{"candidates": [raw]}]}, examples=long_examples)
        self.assertEqual(item.coverage.prepared, 1)
        self.assertLessEqual(len(offline._json(candidates(item.snapshot)[0])), offline.MAX_ENTRY_BYTES)
        self.assertFalse(examples(item.snapshot))
        self.assertIn("examples_limit", item.issues)

    def test_audio_priority_and_shared_requests_and_batch_byte_budget(self):
        backend = PreparationBackend()
        def download(selection, **options):
            backend.calls.append("download")
            return authoring_media.DownloadedMedia(M4A, "m4a", selection.attribution, selection.license)
        def fetch(word):
            backend.calls.append("examples")
            return response(sentence())
        for maximum in (4, 5):
            backend.calls.clear()
            audio = offline_audio.AudioBatch(backend, offline.MAX_TOTAL_BYTES, download=download)
            item = self.prepare(audio=audio, parse=backend.parse, identify=backend.identify,
                                examples=fetch, budget=offline.Budget(max_requests=maximum))
            self.assertEqual(item.requests, maximum)
            self.assertEqual(backend.calls, ["parse", "identify", "metadata", "download"] + (["examples"] if maximum == 5 else []))
            self.assertEqual(bool(examples(item.snapshot)), maximum == 5)
            self.assertEqual(len(item.media), 1)
        backend.examples = fetch
        notes = [offline_ui.CapturedNote(i, "revision", (), None, None) for i in range(2)]
        prefs = {"languages": ["es"], "dictionaryReveal": {}, "uiLocale": "es", "dictionaries": ["jmdict"], "stackFingerprint": "v1"}
        with patch.object(offline, "MAX_TOTAL_BYTES", 3000), patch.object(authoring_media, "download_selected_media", side_effect=download):
            batch = offline_ui.prepare_batch(notes, {0: ["猫"], 1: ["猫"]}, backend, preferences=prefs, approved={"jmdict"},
                                             sources=[CORPUS], include_audio=True, include_examples=True)
        self.assertTrue(batch.rows)
        files = {name: media for _, item in batch.rows for name, media in item.media.items()}
        self.assertLessEqual(sum(item.bytes_used for _, item in batch.rows) + sum(len(media.data) for media in files.values()), 3000)

    def test_cancellation_after_sentence_request_does_not_process_or_save_its_response(self):
        cancelled = False
        def fetch(word):
            nonlocal cancelled
            cancelled = True
            return response(sentence())
        item = self.prepare(examples=fetch, cancelled=lambda: cancelled)
        self.assertEqual(item.requests, 3)
        self.assertIn("cancelled", item.issues)
        self.assertFalse(examples(item.snapshot))

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_actual_anki_export_import_budgets_partial_refresh_and_missing_media(self):
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("offline_examples_storage_probe.py"))],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 2 tests", result.stderr)

    def test_frozen_sdk_bundle_displays_examples_with_preferred_translation_without_network(self):
        result = subprocess.run(["node", str(Path(__file__).with_name("offline_examples_sdk_probe.mjs"))],
                                input=json.dumps({"snapshot": self.prepare().snapshot}), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["offlineExamples"])

    def test_frozen_sdk_reads_the_exact_compressed_python_media_with_saved_examples(self):
        snapshot = self.prepare().snapshot
        filename, data = offline.snapshot_file(snapshot)
        self.assertTrue(filename.startswith("_shirabe_snapshot_v2_"))
        payload = {"snapshot": snapshot, "media": {"filename": filename, "base64": base64.b64encode(data).decode()}}
        result = subprocess.run(["node", str(Path(__file__).with_name("offline_examples_sdk_probe.mjs"))],
                                input=json.dumps(payload), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["snapshotBytes"], len(data))


if __name__ == "__main__":
    unittest.main()
