"""Preserve disposable note definitions through native sync and the Rails importer."""

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import uuid

import support
from anki.buildinfo import version
from anki.collection import Collection
from ankiweb_live_probe import LiveAccountProbe
from native_sync_server import native_sync_server, full_sync
from study_corpus import add_type
from template_corpus import populate
from sync_http_roundtrip import authorize, emit
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot


def sources(collection, note_ids):
    collection.models._clear_cache()
    notes, cards, types = [], [], {}
    for identity in note_ids:
        note = collection.get_note(identity)
        notes.append({"id": note.id, "guid": note.guid, "mid": note.mid,
            "fields": note.fields, "tags": note.tags})
        cards.extend(SyncSnapshot._rows(collection, "cards", "nid = ?", (note.id,)))
        types[str(note.mid)] = collection.models.get(note.mid)
    return {"notes": notes, "cards": cards, "types": types}


def run(config):
    assert version == "26.08.1"
    root = Path(config["artifact_directory"])
    path = root / "sidecar.sqlite3"
    client = SyncLogin(path, config["origin"])
    authorize(client)
    with ExitStack() as stack:
        if config.get("rehearsal"):
            endpoint = stack.enter_context(native_sync_server(root))
            source = root / "rehearsal-source"
            source.mkdir()
            original = Collection(str(source / "collection.anki2"))
            original.set_config("fsrs", True)
            note = original.new_note(original.models.by_name("Basic"))
            note.fields = ["Existing note", "Must remain unchanged"]
            original.add_note(note, original.decks.id("Existing deck"))
            auth = original.sync_login("probe", "disposable", endpoint)
            original.sync_collection(auth, False)
            full_sync(original, auth, upload=True)
            original.close()
            probe = LiveAccountProbe(root / "copies", source, auth=auth)
        else:
            probe = LiveAccountProbe(root, config["ankiweb_profile"])
        stack.callback(probe.close)
        desktop = probe.clone("desktop", source=config.get("ankiweb_source"))
        receiver = probe.clone("receiver", source=config.get("ankiweb_source"))
        prefix = "Shirabe Test Types " + uuid.uuid4().hex[:12]
        deck = desktop.decks.id(prefix)
        note_ids = populate(desktop, deck, prefix=prefix)
        model = add_type(desktop, prefix + " reversed", ["意味", "Expression", "Optional extra"],
            [("Recognition", "{{Expression}}", "{{意味}}", None),
             ("Production", "{{意味}}", "{{Expression}}", None)], css=".card { color: #123456; }")
        note = desktop.new_note(model)
        note.fields = ["<b>cat</b>", "猫", "arbitrary field order"]
        note.tags = ["template_corpus", "case::reversed"]
        desktop.add_note(note, deck)
        note_ids.append(note.id)
        (root / "fixture.json").write_text(json.dumps({"deck": prefix, "deck_id": deck,
            "note_ids": note_ids, "live": not config.get("rehearsal")}, indent=2) + "\n")
        probe.sync(desktop)
        probe.sync(receiver)
        expected = sources(desktop, note_ids)
        (root / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
        received = sources(receiver, note_ids)
        (root / "received.json").write_text(json.dumps(received, ensure_ascii=False, indent=2) + "\n")
        assert received == expected
        snapshot = SyncSnapshot(path, client)
        snapshot.capture(receiver, [deck])
        profile = snapshot.upload(progress=lambda done, total: emit({"stage": "progress",
            "phase": "template source snapshot", "done": done, "total": total}))
        emit({"stage": "ankiweb_corpus_uploaded", "profile_id": profile["id"]})
        assert json.loads(sys.stdin.readline())["continue"]
        result = probe.report(deck=prefix, deck_id=deck, notes=len(note_ids),
            cards=len(expected["cards"]), note_types=len(expected["types"]),
            source_parity=True, media_tested=False, scripts_executed=False)
        emit({"stage": "ankiweb_corpus_verified", **result})


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
