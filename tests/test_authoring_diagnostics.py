import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

import authoring_desktop_check as harness


class ImportDiagnostics(unittest.TestCase):
    def test_live_recording_metadata_is_only_the_approved_neko_mp3(self):
        server, _requests = harness.fixture_api(live_test_recording=True)
        try:
            self.assertEqual(server.fixture_audio_format, "mp3")
            self.assertTrue(server.live_test_recording)
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/v1/words/cat", timeout=3) as response:
                detail = json.load(response)
            base = "https://cdn.shirabe.org/audio/words/231d52edbc4efb2b35e7603c18c0050f0150846e17b28208e10219a0b63dd448"
            self.assertEqual(detail["reading"], "ねこ")
            self.assertEqual(detail["pitch"], [{"downstep": 1, "pattern": "atamadaka", "audioUrl": base + ".m4a", "audioDownloadUrl": base + ".mp3"}])
        finally:
            server.shutdown()
            server.server_close()

    def test_live_download_uses_the_production_transport_and_refuses_other_urls(self):
        download = Mock(side_effect=OSError("Stop before network"))
        media = SimpleNamespace(download_selected_media=download)
        transport = harness.RecordingDownload(media, Path("."), live_test_recording=True)
        cancelled = lambda: False
        with self.assertRaisesRegex(OSError, "Stop before network"):
            transport(SimpleNamespace(url=harness.LIVE_RECORDING_URL), allowed_hosts={"cdn.shirabe.org"}, cancelled=cancelled)
        call = download.call_args
        self.assertNotIn("opener", call.kwargs)
        self.assertEqual(call.kwargs["max_bytes"], 1_048_576)
        self.assertIs(call.kwargs["cancelled"], cancelled)
        download.reset_mock()
        for url in (harness.LIVE_RECORDING_URL.replace(".mp3", ".m4a"), harness.RECORDING_URLS[0], "https://example.com/recording.mp3"):
            with self.assertRaisesRegex(AssertionError, "Unexpected recording URL"):
                transport(SimpleNamespace(url=url), allowed_hosts={"cdn.shirabe.org"})
        download.assert_not_called()

    def test_live_recording_cannot_accept_changed_bytes_or_a_fixture_opener(self):
        download = Mock(return_value=SimpleNamespace(data=b"ID3changed", extension="mp3"))
        media = SimpleNamespace(download_selected_media=download)
        transport = harness.RecordingDownload(media, Path("."), live_test_recording=True)
        selection = SimpleNamespace(url=harness.LIVE_RECORDING_URL)
        with self.assertRaisesRegex(AssertionError, "approved MP3"):
            transport(selection, allowed_hosts={"cdn.shirabe.org"})
        download.reset_mock()
        with self.assertRaisesRegex(AssertionError, "production transport"):
            transport(selection, allowed_hosts={"cdn.shirabe.org"}, opener=object())
        download.assert_not_called()

    def test_recording_fixture_only_advertises_mp3_when_selected(self):
        for extension in ("m4a", "mp3"):
            with self.subTest(extension=extension):
                server, _requests = harness.fixture_api(audio_format=extension)
                try:
                    with urlopen(f"http://127.0.0.1:{server.server_port}/api/v1/words/cat", timeout=3) as response:
                        detail = json.load(response)
                    for row in detail["pitch"]:
                        if extension == "mp3":
                            self.assertEqual(row["audioDownloadUrl"], row["audioUrl"][:-4] + ".mp3")
                        else:
                            self.assertNotIn("audioDownloadUrl", row)
                finally:
                    server.shutdown()
                    server.server_close()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Synthetic codec fixtures require ffmpeg and ffprobe")
    def test_synthetic_recording_formats_are_real_aac_and_mp3(self):
        from shirabe_addon import authoring_media
        with tempfile.TemporaryDirectory(prefix="shirabe-codec-fixtures-") as root:
            for extension, codec in (("m4a", "aac"), ("mp3", "mp3")):
                transport = harness.RecordingDownload(authoring_media, Path(root), audio_format=extension)
                data = transport.bytes
                path = Path(root) / f"synthetic-tone.{extension}"
                self.assertEqual(data, path.read_bytes())
                result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", str(path)],
                                        check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(result.stdout)["streams"][0]["codec_name"], codec)
                selection = authoring_media.LicensedMedia(transport.urls[-1], "Synthetic fixture", "CC0")
                with self.assertRaisesRegex(OSError, "interrupted"):
                    transport(selection, allowed_hosts={"cdn.shirabe.org"})
                downloaded = transport(selection, allowed_hosts={"cdn.shirabe.org"})
                self.assertEqual(downloaded.data, data)
                self.assertEqual(downloaded.extension, extension)

    def test_non_anki_frames_do_not_expose_their_locals(self):
        def import_file(path):
            return harness.import_context()

        self.assertEqual(import_file("private unrelated value"), [])

    @unittest.skipUnless(importlib.util.find_spec("aqt"), "Run with Anki's bundled Python")
    def test_real_deck_importer_records_the_exact_unsupported_filename_without_ui(self):
        from anki.collection import Collection
        from aqt.import_export import importing
        from aqt.qt import QApplication

        messages = []
        path = str(Path(harness.__file__).resolve())
        with tempfile.TemporaryDirectory(prefix="shirabe-import-diagnostics-") as root:
            col = Collection(str(Path(root) / "collection.anki2"))
            try:
                with patch.object(QApplication, "__init__", side_effect=AssertionError("No UI permitted")), \
                     patch.object(importing, "showWarning", side_effect=lambda text: messages.append((text, harness.import_context()))):
                    importing.import_file(SimpleNamespace(col=col), path)
            finally:
                col.close()
        self.assertEqual(messages[0][0], "Unsupported file type.")
        self.assertEqual(messages[0][1][0]["function"], "import_file")
        self.assertEqual(messages[0][1][0]["path"], path)
        self.assertTrue(messages[0][1][0]["source"].endswith("aqt/import_export/importing.py"))


if __name__ == "__main__":
    unittest.main()
