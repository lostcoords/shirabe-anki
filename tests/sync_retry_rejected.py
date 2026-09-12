"""Recover rejected HTTP records without replacing their saved receipts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import support
from gui_safety import offscreen_test_application
from anki.collection import Collection
from anki.buildinfo import version
from shirabe_addon.sync_changes import SyncChanges, SyncConflict
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit


def run(config):
    app = offscreen_test_application([])
    from shirabe_addon.sync_conflict_ui import NoteConflictDialog, RejectedRecordDialog
    assert version == "26.08.1"
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck_id = collection.decks.id("Rejected retry")
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck_id)
        independent = collection.new_note(collection.models.by_name("Basic (type in the answer)"))
        independent.fields = ["犬", "dog"]
        collection.add_note(independent, deck_id)
        client = SyncLogin(root / "sync.sqlite3", config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(root / "sync.sqlite3", client)
        captured = snapshot.capture(collection, [deck_id])
        profile = snapshot.upload()
        changes = SyncChanges(root / "sync.sqlite3", client)
        model = collection.get_note(note.id).note_type()
        template = collection.models.new_template("Reverse")
        template.update(qfmt="{{Back}}", afmt="{{Front}}")
        collection.models.add_template(model, template)
        collection.models.save(model)
        independent.fields[1] = "hound"
        collection.update_note(independent)
        changes.capture(collection)
        send = client.batch

        def old_transport(profile_id, payload):
            payload = copy.deepcopy(payload)
            for row in payload.get("note_types", []):
                row["template_map"] = [value for value in row["template_map"] if value is not None]
            return send(profile_id, payload)

        client.batch = old_transport
        changes.upload()
        client.batch = send
        try:
            changes.apply(collection)
        except SyncConflict as error:
            problems = error.conflicts
        else:
            raise AssertionError("Expected the old transport to reject the added template and its card")
        assert {problem["kind"] for problem in problems} == {"note_types", "cards"}, problems
        with SyncState(root / "sync.sqlite3") as state:
            originals = {key: state.batch(key) for key in state.get("changes")["keys"]}
        emit({"stage": "retry_rejected", "problems": problems, "note_id": note.id, "independent_id": independent.id})
        assert json.loads(sys.stdin.readline())["continue"]
        sent = []
        lost = False

        def track(profile_id, payload):
            nonlocal lost
            sent.append(payload)
            receipt = send(profile_id, payload)
            if not lost:
                lost = True
                raise ConnectionError("Discard the first accepted retry response")
            return receipt

        def confirm_retry(problem, source="saved"):
            prepared = changes.prepare_rejected_retry(collection, problem)
            submitted = []
            dialog = RejectedRecordDialog(None, prepared, lambda chosen: submitted.append(changes.retry_rejected(collection, prepared, source=chosen)))
            dialog.open()
            app.processEvents()
            assert not submitted
            index = dialog.source.findData(source)
            assert index >= 0, prepared["current_error"]
            dialog.source.setCurrentIndex(index)
            dialog.accept()
            assert len(submitted) == 1
            return submitted[0]

        client.batch = track
        for kind in ("note_types", "cards"):
            problem = next(problem for problem in problems if problem["kind"] == kind)
            if kind == "note_types":
                corrected = collection.get_note(note.id).note_type()
                corrected["css"] = ".card { color: blue; }"
                collection.models.save(corrected)
            confirm_retry(problem, "current" if kind == "note_types" else "saved")
            changes = SyncChanges(root / "sync.sqlite3", client)
            try:
                changes.upload()
            except ConnectionError:
                assert kind == "note_types"
                changes = SyncChanges(root / "sync.sqlite3", client)
                changes.upload()
            try:
                changes.apply(collection)
            except SyncConflict as error:
                assert kind == "note_types" and {item["kind"] for item in error.conflicts} == {"cards"}
                problems = error.conflicts
        assert len(sent) == 3 and sent[0] == sent[1] and all("notes" not in payload for payload in sent)
        with SyncState(root / "sync.sqlite3") as state:
            assert state.get("changes") is None
            for key, original in originals.items():
                assert state.batch(key)["payload"] == original["payload"]
                assert state.batch(key)["receipt"] == original["receipt"]
                assert state.batch_applied(key)
        delivery = SyncDelivery(root / "sync.sqlite3", client, profile_id=profile["id"],
                                key=captured["pair_request"]["key"])
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        assert collection.get_note(independent.id).fields == ["犬", "hound"]
        assert collection.card_count() == 3
        saved_replacements = list(dict.fromkeys(payload["request_key"] for payload in sent))
        client.batch = send

        def reject_note(candidate):
            changed = collection.get_note(note.id)
            changed.fields[0] = candidate
            collection.update_note(changed)
            changes.capture(collection)

            def missing_field(profile_id, payload):
                payload = copy.deepcopy(payload)
                for row in payload.get("notes", []):
                    row["flds"] = row["flds"].split("\x1f")[0]
                return send(profile_id, payload)

            client.batch = missing_field
            try:
                changes.upload()
            finally:
                client.batch = send
            try:
                changes.apply(collection)
            except SyncConflict as error:
                assert len(error.conflicts) == 1 and error.conflicts[0]["kind"] == "notes", error.conflicts
                assert error.conflicts[0]["code"] == "invalid_fields", error.conflicts
                return error.conflicts[0]
            raise AssertionError("Expected the malformed transport row to be rejected")

        for competing in (False, True):
            problem = reject_note("rejected candidate")
            with SyncState(root / "sync.sqlite3") as state:
                original = state.batch(problem["request_key"])
            emit({"stage": "current_retry_ready", "competing": competing, "request_key": problem["request_key"]})
            assert json.loads(sys.stdin.readline())["continue"]
            changed = collection.get_note(note.id)
            changed.fields[0] = "Anki competing edit" if competing else "corrected in Anki"
            collection.update_note(changed)
            replacement = confirm_retry(problem, "current")
            with SyncState(root / "sync.sqlite3") as state:
                assert state.batch(replacement)["payload"]["notes"][0]["base"] == original["payload"]["notes"][0]["base"]
            changes.upload()
            try:
                changes.apply(collection)
            except SyncConflict as error:
                assert competing and len(error.conflicts) == 1
                conflict = error.conflicts[0]
                assert conflict["code"] == "field_conflict"
                with SyncState(root / "sync.sqlite3") as state:
                    assert not state.entity_applied(problem["request_key"], "notes", note.id)
                detail = changes.note_conflict(conflict)
                prepared = changes.prepare_note_resolution(collection, conflict, detail)
                dialog = NoteConflictDialog(None, prepared, lambda fields, tags: changes.resolve_note(collection, prepared, fields, tags))
                dialog.fields[0].setPlainText("chosen correction")
                dialog.accept()
                changes.upload()
                changes.apply(collection)
            else:
                assert not competing, "The competing edit must require a choice"
            with SyncState(root / "sync.sqlite3") as state:
                assert state.batch(problem["request_key"])["receipt"] == original["receipt"]
                assert state.entity_applied(problem["request_key"], "notes", note.id)
            delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            assert collection.get_note(note.id).fields == ["chosen correction" if competing else "corrected in Anki", "server meaning"]
            emit({"stage": "current_retry_settled", "competing": competing})
            assert json.loads(sys.stdin.readline())["continue"]
        card = collection.get_note(note.id).cards()[0]
        card.flags = 2
        collection.update_card(card)
        changes.capture(collection)

        def malformed_card(profile_id, payload):
            payload = copy.deepcopy(payload)
            for entry in payload.get("cards", []):
                entry["captured_at"] = "invalid"
            return send(profile_id, payload)

        client.batch = malformed_card
        changes.upload()
        client.batch = send
        try:
            changes.apply(collection)
        except SyncConflict as error:
            assert len(error.conflicts) == 1 and error.conflicts[0]["code"] == "invalid_card", error.conflicts
            problem = error.conflicts[0]
        else:
            raise AssertionError("Expected the malformed card to be rejected")
        emit({"stage": "card_retry_ready", "card_id": card.id, "request_key": problem["request_key"]})
        assert json.loads(sys.stdin.readline())["continue"]
        card.start_timer()
        collection.sched.answerCard(card, 3)
        history_before = collection.db.all("SELECT * FROM revlog WHERE cid = ?", card.id)
        assert len(history_before) == 1
        confirm_retry(problem, "current")
        attempts = {}

        def lose_responses(profile_id, payload):
            key = payload["request_key"]
            attempts.setdefault(key, []).append(copy.deepcopy(payload))
            receipt = send(profile_id, payload)
            if len(attempts[key]) == 1:
                raise ConnectionError("Discard the accepted dependency or card response")
            return receipt

        client.batch = lose_responses
        for attempt in range(3):
            changes = SyncChanges(root / "sync.sqlite3", client)
            try:
                changes.upload()
            except ConnectionError:
                assert attempt < 2
            else:
                break
        assert len(attempts) == 2 and all(len(sent) == 2 and sent[0] == sent[1] for sent in attempts.values())
        changes.apply(collection)
        client.batch = send
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        assert collection.db.all("SELECT * FROM revlog WHERE cid = ?", card.id) == history_before
        assert collection.get_card(card.id).reps == 1
        changes.capture(collection)
        with SyncState(root / "sync.sqlite3") as state:
            assert state.get("changes")["keys"] == []
            assert state.entity_applied(problem["request_key"], "cards", card.id)
        changes.upload()
        changes.apply(collection)
        emit({"stage": "card_retry_settled", "card_id": card.id, "review_id": history_before[0][0]})
        assert json.loads(sys.stdin.readline())["continue"]
        card = collection.get_card(card.id)
        card.start_timer()
        collection.sched.answerCard(card, 3)
        review_id = collection.db.scalar("SELECT max(id) FROM revlog WHERE cid = ?", card.id)
        changes.capture(collection)

        def malformed_history(profile_id, payload):
            payload = copy.deepcopy(payload)
            for entry in payload.get("history", []):
                entry["row"]["time"] = "invalid"
            return send(profile_id, payload)

        client.batch = malformed_history
        changes.upload()
        client.batch = send
        try:
            changes.apply(collection)
        except SyncConflict as error:
            assert {item["kind"] for item in error.conflicts} == {"history", "cards"}, error.conflicts
            problems = error.conflicts
        else:
            raise AssertionError("Expected the invalid history and its dependent card to be rejected")
        history_problem = next(item for item in problems if item["kind"] == "history")
        assert history_problem["id"] == review_id and history_problem["code"] == "invalid_history"
        with SyncState(root / "sync.sqlite3") as state:
            history_original = state.batch(history_problem["request_key"])
        emit({"stage": "history_retry_ready", "card_id": card.id, "review_id": review_id,
              "request_key": history_problem["request_key"]})
        assert json.loads(sys.stdin.readline())["continue"]
        collection.db.execute("UPDATE revlog SET time = 1234 WHERE id = ?", review_id)
        confirm_retry(history_problem, "current")
        attempts = {}
        client.batch = lose_responses
        try:
            changes.upload()
        except ConnectionError:
            changes = SyncChanges(root / "sync.sqlite3", client)
            changes.upload()
        else:
            raise AssertionError("Expected a lost history correction response")
        assert len(attempts) == 1 and all(len(sent) == 2 and sent[0] == sent[1] for sent in attempts.values())
        client.batch = send
        try:
            changes.apply(collection)
        except SyncConflict as error:
            assert len(error.conflicts) == 1 and error.conflicts[0]["kind"] == "cards", error.conflicts
            confirm_retry(error.conflicts[0], "current")
        else:
            raise AssertionError("Expected the dependent card to remain pending until retried")
        changes.upload()
        changes.apply(collection)
        delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        with SyncState(root / "sync.sqlite3") as state:
            assert state.batch(history_problem["request_key"])["receipt"] == history_original["receipt"]
            assert state.entity_applied(history_problem["request_key"], "history", review_id)
        assert collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id) == 2
        assert collection.db.scalar("SELECT time FROM revlog WHERE id = ?", review_id) == 1234
        assert collection.get_card(card.id).reps == 2
        changes.capture(collection)
        with SyncState(root / "sync.sqlite3") as state:
            assert state.get("changes")["keys"] == []
        changes.upload()
        changes.apply(collection)
        emit({"stage": "history_retry_settled"})
        assert json.loads(sys.stdin.readline())["continue"]
        client.sign_out()
        emit({"stage": "retry_recovered", "keys": list(originals), "replacements": saved_replacements})
    finally:
        collection.close()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
