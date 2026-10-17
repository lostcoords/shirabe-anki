"""Selected licensed media, bounded downloads and Anki-owned file handling."""

from __future__ import annotations

import hashlib
import re
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import net
from .authoring import AuthoringError, _text, identity_text

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = 15
AUDIO_TYPES = {"audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav",
               "audio/mp4": "m4a", "audio/x-m4a": "m4a"}
MEDIA_TYPES = AUDIO_TYPES | {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
RECORDING_URL = re.compile(r"https://cdn\.shirabe\.org/audio/words/[a-f0-9]{64}\.m4a")
ANKI_PERMISSION = "Permitted for Anki deck synchronization and export"


@dataclass(frozen=True)
class LicensedMedia:
    url: str
    attribution: str
    license: str


@dataclass(frozen=True)
class DownloadedMedia:
    data: bytes
    extension: str
    attribution: str
    license: str


@dataclass(frozen=True)
class ImportedMedia:
    filename: str
    field_html: str
    attribution: str
    license: str


@dataclass(frozen=True)
class Recording:
    url: str
    reading: str
    downstep: int
    pattern: str
    download_url: str | None = None

    def __post_init__(self):
        if not isinstance(self.url, str) or not RECORDING_URL.fullmatch(self.url):
            raise AuthoringError("Choose a verified Shirabe word recording.")
        if self.download_url is not None and self.download_url != self.url[:-4] + ".mp3":
            raise AuthoringError("The MP3 download must match the selected recording.")
        _text(self.reading, "recording reading", 256, required=True)
        _text(self.pattern, "accent pattern", 256)
        if type(self.downstep) is not int or not 0 <= self.downstep <= 100:
            raise AuthoringError("Invalid recording accent.")

    @property
    def label(self):
        return f"{self.reading} · accent {self.downstep}" + (f" ({self.pattern})" if self.pattern else "")

    @property
    def media(self):
        return LicensedMedia(self.url, "Shirabe", ANKI_PERMISSION)


def recordings_from_config(config, lookup) -> tuple[Recording, ...]:
    """Re-fetch the pinned entry; native permission covers only Shirabe word audio."""
    from .offline import DictionaryBackend
    if not lookup.entry.startswith("jmdict:"):
        return ()
    backend = DictionaryBackend(config.get("apiBase", "https://shirabe.org"), config.get("cardToken", ""))
    identified = backend.identify({"lemma": lookup.lemma or lookup.surface, "surface": lookup.surface,
                                   "reading": lookup.reading})
    candidates = identified.get("words", [{}])[0].get("candidates", []) if isinstance(identified, dict) else []
    detail = next((row for row in candidates
                   if f"{row.get('dictionary')}:{row.get('sourceId')}" == lookup.entry), None)
    if not isinstance(detail, dict):
        raise AuthoringError("The recording response does not match the selected entry.")
    pitch = detail.get("pitch", [])
    if not isinstance(pitch, list) or len(pitch) > 32 or any(not isinstance(p, dict) for p in pitch):
        raise AuthoringError("Invalid recording metadata.")
    recordings, seen = [], set()
    for row in pitch:
        url = row.get("audioUrl")
        if not isinstance(url, str) or not RECORDING_URL.fullmatch(url) or url in seen:
            continue
        reading = _text(detail.get("reading", ""), "recording reading", 256, required=True)
        download_url = row.get("audioDownloadUrl")
        if download_url != url[:-4] + ".mp3":
            download_url = None
        recordings.append(Recording(url, identity_text(reading, reading=True), row.get("downstep"), row.get("pattern", ""), download_url))
        seen.add(url)
    return tuple(recordings)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthoringError("The media address redirected. Choose a verified direct recording URL.")


def _signature_matches(data: bytes, extension: str) -> bool:
    if extension == "m4a":
        size = int.from_bytes(data[:4], "big")
        return data[4:12] == b"ftypM4A " and 16 <= size <= len(data) - 8 and size % 4 == 0
    return {
        "mp3": data.startswith(b"ID3") or (len(data) > 1 and data[0] == 255 and data[1] & 0xE0 == 0xE0),
        "ogg": data.startswith(b"OggS"),
        "wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": data.startswith(b"\xff\xd8\xff"),
        "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }.get(extension, False)


def download_selected_media(media: LicensedMedia, *, allowed_hosts, cancelled=lambda: False,
                            opener=None, clock=time.monotonic, max_bytes=MAX_BYTES) -> DownloadedMedia:
    """Run without collection access. Hosts and licensing come from native code.

    Card-provided URLs are not an allowlist. Redirects, active formats, credentials,
    arbitrary paths and oversized recordings are refused before media is stored.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise AuthoringError("Invalid media download limit.")
    if not media.attribution.strip() or not media.license.strip() or len(media.attribution) > 4000 or len(media.license) > 1000:
        raise AuthoringError("Choose a recording with its source and download license.")
    if len(media.url) > 2048 or any(ord(c) < 32 for c in media.url):
        raise AuthoringError("Invalid media URL.")
    try:
        url = urlsplit(media.url)
        valid = (url.scheme == "https" and url.hostname in set(allowed_hosts) and url.port in {None, 443}
                 and not url.username and not url.password and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise AuthoringError("Choose media from an approved HTTPS source.")
    if cancelled():
        raise AuthoringError("Media download cancelled.")
    opener = opener or urllib.request.build_opener(_NoRedirects())
    request = net.request(media.url, headers={"Accept": ", ".join(MEDIA_TYPES)})
    started = clock()
    with opener.open(request, timeout=TIMEOUT) as response:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        extension = MEDIA_TYPES.get(content_type)
        if not extension:
            raise AuthoringError("This recording or image format is not supported.")
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if not 1 <= int(length) <= max_bytes:
                    raise AuthoringError(f"The selected media exceeds the {max_bytes:,}-byte limit.")
            except ValueError as error:
                raise AuthoringError("Invalid media size.") from error
        parts, size = [], 0
        read = getattr(response, "read1", response.read)
        while True:
            if cancelled():
                raise AuthoringError("Media download cancelled.")
            if clock() - started > TIMEOUT:
                raise AuthoringError("Media download timed out.")
            part = read(min(65536, max_bytes + 1 - size))
            if not part:
                break
            size += len(part)
            if size > max_bytes:
                raise AuthoringError(f"The selected media exceeds the {max_bytes:,}-byte limit.")
            parts.append(part)
        data = b"".join(parts)
        if length is not None and len(data) != int(length):
            raise AuthoringError("The media download was incomplete.")
        if not _signature_matches(data, extension):
            raise AuthoringError("The downloaded content is not the selected media format.")
    return DownloadedMedia(data, extension, media.attribution, media.license)


def store_selected_media(col, media: DownloadedMedia) -> ImportedMedia:
    """Store downloaded bytes through Anki. Files may remain after a note undo."""
    if not 0 < len(media.data) <= MAX_BYTES or not _signature_matches(media.data, media.extension):
        raise AuthoringError("Invalid downloaded media.")
    digest = hashlib.sha256(media.data).hexdigest()
    name = col.media.write_data(f"shirabe_{digest}.{media.extension}", media.data)
    if any(c in name for c in '\\/:[]<>"\n\r'):
        raise AuthoringError("Anki returned an invalid media filename.")
    value = f"[sound:{name}]" if media.extension in AUDIO_TYPES.values() else f'<img src="{name}">'
    return ImportedMedia(name, value, media.attribution, media.license)


def import_selected_media(mw, selection: LicensedMedia, *, allowed_hosts, on_ready, on_error=None, cancelled=lambda: False, parent=None, allowed_extensions=None):
    """Native-only selected media handoff. on_ready receives an ImportedMedia.

    Pass it as build_preview(media=...) after showing native audio selection.
    Downloads hold no collection access; the subsequent file import uses Anki.
    """
    from aqt.operations import QueryOp
    parent = parent or mw
    def failed(error):
        if not cancelled():
            if on_error:
                on_error(error)
            else:
                from aqt.utils import showWarning
                showWarning(str(error), parent=parent)
    def ready(media):
        if media is not None and not cancelled():
            on_ready(media)
    def store(col, media):
        return None if cancelled() else store_selected_media(col, media)
    def downloaded(media):
        if not cancelled():
            if allowed_extensions is not None and media.extension not in allowed_extensions:
                failed(AuthoringError("The recording is not the selected audio format. Try another recording."))
                return
            QueryOp(parent=parent, op=lambda col: store(col, media), success=ready).failure(failed).run_in_background()
    QueryOp(parent=parent, op=lambda _: download_selected_media(selection, allowed_hosts=allowed_hosts, cancelled=cancelled),
            success=downloaded).without_collection().failure(failed).run_in_background()


def import_recording(mw, recording: Recording, *, on_ready, on_error, cancelled=lambda: False, parent=None, allow_m4a=False):
    """Import one native-approved recording, with no caller-provided host permissions."""
    if not isinstance(recording, Recording):
        raise AuthoringError("Choose a verified Shirabe recording.")
    if cancelled():
        return
    if recording.download_url:
        selection = LicensedMedia(recording.download_url, "Shirabe", ANKI_PERMISSION)
        extension = "mp3"
    elif allow_m4a is True:
        selection, extension = recording.media, "m4a"
    else:
        on_error(AuthoringError("An MP3 copy is not available. AnkiWeb does not show a replay button for M4A. Choose Use M4A anyway to continue."))
        return
    return import_selected_media(mw, selection, allowed_hosts={"cdn.shirabe.org"}, on_ready=on_ready,
                                 on_error=on_error, cancelled=cancelled, parent=parent, allowed_extensions={extension})
