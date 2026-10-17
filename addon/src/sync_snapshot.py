"""Capture on Anki's collection worker; upload on a network worker."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .sync_reviews import verify_platform
from .sync_client import SyncError
from .sync_media import SyncMedia
from .sync_state import SyncState


class SnapshotConflict(Exception):
    pass


def source_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class SyncSnapshot:
    MAX_BYTES = 4 * 1024 * 1024
    SELECTED_CARD = "((cards.odid = 0 AND cards.did IN (SELECT value FROM json_each(?))) OR cards.odid IN (SELECT value FROM json_each(?)))"
    SELECTED_CARDS = f"SELECT id FROM cards WHERE {SELECTED_CARD}"

    def __init__(self, path, client):
        self.path, self.client = path, client

    @staticmethod
    def metadata(collection, *, captured_at=None, source_decks=None, acknowledged_cursor=0):
        source_decks = source_decks if source_decks is not None else {item.id: collection.decks.get(item.id) for item in collection.decks.all_names_and_ids()}
        timing = collection._backend.sched_timing_today()
        return {"crt": collection.crt, "conf": collection.all_config(),
                "decks": {str(source_id): deck for source_id, deck in source_decks.items()},
                "dconf": {str(preset["id"]): preset for preset in collection.decks.all_config()},
                "capture": {"captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
                            "days_elapsed": timing.days_elapsed, "next_day_at": timing.next_day_at,
                            "acknowledged_cursor": acknowledged_cursor}}

    def capture(self, collection, deck_ids):
        verify_platform(collection)
        if not isinstance(deck_ids, list) or not deck_ids or any(type(value) is not int or value <= 0 for value in deck_ids):
            raise SnapshotConflict("Choose at least one ordinary Anki deck")
        roots = sorted(set(deck_ids))
        collection_path = str(Path(collection.path).resolve())
        with SyncState(self.path) as state:
            existing = state.get("initial_snapshot")
            if existing:
                if existing["api"] != self.client.api or existing["collection_path"] != collection_path or existing["root_deck_ids"] != roots:
                    raise SnapshotConflict("Resume the captured collection, server and deck selection before changing them")
                SyncMedia(self.path, self.client).capture_snapshot(collection)
                return state.get("initial_snapshot")
            decks = {item.id: collection.decks.get(item.id) for item in collection.decks.all_names_and_ids()}
            if any(source_id not in decks or decks[source_id].get("dyn") for source_id in roots):
                raise SnapshotConflict("Choose ordinary decks that still exist in this collection")
            names = [decks[source_id]["name"] for source_id in roots]
            selected = [source_id for source_id, deck in sorted(decks.items(), key=lambda item: item[1]["name"])
                        if not deck.get("dyn") and any(deck["name"] == name or deck["name"].startswith(name + "::") for name in names)]
            encoded = json.dumps(selected)
            captured_at = datetime.now(timezone.utc).isoformat()
            metadata = self.metadata(collection, captured_at=captured_at, source_decks=decks)
            pair_request = {"collection_key": str(uuid.uuid4()), "installation_id": str(uuid.uuid4()),
                            "key": str(uuid.uuid4()), "source_metadata": metadata}
            if len(json.dumps(pair_request).encode()) > self.MAX_BYTES:
                raise SnapshotConflict("Collection metadata exceeds the sync byte limit")
            selected_notes = f"SELECT nid FROM cards WHERE id IN ({self.SELECTED_CARDS})"
            siblings = collection.db.scalar(f"SELECT count(*) FROM cards WHERE nid IN ({selected_notes}) AND id NOT IN ({self.SELECTED_CARDS})",
                                            encoded, encoded, encoded, encoded)
            snapshot = {"api": self.client.api, "collection_path": collection_path, "root_deck_ids": roots,
                        "deck_ids": selected, "unselected_siblings": siblings, "pair_request": pair_request,
                        "generated_media": sorted(SyncMedia.latex_references(collection, selected)),
                        "profile": None, "linked_decks": [], "phase": "captured"}
            state.stage_snapshot(snapshot, self._capture_batches(collection, encoded, pair_request["key"], captured_at))
            SyncMedia(self.path, self.client).capture_snapshot(collection)
            return state.get("initial_snapshot")

    def _capture_batches(self, collection, selected, key, captured_at):
        for kind, entries in self._source_entities(collection, selected, captured_at):
            yield from self._batches(kind, entries, key)

    def _source_entities(self, collection, selected, captured_at):
        selected_notes = f"SELECT nid FROM cards WHERE id IN ({self.SELECTED_CARDS})"
        type_ids = collection.db.list(f"SELECT DISTINCT mid FROM notes WHERE id IN ({selected_notes}) ORDER BY mid", selected, selected)
        definitions = ({"definition": collection.models.get(source_id)} for source_id in type_ids)
        yield "note_types", definitions
        notes = self._rows(collection, "notes", f"EXISTS (SELECT 1 FROM cards WHERE cards.nid = notes.id AND {self.SELECTED_CARD})", (selected, selected))
        yield "notes", notes
        history = self._rows(collection, "revlog", f"EXISTS (SELECT 1 FROM cards WHERE cards.id = revlog.cid AND {self.SELECTED_CARD})", (selected, selected))
        yield "history", ({"row": row} for row in history)
        cards = self._rows(collection, "cards", self.SELECTED_CARD, (selected, selected), with_last_review=True, with_last_event=True)

        def card_entries():
            for row in cards:
                last_review = row.pop("last_review_id")
                last_event = row.pop("last_event_id")
                entry = {"row": row, "captured_at": captured_at}
                if last_review:
                    entry["last_review_id"] = last_review
                if last_event:
                    entry["last_event_id"] = last_event
                yield entry

        yield "cards", card_entries()

    @staticmethod
    def _rows(collection, table, where, parameters, *, with_last_review=False, with_last_event=False):
        if table not in ("notes", "cards", "revlog"):
            raise ValueError("Invalid source table")
        columns = [column[1] for column in collection.db.all("SELECT * FROM pragma_table_info(?) ORDER BY cid", table)]
        extra = ""
        if with_last_review:
            columns.append("last_review_id")
            extra = ", (SELECT max(id) FROM revlog WHERE cid = cards.id AND ease BETWEEN 1 AND 4 AND type BETWEEN 0 AND 3)"
        if with_last_event:
            columns.append("last_event_id")
            extra += ", (SELECT max(id) FROM revlog WHERE cid = cards.id)"
        last_id = 0
        page_size = 2000 if table == "revlog" else 200
        while True:
            rows = collection.db.all(f"SELECT {table}.*{extra} FROM {table} WHERE id > ? AND ({where}) ORDER BY id LIMIT {page_size}", last_id, *parameters)
            if not rows:
                return
            for values in rows:
                row = dict(zip(columns, values))
                last_id = row["id"]
                yield row

    def _batches(self, kind, rows, key):
        limit = 20 if kind == "note_types" else 200
        overhead = len(json.dumps({"key": key, "request_key": "0" * 36, kind: []}).encode())
        batch, size = [], overhead
        for row in rows:
            if kind == "notes" and any(len(field.encode()) > 1024 * 1024 for field in row["flds"].split("\x1f")):
                raise SnapshotConflict("A note field exceeds the sync byte limit")
            row_size = len(json.dumps(row).encode())
            if overhead + row_size > self.MAX_BYTES:
                raise SnapshotConflict(f"A {kind} record exceeds the sync byte limit")
            if batch and (len(batch) == limit or size + 2 + row_size > self.MAX_BYTES):
                yield {kind: batch}
                batch, size = [], overhead
            size += row_size + (2 if batch else 0)
            batch.append(row)
        if batch:
            yield {kind: batch}

    def upload(self, *, cancelled=lambda: False, progress=lambda done, total: None, media_progress=lambda done, total: None):
        def check_cancelled():
            if cancelled():
                raise SnapshotConflict("Sync paused. Resume the saved upload when ready.")

        with SyncState(self.path) as state:
            snapshot = state.get("initial_snapshot")
            if not snapshot or snapshot["api"] != self.client.api:
                raise SnapshotConflict("Capture this server's selected decks before uploading")
            if snapshot["phase"] == "uploaded":
                return snapshot["profile"]
            if snapshot["phase"] == "resolving":
                if state.get("changes"):
                    raise SnapshotConflict("Resolve the saved initial records before resuming this upload")
                snapshot["phase"] = "captured"
                state.put("initial_snapshot", snapshot)
            if not snapshot.get("media_captured"):
                raise SnapshotConflict("Finish capturing media from the selected collection before uploading")
            check_cancelled()
            if not snapshot["profile"]:
                try:
                    profile = self.client.pair(**snapshot["pair_request"])
                except SyncError as error:
                    if error.status == 413 and error.code == "MEDIA_QUOTA_EXCEEDED":
                        raise SnapshotConflict("The selected media exceeds your remaining Shirabe storage. Free space or start a smaller snapshot. Your saved upload is unchanged.") from None
                    raise
                if type(profile.get("id")) is not int or profile["id"] <= 0 or any(profile.get(field) != snapshot["pair_request"][source]
                        for field, source in (("collection_key", "collection_key"), ("installation_id", "installation_id"), ("snapshot_key", "key"))):
                    raise SnapshotConflict("The server returned a different collection identity")
                snapshot["profile"] = profile
                state.put("initial_snapshot", snapshot)
            profile_id, key = snapshot["profile"]["id"], snapshot["pair_request"]["key"]
            for source_id in snapshot["deck_ids"]:
                check_cancelled()
                if source_id in snapshot["linked_decks"]:
                    continue
                link = self.client.select_deck(profile_id, key=key, anki_id=source_id)
                if link.get("anki_id") != source_id:
                    raise SnapshotConflict("The server returned a different deck identity")
                snapshot["linked_decks"].append(source_id)
                state.put("initial_snapshot", snapshot)
            keys = state.snapshot_batch_keys()
            for index, request_key in enumerate(keys):
                check_cancelled()
                progress(index, len(keys))
                if state.batch_applied(request_key):
                    continue
                batch = state.batch(request_key)
                receipt = batch["receipt"]
                if receipt is None:
                    receipt = self.client.batch(profile_id, batch["payload"])
                    state.remember_receipt(request_key, receipt)
                if any(result.get("status") != "accepted" for kind in ("note_types", "notes", "history", "cards") for result in receipt.get(kind, [])):
                    from .sync_changes import SyncChanges, SyncConflict

                    entries = list(SyncChanges(self.path, self.client)._entries(batch["payload"], receipt))
                    problems = [{"request_key": request_key, "kind": kind, "id": result["id"],
                                 "status": result["status"], "code": result.get("code", result["status"])}
                                for kind, _, result, _ in entries if result["status"] != "accepted"]
                    state.stage_snapshot_resolution(request_key)
                    raise SyncConflict(problems)
                state.complete_batch(request_key, self._checkpoints(batch["payload"], receipt))
            check_cancelled()
            progress(len(keys), len(keys))
            SyncMedia(self.path, self.client).upload(cancelled=cancelled, progress=media_progress)
            profile = self.client.finish_snapshot(profile_id, key=key)
            if profile.get("id") != profile_id or profile.get("snapshot_key") != key or profile.get("sync_state") != "ready":
                raise SnapshotConflict("The server did not finish this snapshot")
            snapshot["phase"] = "uploaded"
            snapshot["profile"] = profile
            state.put("initial_snapshot", snapshot)
            return profile

    def template_diagnostics(self):
        with SyncState(self.path) as state:
            reports = []
            for source_id, revision in state.checkpoint_digests("note_type").items():
                definition = state.checkpoint("note_type", source_id)["payload"]
                checked = state.checkpoint("template_diagnostics", source_id)
                issues = checked["payload"] if checked and checked["digest"] == revision else None
                reports.append({"id": source_id, "name": definition["name"], "revision": int(revision), "issues": issues})
            return sorted(reports, key=lambda report: (report["name"], report["id"]))

    @staticmethod
    def _source_row(kind, item):
        row = item.get("definition", item.get("row", item))
        if kind == "notes":
            row = {key: value for key, value in row.items() if key not in ("base", "type_revision", "conflict_id")}
        return row

    @staticmethod
    def _checkpoints(payload, receipt):
        checkpoints = []
        for kind, singular in (("note_types", "note_type"), ("notes", "note"), ("history", "history"), ("cards", "card")):
            rows = [SyncSnapshot._source_row(kind, item) for item in payload.get(kind, [])]
            results = receipt.get(kind, [])
            if not isinstance(results, list) or len(results) != len(rows) or any(result.get("id") != row["id"] or result.get("status") != "accepted" for row, result in zip(rows, results)):
                raise SnapshotConflict(f"Shirabe has not accepted every {kind} record in this snapshot")
            for row, result in zip(rows, results):
                digest = "deleted" if kind == "cards" and result.get("source_deleted") is True else source_digest(row)
                checkpoints.append(("source_" + singular, row["id"], digest, row))
                if kind in ("notes", "cards"):
                    checkpoints.append((singular, row["id"], result["snapshot"], result[singular]))
                    if kind == "cards":
                        checkpoints.append(SyncSnapshot.review_checkpoint(row["id"], result["snapshot"], result.get("anki", {}),
                                                                          reset_generation=result["card"].get("reset_generation")))
                elif kind == "note_types":
                    checkpoints.append((singular, row["id"], str(result["revision"]), result["definition"]))
                    if "template_diagnostics" in result:
                        checkpoints.append(("template_diagnostics", row["id"], str(result["revision"]), result["template_diagnostics"]))
        return checkpoints

    @staticmethod
    def review_checkpoint(source_id, snapshot, source, *, reset_generation=None):
        event = source.get("schedule_review")
        value = {"event": event, "card": source["card_after"]} if source.get("card_after") and (event or type(reset_generation) is int and reset_generation >= 0) else None
        if value is not None and type(reset_generation) is int and reset_generation >= 0:
            value["reset_generation"] = reset_generation
        return ("review_schedule", source_id, snapshot, value)
