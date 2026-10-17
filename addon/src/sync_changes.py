"""Capture/apply on the collection worker; upload frozen changes on a network worker."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path

from .sync_cards import active_queue, apply_card_change, auxiliary, CardConflict, same_card, same_schedule, validate_card_row
from .sync_notes import apply_note_change, accepted_note_source, NoteConflict
from .sync_reviews import verify_platform
from .sync_snapshot import SyncSnapshot, SnapshotConflict, source_digest
from .sync_state import SyncState


class SyncConflict(SnapshotConflict):
    def __init__(self, conflicts):
        self.conflicts = conflicts
        noun = "record needs" if len(conflicts) == 1 else "records need"
        super().__init__(f"{len(conflicts)} sync {noun} attention. Accepted changes are saved.")


class MappingRequired(SnapshotConflict):
    pass


class SyncChanges:
    def __init__(self, path, client):
        self.path, self.client = path, client

    def _snapshot(self, state, collection=None):
        snapshot = state.get("initial_snapshot")
        cycle = state.get("changes")
        initial = snapshot and snapshot["phase"] == "resolving" and cycle and cycle.get("initial") is True
        if not snapshot or snapshot["api"] != self.client.api or snapshot["phase"] != "uploaded" and not initial:
            raise SnapshotConflict("Finish this server's initial upload before syncing later changes")
        if collection and snapshot["collection_path"] != str(Path(collection.path).resolve()):
            raise SnapshotConflict("The open Anki collection changed")
        binding = {"api": self.client.api, "profile_id": snapshot["profile"]["id"],
                   "key": snapshot["pair_request"]["key"], "collection_path": snapshot["collection_path"]}
        if cycle and cycle["binding"] != binding:
            raise SnapshotConflict("The saved sync belongs to another collection, server or epoch")
        return snapshot, binding

    def capture(self, collection):
        verify_platform(collection)
        with SyncState(self.path) as state:
            snapshot, binding = self._snapshot(state, collection)
            if state.pending_review_undos():
                raise SnapshotConflict("Finish the pending review Undo delivery before capturing Anki changes")
            existing = state.get("changes")
            if existing:
                return existing
            decks = {item.id: collection.decks.get(item.id) for item in collection.decks.all_names_and_ids()}
            roots = snapshot["root_deck_ids"]
            if any(source_id not in decks or decks[source_id].get("dyn") for source_id in roots):
                raise SnapshotConflict("A selected root deck was deleted or changed")
            names = [decks[source_id]["name"] for source_id in roots]
            selected = {source_id for source_id, deck in decks.items() if not deck.get("dyn") and
                        any(deck["name"] == name or deck["name"].startswith(name + "::") for name in names)}
            if selected != set(snapshot["deck_ids"]):
                raise SnapshotConflict("Selected subdecks changed. Deck selection changes need reconciliation before syncing.")
            return state.stage_changes(binding, self._batches(collection, state, snapshot))

    def _batches(self, collection, state, snapshot):
        scanner = SyncSnapshot(self.path, self.client)
        key = snapshot["pair_request"]["key"]
        selected = json.dumps(snapshot["deck_ids"])
        captured_at = datetime.now(timezone.utc).isoformat()
        owned_types = {state.checkpoint("created_note_type", identity)["payload"]["id"]
                       for identity in state.checkpoint_digests("created_note_type")}
        revisions, changed_layouts, seen_cards = {}, set(), set()
        seen_history, removed_history, restored_history = set(), {}, {}
        for kind, entries in scanner._source_entities(collection, selected, captured_at):
            if kind == "cards":
                removed_history = state.missing_history(seen_history)
            singular = {"note_types": "note_type", "notes": "note", "cards": "card", "history": "history"}[kind]
            previous = state.checkpoint_digests("source_" + singular)

            def changed_entries():
                for entry in entries:
                    row = scanner._source_row(kind, entry)
                    source_id = row["id"]
                    different = previous.get(source_id) != source_digest(row)
                    if kind == "cards":
                        seen_cards.add(source_id)
                        if source_id in removed_history:
                            entry = {**entry, "removed_history": removed_history[source_id]}
                            different = True
                        if source_id in restored_history:
                            entry = {**entry, "restored_history": restored_history[source_id]}
                            different = True
                    if kind == "history":
                        seen_history.add(source_id)
                        if previous.get(source_id) == "deleted":
                            removal = state.checkpoint("history_removal", source_id)
                            if not removal:
                                raise SnapshotConflict("Refresh the removed review identity before restoring this history")
                            restored_history.setdefault(row["cid"], []).append({"row": row, "removal_id": int(removal["digest"])})
                            continue
                    if kind == "note_types":
                        revision = state.checkpoint_digest(singular, source_id)
                        revisions[source_id] = int(revision) if revision is not None else 0
                        if different and revision is not None:
                            original = state.checkpoint(singular, source_id)["payload"]
                            if original["type"] != row["type"]:
                                raise SnapshotConflict("Changing between standard and cloze note types needs reconciliation")
                            entry = {**entry, "base_revision": revisions[source_id],
                                     **self._type_maps(state, key, original, row, revisions[source_id])}
                            if source_id not in owned_types:
                                revisions[source_id] += 1
                            if original["flds"] != row["flds"]:
                                changed_layouts.add(source_id)
                    if not different and not (kind == "notes" and row["mid"] in changed_layouts):
                        continue
                    if kind in ("notes", "cards"):
                        base = state.checkpoint_digest(singular, source_id)
                        if base is not None:
                            entry = {**entry, "base": base}
                    if kind == "notes":
                        entry = {**entry, "type_revision": revisions[row["mid"]]}
                    if kind == "history":
                        sync_id = state.review_sync_id(source_id)
                        if sync_id:
                            entry = {**entry, "sync_id": sync_id}
                    yield entry

            yield from scanner._batches(kind, changed_entries(), key)
        missing = {source_id for source_id, digest in state.checkpoint_digests("source_card").items() if digest != "deleted"} - seen_cards
        yield from self.deletion_batches(collection, state, missing, key=key, captured_at=captured_at)

    def deletion_batches(self, collection, state, missing, *, key, captured_at):
        scanner = SyncSnapshot(self.path, self.client)
        if missing:
            encoded = json.dumps(sorted(missing))
            if collection.db.scalar("SELECT count(*) FROM cards WHERE id IN (SELECT value FROM json_each(?))", encoded):
                raise SnapshotConflict("Previously synced cards moved outside the selected decks. Reconciliation is required.")
            known_history = state.checkpoint_digests("source_history")

            def history_entries():
                for row in scanner._rows(collection, "revlog", "cid IN (SELECT value FROM json_each(?))", (encoded,)):
                    if known_history.get(row["id"]) != source_digest(row):
                        entry = {"row": row}
                        sync_id = state.review_sync_id(row["id"])
                        if sync_id:
                            entry["sync_id"] = sync_id
                        yield entry

            def deletions():
                for source_id in sorted(missing):
                    row = state.checkpoint("source_card", source_id)["payload"]
                    yield {"row": row, "base": state.checkpoint_digest("card", source_id),
                           "note_base": state.checkpoint_digest("note", row["nid"]), "deleted": True, "captured_at": captured_at}

            yield from scanner._batches("history", history_entries(), key)
            yield from scanner._batches("cards", deletions(), key)

    @staticmethod
    def _mapping(previous, current):
        mapping = []
        for item in current:
            matches = [index for index, old in enumerate(previous) if item.get("id") is not None and old.get("id") == item["id"]]
            if not matches:
                matches = [index for index, old in enumerate(previous) if old["name"] == item["name"] and
                           (item.get("id") is None or old.get("id") is None)]
            if len(matches) > 1 or (matches and matches[0] in mapping):
                raise MappingRequired("Ambiguous field or template identity")
            mapping.append(matches[0] if matches else None)
        unmatched_old = [item for index, item in enumerate(previous) if index not in mapping]
        unmatched_new = [item for index, item in enumerate(current) if mapping[index] is None]
        if unmatched_old and unmatched_new and any(item.get("id") is None for item in unmatched_old + unmatched_new):
            raise MappingRequired("Renamed fields or templates without stable IDs need explicit reconciliation")
        return mapping

    @staticmethod
    def validate_mapping(previous, current, mapping):
        if not isinstance(mapping, list) or len(mapping) != len(current) or any(
                value is not None and (type(value) is not int or not 0 <= value < len(previous)) for value in mapping):
            raise SnapshotConflict("Invalid field or template mapping")
        retained = [value for value in mapping if value is not None]
        if len(set(retained)) != len(retained):
            raise SnapshotConflict("Each previous field or template can appear only once in a mapping")
        for item, old in zip(current, mapping):
            incoming_id = item.get("id")
            old_id = previous[old].get("id") if old is not None else None
            matches = [index for index, entry in enumerate(previous) if incoming_id is not None and entry.get("id") == incoming_id]
            if incoming_id is not None and old_id is not None and incoming_id != old_id or matches and old not in matches:
                raise SnapshotConflict("A mapping cannot change a stable field or template identity")

    @staticmethod
    def _mapping_context(key, previous, current, revision):
        return {"key": key, "previous": previous, "current": current, "base_revision": revision}

    def _type_maps(self, state, key, previous, current, revision):
        context = self._mapping_context(key, previous, current, revision)
        saved = state.checkpoint("type_mapping", current["id"])
        if saved and saved["digest"] == source_digest(context):
            mappings = saved["payload"]
            for name, group in (("field_map", "flds"), ("template_map", "tmpls")):
                self.validate_mapping(previous[group], current[group], mappings[name])
            return mappings
        try:
            return {"field_map": self._mapping(previous["flds"], current["flds"]),
                    "template_map": self._mapping(previous["tmpls"], current["tmpls"])}
        except MappingRequired as error:
            raise SyncConflict([{"kind": "note_types", "id": current["id"], "status": "mapping_required",
                                 "code": "ambiguous_mapping", "detail": str(error)}]) from error

    def prepare_type_mapping(self, collection, problem):
        verify_platform(collection)
        if problem.get("kind") != "note_types" or problem.get("status") not in ("mapping_required", "rejected"):
            raise SnapshotConflict("Choose a note type waiting for a mapping")
        with SyncState(self.path) as state:
            _, binding = self._snapshot(state, collection)
            if state.get("changes") and problem["status"] != "rejected":
                raise SnapshotConflict("Finish the saved changes before mapping another note type")
            checkpoint = state.checkpoint("note_type", problem["id"])
            current = collection.models.get(problem["id"])
            if not checkpoint or not current or checkpoint["payload"]["type"] != current["type"]:
                raise SnapshotConflict("The note type identity or kind changed. Reconcile it in Anki first.")
            if problem["status"] == "rejected":
                _, entry, _ = self._rejected_problem(state, problem)
                if entry.get("base_revision") != int(checkpoint["digest"]):
                    raise SnapshotConflict("The note type's comparison revision changed. Resume sync before mapping.")
            return copy.deepcopy({"problem": problem, **self._mapping_context(
                binding["key"], checkpoint["payload"], current, int(checkpoint["digest"]))})

    def resolve_type_mapping(self, collection, prepared, mappings):
        current = self.prepare_type_mapping(collection, prepared["problem"])
        if current != prepared:
            raise SnapshotConflict("The note type changed while this confirmation was open. Reopen it before mapping.")
        if not isinstance(mappings, dict) or set(mappings) != {"field_map", "template_map"}:
            raise SnapshotConflict("Choose a mapping for both fields and templates")
        for name, group in (("field_map", "flds"), ("template_map", "tmpls")):
            self.validate_mapping(current["previous"][group], current["current"][group], mappings[name])
        with SyncState(self.path) as state:
            context = {key: value for key, value in current.items() if key != "problem"}
            state.remember_checkpoint("type_mapping", current["current"]["id"], source_digest(context), mappings)
        if current["problem"]["status"] == "rejected":
            return self.retry_rejected(collection, self.prepare_rejected_retry(collection, current["problem"]), source="current")

    def upload(self, *, cancelled=lambda: False, progress=lambda done, total: None):
        with SyncState(self.path) as state:
            _, binding = self._snapshot(state)
            cycle = state.get("changes")
            if not cycle:
                raise SnapshotConflict("Capture Anki changes before uploading")
            for index, request_key in enumerate(cycle["keys"]):
                if cancelled():
                    raise SnapshotConflict("Sync paused. Resume the saved upload when ready.")
                progress(index, len(cycle["keys"]))
                batch = state.batch(request_key)
                receipt = batch["receipt"]
                if receipt is None:
                    receipt = self.client.batch(binding["profile_id"], batch["payload"])
                    state.remember_receipt(request_key, receipt)
                list(self._entries(batch["payload"], receipt))
            progress(len(cycle["keys"]), len(cycle["keys"]))

    @staticmethod
    def _checkpoints(payload, receipt):
        for item, result in zip(payload.get("cards", []), receipt.get("cards", [])):
            if item.get("removed_history", []) != result.get("removed_history", []):
                raise SnapshotConflict("Shirabe has not accepted the exact removed history for this card")
            if item.get("restored_history", []) != result.get("restored_history", []):
                raise SnapshotConflict("Shirabe has not accepted the exact restored history for this card")
        checkpoints = SyncSnapshot._checkpoints(payload, receipt)
        for item, result in zip(payload.get("history", []), receipt.get("history", [])):
            if item.get("sync_id") and item["sync_id"] != result.get("sync_id"):
                raise SnapshotConflict("Shirabe has not accepted the returned review identity")
        accepted = {}
        for row, result in zip(payload.get("notes", []), receipt.get("notes", [])):
            accepted[("source_note", row["id"])] = accepted_note_source(SyncSnapshot._source_row("notes", row), result["note"])
        for result in receipt.get("cards", []):
            if result.get("source_deleted") is not True:
                accepted[("source_card", result["id"])] = result["anki"]["card_after"]
        removed = [("source_history", row["id"], "deleted", row) for result in receipt.get("cards", []) for row in result.get("removed_history", [])]
        removal_ids = []
        for result in receipt.get("cards", []):
            rows = result.get("removed_history", [])
            identities = result.get("anki", {}).get("history_removals", [])
            if not isinstance(identities, list) or len(identities) != len(rows) or any(
                    not isinstance(identity, dict) or identity.get("id") != row["id"] or type(identity.get("removal_id")) is not int or identity["removal_id"] <= 0
                    for row, identity in zip(rows, identities)):
                raise SnapshotConflict("Shirabe has not identified the removed history")
            removal_ids.extend(("history_removal", row["id"], str(identity["removal_id"]), row) for row, identity in zip(rows, identities))
        restored = [("source_history", item["row"]["id"], source_digest(item["row"]), item["row"])
                    for result in receipt.get("cards", []) for item in result.get("restored_history", [])]
        return removed + removal_ids + restored + [(kind, source_id, source_digest(accepted[(kind, source_id)]), accepted[(kind, source_id)])
                if (kind, source_id) in accepted else (kind, source_id, digest, row)
                for kind, source_id, digest, row in checkpoints]

    def _entries(self, payload, receipt):
        if not isinstance(receipt, dict):
            raise SnapshotConflict("Invalid sync receipt")
        cursor = receipt.get("cursor", 0)
        if type(cursor) is not int or not 0 <= cursor < 2**63:
            raise SnapshotConflict("Invalid accepted entity cursor")
        for kind in ("note_types", "notes", "history", "cards"):
            rows, results = payload.get(kind, []), receipt.get(kind, [])
            if not isinstance(results, list) or len(rows) != len(results):
                raise SnapshotConflict(f"Shirabe returned a different number of {kind} records")
            seen = set()
            for row, result in zip(rows, results):
                source_id = SyncSnapshot._source_row(kind, row)["id"]
                if source_id in seen or not isinstance(result, dict) or type(result.get("id")) is not int or result["id"] != source_id or result.get("status") not in ("accepted", "conflict", "rejected"):
                    raise SnapshotConflict(f"Shirabe returned an invalid {kind} identity or status")
                seen.add(source_id)
                applied_cursor = result.get("applied_cursor", cursor)
                if type(applied_cursor) is not int or not 0 <= applied_cursor <= cursor:
                    raise SnapshotConflict("Invalid applied entity cursor")
                checkpoints = self._checkpoints({kind: [row]}, {kind: [result]}) if result["status"] == "accepted" else []
                yield kind, row, result, checkpoints

    def _note_problem(self, state, problem):
        _, binding = self._snapshot(state)
        cycle = state.get("changes")
        key, source_id = problem["request_key"], problem["id"]
        if problem["kind"] != "notes" or not cycle or key not in cycle["keys"] or state.entity_applied(key, "notes", source_id) or state.replacement_for(key, "notes", source_id):
            raise SnapshotConflict("This note is no longer waiting for a choice. Resume sync.")
        batch = state.batch(key)
        result = next((result for kind, row, result, _ in self._entries(batch["payload"], batch["receipt"])
                       if kind == "notes" and result["id"] == source_id), None)
        if not result or result["status"] not in ("accepted", "conflict"):
            raise SnapshotConflict("This rejected record needs reconciliation before choosing field values")
        return binding, result

    def _rejected_problem(self, state, problem):
        snapshot, _ = self._snapshot(state)
        cycle = state.get("changes")
        key, kind, source_id = problem["request_key"], problem["kind"], problem["id"]
        keys = state.deletion_return_keys() if problem.get("direction") == "returning" else cycle["keys"] if cycle else []
        if key not in keys or state.entity_applied(key, kind, source_id) or state.replacement_for(key, kind, source_id):
            raise SnapshotConflict("This rejected record is no longer waiting. Resume sync.")
        batch = state.batch(key)
        found = next(((entry, result) for item_kind, entry, result, _ in self._entries(batch["payload"], batch["receipt"])
                      if item_kind == kind and result["id"] == source_id), None)
        if not found or found[1]["status"] != "rejected":
            raise SnapshotConflict("Only a rejected record can use this retry action")
        if problem.get("direction") == "returning" and (kind != "cards" or found[0].get("deleted") is not True):
            raise SnapshotConflict("Only source deletion returns can use this retry action")
        return snapshot, *found

    def prepare_rejected_retry(self, collection, problem):
        verify_platform(collection)
        with SyncState(self.path) as state:
            self._snapshot(state, collection)
            snapshot, entry, result = self._rejected_problem(state, problem)
            kind, source_id = problem["kind"], result["id"]
            row = SyncSnapshot._source_row(kind, entry)
            if kind == "note_types":
                current = collection.models.get(source_id)
                if not current or current["type"] != row["type"]:
                    raise SnapshotConflict("The note type identity changed. Reconcile it before retrying.")
            elif kind == "notes":
                current = next(SyncSnapshot._rows(collection, "notes", "id = ?", (source_id,)), None)
                if not current or any(current[name] != row[name] for name in ("guid", "mid")):
                    raise SnapshotConflict("The note identity changed or is absent. Reconcile it before retrying.")
            elif kind == "history":
                current = next(SyncSnapshot._rows(collection, "revlog", "id = ?", (source_id,)), None)
                if current is None or current["cid"] != row["cid"]:
                    raise SnapshotConflict("The review history changed or is absent. Reconcile it before retrying.")
            else:
                card = next(SyncSnapshot._rows(collection, "cards", "id = ?", (source_id,), with_last_review=True, with_last_event=True), None)
                note = next(SyncSnapshot._rows(collection, "notes", "id = ?", (row["nid"],)), None)
                checkpoint = state.checkpoint("source_note", row["nid"])
                if not checkpoint:
                    raise SnapshotConflict("Sync this card's note before retrying the card")
                if card and not note or note and any(note[name] != checkpoint["payload"][name] for name in ("guid", "mid")) or \
                        card and any(card[name] != row[name] for name in ("nid", "ord")):
                    raise SnapshotConflict("The card or note identity changed. Reconcile it before retrying.")
                deleted = entry.get("deleted") is True
                if deleted and card or not deleted and card is None:
                    raise SnapshotConflict("The card's source presence changed. Reconcile deletion or restoration before retrying.")
                if card and (card["odid"] or card["did"]) not in snapshot["deck_ids"]:
                    raise SnapshotConflict("The card moved outside the selected decks")
                current = {"card": card, "note": note}
                if card and not deleted:
                    current["history"] = list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (source_id,)))
            current_entry, current_error, mapping_required = None, None, False
            dependencies = []
            try:
                current_entry = self._current_retry_entry(state, collection, kind, entry, current)
                list(SyncSnapshot(self.path, self.client)._batches(kind, [current_entry], snapshot["pair_request"]["key"]))
                if kind == "cards":
                    known = state.card_history(source_id)
                    restored = {item["row"]["id"] for item in current_entry.get("restored_history", [])}
                    history = []
                    for row in current["history"]:
                        if row["id"] not in restored and known.get(row["id"]) != row:
                            item = {"row": row}
                            sync_id = state.review_sync_id(row["id"])
                            if sync_id:
                                item["sync_id"] = sync_id
                            history.append(item)
                    dependencies = list(SyncSnapshot(self.path, self.client)._batches("history", history, snapshot["pair_request"]["key"]))
            except SnapshotConflict as error:
                current_entry, current_error = None, str(error)
                mapping_required = isinstance(error, SyncConflict) and any(
                    item.get("status") == "mapping_required" for item in error.conflicts)
            return copy.deepcopy({"problem": {**problem, "code": result.get("code", "rejected")}, "entry": entry, "current": current,
                                  "current_entry": current_entry, "current_error": current_error, "dependencies": dependencies,
                                  "mapping_required": mapping_required})

    def _current_retry_entry(self, state, collection, kind, entry, current):
        if kind == "notes":
            checkpoint = state.checkpoint("note_type", current["mid"])
            definition = collection.models.get(current["mid"])
            if not checkpoint or not definition:
                raise SnapshotConflict("Sync this note's note type before sending current data")
            fields = current["flds"].split("\x1f")
            if len(fields) != len(checkpoint["payload"]["flds"]) or definition["flds"] != checkpoint["payload"]["flds"]:
                raise SnapshotConflict("Sync this note's current note type layout before sending current data")
            return {**entry, **current, "type_revision": int(checkpoint["digest"])}
        if kind == "note_types":
            checkpoint = state.checkpoint("note_type", current["id"])
            if checkpoint is None and entry.get("base_revision") is None:
                return {**entry, "definition": current}
            if checkpoint is None or int(checkpoint["digest"]) != entry.get("base_revision"):
                raise SnapshotConflict("The note type's comparison revision changed. Reconcile it before sending current data.")
            previous = checkpoint["payload"]
            _, binding = self._snapshot(state, collection)
            return {**entry, "definition": current, **self._type_maps(state, binding["key"], previous, current, int(checkpoint["digest"]))}
        if kind == "history":
            card_checkpoint = state.checkpoint("source_card", current["cid"])
            card = next(SyncSnapshot._rows(collection, "cards", "id = ?", (current["cid"],)), None)
            if card and card_checkpoint:
                source = card_checkpoint["payload"]
                note_checkpoint = state.checkpoint("source_note", source["nid"])
                note = next(SyncSnapshot._rows(collection, "notes", "id = ?", (source["nid"],)), None)
                if any(card[key] != source[key] for key in ("id", "nid", "ord")) or not note_checkpoint or not note or \
                        any(note[key] != note_checkpoint["payload"][key] for key in ("guid", "mid")):
                    raise SnapshotConflict("The review's card identity changed. Reconcile it before sending current history.")
            checkpoint = state.checkpoint("source_history", current["id"])
            sync_id = state.review_sync_id(current["id"])
            delivered = state.review(sync_id) if sync_id else None
            known = ([checkpoint["payload"]] if checkpoint else []) + ([delivered["row"]] if delivered else [])
            if any({key: value for key, value in previous.items() if key != "usn"} !=
                   {key: value for key, value in current.items() if key != "usn"} for previous in known):
                raise SnapshotConflict("This review was already synced with different content. Reconcile its identity instead of replacing the event.")
            return {**entry, "row": current}
        if kind == "cards" and not entry.get("deleted"):
            row = dict(current["card"])
            current_history = {item["id"]: item for item in current["history"]}
            history = {item["id"]: {key: value for key, value in item.items() if key != "usn"} for item in current["history"]}
            restorations = []
            for item in entry.get("restored_history", []):
                previous = item["row"]
                removal = state.checkpoint("history_removal", previous["id"])
                if history.get(previous["id"]) != {key: value for key, value in previous.items() if key != "usn"} or \
                        not removal or removal["digest"] != str(item["removal_id"]):
                    raise SnapshotConflict("The review restoration changed or was undone. Reconcile it before sending current card data.")
                restorations.append({**item, "row": current_history[previous["id"]]})
            known = state.card_history(row["id"])
            if any(history.get(source_id) != {key: value for key, value in previous.items() if key != "usn"}
                   for source_id, previous in known.items()):
                raise SnapshotConflict("Previously synced review history changed or was undone. Reconcile it before sending current card data.")
            for key in state.get("changes", {}).get("keys", []):
                for item in state.batch(key)["payload"].get("history", []):
                    review = item["row"]
                    if review["cid"] == row["id"] and not state.entity_applied(key, "history", review["id"]):
                        raise SnapshotConflict("Finish this card's pending review history before sending current card data.")
            last_review, last_event = row.pop("last_review_id"), row.pop("last_event_id")
            result = {key: value for key, value in entry.items() if key not in ("last_review_id", "last_event_id")}
            result["row"] = row
            if restorations:
                result["restored_history"] = restorations
            if last_review:
                result["last_review_id"] = last_review
            if last_event:
                result["last_event_id"] = last_event
            return result
        raise SnapshotConflict("Changed review history and source deletions need reconciliation. Retry the saved record or leave it pending.")

    def retry_rejected(self, collection, prepared, *, source="saved"):
        current = self.prepare_rejected_retry(collection, prepared["problem"])
        if current != prepared:
            raise SnapshotConflict("Anki changed while this confirmation was open. Reopen it before retrying.")
        if source not in ("saved", "current"):
            raise SnapshotConflict("Choose saved or current Anki data")
        if source == "current" and current["current_entry"] is None:
            raise SnapshotConflict(current["current_error"])
        entry = current["entry"] if source == "saved" else current["current_entry"]
        if source == "saved" and current["problem"]["kind"] == "history" and \
                {key: value for key, value in current["current"].items() if key != "usn"} != \
                {key: value for key, value in entry["row"].items() if key != "usn"}:
            raise SnapshotConflict("The saved review no longer matches Anki. Choose current Anki data to submit the correction.")
        dependencies = current["dependencies"] if source == "current" else []
        if source == "current" and current["problem"]["kind"] == "cards":
            entry = {**entry, "captured_at": datetime.now(timezone.utc).isoformat()}
        problem = current["problem"]
        with SyncState(self.path) as state:
            self._snapshot(state, collection)
            self._rejected_problem(state, problem)
            return state.stage_resolution(problem["request_key"], problem["kind"], problem["id"], entry, {},
                                          returned=problem.get("direction") == "returning", dependencies=dependencies)

    def note_conflict(self, problem):
        with SyncState(self.path) as state:
            binding, result = self._note_problem(state, problem)
        if result["status"] == "conflict":
            return self.client.note_conflict(binding["profile_id"], result["conflict_id"])
        return {"id": None, "key": binding["key"], "resolved": False, "snapshot": result["snapshot"], "note": result["note"]}

    def prepare_note_resolution(self, collection, problem, detail):
        verify_platform(collection)
        with SyncState(self.path) as state:
            self._snapshot(state, collection)
            binding, result = self._note_problem(state, problem)
        if detail.get("key") != binding["key"] or detail.get("id") != result.get("conflict_id") or detail.get("resolved") is not False:
            raise SnapshotConflict("The conflict or sync epoch changed. Refresh the conflict list.")
        remote = detail["note"]
        note = collection.get_note(problem["id"])
        names = [field["name"] for field in note.note_type()["flds"]]
        if remote["anki_id"] != note.id or remote["guid"] != note.guid or remote["anki_type_id"] != note.mid or remote["field_names"] != names:
            raise SnapshotConflict("The note identity or field layout changed. Reconcile the note type first.")
        local = next(SyncSnapshot._rows(collection, "notes", "id = ?", (note.id,)))
        return {"problem": problem, "detail": detail, "local": local}

    def resolve_note(self, collection, prepared, fields, tags):
        current = self.prepare_note_resolution(collection, prepared["problem"], prepared["detail"])
        if current["local"] != prepared["local"]:
            raise SnapshotConflict("The Anki note changed while this chooser was open. Reopen it before applying a choice.")
        detail, local = prepared["detail"], prepared["local"]
        if not isinstance(fields, list) or len(fields) != len(detail["note"]["field_names"]) or not all(isinstance(value, str) for value in fields) or not isinstance(tags, list) or not all(isinstance(tag, str) and tag and not any(character.isspace() for character in tag) for tag in tags):
            raise SnapshotConflict("Choose one source string per field and space-separated tags")
        row = {**local, "flds": "\x1f".join(fields), "tags": " ".join(sorted(set(tags))),
               "base": detail["snapshot"], "type_revision": detail["note"]["type_revision"]}
        if detail["id"] is not None:
            row["conflict_id"] = detail["id"]
        list(SyncSnapshot(self.path, self.client)._batches("notes", [row], detail["key"]))
        base = {"fields": local["flds"].split("\x1f"), "tags": local["tags"].split()}
        problem = prepared["problem"]
        with SyncState(self.path) as state:
            self._note_problem(state, problem)
            return state.stage_resolution(problem["request_key"], "notes", problem["id"], row, base)

    def _card_problem(self, state, problem):
        _, binding = self._snapshot(state)
        cycle = state.get("changes")
        key, source_id = problem["request_key"], problem["id"]
        keys = state.deletion_return_keys() + state.review_undo_return_keys() if problem.get("direction") == "returning" else cycle["keys"] if cycle else []
        if problem["kind"] != "cards" or key not in keys or state.entity_applied(key, "cards", source_id) or state.replacement_for(key, "cards", source_id):
            raise SnapshotConflict("This card is no longer waiting for a choice. Resume sync.")
        batch = state.batch(key)
        result = next((result for kind, row, result, _ in self._entries(batch["payload"], batch["receipt"])
                       if kind == "cards" and result["id"] == source_id), None)
        if not result or result["status"] not in ("accepted", "conflict"):
            raise SnapshotConflict("This rejected record needs reconciliation before choosing card state")
        return binding, result

    def card_conflict(self, problem):
        with SyncState(self.path) as state:
            binding, result = self._card_problem(state, problem)
        if result["status"] == "conflict":
            return self.client.card_conflict(binding["profile_id"], result["conflict_id"])
        return {"id": None, "key": binding["key"], "resolved": False, "snapshot": result["snapshot"],
                "card": result["card"], "anki": result["anki"]}

    def prepare_card_resolution(self, collection, problem, detail):
        if problem.get("code") == "history_changed_before_undo" or detail.get("history_removal"):
            raise SnapshotConflict("Reconcile the removed review history before choosing card state")
        verify_platform(collection)
        with SyncState(self.path) as state:
            self._snapshot(state, collection)
            binding, result = self._card_problem(state, problem)
        if detail.get("key") != binding["key"] or detail.get("id") != result.get("conflict_id") or detail.get("resolved") is not False:
            raise SnapshotConflict("The conflict or sync epoch changed. Refresh the conflict list.")
        source = detail["anki"]
        remote = source["card_after"]
        validate_card_row(remote)
        local = next(SyncSnapshot._rows(collection, "cards", "id = ?", (problem["id"],), with_last_review=True, with_last_event=True), None)
        if local is None:
            raise SnapshotConflict("The source card no longer exists")
        last_review = local.pop("last_review_id")
        last_event = local.pop("last_event_id")
        note = collection.get_note(local["nid"])
        if detail["card"]["anki_id"] != local["id"] or any(local[key] != remote[key] for key in ("id", "nid", "ord")) or note.guid != source["note_guid"] or note.mid != source["anki_type_id"]:
            raise SnapshotConflict("The note or generated card identity changed. Reconcile its identity first.")
        pending_reviews = []
        if result["status"] == "accepted" and source.get("schedule_review") and last_review and last_event == last_review:
            with SyncState(self.path) as state:
                entry = next(item for item in state.batch(problem["request_key"])["payload"]["cards"] if item["row"]["id"] == local["id"])
                known = state.card_history(local["id"])
                if last_review not in known and not any(entry.get(key) for key in ("deleted", "removed_history", "restored_history")):
                    history = list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (local["id"],)))
                    try:
                        self._current_retry_entry(state, collection, "cards", entry,
                                                  {"card": {**local, "last_review_id": last_review, "last_event_id": last_event}, "history": history})
                    except SnapshotConflict:
                        pass
                    else:
                        pending_reviews = [row for row in history if row["id"] not in known]
        return {"problem": problem, "detail": detail, "local": local, "last_review_id": last_review, "last_event_id": last_event,
                "pending_reviews": pending_reviews,
                "schedule_matches": same_schedule(local, remote),
                "decks": {item.id: item.name for item in collection.decks.all_names_and_ids()}}

    def prepare_history_removal(self, collection, problem, detail):
        verify_platform(collection)
        with SyncState(self.path) as state:
            snapshot, _ = self._snapshot(state, collection)
            binding, result = self._card_problem(state, problem)
            if result["status"] != "conflict" or detail.get("key") != binding["key"] or detail.get("id") != result.get("conflict_id") or detail.get("resolved") is not False:
                raise SnapshotConflict("The Undo conflict or sync epoch changed. Refresh the conflict list.")
            entry = next(item for item in state.batch(problem["request_key"])["payload"]["cards"] if item["row"]["id"] == problem["id"])
            rows = entry.get("removed_history", [])
            restored = entry.get("restored_history", [])
            if not rows or detail.get("history_removal", {}).get("rows") != rows or entry.get("deleted"):
                raise SnapshotConflict("The removed review history changed. Reconcile it before choosing a schedule.")
            if detail.get("history_removal", {}).get("restored", []) != restored:
                raise SnapshotConflict("The restored review history changed. Reconcile it before choosing a schedule.")
            source, remote = detail["anki"], detail["anki"]["card_after"]
            validate_card_row(remote)
            local = next(SyncSnapshot._rows(collection, "cards", "id = ?", (problem["id"],)), None)
            if not local:
                raise SnapshotConflict("The source card no longer exists")
            note = collection.get_note(local["nid"])
            if detail["card"]["anki_id"] != local["id"] or any(local[key] != remote[key] for key in ("id", "nid", "ord")) or \
                    note.guid != source["note_guid"] or note.mid != source["anki_type_id"]:
                raise SnapshotConflict("The note or generated card identity changed. Reconcile its identity first.")
            if any((row["odid"] or row["did"]) not in snapshot["deck_ids"] for row in (local, remote)):
                raise SnapshotConflict("The card is outside the selected decks")
            history = list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (local["id"],)))
            removed_ids = {row["id"] for row in rows}
            core = lambda row: {key: value for key, value in row.items() if key != "usn"}
            expected = {source_id: core(row) for source_id, row in state.card_history(local["id"]).items() if source_id not in removed_ids}
            for item in restored:
                row = item["row"]
                removal = state.checkpoint("history_removal", row["id"])
                if row["cid"] != local["id"] or row["id"] in removed_ids or not removal or \
                        removal["digest"] != str(item["removal_id"]) or core(removal["payload"]) != core(row):
                    raise SnapshotConflict("The restored review identity changed. Reconcile it before choosing a schedule.")
                expected[row["id"]] = core(row)
            if {row["id"]: core(row) for row in history} != expected:
                raise SnapshotConflict("Anki review history changed after this Undo. Reconcile it before choosing a schedule.")
        return {"problem": problem, "detail": detail, "entry": entry, "local": local, "history": history}

    def resolve_history_removal(self, collection, prepared, *, schedule):
        current = self.prepare_history_removal(collection, prepared["problem"], prepared["detail"])
        if current != prepared:
            raise SnapshotConflict("Anki changed while this Undo choice was open. Reopen it before confirming.")
        if schedule not in ("anki", "shirabe"):
            raise SnapshotConflict("Choose the Anki or Shirabe schedule")
        problem, detail = current["problem"], current["detail"]
        entry = {**current["entry"], "row": current["local"] if schedule == "anki" else detail["anki"]["card_after"],
                 "base": detail["snapshot"], "conflict_id": detail["id"], "captured_at": datetime.now(timezone.utc).isoformat()}
        list(SyncSnapshot(self.path, self.client)._batches("cards", [entry], detail["key"]))
        with SyncState(self.path) as state:
            self._snapshot(state, collection)
            self._card_problem(state, problem)
            return state.stage_resolution(problem["request_key"], "cards", problem["id"], entry,
                                          {"card": current["local"], "removed_history": entry["removed_history"],
                                           "restored_history": entry.get("restored_history", [])},
                                          returned=problem.get("direction") == "returning")

    def prepare_deletion_resolution(self, collection, problem, detail):
        verify_platform(collection)
        with SyncState(self.path) as state:
            snapshot, _ = self._snapshot(state, collection)
            binding, result = self._card_problem(state, problem)
            if result["status"] != "conflict" or detail.get("key") != binding["key"] or detail.get("id") != result.get("conflict_id") or detail.get("resolved") is not False:
                raise SnapshotConflict("The deletion conflict or sync epoch changed. Refresh the conflict list.")
            entry = next(item for item in state.batch(problem["request_key"])["payload"]["cards"] if item["row"]["id"] == problem["id"])
            if entry.get("deleted") is not True or not isinstance(detail.get("deletion"), dict):
                raise SnapshotConflict("This conflict is not a source deletion")
            row, source = entry["row"], detail["anki"]
            remote = source["card_after"]
            validate_card_row(remote)
            note = state.checkpoint("note", row["nid"])
            if not note or detail["card"]["anki_id"] != row["id"] or any(remote[key] != row[key] for key in ("id", "nid", "ord")) or \
                    source["note_guid"] != note["payload"].get("guid") or source["anki_type_id"] != note["payload"].get("anki_type_id"):
                raise SnapshotConflict("The deleted note or card identity changed. Reconcile its identity first.")
            if (remote["odid"] or remote["did"]) not in snapshot["deck_ids"]:
                raise SnapshotConflict("The current Shirabe card is outside the selected decks")
        local = next(SyncSnapshot._rows(collection, "cards", "id = ?", (row["id"],)), None)
        local_note = next(SyncSnapshot._rows(collection, "notes", "id = ?", (row["nid"],)), None)
        if local and any(local[key] != row[key] for key in ("id", "nid", "ord")) or \
                local_note and (local_note["guid"] != source["note_guid"] or local_note["mid"] != source["anki_type_id"]):
            raise SnapshotConflict("The Anki identity changed since the source deletion")
        return {"problem": problem, "detail": detail, "entry": entry, "local": local, "local_note": local_note}

    def resolve_deletion(self, collection, prepared):
        current = self.prepare_deletion_resolution(collection, prepared["problem"], prepared["detail"])
        if current != prepared:
            raise SnapshotConflict("The Anki card changed while this chooser was open. Reopen it before confirming deletion.")
        detail, problem = current["detail"], current["problem"]
        entry = {**current["entry"], "base": detail["snapshot"], "note_base": detail["deletion"]["note_base"], "conflict_id": detail["id"]}
        list(SyncSnapshot(self.path, self.client)._batches("cards", [entry], detail["key"]))
        with SyncState(self.path) as state:
            self._snapshot(state, collection)
            self._card_problem(state, problem)
            return state.stage_resolution(problem["request_key"], "cards", problem["id"], entry, {}, returned=problem.get("direction") == "returning")

    def resolve_card(self, collection, prepared, choices):
        current = self.prepare_card_resolution(collection, prepared["problem"], prepared["detail"])
        if any(current[key] != prepared[key] for key in ("local", "last_review_id", "last_event_id", "pending_reviews")):
            raise SnapshotConflict("The Anki card changed while this chooser was open. Reopen it before applying a choice.")
        if not current["schedule_matches"] and not current["pending_reviews"]:
            raise SnapshotConflict("Reconcile review history before choosing between different card schedules")
        if not isinstance(choices, dict) or set(choices) != {"deck", "lifecycle", "properties"} or any(side not in ("anki", "shirabe") for side in choices.values()):
            raise SnapshotConflict("Choose Anki or Shirabe for each card-state dimension")
        detail, local = prepared["detail"], prepared["local"]
        sources = {"anki": local, "shirabe": detail["anki"]["card_after"]}
        properties = auxiliary(sources[choices["properties"]])
        data = {**properties["data"], **{key: value for key, value in json.loads(local["data"] or "{}").items() if key in ("s", "d", "lrt")}}
        row = {**local, "did": sources[choices["deck"]]["did"],
               "queue": min(sources[choices["lifecycle"]]["queue"], 0) or active_queue(local),
               "flags": properties["flags"], "data": json.dumps(data)}
        entry = {"row": row, "base": detail["snapshot"], "captured_at": datetime.now(timezone.utc).isoformat()}
        if prepared["last_review_id"]:
            entry["last_review_id"] = prepared["last_review_id"]
        if prepared["last_event_id"]:
            entry["last_event_id"] = prepared["last_event_id"]
        if detail["id"] is not None:
            entry["conflict_id"] = detail["id"]
        list(SyncSnapshot(self.path, self.client)._batches("cards", [entry], detail["key"]))
        problem = prepared["problem"]
        with SyncState(self.path) as state:
            snapshot, _ = self._snapshot(state, collection)
            self._card_problem(state, problem)
            if (row["odid"] or row["did"]) not in snapshot["deck_ids"]:
                raise SnapshotConflict("The chosen card is outside the selected decks")
            dependencies = list(SyncSnapshot(self.path, self.client)._batches("history",
                [{"row": review} for review in current["pending_reviews"]], detail["key"]))
            base = {"card": local, "new_reviews": current["pending_reviews"]} if dependencies else local
            return state.stage_resolution(problem["request_key"], "cards", problem["id"], entry, base, dependencies=dependencies)

    def apply(self, collection):
        verify_platform(collection)
        with SyncState(self.path) as state:
            self._snapshot(state, collection)
            cycle = state.get("changes")
            if not cycle:
                raise SnapshotConflict("Capture Anki changes before applying replies")
            for request_key in cycle["keys"]:
                batch = state.batch(request_key)
                if batch["receipt"] is None:
                    raise SnapshotConflict("Upload every saved batch before applying replies")
                list(self._entries(batch["payload"], batch["receipt"]))
            conflicts = []
            for request_key in cycle["keys"]:
                if state.batch_applied(request_key):
                    continue
                batch = state.batch(request_key)
                for kind, row, result, checkpoints in self._entries(batch["payload"], batch["receipt"]):
                    if state.entity_applied(request_key, kind, result["id"]) or state.replacement_for(request_key, kind, result["id"]):
                        continue
                    problem = {"request_key": request_key, "kind": kind, "id": result["id"],
                               "status": result["status"], "code": result.get("code", result["status"])}
                    if kind == "cards" and row.get("deleted"):
                        problem["operation"] = "delete"
                    if result["status"] != "accepted":
                        conflicts.append(problem)
                        continue
                    try:
                        if kind == "notes":
                            base = state.resolution_base(request_key) or {"fields": row["flds"].split("\x1f"), "tags": row["tags"].split()}
                            apply_note_change(collection, {"base": base,
                                                           "note": result["note"]})
                        elif kind == "cards":
                            if row.get("deleted"):
                                if result.get("source_deleted") is not True:
                                    raise CardConflict("Shirabe has not accepted this source deletion")
                                state.complete_entity(request_key, kind, result["id"], checkpoints, cursor=batch["receipt"].get("cursor", 0))
                                continue
                            base = state.resolution_base(request_key)
                            if base:
                                if "card" in base:
                                    if base.get("new_reviews"):
                                        if row.get("removed_history") or row.get("restored_history") or row.get("last_event_id") != row.get("last_review_id") or \
                                                row.get("last_review_id") not in {review["id"] for review in base["new_reviews"]} or \
                                                any(review["cid"] != row["row"]["id"] for review in base["new_reviews"]):
                                            raise CardConflict("The new review reconciliation no longer matches its captured events")
                                    else:
                                        if not row.get("removed_history") or row["removed_history"] != base.get("removed_history"):
                                            raise CardConflict("The confirmed Undo no longer matches its removed events")
                                        if row.get("restored_history", []) != base.get("restored_history", []):
                                            raise CardConflict("The confirmed history no longer matches its restored events")
                                    base = base["card"]
                                elif not same_schedule(base, result["anki"]["card_after"]):
                                    raise CardConflict("Reconcile review history before applying a changed card schedule")
                                result = {**result, "anki": {**result["anki"], "card_before": base}}
                            written = apply_card_change(collection, result)
                            if same_card(written, result["anki"]["card_after"]):
                                checkpoints = [checkpoint for checkpoint in checkpoints if checkpoint[0] != "source_card"]
                                digest = source_digest(written) if same_card(result["anki"]["card_before"], result["anki"]["card_after"]) else "unreported"
                                checkpoints.append(("source_card", written["id"], digest, written))
                    except (NoteConflict, CardConflict) as error:
                        conflicts.append({**problem, "status": "local_conflict", "code": str(error)})
                        continue
                    state.complete_entity(request_key, kind, result["id"], checkpoints, cursor=result.get("applied_cursor", batch["receipt"].get("cursor", 0)))
            state.settle_resolutions()
            for request_key in cycle["keys"]:
                batch = state.batch(request_key)
                if all(state.entity_applied(request_key, kind, result["id"]) for kind, _, result, _ in self._entries(batch["payload"], batch["receipt"])):
                    state.complete_batch(request_key, [])
            if conflicts:
                raise SyncConflict(conflicts)
            state.complete_changes()
