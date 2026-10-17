from paths import SCRATCH
"""Measure real HTTP sync with fresh native collections, including media."""

from contextlib import closing, contextmanager
import cProfile
import hashlib
import io
import json
from pathlib import Path
import pstats
import sys
import tempfile
from threading import Event, Thread
from time import monotonic

import support
from anki.buildinfo import version
from anki.collection import AddNoteRequest, Collection
from shirabe_addon.sync_changes import SyncChanges
from shirabe_addon.sync_delivery import SyncDelivery
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_media import SyncMedia
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit
from sync_profile_roundtrip import fingerprints


class Measurements:
    def __init__(self, client):
        self.results = {}
        self.totals = {"requests": 0, "request_body_bytes": 0, "response_body_bytes": 0}
        open_request = client.opener.open

        def measured_open(request, **options):
            self.totals["requests"] += 1
            data = request.data
            self.totals["request_body_bytes"] += len(data) if isinstance(data, bytes) else int(request.get_header("Content-length", 0))
            response = open_request(request, **options)
            read = response.read

            def measured_read(*args):
                result = read(*args)
                self.totals["response_body_bytes"] += len(result)
                return result

            response.read = measured_read
            return response

        client.opener.open = measured_open

    @contextmanager
    def phase(self, name):
        stopped = Event()

        def heartbeat():
            while not stopped.wait(10):
                emit({"stage": "progress", "phase": name, "done": self.totals["requests"], "total": 0})

        worker = Thread(target=heartbeat, daemon=True)
        before, started = self.totals.copy(), monotonic()
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join()
            self.results[name] = {"seconds": monotonic() - started,
                                  **{key: value - before[key] for key, value in self.totals.items()}}


def run(config):
    root = Path(config["directory"]).resolve(strict=True)
    if not root.is_relative_to(SCRATCH) or (root / "collection.anki2").exists():
        raise ValueError("Use a fresh disposable directory under workspace tmp")
    size = config["cards"]
    if type(size) is not int or size < 1000 or size > 100_000 or size % 1000:
        raise ValueError("Use 1000 to 100000 cards, in multiples of 1000")
    path = root / "sync.sqlite3"
    client = SyncLogin(path, config["origin"])
    authorize(client)
    measurements = Measurements(client)
    with closing(Collection(str(root / "collection.anki2"))) as collection:
        collection.set_config("fsrs", True)
        expected_media = {}
        with measurements.phase("build"):
            deck = collection.decks.id("HTTP scale")
            model = collection.models.by_name("Basic")
            for index in range(size // 100):
                name = f"scale-{index}.svg"
                prefix = f'<svg xmlns="http://www.w3.org/2000/svg"><text>{index}</text></svg><!--'.encode()
                data = prefix + b"x" * (32_768 - 3 - len(prefix)) + b"-->"
                assert len(data) == 32_768
                assert collection.media.write_data(name, data) == name
                expected_media[name] = {"digest": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for offset in range(0, size, 1000):
                requests = []
                for index in range(offset, offset + 1000):
                    note = collection.new_note(model)
                    note.fields = [f'単語 {index} <img src="scale-{index // 100}.svg">', f"Meaning {index}"]
                    requests.append(AddNoteRequest(note=note, deck_id=deck))
                collection.add_notes(requests)
        snapshot = SyncSnapshot(path, client)
        with measurements.phase("initial_capture"):
            captured = snapshot.capture(collection, [deck])
        with measurements.phase("initial_upload_including_media"):
            profile = snapshot.upload()
        key = captured["pair_request"]["key"]
        changes = SyncChanges(path, client)
        delivery = SyncDelivery(path, client, profile_id=profile["id"], key=key)
        media = SyncMedia(path, client)

        def receive_all():
            while True:
                page = delivery.receive()
                delivery.apply(collection)
                delivery.acknowledge()
                if not page["changes"]:
                    return

        def sync(*, unchanged=False):
            cycle = changes.capture(collection)
            if unchanged:
                assert not cycle["keys"], "Unchanged source queued a batch"
            changes.upload()
            changes.apply(collection)
            receive_all()
            assert media.capture(collection) == 0
            media.exchange()
            assert media.apply(collection) == 0

        with measurements.phase("no_op"):
            sync(unchanged=True)
        no_op_profile = None
        if config.get("profile_no_op"):
            profiler = cProfile.Profile()
            with measurements.phase("profiled_no_op"):
                profiler.runcall(sync, unchanged=True)
            output = io.StringIO()
            pstats.Stats(profiler, stream=output).strip_dirs().sort_stats("cumulative").print_stats(50)
            no_op_profile = output.getvalue()
        before = fingerprints(collection)
        with measurements.phase("media_download"):
            downloaded, cursor = {}, 0
            while True:
                page = client.media_manifest(profile["id"], key=key, after=cursor)
                if not page["files"]:
                    break
                for file in page["files"]:
                    assert file["filename"] not in downloaded
                    with tempfile.TemporaryFile() as target:
                        client.download_media(profile["id"], key=key, id=file["id"], digest=file["digest"], bytes=file["bytes"], destination=target)
                        downloaded[file["filename"]] = {"digest": hashlib.file_digest(target, "sha256").hexdigest(), "bytes": target.seek(0, 2)}
                assert page["cursor"] > cursor
                cursor = page["cursor"]
            assert downloaded == expected_media
        card_id = collection.db.scalar("SELECT min(id) FROM cards")
        emit({"stage": "scale_uploaded", "cards": size, "media": expected_media, "fingerprints": before,
              "card_id": card_id, "no_op_profile": no_op_profile, "measurements": measurements.results})
        assert json.loads(sys.stdin.readline())["continue"]
        notes = [collection.get_note(note_id) for note_id in collection.db.list("SELECT id FROM notes ORDER BY id LIMIT 1000")]
        for note in notes:
            note.fields[-1] += " edited in Anki"
            note.tags.append("scale-edited")
        collection.update_notes(notes)
        with measurements.phase("1000_note_edit"):
            sync()
        emit({"stage": "scale_edited", "fingerprints": fingerprints(collection), "edited_ids": [note.id for note in notes]})
        command = json.loads(sys.stdin.readline())
        assert command["continue"]
        with measurements.phase("one_shirabe_review"):
            sync(unchanged=True)
        card = collection.get_card(card_id)
        assert card.reps == command["reps"] == 1
        delivered = collection.db.list("SELECT id FROM revlog WHERE cid = ?", card_id)
        assert len(delivered) == 1
        card.start_timer()
        collection.sched.answerCard(card, 3)
        native = collection.db.list("SELECT id FROM revlog WHERE cid = ? AND id != ?", card_id, delivered[0])
        assert len(native) == 1
        with measurements.phase("one_anki_review"):
            sync()
        after = fingerprints(collection)
        with measurements.phase("final_no_op"):
            sync(unchanged=True)
        assert fingerprints(collection) == after
        assert collection.note_count() == collection.card_count() == size
        assert collection.db.scalar("SELECT count(*) FROM revlog") == 2
        with SyncState(path) as state:
            assert not state.pending()
            assert not state.get("changes")
        client.sign_out()
        emit({"stage": "scale_complete", "cards": size, "anki": version, "fingerprints": after,
              "scope": "Loopback Rails test server; Basic notes; initially new cards and no history; HTTP body bytes exclude headers",
              "delivered_review_id": delivered[0], "native_review_id": native[0],
              "media_files": len(downloaded), "media_bytes": sum(file["bytes"] for file in downloaded.values()),
              "sidecar_bytes": path.stat().st_size, "measurements": measurements.results, "no_op_profile": no_op_profile})


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
