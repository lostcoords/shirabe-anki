"""Rerunnable size/build benchmark with synthetic definitions and modeled latency.

Run: python3 tests/offline_measure.py --rounds 5 --latency-ms 25
No DOM capture, sync, dictionary packs, or real network are timed.
--native-output separately times and verifies real Anki media persistence.
"""

import argparse
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
import platform
import statistics
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import support
from offline_fixture import identify, parse
from shirabe_addon import authoring_media, offline, offline_audio, offline_ui

CASES = (
    ("two_words", ["猫", "犬"]),
    ("emoji_and_repeat", ["😀猫は猫", "犬"]),
    ("twelve_repeated_words", ["猫犬猫犬猫犬猫犬", "犬猫犬猫"]),
    ("request_boundary", ["猫" * 64]),
    ("request_limited", ["猫" * 70]),
)
PREFERENCES = {
    "stackFingerprint": "fixture-stack-v1", "dictionaries": ["jmdict:es", "jmdict:en"],
    "languages": ["es", "en"], "uiLocale": "es", "dictionaryReveal": {"jmdict:en": "hover"},
}
SOURCES = [{"slug": "jmdict", "name": "JMdict", "source": {
    "name": "EDRDG", "license": "CC BY-SA 4.0", "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
}}]
RECORDING_URL = "https://cdn.shirabe.org/audio/words/231d52edbc4efb2b35e7603c18c0050f0150846e17b28208e10219a0b63dd448.m4a"
LOCAL_P95_MS_PER_SET = 350
JSON_BYTES_PER_SET = {(False, False): 96000, (True, False): 100000, (False, True): 144000, (True, True): 150000}


def check_budgets(report):
    options, timing = report["options"], report["timing"]
    copies, examples, audio = options["copies"], options["examples"], options["audio"]
    samples = timing["measured_local_samples_ms"]
    if (not isinstance(samples, list) or len(samples) < 12
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in samples)):
        raise ValueError("Preparation budgets require at least 12 finite local timing samples")
    p95 = sorted(samples)[math.ceil(len(samples) * .95) - 1]
    if p95 != timing["measured_local_p95_ms"] or len(samples) != timing["rounds"]:
        raise ValueError("Preparation timings do not match their samples")
    limits = {"local_p95_ms": LOCAL_P95_MS_PER_SET * copies,
              "requests": (8 + 8 * examples) * copies + 2 + 3 * audio,
              "snapshot_json_bytes": JSON_BYTES_PER_SET[(examples, audio)] * copies,
              "minimum_complete_notes": (2 if examples else 5) * copies}
    if p95 > limits["local_p95_ms"] or report["requests"] > limits["requests"] or report["batch_payload_bytes"] > limits["snapshot_json_bytes"]:
        raise ValueError("Preparation exceeded its local latency, request or JSON-size budget")
    if (len(report["notes"]) != len(CASES) * copies or report["complete_notes"] < limits["minimum_complete_notes"]
            or report["stopped"] or report["skipped_notes"]):
        raise ValueError("Preparation coverage regressed")
    for row, (case, _), minimum in zip(report["notes"], CASES * copies, (2, 3, 12, 64, 70) * copies):
        if row["case"] != case or row["prepared"] < minimum:
            raise ValueError("Prepared token coverage regressed")
    return limits


class FixtureTransport:
    def __init__(self, examples=None):
        self.calls = []
        self.example_response = examples

    def open(self, request, *, timeout):
        url = urlsplit(request.full_url)
        path, query = url.path, parse_qs(url.query)
        body = json.loads(request.data) if request.data else None
        assert timeout == 15
        if body is not None:
            assert request.get_method() == "POST"
            if path != "/api/v1/parse":
                assert body["dictionaries"] == ["jmdict"]
        else:
            assert request.get_method() == "GET"
        self.calls.append((path, body))
        if path == "/api/v1/parse":
            payload = parse(body["texts"][0])
        elif path == "/api/v1/words/identify":
            if body["include"] == ["pitch"]:
                payload = identify(body["tokens"][0])
                for row in payload["words"][0]["candidates"]:
                    if row["dictionary"] == "jmdict" and row["sourceId"] == "1":
                        row["pitch"] = [{"downstep": 1, "pattern": "HL", "audioUrl": RECORDING_URL}]
            else:
                assert body["include"] == ["pitch", "frequency", "furigana", "jlpt", "forms", "notes", "relations"]
                payload = identify(body["tokens"][0])
        elif path == "/api/v1/sentences":
            assert self.example_response is not None and query["limit"] == ["2"]
            payload = self.example_response if query["entry"] == ["jmdict:1"] else {"sentences": [], "pagination": {"hasMore": False}}
        else:
            raise AssertionError(path)
        if path in ("/api/v1/parse", "/api/v1/words/identify"):
            payload["stackFingerprint"] = PREFERENCES["stackFingerprint"]
        data = json.dumps(payload, ensure_ascii=False).encode()
        response = io.BytesIO(data)
        response.headers = {"Content-Length": str(len(data))}
        return response


def measure(*, rounds=5, latency_ms=25, include_examples=False, audio_data=None, copies=1, media=None):
    if (type(rounds) is not int or not 1 <= rounds <= 100 or type(latency_ms) not in (int, float)
            or not 0 < latency_ms <= 60_000 or type(copies) is not int or not 1 <= copies <= 20):
        raise ValueError("Use 1–100 rounds, 1–20 copies and a modeled request latency of 1–60,000 ms")
    if audio_data is not None and (not isinstance(audio_data, bytes) or not 0 < len(audio_data) <= offline_audio.MAX_CLIP_BYTES
                                   or not authoring_media._signature_matches(audio_data, "m4a")):
        raise ValueError("Use one bounded M4A fixture recording")
    corpus = json.loads(Path(__file__).with_name("fixtures").joinpath("tatoeba_cat_examples.json").read_text()) if include_examples else None
    sources = [*SOURCES, corpus["corpus"]] if corpus else SOURCES
    elapsed = []
    cases = CASES * copies
    notes = [offline_ui.CapturedNote(i, "fixture", (), None, None) for i in range(len(cases))]
    runs = {i: texts for i, (_, texts) in enumerate(cases)}
    for _ in range(rounds):
        transport = FixtureTransport(corpus["response"] if corpus else None)
        downloads = []
        def download(selection, *, allowed_hosts, cancelled, max_bytes):
            assert selection.url == RECORDING_URL and allowed_hosts == {"cdn.shirabe.org"} and not cancelled()
            if audio_data is None or len(audio_data) > max_bytes:
                raise offline.SnapshotError("Fixture media exceeds download budget", code="audio_limit")
            downloads.append(selection.url)
            return authoring_media.DownloadedMedia(audio_data, "m4a", selection.attribution, selection.license)
        backend = offline.DictionaryBackend("https://fixture.invalid", json.dumps({"access": "fixture", "expiresAt": 2000}),
                                            dictionaries=["jmdict"], now=lambda: 1, stack_fingerprint=PREFERENCES["stackFingerprint"])
        backend._opener = transport
        with patch.object(authoring_media, "download_selected_media", side_effect=download), \
                patch.object(offline_ui, "time", SimpleNamespace(time=lambda: 1_700_000_000)):
            started = time.perf_counter()
            batch = offline_ui.prepare_batch(notes, runs, backend, preferences=PREFERENCES, approved={"jmdict"}, sources=sources,
                                             include_examples=include_examples, include_audio=audio_data is not None)
        elapsed.append((time.perf_counter() - started) * 1000)
    rows = []
    shared_files = {}
    snapshots = {}
    for captured, preparation in batch.rows:
        wire = json.dumps(preparation.snapshot, ensure_ascii=False, separators=(",", ":")).encode()
        assert len(wire) == preparation.bytes_used
        assert [row for row in preparation.snapshot["sources"] if row["slug"] != "tatoeba"] == SOURCES
        assert all(word not in wire for word in (b"audioUrl", b"secret", b"study"))
        recordings = offline_audio.recordings(preparation.snapshot)
        for filename, info in recordings.items():
            assert filename not in shared_files or shared_files[filename] == info
            shared_files[filename] = info
            assert len(preparation.media[filename].data) == info["bytes"]
        examples = [example for segment in preparation.snapshot["segments"] for result in segment["results"]
                    for candidate in result["candidates"] for example in candidate.get("examples", [])]
        assert include_examples or not examples
        name, stored = offline.snapshot_file(preparation.snapshot)
        snapshots[name] = len(stored)
        rows.append({
            "case": cases[captured.note_id][0], "snapshot_bytes": len(wire), "snapshot_media_bytes": len(stored),
            "export_reference_bytes": len(offline.snapshot_field(name, recordings).encode()),
            "examples": len(examples), "recording_bytes_referenced": sum(row["bytes"] for row in recordings.values()),
            "tokens": preparation.coverage.tokens, "prepared": preparation.coverage.prepared,
            "requests": preparation.requests, "complete": preparation.complete, "issues": list(preparation.issues),
        })
    requests = sum(row["requests"] for row in rows)
    assert requests == len(transport.calls) + len(downloads)
    modeled_ms = requests * latency_ms
    local_ms = statistics.median(elapsed)
    local_p95 = sorted(elapsed)[math.ceil(len(elapsed) * 0.95) - 1]
    snapshot_bytes = sum(row["snapshot_bytes"] for row in rows)
    shared_audio_bytes = sum(row["bytes"] for row in shared_files.values())
    budget_bytes = snapshot_bytes + shared_audio_bytes
    stored_bytes = sum(snapshots.values()) + shared_audio_bytes
    assert stored_bytes <= budget_bytes <= offline.MAX_TOTAL_BYTES
    report = {
        "fixture": "Synthetic one-character tokens and small bilingual definitions; not representative dictionary density",
        "runtime": {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine()},
        "source_sha256": {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                          for module in (offline, offline_ui, offline_audio, authoring_media)},
        "selected_dictionaries": ["jmdict"], "notes": rows, "requests": requests,
        "preparation_timestamp_ms": 1_700_000_000_000,
        "options": {"examples": include_examples, "audio": audio_data is not None, "copies": copies},
        "request_counts": dict(Counter(path for path, _ in transport.calls) + Counter({"audio_download": len(downloads)})),
        "complete_notes": sum(row["complete"] for row in rows),
        "batch_payload_bytes": snapshot_bytes,
        "unique_snapshot_files": len(snapshots), "unique_snapshot_bytes": sum(snapshots.values()),
        "unique_audio_files": len(shared_files), "unique_audio_bytes": shared_audio_bytes,
        "recording_sha256": hashlib.sha256(audio_data).hexdigest() if audio_data is not None else None,
        "batch_stored_bytes": stored_bytes,
        "batch_budget_bytes": budget_bytes,
        "batch_reference_bytes": sum(row["export_reference_bytes"] for row in rows),
        "skipped_notes": batch.skipped, "stopped": batch.stopped,
        "timing": {
            "network": "simulated serial requests; no network or sleeps", "request_latency_ms": latency_ms,
            "rounds": rounds, "modeled_network_ms": modeled_ms,
            "measured_local_median_ms": round(local_ms, 3),
            "measured_local_p95_ms": round(local_p95, 3),
            "measured_local_samples_ms": [round(value, 3) for value in elapsed],
            "measured_local_notes_per_second": round(len(rows) * 1000 / local_ms, 3),
            "measured_local_complete_notes_per_second": round(sum(row["complete"] for row in rows) * 1000 / local_ms, 3),
            "modeled_prepare_batch_ms": round(modeled_ms + local_ms, 3),
            "modeled_notes_per_second": round(len(rows) * 1000 / (modeled_ms + local_ms), 3),
            "modeled_complete_notes_per_second": round(sum(row["complete"] for row in rows) * 1000 / (modeled_ms + local_ms), 3),
            "excludes": ["DOM capture", "initial fixture/recording file reads", "media persistence", "sync", "real server processing and network"],
        },
    }
    if media is not None:
        from anki.buildinfo import version
        report["runtime"]["anki"] = version
        before = offline.snapshot_storage(media, audio=True)
        started = time.perf_counter()
        stored = [(item, offline.persist_snapshot(media, item)) for _, item in batch.rows]
        written = sum(saved.bytes_used for _, saved in stored)
        persist_ms = (time.perf_counter() - started) * 1000
        after = offline.snapshot_storage(media, audio=True)
        assert before.bytes_used == 0 and after.bytes_used == written == stored_bytes
        assert after.files == len(snapshots) + len(shared_files)
        for item, saved in stored:
            assert offline.read_snapshot(media, saved.filename) == item.snapshot
        for filename, info in shared_files.items():
            assert offline_audio.verified_size(media, filename, expected=info["bytes"]) == info["bytes"]
        report["native_storage"] = {"verified_bytes": after.bytes_used, "verified_files": after.files,
                                    "measured_persist_ms": round(persist_ms, 3), "samples": 1,
                                    "scope": "Production persistence into new Anki media; byte/hash checks, no notes, GUI or sync"}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--latency-ms", type=int, default=25)
    parser.add_argument("--examples", action="store_true")
    parser.add_argument("--recording", type=Path, help="Local M4A fixture bytes; simulated delivery, never played")
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--output", type=Path, help="New JSON report file")
    parser.add_argument("--native-output", type=Path, help="New isolated Anki collection for real persistence and hash checks")
    parser.add_argument("--check-budget", action="store_true", help="Require 12+ rounds and enforce the fixed workload's regression budgets")
    options = parser.parse_args()
    if options.check_budget and options.rounds < 12:
        parser.error("Budget checks require at least 12 rounds")
    if options.output and options.output.exists():
        parser.error("Choose a new report filename")
    if options.recording and options.recording.stat().st_size > offline_audio.MAX_CLIP_BYTES:
        parser.error("The recording exceeds the 1 MiB fixture limit")
    col = None
    if options.native_output:
        from fixture_deck import require_anki
        require_anki()
        from anki.collection import Collection
        options.native_output.mkdir(parents=True, exist_ok=False)
        col = Collection(str(options.native_output / "collection.anki2"))
    try:
        report = measure(rounds=options.rounds, latency_ms=options.latency_ms, include_examples=options.examples, copies=options.copies,
                         audio_data=options.recording.read_bytes() if options.recording else None, media=col.media if col else None)
    finally:
        if col:
            col.close()
    if options.check_budget:
        report["checked_budgets"] = check_budgets(report)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if options.output:
        with options.output.open("x") as output:
            output.write(rendered + "\n")
    print(rendered)
