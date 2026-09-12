"""Exercise a requested review Undo against native Anki and the Rails HTTP API."""

import json
from pathlib import Path
import sys
from unittest.mock import patch

import support
from gui_safety import offscreen_test_application
from anki.collection import Collection
from aqt.qt import QDialogButtonBox, QWidget
from shirabe_addon.sync_cards import apply_card_change
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_conflict_ui import CardConflictDialog, HistoryRemovalDialog, ReviewUndoDialog
from shirabe_addon.sync_delivery import DeliveryConflict, SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_reviews import card_row, history_row
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit


def resolve_schedule_conflict(collection, delivery, client, sidecar, problem, schedule, parent, removed_row):
    changes = SyncChanges(sidecar, client)
    detail = changes.card_conflict(problem)
    prepared = changes.prepare_history_removal(collection, problem, detail)
    emit({"stage": "undo_schedule_conflict", "native_reps": collection.get_card(problem["id"]).reps})
    assert json.loads(sys.stdin.readline())["continue"]
    submitted = []

    def select(side):
        submitted.append(changes.resolve_history_removal(collection, prepared, schedule=side))

    chooser = HistoryRemovalDialog(parent, prepared, select)
    chooser.schedule.setCurrentIndex(chooser.schedule.findData(schedule))
    chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
    assert len(submitted) == 1
    chooser.deleteLater()
    send, attempts = client.batch, []

    def lose_response(profile_id, payload):
        attempts.append(payload)
        result = send(profile_id, payload)
        if len(attempts) == 1:
            raise ConnectionError("Lost accepted Undo schedule choice")
        return result

    client.batch = lose_response
    try:
        delivery.acknowledge()
    except ConnectionError:
        delivery = SyncDelivery(sidecar, client, profile_id=delivery.profile_id, key=delivery.key)
        delivery.apply(collection)
    else:
        raise AssertionError("Expected a lost schedule resolution response")
    try:
        delivery.acknowledge()
    except DeliveryConflict as error:
        assert schedule == "shirabe", error.conflicts
        problem = next(item for item in error.conflicts if item["kind"] == "review_undo_results")
        accepted = delivery.prepare_review_undo_result_resolution(collection, problem)
        assert accepted["schedule_confirmed"], accepted
        applied = []

        def apply(choices):
            delivery.resolve_review_undo_result(collection, accepted, choices)
            applied.append(choices)

        chooser = CardConflictDialog(parent, accepted, apply)
        chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        assert len(applied) == 1
        chooser.deleteLater()
        delivery.acknowledge()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    client.batch = send
    with SyncState(sidecar) as state:
        assert state.batch_applied(submitted[0])
    for _ in range(10):
        page = delivery.receive()
        delivery.apply(collection)
        delivery.acknowledge()
        if not page["changes"]:
            break
    else:
        raise AssertionError("The confirmed schedule did not drain its competing review")
    assert history_row(collection, removed_row["id"]) is None
    assert collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", problem["id"]) == 1
    with SyncState(sidecar) as state:
        assert not state.pending_review_undos()
    emit({"stage": "undo_schedule_reconciled", "native_reps": collection.get_card(problem["id"]).reps,
          "history_ids": collection.db.list("SELECT id FROM revlog WHERE cid = ?", problem["id"])})
    assert json.loads(sys.stdin.readline())["continue"]


def run(config):
    app = offscreen_test_application([])
    parent = QWidget()
    root = Path(config["directory"])
    path = root / "collection.anki2"
    assert not path.exists()
    collection = Collection(str(path))
    collection.set_config("fsrs", True)
    try:
        deck = collection.decks.id("Requested Undo")
        note = collection.new_note(collection.models.by_name("Basic"))
        note.fields = ["猫", "cat"]
        collection.add_note(note, deck)
        card_id = note.cards()[0].id
        sidecar = root / "sync.sqlite3"
        client = SyncLogin(sidecar, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(sidecar, client)
        captured = snapshot.capture(collection, [deck])
        profile = snapshot.upload()
        key = captured["pair_request"]["key"]
        delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=key)
        emit({"stage": "awaiting_requested_review", "card_id": card_id})
        review_id = json.loads(sys.stdin.readline())["review_id"]
        page = client.deliver(profile["id"], key=key, cursor=0, limit=200) if config.get("unacknowledged_review") else delivery.receive()
        original = next(item for item in page["changes"] if item["entity_kind"] == "review")
        if not config.get("unacknowledged_review"):
            delivery.apply(collection)
            delivery.acknowledge()
        requested = client._profile_request(profile["id"], "/review_undos", {"review_id": review_id, "request_key": "native-undo-click"})
        assert requested["status"] == "pending", requested
        if config.get("same_page"):
            emit({"stage": "undo_waiting_for_delivery", "request_id": requested["id"]})
            assert json.loads(sys.stdin.readline())["continue"]
        page = delivery.receive()
        undo = next(item for item in page["changes"] if item["entity_kind"] == "review" and item["operation"] == "delete")
        try:
            delivery.apply(collection)
        except DeliveryConflict as error:
            problem = next(item for item in error.conflicts if item["kind"] == "review_undos")
        else:
            raise AssertionError("Undo applied without confirmation")
        prepared = delivery.prepare_review_undo_resolution(collection, problem)
        assert "undo_error" not in prepared, prepared
        confirmed = []

        def submit(action):
            assert action == "undo"
            delivery.confirm_review_undo(collection, prepared)
            confirmed.append(action)

        chooser = ReviewUndoDialog(parent, prepared, submit)
        chooser.show()
        app.processEvents()
        chooser.action.setCurrentIndex(chooser.action.findData("undo"))
        chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        assert confirmed == ["undo"]
        chooser.deleteLater()
        if config.get("interrupted_review"):
            with patch.object(SyncState, "finish_delivery_change", side_effect=RuntimeError("lost Undo output receipt")):
                try:
                    delivery.apply(collection)
                except RuntimeError as error:
                    assert str(error) == "lost Undo output receipt", error
                else:
                    raise AssertionError("Undo completion was not interrupted")
            card = collection.get_card(card_id)
            card.start_timer()
            collection.sched.answerCard(card, 3)
            later_card = card_row(collection, card_id)
            later_history = list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (card_id,)))
            assert len(later_history) == 1
            delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=key)
        delivery.apply(collection)
        row = prepared["row"]
        assert history_row(collection, row["id"]) is None
        emit({"stage": "native_undo_waiting_for_receipt", "request_id": requested["id"], "native_reps": collection.get_card(card_id).reps})
        assert json.loads(sys.stdin.readline())["continue"]
        send = client.batch
        attempts = []

        def lose_response(profile_id, payload):
            result = send(profile_id, payload)
            if "cards" not in payload:
                assert config.get("unacknowledged_review") and len(result["history"]) == 1, result
                assert result["history"][0]["status"] == "accepted", result
                return result
            attempts.append(payload)
            if result["cards"][0]["status"] == "accepted":
                assert result["cards"][0]["review_undo_cursor"] == undo["cursor"], result
            else:
                assert config.get("schedule") and result["cards"][0]["code"] == "history_changed_before_undo", result
            if len(attempts) == 1:
                raise ConnectionError("Lost Undo receipt")
            return result

        client.batch = lose_response
        try:
            delivery.acknowledge()
        except ConnectionError:
            delivery = SyncDelivery(sidecar, client, profile_id=profile["id"], key=key)
            delivery.apply(collection)
        else:
            raise AssertionError("Expected the lost Undo receipt")
        try:
            delivery.acknowledge()
        except DeliveryConflict as error:
            if config.get("schedule"):
                problem = next(item for item in error.conflicts if item["kind"] == "cards")
                assert len(attempts) == 2 and attempts[0] == attempts[1]
                client.batch = send
                resolve_schedule_conflict(collection, delivery, client, sidecar, problem, config["schedule"], parent, row)
                client.sign_out()
                return
            result_problem = next(item for item in error.conflicts if item["kind"] == "review_undo_results")
        else:
            if config.get("interrupted_review"):
                assert card_row(collection, card_id) == later_card
                assert len(attempts) == 2 and attempts[0] == attempts[1]
                assert attempts[0]["cards"][0]["row"]["reps"] == 0
                assert "last_review_id" not in attempts[0]["cards"][0]
                assert list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (card_id,))) == later_history
                emit({"stage": "interrupted_undo_acknowledged", "native_reps": later_card["reps"], "history": later_history})
                assert json.loads(sys.stdin.readline())["continue"]
                client.batch = send
                changes = SyncChanges(sidecar, client)
                changes.capture(collection)
                changes.upload()
                changes.apply(collection)
                for _ in range(10):
                    page = delivery.receive()
                    delivery.apply(collection)
                    delivery.acknowledge()
                    if not page["changes"]:
                        break
                else:
                    raise AssertionError("The interrupted Undo and later review did not drain")
                assert card_row(collection, card_id) == later_card
                assert list(SyncSnapshot._rows(collection, "revlog", "cid = ?", (card_id,))) == later_history
                emit({"stage": "interrupted_undo_history_published", "native_card": later_card, "history": later_history})
                assert json.loads(sys.stdin.readline())["continue"]
                client.sign_out()
                return
            raise AssertionError("The merged Undo result was acknowledged without reconciliation")
        accepted_schedule = config.get("accepted_schedule")
        if accepted_schedule:
            card = collection.get_card(card_id)
            card.due += 10
            collection.update_card(card)
        resolution = delivery.prepare_review_undo_result_resolution(collection, result_problem)
        assert resolution["schedule_matches"] == (not accepted_schedule)
        assert resolution["local"]["flags"] == 0
        assert resolution["detail"]["anki"]["card_after"]["flags"] == 3
        emit({"stage": "accepted_undo_waiting_for_choice", "native_flags": collection.get_card(card_id).flags,
              "native_due": resolution["local"]["due"], "accepted_due": resolution["result"]["anki"]["card_after"]["due"]})
        assert json.loads(sys.stdin.readline())["continue"]
        resolved = []

        def resolve(choices):
            delivery.resolve_review_undo_result(collection, resolution, choices)
            resolved.append(choices)

        chooser = CardConflictDialog(parent, resolution, resolve)
        chooser.show()
        app.processEvents()
        chooser.choices["properties"].setCurrentIndex(chooser.choices["properties"].findData("shirabe"))
        if accepted_schedule:
            assert not chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            chooser.choices["schedule"].setCurrentIndex(chooser.choices["schedule"].findData(accepted_schedule))
        chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        assert len(resolved) == 1
        assert collection.get_card(card_id).flags == 3
        assert history_row(collection, row["id"]) is None
        chooser.deleteLater()
        delivery.acknowledge()
        client.batch = send
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        status = client._profile_request(profile["id"], f"/review_undos/{requested['id']}", None, method="GET")
        assert status["status"] == "reconciled", status
        with SyncState(sidecar) as state:
            removal_id = int(state.checkpoint_digest("history_removal", row["id"]))
        emit({"stage": "requested_undo_reconciled", "review_id": row["id"], "removal_id": removal_id})
        assert json.loads(sys.stdin.readline())["continue"]
        if accepted_schedule:
            chosen = resolution["local"] if accepted_schedule == "anki" else resolution["result"]["anki"]["card_after"]
            assert collection.get_card(card_id).due == chosen["due"]
            changes = SyncChanges(sidecar, client)
            changes.capture(collection)
            changes.upload()
            changes.apply(collection)
            emit({"stage": "undo_schedule_published", "native_due": collection.get_card(card_id).due})
            assert json.loads(sys.stdin.readline())["continue"]
        source = original["payload"]["anki"]
        local = card_row(collection, card_id)
        apply_card_change(collection, {"anki": {**source, "card_before": local, "card_after": {**source["card_after"], "flags": local["flags"]}}},
                          restored_history=[{"row": row, "removal_id": removal_id}])
        changes = SyncChanges(sidecar, client)
        changes.capture(collection)
        changes.upload()
        changes.apply(collection)
        for _ in range(10):
            page = delivery.receive()
            delivery.apply(collection)
            delivery.acknowledge()
            if not page["changes"]:
                break
        else:
            raise AssertionError("The review restoration did not drain")
        assert history_row(collection, row["id"]) == row
        emit({"stage": "requested_review_restored", "native_reps": collection.get_card(card_id).reps})
        assert json.loads(sys.stdin.readline())["continue"]
        client.sign_out()
    finally:
        collection.close()
        parent.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
