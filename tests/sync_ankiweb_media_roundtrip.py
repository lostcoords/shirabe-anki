"""Carry isolated media bytes through Rails and native AnkiWeb sync in both directions."""

import base64
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import uuid
import wave

import support
from anki.collection import Collection
from anki.buildinfo import version
from ankiweb_live_probe import LiveAccountProbe
from ankiweb_media_probe import PreservedMedia
from native_sync_server import native_sync_server, full_sync
from sync_http_roundtrip import authorize, emit
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_media import SyncMedia
from shirabe_addon.sync_snapshot import SyncSnapshot


def progress(done, total):
    if done % 10000 == 0 or done == total:
        emit({"stage": "progress", "phase": "preserving Anki media", "done": done, "total": total})


def media_bytes(prefix):
    fixtures = Path(__file__).resolve().parent / "fixtures/files"
    sound = io.BytesIO()
    with wave.open(sound, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 800)
    return {
        prefix + ".png": (fixtures / "avatar.png").read_bytes(),
        prefix + ".wav": sound.getvalue(),
        prefix + ".webm": base64.b64decode((fixtures / "study_video.webm.base64").read_text()),
        prefix + ".ttf": base64.b64decode((fixtures / "study_font.ttf.base64").read_text()),
        prefix + ".css": f'@import url("{prefix}-inner.css"); @font-face {{ font-family: ProbeFont; src: url("{prefix}.ttf"); }}'.encode(),
        prefix + "-inner.css": b".probe { color: rgb(12, 34, 56); }",
    }


def run(config):
    assert version == "26.08.1"
    root = Path(config["artifact_directory"])
    path = root / "sidecar.sqlite3"
    client = SyncLogin(path, config["origin"])
    authorize(client)
    with ExitStack() as stack:
        source_collection = config.get("ankiweb_source")
        if config.get("rehearsal"):
            endpoint = stack.enter_context(native_sync_server(root))
            source = root / "rehearsal-source"
            source.mkdir()
            original = Collection(str(source / "collection.anki2"))
            original.set_config("fsrs", True)
            note = original.new_note(original.models.by_name("Basic"))
            note.fields = ["Existing note", "Must remain unchanged"]
            original.add_note(note, original.decks.id("Existing deck"))
            original.media.write_data("existing.bin", b"Existing media must remain unchanged")
            auth = original.sync_login("probe", "disposable", endpoint)
            original.sync_collection(auth, False)
            full_sync(original, auth, upload=True)
            original.sync_media(auth)
            deadline = time.monotonic() + 20
            while original.media_sync_status().active:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            original.close()
            probe = LiveAccountProbe(root / "copies", source, auth=auth)
        else:
            source = Path(config["ankiweb_profile"])
            probe = LiveAccountProbe(root, source)
        stack.callback(probe.close)
        preserved = PreservedMedia(source, progress)
        desktop = probe.clone("desktop", source=source_collection, media=preserved)
        receiver = probe.clone("receiver", source=source_collection, media=preserved)
        prefix = "_shirabe_sync_" + uuid.uuid4().hex[:12]
        files = media_bytes(prefix)
        assert not files.keys() & preserved.files.keys()
        for name, data in files.items():
            assert desktop.media.write_data(name, data) == name
        deck_name = "Shirabe Test Media " + prefix.removeprefix("_shirabe_sync_")
        deck_id = desktop.decks.id(deck_name)
        model = desktop.models.new(deck_name)
        for name in ("Front", "Back"):
            desktop.models.add_field(model, desktop.models.new_field(name))
        template = desktop.models.new_template("Media")
        template.update(qfmt="{{Front}}", afmt="{{FrontSide}}<hr>{{Back}}")
        desktop.models.add_template(model, template)
        desktop.models.add(model)
        note = desktop.new_note(model)
        note.fields = [f'<link rel="stylesheet" href="{prefix}.css"><div class="probe">Media test</div>'
                       f'<img src="{prefix}.png">[sound:{prefix}.wav]<video src="{prefix}.webm" controls></video>', "Preserved bytes"]
        desktop.add_note(note, deck_id)
        (root / "fixture.json").write_text(json.dumps({"deck": deck_name, "deck_id": deck_id,
            "note_id": note.id, "prefix": prefix, "files": list(files), "live": not config.get("rehearsal")}, indent=2) + "\n")
        probe.sync(desktop)
        probe.sync(receiver)
        preserved.sync(desktop, probe.auth)
        preserved.sync(receiver, probe.auth)
        for name, data in files.items():
            assert (Path(receiver.media.dir()) / name).read_bytes() == data
        snapshot = SyncSnapshot(path, client)
        snapshot.capture(desktop, [deck_id])
        profile = snapshot.upload(progress=lambda done, total: emit({"stage": "progress", "phase": "media snapshot", "done": done, "total": total}))
        expected = {name: {"digest": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in files.items()}
        emit({"stage": "ankiweb_media_uploaded", "profile_id": profile["id"], "expected": expected,
              "replacement": prefix + "-inner.css"})
        assert json.loads(sys.stdin.readline())["continue"]
        media = SyncMedia(path, client)
        media.capture(desktop)
        media.exchange()
        media.apply(desktop)
        replacement = prefix + "-inner.css"
        assert (Path(desktop.media.dir()) / replacement).read_bytes() == b".probe { color: blue; }"
        preserved.sync(desktop, probe.auth)
        preserved.sync(receiver, probe.auth)
        assert (Path(receiver.media.dir()) / replacement).read_bytes() == b".probe { color: blue; }"
        receiver.media.trash_files([replacement])
        assert receiver.media.write_data(replacement, b".probe { color: green; font-weight: bold; }") == replacement
        preserved.sync(receiver, probe.auth)
        preserved.sync(desktop, probe.auth)
        media.capture(desktop)
        media.exchange()
        media.apply(desktop)
        assert (Path(desktop.media.dir()) / replacement).read_bytes() == b".probe { color: green; font-weight: bold; }"
        unchanged = preserved.unchanged()
        result = probe.report(deck=deck_name, deck_id=deck_id, note_id=note.id, media_tested=True,
            preserved_media_files=unchanged, media_native_syncs=6, replacement=replacement,
            expected=expected, replacement_digest=hashlib.sha256(b".probe { color: green; font-weight: bold; }").hexdigest())
        emit({"stage": "ankiweb_media_verified", **result})


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
