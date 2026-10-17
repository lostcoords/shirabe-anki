"""Apply a reconciled note on Anki's collection thread, without network I/O."""

from __future__ import annotations


class NoteConflict(Exception):
    pass


def accepted_note_source(row, remote):
    source = {**row, "flds": "\x1f".join(remote["fields"])}
    if set(source["tags"].split()) != set(remote["tags"]):
        source["tags"] = " " + " ".join(sorted(remote["tags"])) + " " if remote["tags"] else ""
    return source


def apply_note_change(collection, payload):
    remote = payload["note"]
    base = payload["base"]
    note = collection.get_note(remote["anki_id"])
    model = note.note_type()
    if not model or note.guid != remote["guid"] or note.mid != remote["anki_type_id"] or [field["name"] for field in model["flds"]] != remote["field_names"]:
        raise NoteConflict("The note identity or field layout changed")
    if len(base["fields"]) != len(note.fields) or len(remote["fields"]) != len(note.fields):
        raise NoteConflict("The note field count changed")
    fields = []
    for original, local, incoming in zip(base["fields"], note.fields, remote["fields"]):
        if local != original and incoming != original and local != incoming:
            raise NoteConflict("A note field changed independently in Anki")
        fields.append(incoming if local == original else local)
    original_tags, local_tags, remote_tags = set(base["tags"]), set(note.tags), set(remote["tags"])
    tags = sorted((original_tags & local_tags & remote_tags) | (local_tags - original_tags) | (remote_tags - original_tags))
    if fields != note.fields or set(tags) != set(note.tags):
        note.fields = fields
        note.tags = tags
        collection.update_note(note)
    return note
