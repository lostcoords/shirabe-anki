import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import support
from shirabe_addon.sync_client import SyncClient, SyncError
from shirabe_addon.sync_login import SyncLogin, private_sync_path
from shirabe_addon.sync_state import SyncState


class SyncLoginTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = private_sync_path(self.directory.name)
        self.login = SyncLogin(self.path, "https://shirabe.example")
        self.grant = {"device_code": "private-code", "user_code": "ABCD", "verification_uri": "https://shirabe.example/link",
                      "verification_uri_complete": "https://shirabe.example/link?code=ABCD", "expires_in": 600, "interval": 5}
        self.tokens = {"access_token": "private-access", "refresh_token": "private-refresh", "expires_in": 3600,
                       "scope": "READ_SRS WRITE_SRS"}

    def tearDown(self):
        self.directory.cleanup()

    def test_device_login_resumes_and_respects_polling_interval_after_reopening(self):
        with patch.object(SyncClient, "device_authorization", return_value=self.grant), patch("shirabe_addon.sync_login.time", return_value=100):
            self.login.start_login()
        self.login = SyncLogin(self.path, "https://shirabe.example")
        with patch.object(SyncClient, "poll_device", return_value=self.tokens) as poll, patch("shirabe_addon.sync_login.time", return_value=102):
            self.assertFalse(self.login.poll_login())
            poll.assert_not_called()
        with patch.object(SyncClient, "poll_device", return_value=self.tokens), patch("shirabe_addon.sync_login.time", return_value=105):
            self.assertTrue(self.login.poll_login())
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("device_login"))
            self.assertEqual("private-refresh", state.get("native_login")["refresh_token"])

    def test_slow_down_is_persisted_and_does_not_retry_early(self):
        with patch.object(SyncClient, "device_authorization", return_value=self.grant), patch("shirabe_addon.sync_login.time", return_value=100):
            self.login.start_login()
        with patch.object(SyncClient, "poll_device", side_effect=SyncError(400, "slow_down")), patch("shirabe_addon.sync_login.time", return_value=105):
            self.assertFalse(self.login.poll_login())
        with SyncState(self.path) as state:
            self.assertEqual(10, state.get("device_login")["interval"])
            self.assertEqual(115, state.get("device_login")["next_poll_at"])

    def test_expired_grants_do_not_poll_and_other_origins_cannot_use_credentials(self):
        with patch.object(SyncClient, "device_authorization", return_value=self.grant), patch("shirabe_addon.sync_login.time", return_value=100):
            self.login.start_login()
        with patch.object(SyncClient, "poll_device") as poll, patch("shirabe_addon.sync_login.time", return_value=701):
            with self.assertRaises(SyncError):
                self.login.poll_login()
            poll.assert_not_called()
        self.login.save_tokens(self.tokens)
        other = SyncLogin(self.path, "https://another.example")
        with patch.object(SyncClient, "_request") as send:
            with self.assertRaises(SyncError):
                other.batch(123, {})
            send.assert_not_called()

    def test_refresh_is_saved_before_the_authenticated_request(self):
        with patch("shirabe_addon.sync_login.time", return_value=100):
            self.login.save_tokens({**self.tokens, "expires_in": 1})
        calls = []

        def request(client, path, payload, **kwargs):
            calls.append((path, client.access_token, kwargs))
            if path == "/oauth/token":
                return {**self.tokens, "access_token": "renewed", "refresh_token": "rotated"}
            with SyncState(self.path) as state:
                self.assertEqual("rotated", state.get("native_login")["refresh_token"])
            return {}

        with patch.object(SyncClient, "_request", request), patch("shirabe_addon.sync_login.time", return_value=102):
            self.login.batch(123, {"key": "epoch"})
        self.assertEqual(["/oauth/token", "/srs/anki/profiles/123/batches"], [call[0] for call in calls])
        self.assertFalse(calls[0][2]["authenticated"])
        self.assertEqual("renewed", calls[1][1])

    def test_sign_out_revokes_native_grant_without_touching_sync_data(self):
        self.login.save_tokens(self.tokens)
        with SyncState(self.path) as state:
            state.put("initial_snapshot", {"preserved": True})
        with patch.object(SyncClient, "revoke") as revoke:
            self.login.sign_out()
            revoke.assert_called_once_with("private-refresh")
        with SyncState(self.path) as state:
            self.assertIsNone(state.get("native_login"))
            self.assertEqual({"preserved": True}, state.get("initial_snapshot"))

    def test_private_storage_is_outside_collection_media_and_rejects_symlinks(self):
        self.assertEqual("shirabe-sync", self.path.parent.name)
        self.assertEqual(0o700, self.path.parent.stat().st_mode & 0o777)
        self.login.save_tokens(self.tokens)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        with tempfile.TemporaryDirectory() as profile:
            (Path(profile) / "shirabe-sync").symlink_to(self.path.parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                private_sync_path(profile)

    def test_unexpected_scopes_or_verification_origins_are_rejected(self):
        with self.assertRaises(SyncError):
            self.login.save_tokens({**self.tokens, "scope": "READ_SRS"})
        with patch.object(SyncClient, "device_authorization", return_value={**self.grant, "verification_uri_complete": "https://attacker.example"}):
            with self.assertRaises(SyncError):
                self.login.start_login()
