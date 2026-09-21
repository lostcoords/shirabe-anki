import hashlib
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import support
from check_against_anki import anki_python
from offline_fixture import candidate, identify, parse, prepared
from shirabe_addon import authoring_media, offline, offline_audio, offline_ui


M4A = b"\x00\x00\x00\x1cftypM4A \x00\x00\x02\x00M4A isomiso2\x00\x00\x00\x0cmdatAAC!"
PERMISSION = "Permitted for Anki deck synchronization and export"


def recording(data=M4A, extension="m4a"):
    digest = hashlib.sha256(data).hexdigest()
    return {"filename": f"shirabe_{digest}.{extension}", "sha256": digest, "bytes": len(data),
            "mimeType": "audio/mp4" if extension == "m4a" else "audio/mpeg",
            "attribution": "Shirabe", "license": PERMISSION}


def first_candidate(snapshot):
    return snapshot["segments"][0]["results"][0]["candidates"][0]


class RecordingContract(unittest.TestCase):
    def test_recording_survives_validation_without_remote_urls(self):
        snapshot = prepared(("猫",)).snapshot
        first_candidate(snapshot)["pitch"][0]["recording"] = recording()
        clean = offline.validate_snapshot(snapshot)
        self.assertEqual(clean, snapshot)
        self.assertNotIn("audioUrl", first_candidate(clean)["pitch"][0])
        largest = {**recording(), "bytes": offline_audio.MAX_CLIP_BYTES}
        self.assertLess(len(offline._json({"recording": largest})), offline_audio.RECORDING_METADATA_BYTES)

    def test_invalid_recording_does_not_discard_its_definition(self):
        for field, value in (("filename", "../voice.m4a"), ("sha256", "0" * 64),
                             ("bytes", True), ("bytes", 1048577), ("mimeType", "audio/mpeg"),
                             ("attribution", "Someone else"), ("license", "CC0")):
            with self.subTest(field=field):
                snapshot = prepared(("猫",)).snapshot
                bad = {**recording(), field: value}
                first_candidate(snapshot)["pitch"][0]["recording"] = bad
                clean = offline.validate_snapshot(snapshot)
                self.assertEqual(offline.coverage(clean).prepared, 1)
                self.assertNotIn("recording", first_candidate(clean)["pitch"][0])

    def test_recording_file_and_byte_limits_are_per_snapshot_and_deduplicated(self):
        snapshot = prepared(("猫犬",)).snapshot
        word = first_candidate(snapshot)
        word["pitch"] = [{"downstep": i, "pattern": "H", "recording": recording(M4A + bytes([i]))} for i in range(20)]
        clean = offline.validate_snapshot(snapshot)
        self.assertEqual(len(offline_audio.recordings(clean)), 0)
        for pitch in word["pitch"]:
            pitch["recording"]["bytes"] = offline_audio.MAX_CLIP_BYTES
        clean = offline.validate_snapshot(snapshot)
        self.assertEqual(len(offline_audio.recordings(clean)), 0)
        for pitch in word["pitch"]:
            pitch["recording"] = recording()
        clean = offline.validate_snapshot(snapshot)
        self.assertEqual(len(offline_audio.recordings(clean)), 1)
        self.assertEqual(len(first_candidate(clean)["pitch"]), 20)
        word["pitch"][1]["recording"] = {**recording(), "bytes": 1}
        self.assertFalse(offline_audio.recordings(offline.validate_snapshot(snapshot)))

    def test_mp3_is_valid_transport_but_not_native_download_permission(self):
        snapshot = prepared(("猫",)).snapshot
        first_candidate(snapshot)["pitch"][0]["recording"] = recording(b"ID3test", "mp3")
        self.assertEqual(offline.validate_snapshot(snapshot), snapshot)

    def test_hidden_audio_reference_decodes_to_snapshot_filename(self):
        name = "_shirabe_snapshot_v1_" + "a" * 64 + ".json"
        field = offline.snapshot_field(name, [recording()["filename"]])
        self.assertIn('<audio src="' + recording()["filename"], field)
        self.assertNotIn("[sound:", field)
        self.assertEqual(offline.snapshot_filename(field), name)
        for bad in (field.replace('preload="none"', 'autoplay="true"'),
                    field.replace('<audio src="', '<audio src="https://evil.test/'),
                    field + '<audio src="bad.m4a"></audio>'):
            self.assertIsNone(offline.snapshot_filename(bad))


class PreparationBackend:
    def __init__(self):
        self.calls = []

    def check_access(self):
        pass

    def parse(self, text):
        self.calls.append("parse")
        return parse(text)

    def identify(self, token):
        self.calls.append("identify")
        return identify(token)

    def recordings(self, candidate):
        self.calls.append("metadata")
        return (authoring_media.Recording("https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a", "ねこ", 1, "HL"),)


class AudioPreparation(unittest.TestCase):
    def setUp(self):
        self.backend = PreparationBackend()
        self.downloads = []

    def download(self, selection, **options):
        self.downloads.append((selection, options))
        self.backend.calls.append("download")
        return authoring_media.DownloadedMedia(M4A, "m4a", selection.attribution, selection.license)

    def prepare(self, texts=("猫",), **options):
        audio = options.pop("audio", offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES, download=self.download))
        return prepared(texts, parse=self.backend.parse, identify=self.backend.identify, audio=audio, **options)

    def test_default_off_does_not_fetch_metadata_or_download_or_trust_api_local_copies(self):
        raw = candidate()
        raw["pitch"][0]["recording"] = recording()
        result = prepared(("猫",), identify=lambda _: {"words": [{"candidates": [raw]}]})
        self.assertEqual(result.requests, 2)
        self.assertFalse(result.media)
        self.assertNotIn("recording", first_candidate(result.snapshot)["pitch"][0])
        notes = [offline_ui.CapturedNote(1, "revision", (), None, None)]
        prefs = {"stackFingerprint": "v1", "dictionaries": ["jmdict:en"], "languages": ["en"], "uiLocale": "en"}
        offline_ui.prepare_batch(notes, {1: ["猫"]}, self.backend, preferences=prefs, approved={"jmdict"})
        self.assertEqual(self.backend.calls, ["parse", "identify"])

    def test_actual_shared_request_budget_and_cached_batch_download(self):
        audio = offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES, download=self.download)
        first = self.prepare(("猫猫",), audio=audio)
        second = self.prepare(("猫",), audio=audio)
        self.assertEqual((first.requests, second.requests), (5, 2))
        self.assertEqual(len(self.backend.calls), first.requests + second.requests)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.downloads[0][1]["max_bytes"], 1048576)
        self.assertEqual(self.downloads[0][1]["allowed_hosts"], {"cdn.shirabe.org"})
        self.assertEqual(first.media, second.media)
        self.assertEqual(offline_audio.recordings(first.snapshot), {recording()["filename"]: recording()})
        self.assertTrue(first.complete)

    def test_preparation_prefers_the_licensed_mp3_copy_when_available(self):
        url = "https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a"
        self.backend.recordings = lambda _: (authoring_media.Recording(
            url, "ねこ", 1, "HL", download_url=url[:-4] + ".mp3"),)

        def download(selection, **options):
            self.downloads.append((selection, options))
            return authoring_media.DownloadedMedia(b"ID3test", "mp3", selection.attribution, selection.license)

        result = self.prepare(audio=offline_audio.AudioBatch(
            self.backend, offline.MAX_TOTAL_BYTES, download=download))
        saved = next(iter(offline_audio.recordings(result.snapshot).values()))
        self.assertEqual(self.downloads[0][0].url, url[:-4] + ".mp3")
        self.assertEqual(saved["mimeType"], "audio/mpeg")
        self.assertTrue(saved["filename"].endswith(".mp3"))

    def test_distinct_recording_urls_with_identical_bytes_share_one_cached_file_charge(self):
        def recordings(word):
            return (authoring_media.Recording("https://cdn.shirabe.org/audio/words/" + word["sourceId"] * 64 + ".m4a", "ねこ", 1, "HL"),)
        self.backend.recordings = recordings
        audio = offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES, download=self.download)
        result = self.prepare(("猫犬",), audio=audio)
        self.assertEqual(len(self.downloads), 2)
        self.assertEqual(len(result.media), 1)
        self.assertEqual(audio.remaining_bytes, offline.MAX_TOTAL_BYTES - len(M4A))
        self.assertEqual(len(audio.files), 1)

    def test_successful_metadata_response_that_lost_previous_audio_marks_failed_refresh(self):
        prior = self.prepare().snapshot
        self.backend.recordings = lambda _: ()
        result = self.prepare(previous=prior)
        self.assertFalse(result.complete)
        self.assertIn("audio_unavailable", result.issues)
        self.assertEqual(result.coverage.prepared, 1)
        self.backend.identify = lambda _: (_ for _ in ()).throw(OSError("lookup failed"))
        reused = self.prepare(previous=prior)
        self.assertIn("audio_unavailable", reused.issues)
        self.assertEqual(reused.coverage.prepared, 1)

    def test_recording_requests_cannot_exceed_parser_identification_budget(self):
        for budget, calls in ((2, ["parse", "identify"]), (3, ["parse", "identify", "metadata"]),
                              (4, ["parse", "identify", "metadata", "download"])):
            self.backend.calls.clear()
            result = self.prepare(budget=offline.Budget(max_requests=budget))
            self.assertEqual(result.requests, budget)
            self.assertEqual(self.backend.calls, calls)
            self.assertEqual(result.coverage.prepared, 1)
            self.assertEqual("request_limit" in result.issues, budget < 4)

    def test_tight_batch_budget_is_passed_to_downloader_before_any_file_is_kept(self):
        seen = []
        def download(selection, **options):
            seen.append(options["max_bytes"])
            raise OSError("clip too large")
        audio = offline_audio.AudioBatch(self.backend, 2000, download=download)
        result = self.prepare(audio=audio)
        self.assertEqual(len(seen), 1)
        self.assertTrue(0 < seen[0] < 2000)
        self.assertFalse(result.media)
        self.assertFalse(result.complete)

    def test_native_unique_file_and_audio_byte_limits_stop_before_extra_downloads(self):
        word = candidate()
        word["pitch"] = [{"downstep": i, "pattern": "H"} for i in range(17)]
        self.backend.identify = lambda _: {"words": [{"candidates": [word]}]}
        self.backend.recordings = lambda _: tuple(authoring_media.Recording(
            f"https://cdn.shirabe.org/audio/words/{i:064x}.m4a", "ねこ", i, "H") for i in range(17))
        for clip_size, expected in ((len(M4A) + 1, 16), (offline_audio.MAX_CLIP_BYTES, 4)):
            calls = []
            def download(selection, **options):
                calls.append(selection.url)
                data = M4A + bytes(clip_size - len(M4A) - 1) + bytes([len(calls)])
                self.assertLessEqual(len(data), options["max_bytes"])
                return authoring_media.DownloadedMedia(data, "m4a", "Shirabe", PERMISSION)
            audio = offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES, download=download)
            result = self.prepare(audio=audio)
            self.assertEqual(len(calls), expected)
            self.assertEqual(result.requests, 3 + expected)
            self.assertEqual(len(result.media), expected)
            self.assertEqual(sum(len(item.data) for item in result.media.values()), clip_size * expected)
            self.assertIn("audio_limit", result.issues)
            self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)

    def test_invalid_batch_budgets_are_rejected_without_network_work(self):
        for size in (-1, True, offline.MAX_TOTAL_BYTES + 1):
            with self.assertRaises(offline.SnapshotError):
                offline_audio.AudioBatch(self.backend, size)
        self.assertEqual(self.backend.calls, [])

    def test_batch_cache_counts_audio_and_json_against_one_batch_limit(self):
        notes = [offline_ui.CapturedNote(i, "revision", (), None, None) for i in range(3)]
        prefs = {"stackFingerprint": "v1", "dictionaries": ["jmdict:en"], "languages": ["en"], "uiLocale": "en"}
        with patch.object(authoring_media, "download_selected_media", side_effect=self.download):
            batch = offline_ui.prepare_batch(notes, {i: ["猫"] for i in range(3)}, self.backend,
                                            preferences=prefs, approved={"jmdict"}, include_audio=True)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual([item.requests for _, item in batch.rows], [4, 1, 1])
        self.assertTrue(all(item.media for _, item in batch.rows))
        limit = batch.rows[0][1].bytes_used * 2 + len(M4A)
        self.downloads.clear()
        with patch.object(offline, "MAX_TOTAL_BYTES", limit), patch.object(authoring_media, "download_selected_media", side_effect=self.download):
            limited = offline_ui.prepare_batch(notes, {i: ["猫"] for i in range(3)}, self.backend,
                                              preferences=prefs, approved={"jmdict"}, include_audio=True)
        self.assertEqual(len(limited.rows), 2)
        self.assertTrue(limited.stopped)
        self.assertEqual(sum(item.bytes_used for _, item in limited.rows) + len(M4A), limit)
        self.assertEqual(len(self.downloads), 1)

    def test_cancellation_and_bad_download_keep_definitions_without_local_audio(self):
        def cancel_download(selection, **options):
            raise OSError("cancelled secret URL")
        audio = offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES, download=cancel_download)
        result = self.prepare(audio=audio)
        self.assertEqual(result.issues, ("audio_unavailable",))
        self.assertFalse(result.media)
        self.assertEqual(result.coverage.prepared, 1)
        self.assertNotIn("secret", repr(result))
        for data, extension in ((b"ID3test", "mp3"), (M4A + bytes(1048576), "m4a")):
            audio = offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES,
                download=lambda selection, **_: authoring_media.DownloadedMedia(data, extension, "Shirabe", PERMISSION))
            self.assertFalse(self.prepare(audio=audio).media)
        cancelled = self.prepare(cancelled=lambda: "metadata" in self.backend.calls)
        self.assertFalse(cancelled.media)
        self.assertIn("cancelled", cancelled.issues)

    def test_real_downloader_refuses_oversized_clip_headers_without_reading(self):
        class Response(io.BytesIO):
            headers = {"Content-Type": "audio/mp4", "Content-Length": "1048577"}
            def read(self, *_):
                raise AssertionError("Do not read the oversized body")
            read1 = read
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()
        opener = Opener()
        audio = offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES,
            download=lambda selection, **opts: authoring_media.download_selected_media(selection, opener=opener, **opts))
        result = self.prepare(audio=audio)
        self.assertEqual(result.coverage.prepared, 1)
        self.assertFalse(result.media)
        self.assertIsNone(opener.request.get_header("Authorization"))

    def test_cancelling_real_stream_leaves_no_media_and_counts_the_download_request(self):
        stopped = [False]
        class Response(io.BytesIO):
            headers = {"Content-Type": "audio/mp4"}
            def read1(self, size):
                stopped[0] = True
                return super().read(min(size, 16))
        class Opener:
            def open(self, *_args, **_kwargs):
                return Response(M4A)
        audio = offline_audio.AudioBatch(self.backend, offline.MAX_TOTAL_BYTES,
            download=lambda selection, **opts: authoring_media.download_selected_media(selection, opener=Opener(), **opts))
        result = self.prepare(audio=audio, cancelled=lambda: stopped[0])
        self.assertEqual(result.requests, 4)
        self.assertEqual(result.coverage.prepared, 1)
        self.assertFalse(result.media)
        self.assertIn("cancelled", result.issues)

    def test_audio_metadata_does_not_push_entries_or_snapshot_over_their_json_caps(self):
        original = candidate()
        original["entries"][0]["senses"][0]["definitions"] = [{"lang": "en", "text": "x" * 3900} for _ in range(6)]
        self.backend.identify = lambda _: {"words": [{"candidates": [original]}]}
        baseline = prepared(("猫",), identify=self.backend.identify)
        self.assertTrue(baseline.complete)
        result = self.prepare(budget=offline.Budget(max_bytes=baseline.bytes_used + 50))
        self.assertFalse(result.media)
        self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)
        self.assertLessEqual(result.bytes_used, baseline.bytes_used + 50)
        self.assertIn("byte_limit", result.issues)


class RefreshedMetadata(unittest.TestCase):
    def test_fresh_pinned_identity_and_jmdict_only_selector(self):
        backend = offline.DictionaryBackend("https://shirabe.org", "{}", locale="es", dictionaries=["uploaded", "jmdict"])
        detail = candidate()
        detail["pitch"][0]["audioUrl"] = "https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a"
        detail["pitch"][0]["audioDownloadUrl"] = detail["pitch"][0]["audioUrl"][:-4] + ".mp3"
        with patch.object(backend, "_request", return_value={"words": [{"candidates": [detail]}]}) as request:
            rows = backend.recordings(candidate())
        self.assertEqual(len(rows), 1)
        self.assertEqual(request.call_args.args, ("/words/identify", {
            "tokens": [{"lemma": "猫", "surface": "猫", "reading": "ネコ"}],
            "include": ["pitch"], "dictionaries": ["jmdict"],
        }))
        self.assertEqual(rows[0].reading, "ねこ")
        self.assertEqual(rows[0].download_url, detail["pitch"][0]["audioDownloadUrl"])
        for field, value in (("dictionary", "uploaded"), ("sourceId", "other"), ("reading", "イヌ")):
            with patch.object(backend, "_request", return_value={"words": [{"candidates": [{**detail, field: value}]}]}), self.assertRaises(offline.SnapshotError):
                backend.recordings(candidate())
        with patch.object(backend, "_request") as request:
            self.assertEqual(backend.recordings({"dictionary": "uploaded"}), ())
            request.assert_not_called()


class NativeCollectionRegression(unittest.TestCase):
    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_audio_storage_export_and_reviewed_trash_with_real_anki(self):
        result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("offline_audio_storage_probe.py"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
