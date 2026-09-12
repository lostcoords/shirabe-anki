from __future__ import annotations

from datetime import datetime, timezone
import copy
import hmac

from .sync_notes import NoteConflict
from .sync_reviews import verify_platform
from .sync_snapshot import SyncSnapshot, source_digest


CREATION_RECEIPT = "shirabe_pending_note_creation"


def apply_note_creation(collection, state, change, selected_decks):
    verify_platform(collection)
    payload = change["payload"]
    remote, creation = payload["note"], payload["creation"]
    entries = payload["cards"]
    if remote.get("anki_id") is not None or not isinstance(remote.get("guid"), str) or not remote["guid"] or \
            not isinstance(remote.get("fields"), list) or not all(isinstance(value, str) for value in remote["fields"]) or \
            not isinstance(remote.get("tags"), list) or not all(isinstance(tag, str) for tag in remote["tags"]) or \
            not isinstance(entries, list) or any(not isinstance(entry, dict) or type(entry.get("ordinal")) is not int or
                entry["ordinal"] < 0 or type(entry.get("card_id")) is not int or entry["card_id"] <= 0 for entry in entries):
        raise NoteConflict("Invalid note creation request")
    expected = {entry["ordinal"]: entry["anki_deck_id"] for entry in entries}
    if len(expected) != len(entries) or len({entry["card_id"] for entry in entries}) != len(entries):
        raise NoteConflict("Duplicate creation sibling identities")
    if creation["anki_deck_id"] not in selected_decks or set(expected.values()) - selected_decks:
        raise NoteConflict("A creation destination is outside the selected decks")

    intent = state.prepare_note_creation(change)
    output = intent["output"]
    marker = collection.get_config(CREATION_RECEIPT)
    receipt = _receipt(intent, output) if output is not None else None
    applied = state.delivery_result(change["cursor"])
    if applied is not None:
        if marker == receipt and receipt is not None:
            collection.set_config(CREATION_RECEIPT, None)
        return applied
    if output is None and marker is not None:
        candidate = marker.get("output") if isinstance(marker, dict) else None
        if not isinstance(candidate, dict) or marker != _receipt(intent, candidate):
            raise NoteConflict("Another native creation or a changed receipt is waiting for its durable result")
        output = candidate
        receipt = _receipt(intent, output)
    if output is not None:
        if marker != receipt:
            raise NoteConflict("The prepared creation has no matching native commit receipt. Reconcile before retrying.")
        _verify_created_identities(collection, output)
    else:
        owned = "owned_type_id" in creation
        if owned:
            model = _owned_model(collection, state, payload)
        else:
            model = collection.models.get(remote["anki_type_id"])
            if not model or _without_stamps(model) != _without_stamps(payload["definition"]):
                raise NoteConflict("The note type changed before creation")
        definition = model or payload["definition"]
        if [field["name"] for field in definition["flds"]] != remote["field_names"]:
            raise NoteConflict("The note type changed before creation")
        if len(remote["fields"]) != len(definition["flds"]):
            raise NoteConflict("The creation field count changed")
        if collection.db.scalar("SELECT count(*) FROM notes WHERE guid = ?", remote["guid"]):
            raise NoteConflict("An existing GUID is not proof that this creation was applied")
        deck_ids = {item.id for item in collection.decks.all_names_and_ids()}
        if any(deck_id not in deck_ids or collection.decks.get(deck_id).get("dyn")
               for deck_id in {creation["anki_deck_id"], *expected.values()}):
            raise NoteConflict("A creation destination no longer exists or is filtered")

        def create():
            nonlocal output, model
            if model is None:
                requested = copy.deepcopy(payload["definition"])
                requested["id"] = 0
                identity = collection.models.add_dict(requested).id
                model = collection.models.get(identity)
                if not _published_definition_matches(payload["definition"], model):
                    raise NoteConflict("Anki changed the owned note type during creation")
            note = collection.new_note(model)
            note.guid, note.fields, note.tags = remote["guid"], list(remote["fields"]), list(remote["tags"])
            collection.add_note(note, creation["anki_deck_id"])
            written = collection.get_note(note.id)
            if written.guid != remote["guid"] or written.mid != model["id"] or \
                    written.fields != remote["fields"] or set(written.tags) != set(remote["tags"]):
                raise NoteConflict("Anki changed the note content during creation")
            cards = list(SyncSnapshot._rows(collection, "cards", "nid = ?", (note.id,)))
            if len(cards) != len(expected) or {card["ord"]: card["did"] for card in cards} != expected:
                raise NoteConflict("Anki generated different siblings or destinations")
            result = {"creation": {"key": creation["key"], "delivery_cursor": change["cursor"],
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "note": next(SyncSnapshot._rows(collection, "notes", "id = ?", (note.id,))), "cards": cards}}
            if owned:
                result["creation"].update(owned_type_id=creation["owned_type_id"], definition=copy.deepcopy(model))
            collection.set_config(CREATION_RECEIPT, _receipt(intent, result))
            output = result

        try:
            collection.db.transact(create)
        except Exception:
            collection.models._clear_cache()
            raise

    state.remember_note_creation_output(change["cursor"], output)
    if "owned_type_id" in output["creation"]:
        state.remember_checkpoint("created_note_type", output["creation"]["owned_type_id"],
            source_digest(payload["definition"]), output["creation"]["definition"])
    state.finish_delivery_change(change["cursor"], output)
    collection.set_config(CREATION_RECEIPT, None)
    return output


def _without_stamps(definition):
    return {key: value for key, value in definition.items() if key not in ("mod", "usn")}


def _owned_model(collection, state, payload):
    identity = payload["creation"]["owned_type_id"]
    definition = payload["definition"]
    marker = definition.get("shirabe", {}) if isinstance(definition, dict) else {}
    if type(identity) is not int or identity <= 0 or payload["note"].get("note_type_id") != identity or \
            payload["note"].get("anki_type_id") is not None or \
            not isinstance(marker, dict) or marker.get("kind") not in ("vocab", "kanji", "writing") or \
            type(marker.get("version")) is not int or marker["version"] <= 0 or definition.get("id") is not None or \
            definition.get("type") != 0 or not isinstance(definition.get("name"), str) or not definition["name"].strip() or \
            not isinstance(definition.get("flds"), list) or not definition["flds"] or \
            not isinstance(definition.get("tmpls"), list) or not definition["tmpls"]:
        raise NoteConflict("Invalid owned note type creation request")
    bound = state.checkpoint("created_note_type", identity)
    if bound is None:
        return None
    model = collection.models.get(bound["payload"]["id"])
    if bound["digest"] != source_digest(definition) or not model or _without_stamps(model) != _without_stamps(bound["payload"]):
        raise NoteConflict("The owned note type changed before creation")
    return model


def _published_definition_matches(requested, actual):
    for key, value in requested.items():
        if key in ("id", "mod", "usn", "req", "name"):
            continue
        if key in ("flds", "tmpls"):
            if len(value) != len(actual.get(key, [])):
                return False
            for source, target in zip(value, actual[key]):
                if any(target.get(name) != item for name, item in source.items() if name not in ("id", "mod", "usn")):
                    return False
        elif actual.get(key) != value:
            return False
    return actual["name"] == requested["name"] or actual["name"].startswith(requested["name"] + "-")


def _receipt(intent, output):
    proof = hmac.new(intent["nonce"].encode(), source_digest([intent["change"], output]).encode(), "sha256").hexdigest()
    return {"output": output, "proof": proof}


def _verify_created_identities(collection, output):
    created = output["creation"]
    note = created["note"]
    identity = collection.db.first("SELECT guid, mid FROM notes WHERE id = ?", note["id"])
    if not identity or tuple(identity) != (note["guid"], note["mid"]):
        raise NoteConflict("The created note identity changed or was undone")
    for card in created["cards"]:
        identity = collection.db.first("SELECT nid, ord FROM cards WHERE id = ?", card["id"])
        if not identity or tuple(identity) != (note["id"], card["ord"]):
            raise NoteConflict("A created card identity changed or was undone")
