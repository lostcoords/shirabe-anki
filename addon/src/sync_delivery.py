"""Receive/acknowledge on a network worker; apply on Anki's collection worker."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .sync_notes import apply_note_change, accepted_note_source, NoteConflict
from .sync_note_deletions import apply_note_deletion, clear_note_deletion_receipt, ensure_note_absent, note_deletion_cards
from .sync_note_creations import apply_note_creation
from .sync_changes import SyncChanges
from .sync_cards import active_queue, apply_card_change, auxiliary, CardConflict, same_card, same_schedule, validate_card_row
from .sync_review_undos import ReviewUndos
from .sync_reviews import apply_review, canonical_review_applied, card_row, history_row, ReviewConflict, STATE_COLUMNS, validate_history_change, verify_platform
from .sync_state import SyncState
from .sync_snapshot import SyncSnapshot, source_digest


class DeliveryConflict(Exception):
    def __init__(self, message, conflicts=()):
        super().__init__(message)
        self.conflicts = list(conflicts)


class SyncDelivery(ReviewUndos):
    def __init__(self, path, client, *, profile_id, key):
        self.path, self.client = path, client
        self.profile_id, self.key = profile_id, key
        binding = {"api": client.api, "profile_id": profile_id, "key": key}
        with SyncState(path) as state:
            try:
                state.bind_delivery(binding)
            except ValueError as error:
                raise DeliveryConflict(str(error)) from None

    def receive(self):
        with SyncState(self.path) as state:
            pending = state.get("delivery")
            if pending:
                return pending["response"]
            cursor = state.get("delivery_cursor", 0)
        response = self.client.deliver(self.profile_id, key=self.key, cursor=cursor, limit=200)
        self._validate_page(cursor, response)
        with SyncState(self.path) as state:
            state.remember_delivery(cursor, response)
        return response

    def apply(self, collection):
        verify_platform(collection)
        with SyncState(self.path) as state:
            delivery = state.get("delivery")
            if not delivery:
                raise DeliveryConflict("Receive a delivery before applying changes")
            changes = delivery["response"]["changes"]
            if any(not (change["operation"] == "upsert" and change["entity_kind"] in ("note", "card", "review") or
                        change["operation"] == "delete" and (change["entity_kind"] in ("note", "review") or
                            change["entity_kind"] == "card" and change["payload"].get("source_deleted") is True)) for change in changes):
                raise DeliveryConflict("This delivery includes a change the add-on cannot apply yet")
            state.begin_delivery()
            conflicts, blocked = [], set()
            for change in changes:
                creation = change["entity_kind"] == "note" and change["operation"] == "upsert" and "creation" in change["payload"]
                deletion = change["entity_kind"] == "note" and change["operation"] == "delete"
                if state.delivery_result(change["cursor"]) is not None and not creation:
                    if deletion:
                        clear_note_deletion_receipt(collection, change)
                    continue
                kind = change["entity_kind"]
                source_id = change["payload"]["anki_card_id"] if kind == "review" else change["payload"][kind]["anki_id"]
                identity = ("creation", change["entity_id"]) if creation else ("note" if kind == "note" else "card", source_id)
                if identity in blocked:
                    continue
                try:
                    self._apply_change(collection, state, change)
                    if deletion:
                        clear_note_deletion_receipt(collection, change)
                except (NoteConflict, CardConflict, ReviewConflict, DeliveryConflict) as error:
                    conflicts.append({"direction": "incoming", "cursor": change["cursor"], "id": source_id, "operation": change["operation"],
                                      "kind": ("review_undos" if change["operation"] == "delete" else "history") if kind == "review" else kind + "s",
                                      "status": "local_conflict", "code": str(error)})
                    blocked.add(identity)
            if conflicts:
                noun = "record needs" if len(conflicts) == 1 else "records need"
                raise DeliveryConflict(f"{len(conflicts)} incoming sync {noun} attention. Applied changes are saved.", conflicts)
            state.prepare_delivery_return(self.key)

    def _apply_change(self, collection, state, change):
        payload, kind = change["payload"], change["entity_kind"]
        if kind == "note" and change["operation"] == "upsert" and "creation" in payload:
            apply_note_creation(collection, state, change, self._deletion_decks(collection, state))
            return
        if kind == "review" and change["operation"] == "delete":
            self._deletion_snapshot(collection, state)
            choice = state.delivery_choice(change["cursor"])
            if not choice:
                raise DeliveryConflict("This review has an Undo request. Open its chooser to confirm or withdraw it.")
            if choice.get("action") == "undo_review":
                self._apply_review_undo(collection, state, change, choice)
                return
            if choice != {"change": change, "action": "withdraw_review_undo"}:
                raise DeliveryConflict("The saved review Undo choice changed")
            started = state.review_undo(change["cursor"])
            if started and history_row(collection, started["row"]["id"]) is None:
                raise DeliveryConflict("This Undo removed history after withdrawal was chosen. Reconcile its result first.")
            state.finish_delivery_change(change["cursor"], {"kept_review": change["cursor"]})
            return
        if kind == "note" and change["operation"] == "delete":
            choice = state.delivery_choice(change["cursor"])
            if choice:
                self._deletion_snapshot(collection, state)
                if choice.get("change") != change or choice.get("action") != "keep_note":
                    raise DeliveryConflict("The saved note deletion choice changed")
                state.finish_delivery_change(change["cursor"], {"kept_note": change["cursor"]})
                return
            selected = self._deletion_decks(collection, state)
            missing = self._deletion_sources(state, change)
            if not missing:
                state.finish_delivery_change(change["cursor"], {})
                return
            if len(missing) != len(payload["cards"]):
                raise DeliveryConflict("Some requested cards already have accepted source deletions. Reconcile before deleting again.")
            apply_note_deletion(collection, state, change, selected)
            self._finish_note_deletion(collection, state, change)
            return
        if kind in ("note", "card") and change["cursor"] <= state.accepted_cursor(kind + "s", payload[kind]["anki_id"]):
            state.finish_delivery_change(change["cursor"], {})
            return
        if change["operation"] == "delete":
            raise DeliveryConflict("Resume the source deletion upload before acknowledging its tombstone")
        if kind == "note":
            source_id = payload["note"]["anki_id"]
            row = next(SyncSnapshot._rows(collection, "notes", "id = ?", (source_id,)), None)
            if row is None:
                raise DeliveryConflict("The source note no longer exists")
            source = accepted_note_source(row, payload["note"])
            choice = state.delivery_choice(change["cursor"])
            if choice:
                if choice["change"] != change:
                    raise DeliveryConflict("The incoming change differs from the saved choice")
                local = choice["local"]
                apply_note_change(collection, {"base": {"fields": local["flds"].split("\x1f"), "tags": local["tags"].split()}, "note": choice["note"]})
            else:
                apply_note_change(collection, payload)
            checkpoints = [("note", source_id, payload["snapshot"], payload["note"]),
                           ("source_note", source_id, source_digest(source), source)]
            result = {}
        elif kind == "card":
            removed_history = payload["anki"].get("removed_history", [])
            removal_ids = payload["anki"].get("history_removals", [])
            entries = self._history_removal_entries(payload)
            choice = state.delivery_choice(change["cursor"])
            def prepare_removal(history_exists):
                started = state.history_removal(change["cursor"])
                attempt = choice.get("history_removal_attempt") if choice else None
                if started == {"change": change, "attempt": attempt}:
                    if history_exists:
                        raise DeliveryConflict("An interrupted history removal has live reviews. Reconcile before removing them again.")
                elif attempt:
                    if card_row(collection, payload["card"]["anki_id"]) != choice["local"] or self._history_digest(collection, payload["card"]["anki_id"]) != choice["history_digest"]:
                        raise DeliveryConflict("The Anki card or history changed after confirmation. Reopen the Undo chooser.")
                elif started:
                    raise DeliveryConflict("The pending history removal changed")
                state.prepare_history_removal(change, attempt=attempt)

            history = {"removed_history": entries,
                       "restored_history": payload["anki"].get("restored_history", []), "before_history_removal": prepare_removal}
            if choice:
                if choice["change"] != change:
                    raise DeliveryConflict("The incoming change differs from the saved choice")
                self._selected_card(collection, state, choice["card"])
                apply_card_change(collection, {**payload, "anki": {**payload["anki"],
                    "card_before": choice["local"], "card_after": choice["card"]}}, **history)
            else:
                apply_card_change(collection, payload, **history)
            source = payload["anki"]["card_after"]
            checkpoints = [("card", payload["card"]["anki_id"], payload["snapshot"], payload["card"]),
                           ("source_card", source["id"], source_digest(source), source),
                           SyncSnapshot.review_checkpoint(source["id"], payload["snapshot"], payload["anki"], reset_generation=payload["card"].get("reset_generation"))]
            checkpoints.extend(("source_history", row["id"], "deleted", row) for row in removed_history)
            checkpoints.extend(("history_removal", row["id"], str(identity["removal_id"]), row) for row, identity in zip(removed_history, removal_ids))
            for item in payload["anki"].get("restored_history", []):
                row = history_row(collection, item["row"]["id"])
                checkpoints.append(("source_history", row["id"], source_digest(row), row))
            result = {}
        else:
            result = apply_review(collection, state, payload)
            source = payload["anki"]["card_after"]
            checkpoints = [("card", source["id"], payload["snapshot"], payload["card"]),
                           ("source_card", source["id"], source_digest(source), source),
                           SyncSnapshot.review_checkpoint(source["id"], payload["snapshot"], {**payload["anki"],
                               "schedule_review": {"sync_id": payload["sync_id"], "reviewed_at": payload["reviewed_at"]}}, reset_generation=payload["card"].get("reset_generation"))]
            current = card_row(collection, source["id"])
            if canonical_review_applied(state, current, payload) or not same_schedule(current, source):
                checkpoints = []
        state.finish_delivery_change(change["cursor"], result, checkpoints=checkpoints)

    def _deletion_sources(self, state, change):
        expected = note_deletion_cards(change)
        missing = []
        for source_id, entry in expected.items():
            source = state.checkpoint("source_card", source_id)
            if not source or any(source["payload"][key] != entry["row"][key] for key in ("id", "nid", "ord")) or \
                    not state.checkpoint_digest("card", source_id) or not state.checkpoint_digest("note", entry["row"]["nid"]):
                raise DeliveryConflict("Reconcile the requested card identities before confirming deletion")
            if source["digest"] != "deleted" or state.accepted_cursor("cards", source_id) <= change["cursor"]:
                missing.append(source_id)
        return missing

    def _finish_note_deletion(self, collection, state, change):
        ensure_note_absent(collection, change)
        batches = SyncChanges(self.path, self.client).deletion_batches(collection, state,
            self._deletion_sources(state, change), key=self.key, captured_at=datetime.now(timezone.utc).isoformat())
        state.finish_delivery_change(change["cursor"], {}, return_batches=batches)

    def _deletion_snapshot(self, collection, state):
        snapshot = state.get("initial_snapshot")
        if not snapshot or snapshot["phase"] != "uploaded" or snapshot["api"] != self.client.api or snapshot["profile"]["id"] != self.profile_id or snapshot["pair_request"]["key"] != self.key or snapshot["collection_path"] != str(Path(collection.path).resolve()):
            raise DeliveryConflict("The linked collection, server or sync epoch changed")
        return snapshot

    def _deletion_decks(self, collection, state):
        snapshot = self._deletion_snapshot(collection, state)
        if state.get("changes"):
            raise DeliveryConflict("Finish the saved outgoing sync before applying a deletion")
        decks = {item.id: collection.decks.get(item.id) for item in collection.decks.all_names_and_ids()}
        roots = snapshot["root_deck_ids"]
        if not roots or any(source_id not in decks or decks[source_id].get("dyn") for source_id in roots):
            raise DeliveryConflict("The selected root decks changed before deletion")
        names = [decks[source_id]["name"] for source_id in roots]
        selected = {source_id for source_id, deck in decks.items() if not deck.get("dyn") and
                    any(deck["name"] == name or deck["name"].startswith(name + "::") for name in names)}
        if selected != set(snapshot["deck_ids"]):
            raise DeliveryConflict("Reconcile changed deck selection before applying a deletion")
        return selected

    def _pending_change(self, state, problem, kind, *, operation="upsert"):
        delivery = state.get("delivery")
        cursor = problem.get("cursor")
        if problem.get("direction") != "incoming" or problem.get("kind") != kind + "s" or type(cursor) is not int or not delivery or state.delivery_result(cursor) is not None:
            raise DeliveryConflict(f"This incoming {kind} is no longer waiting for a choice. Resume sync.")
        change = next((item for item in delivery["response"]["changes"] if item["cursor"] == cursor), None)
        if not change or change["entity_kind"] != kind or change["operation"] != operation or change["payload"][kind]["anki_id"] != problem["id"]:
            raise DeliveryConflict(f"The incoming {kind} identity changed")
        return change

    def _pending_review_undo(self, state, problem):
        delivery = state.get("delivery")
        cursor = problem.get("cursor")
        if problem.get("direction") != "incoming" or problem.get("kind") != "review_undos" or type(cursor) is not int or not delivery or state.delivery_result(cursor) is not None:
            raise DeliveryConflict("This Undo request is no longer waiting for a choice. Resume sync.")
        change = next((item for item in delivery["response"]["changes"] if item["cursor"] == cursor), None)
        if not change or change["entity_kind"] != "review" or change["operation"] != "delete" or change["payload"]["anki_card_id"] != problem["id"]:
            raise DeliveryConflict("The incoming Undo request identity changed")
        return change

    def prepare_note_deletion_resolution(self, collection, problem):
        verify_platform(collection)
        with SyncState(self.path) as state:
            self._deletion_snapshot(collection, state)
            change = self._pending_change(state, problem, "note", operation="delete")
            attempted = state.note_deletion(change["cursor"]) is not None
        remote = change["payload"]["note"]
        local = next(SyncSnapshot._rows(collection, "notes", "id = ?", (problem["id"],)), None)
        if local is None:
            ensure_note_absent(collection, change)
            return {"problem": problem, "change": change, "local": None, "attempted": attempted,
                    "fields": [], "card_count": 0}
        if local["guid"] != remote.get("guid"):
            raise DeliveryConflict("The note identity changed. Reconcile its identity before withdrawing deletion.")
        note = collection.get_note(problem["id"])
        return {"problem": problem, "change": change, "local": local, "attempted": attempted,
                "fields": [field["name"] for field in note.note_type()["flds"]], "card_count": len(note.cards())}

    def keep_note(self, collection, prepared):
        current = self.prepare_note_deletion_resolution(collection, prepared["problem"])
        if current["local"] is None:
            raise DeliveryConflict("The note is absent in Anki and cannot be reported as kept")
        if current != prepared:
            raise DeliveryConflict("The Anki note changed while this chooser was open. Reopen it before choosing to keep it.")
        with SyncState(self.path) as state:
            self._deletion_snapshot(collection, state)
            self._pending_change(state, prepared["problem"], "note", operation="delete")
            state.remember_delivery_choice(prepared["problem"]["cursor"], {"change": current["change"], "action": "keep_note"})

    def confirm_note_absence(self, collection, prepared):
        current = self.prepare_note_deletion_resolution(collection, prepared["problem"])
        if current != prepared or current["local"] is not None:
            raise DeliveryConflict("The Anki note changed while this chooser was open. Reopen it before confirming deletion.")
        with SyncState(self.path) as state:
            self._deletion_decks(collection, state)
            change = self._pending_change(state, prepared["problem"], "note", operation="delete")
            self._finish_note_deletion(collection, state, change)

    def prepare_note_resolution(self, collection, problem):
        verify_platform(collection)
        with SyncState(self.path) as state:
            change = self._pending_change(state, problem, "note")
        remote = change["payload"]["note"]
        note = collection.get_note(problem["id"])
        names = [field["name"] for field in note.note_type()["flds"]]
        if remote["guid"] != note.guid or remote["anki_type_id"] != note.mid or remote["field_names"] != names:
            raise DeliveryConflict("The note identity or field layout changed. Reconcile the note type first.")
        local = next(SyncSnapshot._rows(collection, "notes", "id = ?", (note.id,)))
        return {"problem": problem, "change": change, "local": local, "detail": {"note": remote}}

    def resolve_note(self, collection, prepared, fields, tags):
        current = self.prepare_note_resolution(collection, prepared["problem"])
        if current["local"] != prepared["local"] or current["change"] != prepared["change"]:
            raise DeliveryConflict("The note changed while this chooser was open. Reopen it before applying a choice.")
        remote = current["detail"]["note"]
        if not isinstance(fields, list) or len(fields) != len(remote["field_names"]) or not all(isinstance(value, str) for value in fields) or not isinstance(tags, list) or not all(isinstance(tag, str) and tag and not any(character.isspace() for character in tag) for tag in tags):
            raise DeliveryConflict("Choose one source string per field and space-separated tags")
        row = {**current["local"], "flds": "\x1f".join(fields), "tags": " ".join(sorted(set(tags)))}
        list(SyncSnapshot(self.path, self.client)._batches("notes", [row], self.key))
        choice = {"change": current["change"], "local": current["local"], "note": {**remote, "fields": fields, "tags": sorted(set(tags))}}
        with SyncState(self.path) as state:
            self._pending_change(state, prepared["problem"], "note")
            state.remember_delivery_choice(prepared["problem"]["cursor"], choice)

    def _selected_card(self, collection, state, row):
        snapshot = state.get("initial_snapshot")
        if not snapshot or snapshot["phase"] != "uploaded" or snapshot["api"] != self.client.api or snapshot["profile"]["id"] != self.profile_id or snapshot["pair_request"]["key"] != self.key or snapshot["collection_path"] != str(Path(collection.path).resolve()):
            raise DeliveryConflict("The linked collection, server or sync epoch changed")
        if (row["odid"] or row["did"]) not in snapshot["deck_ids"]:
            raise DeliveryConflict("The chosen card is outside the selected decks")

    @staticmethod
    def _history_removal_entries(payload):
        rows = payload["anki"].get("removed_history", [])
        identities = payload["anki"].get("history_removals", [])
        if not isinstance(rows, list) or not isinstance(identities, list) or len(identities) != len(rows) or any(
                not isinstance(row, dict) or not isinstance(identity, dict) or identity.get("id") != row.get("id") or type(identity.get("removal_id")) is not int or identity["removal_id"] <= 0
                for row, identity in zip(rows, identities)):
            raise DeliveryConflict("Shirabe has not identified the removed history")
        if any(row.get("cid") != payload["card"]["anki_id"] or type(row.get("id")) is not int or row["id"] <= 0 for row in rows):
            raise DeliveryConflict("The removed review identity does not match this card")
        return [{"row": row, "removal_id": identity["removal_id"]} for row, identity in zip(rows, identities)]

    @staticmethod
    def _history_digest(collection, card_id):
        return source_digest(list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (card_id,))))

    def prepare_card_resolution(self, collection, problem):
        verify_platform(collection)
        with SyncState(self.path) as state:
            change = self._pending_change(state, problem, "card")
            source = change["payload"]["anki"]
            remote = source["card_after"]
            validate_card_row(source["card_before"])
            validate_card_row(remote)
            if any(source["card_before"][key] != remote[key] for key in ("id", "nid", "ord")):
                raise DeliveryConflict("The incoming card identity changed")
            self._selected_card(collection, state, remote)
        local = next(SyncSnapshot._rows(collection, "cards", "id = ?", (problem["id"],), with_last_review=True), None)
        if local is None:
            raise DeliveryConflict("The source card no longer exists")
        last_review = local.pop("last_review_id")
        note = collection.get_note(local["nid"])
        if any(local[key] != remote[key] for key in ("id", "nid", "ord")) or note.guid != source["note_guid"] or note.mid != source["anki_type_id"]:
            raise DeliveryConflict("The note or generated card identity changed. Reconcile its identity first.")
        prepared = {"problem": problem, "change": change, "detail": change["payload"], "local": local,
                "last_review_id": last_review, "schedule_matches": same_schedule(local, remote),
                "decks": {item.id: item.name for item in collection.decks.all_names_and_ids()}}
        if source.get("removed_history"):
            entries = self._history_removal_entries(change["payload"])
            validate_history_change(collection, remote["id"], entries, source.get("restored_history", []))
            with SyncState(self.path) as state:
                self._selected_card(collection, state, local)
                prepared["prior_removal"] = state.history_removal(change["cursor"])
            prepared["entry"] = {"removed_history": source["removed_history"]}
            if source.get("restored_history"):
                prepared["entry"]["restored_history"] = source["restored_history"]
            prepared["history_digest"] = self._history_digest(collection, local["id"])
        return prepared

    def resolve_history_removal(self, collection, prepared, *, schedule):
        current = self.prepare_card_resolution(collection, prepared["problem"])
        if "entry" not in current or current != prepared:
            raise DeliveryConflict("The Anki card or history changed while this chooser was open. Reopen it before confirming Undo.")
        if schedule not in ("anki", "shirabe"):
            raise DeliveryConflict("Choose Anki or the incoming schedule after Undo")
        local = current["local"]
        selected = local if schedule == "anki" else current["detail"]["anki"]["card_after"]
        if local["did"] != selected["did"] and (local["odid"] or selected["odid"]):
            raise DeliveryConflict("Reconcile the filtered deck before choosing its incoming schedule")
        row = {**local, **{key: selected[key] for key in STATE_COLUMNS}}
        data = {**auxiliary(local)["data"], **{key: value for key, value in json.loads(selected["data"] or "{}").items() if key in ("s", "d", "lrt")}}
        row.update(queue=min(local["queue"], 0) or active_queue(selected), data=json.dumps(data))
        choice = {"change": current["change"], "local": local, "card": row, "history_digest": current["history_digest"],
                  "prior_removal": current["prior_removal"], "history_removal_attempt": str(uuid.uuid4())}
        list(SyncSnapshot(self.path, self.client)._batches("cards", [{"row": row}], self.key))
        with SyncState(self.path) as state:
            self._pending_change(state, prepared["problem"], "card")
            self._selected_card(collection, state, row)
            if state.history_removal(current["change"]["cursor"]) != current["prior_removal"]:
                raise DeliveryConflict("The pending history removal changed before confirmation")
            state.remember_delivery_choice(current["change"]["cursor"], choice)

    def resolve_card(self, collection, prepared, choices):
        current = self.prepare_card_resolution(collection, prepared["problem"])
        if "entry" in current:
            raise DeliveryConflict("Confirm the removed history in the Undo chooser")
        if current["local"] != prepared["local"] or current["last_review_id"] != prepared["last_review_id"] or current["change"] != prepared["change"]:
            raise DeliveryConflict("The Anki card changed while this chooser was open. Reopen it before applying a choice.")
        if not current["schedule_matches"]:
            raise DeliveryConflict("Reconcile review history before choosing between different card schedules")
        if not isinstance(choices, dict) or set(choices) != {"deck", "lifecycle", "properties"} or any(side not in ("anki", "shirabe") for side in choices.values()):
            raise DeliveryConflict("Choose Anki or Shirabe for each card-state dimension")
        local = current["local"]
        sources = {"anki": local, "shirabe": current["detail"]["anki"]["card_after"]}
        properties = auxiliary(sources[choices["properties"]])
        data = {**properties["data"], **{key: value for key, value in json.loads(local["data"] or "{}").items() if key in ("s", "d", "lrt")}}
        row = {**local, "did": sources[choices["deck"]]["did"],
               "queue": min(sources[choices["lifecycle"]]["queue"], 0) or active_queue(local),
               "flags": properties["flags"], "data": json.dumps(data)}
        list(SyncSnapshot(self.path, self.client)._batches("cards", [{"row": row}], self.key))
        with SyncState(self.path) as state:
            self._pending_change(state, prepared["problem"], "card")
            self._selected_card(collection, state, row)
            state.remember_delivery_choice(prepared["problem"]["cursor"],
                {"change": current["change"], "local": local, "card": row})

    def acknowledge(self):
        with SyncState(self.path) as state:
            delivery = state.get("delivery")
            if not delivery or delivery["return_key"] is None:
                raise DeliveryConflict("Apply every change before acknowledging this delivery")
            batch = state.batch(delivery["return_key"]) if delivery["return_key"] else None
            kept = [change["cursor"] for change in delivery["response"]["changes"]
                    if state.delivery_result(change["cursor"]).get("kept_note") == change["cursor"]]
            kept_reviews = [change["cursor"] for change in delivery["response"]["changes"]
                            if state.delivery_result(change["cursor"]).get("kept_review") == change["cursor"]]
            deletion_keys = state.deletion_return_keys()
            undo_keys = state.review_undo_return_keys()
        if batch:
            receipt = batch["receipt"]
            if receipt is None:
                receipt = self.client.batch(self.profile_id, batch["payload"])
                with SyncState(self.path) as state:
                    state.remember_receipt(delivery["return_key"], receipt)
            expected = {(item["row"]["id"], item["sync_id"]) for item in batch["payload"]["history"]}
            results = receipt.get("history", [])
            accepted = {(item.get("id"), item.get("sync_id")) for item in results if item.get("status") == "accepted"}
            if len(results) != len(expected) or accepted != expected:
                raise DeliveryConflict("Shirabe has not accepted every returned review identity")
            self._accept_returned_identities(batch, receipt)
        self._return_source_deletions(deletion_keys)
        self._return_review_undos(undo_keys)
        cursor = delivery["response"]["cursor"]
        self.client.acknowledge(self.profile_id, key=self.key, cursor=cursor,
            **({"kept_notes": kept} if kept else {}), **({"kept_reviews": kept_reviews} if kept_reviews else {}))
        with SyncState(self.path) as state:
            state.complete_delivery(cursor)
            if not delivery["response"]["changes"]:
                state.prune()
        return cursor

    def _accept_returned_identities(self, batch, receipt):
        expected = batch["payload"].get("creations", [])
        results = receipt.get("creations", [])
        if len(results) != len(expected):
            raise DeliveryConflict("Shirabe has not accepted every returned creation identity")
        history = batch["payload"].get("history", [])
        removed = {item["id"] for item in receipt.get("history", []) if item.get("removed") is True}
        checkpoints = [("source_history", item["row"]["id"], "deleted" if item["row"]["id"] in removed else source_digest(item["row"]), item["row"]) for item in history]
        for creation, result in zip(expected, results):
            note_result = result.get("note", {})
            cards = result.get("cards", [])
            native_note = creation["note"]
            native_cards = {row["id"]: row for row in creation["cards"]}
            if result.get("status") != "accepted" or result.get("key") != creation["key"] or \
                    result.get("delivery_cursor") != creation["delivery_cursor"] or note_result.get("id") != native_note["id"] or \
                    note_result.get("note", {}).get("anki_id") != native_note["id"] or not note_result.get("snapshot") or \
                    len(cards) != len(native_cards) or {card.get("id") for card in cards} != set(native_cards) or \
                    any(card.get("card", {}).get("anki_id") != card.get("id") or not card.get("snapshot") for card in cards):
                raise DeliveryConflict("Shirabe returned different creation identities")
            if "owned_type_id" in creation:
                accepted_type = result.get("note_type", {})
                definition = creation["definition"]
                if accepted_type.get("id") != native_note["mid"] or definition.get("id") != native_note["mid"] or \
                        accepted_type.get("note_type_id") != creation["owned_type_id"] or \
                        type(accepted_type.get("revision")) is not int or accepted_type["revision"] < 0 or \
                        accepted_type.get("definition") != definition:
                    raise DeliveryConflict("Shirabe returned a different owned note type identity or definition")
                checkpoints.extend([("note_type", definition["id"], str(accepted_type["revision"]), definition),
                    ("source_note_type", definition["id"], source_digest(definition), definition)])
            checkpoints.extend([("note", native_note["id"], note_result["snapshot"], note_result["note"]),
                ("source_note", native_note["id"], source_digest(native_note), native_note)])
            for card in cards:
                source = native_cards[card["id"]]
                checkpoints.extend([("card", card["id"], card["snapshot"], card["card"]),
                    ("source_card", card["id"], source_digest(source), source)])
        if expected or history:
            with SyncState(self.path) as state:
                state.complete_batch(batch["payload"]["request_key"], checkpoints)

    def _return_review_undos(self, keys):
        problems = []
        for key in keys:
            with SyncState(self.path) as state:
                if state.batch_applied(key):
                    continue
                batch = state.batch(key)
            receipt = batch["receipt"]
            if receipt is None:
                receipt = self.client.batch(self.profile_id, batch["payload"])
                with SyncState(self.path) as state:
                    state.remember_receipt(key, receipt)
            entries = list(SyncChanges(self.path, self.client)._entries(batch["payload"], receipt))
            with SyncState(self.path) as state:
                for kind, entry, result, checkpoints in entries:
                    if kind != "cards" or result["status"] == "accepted" and result.get("review_undo_cursor") != entry["review_undo_cursor"]:
                        raise DeliveryConflict("Shirabe has not accepted the requested Undo. Its saved result needs reconciliation.")
                    if state.entity_applied(key, kind, result["id"]) or state.replacement_for(key, kind, result["id"]):
                        continue
                    if kind == "cards" and result["status"] == "conflict":
                        problems.append({"direction": "returning", "kind": "cards", "request_key": key,
                            "id": result["id"], "status": "conflict", "code": result.get("code", "review_undo_not_accepted")})
                        continue
                    if result["status"] != "accepted":
                        raise DeliveryConflict("Shirabe has not accepted the requested Undo. Its saved result needs reconciliation.")
                    after = result["anki"]["card_after"]
                    if any(entry["row"][field] != after[field] for field in ("id", "nid", "ord")):
                        raise DeliveryConflict("The accepted Undo has a different card identity. Reconcile it before acknowledging.")
                    base = state.resolution_base(key)
                    if not same_card(entry["row"], after) or base and not same_card(base["card"], after):
                        message = "The accepted Undo has a different card state. Choose how to reconcile it before acknowledging."
                        problems.append({"direction": "returning", "kind": "review_undo_results",
                            "request_key": key, "cursor": entry["review_undo_cursor"], "id": result["id"],
                            "status": "local_conflict", "code": message})
                        continue
                    state.complete_entity(key, kind, result["id"], checkpoints, cursor=receipt.get("cursor", 0))
        with SyncState(self.path) as state:
            state.settle_resolutions()
            for key in keys:
                batch = state.batch(key)
                if all(state.entity_applied(key, kind, result["id"]) for kind, _, result, _ in
                       SyncChanges(self.path, self.client)._entries(batch["payload"], batch["receipt"])):
                    state.complete_batch(key, [])
        if problems:
            raise DeliveryConflict("Reconcile the returned Undo results before acknowledging.", problems)

    def _return_source_deletions(self, keys):
        problems = []
        for key in keys:
            problems.extend(self._return_source_deletion(key))
        with SyncState(self.path) as state:
            state.settle_resolutions()
            for key in keys:
                batch = state.batch(key)
                if all(state.entity_applied(key, kind, result["id"]) for kind, _, result, _ in
                       SyncChanges(self.path, self.client)._entries(batch["payload"], batch["receipt"])):
                    state.complete_batch(key, [])
        if problems:
            raise DeliveryConflict("Shirabe has not accepted the source deletion. Choose how to resolve its conflicts.", problems)

    def _return_source_deletion(self, key):
        with SyncState(self.path) as state:
            if state.batch_applied(key):
                return []
            batch = state.batch(key)
        receipt = batch["receipt"]
        if receipt is None:
            receipt = self.client.batch(self.profile_id, batch["payload"])
            with SyncState(self.path) as state:
                state.remember_receipt(key, receipt)
        entries = list(SyncChanges(self.path, self.client)._entries(batch["payload"], receipt))
        problems = []
        with SyncState(self.path) as state:
            for kind, _, result, checkpoints in entries:
                if state.entity_applied(key, kind, result["id"]) or state.replacement_for(key, kind, result["id"]):
                    continue
                if result["status"] != "accepted" or kind == "cards" and result.get("source_deleted") is not True:
                    problems.append({"direction": "returning", "request_key": key, "kind": kind, "id": result["id"],
                                     "operation": "delete", "status": result["status"], "code": result.get("code", "source_deletion_not_accepted")})
                    continue
                state.complete_entity(key, kind, result["id"], checkpoints, cursor=receipt.get("cursor", 0))
        return problems

    @staticmethod
    def _validate_page(start_cursor, response):
        cursor, changes = response.get("cursor"), response.get("changes")
        if type(cursor) is not int or cursor < start_cursor or not isinstance(changes, list) or len(changes) > 200:
            raise DeliveryConflict("Invalid delivery page")
        previous = start_cursor
        for change in changes:
            if not isinstance(change, dict) or type(change.get("cursor")) is not int or not previous < change["cursor"] <= cursor:
                raise DeliveryConflict("Invalid delivery cursor order")
            if not isinstance(change.get("payload"), dict) or not isinstance(change.get("entity_kind"), str) or not isinstance(change.get("operation"), str):
                raise DeliveryConflict("Invalid delivered change")
            previous = change["cursor"]
