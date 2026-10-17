from paths import SCRATCH
"""Native media transfer against Rails, using only a disposable collection."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import support
from anki.collection import Collection
from anki.import_export_pb2 import ImportAnkiPackageRequest
from shirabe_addon.sync_login import SyncLogin
from shirabe_addon.sync_media import SyncMedia
from shirabe_addon.sync_snapshot import SyncSnapshot
from shirabe_addon.sync_state import SyncState
from sync_http_roundtrip import authorize, emit


def run(config):
    root = Path(config["directory"])
    with closing(Collection(str(root / "collection.anki2"))) as collection:
        collection.set_config("fsrs", True)
        package = None
        if config.get("package"):
            package = Path(config["package"]).resolve(strict=True)
            if not package.is_relative_to(SCRATCH) or package.suffix != ".apkg":
                raise ValueError("Use an authorized trial package under workspace tmp")
            with package.open("rb") as source:
                package_digest = hashlib.file_digest(source, "sha256").hexdigest()
            collection.import_anki_package(ImportAnkiPackageRequest(package_path=str(package)))
            decks = [deck.id for deck in collection.decks.all_names_and_ids()]
        else:
            decks = [collection.decks.id("Media roundtrip")]
            collection.media.write_data("猫.mp3", b"\x00\xffnative audio")
            collection.media.write_data("_trial.css", b'@font-face { src: url(font.woff2) }')
            collection.media.write_data("font.woff2", b"\x00font bytes")
            note = collection.new_note(collection.models.by_name("Basic"))
            note.fields = ["猫 [sound:猫.mp3]", "cat"]
            collection.add_note(note, decks[0])
        expected = {}
        for file in Path(collection.media.dir()).iterdir():
            if file.is_file() and not file.name.startswith("."):
                with file.open("rb") as source:
                    expected[file.name] = {"digest": hashlib.file_digest(source, "sha256").hexdigest(), "bytes": file.stat().st_size}
        path = root / "sync.sqlite3"
        client = SyncLogin(path, config["origin"])
        authorize(client)
        snapshot = SyncSnapshot(path, client)
        captured = snapshot.capture(collection, decks)
        send = client.upload_media
        accepted = []

        def lose_receipt(*args, **kwargs):
            accepted.append(send(*args, **kwargs))
            raise ConnectionError("Lost committed media upload response")

        client.upload_media = lose_receipt
        try:
            snapshot.upload()
        except ConnectionError:
            assert len(accepted) == 1
        else:
            raise AssertionError("Expected lost media response")
        client.upload_media = send
        profile = SyncSnapshot(path, client).upload()
        with SyncState(path) as state:
            receipt = state.db.execute("SELECT receipt FROM media_uploads ORDER BY rowid LIMIT 1").fetchone()[0]
            assert json.loads(receipt) == accepted[0]
        key = captured["pair_request"]["key"]
        manifest = client.media_manifest(profile["id"], key=key)
        received = {file["filename"]: {field: file[field] for field in ("digest", "bytes")} for file in manifest["files"]}
        assert received == expected, {"missing": sorted(expected.keys() - received.keys()), "extra": sorted(received.keys() - expected.keys())}
        emit({"stage": "media_uploaded", "manifest": manifest, "expected": expected})
        command = json.loads(sys.stdin.readline())
        assert command["continue"]
        if command.get("media_cycle"):
            collection.media.write_data("_native-added.bin", b"later native asset")
            media_sync = SyncMedia(path, client)
            assert media_sync.capture(collection) == 1
            media_sync.exchange()
            assert media_sync.apply(collection) == 1
            assert (Path(collection.media.dir()) / "_SHIRABE_added.bin").read_bytes() == b"later Shirabe asset"
            assert media_sync.capture(collection) == 0
            media_sync.exchange()
            assert media_sync.apply(collection) == 0
            for filename, properties in expected.items():
                with (Path(collection.media.dir()) / filename).open("rb") as original:
                    assert hashlib.file_digest(original, "sha256").hexdigest() == properties["digest"]
        manifest = client.media_manifest(profile["id"], key=key)
        downloaded, aliases = {}, []
        with closing(Collection(str(root / "receiving.anki2"))) as receiving:
            for added in manifest["files"]:
                with tempfile.TemporaryFile() as temporary:
                    client.download_media(profile["id"], key=key, id=added["id"], digest=added["digest"], bytes=added["bytes"], destination=temporary)
                    name = receiving.media.write_data(added["filename"], temporary.read())
                requested = Path(receiving.media.dir()) / added["filename"]
                assert requested.is_file(), {"requested": added["filename"], "stored": name}
                data = requested.read_bytes()
                assert hashlib.sha256(data).hexdigest() == added["digest"]
                downloaded[added["filename"]] = {"digest": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                if name != added["filename"]:
                    aliases.append({"requested": added["filename"], "stored": name})
        if package:
            with package.open("rb") as source:
                assert hashlib.file_digest(source, "sha256").hexdigest() == package_digest
        emit({"stage": "media_downloaded", "files": downloaded, "aliases": aliases, "source_unchanged": True})
        client.sign_out()


if __name__ == "__main__":
    run(json.loads(sys.stdin.readline()))
