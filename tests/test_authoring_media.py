import io
import unittest
from unittest.mock import patch

import support

from shirabe_addon import authoring_media as media
from shirabe_addon.authoring import AuthoringError, Lookup
from shirabe_addon.authoring_examples import search_examples


class Response(io.BytesIO):
    def __init__(self, data=b"ID3recording", headers=None):
        super().__init__(data)
        self.headers = headers or {"Content-Type": "audio/mpeg"}


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class SelectedMedia(unittest.TestCase):
    def setUp(self):
        self.selection = media.LicensedMedia("https://media.example/voice.mp3", "Speaker, corpus reference", "CC BY 4.0")

    def test_download_has_a_timeout_user_agent_and_attribution(self):
        opener = Opener(Response())
        result = media.download_selected_media(self.selection, allowed_hosts={"media.example"}, opener=opener)
        self.assertEqual(result.data, b"ID3recording")
        self.assertEqual(result.attribution, self.selection.attribution)
        self.assertIn("Shirabe-Anki", opener.requests[0][0].get_header("User-agent"))
        self.assertEqual(opener.requests[0][1], 15)

    def test_m4a_download_retains_the_audio_container(self):
        data = b"\x00\x00\x00\x1cftypM4A \x00\x00\x02\x00M4A isomiso2\x00\x00\x00\x0cmdatAAC!"
        selection = media.LicensedMedia("https://media.example/tone.m4a", "Synthetic test tone", "CC0")
        for content_type in ("audio/mp4", "audio/mp4; codecs=mp4a.40.2", "audio/x-m4a"):
            with self.subTest(content_type=content_type):
                response = Response(data, {"Content-Type": content_type})
                result = media.download_selected_media(selection, allowed_hosts={"media.example"}, opener=Opener(response))
                self.assertEqual(result.extension, "m4a")
                self.assertEqual(result.data, data)

    def test_m4a_header_cannot_be_truncated_or_a_video_brand(self):
        cases = (b"\x00\x00\x00\x1cftypM4A ",
                 b"\x00\x00\x10\x00ftypM4A \x00\x00\x02\x00M4A isomiso2",
                 b"\x00\x00\x00\x0cftypM4A \x00\x00\x02\x00M4A isomiso2",
                 b"\x00\x00\x00\x1dftypM4A \x00\x00\x02\x00M4A isomiso2extra",
                 b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41extra",
                 b"<html><script>alert('not audio')</script></html>")
        for data in cases:
            with self.subTest(data=data), self.assertRaises(AuthoringError):
                media.download_selected_media(self.selection, allowed_hosts={"media.example"},
                    opener=Opener(Response(data, {"Content-Type": "audio/mp4"})))

    def test_unlicensed_or_untrusted_urls_never_reach_the_network(self):
        for selection in (media.LicensedMedia(self.selection.url, "", ""),
                          media.LicensedMedia("file:///etc/passwd", "source", "license"),
                          media.LicensedMedia("https://evil.example/audio", "source", "license"),
                          media.LicensedMedia("https://user:pass@media.example/audio", "source", "license")):
            opener = Opener(Response())
            with self.subTest(selection=selection), self.assertRaises(AuthoringError):
                media.download_selected_media(selection, allowed_hosts={"media.example"}, opener=opener)
            self.assertEqual(opener.requests, [])

    def test_redirects_cannot_reach_a_second_host(self):
        with self.assertRaises(AuthoringError):
            media._NoRedirects().redirect_request(None, None, 302, "", {}, "http://127.0.0.1/private")

    def test_rejects_oversize_mislabeled_truncated_and_active_media(self):
        cases = [Response(headers={"Content-Type": "audio/mpeg", "Content-Length": str(media.MAX_BYTES + 1)}),
                 Response(b"<script>alert(1)</script>"),
                 Response(headers={"Content-Type": "audio/mpeg", "Content-Length": "100"}),
                 Response(b"<svg onload=alert(1)>", {"Content-Type": "image/svg+xml"}),
                 Response(b"ID3" + b"x" * media.MAX_BYTES)]
        for response in cases:
            with self.subTest(headers=response.headers), self.assertRaises(AuthoringError):
                media.download_selected_media(self.selection, allowed_hosts={"media.example"}, opener=Opener(response))

    def test_stream_deadline_and_cancellation_are_enforced(self):
        with self.assertRaises(AuthoringError):
            media.download_selected_media(self.selection, allowed_hosts={"media.example"}, opener=Opener(Response()), cancelled=lambda: True)
        ticks = iter([0, 16])
        with self.assertRaises(AuthoringError):
            media.download_selected_media(self.selection, allowed_hosts={"media.example"}, opener=Opener(Response()), clock=lambda: next(ticks))

    def test_caller_size_limit_rejects_headers_before_read_and_bounds_streaming(self):
        class Unread(Response):
            def read(self, *_):
                raise AssertionError("Oversized response must not be read")
            read1 = read
        response = Unread(headers={"Content-Type": "audio/mpeg", "Content-Length": "9"})
        with self.assertRaises(AuthoringError):
            media.download_selected_media(self.selection, allowed_hosts={"media.example"},
                                          max_bytes=8, opener=Opener(response))
        response = Response(b"ID3" + b"x" * 100)
        with self.assertRaises(AuthoringError):
            media.download_selected_media(self.selection, allowed_hosts={"media.example"},
                                          max_bytes=8, opener=Opener(response))
        for limit in (0, True, media.MAX_BYTES + 1):
            opener = Opener(Response())
            with self.assertRaises(AuthoringError):
                media.download_selected_media(self.selection, allowed_hosts={"media.example"}, max_bytes=limit, opener=opener)
            self.assertEqual(opener.requests, [])

    def test_native_recordings_refetch_pinned_entry_and_scope_permission_to_shirabe(self):
        calls = []
        url = "https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a"

        class Backend:
            def __init__(self, *args, **kwargs):
                pass
            def identify(self, token):
                calls.append(token)
                return {"words": [{"candidates": [{"dictionary": "jmdict", "sourceId": "word:cat", "reading": "ネコ", "pitch": [
                    {"downstep": 1, "pattern": "HL", "audioUrl": url},
                    {"downstep": 0, "pattern": "LH", "audioUrl": "https://third-party.example/voice.m4a"}]}]}]}

        lookup = Lookup(lemma="猫", entry="jmdict:word:cat", audio_urls=("file:///private/steal.mp3",))
        with patch("shirabe_addon.offline.DictionaryBackend", Backend):
            result = media.recordings_from_config({}, lookup)
        self.assertEqual(len(result), 1)
        self.assertIn("ねこ", result[0].label)
        self.assertIn("1", result[0].label)
        self.assertEqual(result[0].media, media.LicensedMedia(url, "Shirabe", "Permitted for Anki deck synchronization and export"))
        self.assertEqual(calls, [{"lemma": "猫", "surface": "", "reading": ""}])

    def test_recording_approval_excludes_other_paths_hosts_and_custom_dictionary_entries(self):
        approved = "https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a"
        invalid = (approved + "?download=1", approved + "#clip", approved.replace("https:", "http:"),
                   approved.replace("cdn.shirabe.org", "cdn.shirabe.org.evil.example"),
                   approved.replace("/words/", "/uploads/"), approved.replace(".m4a", ".mp3"),
                   approved.replace("/words/", "/words/../words/"), "file:///private/voice.m4a")
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(AuthoringError):
                media.Recording(url, "ねこ", 1, "HL")
        with patch("shirabe_addon.offline.DictionaryBackend") as backend:
            self.assertEqual(media.recordings_from_config({}, Lookup(lemma="猫", entry="custom:cat")), ())
            self.assertEqual(media.recordings_from_config({}, Lookup(lemma="猫")), ())
            backend.assert_not_called()

    def test_recording_response_must_match_the_pinned_entry(self):
        with patch("shirabe_addon.offline.DictionaryBackend") as backend:
            backend.return_value.identify.return_value = {
                "words": [{"candidates": [{"dictionary": "jmdict", "sourceId": "other"}]}]
            }
            with self.assertRaisesRegex(AuthoringError, "does not match"):
                media.recordings_from_config({}, Lookup(lemma="猫", entry="jmdict:cat"))


class ExampleCorpus(unittest.TestCase):
    def test_filters_language_length_and_deduplicates_entry_surface_results(self):
        calls = []
        rows = [
            {"id": "tatoeba:1", "japanese": "猫です。", "translations": [{"lang": "en", "text": "A cat."}]},
            {"id": "tatoeba:2", "japanese": "猫だ。", "translations": [{"lang": "es", "text": "Un gato."}]},
            {"id": "tatoeba:3", "japanese": "猫" * 30, "translations": [{"lang": "en", "text": "Long."}]},
        ]

        def fetch(path, params):
            calls.append((path, params))
            return {"examples" if "/words/" in path else "sentences": rows}

        examples = search_examples(fetch, Lookup(lemma="猫", surface="猫", entry="jmdict:1467640", word_id="猫-ねこ"), max_length=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["filter[wordId]"], "猫-ねこ")
        self.assertEqual(calls[0][1]["filter[translationLanguages][]"], "en")
        self.assertEqual(calls[0][1]["query"], "猫")
        self.assertEqual([(e.japanese, e.translation, e.source, e.reference) for e in examples], [("猫です。", "A cat.", "Tatoeba", "tatoeba:1")])

    def test_cancellation_and_invalid_filters_make_no_requests(self):
        calls = []
        self.assertEqual(search_examples(lambda *args: calls.append(args), Lookup(lemma="猫"), cancelled=lambda: True), ())
        with self.assertRaises(AuthoringError):
            search_examples(lambda *args: calls.append(args), Lookup(lemma="猫"), max_length=0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
