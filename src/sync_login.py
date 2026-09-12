"""Native study credentials, never stored in add-on config or collection media."""

from __future__ import annotations

import os
from pathlib import Path
from time import time
from urllib.parse import urlsplit

from .sync_client import SyncClient, SyncError
from .sync_state import SyncState


def private_sync_path(profile_directory):
    directory = Path(profile_directory) / "shirabe-sync"
    if directory.is_symlink():
        raise ValueError("The private sync directory must not be a symbolic link")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory / "state.sqlite3"


class SyncLogin(SyncClient):
    def __init__(self, path, origin):
        super().__init__(origin)
        self.path = path

    def credentials(self):
        with SyncState(self.path) as state:
            value = state.get("native_login")
        if value and value["api"] != self.api:
            raise SyncError(401, "SYNC_ORIGIN_CHANGED")
        return value

    def start_login(self):
        self.credentials()
        grant = self.device_authorization()
        origin = urlsplit(self.api)
        for field in ("verification_uri", "verification_uri_complete"):
            target = urlsplit(grant.get(field, ""))
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                raise SyncError(400, "INVALID_VERIFICATION_URL")
        if not all(isinstance(grant.get(field), str) and grant[field] for field in ("device_code", "user_code")) or any(type(grant.get(field)) is not int or grant[field] <= 0 for field in ("expires_in", "interval")):
            raise SyncError(400, "INVALID_DEVICE_GRANT")
        now = time()
        pending = {**grant, "api": self.api, "expires_at": now + grant["expires_in"], "next_poll_at": now + grant["interval"]}
        with SyncState(self.path) as state:
            state.put("device_login", pending)
        return pending

    def pending_login(self):
        with SyncState(self.path) as state:
            pending = state.get("device_login")
        if pending and pending["api"] != self.api:
            raise SyncError(401, "SYNC_ORIGIN_CHANGED")
        return pending

    def poll_login(self):
        pending = self.pending_login()
        now = time()
        if not pending or now >= pending["expires_at"]:
            raise SyncError(400, "expired_token")
        if now < pending["next_poll_at"]:
            return False
        pending["next_poll_at"] = now + pending["interval"]
        with SyncState(self.path) as state:
            state.put("device_login", pending)
        try:
            tokens = self.poll_device(pending["device_code"])
        except SyncError as error:
            if error.code not in ("authorization_pending", "slow_down"):
                raise
            if error.code == "slow_down":
                pending["interval"] += 5
                pending["next_poll_at"] = now + pending["interval"]
                with SyncState(self.path) as state:
                    state.put("device_login", pending)
            return False
        self.save_tokens(tokens)
        return True

    def save_tokens(self, tokens):
        if not all(isinstance(tokens.get(field), str) and tokens[field] for field in ("access_token", "refresh_token")) or set(tokens.get("scope", "").split()) != set(self.SCOPES.split()) or type(tokens.get("expires_in")) is not int or tokens["expires_in"] <= 0:
            raise SyncError(400, "INVALID_SYNC_CREDENTIALS")
        value = {"api": self.api, "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                 "expires_at": time() + tokens["expires_in"]}
        with SyncState(self.path) as state:
            with state.db:
                state._put("native_login", value)
                state._put("device_login", None)
        self.access_token = value["access_token"]

    def sign_out(self):
        credentials = self.credentials()
        if credentials:
            self.revoke(credentials["refresh_token"])
        with SyncState(self.path) as state:
            with state.db:
                state._put("native_login", None)
                state._put("device_login", None)
        self.access_token = ""

    def _request(self, path, payload, *, method="POST", authenticated=True, **options):
        if authenticated:
            credentials = self.credentials()
            if not credentials:
                raise SyncError(401, "TOKEN_REQUIRED")
            if credentials["expires_at"] <= time() + 30:
                self.save_tokens(self.refresh(credentials["refresh_token"]))
                credentials = self.credentials()
            self.access_token = credentials["access_token"]
        return super()._request(path, payload, method=method, authenticated=authenticated, **options)
