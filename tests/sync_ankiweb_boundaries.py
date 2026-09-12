"""Exercise imported history, source schemas and selected-deck limits through native sync."""

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import uuid

import support
from anki.buildinfo import version
from anki.collection import Collection
from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageOptions, ImportAnkiPackageRequest
from ankiweb_live_probe import LiveAccountProbe
from native_sync_server import native_sync_server, full_sync
from study_corpus import add_type
from sync_competing_reviews import drain
from sync_http_roundtrip import authorize, emit
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_collection import SyncCollection
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import card_row, history_row, same_state
from shirabe_addon.sync_snapshot import SnapshotConflict, SyncSnapshot


def proceed():
    assert json.loads(sys.stdin.readline())["continue"]


def history_for(collection, identities):
    return [history_row(collection, identity) for identity in collection.db.list(
        "SELECT id FROM revlog WHERE cid IN (?, ?, ?) ORDER BY id", *identities)]


def legacy_package(root, prefix):
    source = Collection(str(root / "legacy.anki2"))
    try:
        assert not source.get_config("fsrs", False)
        home, outside = source.decks.id(prefix), source.decks.id(prefix + " outside")
        model = add_type(source, prefix, ["Expression", "Meaning"], [
            ("Recognition", "{{Expression}}", "{{Meaning}}", None),
            ("Production", "{{Meaning}}", "{{Expression}}", outside)])
        identities = []
        for label in ("Legacy history", "Filtered source", "Limited new card"):
            note = source.new_note(model)
            note.fields = [label, "original meaning"]
            source.add_note(note, home)
            identities.append(note.id)
        for card in source.get_note(identities[0]).cards():
            card.start_timer()
            source.sched.answerCard(card, 4)
            assert source.get_card(card.id).memory_state is None
        package = root / "legacy.apkg"
        source.export_anki_package(out_path=str(package), limit=None, options=ExportAnkiPackageOptions(
            with_scheduling=True, with_deck_configs=False, with_media=False))
        return package, identities
    finally:
        source.close()


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
        emit({"stage": "progress", "phase": "Prepared desktop copy", "done": 0, "total": 6})
        receiver = probe.clone("receiver", source=config.get("ankiweb_source"))
        emit({"stage": "progress", "phase": "Prepared receiver copy", "done": 0, "total": 6})

        def sync(collection):
            probe.sync(collection)
            emit({"stage": "progress", "phase": "AnkiWeb boundary probe", "done": probe.syncs, "total": 6})

        prefix = "Shirabe Test Boundaries " + uuid.uuid4().hex[:12]
        package, note_ids = legacy_package(root, prefix)
        desktop.import_anki_package(ImportAnkiPackageRequest(package_path=str(package),
            options=ImportAnkiPackageOptions(with_scheduling=True, with_deck_configs=False)))
        assert desktop.get_config("fsrs") is True
        home, outside = desktop.decks.id(prefix), desktop.decks.id(prefix + " outside")
        selected = [next(card.id for card in desktop.get_note(identity).cards() if card.did == home)
                    for identity in note_ids]
        excluded = [next(card.id for card in desktop.get_note(identity).cards() if card.did == outside)
                    for identity in note_ids]
        legacy_history = history_row(desktop, desktop.db.scalar("SELECT id FROM revlog WHERE cid = ?", selected[0]))
        assert desktop.get_card(selected[0]).memory_state is None
        preset = desktop.decks.add_config(prefix)
        preset.update(desiredRetention=0.86)
        preset["new"]["perDay"], preset["rev"]["perDay"] = 2, 3
        desktop.decks.update_config(preset)
        deck = desktop.decks.get(home)
        deck.update(conf=preset["id"], newToday=[desktop.sched.today, 1], revToday=[desktop.sched.today, 0],
                    timeToday=[desktop.sched.today, legacy_history["time"]])
        desktop.decks.save(deck)
        filtered = desktop.decks.new_filtered(prefix + " filtered")
        definition = desktop.decks.get(filtered)
        definition.update(resched=False, terms=[[f"cid:{selected[1]}", 1, 0]])
        desktop.decks.save(definition)
        assert desktop.sched.rebuild_filtered_deck(filtered).count == 1
        assert desktop.get_card(selected[1]).odid == home
        (root / "fixture.json").write_text(json.dumps({"deck": prefix, "home": home, "outside": outside,
            "filtered": filtered, "note_ids": note_ids, "selected": selected, "excluded": excluded,
            "live": not config.get("rehearsal")}, indent=2) + "\n")
        sync(desktop)
        sync(receiver)
        snapshot = SyncSnapshot(path, client)
        try:
            snapshot.capture(desktop, [filtered])
        except SnapshotConflict:
            pass
        else:
            raise AssertionError("A filtered session was accepted as a selected deck")
        captured = snapshot.capture(desktop, [home])
        assert captured["unselected_siblings"] == 3
        profile = snapshot.upload()
        delivery = SyncDelivery(path, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        changes = SyncChanges(path, client)
        excluded_before = [card_row(desktop, identity) for identity in excluded]
        excluded_history = history_for(desktop, excluded)
        filtered_counters = {key: desktop.decks.get(filtered)[key] for key in ("newToday", "revToday", "timeToday")}
        emit({"stage": "boundaries_uploaded", "profile_id": profile["id"], "note_ids": note_ids,
              "selected": selected, "excluded": excluded, "home": home, "filtered": filtered,
              "legacy_history": legacy_history, "preset": preset})
        proceed()
        drain(delivery, desktop)
        assert desktop.get_card(selected[0]).memory_state is not None
        assert desktop.get_card(selected[1]).odid == 0 and desktop.get_card(selected[1]).did == home
        assert [card_row(desktop, identity) for identity in excluded] == excluded_before
        assert history_for(desktop, excluded) == excluded_history
        assert desktop.decks.get(home)["newToday"] == [desktop.sched.today, 2]
        assert {key: desktop.decks.get(filtered)[key] for key in filtered_counters} == filtered_counters
        assert desktop.get_note(note_ids[0]).fields[1] == "Edited whole note from Shirabe"
        sync(desktop)
        sync(receiver)
        for identity in selected + excluded:
            assert same_state(card_row(desktop, identity), card_row(receiver, identity))
        receiver.models._clear_cache()
        model = receiver.get_note(note_ids[0]).note_type()
        receiver.models.rename_field(model, model["flds"][0], "Prompt")
        model["tmpls"][0]["name"] = "Renamed recognition"
        model["css"] = ".card { color: #234567; }"
        receiver.models.save(model)
        preset = receiver.decks.config_dict_for_deck_id(home)
        preset.update(desiredRetention=0.91)
        preset["new"]["perDay"], preset["rev"]["perDay"] = 5, 7
        receiver.decks.update_config(preset)
        sync(receiver)
        sync(desktop)
        desktop.models._clear_cache()
        settings = SyncCollection(path, client)
        settings.capture(desktop)
        settings.upload()
        changes.capture(desktop)
        changes.upload()
        changes.apply(desktop)
        drain(delivery, desktop)
        assert [card_row(desktop, identity) for identity in excluded] == excluded_before
        assert history_for(desktop, excluded) == excluded_history
        emit({"stage": "boundaries_updated", "definition": desktop.get_note(note_ids[0]).note_type(),
              "preset": desktop.decks.config_dict_for_deck_id(home),
              "cards": [card_row(desktop, identity) for identity in selected],
              "history": history_for(desktop, selected)})
        proceed()
        result = probe.report(deck=prefix, note_ids=note_ids, selected=selected, excluded=excluded,
            native_schema_rename=True, native_presets=True, legacy_sm2_history=True,
            filtered_home_review=True, mixed_deck_selection=True, media_tested=False)
        emit({"stage": "boundaries_verified", **result})


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
