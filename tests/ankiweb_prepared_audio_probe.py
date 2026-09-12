#!/usr/bin/env python3
"""Add pronunciation and optional public examples to the isolated AnkiWeb fixture."""

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from fixture_deck import output_directory, require_anki, save_json


def prepare_cat(snapshot, recording, downloaded, example_fixture=None):
    import support
    from shirabe_addon import offline, offline_audio
    from shirabe_addon.authoring import identity_text

    segments = [segment for segment in snapshot["segments"] if segment["text"].count("猫") == 1]
    if len(segments) != 1:
        raise ValueError("Expected one cat fixture segment.")
    segment = segments[0]
    cats = [candidate for result in segment["results"] for candidate in result["candidates"]
            if candidate["dictionary"] == "jmdict" and candidate["headword"] == "猫"
            and identity_text(candidate.get("reading", ""), reading=True) == recording.reading]
    if len(cats) != 1:
        raise ValueError("Expected one synthetic cat definition.")
    candidate = copy.deepcopy(cats[0])
    token = {"surface": "猫", "lemma": "猫", "reading": candidate["reading"], "kind": "word", "length": 1,
             "position": len(segment["text"].split("猫")[0].encode("utf-16-le")) // 2,
             "entry": {"dictionary": "jmdict", "sourceId": candidate["sourceId"]}}
    sources = [row for row in snapshot.get("sources", []) if row["slug"] != "tatoeba"]
    if example_fixture is not None:
        sources.append(example_fixture["corpus"])
    backend = SimpleNamespace(recordings=lambda _: [recording])
    audio = offline_audio.AudioBatch(backend, offline.MAX_TOTAL_BYTES, download=lambda *args, **options: downloaded)
    item = offline.prepare_snapshot([segment["text"]], parse=lambda _: {"tokens": [token]},
                                   identify=lambda _: {"words": [{"candidates": [candidate]}]},
                                   content_version=snapshot["contentVersion"], allowed_dictionaries={"jmdict"},
                                   preferences=snapshot.get("preferences"), sources=sources, audio=audio,
                                   examples=(lambda _: example_fixture["response"]) if example_fixture is not None else None)
    if not item.complete or not item.media:
        raise ValueError(f"Cat preparation is incomplete: {item.issues}")
    if example_fixture is not None and not any(c.get("examples") for r in item.snapshot["segments"][0]["results"] for c in r["candidates"]):
        raise ValueError("The public-example fixture did not save any examples.")
    return item


def payload_sizes(preparation, reference):
    import support
    from shirabe_addon import offline, offline_audio

    snapshot = preparation.snapshot
    without_examples = copy.deepcopy(snapshot)
    example_count = 0
    for segment in without_examples["segments"]:
        for result in segment["results"]:
            for candidate in result["candidates"]:
                example_count += len(candidate.pop("examples", []))
    if "sources" in without_examples:
        without_examples["sources"] = [row for row in without_examples["sources"] if row["slug"] != "tatoeba"]
    recordings = offline_audio.recordings(snapshot)
    audio_bytes = sum(info["bytes"] for info in recordings.values())
    payload_bytes = len(offline._json(snapshot))
    stored_bytes = len(offline.snapshot_file(snapshot)[1])
    reference_bytes = len(reference.encode("utf-8"))
    return {"prepared_tokens": preparation.coverage.prepared, "examples": example_count,
            "snapshot_json_bytes": payload_bytes, "snapshot_media_bytes": stored_bytes,
            "examples_and_attribution_bytes": payload_bytes - len(offline._json(without_examples)),
            "pronunciation_files": len(recordings), "pronunciation_bytes": audio_bytes,
            "snapshot_reference_bytes": reference_bytes, "saved_payload_bytes": stored_bytes + audio_bytes,
            "payload_with_reference_bytes": stored_bytes + audio_bytes + reference_bytes}


def build(manifest_path, root, bundle_sha256, recording_sha256, example_path=None):
    import support
    from anki.collection import Collection, DeckIdLimit
    from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageRequest
    from shirabe_addon import assets, offline, offline_audio, template
    from shirabe_addon.authoring_media import ANKI_PERMISSION, LicensedMedia, Recording, download_selected_media

    bundle = assets.WEB / "shirabe-card.js"
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != bundle_sha256:
        raise ValueError("The shared popup bundle changed.")
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["apkg"])
    if not manifest["deck"].startswith("Shirabe Test ") or hashlib.sha256(source.read_bytes()).hexdigest() != manifest["apkg_sha256"]:
        raise ValueError("Expected the unchanged isolated test package.")
    example_fixture = manifest.get("public_examples")
    if example_path is not None:
        if example_path.stat().st_size > offline.MAX_RESPONSE_BYTES:
            raise ValueError("Public examples exceed the response limit.")
        example_fixture = json.loads(example_path.read_text())
    col = Collection(str(root / "collection.anki2"))
    try:
        col.import_anki_package(ImportAnkiPackageRequest(package_path=str(source)))
        notes = [col.get_note(row["id"]) for row in manifest["notes"]]
        if set(col.find_notes("")) != {note.id for note in notes}:
            raise ValueError("Unexpected notes in the test package.")
        for note, row in zip(notes, manifest["notes"]):
            if note.guid != row["guid"] or note.fields != row["fields"] or not note.note_type()["name"].startswith(manifest["deck"]):
                raise ValueError("A test note or its type changed.")
        deck = col.decks.by_name(manifest["deck"])
        if deck is None or any(card.did != deck["id"] for note in notes for card in note.cards()):
            raise ValueError("The test notes do not belong to the named fixture deck.")
        selected = [note for note in notes if "ShirabeLookup" in note and "Front" in note and note["Front"] == "猫がいます。"]
        if len(selected) != 1:
            raise ValueError("Expected one prepared cat definition note.")
        note = selected[0]
        snapshot = offline.read_snapshot(col.media, offline.snapshot_filename(note["ShirabeLookup"]))
        if snapshot is None:
            raise ValueError("The existing snapshot is not valid.")
        url = "https://cdn.shirabe.org/audio/words/231d52edbc4efb2b35e7603c18c0050f0150846e17b28208e10219a0b63dd448.m4a"
        recording = Recording(url, "ねこ", 1, "HL", download_url=url[:-4] + ".mp3")
        downloaded = download_selected_media(LicensedMedia(recording.download_url, "Shirabe", ANKI_PERMISSION),
                                             allowed_hosts={"cdn.shirabe.org"}, max_bytes=offline_audio.MAX_CLIP_BYTES)
        info = offline_audio.metadata(downloaded)
        if info is None or info["sha256"] != recording_sha256:
            raise ValueError("The approved pronunciation changed.")
        preparation = prepare_cat(snapshot, recording, downloaded, example_fixture)
        snapshot = preparation.snapshot
        stored = offline.persist_snapshot(col.media, preparation)
        note["ShirabeLookup"] = offline.snapshot_field(stored.filename, stored.recordings)
        col.update_note(note)
        installed = assets.install(col.media)
        for mid in {item.mid for item in notes if "ShirabeLookup" in item}:
            model = col.models.get(mid)
            for card in model["tmpls"]:
                card["afmt"] = template.with_block(card["afmt"], template.PRODUCTION,
                    template.CLIENT_ID, "click", assets=installed, snapshot_field=True)
            col.models.update_dict(model)
        apkg = root / "shirabe-ankiweb-prepared-audio.apkg"
        col.export_anki_package(out_path=str(apkg), limit=DeckIdLimit(deck["id"]),
            options=ExportAnkiPackageOptions(with_media=True, with_scheduling=False, with_deck_configs=False, legacy=True))
        check = Collection(str(root / "verification.anki2"))
        try:
            check.import_anki_package(ImportAnkiPackageRequest(package_path=str(apkg)))
            assert set(check.find_notes("")) == {item.id for item in notes}
            for expected in notes:
                actual = check.get_note(expected.id)
                assert actual.guid == expected.guid and actual.fields == expected.fields
                assert len(actual.cards()) == len(expected.cards())
            actual = check.get_note(note.id)
            assert info["filename"] in check.media.files_in_str(actual.mid, actual["ShirabeLookup"])
            assert not actual.cards()[0].answer_av_tags()
            assert (Path(check.media.dir()) / info["filename"]).read_bytes() == downloaded.data
            assert offline.read_snapshot(check.media, stored.filename) == snapshot
            assert hashlib.sha256((Path(check.media.dir()) / installed["shirabe-card.js"]).read_bytes()).hexdigest() == bundle_sha256
            files = [p for p in Path(check.media.dir()).iterdir() if p.is_file()]
            manifest["media"] = {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
            if example_fixture is not None:
                expected = [{"japanese": row["japanese"], "translation": row["translations"][0]["text"]}
                            for row in example_fixture["response"]["sentences"]]
                result = subprocess.run(["node", str(Path(__file__).with_name("offline_examples_sdk_probe.mjs"))],
                                        input=json.dumps({"snapshot": offline.read_snapshot(check.media, stored.filename), "expectedExamples": expected,
                                                          "media": {"filename": stored.filename,
                                                                    "base64": base64.b64encode((Path(check.media.dir()) / stored.filename).read_bytes()).decode()}}),
                                        capture_output=True, text=True, timeout=20, check=True)
                sdk_check = json.loads(result.stdout)
                assert sdk_check["bundleHash"] == bundle_sha256
                save_json(root / "saved-example-sdk.json", sdk_check)
        finally:
            check.close()
        manifest.update(apkg=str(apkg), apkg_sha256=hashlib.sha256(apkg.read_bytes()).hexdigest(), deck_id=deck["id"],
                        native_reimport_verified=True, prepared_audio_note_id=note.id,
                        prepared_recording=info, sdk_sha256=bundle_sha256,
                        scope="Single synthetic cat token/definition with optional captured public examples and live approved CDN recording; production snapshot preparation and native APKG reimport, not linguistic segmentation, native consent or production OAuth",
                        notes=[{"id": n.id, "guid": n.guid, "fields": n.fields, "cards": [c.id for c in n.cards()]} for n in notes])
        manifest["prepared_sizes"] = {**payload_sizes(preparation, note["ShirabeLookup"]),
                                      "incremental_media_bytes": stored.bytes_used,
                                      "all_exported_media_bytes": sum(row["bytes"] for row in manifest["media"].values()),
                                      "apkg_bytes": apkg.stat().st_size}
        manifest["preparation_requests"] = preparation.requests
        if example_fixture is not None:
            manifest["public_examples"] = example_fixture
        save_json(root / "ankiweb-prepared-audio.json", manifest)
        return apkg
    finally:
        col.close()


if __name__ == "__main__":
    require_anki()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--recording-sha256", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--examples", type=Path, help="Captured public examples and corpus provenance JSON")
    args = parser.parse_args()
    print(build(args.manifest.resolve(strict=True), output_directory(args.output), args.bundle_sha256,
                args.recording_sha256,
                args.examples.resolve(strict=True) if args.examples else None))
