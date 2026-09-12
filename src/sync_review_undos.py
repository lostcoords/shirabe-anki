from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from .sync_cards import active_queue, apply_card_change, auxiliary, same_card, same_schedule, validate_card_row
from .sync_changes import SyncChanges
from .sync_reviews import card_row, history_row, removable_history, ReviewConflict, update_review_stats, verify_platform
from .sync_snapshot import SyncSnapshot, source_digest
from .sync_state import SyncState


class ReviewUndos:
    def _review_undo_source(self, change):
        payload = change["payload"]
        if "anki" in payload:
            source = payload["anki"]
            for side in ("card_before", "card_after"):
                validate_card_row(source[side])
            original = payload["review"]["anki"]
            if source["note_guid"] != original["note_guid"] or source["anki_type_id"] != original["anki_type_id"] or \
                    any(source[side][key] != original["card_after"][key] for side in ("card_before", "card_after") for key in ("id", "nid", "ord")):
                raise ReviewConflict("The reconciled Undo identity changed")
            return source
        if payload["review"].get("history_only"):
            raise ReviewConflict("Reconcile the preceding review before confirming Undo")
        source = payload["review"]["anki"]
        return {**source, "card_before": source["card_after"], "card_after": source["card_before"]}

    def _pending_review_undo_result(self, state, problem):
        from .sync_delivery import DeliveryConflict

        delivery = state.get("delivery")
        key, cursor = problem.get("request_key"), problem.get("cursor")
        if problem.get("direction") != "returning" or problem.get("kind") != "review_undo_results" or not delivery or type(cursor) is not int:
            raise DeliveryConflict("This Undo result is no longer waiting for a choice. Resume sync.")
        change = next((item for item in delivery["response"]["changes"] if item["cursor"] == cursor), None)
        returned = state.delivery_result(cursor)
        attempt = state.review_undo(cursor)
        if not change or change["entity_kind"] != "review" or change["operation"] != "delete" or not returned or \
                not attempt or attempt.get("change") != change or key not in state.review_undo_return_keys() or state.entity_applied(key, "cards", problem.get("id")):
            raise DeliveryConflict("This Undo result is no longer waiting for a choice. Resume sync.")
        batch = state.batch(key)
        entries = list(SyncChanges(self.path, self.client)._entries(batch["payload"], batch["receipt"]))
        if len(entries) != 1:
            raise DeliveryConflict("The saved Undo result changed")
        kind, entry, result, checkpoints = entries[0]
        if kind != "cards" or result["status"] != "accepted" or result.get("review_undo_cursor") != cursor or \
                entry.get("review_undo_cursor") != cursor or result["id"] != problem.get("id") or result["id"] != change["payload"]["anki_card_id"] or result["card"]["anki_id"] != result["id"]:
            raise DeliveryConflict("The accepted Undo request identity changed")
        remote = result["anki"]["card_after"]
        validate_card_row(remote)
        if any(entry["row"][field] != remote[field] for field in ("id", "nid", "ord")):
            raise DeliveryConflict("The accepted Undo has a different card identity")
        return change, entry, result, checkpoints, batch["receipt"].get("cursor", 0)

    def prepare_review_undo_result_resolution(self, collection, problem):
        with SyncState(self.path) as state:
            self._deletion_snapshot(collection, state)
            change, entry, result, _, _ = self._pending_review_undo_result(state, problem)
            base = state.resolution_base(problem["request_key"])
            if base and base.get("removed_history") != entry["removed_history"]:
                raise ReviewConflict("The confirmed Undo no longer matches its removed events")
            confirmed = bool(base and same_schedule(entry["row"], result["anki"]["card_after"]))
            schedules = (base["card"], result["anki"]["card_after"]) if confirmed else ()
            saved = state.review_undo_result_choice(problem["request_key"])
            if saved:
                if saved["result"] != result:
                    raise ReviewConflict("The accepted Undo result changed after its schedule choice")
                schedules += (saved["local"], saved["chosen"])
            if not confirmed:
                schedules += (card_row(collection, problem["id"]),)
            current = self._review_undo_state(collection, state, change, recovering=True, accepted_schedules=schedules)
            if current["present"] or entry["removed_history"] != [current["row"]]:
                raise ReviewConflict("The undone review exists again or its identity changed. Reconcile history first.")
            if saved and saved["history_digest"] != current["history_digest"]:
                raise ReviewConflict("The review history changed after the accepted Undo choice. Reconcile history first.")
            matches = same_schedule(current["local"], result["anki"]["card_after"])
            return {"problem": problem, "change": change, "result": result, "undo_accepted": True,
                    "local": current["local"], "history_digest": current["history_digest"],
                    "schedule_confirmed": confirmed,
                    "schedule_matches": matches,
                    "schedule_choice_required": not confirmed and (not matches or bool(saved and "schedule" in saved["choices"])),
                    "prior_result_choice": saved,
                    "detail": {"snapshot": result["snapshot"], "anki": result["anki"]},
                    "decks": {item.id: item.name for item in collection.decks.all_names_and_ids()}}

    def resolve_review_undo_result(self, collection, prepared, choices):
        current = self.prepare_review_undo_result_resolution(collection, prepared["problem"])
        if current != prepared:
            raise ReviewConflict("The Anki card or history changed while this chooser was open. Reopen it.")
        dimensions = {"deck", "lifecycle", "properties"}
        if current["schedule_choice_required"]:
            if not isinstance(choices, dict) or choices.get("schedule") not in ("anki", "shirabe"):
                raise ReviewConflict("Choose explicitly between the different card schedules before acknowledging this Undo")
            dimensions.add("schedule")
        if not isinstance(choices, dict) or set(choices) != dimensions or any(side not in ("anki", "shirabe") for side in choices.values()):
            raise ReviewConflict("Choose Anki or Shirabe for each card-state dimension")
        local = current["local"]
        sides = {"anki": local, "shirabe": current["result"]["anki"]["card_after"]}
        schedule_side = choices.get("schedule", "shirabe" if current["schedule_confirmed"] else "anki")
        schedule = sides[schedule_side]
        if local["did"] != schedule["did"] and (local["odid"] or schedule["odid"]):
            raise ReviewConflict("Reconcile the filtered deck before applying the confirmed Undo schedule")
        properties = auxiliary(sides[choices["properties"]])
        data = {**properties["data"], **{key: value for key, value in json.loads(schedule["data"] or "{}").items() if key in ("s", "d", "lrt")}}
        chosen = {**schedule, "did": sides[choices["deck"]]["did"],
                  "queue": min(sides[choices["lifecycle"]]["queue"], 0) or active_queue(schedule),
                  "flags": properties["flags"], "data": json.dumps(data)}
        with SyncState(self.path) as state:
            self._selected_card(collection, state, chosen)
            state.remember_review_undo_result_choice(prepared["problem"]["request_key"],
                {"result": current["result"], "local": local, "chosen": chosen, "choices": choices, "history_digest": current["history_digest"]},
                previous=current["prior_result_choice"])
        source = current["change"]["payload"]["review"]["anki"]
        apply_card_change(collection, {"anki": {**source, "card_before": local, "card_after": chosen}})
        with SyncState(self.path) as state:
            self._deletion_snapshot(collection, state)
            _, _, result, checkpoints, cursor = self._pending_review_undo_result(state, prepared["problem"])
            state.complete_entity(prepared["problem"]["request_key"], "cards", result["id"], checkpoints, cursor=cursor)

    def _review_undo_state(self, collection, state, change, *, recovering=False, accepted_schedules=(), additional_history=False):
        verify_platform(collection)
        payload = change["payload"]
        saved = state.review(payload["sync_id"])
        if not saved or not saved["applied"] or saved["payload"] != payload["review"] or \
                saved["payload"]["anki_card_id"] != payload["anki_card_id"] or \
                type(payload.get("delivery_cursor")) is not int or not 0 < payload["delivery_cursor"] < change["cursor"]:
            raise ReviewConflict("Reconcile the original delivered review before confirming Undo")
        source = self._review_undo_source(change)
        row = saved["row"]
        local = card_row(collection, payload["anki_card_id"])
        self._selected_card(collection, state, local)
        self._deletion_decks(collection, state)
        note = collection.get_note(local["nid"])
        if any(local[key] != source["card_before"][key] for key in ("id", "nid", "ord")) or \
                note.guid != source["note_guid"] or note.mid != source["anki_type_id"]:
            raise ReviewConflict("The review's note or card identity changed")
        history = list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (local["id"],)))
        remaining = [item for item in history if item["id"] != row["id"]]
        known = state.card_history(local["id"])
        known.pop(row["id"], None)
        remaining_rows = {item["id"]: item for item in remaining}
        if "anki" in payload:
            predecessor = payload.get("predecessor")
            if not isinstance(predecessor, dict) or predecessor.get("cid") != local["id"] or \
                    predecessor.get("id") not in remaining_rows or \
                    predecessor.get("id") != max(remaining_rows, default=None) or \
                    remaining_rows[predecessor["id"]] != predecessor:
                raise ReviewConflict("Reconcile the preceding review before confirming Undo")
        if any(remaining_rows.get(source_id) != item for source_id, item in known.items()) or (not additional_history and remaining_rows != known):
            raise ReviewConflict("Reconcile changed review history before confirming Undo")
        if additional_history and any(state.checkpoint("source_history", source_id) for source_id in remaining_rows.keys() - known.keys()):
            raise ReviewConflict("Reconcile restored or reassigned review history before resuming Undo")
        present = history_row(collection, row["id"])
        if present:
            removable_history(collection, local["id"], [{"row": row, "removal_id": change["cursor"]}])
            if max(item["id"] for item in history) != row["id"] or not same_schedule(local, source["card_before"]):
                raise ReviewConflict("The card has changed since this review")
        elif not recovering or not any(same_schedule(local, row) for row in (source["card_after"], *accepted_schedules)):
            raise ReviewConflict("The requested review is absent or its Undo schedule changed. Reconcile history first.")
        leech = payload.get("leech_tag")
        if leech is not None:
            if not isinstance(leech, dict) or any(not isinstance(leech.get(side), list) or
                    any(not isinstance(tag, str) for tag in leech[side]) for side in ("before", "after")) or \
                    any(tag.casefold() == "leech" for tag in leech["before"]) or \
                    sorted(tag.casefold() for tag in leech["after"]) != sorted([*(tag.casefold() for tag in leech["before"]), "leech"]):
                raise ReviewConflict("Invalid leech tag change")
            if present and sorted(tag.casefold() for tag in note.tags) != sorted(tag.casefold() for tag in leech["after"]):
                raise ReviewConflict("The note's tags changed since this review. Reconcile them before confirming Undo.")
        siblings = self._review_undo_siblings(collection, state, change) if present else []
        return {"local": local, "row": row, "present": bool(present), "history_digest": source_digest(history),
                "remaining_digest": source_digest(remaining), "siblings": siblings,
                "leech_tags": sorted(tag.casefold() for tag in note.tags) if leech else None}

    def _review_undo_siblings(self, collection, state, change):
        burials = change["payload"].get("sibling_burials", [])
        if not isinstance(burials, list):
            raise ReviewConflict("Invalid sibling burial list")
        note_id = change["payload"]["review"]["anki"]["card_after"]["nid"]
        seen = {change["payload"]["anki_card_id"]}
        current = []
        for burial in burials:
            source = burial["anki"]
            before, after = source["card_before"], source["card_after"]
            validate_card_row(before)
            validate_card_row(after)
            if before["id"] in seen or before["nid"] != note_id or before["queue"] != -2 or after["queue"] < 0 or \
                    any(before[key] != after[key] for key in ("id", "nid", "ord")) or not same_card({**before, "queue": after["queue"]}, after):
                raise ReviewConflict("The sibling burial identity changed")
            seen.add(before["id"])
            local = card_row(collection, before["id"])
            self._selected_card(collection, state, local)
            note = collection.get_note(local["nid"])
            if any(local[key] != before[key] for key in ("id", "nid", "ord")) or note.guid != source["note_guid"] or \
                    note.mid != source["anki_type_id"] or not same_card(local, before):
                raise ReviewConflict("A sibling has changed since it was buried. Reconcile it before confirming Undo.")
            current.append(local)
        return current

    def prepare_review_undo_resolution(self, collection, problem):
        from .sync_delivery import DeliveryConflict

        with SyncState(self.path) as state:
            self._deletion_snapshot(collection, state)
            change = self._pending_review_undo(state, problem)
            prepared = {"problem": problem, "change": change, "prior_removal": state.review_undo(change["cursor"])}
            try:
                prepared.update(self._review_undo_state(collection, state, change))
            except (ReviewConflict, DeliveryConflict) as error:
                prepared["undo_error"] = str(error)
            return prepared

    def confirm_review_undo(self, collection, prepared):
        current = self.prepare_review_undo_resolution(collection, prepared["problem"])
        if current != prepared:
            raise ReviewConflict("The Anki card or history changed while this chooser was open. Reopen it before confirming Undo.")
        if current.get("undo_error"):
            raise ReviewConflict(current["undo_error"])
        choice = {key: value for key, value in current.items() if key not in ("problem", "present")}
        choice.update(action="undo_review", attempt=str(uuid.uuid4()))
        with SyncState(self.path) as state:
            self._deletion_snapshot(collection, state)
            self._pending_review_undo(state, prepared["problem"])
            state.remember_delivery_choice(current["change"]["cursor"], choice)

    def withdraw_review_undo(self, collection, prepared):
        with SyncState(self.path) as state:
            self._deletion_snapshot(collection, state)
            change = self._pending_review_undo(state, prepared["problem"])
            if change != prepared["change"]:
                raise ReviewConflict("The Undo request changed while this chooser was open. Reopen it.")
            started = state.review_undo(change["cursor"])
            if started and history_row(collection, started["row"]["id"]) is None:
                raise ReviewConflict("This Undo already removed history. Resume its result upload before withdrawing it.")
            state.remember_delivery_choice(change["cursor"], {"change": change, "action": "withdraw_review_undo"})

    def _apply_review_undo(self, collection, state, change, choice):
        if choice.get("change") != change or choice.get("action") != "undo_review":
            raise ReviewConflict("The saved review Undo choice changed")
        started = state.review_undo(change["cursor"])
        attempt = {key: value for key, value in choice.items() if key != "prior_removal"}
        output = state.review_undo_output(change["cursor"]) if started == attempt else None
        schedules = (card_row(collection, change["payload"]["anki_card_id"]),) if output else ()
        current = self._review_undo_state(collection, state, change, recovering=started == attempt,
            accepted_schedules=schedules, additional_history=bool(output))
        if started == attempt:
            if current["present"]:
                raise ReviewConflict("An interrupted Undo has a live review. Reopen the chooser before removing it again.")
            if not output:
                raise ReviewConflict("The interrupted Undo has no saved result. Reconcile it before continuing.")
        else:
            source = self._review_undo_source(change)

            def reserve(_exists):
                latest = self._review_undo_state(collection, state, change)
                if any(latest[key] != choice[key] for key in ("local", "history_digest", "siblings", "leech_tags")):
                    raise ReviewConflict("The Anki card or history changed after confirmation. Reopen the Undo chooser.")
                state.prepare_review_undo(choice)

            def record(_written):
                if leech := change["payload"].get("leech_tag"):
                    note = collection.get_note(source["card_after"]["nid"])
                    note.tags = leech["before"]
                    collection.update_note(note, skip_undo_entry=True)
                for burial in change["payload"].get("sibling_burials", []):
                    after = burial["anki"]["card_after"]
                    sibling = collection.get_card(after["id"])
                    sibling.queue = after["queue"]
                    collection.update_card(sibling, skip_undo_entry=True)
                    if not same_card(card_row(collection, sibling.id), after):
                        raise ReviewConflict("Anki could not restore the sibling burial")
                update_review_stats(collection, change["payload"]["review"], direction=-1)
                entry = self._review_undo_output(collection, change, current["row"])
                state.remember_review_undo_output(choice, {"cards": [entry]})

            apply_card_change(collection, {"anki": source},
                removed_history=[{"row": current["row"], "removal_id": change["cursor"]}], before_history_removal=reserve, before_commit=record)
            output = state.review_undo_output(change["cursor"])
        state.finish_delivery_change(change["cursor"], {}, review_undo_batch=output)

    def _review_undo_output(self, collection, change, removed_row):
        row = next(SyncSnapshot._rows(collection, "cards", "id = ?", (change["payload"]["anki_card_id"],),
                                     with_last_review=True, with_last_event=True))
        last_review, last_event = row.pop("last_review_id"), row.pop("last_event_id")
        entry = {"row": row, "base": change["payload"]["snapshot"], "captured_at": datetime.now(timezone.utc).isoformat(),
                 "removed_history": [removed_row], "review_undo_cursor": change["cursor"]}
        if last_review:
            entry["last_review_id"] = last_review
        if last_event:
            entry["last_event_id"] = last_event
        list(SyncSnapshot(self.path, self.client)._batches("cards", [entry], self.key))
        return entry
