"""SIGKILL disposable sync workers and compare recovery with a control sync."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys

import support
from anki.collection import Collection
from anki.dbproxy import DBProxy
from anki.media import MediaManager
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_collection import SyncCollection
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_media import SyncMedia
from shirabe_addon.sync_note_creations import CREATION_RECEIPT
from shirabe_addon.sync_note_deletions import DELETION_RECEIPT
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from shirabe_addon.sync_reviews import CARD_COLUMNS
from sync_http_roundtrip import authorize, emit
from sync_profile_roundtrip import counts, fingerprints


def await_kill(checkpoint):
    emit({"stage": "kill_ready", "checkpoint": checkpoint})
    sys.stdin.readline()
    raise AssertionError("The parent must kill this worker, not let it continue")


def interrupt_after(target, method, checkpoint, predicate=lambda args, kwargs: True):
    original = getattr(target, method)

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if predicate(args, kwargs):
            if checkpoint.endswith("_inside"):
                assert args[0].db.in_transaction, "The interruption must happen before the SQLite commit"
            await_kill(checkpoint)
        return result

    setattr(target, method, interrupted)


def install_interrupt(checkpoint, client):
    if checkpoint is None:
        return
    if checkpoint == "media_replacement_reserved":
        original = MediaManager.trash_files

        def before_trash(media, filenames):
            if filenames == ["_crash.svg"]:
                await_kill(checkpoint)
            return original(media, filenames)

        MediaManager.trash_files = before_trash
        return
    native_commits = {
        "review_before_native_commit": ("shirabe_addon.sync_reviews", "apply"),
        "review_after_native_commit": ("shirabe_addon.sync_reviews", "apply"),
        "card_before_native_commit": ("shirabe_addon.sync_cards", "apply_and_record"),
        "card_after_native_commit": ("shirabe_addon.sync_cards", "apply_and_record"),
        "undo_before_native_commit": ("shirabe_addon.sync_cards", "apply_and_record"),
        "undo_after_native_commit": ("shirabe_addon.sync_cards", "apply_and_record"),
        "creation_before_native_commit": ("shirabe_addon.sync_note_creations", "create"),
        "creation_after_native_commit": ("shirabe_addon.sync_note_creations", "create"),
        "deletion_before_native_commit": ("shirabe_addon.sync_note_deletions", "delete"),
        "deletion_after_native_commit": ("shirabe_addon.sync_note_deletions", "delete"),
        "card_choice_before_native_commit": ("shirabe_addon.sync_cards", "apply_and_record"),
        "card_choice_after_native_commit": ("shirabe_addon.sync_cards", "apply_and_record"),
    }
    if checkpoint in native_commits:
        transact = DBProxy.transact

        def interrupted_transaction(db, operation):
            if (operation.__module__, operation.__name__) != native_commits[checkpoint]:
                return transact(db, operation)

            def apply():
                operation()
                if checkpoint.endswith("before_native_commit"):
                    await_kill(checkpoint)

            result = transact(db, apply)
            if checkpoint.endswith("after_native_commit"):
                await_kill(checkpoint)
            return result

        DBProxy.transact = interrupted_transaction
        return
    hooks = {
        "capture_inside": (SyncState, "_enqueue", lambda args, kwargs: "notes" in args[2]),
        "snapshot_staged": (SyncState, "stage_snapshot", lambda args, kwargs: True),
        "media_staged": (SyncMedia, "_stage", lambda args, kwargs: True),
        "capture_saved": (SyncSnapshot, "capture", lambda args, kwargs: True),
        "pair_returned": (client, "pair", lambda args, kwargs: True),
        "deck_returned": (client, "select_deck", lambda args, kwargs: True),
        "receipt_saved": (SyncState, "remember_receipt", lambda args, kwargs: True),
        "checkpoint_inside": (SyncState, "_remember_checkpoint", lambda args, kwargs: args[1] == "source_note"),
        "checkpoint_saved": (SyncState, "complete_batch", lambda args, kwargs: True),
        "media_returned": (client, "upload_media", lambda args, kwargs: True),
        "finish_returned": (client, "finish_snapshot", lambda args, kwargs: True),
        "uploaded_saved": (SyncState, "put", lambda args, kwargs: args[1] == "initial_snapshot" and args[2]["phase"] == "uploaded"),
        "changes_capture_inside": (SyncState, "_enqueue", lambda args, kwargs: "notes" in args[2]),
        "changes_capture_saved": (SyncChanges, "capture", lambda args, kwargs: True),
        "changes_receipt_saved": (SyncState, "remember_receipt", lambda args, kwargs: True),
        "changes_checkpoint_inside": (SyncState, "_remember_checkpoint", lambda args, kwargs: args[1] == "source_note"),
        "changes_applied": (SyncChanges, "apply", lambda args, kwargs: True),
        "delivery_returned": (client, "deliver", lambda args, kwargs: True),
        "delivery_saved": (SyncState, "remember_delivery", lambda args, kwargs: True),
        "delivery_started": (SyncState, "begin_delivery", lambda args, kwargs: True),
        "review_reserved": (SyncState, "prepare_review", lambda args, kwargs: True),
        "review_finished": (SyncState, "finish_review", lambda args, kwargs: True),
        "delivery_checkpoint_inside": (SyncState, "_remember_checkpoint", lambda args, kwargs: args[1] == "source_card"),
        "delivery_change_saved": (SyncState, "finish_delivery_change", lambda args, kwargs: True),
        "delivery_return_saved": (SyncState, "prepare_delivery_return", lambda args, kwargs: True),
        "review_identity_returned": (client, "batch", lambda args, kwargs: "history" in args[1]),
        "delivery_acknowledged": (client, "acknowledge", lambda args, kwargs: True),
        "delivery_completed": (SyncState, "complete_delivery", lambda args, kwargs: True),
        "note_committed": (Collection, "update_note", lambda args, kwargs: True),
        "note_checkpoint_inside": (SyncState, "_remember_checkpoint", lambda args, kwargs: args[1] == "source_note"),
        "undo_choice_saved": (SyncState, "remember_delivery_choice", lambda args, kwargs: args[2].get("action") == "undo_review"),
        "undo_reserved": (SyncState, "prepare_review_undo", lambda args, kwargs: True),
        "undo_output_saved": (SyncState, "remember_review_undo_output", lambda args, kwargs: True),
        "undo_change_saved": (SyncState, "finish_delivery_change", lambda args, kwargs: kwargs.get("review_undo_batch") is not None),
        "undo_result_returned": (client, "batch", lambda args, kwargs: any("review_undo_cursor" in item for item in args[1].get("cards", []))),
        "creation_reserved": (SyncState, "prepare_note_creation", lambda args, kwargs: True),
        "creation_output_saved": (SyncState, "remember_note_creation_output", lambda args, kwargs: True),
        "creation_result_saved": (SyncState, "finish_delivery_change", lambda args, kwargs: "creation" in args[2]),
        "creation_binding_returned": (client, "batch", lambda args, kwargs: "creations" in args[1]),
        "deletion_reserved": (SyncState, "prepare_note_deletion", lambda args, kwargs: True),
        "deletion_result_saved": (SyncState, "finish_delivery_change", lambda args, kwargs: True),
        "deletion_cleanup_saved": (Collection, "set_config", lambda args, kwargs: args[1] == DELETION_RECEIPT and args[2] is None),
        "deletion_batch_returned": (client, "batch", lambda args, kwargs: any(item.get("deleted") for item in args[1].get("cards", []))),
        "note_choice_saved": (SyncState, "remember_delivery_choice", lambda args, kwargs: "note" in args[2]),
        "note_choice_committed": (Collection, "update_note", lambda args, kwargs: True),
        "note_choice_checkpoint_inside": (SyncState, "_remember_checkpoint", lambda args, kwargs: args[1] == "source_note"),
        "note_choice_result_saved": (SyncState, "finish_delivery_change", lambda args, kwargs: True),
        "note_choice_returned": (client, "batch", lambda args, kwargs: "notes" in args[1]),
        "card_choice_saved": (SyncState, "remember_delivery_choice", lambda args, kwargs: "card" in args[2]),
        "card_choice_checkpoint_inside": (SyncState, "_remember_checkpoint", lambda args, kwargs: args[1] == "source_card"),
        "card_choice_returned": (client, "batch", lambda args, kwargs: "cards" in args[1]),
        "outgoing_note_choice_saved": (SyncState, "stage_resolution", lambda args, kwargs: args[2] == "notes"),
        "outgoing_note_choice_returned": (client, "batch", lambda args, kwargs: any(item.get("conflict_id") for item in args[1].get("notes", []))),
        "outgoing_note_choice_committed": (Collection, "update_note", lambda args, kwargs: True),
        "outgoing_note_choice_checkpoint_inside": (SyncState, "_remember_checkpoint", lambda args, kwargs: args[1] == "source_note"),
        "outgoing_note_choice_entity_saved": (SyncState, "complete_entity", lambda args, kwargs: args[2] == "notes"),
        "settings_saved": (SyncState, "put", lambda args, kwargs: args[1] == "collection_settings" and args[2] is not None),
        "settings_returned": (client, "collection_settings", lambda args, kwargs: True),
        "settings_checkpoint_inside": (SyncState, "_put", lambda args, kwargs: args[1] == "collection_revision"),
        "settings_completed": (SyncCollection, "upload", lambda args, kwargs: True),
        "media_upload_returned": (client, "upload_media", lambda args, kwargs: True),
        "media_download_saved": (SyncMedia, "_download_file", lambda args, kwargs: args[3]["filename"] == "_crash.svg"),
        "media_exchange_saved": (SyncMedia, "exchange", lambda args, kwargs: True),
        "media_original_trashed": (MediaManager, "trash_files", lambda args, kwargs: args[1] == ["_crash.svg"]),
        "media_replacement_written": (MediaManager, "write_data", lambda args, kwargs: args[1] == "_crash.svg"),
        "media_checkpoint_saved": (SyncMedia, "_complete_incoming", lambda args, kwargs: args[2] == "_crash.svg"),
    }
    for kind in ("note_types", "notes", "history", "cards"):
        hooks[f"{kind}_returned"] = (client, "batch", lambda args, kwargs, kind=kind: kind in args[1])
        hooks[f"changes_{kind}_returned"] = hooks[f"{kind}_returned"]
    target, method, predicate = hooks[checkpoint]
    interrupt_after(target, method, checkpoint, predicate)


def daily_counts(collection):
    return {str(item.id): {key: collection.decks.get(item.id)[key] for key in ("newToday", "revToday", "timeToday")}
            for item in collection.decks.all_names_and_ids()}


def fingerprints_without_card_mod(collection, creation_guid=None):
    creation_id = collection.db.scalar("SELECT id FROM notes WHERE guid = ?", creation_guid) if creation_guid else None
    cards = hashlib.sha256()
    card_rows = []
    for row in SyncSnapshot._rows(collection, "cards", "1 = 1", ()):
        if creation_id is not None and row["nid"] == creation_id:
            row = {**row, "id": -1 - row["ord"], "nid": -1}
        card_rows.append(row)
    for row in sorted(card_rows, key=lambda row: row["id"]):
        values = [row[column] for column in CARD_COLUMNS if column != "mod"]
        cards.update((json.dumps(values, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    result = {**fingerprints(collection), "cards": cards.hexdigest()}
    if creation_guid:
        assert creation_id is not None
        notes = hashlib.sha256()
        for row in SyncSnapshot._rows(collection, "notes", "1 = 1", ()):
            values = [-1 if row["id"] == creation_id else row["id"], row["guid"], row["mid"], row["flds"].split("\x1f"), sorted(row["tags"].split())]
            notes.update((json.dumps(values, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        result["notes"] = notes.hexdigest()
    return result


def worker(config):
    reconciling = config.get("mode", "").startswith("incoming") or config.get("mode") in ("outgoing_note_choice", "media")
    root = Path(config["directory"]).resolve(strict=True)
    if not root.is_relative_to(Path(__file__).resolve().parents[2] / "tmp") or not (root / "collection.anki2").resolve(strict=True).is_relative_to(root):
        raise ValueError("Crash workers require a disposable workspace directory")
    client = SyncLogin(Path(config["login_path"]), config["origin"])
    path = root / "sync.sqlite3"
    install_interrupt(config.get("checkpoint"), client)
    with closing(Collection(str(root / "collection.anki2"))) as collection:
        assert collection.db.scalar("PRAGMA integrity_check") == "ok"
        before = fingerprints(collection)
        changes = SyncChanges(path, client)
        if config.get("mode") in ("incremental", "settings") or reconciling:
            with SyncState(path) as state:
                profile = state.get("initial_snapshot")["profile"]
            if config["mode"] == "incremental":
                changes.capture(collection)
                changes.upload()
                changes.apply(collection)
        else:
            snapshot = SyncSnapshot(path, client)
            snapshot.capture(collection, config["decks"])
            profile = snapshot.upload()
        with SyncState(path) as state:
            key = state.get("initial_snapshot")["pair_request"]["key"]
        delivery = SyncDelivery(path, client, profile_id=profile["id"], key=key)
        undo_confirmations = 0
        note_confirmations = 0
        card_confirmations = 0
        if config.get("mode") == "outgoing_note_choice":
            changes.capture(collection)
            changes.upload()
            try:
                changes.apply(collection)
            except SyncConflict as error:
                assert len(error.conflicts) == 1 and error.conflicts[0]["kind"] == "notes", error.conflicts
                problem = error.conflicts[0]
                prepared = changes.prepare_note_resolution(collection, problem, changes.note_conflict(problem))
                assert prepared["local"]["flds"].split("\x1f")[-1] == "edited in Anki 犬"
                assert prepared["detail"]["note"]["fields"][-1] == "edited in Shirabe 猫"
                changes.resolve_note(collection, prepared,
                                     [prepared["detail"]["note"]["fields"][0], "chosen meaning 猫と犬"], ["chosen-tag"])
                note_confirmations += 1
                changes.upload()
                changes.apply(collection)
        def receive_all():
            nonlocal undo_confirmations, note_confirmations, card_confirmations
            while True:
                page = delivery.receive()
                try:
                    delivery.apply(collection)
                except DeliveryConflict as error:
                    assert len(error.conflicts) == 1, error.conflicts
                    if config.get("mode") == "incoming_note_choice":
                        assert error.conflicts[0]["kind"] == "notes", error.conflicts
                        prepared = delivery.prepare_note_resolution(collection, error.conflicts[0])
                        assert prepared["local"]["flds"].split("\x1f")[-1] == "edited in Anki 犬"
                        assert prepared["detail"]["note"]["fields"][-1] == "edited in Shirabe 猫"
                        delivery.resolve_note(collection, prepared,
                                              [prepared["detail"]["note"]["fields"][0], "chosen meaning 猫と犬"], ["chosen-tag"])
                        note_confirmations += 1
                    elif config.get("mode") == "incoming_card_choice":
                        assert error.conflicts[0]["kind"] == "cards", error.conflicts
                        prepared = delivery.prepare_card_resolution(collection, error.conflicts[0])
                        assert prepared["local"]["queue"] == -2
                        assert prepared["local"]["flags"] == 3
                        assert prepared["detail"]["anki"]["card_after"]["queue"] == -1
                        delivery.resolve_card(collection, prepared, {"deck": "anki", "lifecycle": "shirabe", "properties": "anki"})
                        card_confirmations += 1
                    else:
                        assert config.get("mode") == "incoming_undo", error.conflicts
                        assert error.conflicts[0]["kind"] == "review_undos", error.conflicts
                        prepared = delivery.prepare_review_undo_resolution(collection, error.conflicts[0])
                        assert "undo_error" not in prepared, prepared
                        delivery.confirm_review_undo(collection, prepared)
                        undo_confirmations += 1
                    delivery.apply(collection)
                delivery.acknowledge()
                if not page["changes"]:
                    return

        receive_all()
        settings_metadata = None
        if config.get("mode") == "settings":
            collection_settings = SyncCollection(path, client)
            collection_settings.capture(collection)
            collection_settings.upload()
            assert collection_settings.capture(collection) is None
            settings_metadata = SyncSnapshot.metadata(collection)
            settings_metadata["capture"].pop("captured_at")
        if reconciling:
            changes.capture(collection)
            changes.upload()
            changes.apply(collection)
            receive_all()
        media_files = None
        if config.get("mode") == "media":
            media_sync = SyncMedia(path, client)
            media_sync.capture(collection)
            media_sync.exchange()
            media_sync.apply(collection)
            assert media_sync.capture(collection) == 0
            media_sync.exchange()
            assert media_sync.apply(collection) == 0
            media_files = {file.name: {"digest": hashlib.sha256(file.read_bytes()).hexdigest(), "bytes": file.stat().st_size}
                           for file in Path(collection.media.dir()).iterdir() if file.is_file() and not file.name.startswith(".")}
        assert not changes.capture(collection)["keys"], "Recovered upload queued a duplicate source change"
        changes.upload()
        changes.apply(collection)
        if not reconciling:
            assert fingerprints(collection) == before
        if config.get("mode") == "incoming_undo":
            needs_confirmation = config.get("recovery_checkpoint") in (None, "undo_reserved", "undo_output_saved", "undo_before_native_commit")
            assert undo_confirmations == int(needs_confirmation), (config.get("recovery_checkpoint"), undo_confirmations)
        if config.get("mode") in ("incoming_note_choice", "outgoing_note_choice"):
            assert note_confirmations == int(config.get("recovery_checkpoint") is None), note_confirmations
            chosen = collection.get_note(config["note_id"])
            assert chosen.fields[-1] == "chosen meaning 猫と犬"
            assert chosen.tags == ["chosen-tag"]
        if config.get("mode") == "incoming_card_choice":
            assert card_confirmations == int(config.get("recovery_checkpoint") is None), card_confirmations
            chosen = collection.get_card(config["card_id"])
            assert (chosen.queue, chosen.flags) == (-1, 3)
        with SyncState(path) as state:
            assert not state.pending()
            assert not state.get("changes")
            assert not state.get("delivery")
            assert not state.pending_review_undos()
            assert state.db.execute("SELECT count(*) FROM note_creations").fetchone()[0] == 0
            assert state.get("initial_snapshot")["phase"] == "uploaded"
            assert all(state.batch_applied(key) for key in state.snapshot_batch_keys())
            assert state.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert state.db.execute("SELECT count(*) FROM media_uploads WHERE receipt IS NULL").fetchone()[0] == 0
            assert state.db.execute("SELECT count(*) FROM media_incoming").fetchone()[0] == 0
            assert state.db.execute("SELECT count(*) FROM media_replacements").fetchone()[0] == 0
            if config.get("mode") == "settings":
                assert state.get("collection_settings") is None
                assert state.get("collection_revision") == 1
        assert collection.get_config(CREATION_RECEIPT) is None
        assert collection.get_config(DELETION_RECEIPT) is None
        emit({"stage": "worker_complete", "profile_id": profile["id"], "fingerprints": fingerprints(collection),
              "fingerprints_without_card_mod": fingerprints_without_card_mod(collection, config.get("creation_guid")), "daily_counts": daily_counts(collection),
              "card_rows": list(SyncSnapshot._rows(collection, "cards", "1 = 1", ())),
              "counts": counts(collection),
              "settings_metadata": settings_metadata,
              "media_files": media_files,
              "note_types": [collection.models.get(source_id) for source_id in collection.db.list("SELECT DISTINCT mid FROM notes ORDER BY mid")]})


def worker_stage(process):
    pending = b""
    with selectors.DefaultSelector() as reader:
        reader.register(process.stdout, selectors.EVENT_READ)
        while True:
            assert reader.select(40), "Crash worker stopped reporting"
            chunk = os.read(process.stdout.fileno(), 64 * 1024)
            assert chunk, f"Crash worker exited with {process.poll()}"
            pending += chunk
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                if line.startswith(b"{"):
                    return json.loads(line)


def run_worker(config, checkpoint=None):
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        process.stdin.write(json.dumps({**config, "checkpoint": checkpoint}) + "\n")
        process.stdin.flush()
        result = worker_stage(process)
        if checkpoint:
            assert result == {"stage": "kill_ready", "checkpoint": checkpoint}, result
            assert process.poll() is None
            process.kill()
            assert process.wait(timeout=10) == -signal.SIGKILL
            if checkpoint in ("review_before_native_commit", "review_after_native_commit"):
                directory = Path(config["directory"])
                with closing(sqlite3.connect(directory / "collection.anki2")) as native:
                    expected = 1 if checkpoint == "review_before_native_commit" else 2
                    assert native.execute("SELECT count(*) FROM revlog").fetchone()[0] == expected
                with closing(sqlite3.connect((directory / "sync.sqlite3").as_uri() + "?mode=ro", uri=True)) as sidecar:
                    assert sidecar.execute("SELECT applied FROM reviews").fetchall() == [(0,)]
            if config.get("mode") == "incoming_edits":
                with closing(sqlite3.connect(Path(config["directory"]) / "collection.anki2")) as native:
                    queue, fields, tags = native.execute("SELECT c.queue, n.flds, n.tags FROM cards c JOIN notes n ON n.id = c.nid WHERE c.id = ?",
                                                        (config["card_id"],)).fetchone()
                    assert fields.split("\x1f")[-1] == "edited in Shirabe 猫"
                    assert tags.split() == ["edited-in-shirabe"]
                    assert queue == (-1 if checkpoint == "card_after_native_commit" else config["source_queue"])
            if config.get("mode") == "incoming_undo":
                directory = Path(config["directory"])
                committed = checkpoint in ("undo_after_native_commit", "undo_change_saved", "undo_result_returned")
                with closing(sqlite3.connect((directory / "collection.anki2").as_uri() + "?mode=ro", uri=True)) as native:
                    assert native.execute("SELECT count(*) FROM revlog").fetchone()[0] == (1 if committed else 2)
                with closing(sqlite3.connect((directory / "sync.sqlite3").as_uri() + "?mode=ro", uri=True)) as sidecar:
                    assert sidecar.execute("SELECT count(*) FROM review_undos").fetchone()[0] == (0 if checkpoint == "undo_choice_saved" else 1)
            if config.get("mode") == "incoming_creation":
                directory = Path(config["directory"])
                committed = checkpoint not in ("creation_reserved", "creation_before_native_commit")
                with closing(sqlite3.connect((directory / "collection.anki2").as_uri() + "?mode=ro", uri=True)) as native:
                    assert native.execute("SELECT count(*) FROM notes").fetchone()[0] == (3 if committed else 2)
                    assert native.execute("SELECT count(*) FROM cards").fetchone()[0] == (5 if committed else 3)
                    assert native.execute("SELECT count(*) FROM revlog").fetchone()[0] == 1
            if config.get("mode") == "incoming_deletion":
                directory = Path(config["directory"])
                committed = checkpoint not in ("deletion_reserved", "deletion_before_native_commit")
                with closing(sqlite3.connect((directory / "collection.anki2").as_uri() + "?mode=ro", uri=True)) as native:
                    assert native.execute("SELECT count(*) FROM notes").fetchone()[0] == (1 if committed else 2)
                    assert native.execute("SELECT count(*) FROM cards").fetchone()[0] == (1 if committed else 3)
                    assert native.execute("SELECT count(*) FROM revlog").fetchone()[0] == 1
            if config.get("mode") in ("incoming_note_choice", "outgoing_note_choice"):
                directory = Path(config["directory"])
                committed = checkpoint not in ("note_choice_saved", "outgoing_note_choice_saved", "outgoing_note_choice_returned")
                with closing(sqlite3.connect((directory / "collection.anki2").as_uri() + "?mode=ro", uri=True)) as native:
                    fields, tags = native.execute("SELECT flds, tags FROM notes WHERE id = ?", (config["note_id"],)).fetchone()
                    assert fields.split("\x1f")[-1] == ("chosen meaning 猫と犬" if committed else "edited in Anki 犬")
                    assert tags.split() == (["chosen-tag"] if committed else ["edited-in-anki"])
                    assert native.execute("SELECT count(*) FROM revlog").fetchone()[0] == 1
                with closing(sqlite3.connect((directory / "sync.sqlite3").as_uri() + "?mode=ro", uri=True)) as sidecar:
                    if config["mode"] == "incoming_note_choice" and checkpoint != "note_choice_returned":
                        choices = sidecar.execute("SELECT choice FROM delivery_choices").fetchall()
                        assert len(choices) == 1
                        assert json.loads(choices[0][0])["note"]["fields"][-1] == "chosen meaning 猫と犬"
                    elif config["mode"] == "outgoing_note_choice":
                        assert sidecar.execute("SELECT count(*) FROM batch_resolutions").fetchone()[0] == 1
            if config.get("mode") == "incoming_card_choice":
                directory = Path(config["directory"])
                committed = checkpoint not in ("card_choice_saved", "card_choice_before_native_commit")
                with closing(sqlite3.connect((directory / "collection.anki2").as_uri() + "?mode=ro", uri=True)) as native:
                    row = native.execute("SELECT queue, flags FROM cards WHERE id = ?", (config["card_id"],)).fetchone()
                    assert row == (-1 if committed else -2, 3), row
                    assert native.execute("SELECT count(*) FROM revlog").fetchone()[0] == 1
                with closing(sqlite3.connect((directory / "sync.sqlite3").as_uri() + "?mode=ro", uri=True)) as sidecar:
                    if checkpoint != "card_choice_returned":
                        choices = sidecar.execute("SELECT choice FROM delivery_choices").fetchall()
                        assert len(choices) == 1
                        chosen = json.loads(choices[0][0])["card"]
                        assert (chosen["queue"], chosen["flags"]) == (-1, 3)
            if config.get("mode") == "settings":
                with closing(sqlite3.connect((Path(config["directory"]) / "sync.sqlite3").as_uri() + "?mode=ro", uri=True)) as sidecar:
                    values = dict(sidecar.execute("SELECT key, value FROM settings"))
                    if checkpoint == "settings_completed":
                        assert json.loads(values["collection_revision"]) == 1
                        assert json.loads(values["collection_settings"]) is None
                    else:
                        assert "collection_revision" not in values
                        assert json.loads(values["collection_settings"])["request"]["base_revision"] == 0
            if config.get("mode") == "media":
                directory = Path(config["directory"])
                destination = directory / "collection.media" / "_crash.svg"
                if checkpoint == "media_original_trashed":
                    assert not destination.exists()
                elif checkpoint in ("media_replacement_written", "media_checkpoint_saved"):
                    assert destination.read_bytes() == b'<svg xmlns="http://www.w3.org/2000/svg"><text>updated</text></svg>'
                else:
                    assert destination.read_bytes() == b'<svg xmlns="http://www.w3.org/2000/svg"><text>copy</text></svg>'
                with closing(sqlite3.connect((directory / "sync.sqlite3").as_uri() + "?mode=ro", uri=True)) as sidecar:
                    pending = checkpoint in ("media_replacement_reserved", "media_original_trashed", "media_replacement_written")
                    assert sidecar.execute("SELECT count(*) FROM media_replacements").fetchone()[0] == int(pending)
            emit({"stage": "progress", "phase": f"SIGKILL at {checkpoint}", "done": 1, "total": 1})
        else:
            assert result["stage"] == "worker_complete", result
            assert process.wait(timeout=10) == 0
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdin.close()
        process.stdout.close()


def run(config):
    reconciling = config.get("mode", "").startswith("incoming") or config.get("mode") in ("outgoing_note_choice", "media")
    root = Path(config["directory"]).resolve(strict=True)
    if not root.is_relative_to(Path(__file__).resolve().parents[2] / "tmp"):
        raise ValueError("Use a fresh disposable workspace directory")
    source = root / "source.anki2"
    if source.exists():
        raise ValueError("The source fixture must be new")
    media = {"_crash.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><text>copy</text></svg>',
             "_helper.css": b".card { color: black }"}
    with closing(Collection(str(source))) as collection:
        collection.set_config("fsrs", True)
        deck = collection.decks.id("Crash checks")
        child = collection.decks.id("Crash checks::Cloze")
        for filename, data in media.items():
            assert collection.media.write_data(filename, data) == filename
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ['猫 <img src="_crash.svg">', "cat"]
        note.tags = ["crash-check"]
        collection.add_note(note, deck)
        card = note.cards()[0]
        card.start_timer()
        collection.sched.answerCard(card, 4 if config.get("mode") in ("incoming", "incoming_undo") else 3)
        source_queue = collection.get_card(card.id).queue
        reviewed_at = collection.db.scalar("SELECT max(id) FROM revlog") / 1000 + 60
        cloze = collection.new_note(collection.models.by_name("Cloze"))
        cloze.fields = ["{{c1::猫}} {{c3::犬}}", "cat and dog"]
        collection.add_note(cloze, child)
        expected = fingerprints(collection)
        original_daily_counts = daily_counts(collection)
    with source.open("rb") as stream:
        source_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    changed = None
    if config.get("mode") == "incremental":
        changed = root / "changed.anki2"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(changed)) as copied:
            original.backup(copied)
        with closing(Collection(str(changed))) as collection:
            updated = collection.get_note(note.id)
            updated.fields[-1] = "edited meaning"
            updated.tags.append("edited-in-anki")
            collection.update_note(updated)
            reviewed = collection.get_card(card.id)
            reviewed.start_timer()
            collection.sched.answerCard(reviewed, 3)
            expected = fingerprints(collection)
    login_path = root / "login.sqlite3"
    client = SyncLogin(login_path, config["origin"])
    authorize(client)
    reports = []
    for name in ("control", "recovered"):
        directory = root / name
        directory.mkdir(mode=0o700)
        destination = directory / "collection.anki2"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
        shutil.copytree(source.with_suffix(".media"), destination.with_suffix(".media"))
        settings = {"directory": str(directory), "login_path": str(login_path), "origin": config["origin"], "decks": [deck]}
        if config.get("mode") == "settings":
            run_worker(settings)
            with closing(Collection(str(destination))) as collection:
                changed_deck = collection.decks.get(deck)
                changed_deck.update(newLimit=7, reviewLimit=31)
                collection.decks.update(changed_deck)
            settings["mode"] = "settings"
        if changed:
            run_worker(settings)
            with closing(sqlite3.connect(changed.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(destination)) as copied:
                original.backup(copied)
            settings["mode"] = "incremental"
        if reconciling:
            initial = run_worker(settings)
            emit({"stage": "prepare_incoming", "profile_id": initial["profile_id"], "card_id": card.id,
                  "reviewed_at": reviewed_at, "deletion_note_id": cloze.id})
            assert json.loads(sys.stdin.readline())["continue"]
            settings.update(mode=config["mode"], card_id=card.id, source_queue=source_queue)
            if config["mode"] == "media":
                with closing(Collection(str(destination))) as collection:
                    collection.media.write_data("_native-later.bin", b"later native asset")
            if config["mode"] in ("incoming_note_choice", "outgoing_note_choice"):
                settings["note_id"] = note.id
                with closing(Collection(str(destination))) as collection:
                    local = collection.get_note(note.id)
                    local.fields[-1] = "edited in Anki 犬"
                    local.tags = ["edited-in-anki"]
                    collection.update_note(local)
            if config["mode"] == "incoming_card_choice":
                with closing(Collection(str(destination))) as collection:
                    local = collection.get_card(card.id)
                    local.queue, local.flags = -2, 3
                    collection.update_card(local)
            if config["mode"] == "incoming_creation":
                settings["creation_guid"] = "crash-created-note"
            if config["mode"] == "incoming_undo":
                reviewed = run_worker({**settings, "mode": "incoming"})
                assert reviewed["counts"]["history"] == 2
                emit({"stage": "prepare_undo", "profile_id": initial["profile_id"], "card_id": card.id})
                assert json.loads(sys.stdin.readline())["continue"]
        if name == "recovered":
            run_worker(settings, config["checkpoint"])
        report = run_worker({**settings, "recovery_checkpoint": config["checkpoint"] if name == "recovered" else None})
        if config.get("mode") == "incoming":
            assert report["counts"]["history"] == 2
            assert report["daily_counts"][str(deck)]["timeToday"][1] == original_daily_counts[str(deck)]["timeToday"][1] + 2000
            assert report["daily_counts"][str(deck)]["revToday"][1] == original_daily_counts[str(deck)]["revToday"][1] + 1
            assert report["daily_counts"][str(deck)]["newToday"] == original_daily_counts[str(deck)]["newToday"]
        elif config.get("mode") in ("incoming_edits", "incoming_undo", "incoming_creation", "incoming_deletion", "incoming_note_choice", "incoming_card_choice", "outgoing_note_choice"):
            assert report["counts"]["history"] == 1
            assert report["daily_counts"] == original_daily_counts
        else:
            assert report["fingerprints"] == expected
        expected_media = media
        if config.get("mode") == "media":
            expected_media = {**media, "_crash.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><text>updated</text></svg>',
                              "_native-later.bin": b"later native asset", "_remote-later.bin": b"later Shirabe asset"}
            assert report["counts"]["history"] == 1
            assert report["daily_counts"] == original_daily_counts
        reports.append({key: value for key, value in report.items() if key not in ("fingerprints", "card_rows") or not reconciling})
        emit({**report, "stage": f"{name}_uploaded", "checkpoint": config["checkpoint"],
              "media": {filename: {"digest": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for filename, data in expected_media.items()}})
        assert json.loads(sys.stdin.readline())["continue"]
    assert reports[0] == {**reports[1], "profile_id": reports[0]["profile_id"]}
    with source.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == source_digest
    client.sign_out()
    emit({"stage": "crash_recovery_complete", "checkpoint": config["checkpoint"], "source_unchanged": True})


if __name__ == "__main__":
    config = json.loads(sys.stdin.readline())
    worker(config) if "--worker" in sys.argv else run(config)
