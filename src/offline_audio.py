"""Native-approved pronunciation files shared by prepared snapshots and Anki."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace

from . import authoring_media
from .authoring import identity_text

MAX_CLIP_BYTES = 1024 * 1024
MAX_NOTE_BYTES = 4 * 1024 * 1024
MAX_NOTE_FILES = 16
MAX_CACHE_ENTRIES = 256
RECORDING_METADATA_BYTES = 512
MEDIA_NAME = re.compile(r"shirabe_([a-f0-9]{64})\.(m4a|mp3)\Z")
MIME_TYPES = {"m4a": "audio/mp4", "mp3": "audio/mpeg"}


def recording(value):
    if not isinstance(value, dict):
        return None
    filename = value.get("filename")
    match = MEDIA_NAME.fullmatch(filename) if isinstance(filename, str) and len(filename) < 100 else None
    size = value.get("bytes")
    if (not match or value.get("sha256") != match[1] or type(size) is not int or not 1 <= size <= MAX_CLIP_BYTES
            or value.get("mimeType") != MIME_TYPES[match[2]] or value.get("attribution") != "Shirabe"
            or value.get("license") != authoring_media.ANKI_PERMISSION):
        return None
    return {key: value[key] for key in ("filename", "sha256", "bytes", "mimeType", "attribution", "license")}


def pitches(snapshot):
    for segment in snapshot["segments"]:
        for result in segment["results"]:
            for candidate in result["candidates"]:
                for pitch in candidate["pitch"]:
                    yield candidate, pitch


def recordings(snapshot):
    return {pitch["recording"]["filename"]: pitch["recording"] for _, pitch in pitches(snapshot) if "recording" in pitch}


def limit_recordings(snapshot):
    files = {}
    conflict = False
    for _, pitch in pitches(snapshot):
        value = pitch.get("recording")
        if value is None:
            continue
        name = value["filename"]
        if name in files and files[name] != value:
            conflict = True
        files[name] = value
    if conflict or len(files) > MAX_NOTE_FILES or sum(value["bytes"] for value in files.values()) > MAX_NOTE_BYTES:
        for _, pitch in pitches(snapshot):
            pitch.pop("recording", None)


def metadata(download):
    digest = hashlib.sha256(download.data).hexdigest()
    return recording({"filename": f"shirabe_{digest}.{download.extension}", "sha256": digest,
                      "bytes": len(download.data), "mimeType": MIME_TYPES.get(download.extension),
                      "attribution": download.attribution, "license": download.license})


def verified_size(media, filename, *, expected=None):
    match = MEDIA_NAME.fullmatch(filename) if isinstance(filename, str) else None
    if not match:
        return None
    try:
        path = Path(media.dir()) / filename
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CLIP_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CLIP_BYTES:
                return None
            data = source.read(MAX_CLIP_BYTES + 1)
        if (not 0 < len(data) <= MAX_CLIP_BYTES or hashlib.sha256(data).hexdigest() != match[1]
                or not authoring_media._signature_matches(data, match[2]) or (expected is not None and len(data) != expected)):
            return None
        return len(data)
    except OSError:
        return None


def store(media, download):
    from .offline import SnapshotError
    info = metadata(download)
    if info is None:
        raise SnapshotError("Invalid prepared recording", code="audio_unavailable")
    imported = authoring_media.store_selected_media(SimpleNamespace(media=media), download)
    if imported.filename != info["filename"] or verified_size(media, imported.filename, expected=info["bytes"]) is None:
        raise SnapshotError("Prepared recording write could not be verified", code="audio_unavailable")


@dataclass
class AudioBatch:
    backend: object
    remaining_bytes: int
    download: object = None
    downloads: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    entries: dict = field(default_factory=dict)

    def __post_init__(self):
        from .offline import MAX_TOTAL_BYTES, SnapshotError
        if type(self.remaining_bytes) is not int or not 0 <= self.remaining_bytes <= MAX_TOTAL_BYTES:
            raise SnapshotError("Invalid offline audio batch budget", code="audio_limit")

    def prepare(self, snapshot, request, issues, cancelled):
        from .offline import SnapshotError, _json
        selected = {}
        size = 0
        for candidate, pitch in pitches(snapshot):
            pitch.pop("recording", None)
            if candidate["dictionary"] != "jmdict" or not candidate.get("reading"):
                continue
            key = (candidate["sourceId"], candidate.get("reading"))
            try:
                if cancelled():
                    raise SnapshotError("Preparation cancelled", code="cancelled")
                if key not in self.entries:
                    if len(self.entries) >= MAX_CACHE_ENTRIES:
                        self.entries.pop(next(iter(self.entries)))
                    self.entries[key] = request(self.backend.recordings, candidate)
                matching = [r for r in self.entries[key] if r.downstep == pitch["downstep"] and r.pattern == pitch["pattern"]
                            and r.reading == identity_text(candidate.get("reading") or "", reading=True)]
                if not matching:
                    continue
                clip = matching[0]
                selected_url = clip.download_url or clip.url
                selected_media = (authoring_media.LicensedMedia(selected_url, "Shirabe", authoring_media.ANKI_PERMISSION)
                                  if clip.download_url else clip.media)
                expected_extension = "mp3" if clip.download_url else "m4a"
                if selected_url not in self.downloads:
                    limit = min(MAX_CLIP_BYTES, MAX_NOTE_BYTES - size, self.remaining_bytes - len(_json(snapshot)) - RECORDING_METADATA_BYTES)
                    if limit <= 0 or len(self.downloads) >= MAX_CACHE_ENTRIES or len(selected) >= MAX_NOTE_FILES:
                        raise SnapshotError("Prepared recording budget reached", code="audio_limit")
                    download = self.download or authoring_media.download_selected_media
                    downloaded = request(lambda _: download(selected_media, allowed_hosts={"cdn.shirabe.org"},
                                                            cancelled=cancelled, max_bytes=limit), None)
                    info = metadata(downloaded)
                    if downloaded.extension != expected_extension or info is None or info["bytes"] > limit:
                        raise SnapshotError("Invalid prepared recording", code="audio_unavailable")
                    if info["filename"] not in self.files:
                        self.files[info["filename"]] = downloaded
                        self.remaining_bytes -= info["bytes"]
                    self.downloads[selected_url] = self.files[info["filename"]]
                downloaded = self.downloads[selected_url]
                info = metadata(downloaded)
                name = info["filename"]
                if name not in selected:
                    if len(selected) >= MAX_NOTE_FILES or size + info["bytes"] > MAX_NOTE_BYTES:
                        raise SnapshotError("Prepared recording budget reached", code="audio_limit")
                    selected[name] = downloaded
                    size += info["bytes"]
                pitch["recording"] = info
            except Exception as error:
                issues.append(error.code if isinstance(error, SnapshotError) else "audio_unavailable")
                if cancelled():
                    issues.append("cancelled")
                    break
        return selected
