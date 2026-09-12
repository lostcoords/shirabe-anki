"""Private saved media uploads, independent of Anki's live media directory."""

from __future__ import annotations

import hashlib
import heapq
import html
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote
import uuid

from .sync_client import SyncClient, SyncError
from .sync_state import SyncState
from .sync_reviews import verify_platform


class MediaConflict(Exception):
    def __init__(self, message, filenames=()):
        self.conflicts = [{"kind": "media", "id": filename, "filename": filename, "code": "media_conflict"} for filename in filenames]
        super().__init__(message)


class SyncMedia:
    TEXT_EXTENSIONS = {".css", ".js", ".mjs", ".json", ".html", ".htm", ".svg"}

    @staticmethod
    def latex_references(collection, deck_ids):
        from .sync_snapshot import SyncSnapshot

        pattern = re.compile(r"\[(?:latex|\$\$?)\]", re.I)
        types = [model["id"] for model in collection.models.all()
                 if any(pattern.search(template[side]) for template in model["tmpls"] for side in ("qfmt", "afmt"))]
        selected = json.dumps(deck_ids)
        cards = collection.db.list(f"""SELECT cards.id FROM cards JOIN notes ON notes.id = cards.nid
            WHERE {SyncSnapshot.SELECTED_CARD} AND (notes.mid IN (SELECT value FROM json_each(?))
            OR instr(lower(notes.flds), '[latex]') > 0 OR instr(notes.flds, '[$]') > 0 OR instr(notes.flds, '[$$]') > 0)""",
            selected, selected, json.dumps(types))
        names = set()
        for card_id in cards:
            rendered = collection._backend.render_existing_card(card_id=card_id, browser=False, partial_render=False)
            for nodes in (rendered.question_nodes, rendered.answer_nodes):
                extracted = collection._backend.extract_latex(text="".join(node.text for node in nodes),
                    svg=rendered.latex_svg, expand_clozes=False)
                names.update(item.filename for item in extracted.latex)
        return names

    def __init__(self, path, client):
        self.path, self.client = Path(path), client
        self.directory = self.path.with_suffix(".media")
        if self.directory.is_symlink():
            raise MediaConflict("The private media directory must not be a symbolic link")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)

    @staticmethod
    def references(text):
        names = set()
        values = re.findall(r"\[sound:([^\]]+)\]", text)
        values += re.findall(r"(?:url\(\s*|(?:src|href|poster|data)\s*=\s*)([^\s'\"<>\)]+)", text, re.I)
        values += [match[0] or match[1] or match[2] for match in re.findall(r'''"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|`([^`$]*)`''', text)]
        for value in values:
            value = html.unescape(value.strip())
            value = re.sub(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|\\([\\/\"'`])",
                           lambda match: match[3] or chr(int(match[1] or match[2], 16)), value)
            if not value or value.startswith(("#", "/")) or re.match(r"[a-z][a-z0-9+.-]*:", value, re.I):
                continue
            value = value.removeprefix("./")
            for name in (value, unquote(re.split(r"[?#]", value, maxsplit=1)[0])):
                if any(0xD800 <= ord(char) <= 0xDFFF for char in name):
                    continue
                if name and len(name.encode()) <= 255 and name not in (".", "..") and not any(char in name for char in "/\\") and not any(ord(char) < 32 for char in name):
                    names.add(name)
        return names

    @staticmethod
    def _shared_files(directory: Path, known=()):
        names, by_case = set(), {}
        for filename in known:
            by_case.setdefault(filename.casefold(), []).append(filename)
        for entry in directory.iterdir():
            if not entry.name.startswith("_") or entry.is_symlink() or not entry.is_file():
                continue
            aliases = [filename for filename in by_case.get(entry.name.casefold(), [])
                       if (directory / filename).is_file() and not (directory / filename).is_symlink() and entry.samefile(directory / filename)]
            names.update(aliases or [entry.name])
        return names

    def capture_snapshot(self, collection):
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            if not snapshot or snapshot["api"] != self.client.api or snapshot["collection_path"] != str(Path(collection.path).resolve()):
                raise MediaConflict("Media must belong to the captured Anki collection and server")
            if snapshot.get("media_captured"):
                return
            media = Path(collection.media.dir())
            wanted = self._shared_files(media) | set(snapshot["generated_media"])
            counts = dict.fromkeys(("note_types", "notes", "history", "cards"), 0)
            for request_key in state.snapshot_batch_keys():
                batch = state.batch(request_key)["payload"]
                for kind in counts:
                    counts[kind] += len(batch.get(kind, []))
                for note in batch.get("notes", []):
                    wanted.update(self.references(note["flds"]))
                for entry in batch.get("note_types", []):
                    definition = entry["definition"]
                    wanted.update(self.references(definition.get("css", "")))
                    for template in definition["tmpls"]:
                        wanted.update(self.references(template["qfmt"]))
                        wanted.update(self.references(template["afmt"]))
            pending, missing = list(wanted), set()
            heapq.heapify(pending)
            while pending:
                filename = heapq.heappop(pending)
                source = media / filename
                if source.is_symlink() or not source.is_file():
                    missing.add(filename)
                    continue
                staged = self._stage(state, snapshot["pair_request"]["key"], filename, source)
                if source.suffix.lower() in self.TEXT_EXTENSIONS:
                    for dependency in self.references(staged.read_text(encoding="utf-8", errors="replace")) - wanted:
                        wanted.add(dependency)
                        heapq.heappush(pending, dependency)
            snapshot["media_captured"] = True
            snapshot["missing_media"] = sorted(missing)
            counts["media_files"], counts["media_bytes"] = state.db.execute(
                "SELECT count(*), coalesce(sum(bytes), 0) FROM media_uploads WHERE epoch = ?",
                (snapshot["pair_request"]["key"],)).fetchone()
            snapshot["counts"] = counts
            snapshot["pair_request"]["media_bytes"] = counts["media_bytes"]
            state.put("initial_snapshot", snapshot)

    def _snapshot(self, state, collection=None):
        snapshot = state.get("initial_snapshot")
        if not snapshot or snapshot["api"] != self.client.api or snapshot["phase"] != "uploaded":
            raise MediaConflict("Finish this server's initial snapshot before exchanging media")
        if collection and snapshot["collection_path"] != str(Path(collection.path).resolve()):
            raise MediaConflict("The open Anki collection changed before media synchronization")
        return snapshot

    def capture(self, collection):
        verify_platform(collection)
        from .sync_snapshot import SyncSnapshot

        with SyncState(self.path) as state:
            snapshot = self._snapshot(state, collection)
            decisions = state.db.execute("SELECT count(*) FROM media_decisions WHERE state IN ('pending', 'conflict')").fetchone()[0]
            if decisions:
                return decisions
            if state.db.execute("SELECT 1 FROM media_incoming").fetchone():
                return 0
            media = Path(collection.media.dir())
            known = {row[0] for row in state.db.execute("SELECT filename FROM media_checkpoints")}
            wanted = known | self._shared_files(media, known) | self.latex_references(collection, snapshot["deck_ids"])
            scanner = SyncSnapshot(self.path, self.client)
            for kind, entries in scanner._source_entities(collection, json.dumps(snapshot["deck_ids"]), None):
                if kind not in ("notes", "note_types"):
                    break
                for entry in entries:
                    if kind == "notes":
                        wanted.update(self.references(entry["flds"]))
                    else:
                        definition = entry["definition"]
                        wanted.update(self.references(definition.get("css", "")))
                        for template in definition["tmpls"]:
                            wanted.update(self.references(template["qfmt"]))
                            wanted.update(self.references(template["afmt"]))
            pending, missing = list(wanted), set()
            heapq.heapify(pending)
            while pending:
                filename = heapq.heappop(pending)
                source = media / filename
                if source.is_symlink() or not source.is_file():
                    missing.add(filename)
                    continue
                saved = self._stage(state, snapshot["pair_request"]["key"], filename, source)
                if source.suffix.lower() in self.TEXT_EXTENSIONS:
                    for dependency in self.references(saved.read_text(encoding="utf-8", errors="replace")) - wanted:
                        wanted.add(dependency)
                        heapq.heappush(pending, dependency)
            state.put("missing_media", sorted(missing))
            return state.db.execute("SELECT count(*) FROM media_uploads WHERE receipt IS NULL").fetchone()[0]

    def _stage(self, state, epoch, filename, source):
        pending = state.db.execute("SELECT digest FROM media_uploads WHERE filename = ? AND receipt IS NULL", (filename,)).fetchone()
        if pending:
            return self.directory / pending[0]
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as output:
            temporary = Path(output.name)
            try:
                actual, size = hashlib.sha256(), 0
                descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as incoming:
                    while chunk := incoming.read(64 * 1024):
                        size += len(chunk)
                        if size > SyncClient.MAX_MEDIA_BYTES:
                            raise MediaConflict(f"Media file exceeds the sync byte limit: {filename}")
                        output.write(chunk)
                        actual.update(chunk)
                output.flush()
                digest = actual.hexdigest()
                checkpoint = state.db.execute("SELECT digest FROM media_checkpoints WHERE filename = ?", (filename,)).fetchone()
                if checkpoint and checkpoint[0] == digest:
                    return source
                os.fsync(output.fileno())
                saved = self.directory / digest
                os.replace(temporary, saved)
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                with state.db:
                    state.db.execute("INSERT INTO media_uploads(request_key, epoch, filename, digest, base, bytes) VALUES (?, ?, ?, ?, ?, ?)",
                                     (str(uuid.uuid4()), epoch, filename, digest, checkpoint[0] if checkpoint and checkpoint[0] != "deleted" else None, size))
                return saved
            finally:
                temporary.unlink(missing_ok=True)

    def upload(self, *, cancelled=lambda: False, progress=lambda done, total: None):
        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            if not snapshot or snapshot["api"] != self.client.api or not snapshot["profile"]:
                raise MediaConflict("Pair the captured collection before uploading media")
            rows = state.db.execute("SELECT request_key, epoch, filename, digest, base, bytes FROM media_uploads WHERE receipt IS NULL ORDER BY rowid").fetchall()
            for index, (request_key, epoch, filename, digest, base, size) in enumerate(rows):
                if cancelled():
                    raise MediaConflict("Media sync paused. Resume the saved upload when ready.")
                progress(index, len(rows))
                if epoch != snapshot["pair_request"]["key"]:
                    raise MediaConflict("Saved media belongs to a different sync snapshot")
                try:
                    receipt = self.client.upload_media(snapshot["profile"]["id"], key=epoch, request_key=request_key,
                                                       filename=filename, digest=digest, base=base, source=self.directory / digest)
                except SyncError as error:
                    if error.status == 409:
                        raise MediaConflict("A media file changed in both apps. Choose which version to keep.", [filename]) from error
                    raise
                if (receipt.get("filename"), receipt.get("digest"), receipt.get("bytes"), receipt.get("deleted")) != (filename, digest, size, False) or type(receipt.get("id")) is not int or receipt["id"] <= 0:
                    raise MediaConflict("The server did not accept the saved media file")
                with state.db:
                    state.db.execute("UPDATE media_uploads SET receipt = ? WHERE request_key = ?", (json.dumps(receipt), request_key))
                    state.db.execute("INSERT INTO media_checkpoints VALUES (?, ?, ?) ON CONFLICT(filename) DO UPDATE SET digest = excluded.digest, server_id = excluded.server_id",
                                     (filename, digest, receipt["id"]))
                if not state.db.execute("SELECT 1 FROM media_uploads WHERE digest = ? AND receipt IS NULL", (digest,)).fetchone():
                    (self.directory / digest).unlink(missing_ok=True)
            progress(len(rows), len(rows))

    def exchange(self, *, cancelled=lambda: False, progress=lambda done, total: None):
        self._exchange_decisions(cancelled)
        with SyncState(self.path) as state:
            snapshot = self._snapshot(state)
            if state.db.execute("SELECT 1 FROM media_incoming").fetchone():
                return
        self.upload(cancelled=cancelled, progress=progress)
        with SyncState(self.path) as state:
            snapshot = self._snapshot(state)
            epoch, profile_id = snapshot["pair_request"]["key"], snapshot["profile"]["id"]
            for entry in self._manifest(profile_id, epoch, cancelled):
                if cancelled():
                    raise MediaConflict("Media sync paused before the next download")
                filename = entry["filename"]
                target = "deleted" if entry["deleted"] else entry["digest"]
                checkpoint = state.db.execute("SELECT digest, server_id FROM media_checkpoints WHERE filename = ?", (filename,)).fetchone()
                if checkpoint == (target, entry["id"]):
                    continue
                base = checkpoint[0] if checkpoint and checkpoint[0] != "deleted" else None
                if not entry["deleted"]:
                    self._download_file(profile_id, epoch, entry)
                with state.db:
                    state.db.execute("INSERT INTO media_incoming VALUES (?, ?, ?, ?)", (filename, epoch, base, json.dumps(entry)))

    def _download_file(self, profile_id, epoch, entry):
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
            path = Path(temporary.name)
            try:
                self.client.download_media(profile_id, key=epoch, id=entry["id"], digest=entry["digest"],
                                           bytes=entry["bytes"], destination=temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                if self._digest(path) != entry["digest"] or path.stat().st_size != entry["bytes"]:
                    raise MediaConflict("Downloaded media does not match its manifest")
                os.replace(path, self.directory / entry["digest"])
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                path.unlink(missing_ok=True)

    def _manifest(self, profile_id, epoch, cancelled):
        for _ in range(3):
            after, revision, files, names = 0, None, [], set()
            while True:
                if cancelled():
                    raise MediaConflict("Media sync paused during its manifest scan")
                page = self.client.media_manifest(profile_id, key=epoch, after=after)
                if type(page.get("revision")) is not int or page["revision"] < 0 or not isinstance(page.get("files"), list) or len(page["files"]) > 200:
                    raise MediaConflict("Invalid remote media manifest")
                if revision is not None and page["revision"] != revision:
                    break
                revision = page["revision"]
                previous = after
                for entry in page["files"]:
                    self._validate_file(entry)
                    if entry["id"] <= previous or entry["filename"] in names:
                        raise MediaConflict("The remote media manifest repeated a file")
                    previous = entry["id"]
                    names.add(entry["filename"])
                    files.append(entry)
                if type(page.get("cursor")) is not int or page["cursor"] != previous:
                    raise MediaConflict("Invalid remote media cursor")
                if not page["files"]:
                    return files
                after = page["cursor"]
        raise MediaConflict("Media changed during the manifest scan. Try sync again.")

    @staticmethod
    def _validate_file(entry):
        filename = entry.get("filename")
        if (type(entry.get("id")) is not int or entry["id"] <= 0 or type(entry.get("bytes")) is not int or
                not 0 <= entry["bytes"] <= SyncClient.MAX_MEDIA_BYTES or type(entry.get("deleted")) is not bool or
                not isinstance(filename, str) or not filename or len(filename.encode()) > 255 or filename in (".", "..") or
                any(char in filename for char in "/\\\x00")):
            raise MediaConflict("Invalid remote media identity")
        SyncClient._media_digest(entry.get("digest"))

    @staticmethod
    def _digest(path):
        if path.is_symlink():
            raise MediaConflict("Media synchronization cannot follow a symbolic link")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            if os.fstat(source.fileno()).st_size > SyncClient.MAX_MEDIA_BYTES:
                raise MediaConflict("Media file exceeds the sync byte limit")
            return hashlib.file_digest(source, "sha256").hexdigest()

    def apply(self, collection, *, cancelled=lambda: False):
        verify_platform(collection)
        applied = 0
        with SyncState(self.path) as state:
            snapshot = self._snapshot(state, collection)
            rows = state.db.execute("SELECT filename, epoch, base, payload FROM media_incoming ORDER BY rowid").fetchall()
            for filename, epoch, base, payload in rows:
                if cancelled():
                    raise MediaConflict("Media sync paused before changing the next Anki file")
                if epoch != snapshot["pair_request"]["key"]:
                    raise MediaConflict("Incoming media belongs to a different sync snapshot")
                entry = json.loads(payload)
                self._validate_file(entry)
                target = None if entry["deleted"] else entry["digest"]
                destination = Path(collection.media.dir()) / filename
                current = self._digest(destination)
                replacement = state.db.execute("SELECT epoch, base, target FROM media_replacements WHERE filename = ?", (filename,)).fetchone()
                if replacement and replacement != (epoch, base, target):
                    raise MediaConflict("The pending media replacement changed. Compare the current files again.", [filename])
                if current != target:
                    if current != base and not (current is None and replacement == (epoch, base, target)):
                        raise MediaConflict("Media changed in Anki before its incoming update. Choose which version to keep.", [filename])
                    data = None
                    if target is not None:
                        source = self.directory / target
                        if self._digest(source) != target or source.stat().st_size != entry["bytes"]:
                            raise MediaConflict("Saved incoming media is missing or changed")
                        data = source.read_bytes()
                    previous = destination.read_bytes() if current is not None else None
                    if current is not None:
                        if data is not None:
                            with state.db:
                                state.db.execute("INSERT OR IGNORE INTO media_replacements VALUES (?, ?, ?, ?)", (filename, epoch, base, target))
                        collection.media.trash_files([filename])
                    try:
                        if data is not None:
                            collection.media.write_data(filename, data)
                        if self._digest(destination) != target:
                            raise MediaConflict("Anki could not preserve the media reference")
                    except Exception:
                        if previous is not None and self._digest(destination) is None:
                            collection.media.write_data(filename, previous)
                        raise
                self._complete_incoming(state, filename, entry)
                applied += 1
            missing = [filename for (filename,) in state.db.execute("SELECT filename FROM media_checkpoints WHERE digest != 'deleted'")
                       if not (Path(collection.media.dir()) / filename).is_file()]
            if missing:
                raise MediaConflict("Synced media is missing in Anki. Choose whether to restore it or confirm its deletion from Shirabe.", missing)
        return applied

    @staticmethod
    def _pending_file(state, filename):
        return {
            "upload": [list(row) for row in state.db.execute("SELECT request_key, epoch, filename, digest, base, bytes FROM media_uploads WHERE filename = ? AND receipt IS NULL", (filename,))],
            "incoming": [list(row) for row in state.db.execute("SELECT filename, epoch, base, payload FROM media_incoming WHERE filename = ?", (filename,))],
            "replacement": [list(row) for row in state.db.execute("SELECT filename, epoch, base, target FROM media_replacements WHERE filename = ?", (filename,))],
            "checkpoint": [list(row) for row in state.db.execute("SELECT digest, server_id FROM media_checkpoints WHERE filename = ?", (filename,))],
        }

    def conflict_detail(self, problem):
        with SyncState(self.path) as state:
            snapshot = self._snapshot(state)
            entries = self._manifest(snapshot["profile"]["id"], snapshot["pair_request"]["key"], lambda: False)
        return next((entry for entry in entries if entry["filename"] == problem["filename"]), None)

    def prepare_resolution(self, collection, problem, remote):
        verify_platform(collection)
        if remote is None:
            raise MediaConflict("The remote media identity is missing. Refresh the linked profile before resolving it.")
        self._validate_file(remote)
        filename = remote["filename"]
        if filename != problem["filename"]:
            raise MediaConflict("The selected media file changed")
        with SyncState(self.path) as state:
            snapshot = self._snapshot(state, collection)
            if state.db.execute("SELECT 1 FROM media_decisions WHERE filename = ? AND state = 'pending'", (filename,)).fetchone():
                raise MediaConflict("Retry the saved media choice before making another decision")
            source = Path(collection.media.dir()) / filename
            digest = self._digest(source)
            return {"problem": problem, "remote": remote, "filename": filename,
                    "binding": {"api": self.client.api, "collection_path": snapshot["collection_path"], "profile_id": snapshot["profile"]["id"], "epoch": snapshot["pair_request"]["key"]},
                    "local": {"digest": digest, "bytes": source.stat().st_size} if digest is not None else None,
                    "pending": self._pending_file(state, filename)}

    def resolve(self, collection, prepared, choice):
        if choice not in ("anki", "shirabe"):
            raise MediaConflict("Choose which media version to keep")
        current = self.prepare_resolution(collection, prepared["problem"], prepared["remote"])
        if current != prepared:
            raise MediaConflict("Media changed while the choice was open. Compare the current files again.", [prepared["filename"]])
        if choice == "anki" and prepared["local"] is not None:
            source = Path(collection.media.dir()) / prepared["filename"]
            with source.open("rb") as incoming:
                data = incoming.read(SyncClient.MAX_MEDIA_BYTES + 1)
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != prepared["local"]["bytes"] or digest != prepared["local"]["digest"]:
                raise MediaConflict("Anki media changed before the choice was saved", [prepared["filename"]])
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as output:
                temporary = Path(output.name)
                try:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    os.replace(temporary, self.directory / digest)
                    descriptor = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                finally:
                    temporary.unlink(missing_ok=True)
        with SyncState(self.path) as state, state.db:
            snapshot = self._snapshot(state, collection)
            if self._pending_file(state, prepared["filename"]) != prepared["pending"] or snapshot["pair_request"]["key"] != prepared["binding"]["epoch"]:
                raise MediaConflict("Pending media changed before the choice was saved")
            state.db.execute("UPDATE media_decisions SET state = 'superseded' WHERE filename = ? AND state = 'conflict'", (prepared["filename"],))
            state.db.execute("INSERT INTO media_decisions(request_key, filename, payload, state) VALUES (?, ?, ?, 'pending')",
                             (str(uuid.uuid4()), prepared["filename"], json.dumps({"prepared": prepared, "choice": choice})))

    def _exchange_decisions(self, cancelled):
        with SyncState(self.path) as state:
            snapshot = self._snapshot(state)
            rows = state.db.execute("SELECT request_key, filename, payload, state FROM media_decisions WHERE state IN ('pending', 'conflict') ORDER BY rowid").fetchall()
            for request_key, filename, payload, status in rows:
                if cancelled():
                    raise MediaConflict("Media sync paused before sending the saved choice")
                if status == "conflict":
                    raise MediaConflict("Media changed after the choice. Compare the current files again.", [filename])
                decision = json.loads(payload)
                prepared, choice = decision["prepared"], decision["choice"]
                binding, remote, local = prepared["binding"], prepared["remote"], prepared["local"]
                if binding != {"api": self.client.api, "collection_path": snapshot["collection_path"], "profile_id": snapshot["profile"]["id"], "epoch": snapshot["pair_request"]["key"]}:
                    raise MediaConflict("The saved media choice belongs to a different profile or snapshot")
                try:
                    if choice == "anki":
                        if local is None:
                            result = self.client.delete_media(binding["profile_id"], key=binding["epoch"], request_key=request_key, id=remote["id"], base=remote["digest"])
                        else:
                            result = self.client.upload_media(binding["profile_id"], key=binding["epoch"], request_key=request_key,
                                                             filename=filename, digest=local["digest"], base=None if remote["deleted"] else remote["digest"],
                                                             source=self.directory / local["digest"])
                        self._validate_file(result)
                        if result["filename"] != filename or result["deleted"] != (local is None) or local and (result["digest"], result["bytes"]) != (local["digest"], local["bytes"]):
                            raise MediaConflict("The server did not accept the chosen media version")
                    else:
                        if remote["deleted"]:
                            if self.conflict_detail(prepared["problem"]) != remote:
                                raise SyncError(409, "SYNC_CONFLICT")
                        else:
                            self._download_file(binding["profile_id"], binding["epoch"], remote)
                        result = remote
                except SyncError as error:
                    if error.status != 409:
                        raise
                    with state.db:
                        state.db.execute("UPDATE media_decisions SET state = 'conflict' WHERE request_key = ?", (request_key,))
                    raise MediaConflict("Media changed after the choice. Compare the current files again.", [filename]) from error
                with state.db:
                    if self._pending_file(state, filename) != prepared["pending"]:
                        raise MediaConflict("Pending media changed before the chosen result was saved")
                    state.db.execute("DELETE FROM media_uploads WHERE filename = ? AND receipt IS NULL", (filename,))
                    state.db.execute("DELETE FROM media_incoming WHERE filename = ?", (filename,))
                    if choice == "anki":
                        state.db.execute("INSERT INTO media_checkpoints VALUES (?, ?, ?) ON CONFLICT(filename) DO UPDATE SET digest = excluded.digest, server_id = excluded.server_id",
                                         (filename, "deleted" if result["deleted"] else result["digest"], result["id"]))
                    else:
                        state.db.execute("INSERT INTO media_incoming VALUES (?, ?, ?, ?)", (filename, binding["epoch"], local["digest"] if local else None, json.dumps(result)))
                    state.db.execute("UPDATE media_decisions SET state = 'complete', receipt = ? WHERE request_key = ?", (json.dumps(result), request_key))

    def _complete_incoming(self, state, filename, entry):
        with state.db:
            state.db.execute("INSERT INTO media_checkpoints VALUES (?, ?, ?) ON CONFLICT(filename) DO UPDATE SET digest = excluded.digest, server_id = excluded.server_id",
                             (filename, "deleted" if entry["deleted"] else entry["digest"], entry["id"]))
            state.db.execute("DELETE FROM media_incoming WHERE filename = ?", (filename,))
        if not entry["deleted"]:
            digest = entry["digest"]
            if (not state.db.execute("SELECT 1 FROM media_uploads WHERE digest = ? AND receipt IS NULL", (digest,)).fetchone() and
                    not state.db.execute("SELECT 1 FROM media_incoming WHERE json_extract(payload, '$.digest') = ?", (digest,)).fetchone()):
                (self.directory / digest).unlink(missing_ok=True)
