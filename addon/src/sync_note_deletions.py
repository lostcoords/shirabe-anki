from __future__ import annotations

import json

from .sync_cards import validate_card_row
from .sync_notes import NoteConflict
from .sync_reviews import verify_platform
from .sync_snapshot import SyncSnapshot, source_digest


DELETION_RECEIPT = "shirabe_pending_note_deletion"


def clear_note_deletion_receipt(collection, change):
    if collection.get_config(DELETION_RECEIPT) == source_digest(change):
        collection.set_config(DELETION_RECEIPT, None)


def note_deletion_cards(change):
    payload = change["payload"]
    remote, entries, definition = payload.get("note"), payload.get("cards"), payload.get("definition")
    source_note = payload.get("source_note")
    if not isinstance(remote, dict) or not isinstance(entries, list) or not entries or not isinstance(definition, dict) or not isinstance(source_note, dict):
        raise NoteConflict("Invalid note deletion request")
    source_id = remote.get("anki_id")
    if type(source_id) is not int or not 0 < source_id < 2**63:
        raise NoteConflict("Invalid note deletion identity")
    expected = {}
    for entry in entries:
        if not isinstance(entry, dict) or "last_event_id" not in entry or (entry["last_event_id"] is not None and
                (type(entry["last_event_id"]) is not int or not 0 < entry["last_event_id"] < 2**63)):
            raise NoteConflict("Invalid deletion review checkpoint")
        row = entry.get("row")
        validate_card_row(row)
        if row["nid"] != source_id or row["id"] in expected:
            raise NoteConflict("Invalid deletion sibling identities")
        expected[row["id"]] = entry
    return expected


def ensure_note_absent(collection, change):
    expected = note_deletion_cards(change)
    source_id = change["payload"]["note"]["anki_id"]
    if collection.db.scalar("SELECT count(*) FROM notes WHERE id = ?", source_id) or collection.db.scalar(
            "SELECT count(*) FROM cards WHERE nid = ? OR id IN (SELECT value FROM json_each(?))",
            source_id, json.dumps(list(expected))):
        raise NoteConflict("The note or a requested card exists in Anki. Reopen the deletion chooser.")


def apply_note_deletion(collection, state, change, selected_decks):
    verify_platform(collection)
    expected = note_deletion_cards(change)
    payload = change["payload"]
    remote, definition, source_note = payload["note"], payload["definition"], payload["source_note"]
    source_id = remote["anki_id"]

    exists = collection.db.scalar("SELECT count(*) FROM notes WHERE id = ?", source_id)
    started = state.note_deletion(change["cursor"])
    marker = collection.get_config(DELETION_RECEIPT)
    if started and started != change:
        raise NoteConflict("The pending deletion changed")
    if marker is not None:
        if not started or marker != source_digest(change):
            raise NoteConflict("Another native deletion or a changed receipt is waiting for its durable result")
        remaining = collection.db.scalar("SELECT count(*) FROM cards WHERE nid = ? OR id IN (SELECT value FROM json_each(?))",
                                         source_id, json.dumps(list(expected)))
        if exists or remaining:
            raise NoteConflict("An interrupted deletion has a live note or card. Reconcile before deleting again.")
        return
    if not exists:
        raise NoteConflict("The source note disappeared before deletion could be verified")
    note = collection.get_note(source_id)
    model = note.note_type()
    if note.guid != remote.get("guid") or note.mid != remote.get("anki_type_id") or not model or \
            [field["name"] for field in model["flds"]] != remote.get("field_names"):
        raise NoteConflict("The note identity or field layout changed before deletion")
    if note.fields != remote.get("fields") or set(note.tags) != set(remote.get("tags", [])):
        raise NoteConflict("The note fields or tags changed before deletion")
    current_note = next(SyncSnapshot._rows(collection, "notes", "id = ?", (source_id,)))
    if set(current_note) != set(source_note) or any(current_note[key] != source_note[key]
            for key in current_note if key not in ("mod", "usn", "sfld", "csum", "flds", "tags")):
        raise NoteConflict("The source note metadata changed before deletion")
    if {key: value for key, value in model.items() if key not in ("mod", "usn")} != \
            {key: value for key, value in definition.items() if key not in ("mod", "usn")}:
        raise NoteConflict("The note type changed before deletion")
    cards = list(SyncSnapshot._rows(collection, "cards", "nid = ?", (source_id,), with_last_event=True))
    if {row["id"] for row in cards} != set(expected):
        raise NoteConflict("The complete sibling card set changed before deletion")
    for row in cards:
        if (row["odid"] or row["did"]) not in selected_decks:
            raise NoteConflict("A sibling card is outside the selected decks")
        entry = expected[row["id"]]
        if row.pop("last_event_id") != entry["last_event_id"]:
            raise NoteConflict("A sibling card has new review history")
        before = entry["row"]
        if any(row[key] != before[key] for key in row if key not in ("mod", "usn", "data")) or \
                json.loads(row["data"] or "{}") != json.loads(before["data"] or "{}"):
            raise NoteConflict("A sibling card changed before deletion")
    state.prepare_note_deletion(change)

    def delete():
        collection.remove_notes([source_id])
        collection.set_config(DELETION_RECEIPT, source_digest(change))

    collection.db.transact(delete)
