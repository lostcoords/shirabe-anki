from __future__ import annotations

import hashlib
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

WEB = Path(__file__).parent / "web"
BUNDLE_NAME = re.compile(r"_shirabe_([a-f0-9]{20})_(?:attach\.js|shirabe-card\.js)\Z")
MAX_BUNDLE_BYTES = 4 * 1024 * 1024


def verified_bundle_size(media, name: str) -> int | None:
    match = BUNDLE_NAME.fullmatch(name) if isinstance(name, str) else None
    if not match:
        return None
    path = Path(media.dir()) / name
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BUNDLE_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BUNDLE_BYTES:
                return None
            digest = hashlib.sha256()
            size = 0
            while data := source.read(min(64 * 1024, MAX_BUNDLE_BYTES + 1 - size)):
                size += len(data)
                if size > MAX_BUNDLE_BYTES:
                    return None
                digest.update(data)
        return size if size == info.st_size and digest.hexdigest()[:20] == match[1] else None
    except OSError:
        return None


@dataclass(frozen=True)
class BundleStorage:
    bytes_used: int
    files: int


def bundle_storage(media) -> BundleStorage:
    size = count = 0
    with os.scandir(media.dir()) as entries:
        for entry in entries:
            if BUNDLE_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                size += entry.stat(follow_symlinks=False).st_size
                count += 1
    return BundleStorage(size, count)


def install(media, web: Path = WEB) -> dict[str, str]:
    payloads = {}
    for name in ("shirabe-card.js", "attach.js"):
        with (web / name).open("rb") as source:
            payloads[name] = source.read(MAX_BUNDLE_BYTES + 1)
    if any(not data or len(data) > MAX_BUNDLE_BYTES for data in payloads.values()):
        raise ValueError("The popup bundle is empty or exceeds 4 MiB. Reinstall the Shirabe add-on.")
    return {
        name: media.write_data(f"_shirabe_{hashlib.sha256(data).hexdigest()[:20]}_{name}", data)
        for name, data in payloads.items()
    }
