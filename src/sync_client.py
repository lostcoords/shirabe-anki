"""Native-only transport for linked study data; never expose it to card bridges."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import net


class SyncError(Exception):
    def __init__(self, status, code, *, retry_after=None):
        self.status = status
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Shirabe sync failed ({status}: {code})")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SyncClient:
    CLIENT_ID = "shirabe-anki-sync"
    SCOPES = "READ_SRS WRITE_SRS"
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    MAX_MEDIA_BYTES = 20 * 1024 * 1024

    def __init__(self, base_url, access_token="", *, timeout=30):
        parsed = urllib.parse.urlsplit(base_url)
        local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if not parsed.hostname or not (parsed.scheme == "https" or local) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Use an HTTPS origin or a local development server")
        self.api = base_url.rstrip("/") + "/api/v1"
        self.access_token = access_token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirects())

    def device_authorization(self):
        return self._request("/oauth/device_authorization", {
            "client_id": self.CLIENT_ID, "scope": self.SCOPES, "resource": self.api,
        }, authenticated=False)

    def poll_device(self, device_code):
        return self._request("/oauth/token", {
            "client_id": self.CLIENT_ID, "resource": self.api, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, authenticated=False)

    def refresh(self, refresh_token):
        return self._request("/oauth/token", {
            "client_id": self.CLIENT_ID, "resource": self.api, "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, authenticated=False)

    def revoke(self, token):
        return self._request("/oauth/revoke", {"client_id": self.CLIENT_ID, "token": token}, authenticated=False)

    def pair(self, *, collection_key, installation_id, key, source_metadata, media_bytes):
        return self._request("/srs/anki/profiles", {
            "collection_key": collection_key, "installation_id": installation_id,
            "key": key, "source_metadata": source_metadata, "media_bytes": media_bytes,
        })

    def select_deck(self, profile_id, *, key, anki_id):
        return self._profile_request(profile_id, "/deck_links", {"key": key, "anki_id": anki_id})

    def batch(self, profile_id, payload):
        result = self._profile_request(profile_id, "/batches", payload)
        if "receipt" not in result:
            return result
        return self._receipt(profile_id, payload["key"], result, resource="batches")

    def _receipt(self, profile_id, key, result, *, resource):
        identity = None
        digest = hashlib.sha256()
        with tempfile.TemporaryFile() as incoming:
            while True:
                part = result.get("receipt")
                if not isinstance(part, dict) or any(type(part.get(field)) is not int for field in ("id", "offset", "total_bytes")) or \
                        part["id"] <= 0 or part["total_bytes"] <= 0 or part["offset"] != incoming.tell() or \
                        not isinstance(part.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", part["sha256"]):
                    raise SyncError(200, "INVALID_RECEIPT")
                current = (part["id"], part["total_bytes"], part["sha256"])
                if identity is not None and identity != current:
                    raise SyncError(200, "INVALID_RECEIPT")
                identity = current
                try:
                    chunk = base64.b64decode(part["data"], validate=True)
                except (KeyError, ValueError, TypeError, binascii.Error):
                    raise SyncError(200, "INVALID_RECEIPT") from None
                if not chunk or len(chunk) > 4 * 1024 * 1024 or incoming.tell() + len(chunk) > part["total_bytes"]:
                    raise SyncError(200, "INVALID_RECEIPT")
                incoming.write(chunk)
                digest.update(chunk)
                if incoming.tell() == part["total_bytes"]:
                    break
                query = urllib.parse.urlencode({"key": key, "offset": incoming.tell()})
                result = self._profile_request(profile_id, f"/{resource}/{part['id']}?{query}", None, method="GET")
            if digest.hexdigest() != identity[2]:
                raise SyncError(200, "INVALID_RECEIPT")
            incoming.seek(0)
            try:
                decoded = json.load(incoming)
            except (ValueError, UnicodeDecodeError):
                raise SyncError(200, "INVALID_RECEIPT") from None
            if not isinstance(decoded, dict) or "receipt" in decoded:
                raise SyncError(200, "INVALID_RECEIPT")
            return decoded

    def collection_settings(self, profile_id, payload):
        return self._profile_request(profile_id, "/collection_settings", payload, method="PATCH")

    def prepare_publications(self, profile_id, payload):
        return self._profile_request(profile_id, "/publications", payload)

    def media_manifest(self, profile_id, *, key, after=0):
        if type(after) is not int or after < 0:
            raise ValueError("Invalid media cursor")
        query = urllib.parse.urlencode({"key": key, "after": after})
        return self._profile_request(profile_id, f"/media_files?{query}", None, method="GET")

    def delete_media(self, profile_id, *, key, request_key, id, base):
        if type(id) is not int or id <= 0:
            raise ValueError("Invalid media identity")
        self._media_digest(base)
        return self._profile_request(profile_id, f"/media_files/{id}", {"key": key, "request_key": request_key, "base": base}, method="DELETE")

    def upload_media(self, profile_id, *, key, request_key, filename, digest, base, source):
        if not isinstance(filename, str) or not filename or len(filename.encode()) > 255 or filename in (".", "..") or any(char in filename for char in "/\\\x00"):
            raise ValueError("Invalid media filename")
        self._media_digest(digest)
        if base is not None:
            self._media_digest(base)
        boundary = uuid.uuid4().hex
        fields = {"key": key, "request_key": request_key, "filename": filename, "digest": digest, "base": base or ""}
        with tempfile.TemporaryFile() as body:
            for name, value in fields.items():
                body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="media.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
            size = 0
            actual = hashlib.sha256()
            with open(source, "rb") as incoming:
                while chunk := incoming.read(64 * 1024):
                    size += len(chunk)
                    if size > self.MAX_MEDIA_BYTES:
                        raise ValueError("Media file exceeds the sync byte limit")
                    actual.update(chunk)
                    body.write(chunk)
            if actual.hexdigest() != digest:
                raise ValueError("Media file changed before upload")
            body.write(f"\r\n--{boundary}--\r\n".encode())
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(body.tell())}
            body.seek(0)
            return self._profile_request(profile_id, "/media_files", None, body=body, headers=headers)

    def download_media(self, profile_id, *, key, id, digest, bytes, destination):
        if type(id) is not int or id <= 0 or type(bytes) is not int or not 0 <= bytes <= self.MAX_MEDIA_BYTES:
            raise ValueError("Invalid media download")
        self._media_digest(digest)
        if destination.seek(0, 2):
            raise ValueError("Media downloads require an empty temporary file")
        query = urllib.parse.urlencode({"key": key, "digest": digest})

        def receive(response):
            actual, size = hashlib.sha256(), 0
            try:
                if response.status != 200:
                    raise SyncError(response.status, "INVALID_MEDIA_RESPONSE")
                while chunk := response.read(min(64 * 1024, bytes - size + 1)):
                    size += len(chunk)
                    if size > bytes:
                        raise SyncError(response.status, "MEDIA_SIZE_MISMATCH")
                    actual.update(chunk)
                    destination.write(chunk)
                if size != bytes or actual.hexdigest() != digest:
                    raise SyncError(response.status, "MEDIA_DIGEST_MISMATCH")
            except Exception:
                destination.seek(0)
                destination.truncate()
                raise
            destination.seek(0)

        return self._profile_request(profile_id, f"/media_files/{id}?{query}", None, method="GET",
                                     headers={"Accept": "application/octet-stream"}, receive=receive)

    @staticmethod
    def _media_digest(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid media digest")

    def note_conflict(self, profile_id, conflict_id):
        if type(conflict_id) is not int or conflict_id <= 0:
            raise ValueError("Invalid note conflict ID")
        return self._profile_request(profile_id, f"/note_conflicts/{conflict_id}", None, method="GET")

    def card_conflict(self, profile_id, conflict_id):
        if type(conflict_id) is not int or conflict_id <= 0:
            raise ValueError("Invalid card conflict ID")
        result = self._profile_request(profile_id, f"/card_conflicts/{conflict_id}", None, method="GET")
        if "receipt" not in result:
            return result
        key = result.get("key")
        if not isinstance(key, str) or not key or len(key.encode()) > 128:
            raise SyncError(200, "INVALID_RECEIPT")
        decoded = self._receipt(profile_id, key, result, resource=f"card_conflicts/{conflict_id}/comparisons")
        if decoded.get("id") != conflict_id or decoded.get("key") != key:
            raise SyncError(200, "INVALID_RECEIPT")
        return decoded

    def start_snapshot(self, profile_id, *, installation_id, key, previous_key):
        return self._profile_request(profile_id, "/snapshot", {
            "installation_id": installation_id, "key": key, "previous_key": previous_key,
        })

    def finish_snapshot(self, profile_id, *, key):
        return self._profile_request(profile_id, "/snapshot", {"key": key}, method="PATCH")

    def deliver(self, profile_id, *, key, cursor, limit=200):
        result = self._profile_request(profile_id, "/deliveries", {"key": key, "cursor": cursor, "limit": limit})
        if not isinstance(result.get("changes"), list):
            raise SyncError(200, "INVALID_RESPONSE")
        changes = []
        for change in result["changes"]:
            if not isinstance(change, dict):
                raise SyncError(200, "INVALID_RESPONSE")
            if "receipt" in change:
                decoded = self._receipt(profile_id, key, change, resource="deliveries")
                if decoded.get("cursor") != change["receipt"]["id"]:
                    raise SyncError(200, "INVALID_RECEIPT")
                change = decoded
            changes.append(change)
        return {**result, "changes": changes}

    def acknowledge(self, profile_id, *, key, cursor, kept_notes=(), kept_reviews=()):
        payload = {"key": key, "cursor": cursor}
        if kept_notes:
            payload["kept_notes"] = list(kept_notes)
        if kept_reviews:
            payload["kept_reviews"] = list(kept_reviews)
        return self._profile_request(profile_id, "/acknowledgements", payload)

    def unlink(self, profile_id):
        return self._profile_request(profile_id, "", None, method="DELETE")

    def _profile_request(self, profile_id, path, payload, *, method="POST", **options):
        if type(profile_id) is not int or profile_id <= 0:
            raise ValueError("Invalid profile ID")
        return self._request(f"/srs/anki/profiles/{profile_id}{path}", payload, method=method, **options)

    def _request(self, path, payload, *, method="POST", authenticated=True, body=None, headers=None, receive=None):
        if authenticated and not self.access_token:
            raise SyncError(401, "TOKEN_REQUIRED")
        headers = {**(headers or {}), **({"Authorization": f"Bearer {self.access_token}"} if authenticated else {})}
        request = (net.request(self.api + path, data=body, method=method, headers=headers) if body is not None else
                   net.json_request(self.api + path, payload, method=method, headers=headers))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if receive:
                    return receive(response)
                if response.status == 204 or path == "/oauth/revoke":
                    return None
                return self._decode(response)
        except urllib.error.HTTPError as error:
            delay = error.headers.get("Retry-After", "")
            retry_after = int(delay) if re.fullmatch(r"[0-9]{1,5}", delay) and 1 <= int(delay) <= 86400 else None
            with error:
                try:
                    body = self._decode(error)
                except SyncError:
                    body = {}
            code = body.get("error", body.get("code", "HTTP_ERROR"))
            raise SyncError(error.code, code, retry_after=retry_after) from None

    def _decode(self, response):
        raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise SyncError(response.status, "RESPONSE_TOO_LARGE")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SyncError(response.status, "INVALID_RESPONSE") from None
        if not isinstance(data, dict):
            raise SyncError(response.status, "INVALID_RESPONSE")
        return data
