"""Isolate and audit existing media before an opt-in native media sync."""

from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


def digest(path, algorithm="sha1"):
    hasher = hashlib.new(algorithm)
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class PreservedMedia:
    def __init__(self, profile, progress=lambda done, total: None):
        self.source = Path(profile) / "collection.media"
        self.index = Path(profile) / "collection.media.db2"
        if Path(str(self.index) + "-wal").exists():
            raise ValueError("Close the source profile before copying its media index")
        self.index_digest = digest(self.index, "sha256")
        with closing(sqlite3.connect(self.index.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            if db.execute("SELECT count(*) FROM media WHERE dirty = 1").fetchone()[0]:
                raise ValueError("Source media has unpublished changes")
            self.files = dict(db.execute("SELECT fname, csum FROM media"))
        if any(not name or name in (".", "..") or Path(name).name != name for name in self.files):
            raise ValueError("Media index contains a path outside its directory")
        self.progress = progress
        self.verify(self.source, exact=True)

    def verify(self, directory, *, exact=False):
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError("Media directory must not be a symbolic link")
        inodes = set()
        total = len(self.files)
        for index, (name, expected) in enumerate(self.files.items(), 1):
            path = directory / name
            if path.is_symlink() or not path.is_file() or not expected or digest(path) != expected:
                raise AssertionError("Existing media is missing or changed; refusing native sync")
            inodes.add(path.stat().st_ino)
            if index % 1000 == 0 or index == total:
                self.progress(index, total)
        if exact and any(path.is_symlink() or not path.is_file() or path.stat().st_ino not in inodes for path in directory.iterdir()):
            raise AssertionError("Source media contains unindexed files; refusing native sync")

    def copy(self, destination):
        destination = Path(destination)
        index = destination.with_name(destination.name + ".db2")
        if destination.exists() or index.exists():
            raise ValueError("Media copy destination must be new")
        if sys.platform == "darwin":
            with subprocess.Popen(["cp", "-cpR", str(self.source), str(destination)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
                while True:
                    try:
                        if process.wait(timeout=5):
                            raise RuntimeError("Copy-on-write media cloning failed")
                        break
                    except subprocess.TimeoutExpired:
                        self.progress(0, len(self.files))
            subprocess.run(["cp", "-cp", str(self.index), str(index)], check=True, capture_output=True)
        else:
            shutil.copytree(self.source, destination, copy_function=shutil.copy2)
            shutil.copy2(self.index, index)
        if digest(self.index, "sha256") != self.index_digest or Path(str(self.index) + "-wal").exists():
            raise AssertionError("Source media index changed during copying")
        self.verify(destination, exact=True)

    def sync(self, collection, auth):
        directory = Path(collection.media.dir())
        if directory.resolve() == self.source.resolve():
            raise ValueError("Never sync media directly from the original profile")
        self.verify(directory)
        collection.sync_media(auth)
        deadline = time.monotonic() + 180
        while collection.media_sync_status().active:
            if time.monotonic() >= deadline:
                collection.abort_media_sync()
                raise TimeoutError("Native media sync exceeded the probe deadline")
            self.progress(0, len(self.files))
            time.sleep(1)
        self.verify(directory)

    def unchanged(self):
        if digest(self.index, "sha256") != self.index_digest:
            raise AssertionError("Original media index changed")
        self.verify(self.source, exact=True)
        return len(self.files)
