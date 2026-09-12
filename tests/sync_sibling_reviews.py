from contextlib import closing
import json
from pathlib import Path
import sys
from unittest.mock import patch

import support
from anki.collection import Collection
from shirabe_addon.sync_login import SyncLogin, private_sync_path
from shirabe_addon.sync_snapshot import SyncSnapshot
from sync_competing_review_undo import run_session
from sync_http_roundtrip import authorize, emit, source_rows


def run(config):
    root = Path(config["directory"])
    target = root / "sibling-reviews.anki2"
    assert not target.exists()
    with closing(Collection(str(target))) as collection:
        collection.set_config("fsrs", True)
        deck = collection.decks.id("Sibling reviews")
        preset = collection.decks.config_dict_for_deck_id(deck)
        preset["new"]["bury"] = True
        collection.decks.update_config(preset)
        note = collection.new_note(collection.models.by_name("Cloze"))
        note.fields = ["{{c1::猫}}" if config["generated"] else "{{c1::猫}} {{c2::犬}} {{c3::鳥}}", "Japanese"]
        collection.add_note(note, deck)
        client = SyncLogin(private_sync_path(root), config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(private_sync_path(root), client)
        snapshot.capture(collection, [deck])
        snapshot.upload()
        if config["generated"]:
            note.fields[0] = "{{c1::猫}} {{c2::犬}} {{c3::鳥}}"
            collection.update_note(note)
        card = next(item for item in note.cards() if item.ord == 1)
        card.start_timer()
        collection.sched.answerCard(card, 4)
        native = next(row for row in source_rows(collection, "cards") if row["id"] == card.id)
        history = source_rows(collection, "revlog")
        emit({"stage": "native_sibling_answer", "card": native, "history": history, "fields": note.fields})
        assert json.loads(sys.stdin.readline())["continue"]
        batches, send, attempts = SyncSnapshot._batches, SyncLogin.batch, []

        def split_cards(scanner, kind, rows, key):
            if kind == "cards":
                for row in sorted(rows, key=lambda entry: entry["row"]["ord"]):
                    yield from batches(scanner, kind, [row], key)
            else:
                yield from batches(scanner, kind, rows, key)

        def lose_response(connection, profile_id, payload):
            result = send(connection, profile_id, payload)
            if any(entry["row"]["id"] == card.id for entry in payload.get("cards", [])):
                attempts.append(payload)
                if len(attempts) == 1:
                    raise ConnectionError("Lost committed sibling binding receipt")
            return result

        with patch.object(SyncSnapshot, "_batches", split_cards if config["partial"] else batches), \
                patch.object(SyncLogin, "batch", lose_response):
            try:
                run_session(collection, root, config["origin"])
            except ConnectionError:
                pass
            else:
                raise AssertionError("The sibling receipt was not interrupted")
            run_session(collection, root, config["origin"])
        assert len(attempts) >= 2 and attempts[0] == attempts[1]
        settled = {table: source_rows(collection, table) for table in ("notes", "cards", "revlog")}
        run_session(collection, root, config["origin"])
        assert settled == {table: source_rows(collection, table) for table in settled}
        after = next(row for row in settled["cards"] if row["id"] == card.id)
        stable = lambda row: {key: value for key, value in row.items() if key not in ("mod", "usn", "queue")}
        assert stable(native) == stable(after), (native, after)
        assert after["queue"] == -2, after
        assert all(row in settled["revlog"] for row in history)
        emit({"stage": "siblings_reconciled", **settled})
        assert json.loads(sys.stdin.readline())["continue"]
        client.sign_out()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
