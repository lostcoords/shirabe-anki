"""Apply reconciled card state through Anki's public collection API."""

from __future__ import annotations

import json

from .sync_reviews import CARD_COLUMNS, STATE_COLUMNS, card_row, remove_unsynced_history, restore_history, same_state, validate_history_change, verify_platform


class CardConflict(Exception):
    pass


def active_queue(row):
    if row["queue"] >= 0:
        return row["queue"]
    if row["type"] in (0, 2):
        return row["type"]
    due = row["odue"] if row["odid"] > 0 and row["odue"] else row["due"]
    return 1 if due > 1_000_000_000 else 3


def auxiliary(row):
    return {"flags": row["flags"], "data": {key: value for key, value in json.loads(row["data"] or "{}").items() if key not in ("s", "d", "lrt")}}


def same_schedule(left, right):
    return same_state({**left, "queue": active_queue(left)}, {**right, "queue": active_queue(right)})


def same_card(left, right):
    return left["did"] == right["did"] and same_state(left, right) and auxiliary(left) == auxiliary(right)


def validate_card_row(row):
    if not isinstance(row, dict) or set(row) != set(CARD_COLUMNS) or any(type(row[key]) is not int for key in CARD_COLUMNS if key != "data") or not isinstance(row["data"], str):
        raise CardConflict("Invalid source card state")
    try:
        data = json.loads(row["data"] or "{}")
    except ValueError:
        raise CardConflict("Invalid source card data") from None
    if not isinstance(data, dict):
        raise CardConflict("Invalid source card data")


def apply_card_change(collection, payload, *, restored_history=(), removed_history=(), before_history_removal=None, before_commit=None):
    from anki.cards_pb2 import FsrsMemoryState

    verify_platform(collection)
    source = payload["anki"]
    before, after = source["card_before"], source["card_after"]
    for row in (before, after):
        validate_card_row(row)
    if any(before[key] != after[key] for key in ("id", "nid", "ord")):
        raise CardConflict("The source card identity changed")
    card = collection.get_card(after["id"])
    note = collection.get_note(card.nid)
    if note.guid != source["note_guid"] or note.mid != source["anki_type_id"] or card.nid != after["nid"] or card.ord != after["ord"]:
        raise CardConflict("The source note or template identity changed")

    def apply():
        current = card_row(collection, card.id)

        def apply_history():
            if removed_history and restored_history:
                validate_history_change(collection, card.id, removed_history, restored_history)
            remove_unsynced_history(collection, card.id, removed_history, before_remove=before_history_removal)
            restore_history(collection, card.id, restored_history)

        def choose(dimension, equal):
            if equal(current, before):
                return after
            if equal(after, before) or equal(current, after):
                return current
            raise CardConflict(f"The card's {dimension} changed independently in Anki and Shirabe")

        schedule = choose("schedule", same_schedule)
        deck = choose("deck", lambda a, b: a["did"] == b["did"])
        lifecycle = lambda row: min(row["queue"], 0)
        life = choose("suspension or burial", lambda a, b: lifecycle(a) == lifecycle(b))
        flags = choose("flags", lambda a, b: a["flags"] == b["flags"])
        properties = choose("auxiliary data", lambda a, b: auxiliary(a)["data"] == auxiliary(b)["data"])
        data = {**auxiliary(properties)["data"], **{key: value for key, value in json.loads(schedule["data"] or "{}").items() if key in ("s", "d", "lrt")}}
        target = {**schedule, "did": deck["did"], "queue": lifecycle(life) or active_queue(schedule),
                  "flags": flags["flags"], "data": json.dumps(data)}
        apply_history()
        if same_card(current, target):
            return current
        for key in (*STATE_COLUMNS, "did", "flags"):
            setattr(card, key, target[key])
        card.memory_state = FsrsMemoryState(stability=data["s"], difficulty=data["d"]) if "s" in data and "d" in data else None
        card.last_review_time = data.get("lrt")
        card.original_position = data.get("pos")
        card.custom_data = data.get("cd", "")
        card.desired_retention = data.get("dr")
        card.decay = data.get("decay")
        collection.update_card(card, skip_undo_entry=bool(removed_history or restored_history))
        written = card_row(collection, card.id)
        if not same_card(written, target):
            raise CardConflict("Anki cannot preserve this source card through its public API")
        return written

    def apply_and_record():
        written = apply()
        if before_commit:
            before_commit(written)

    collection.db.transact(apply_and_record)
    return card_row(collection, card.id)
