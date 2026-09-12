"""Profile-local retry receipts and checkpoints, outside Anki's collection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import uuid


class SyncState:
    def __init__(self, path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError("The sync sidecar must not be a symbolic link")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media_uploads (
                request_key TEXT PRIMARY KEY, epoch TEXT NOT NULL, filename TEXT NOT NULL,
                digest TEXT NOT NULL, base TEXT, bytes INTEGER NOT NULL, receipt TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS pending_media_filename ON media_uploads(filename) WHERE receipt IS NULL;
            CREATE TABLE IF NOT EXISTS media_checkpoints (
                filename TEXT PRIMARY KEY, digest TEXT NOT NULL, server_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_incoming (
                filename TEXT PRIMARY KEY, epoch TEXT NOT NULL, base TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_replacements (
                filename TEXT PRIMARY KEY REFERENCES media_incoming(filename) ON DELETE CASCADE,
                epoch TEXT NOT NULL, base TEXT NOT NULL, target TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_decisions (
                request_key TEXT PRIMARY KEY, filename TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'conflict', 'complete', 'superseded')), receipt TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unresolved_media_decision ON media_decisions(filename) WHERE state IN ('pending', 'conflict');
            CREATE TABLE IF NOT EXISTS batches (
                request_key TEXT PRIMARY KEY, payload TEXT NOT NULL, receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                kind TEXT NOT NULL, source_id INTEGER NOT NULL,
                digest TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (kind, source_id)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                sync_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                source_row TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
                source_id INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS delivery_results (
                cursor INTEGER PRIMARY KEY, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_choices (
                cursor INTEGER PRIMARY KEY, choice TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS note_deletions (
                cursor INTEGER PRIMARY KEY, change TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS note_creations (
                cursor INTEGER PRIMARY KEY, intent TEXT NOT NULL, output TEXT
            );
            CREATE TABLE IF NOT EXISTS history_removals (
                cursor INTEGER PRIMARY KEY, change TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_undos (
                cursor INTEGER PRIMARY KEY, attempt TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_undo_outputs (
                cursor INTEGER PRIMARY KEY REFERENCES review_undos(cursor) ON DELETE CASCADE,
                output TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_undo_result_choices (
                request_key TEXT PRIMARY KEY REFERENCES batches(request_key) ON DELETE CASCADE,
                choice TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshot_batches (request_key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS applied_batches (request_key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS applied_entities (
                request_key TEXT NOT NULL REFERENCES batches(request_key) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('note_types', 'notes', 'history', 'cards')),
                source_id INTEGER NOT NULL,
                accepted_cursor INTEGER NOT NULL CHECK(accepted_cursor >= 0),
                PRIMARY KEY (request_key, kind, source_id)
            );
            CREATE INDEX IF NOT EXISTS applied_entity_identity ON applied_entities(kind, source_id);
            CREATE TABLE IF NOT EXISTS batch_resolutions (
                request_key TEXT NOT NULL REFERENCES batches(request_key) ON DELETE CASCADE,
                kind TEXT NOT NULL, source_id INTEGER NOT NULL,
                replacement_key TEXT NOT NULL UNIQUE REFERENCES batches(request_key) ON DELETE CASCADE,
                base TEXT NOT NULL,
                PRIMARY KEY (request_key, kind, source_id)
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self._put(key, value)

    def _put(self, key, value):
        self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)))

    def enqueue(self, *, key, **entities):
        with self.db:
            return self._enqueue(key, entities)

    def _enqueue(self, key, entities):
        if entities.keys() - {"note_types", "notes", "cards", "history", "creations"}:
            raise ValueError("Unknown sync batch properties")
        request_key = str(uuid.uuid4())
        payload = {"key": key, "request_key": request_key, **entities}
        self.db.execute("INSERT INTO batches(request_key, payload) VALUES (?, ?)",
                        (request_key, json.dumps(payload, ensure_ascii=False)))
        return payload

    def batch(self, request_key):
        row = self.db.execute("SELECT payload, receipt FROM batches WHERE request_key = ?", (request_key,)).fetchone()
        if not row:
            raise ValueError("Unknown outgoing batch")
        return {"payload": json.loads(row[0]), "receipt": json.loads(row[1]) if row[1] else None}

    def pending(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM batches WHERE receipt IS NULL ORDER BY rowid")]

    def remember_receipt(self, request_key, receipt):
        encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False)
        with self.db:
            row = self.db.execute("SELECT receipt FROM batches WHERE request_key = ?", (request_key,)).fetchone()
            if not row:
                raise ValueError("Unknown outgoing batch")
            if row[0] and row[0] != encoded:
                raise ValueError("A batch receipt cannot change")
            self.db.execute("UPDATE batches SET receipt = ? WHERE request_key = ?", (encoded, request_key))

    def checkpoint(self, kind, source_id):
        row = self.db.execute("SELECT digest, payload FROM checkpoints WHERE kind = ? AND source_id = ?", (kind, source_id)).fetchone()
        return {"digest": row[0], "payload": json.loads(row[1])} if row else None

    def checkpoint_digest(self, kind, source_id):
        row = self.db.execute("SELECT digest FROM checkpoints WHERE kind = ? AND source_id = ?", (kind, source_id)).fetchone()
        return row[0] if row else None

    def checkpoint_digests(self, kind):
        return dict(self.db.execute("SELECT source_id, digest FROM checkpoints WHERE kind = ?", (kind,)))

    def card_history(self, source_id):
        return {row_id: json.loads(payload) for row_id, payload in self.db.execute(
            "SELECT source_id, payload FROM checkpoints WHERE kind = 'source_history' AND digest != 'deleted' AND json_extract(payload, '$.cid') = ?", (source_id,))}

    def missing_history(self, present_ids):
        grouped = {}
        for (payload,) in self.db.execute("""
            SELECT payload FROM checkpoints WHERE kind = 'source_history' AND digest != 'deleted'
            AND source_id NOT IN (SELECT value FROM json_each(?))
        """, (json.dumps(sorted(present_ids)),)):
            row = json.loads(payload)
            grouped.setdefault(row["cid"], []).append(row)
        return grouped

    def remember_checkpoint(self, kind, source_id, digest, payload):
        with self.db:
            self._remember_checkpoint(kind, source_id, digest, payload)

    def _remember_checkpoint(self, kind, source_id, digest, payload):
        self.db.execute("""
            INSERT INTO checkpoints VALUES (?, ?, ?, ?)
            ON CONFLICT(kind, source_id) DO UPDATE SET digest = excluded.digest, payload = excluded.payload
        """, (kind, source_id, digest, json.dumps(payload, ensure_ascii=False)))

    def bind_delivery(self, binding):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('delivery_binding', ?) ON CONFLICT(key) DO NOTHING", (json.dumps(binding),))
            if self.get("delivery_binding") != binding:
                raise ValueError("This sidecar belongs to another server, profile or sync epoch")

    def remember_delivery(self, start_cursor, response):
        delivery = {"start_cursor": start_cursor, "response": response, "return_key": None, "phase": "received"}
        with self.db:
            existing = self.get("delivery")
            if existing:
                if existing["start_cursor"] != start_cursor or existing["response"] != response:
                    raise ValueError("A pending delivery cannot change")
                return existing
            if start_cursor != self.get("delivery_cursor", 0):
                raise ValueError("The delivery cursor changed")
            self._put("delivery", delivery)
        return delivery

    def begin_delivery(self):
        with self.db:
            delivery = self.get("delivery")
            if not delivery:
                raise ValueError("Receive a delivery before applying changes")
            delivery["phase"] = "applying"
            self._put("delivery", delivery)

    def stage_snapshot(self, snapshot, batches):
        with self.db:
            if self.get("initial_snapshot") is not None:
                raise ValueError("A snapshot has already been captured")
            self._put("initial_snapshot", snapshot)
            for entities in batches:
                batch = self._enqueue(snapshot["pair_request"]["key"], entities)
                self.db.execute("INSERT INTO snapshot_batches VALUES (?)", (batch["request_key"],))

    def snapshot_batch_keys(self):
        return [row[0] for row in self.db.execute("SELECT request_key FROM snapshot_batches ORDER BY rowid")]

    def stage_snapshot_resolution(self, request_key):
        with self.db:
            snapshot = self.get("initial_snapshot")
            if not snapshot or snapshot["phase"] != "captured" or not snapshot["profile"] or self.get("changes"):
                raise ValueError("Finish the saved initial resolution before starting another")
            if request_key not in self.snapshot_batch_keys() or self.batch_applied(request_key) or self.batch(request_key)["receipt"] is None:
                raise ValueError("This initial batch is not waiting for resolution")
            binding = {"api": snapshot["api"], "profile_id": snapshot["profile"]["id"],
                       "key": snapshot["pair_request"]["key"], "collection_path": snapshot["collection_path"]}
            snapshot["phase"] = "resolving"
            self._put("initial_snapshot", snapshot)
            self._put("changes", {"binding": binding, "keys": [request_key], "initial": True})

    def stage_changes(self, binding, batches):
        with self.db:
            if self.get("changes") is not None:
                raise ValueError("Finish the saved incremental sync first")
            keys = [self._enqueue(binding["key"], entities)["request_key"] for entities in batches]
            cycle = {"binding": binding, "keys": keys}
            self._put("changes", cycle)
        return cycle

    def complete_changes(self):
        with self.db:
            cycle = self.get("changes")
            if not cycle or not all(self.batch_applied(key) for key in cycle["keys"]):
                raise ValueError("Apply every incremental batch before finishing sync")
            self.db.execute("DELETE FROM settings WHERE key = 'changes'")

    def replacement_for(self, request_key, kind, source_id):
        row = self.db.execute("SELECT replacement_key FROM batch_resolutions WHERE request_key = ? AND kind = ? AND source_id = ?",
                              (request_key, kind, source_id)).fetchone()
        return row[0] if row else None

    def resolution_base(self, request_key):
        row = self.db.execute("SELECT base FROM batch_resolutions WHERE replacement_key = ?", (request_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def deletion_return_keys(self):
        return self._return_keys("source_deletion_keys")

    def review_undo_return_keys(self):
        return self._return_keys("review_undo_keys")

    def _return_keys(self, field):
        delivery = self.get("delivery")
        if not delivery:
            return []
        roots = [key for change in delivery["response"]["changes"]
                 for key in (self.delivery_result(change["cursor"]) or {}).get(field, [])]
        return [row[0] for row in self.db.execute("""
            WITH RECURSIVE pending(request_key) AS (
                SELECT value FROM json_each(?) UNION
                SELECT r.replacement_key FROM batch_resolutions r JOIN pending p ON r.request_key = p.request_key
            ) SELECT request_key FROM pending
        """, (json.dumps(roots),))]

    def stage_resolution(self, request_key, kind, source_id, entry, base, *, returned=False, dependencies=()):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cycle = self.get("changes")
            keys = self.deletion_return_keys() + self.review_undo_return_keys() if returned else cycle["keys"] if cycle else []
            if request_key not in keys or self.entity_applied(request_key, kind, source_id):
                raise ValueError("This sync record is no longer waiting for resolution")
            if returned:
                original = next((item for item in self.batch(request_key)["payload"].get("cards", []) if item["row"]["id"] == source_id), {})
                undo = original.get("review_undo_cursor")
                if undo:
                    if kind != "cards" or entry.get("review_undo_cursor") != undo or entry.get("removed_history") != original.get("removed_history") or entry.get("deleted") or entry.get("restored_history"):
                        raise ValueError("A review Undo resolution must preserve its request and removed history")
                elif kind != "cards" or entry.get("deleted") is not True:
                    raise ValueError("A source deletion return requires a deletion resolution")
            if dependencies and (returned or kind != "cards" or entry.get("deleted")):
                raise ValueError("Only a current card retry can add review dependencies")
            existing = self.replacement_for(request_key, kind, source_id)
            if existing:
                if dependencies or self.batch(existing)["payload"].get(kind) != [entry] or self.resolution_base(existing) != base:
                    raise ValueError("Finish the saved resolution before choosing another one")
                return existing
            key = self.get("delivery_binding")["key"] if returned else cycle["binding"]["key"]
            for dependency in dependencies:
                if set(dependency) != {"history"} or any(item["row"]["cid"] != source_id for item in dependency["history"]):
                    raise ValueError("Review dependencies must belong to this card")
                cycle["keys"].append(self._enqueue(key, dependency)["request_key"])
            replacement = self._enqueue(key, {kind: [entry]})["request_key"]
            self.db.execute("INSERT INTO batch_resolutions VALUES (?, ?, ?, ?, ?)",
                            (request_key, kind, source_id, replacement, json.dumps(base, ensure_ascii=False)))
            if not returned:
                cycle["keys"].append(replacement)
                self._put("changes", cycle)
            return replacement

    def settle_resolutions(self):
        with self.db:
            while self.db.execute("""
                INSERT OR IGNORE INTO applied_entities
                SELECT r.request_key, r.kind, r.source_id, a.accepted_cursor
                FROM batch_resolutions r JOIN applied_entities a
                ON a.request_key = r.replacement_key AND a.kind = r.kind AND a.source_id = r.source_id
            """).rowcount:
                pass

    def prune(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            snapshot = self.get("initial_snapshot")
            if self.get("changes") or self.get("delivery") or snapshot and snapshot["phase"] != "uploaded":
                return 0
            accepted = self.db.execute("SELECT coalesce(max(accepted_cursor), 0) FROM applied_entities").fetchone()[0]
            if accepted > self.get("delivery_cursor", 0):
                return 0
            removed = self.db.execute("DELETE FROM batches WHERE request_key IN (SELECT request_key FROM applied_batches)").rowcount
            self.db.execute("DELETE FROM snapshot_batches WHERE request_key NOT IN (SELECT request_key FROM batches)")
            self.db.execute("DELETE FROM applied_batches WHERE request_key NOT IN (SELECT request_key FROM batches)")
        self.compact()
        return removed

    def compact(self):
        free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        size = self.db.execute("PRAGMA page_size").fetchone()[0]
        if free * size < 1024 * 1024 or free * 4 < pages:
            return
        try:
            self.db.execute("VACUUM")
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise

    def source_ids(self, kind):
        return {row[0] for row in self.db.execute("SELECT source_id FROM checkpoints WHERE kind = ?", (kind,))}

    def batch_applied(self, request_key):
        return self.db.execute("SELECT 1 FROM applied_batches WHERE request_key = ?", (request_key,)).fetchone() is not None

    def entity_applied(self, request_key, kind, source_id):
        return self.db.execute("SELECT 1 FROM applied_entities WHERE request_key = ? AND kind = ? AND source_id = ?",
                               (request_key, kind, source_id)).fetchone() is not None

    def accepted_cursor(self, kind, source_id):
        return self.db.execute("SELECT coalesce(max(accepted_cursor), 0) FROM applied_entities WHERE kind = ? AND source_id = ?",
                               (kind, source_id)).fetchone()[0]

    def complete_entity(self, request_key, kind, source_id, checkpoints, *, cursor=0):
        if type(cursor) is not int or not 0 <= cursor < 2**63:
            raise ValueError("Invalid accepted entity cursor")
        with self.db:
            if self.entity_applied(request_key, kind, source_id):
                return
            if not self.db.execute("SELECT 1 FROM batches WHERE request_key = ? AND receipt IS NOT NULL", (request_key,)).fetchone():
                raise ValueError("The outgoing batch has no receipt")
            for checkpoint in checkpoints:
                self._remember_checkpoint(*checkpoint)
            self.db.execute("INSERT INTO applied_entities VALUES (?, ?, ?, ?)", (request_key, kind, source_id, cursor))

    def complete_batch(self, request_key, checkpoints):
        with self.db:
            if self.batch(request_key)["receipt"] is None:
                raise ValueError("The outgoing batch has no receipt")
            if self.batch_applied(request_key):
                return
            for checkpoint in checkpoints:
                self._remember_checkpoint(*checkpoint)
            self.db.execute("INSERT INTO applied_batches VALUES (?)", (request_key,))

    def delivery_result(self, cursor):
        row = self.db.execute("SELECT result FROM delivery_results WHERE cursor = ?", (cursor,)).fetchone()
        return json.loads(row[0]) if row else None

    def delivery_choice(self, cursor):
        row = self.db.execute("SELECT choice FROM delivery_choices WHERE cursor = ?", (cursor,)).fetchone()
        return json.loads(row[0]) if row else None

    def remember_delivery_choice(self, cursor, choice):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            delivery = self.get("delivery")
            if not delivery or self.delivery_result(cursor) is not None or choice["change"] not in delivery["response"]["changes"] or choice["change"]["cursor"] != cursor:
                raise ValueError("This incoming change is no longer waiting for a choice")
            self.db.execute("INSERT INTO delivery_choices VALUES (?, ?) ON CONFLICT(cursor) DO UPDATE SET choice = excluded.choice",
                            (cursor, json.dumps(choice, ensure_ascii=False)))

    def finish_delivery_change(self, cursor, result, *, checkpoints=(), return_batches=(), review_undo_batch=None):
        with self.db:
            delivery = self.get("delivery")
            if not delivery or cursor not in [change["cursor"] for change in delivery["response"]["changes"]]:
                raise ValueError("Unknown delivered change")
            existing = self.delivery_result(cursor)
            if existing is not None:
                if existing != result:
                    raise ValueError("An applied delivery result cannot change")
                return
            keys = [self._enqueue(self.get("delivery_binding")["key"], batch)["request_key"] for batch in return_batches]
            if keys:
                result = {**result, "source_deletion_keys": keys}
            if review_undo_batch is not None:
                key = self._enqueue(self.get("delivery_binding")["key"], review_undo_batch)["request_key"]
                result = {**result, "review_undo_keys": [key]}
            self.db.execute("INSERT INTO delivery_results VALUES (?, ?)", (cursor, json.dumps(result)))
            for checkpoint in checkpoints:
                self._remember_checkpoint(*checkpoint)

    def note_deletion(self, cursor):
        row = self.db.execute("SELECT change FROM note_deletions WHERE cursor = ?", (cursor,)).fetchone()
        return json.loads(row[0]) if row else None

    def note_creation(self, cursor):
        row = self.db.execute("SELECT intent, output FROM note_creations WHERE cursor = ?", (cursor,)).fetchone()
        return {**json.loads(row[0]), "output": json.loads(row[1]) if row[1] else None} if row else None

    def prepare_note_creation(self, change):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            delivery = self.get("delivery")
            if not delivery or change not in delivery["response"]["changes"] or \
                    change["entity_kind"] != "note" or change["operation"] != "upsert" or not change["payload"].get("creation"):
                raise ValueError("This note creation is no longer pending")
            existing = self.note_creation(change["cursor"])
            if existing:
                if existing["change"] != change:
                    raise ValueError("A pending note creation cannot change")
                return existing
            intent = {"change": change, "nonce": str(uuid.uuid4())}
            self.db.execute("INSERT INTO note_creations(cursor, intent) VALUES (?, ?)", (change["cursor"], json.dumps(intent)))
            return {**intent, "output": None}

    def remember_note_creation_output(self, cursor, output):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            intent = self.note_creation(cursor)
            if not intent or self.delivery_result(cursor) is not None:
                raise ValueError("This note creation is no longer pending")
            if intent["output"] is not None and intent["output"] != output:
                raise ValueError("A prepared note creation result cannot change")
            self.db.execute("UPDATE note_creations SET output = ? WHERE cursor = ?", (json.dumps(output), cursor))

    def prepare_note_deletion(self, change):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            delivery = self.get("delivery")
            if not delivery or change not in delivery["response"]["changes"] or self.delivery_result(change["cursor"]) is not None or \
                    change["entity_kind"] != "note" or change["operation"] != "delete":
                raise ValueError("This note deletion is no longer pending")
            existing = self.note_deletion(change["cursor"])
            if existing:
                if existing != change:
                    raise ValueError("A pending note deletion cannot change")
                return
            self.db.execute("INSERT INTO note_deletions VALUES (?, ?)", (change["cursor"], json.dumps(change)))

    def history_removal(self, cursor):
        row = self.db.execute("SELECT change FROM history_removals WHERE cursor = ?", (cursor,)).fetchone()
        return json.loads(row[0]) if row else None

    def prepare_history_removal(self, change, *, attempt=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            delivery = self.get("delivery")
            if not delivery or change not in delivery["response"]["changes"] or self.delivery_result(change["cursor"]) is not None or \
                    change["entity_kind"] != "card" or change["operation"] != "upsert" or not change["payload"]["anki"].get("removed_history"):
                raise ValueError("This history removal is no longer pending")
            existing = self.history_removal(change["cursor"])
            record = {"change": change, "attempt": attempt}
            if existing == record:
                return
            if attempt is not None:
                choice = self.delivery_choice(change["cursor"])
                if not choice or choice.get("history_removal_attempt") != attempt or choice.get("change") != change or choice.get("prior_removal") != existing:
                    raise ValueError("This history removal attempt has not been confirmed")
            elif existing:
                raise ValueError("A pending history removal cannot change")
            self.db.execute("INSERT INTO history_removals VALUES (?, ?) ON CONFLICT(cursor) DO UPDATE SET change = excluded.change",
                            (change["cursor"], json.dumps(record)))

    def review_undo(self, cursor):
        row = self.db.execute("SELECT attempt FROM review_undos WHERE cursor = ?", (cursor,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending_review_undos(self):
        return self.db.execute("SELECT 1 FROM review_undos LIMIT 1").fetchone() is not None

    def review_undo_output(self, cursor):
        row = self.db.execute("SELECT output FROM review_undo_outputs WHERE cursor = ?", (cursor,)).fetchone()
        return json.loads(row[0]) if row else None

    def remember_review_undo_output(self, choice, output):
        cursor = choice["change"]["cursor"]
        attempt = {key: value for key, value in choice.items() if key != "prior_removal"}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.review_undo(cursor) != attempt or self.delivery_choice(cursor) != choice or self.delivery_result(cursor) is not None:
                raise ValueError("The pending Undo changed before its result was saved")
            existing = self.review_undo_output(cursor)
            if existing is not None:
                if existing != output:
                    raise ValueError("The saved Undo result cannot change")
                return
            self.db.execute("INSERT INTO review_undo_outputs VALUES (?, ?)", (cursor, json.dumps(output)))

    def review_undo_result_choice(self, request_key):
        row = self.db.execute("SELECT choice FROM review_undo_result_choices WHERE request_key = ?", (request_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def remember_review_undo_result_choice(self, request_key, choice, *, previous):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if request_key not in self.review_undo_return_keys() or self.batch_applied(request_key) or \
                    self.review_undo_result_choice(request_key) != previous:
                raise ValueError("The pending Undo result choice changed. Reopen it.")
            self.db.execute("INSERT INTO review_undo_result_choices VALUES (?, ?) ON CONFLICT(request_key) DO UPDATE SET choice = excluded.choice",
                            (request_key, json.dumps(choice)))

    def prepare_review_undo(self, choice):
        change = choice["change"]
        record = {key: value for key, value in choice.items() if key != "prior_removal"}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            delivery = self.get("delivery")
            if not delivery or change not in delivery["response"]["changes"] or self.delivery_result(change["cursor"]) is not None or \
                    change["entity_kind"] != "review" or change["operation"] != "delete" or choice.get("action") != "undo_review" or \
                    not choice.get("attempt") or self.delivery_choice(change["cursor"]) != choice:
                raise ValueError("This review Undo attempt has not been confirmed")
            existing = self.review_undo(change["cursor"])
            if existing == record:
                return
            if choice.get("prior_removal") != existing:
                raise ValueError("The pending review Undo changed")
            self.db.execute("DELETE FROM review_undo_outputs WHERE cursor = ?", (change["cursor"],))
            self.db.execute("INSERT INTO review_undos VALUES (?, ?) ON CONFLICT(cursor) DO UPDATE SET attempt = excluded.attempt",
                            (change["cursor"], json.dumps(record)))

    def prepare_delivery_return(self, key):
        with self.db:
            delivery = self.get("delivery")
            if not delivery:
                raise ValueError("No pending delivery")
            if delivery["return_key"] is None:
                results = [self.delivery_result(change["cursor"]) for change in delivery["response"]["changes"]]
                if any(result is None for result in results):
                    raise ValueError("The delivery has unapplied changes")
                history = [{**result, "delivery_cursor": change["cursor"]}
                           for change, result in zip(delivery["response"]["changes"], results) if "row" in result]
                creations = [result["creation"] for result in results if "creation" in result]
                returned = {"history": history, **({"creations": creations} if creations else {})}
                delivery["return_key"] = self._enqueue(key, returned)["request_key"] if history or creations else ""
                self._put("delivery", delivery)
        return delivery

    def complete_delivery(self, cursor):
        with self.db:
            delivery = self.get("delivery")
            if not delivery or delivery["response"]["cursor"] != cursor or delivery["return_key"] is None:
                raise ValueError("The acknowledged delivery changed or is not applied")
            if delivery["return_key"]:
                if self.batch(delivery["return_key"])["receipt"] is None:
                    raise ValueError("The returned reviews have no receipt")
                self.db.execute("INSERT INTO applied_batches VALUES (?) ON CONFLICT DO NOTHING", (delivery["return_key"],))
            for change in delivery["response"]["changes"]:
                for key in self.delivery_result(change["cursor"]).get("source_deletion_keys", []):
                    if not self.batch_applied(key):
                        raise ValueError("The source deletion has not been accepted")
                for key in self.delivery_result(change["cursor"]).get("review_undo_keys", []):
                    if not self.batch_applied(key):
                        raise ValueError("The review Undo has not been accepted")
            self._put("delivery_cursor", cursor)
            self.db.execute("DELETE FROM settings WHERE key = 'delivery'")
            self.db.execute("DELETE FROM delivery_results")
            self.db.execute("DELETE FROM delivery_choices")
            self.db.execute("DELETE FROM note_deletions")
            self.db.execute("DELETE FROM note_creations")
            self.db.execute("DELETE FROM review_undos")
            self.db.execute("DELETE FROM review_undo_result_choices")

    def review(self, sync_id):
        row = self.db.execute("SELECT payload, source_row, applied FROM reviews WHERE sync_id = ?", (sync_id,)).fetchone()
        return {"payload": json.loads(row[0]), "row": json.loads(row[1]), "applied": bool(row[2])} if row else None

    def review_id_reserved(self, source_id):
        return self.db.execute("SELECT 1 FROM reviews WHERE source_id = ?", (source_id,)).fetchone() is not None

    def review_sync_id(self, source_id):
        row = self.db.execute("SELECT sync_id FROM reviews WHERE source_id = ? AND applied = 1", (source_id,)).fetchone()
        return row[0] if row else None

    def prepare_review(self, sync_id, payload, source_row):
        with self.db:
            existing = self.review(sync_id)
            if existing:
                if existing["payload"] != payload or existing["row"] != source_row:
                    raise ValueError("A review delivery cannot change")
                return existing
            self.db.execute("INSERT INTO reviews(sync_id, payload, source_row, source_id) VALUES (?, ?, ?, ?)",
                            (sync_id, json.dumps(payload), json.dumps(source_row), source_row["id"]))
        return self.review(sync_id)

    def finish_review(self, sync_id):
        with self.db:
            if self.db.execute("UPDATE reviews SET applied = 1 WHERE sync_id = ?", (sync_id,)).rowcount != 1:
                raise ValueError("Unknown delivered review")
