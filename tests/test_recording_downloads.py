import unittest
from unittest.mock import patch

import support

from shirabe_addon import authoring_media as media
from shirabe_addon.authoring import AuthoringError, Lookup


class RecordingDownloads(unittest.TestCase):
    def setUp(self):
        self.m4a = "https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a"
        self.mp3 = self.m4a[:-4] + ".mp3"
        self.lookup = Lookup(lemma="猫", entry="jmdict:cat")

    def recordings(self, download_url=None):
        pitch = {"downstep": 1, "pattern": "HL", "audioUrl": self.m4a}
        if download_url is not None:
            pitch["audioDownloadUrl"] = download_url
        with patch("shirabe_addon.offline.DictionaryBackend") as backend:
            backend.return_value.identify.return_value = {"words": [{"candidates": [{
                "dictionary": "jmdict", "sourceId": "cat", "reading": "ねこ", "pitch": [pitch]}]}]}
            return media.recordings_from_config({}, self.lookup)

    def test_verified_mp3_is_used_for_note_fields_without_changing_popup_source(self):
        recording, = self.recordings(self.mp3)
        self.assertEqual(recording.url, self.m4a)
        self.assertEqual(recording.media.url, self.m4a)
        with patch.object(media, "import_selected_media") as importer:
            media.import_recording(None, recording, on_ready=lambda _: None, on_error=self.fail)
        selected = importer.call_args.args[1]
        self.assertEqual(selected, media.LicensedMedia(self.mp3, "Shirabe", media.ANKI_PERMISSION))
        self.assertEqual(importer.call_args.kwargs["allowed_extensions"], {"mp3"})

    def test_missing_download_url_never_guesses_a_url_or_silently_uses_m4a(self):
        recording, = self.recordings()
        errors = []
        with patch.object(media, "import_selected_media") as importer:
            media.import_recording(None, recording, on_ready=self.fail, on_error=errors.append)
        importer.assert_not_called()
        self.assertEqual(len(errors), 1)
        self.assertIn("AnkiWeb", str(errors[0]))
        self.assertEqual(recording.media.url, self.m4a)

    def test_m4a_requires_explicit_opt_in(self):
        recording, = self.recordings()
        with patch.object(media, "import_selected_media") as importer:
            media.import_recording(None, recording, allow_m4a=True, on_ready=lambda _: None, on_error=self.fail)
        self.assertEqual(importer.call_args.args[1].url, self.m4a)
        self.assertEqual(importer.call_args.kwargs["allowed_extensions"], {"m4a"})

    def test_untrusted_or_different_recording_downloads_are_ignored(self):
        invalid = (self.mp3 + "?download=1", self.mp3 + "#clip", self.mp3.replace("a" * 64, "b" * 64),
                   self.mp3.replace("cdn.shirabe.org", "evil.example"), self.m4a, True,
                   self.mp3.replace("/words/", "/uploads/"))
        for url in invalid:
            with self.subTest(url=url):
                recording, = self.recordings(url)
                self.assertIsNone(recording.download_url)
                self.assertEqual(recording.media.url, self.m4a)

    def test_native_recording_constructor_rejects_mismatched_download_identity(self):
        for url in (self.mp3.replace("a" * 64, "b" * 64), self.mp3 + "?x=1", self.m4a, True):
            with self.subTest(url=url), self.assertRaises(AuthoringError):
                media.Recording(self.m4a, "ねこ", 1, "HL", download_url=url)

    def test_mp3_failure_has_no_automatic_m4a_fallback(self):
        recording, = self.recordings(self.mp3)
        errors = []
        with patch.object(media, "import_selected_media") as importer:
            importer.side_effect = lambda *args, **kwargs: kwargs["on_error"](AuthoringError("Download failed"))
            media.import_recording(None, recording, on_ready=self.fail, on_error=errors.append)
        self.assertEqual(importer.call_count, 1)
        self.assertEqual(importer.call_args.args[1].url, self.mp3)
        self.assertEqual(str(errors[0]), "Download failed")


if __name__ == "__main__":
    unittest.main()
