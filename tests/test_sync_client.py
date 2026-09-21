from __future__ import annotations

import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

import support
from shirabe_addon.sync_client import SyncClient, SyncError
from shirabe_addon.sync_state import SyncState


class SyncClientTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        captured = self.requests
        self.receipt_parts = {}
        parts = self.receipt_parts

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                captured.append((self.path, self.headers.get("Authorization"), None))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(parts.get(self.path, {"result": "ok"})).encode())

            def do_POST(self):
                captured.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                if captured[-1][2].get("rate_limited"):
                    self.send_response(429)
                    self.send_header("Retry-After", captured[-1][2].get("delay", "7"))
                    self.end_headers()
                    self.wfile.write(b'{"code":"RATE_LIMIT_EXCEEDED"}')
                    return
                if self.path.endswith("/oauth/token"):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"code":"AUTHORIZATION_PENDING","error":"authorization_pending"}')
                    return
                self.send_response(302 if self.path.endswith("/deliveries") else 200)
                if self.path.endswith("/deliveries"):
                    self.send_header("Location", "/credential-leak")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                result = parts["first"] if captured[-1][2].get("request_key") == "parts" else {"result": "ok"}
                self.wfile.write(json.dumps(result).encode())

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = SyncClient(f"http://127.0.0.1:{self.server.server_port}", "native-secret")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_native_token_is_only_sent_to_authenticated_sync_requests(self):
        self.client.device_authorization()
        self.assertIsNone(self.requests[-1][1])
        self.assertEqual("shirabe-anki-sync", self.requests[-1][2]["client_id"])
        self.assertEqual("user:study:read user:study:write", self.requests[-1][2]["scope"])
        self.client.batch(123, {"key": "epoch", "request_key": "retry"})
        self.assertEqual("/api/v1/srs/anki/profiles/123/batches", self.requests[-1][0])
        self.assertEqual("Bearer native-secret", self.requests[-1][1])

    def test_redirects_never_forward_the_native_credential(self):
        with self.assertRaises(SyncError) as error:
            self.client.deliver(123, key="epoch", cursor=0)
        self.assertEqual(302, error.exception.status)
        self.assertEqual(1, len(self.requests))

    def test_large_batch_receipts_reassemble_exact_utf8_bytes_before_returning_a_result(self):
        result = {"notes": [{"fields": ["猫<&>"]}], "cards": [], "cursor": 7}
        raw = json.dumps(result, ensure_ascii=False).encode()
        split = raw.index("猫".encode()) + 1
        self.receipt_parts["first"] = self.receipt_part(raw, 0, split)
        self.receipt_parts[f"/api/v1/srs/anki/profiles/123/batches/456?key=epoch&offset={split}"] = self.receipt_part(raw, split, len(raw))
        self.assertEqual(result, self.client.batch(123, {"key": "epoch", "request_key": "parts"}))
        self.assertEqual(2, len(self.requests))
        self.assertIsNone(self.requests[-1][2])
        self.assertEqual("Bearer native-secret", self.requests[-1][1])

    def test_receipt_parts_reject_changed_identity_offsets_lengths_digests_and_empty_data(self):
        raw = b'{"result":"complete"}'
        split = 10
        path = f"/api/v1/srs/anki/profiles/123/batches/456?key=epoch&offset={split}"
        for field, value in (("id", 789), ("id", True), ("offset", 0), ("total_bytes", len(raw) + 1),
                             ("sha256", "0" * 64), ("data", ""), ("data", "not base64"),
                             ("data", base64.b64encode(b"x" * len(raw)).decode())):
            with self.subTest(field=field, value=value):
                self.receipt_parts["first"] = self.receipt_part(raw, 0, split)
                self.receipt_parts[path] = self.receipt_part(raw, split, len(raw))
                self.receipt_parts[path]["receipt"][field] = value
                with self.assertRaises(SyncError) as error:
                    self.client.batch(123, {"key": "epoch", "request_key": "parts"})
                self.assertEqual("INVALID_RECEIPT", error.exception.code)

    def test_corrupt_complete_receipt_is_not_returned_and_a_retry_can_retrieve_the_original(self):
        raw = b'{"result":"complete"}'
        self.receipt_parts["first"] = self.receipt_part(raw, 0, len(raw))
        self.receipt_parts["first"]["receipt"]["data"] = base64.b64encode(raw.replace(b"complete", b"tampered")).decode()
        with self.assertRaises(SyncError) as error:
            self.client.batch(123, {"key": "epoch", "request_key": "parts"})
        self.assertEqual("INVALID_RECEIPT", error.exception.code)
        self.receipt_parts["first"] = self.receipt_part(raw, 0, len(raw))
        self.assertEqual({"result": "complete"}, self.client.batch(123, {"key": "epoch", "request_key": "parts"}))

    @staticmethod
    def receipt_part(raw, start, end):
        return {"receipt": {"id": 456, "offset": start, "total_bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "data": base64.b64encode(raw[start:end]).decode()}}

    def test_rate_limit_preserves_the_server_wait_without_retrying_in_the_transport(self):
        with self.assertRaises(SyncError) as error:
            self.client.batch(123, {"rate_limited": True})
        self.assertEqual(7, error.exception.retry_after)
        self.assertEqual(1, len(self.requests))

    def test_invalid_or_unbounded_retry_delays_do_not_become_automatic_retries(self):
        for delay in ("0", "-1", "1.5", "86401", "999999999999999", "tomorrow"):
            with self.subTest(delay=delay), self.assertRaises(SyncError) as error:
                self.client.batch(123, {"rate_limited": True, "delay": delay})
            self.assertIsNone(error.exception.retry_after)

    def test_acknowledgement_carries_only_explicitly_withdrawn_undo_requests(self):
        self.client.acknowledge(123, key="epoch", cursor=8, kept_notes=[6], kept_reviews=[7])
        self.assertEqual(("/api/v1/srs/anki/profiles/123/acknowledgements", "Bearer native-secret",
                          {"key": "epoch", "cursor": 8, "kept_notes": [6], "kept_reviews": [7]}), self.requests[-1])
        self.client.acknowledge(123, key="epoch", cursor=9)
        self.assertEqual({"key": "epoch", "cursor": 9}, self.requests[-1][2])

    def test_conflict_comparisons_use_authenticated_gets_without_request_bodies(self):
        self.assertEqual({"result": "ok"}, self.client.note_conflict(123, 456))
        self.assertEqual({"result": "ok"}, self.client.card_conflict(123, 789))
        self.assertEqual([
            ("/api/v1/srs/anki/profiles/123/note_conflicts/456", "Bearer native-secret", None),
            ("/api/v1/srs/anki/profiles/123/card_conflicts/789", "Bearer native-secret", None),
        ], self.requests)

    def test_chunked_card_comparison_keeps_its_conflict_and_epoch_identity(self):
        result = {"id": 789, "key": "epoch", "snapshot": "current", "card": {"fields": ["猫"]}}
        raw = json.dumps(result, ensure_ascii=False).encode()
        split = raw.index("猫".encode()) + 1
        self.receipt_parts["/api/v1/srs/anki/profiles/123/card_conflicts/789"] = {
            **self.receipt_part(raw, 0, split), "key": "epoch"}
        self.receipt_parts[f"/api/v1/srs/anki/profiles/123/card_conflicts/789/comparisons/456?key=epoch&offset={split}"] = self.receipt_part(raw, split, len(raw))
        self.assertEqual(result, self.client.card_conflict(123, 789))
        self.assertEqual(2, len(self.requests))
        self.assertTrue(all(request[1:] == ("Bearer native-secret", None) for request in self.requests))

    def test_chunked_card_comparison_refuses_another_conflict_or_epoch(self):
        for field, value in (("id", 790), ("key", "another-epoch")):
            with self.subTest(field=field):
                result = {"id": 789, "key": "epoch", field: value}
                raw = json.dumps(result).encode()
                self.receipt_parts["/api/v1/srs/anki/profiles/123/card_conflicts/789"] = {
                    **self.receipt_part(raw, 0, len(raw)), "key": "epoch"}
                with self.assertRaises(SyncError) as error:
                    self.client.card_conflict(123, 789)
                self.assertEqual("INVALID_RECEIPT", error.exception.code)

    def test_oauth_polling_preserves_protocol_errors_alongside_api_problem_codes(self):
        with self.assertRaises(SyncError) as error:
            self.client.poll_device("private-code")
        self.assertEqual("authorization_pending", error.exception.code)

    def test_origins_and_profile_ids_cannot_redirect_credentials(self):
        for origin in ("http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=1"):
            with self.assertRaises(ValueError):
                SyncClient(origin, "secret")
        for profile_id in ("../other", -1, True):
            with self.assertRaises(ValueError):
                self.client.deliver(profile_id, key="epoch", cursor=0)
        for conflict_id in ("../other", -1, True):
            with self.assertRaises(ValueError):
                self.client.note_conflict(123, conflict_id)
            with self.assertRaises(ValueError):
                self.client.card_conflict(123, conflict_id)
        self.assertEqual([], self.requests)


class SyncStateTest(unittest.TestCase):
    def test_lost_response_reopens_with_the_exact_outgoing_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync.sqlite3"
            state = SyncState(path)
            batch = state.enqueue(key="epoch", notes=[{"id": 12, "flds": "猫"}])
            state.close()
            state = SyncState(path)
            try:
                self.assertEqual([batch], state.pending())
                receipt = {"notes": [{"id": 12, "status": "accepted"}]}
                state.remember_receipt(batch["request_key"], receipt)
                state.remember_receipt(batch["request_key"], receipt)
                self.assertEqual([], state.pending())
                with self.assertRaises(ValueError):
                    state.remember_receipt(batch["request_key"], {"different": True})
                state.remember_checkpoint("note", 12, "digest", {"fields": ["猫"]})
                self.assertEqual({"digest": "digest", "payload": {"fields": ["猫"]}}, state.checkpoint("note", 12))
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
            finally:
                state.close()
