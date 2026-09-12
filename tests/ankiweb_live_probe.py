"""Opt-in AnkiWeb checks using disposable copies and an unchanged source profile."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import pickle
import sqlite3
from urllib.parse import urlparse

from anki.collection import Collection
from anki.sync import SyncAuth, SyncStatus

from ankiweb_audit import snapshot, compare


def saved_auth(profile):
    with closing(sqlite3.connect((profile.parent / "prefs21.db").as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT data FROM profiles WHERE name = ?", (profile.name,)).fetchone()
    if not row:
        raise ValueError("Profile has no saved preferences")
    preferences = pickle.loads(row[0])
    endpoint = preferences.get("currentSyncUrl") or preferences.get("customSyncUrl") or ""
    if endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".ankiweb.net"):
            raise ValueError("This probe only accepts official AnkiWeb endpoints")
    if not preferences.get("syncKey"):
        raise ValueError("Profile is not logged into AnkiWeb")
    return SyncAuth(hkey=preferences["syncKey"], endpoint=endpoint, io_timeout_secs=20)


def copy_collection(source, target):
    if target.exists():
        raise ValueError("Probe destination must be new")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as copied:
            original.backup(copied)


def require_preserved(baseline, path):
    result = compare(baseline, snapshot(path))
    if not result["ok"]:
        raise AssertionError("Existing collection records changed; refusing further sync")
    return {table: values["baseline_count"] for table, values in result["tables"].items()}


class LiveAccountProbe:
    def __init__(self, root, profile, *, auth=None):
        self.root, self.profile = Path(root).resolve(), Path(profile).resolve()
        self.source = self.profile / "collection.anki2"
        if self.root == self.profile or self.profile in self.root.parents:
            raise ValueError("Probe artifacts must be outside the original profile")
        self.auth = auth or saved_auth(self.profile)
        self.baseline = snapshot(self.source)
        self.source_digest = self.digest()
        self.collections = []
        self.syncs = 0

    def digest(self):
        with self.source.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def clone(self, name, *, source=None, media=None):
        path = self.root / name / "collection.anki2"
        copy_collection(Path(source).resolve() if source else self.source, path)
        if media:
            media.copy(path.with_suffix(".media"))
        collection = Collection(str(path))
        self.collections.append(collection)
        require_preserved(self.baseline, path)
        if not collection.get_config("fsrs", False):
            raise AssertionError("AnkiWeb probe requires FSRS already enabled")
        for table in ("notes", "cards", "revlog", "graves", "decks", "notetypes", "deck_config"):
            if collection.db.scalar(f"SELECT count(*) FROM {table} WHERE usn = -1"):
                raise AssertionError("Source has unpublished changes; refusing to upload them")
        if collection.sync_status(self.auth).required != SyncStatus.NO_CHANGES:
            raise AssertionError("Source and AnkiWeb must already be synchronized")
        return collection

    def sync(self, collection):
        require_preserved(self.baseline, collection.path)
        if collection.sync_status(self.auth).required == SyncStatus.FULL_SYNC:
            raise AssertionError("Full sync is prohibited for this account probe")
        result = collection.sync_collection(self.auth, False)
        if result.required not in (0, 1):
            raise AssertionError("Native sync requested replacement; no full sync was performed")
        if result.new_endpoint:
            parsed = urlparse(result.new_endpoint)
            if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".ankiweb.net"):
                raise AssertionError("Unexpected native sync endpoint")
            self.auth.endpoint = result.new_endpoint
        self.syncs += 1
        require_preserved(self.baseline, collection.path)

    def report(self, **details):
        if self.digest() != self.source_digest:
            raise AssertionError("Original collection file changed during the probe")
        counts = require_preserved(self.baseline, self.source)
        result = {"original_unchanged": True, "preserved_counts": counts, "native_syncs": self.syncs, **details}
        (self.root / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        return result

    def close(self):
        for collection in reversed(self.collections):
            collection.close()
