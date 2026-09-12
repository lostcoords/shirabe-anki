from contextlib import closing
from collections import deque
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import support
from anki.collection import Collection
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin, private_sync_path
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit, source_rows
from sync_competing_reviews import drain


def run_session(collection, root, origin):
    from aqt.qt import QWidget
    from gui_safety import offscreen_test_application
    from shirabe_addon.sync_session import SyncSession

    app = offscreen_test_application([])
    window = QWidget()
    window.col = collection
    window.pm = SimpleNamespace(profileFolder=lambda: str(root))
    window.taskman = SimpleNamespace(run_on_main=lambda callback: callback())
    window.state = "deckBrowser"
    session = SyncSession(window, origin)
    pending = deque()

    def enqueue(operation, success, **options):
        pending.append((operation, success, options))
        return True

    session._run = enqueue
    try:
        assert session.start()
        steps = []
        while pending:
            assert len(steps) < 100, ("The sync session did not finish", steps[-12:])
            operation, success, options = pending.popleft()
            result = operation(collection) if options.get("collection") or options.get("mutating") else operation()
            steps.append((operation.__qualname__, result))
            success(result)
            assert not session.error, (session.error, session.conflicts)
        assert not session.busy
        assert session.status == "Saved changes exchanged."
    finally:
        session.close()
        window.deleteLater()
        app.processEvents()


def run(config):
    root = Path(config["directory"])
    target = root / "competing-undo.anki2"
    assert not target.exists()
    with closing(Collection(str(target))) as collection:
        collection.set_config("fsrs", True)
        deck = collection.decks.id("Competing Undo")
        burial = config.get("burial") or ""
        if config.get("burial"):
            preset = collection.decks.config_dict_for_deck_id(deck)
            preset["new"]["bury"] = True
            collection.decks.update_config(preset)
        generated = config.get("generated", False)
        note = collection.new_note(collection.models.by_name("Cloze" if generated else "Basic"))
        note.fields = ["{{c1::猫}}" if generated else "猫", "cat"]
        collection.add_note(note, deck)
        card = note.cards()[0]
        sidecar = private_sync_path(root)
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        captured = snapshot.capture(collection, [deck])
        profile = snapshot.upload()
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
        if config.get("creation"):
            emit({"stage": "creation_ready", "card_id": card.id})
            assert json.loads(sys.stdin.readline())["continue"]
            page = delivery.receive()
            delivery.apply(collection)
            created_id = collection.db.scalar("SELECT id FROM notes WHERE id != ?", note.id)
            note = collection.get_note(created_id)
            card = note.cards()[0]
            with SyncState(sidecar) as state:
                original_creation = state.note_creation(page["changes"][0]["cursor"])["output"]
        if generated:
            note.fields[0] += " {{c2::犬}}"
            if burial and "mixed" not in burial:
                note.fields[0] += " {{c3::鳥}}"
            collection.update_note(note)
            card = next(item for item in note.cards() if item.ord == 1)
        card.start_timer()
        collection.sched.answerCard(card, 4)
        if "mixed" in burial:
            note.fields[0] += " {{c3::鳥}}"
            collection.update_note(note)
        native = next(row for row in source_rows(collection, "cards") if row["id"] == card.id)
        native_siblings = {row["id"]: row for row in source_rows(collection, "cards") if row["id"] != card.id}
        native_history = source_rows(collection, "revlog")
        native_counters = collection.decks.get(deck)
        emit({"stage": "native_answer", "card": native, "history": native_history, "fields": note.fields,
              "creation": original_creation["creation"] if config.get("creation") else None, "siblings": list(native_siblings.values())})
        review_id = json.loads(sys.stdin.readline())["review_id"]
        changes = SyncChanges(sidecar, client)

        def answer_after_receipt():
            nonlocal card, native, native_history, native_counters
            card = collection.get_card(card.id)
            card.start_timer()
            collection.sched.answerCard(card, 4)
            native = next(row for row in source_rows(collection, "cards") if row["id"] == card.id)
            native_history = source_rows(collection, "revlog")
            native_counters = collection.decks.get(deck)
            emit({"stage": "native_after_receipt", "card": native, "history": native_history})
            assert json.loads(sys.stdin.readline())["continue"]

        if config.get("creation"):
            if config.get("lose_response"):
                send = client.batch

                def lose_creation_response(profile_id, payload):
                    assert payload["creations"] == [original_creation["creation"]]
                    send(profile_id, payload)
                    raise ConnectionError("Lost committed creation binding receipt")

                client.batch = lose_creation_response
                try:
                    delivery.acknowledge()
                except ConnectionError:
                    if config.get("after_receipt"):
                        answer_after_receipt()
                else:
                    raise AssertionError("Creation did not return its binding receipt")
                finally:
                    client.batch = send
            run_session(collection, root, config["origin"])
            assert collection.db.scalar("SELECT count(*) FROM notes") == 2
            settled = {table: source_rows(collection, table) for table in ("notes", "cards", "revlog")}
            run_session(collection, root, config["origin"])
            assert settled == {table: source_rows(collection, table) for table in settled}
        for cycle in range(0 if config.get("creation") else 2):
            batches = SyncSnapshot._batches

            def split_cards(scanner, kind, rows, key):
                if kind == "cards":
                    for row in sorted(rows, key=lambda entry: entry["row"]["ord"]):
                        yield from batches(scanner, kind, [row], key)
                else:
                    yield from batches(scanner, kind, rows, key)

            with patch.object(SyncSnapshot, "_batches", split_cards if burial.startswith("partial") else batches):
                changes.capture(collection)
            send, attempts = client.batch, []

            def lose_binding(profile_id, payload):
                result = send(profile_id, payload)
                if any(entry["row"]["id"] == card.id for entry in payload.get("cards", [])):
                    attempts.append(payload)
                    if len(attempts) == 1:
                        if burial.startswith("partial"):
                            emit({"stage": "partial_binding"})
                            assert json.loads(sys.stdin.readline())["continue"]
                        raise ConnectionError("Lost committed first binding receipt")
                return result

            if generated and config.get("lose_response") and cycle == 0:
                client.batch = lose_binding
                try:
                    changes.upload()
                except ConnectionError:
                    changes = SyncChanges(sidecar, client)
                    if config.get("after_receipt"):
                        answer_after_receipt()
                else:
                    raise AssertionError("Binding did not return its receipt")
            changes.upload()
            if generated and config.get("lose_response") and cycle == 0:
                assert len(attempts) == 2 and attempts[0] == attempts[1]
            client.batch = send
            for attempt in range(3):
                try:
                    changes.apply(collection)
                    break
                except SyncConflict as error:
                    assert config.get("after_receipt"), error.conflicts
                    assert len(error.conflicts) == 1, error.conflicts
                    problem = error.conflicts[0]
                    prepared = changes.prepare_card_resolution(collection, problem, changes.card_conflict(problem))
                    assert len(prepared["pending_reviews"]) == 1, prepared
                    changes.resolve_card(collection, prepared, {"deck": "anki", "lifecycle": "anki", "properties": "anki"})
                    if config.get("after_receipt") == "during_resolution" and attempt == 0:
                        answer_after_receipt()
                    changes = SyncChanges(sidecar, client)
                    changes.upload()
            else:
                raise AssertionError("The new native reviews did not reconcile")
            if config.get("after_receipt"):
                run_session(collection, root, config["origin"])
            else:
                drain(delivery, collection)
        emit({"stage": "reconciled", "card": next(row for row in source_rows(collection, "cards") if row["id"] == card.id),
              "siblings": [row for row in source_rows(collection, "cards") if row["id"] != card.id], "history": source_rows(collection, "revlog")})
        command = json.loads(sys.stdin.readline())
        assert command["continue"]
        if not command.get("undo", True):
            client.sign_out()
            return
        while review_id:
            client._profile_request(profile["id"], "/review_undos", {"review_id": review_id, "request_key": f"competing-undo-{review_id}"})
            delivery.receive()
            try:
                delivery.apply(collection)
            except DeliveryConflict as error:
                problem = next(item for item in error.conflicts if item["kind"] == "review_undos")
            else:
                raise AssertionError("Undo applied without confirmation")
            prepared = delivery.prepare_review_undo_resolution(collection, problem)
            assert "undo_error" not in prepared, prepared
            delivery.confirm_review_undo(collection, prepared)
            delivery.apply(collection)
            send, attempts = client.batch, []

            def lose_response(profile_id, payload):
                attempts.append(payload)
                result = send(profile_id, payload)
                if len(attempts) == 1:
                    raise ConnectionError("Lost committed competing Undo receipt")
                return result

            if config.get("lose_response"):
                client.batch = lose_response
                try:
                    delivery.acknowledge()
                except ConnectionError:
                    delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=captured["pair_request"]["key"])
                    delivery.apply(collection)
                else:
                    raise AssertionError("Undo did not return its result")
            delivery.acknowledge()
            if config.get("lose_response"):
                assert len(attempts) == 2 and attempts[0] == attempts[1]
                client.batch = send
            changes.capture(collection)
            changes.upload()
            changes.apply(collection)
            drain(delivery, collection)
            if command.get("last_undo", True):
                counters = collection.decks.get(deck)
                assert all(counters[key] == native_counters[key] for key in ("newToday", "revToday", "timeToday"))
                if config.get("burial"):
                    stable = lambda row: {key: value for key, value in row.items() if key not in ("mod", "usn")}
                    siblings = {row["id"]: row for row in source_rows(collection, "cards") if row["id"] != card.id}
                    assert {key: stable(value) for key, value in siblings.items()} == {key: stable(value) for key, value in native_siblings.items()}
            emit({"stage": "undone", "card": next(row for row in source_rows(collection, "cards") if row["id"] == card.id), "history": source_rows(collection, "revlog")})
            command = json.loads(sys.stdin.readline())
            assert command["continue"]
            review_id = command.get("review_id")
        client.sign_out()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
