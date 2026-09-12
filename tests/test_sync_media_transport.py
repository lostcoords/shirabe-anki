from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import support
from shirabe_addon.sync_client import SyncClient, SyncError
from shirabe_addon.sync_login import SyncLogin, private_sync_path


class SyncMediaTransportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = b"\x00\xff\x80" + "猫の音声".encode() * 20_000
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.source = Path(self.directory.name) / "source.bin"
        self.source.write_bytes(self.data)
        self.requests = []
        self.response_data = self.data
        self.response_status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                message = BytesParser(policy=default).parsebytes(
                    f"Content-Type: {self.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
                fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                          for part in message.iter_parts()}
                owner.requests.append((self.path, self.headers.get("Authorization"), fields))
                self.send_response(owner.response_status)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                self.wfile.write(b'{"id":1}')

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization"), None))
                self.send_response(owner.response_status)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                self.wfile.write(b'{"files":[],"cursor":0,"revision":1}' if urlsplit(self.path).path.endswith("media_files") else owner.response_data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.client = SyncClient(self.origin, "native-secret")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def upload(self, **changes):
        return self.client.upload_media(123, **{
            "key": "epoch", "request_key": "immutable-retry", "filename": '猫 "音声".mp3',
            "digest": self.digest, "base": None, "source": self.source, **changes,
        })

    def download(self, destination, **changes):
        return self.client.download_media(123, **{
            "key": "epoch", "id": 1, "digest": self.digest, "bytes": len(self.data),
            "destination": destination, **changes,
        })

    def test_multipart_upload_preserves_binary_unicode_and_retry_metadata(self):
        self.assertEqual({"id": 1}, self.upload())
        path, authorization, fields = self.requests[-1]
        self.assertEqual("/api/v1/srs/anki/profiles/123/media_files", path)
        self.assertEqual("Bearer native-secret", authorization)
        self.assertEqual({"key": b"epoch", "request_key": b"immutable-retry", "base": b"",
                          "digest": self.digest.encode(), "filename": '猫 "音声".mp3'.encode(), "file": self.data}, fields)
        self.upload(base=self.digest)
        self.assertEqual(self.digest.encode(), self.requests[-1][2]["base"])
        self.assertEqual(self.data, self.source.read_bytes())

    def test_manifest_has_encoded_query_parameters_and_no_body(self):
        self.assertEqual({"files": [], "cursor": 0, "revision": 1}, self.client.media_manifest(123, key="epoch&other=bad", after=17))
        path, token, body = self.requests[-1]
        self.assertEqual({"key": ["epoch&other=bad"], "after": ["17"]}, parse_qs(urlsplit(path).query))
        self.assertEqual("Bearer native-secret", token)
        self.assertIsNone(body)

    def test_download_checks_bytes_and_digest_before_returning(self):
        with tempfile.TemporaryFile() as destination:
            self.download(destination)
            self.assertEqual(self.data, destination.read())
        self.assertEqual({"key": ["epoch"], "digest": [self.digest]}, parse_qs(urlsplit(self.requests[-1][0]).query))
        self.assertEqual("Bearer native-secret", self.requests[-1][1])

    def test_invalid_downloads_leave_no_partial_bytes(self):
        for data in (self.data[:-1], self.data + b"extra", b"x" * len(self.data)):
            self.response_data = data
            with self.subTest(size=len(data)), tempfile.TemporaryFile() as destination:
                with self.assertRaises(SyncError):
                    self.download(destination)
                self.assertEqual(b"", destination.read())

    def test_redirects_and_http_errors_never_write_to_destination_or_follow(self):
        for status in (302, 409):
            self.response_status = status
            self.response_data = b'{"code":"SYNC_CONFLICT"}'
            with self.subTest(status=status), tempfile.TemporaryFile() as destination:
                before = len(self.requests)
                with self.assertRaises(SyncError) as error:
                    self.download(destination)
                self.assertEqual(status, error.exception.status)
                self.assertEqual(b"", destination.read())
                self.assertEqual(before + 1, len(self.requests))
                with self.assertRaises(SyncError):
                    self.upload()
                self.assertEqual(before + 2, len(self.requests))

    def test_invalid_uploads_and_downloads_do_not_make_requests(self):
        for changes in ({"filename": "../outside"}, {"filename": "a\\b"}, {"filename": ""},
                        {"filename": "猫" * 100}, {"digest": "0" * 64}, {"base": "invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.upload(**changes)
        with patch.object(SyncClient, "MAX_MEDIA_BYTES", 2), self.assertRaises(ValueError):
            self.upload()
        for changes in ({"id": "../other"}, {"bytes": -1}, {"bytes": True},
                        {"bytes": SyncClient.MAX_MEDIA_BYTES + 1}, {"digest": "invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.download(io.BytesIO(), **changes)
        destination = io.BytesIO(b"preserved")
        with self.assertRaises(ValueError):
            self.download(destination)
        self.assertEqual(b"preserved", destination.getvalue())
        self.assertEqual([], self.requests)

    def test_media_uses_native_login_refresh_for_all_three_requests(self):
        login = SyncLogin(private_sync_path(self.directory.name), self.origin)
        tokens = {"access_token": "old", "refresh_token": "refresh", "expires_in": 1,
                  "scope": "READ_SRS WRITE_SRS"}
        with patch("shirabe_addon.sync_login.time", return_value=100):
            login.save_tokens(tokens)
        self.client = login
        with patch("shirabe_addon.sync_login.time", return_value=102), patch.object(
                login, "refresh", return_value={**tokens, "access_token": "renewed", "expires_in": 3600}) as refresh:
            self.upload()
            self.client.media_manifest(123, key="epoch")
            with tempfile.TemporaryFile() as destination:
                self.download(destination)
            refresh.assert_called_once_with("refresh")
        self.assertEqual(["Bearer renewed"] * 3, [request[1] for request in self.requests])
