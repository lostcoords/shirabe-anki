"""Apply delivered reviews on Anki's collection thread, without network I/O."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import json
import math


class ReviewConflict(Exception):
    pass


REVLOG_COLUMNS = ("id", "cid", "usn", "ease", "ivl", "lastIvl", "factor", "time", "type")
CARD_COLUMNS = ("id", "nid", "did", "ord", "mod", "usn", "type", "queue", "due", "ivl", "factor", "reps", "lapses", "left", "odue", "odid", "flags", "data")
STATE_COLUMNS = ("type", "queue", "due", "ivl", "factor", "reps", "lapses", "left", "odue", "odid")


def verify_platform(collection):
    from anki.buildinfo import version

    if version != "26.08.1":
        raise ReviewConflict("Review sync requires the verified Anki 26.08.1 build")
    if collection.get_config("fsrs", False) is not True:
        raise ReviewConflict("Enable FSRS in Anki's deck options before syncing with Shirabe. Pending changes are retained.")
    for table, columns in (("revlog", REVLOG_COLUMNS), ("cards", CARD_COLUMNS)):
        actual = collection.db.all("SELECT * FROM pragma_table_info(?) ORDER BY cid", table)
        if tuple(row[1] for row in actual) != columns or any(row[2].lower() != ("text" if row[1] == "data" else "integer") for row in actual):
            raise ReviewConflict("The Anki collection schema is not supported")


def card_row(collection, card_id):
    row = collection.db.first(f"SELECT {','.join(CARD_COLUMNS)} FROM cards WHERE id = ?", card_id)
    if not row:
        raise ReviewConflict("The source card no longer exists")
    return dict(zip(CARD_COLUMNS, row))


def history_row(collection, source_id):
    row = collection.db.first(f"SELECT {','.join(REVLOG_COLUMNS)} FROM revlog WHERE id = ?", source_id)
    return dict(zip(REVLOG_COLUMNS, row)) if row else None


def removable_history(collection, card_id, entries):
    if not isinstance(entries, (list, tuple)):
        raise ReviewConflict("Invalid removed history list")
    rows = {}
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("removal_id")) is not int or entry["removal_id"] <= 0:
            raise ReviewConflict("The removed review has no removal identity")
        row = entry.get("row")
        if not isinstance(row, dict) or set(row) != set(REVLOG_COLUMNS) or any(type(value) is not int for value in row.values()) or \
                not 0 < row["id"] < 2**63 or row["cid"] != card_id or row["id"] in rows:
            raise ReviewConflict("The removed review identity is invalid")
        rows[row["id"]] = row
    if not rows:
        return []
    placeholders = ",".join("?" for _ in rows)
    existing = collection.db.all(f"SELECT {','.join(REVLOG_COLUMNS)} FROM revlog WHERE id IN ({placeholders})", *rows)
    for values in existing:
        current = dict(zip(REVLOG_COLUMNS, values))
        original = rows[current["id"]]
        if any(current[key] != original[key] for key in REVLOG_COLUMNS if key != "usn"):
            raise ReviewConflict("The removed review ID belongs to a different event")
        # Anki's native sync has no revlog deletion tombstones.
        if current["usn"] != -1 or original["usn"] != -1:
            raise ReviewConflict("This review has already reached native sync. Its history cannot be removed automatically.")
    return existing


def remove_unsynced_history(collection, card_id, entries, *, before_remove=None):
    existing = removable_history(collection, card_id, entries)
    if not entries:
        return
    if before_remove is None:
        raise ReviewConflict("History removal needs a durable pending receipt")
    before_remove(bool(existing))
    if existing:
        collection.db.execute(f"DELETE FROM revlog WHERE id IN ({','.join('?' for _ in existing)})", *(row[0] for row in existing))


def restorable_history(collection, card_id, entries):
    if not isinstance(entries, (list, tuple)):
        raise ReviewConflict("Invalid restored history list")
    seen = set()
    missing = []
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("removal_id")) is not int or entry["removal_id"] <= 0:
            raise ReviewConflict("The restored review has no removal identity")
        row = entry.get("row")
        if not isinstance(row, dict) or set(row) != set(REVLOG_COLUMNS) or any(type(value) is not int for value in row.values()) or \
                not 0 < row["id"] < 2**63 or row["cid"] != card_id or row["id"] in seen:
            raise ReviewConflict("The restored review identity is invalid")
        seen.add(row["id"])
        existing = history_row(collection, row["id"])
        if existing:
            if any(existing[key] != row[key] for key in REVLOG_COLUMNS if key != "usn"):
                raise ReviewConflict("The restored review ID belongs to a different event")
        else:
            missing.append({**row, "usn": collection.usn()})
    return missing


def validate_history_change(collection, card_id, removed, restored):
    removable_history(collection, card_id, removed)
    restorable_history(collection, card_id, restored)
    if {item["row"]["id"] for item in removed} & {item["row"]["id"] for item in restored}:
        raise ReviewConflict("The same review cannot be both removed and restored")


def restore_history(collection, card_id, entries):
    rows = restorable_history(collection, card_id, entries)
    if rows:
        collection.db.executemany(f"INSERT INTO revlog ({','.join(REVLOG_COLUMNS)}) VALUES ({','.join('?' for _ in REVLOG_COLUMNS)})",
                                 [tuple(row[key] for key in REVLOG_COLUMNS) for row in rows])


def same_state(left, right):
    if any(left.get(key) != right.get(key) for key in STATE_COLUMNS):
        return False
    left_data, right_data = json.loads(left["data"] or "{}"), json.loads(right["data"] or "{}")
    if left_data.get("lrt") != right_data.get("lrt"):
        return False
    # Anki stores f32 stability at four decimal places and difficulty at three.
    # https://github.com/ankitects/anki/blob/main/rslib/src/storage/card/data.rs
    for key, tolerance in (("s", 0.00005001), ("d", 0.0005001)):
        a, b = left_data.get(key), right_data.get(key)
        if a == b:
            continue
        if a is None or b is None or not math.isclose(a, b, rel_tol=1e-7, abs_tol=tolerance):
            return False
    return True


def canonical_review_applied(state, current, payload):
    from .sync_cards import same_schedule

    checkpoint = state.checkpoint("review_schedule", current["id"])
    proof = checkpoint and checkpoint["payload"]
    if not proof:
        return False
    try:
        event = proof["event"]
        identity_matches = all(current[key] == proof["card"][key] for key in ("id", "nid", "ord"))
        if not identity_matches or not same_schedule(current, proof["card"]):
            return False
        generation, prior = proof.get("reset_generation"), payload.get("card", {}).get("reset_generation")
        if type(generation) is int and type(prior) is int and generation != prior:
            return generation > prior >= 0
        order = lambda value: (datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00")), value["sync_id"])
        return order(event) >= order(payload)
    except (KeyError, TypeError, ValueError):
        return False


def update_review_stats(collection, payload, *, direction=1):
    cutoff = collection.sched.day_cutoff
    date = datetime.fromtimestamp(cutoff).date() - timedelta(days=1)
    start = datetime.combine(date, time(hour=collection.get_config("rollover", 4))).timestamp()
    instant = datetime.fromisoformat(payload["reviewed_at"].replace("Z", "+00:00")).timestamp()
    if start <= instant < cutoff:
        source = payload["anki"]
        before = source.get("answered", source["card_before"])
        collection.sched.update_stats(before["did"], new_delta=direction * int(before["queue"] == 0),
                                      review_delta=direction * int(before["queue"] in (2, 3)),
                                      milliseconds_delta=direction * source["revlog"]["time"])


def apply_review(collection, state, payload):
    from anki.cards_pb2 import FsrsMemoryState

    verify_platform(collection)
    sync_id = payload.get("sync_id")
    source = payload.get("anki")
    if not isinstance(sync_id, str) or not sync_id or not isinstance(source, dict):
        raise ReviewConflict("This review has no bound Anki source state")
    before, after = source["card_before"], source["card_after"]
    card_id = payload["anki_card_id"]
    if type(card_id) is not int or card_id <= 0 or any(before[key] != after[key] for key in ("id", "nid", "ord")) or after["id"] != card_id:
        raise ReviewConflict("The delivered card identity changed")
    card = collection.get_card(card_id)
    note = collection.get_note(card.nid)
    if note.guid != source["note_guid"] or note.mid != source["anki_type_id"] or card.nid != after["nid"] or card.ord != after["ord"]:
        raise ReviewConflict("The source note or template identity changed")
    reserved = state.review(sync_id)
    if reserved and reserved["payload"] != payload:
        raise ReviewConflict("The delivered review changed after it was reserved")
    if not reserved:
        instant = datetime.fromisoformat(payload["reviewed_at"].replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            raise ReviewConflict("The review timestamp needs a timezone")
        source_id = int(instant.timestamp()) * 1000 + instant.microsecond // 1000
        while history_row(collection, source_id) or state.review_id_reserved(source_id):
            source_id += 1
        if not 0 < source_id < 2**63:
            raise ReviewConflict("The review ID is out of range")
        row = {**source["revlog"], "id": source_id, "usn": collection.usn()}
        if set(row) != set(REVLOG_COLUMNS) or any(type(value) is not int for value in row.values()) or row["cid"] != card_id or row["ease"] != payload["rating"]:
            raise ReviewConflict("The review history payload is invalid")
        reserved = state.prepare_review(sync_id, payload, row)
    row = reserved["row"]
    existing = history_row(collection, row["id"])
    if existing:
        if any(existing[key] != row[key] for key in REVLOG_COLUMNS if key != "usn"):
            raise ReviewConflict("The assigned review ID belongs to a different event")
        state.finish_review(sync_id)
        return {"row": existing, "sync_id": sync_id}
    if reserved["applied"]:
        raise ReviewConflict("An applied review was removed in Anki")

    def insert_history():
        collection.db.execute(f"INSERT INTO revlog ({','.join(REVLOG_COLUMNS)}) VALUES ({','.join('?' for _ in REVLOG_COLUMNS)})",
                              *(row[key] for key in REVLOG_COLUMNS))
        update_review_stats(collection, payload)

    def apply():
        current = card_row(collection, card_id)
        if canonical_review_applied(state, current, payload):
            insert_history()
            return
        if payload.get("history_only"):
            raise ReviewConflict("Reconcile the card's schedule before delivering its review history")
        if current["did"] != before["did"] or not same_state(current, before):
            raise ReviewConflict("The card changed independently before this review arrived")
        current_data = json.loads(current["data"] or "{}")
        after_data = json.loads(after["data"] or "{}")
        for key in STATE_COLUMNS:
            setattr(card, key, after[key])
        card.did = after["did"]
        card.memory_state = FsrsMemoryState(stability=after_data["s"], difficulty=after_data["d"]) if "s" in after_data and "d" in after_data else None
        card.last_review_time = after_data.get("lrt")
        collection.update_card(card, skip_undo_entry=True)
        written = card_row(collection, card_id)
        auxiliary = lambda data: {key: value for key, value in data.items() if key not in ("s", "d", "lrt")}
        if not same_state(written, after) or auxiliary(json.loads(written["data"] or "{}")) != auxiliary(current_data) or written["flags"] != current["flags"]:
            raise ReviewConflict("Anki cannot preserve this card's source data through its public API")
        insert_history()

    collection.db.transact(apply)
    state.finish_review(sync_id)
    return {"row": history_row(collection, row["id"]), "sync_id": sync_id}
