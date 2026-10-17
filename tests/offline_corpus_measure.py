from paths import SHIRABE
"""Measure prepared snapshots from real local controller responses, or replay a capture.

Input is a JSON array of text strings or objects containing text. Existing token
fields are ignored. Captures contain public dictionary data, never credentials.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import io
import json
import math
from pathlib import Path
import select
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import support
from shirabe_addon import offline, offline_audio, offline_ui

ROOT = Path(__file__).resolve().parents[1]
MAX_CAPTURE_BYTES = 256 * 1024 * 1024


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def distribution(values):
    return {"count": len(values), "total": sum(values), "min": min(values, default=0),
            "median": statistics.median(values) if values else 0,
            "p95": sorted(values)[math.ceil(len(values) * .95) - 1] if values else 0,
            "max": max(values, default=0)}


@contextmanager
def controller(log_path):
    with log_path.open("xb") as log:
        process = subprocess.Popen(["bundle", "exec", "ruby", str(SHIRABE / "script/anki-offline-corpus.rb")],
                                   cwd=SHIRABE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        def receive():
            if not select.select([process.stdout], [], [], 120)[0]:
                raise TimeoutError("Read-only controller did not respond in 120 seconds")
            line = process.stdout.readline(4 * 1024 * 1024 + 1)
            if not line.endswith(b"\n") or len(line) > 4 * 1024 * 1024:
                raise RuntimeError(f"Controller capture failed; inspect {log_path}")
            return json.loads(line)
        try:
            metadata = receive()
            if metadata.get("ready") is not True or metadata.get("readonly") is not True:
                raise ValueError("Read-only controller did not initialize")
            def request(value):
                process.stdin.write(wire(value) + b"\n")
                process.stdin.flush()
                return receive()
            yield metadata, request
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()


class Responses:
    def __init__(self, records=None, *, fetch=None, journal=None):
        self.records = records or {}
        self.fetch = fetch
        self.journal = journal
        self.calls = []
        self.controller_samples = []
        self.replay_hits = 0
        self.failures = []
        self.bytes_used = sum(len(wire(row)) for row in self.records.values())

    def open(self, request, *, timeout):
        url = urlsplit(request.full_url)
        query = {"method": request.get_method(), "path": url.path + ("?" + url.query if url.query else ""),
                 "body": json.loads(request.data) if request.data else None}
        key = wire(query)
        try:
            if timeout != 15 or url.netloc != "fixture.invalid":
                raise ValueError("Unexpected benchmark transport")
            if key not in self.records:
                if self.fetch is None:
                    raise ValueError("Replay requested a response absent from the real capture")
                captured = {"request": query, **self.fetch(query)}
                encoded = wire(captured)
                self.bytes_used += len(encoded)
                if self.bytes_used > MAX_CAPTURE_BYTES:
                    raise ValueError("Capture exceeds 256 MiB")
                self.records[key] = captured
                self.controller_samples.append(captured["controller_ms"])
                self.journal.write(encoded + b"\n")
                self.journal.flush()
            else:
                self.replay_hits += 1
            captured = self.records[key]
            self.calls.append((url.path, captured["controller_ms"]))
            data = captured["response_body"].encode("utf-8")
            response = io.BytesIO(data)
            response.headers = {"Content-Length": str(len(data))}
            return response
        except Exception as error:
            self.failures.append(str(error))
            raise


def prepare(texts, metadata, transport):
    preferences = metadata["preferences"]
    rows = []
    batches = []
    for offset in range(0, len(texts), offline_ui.MAX_SELECTED_NOTES):
        backend = offline.DictionaryBackend("https://fixture.invalid", json.dumps({"access": "fixture", "expiresAt": 2000}),
                                            now=lambda: 1, dictionaries=["jmdict"], locale="en",
                                            stack_fingerprint=preferences["stackFingerprint"])
        backend._opener = transport
        indexes = range(offset, min(len(texts), offset + offline_ui.MAX_SELECTED_NOTES))
        notes = [offline_ui.CapturedNote(i, "fixture", (), None, None) for i in indexes]
        with patch.object(offline_ui, "time", SimpleNamespace(time=lambda: 1_700_000_000)):
            batch = offline_ui.prepare_batch(notes, {i: [texts[i]] for i in indexes}, backend,
                                             preferences=preferences, approved={"jmdict"}, sources=metadata["sources"])
        if transport.failures:
            raise RuntimeError(transport.failures[0])
        rows.extend(batch.rows)
        batches.append({"selected": len(notes), "saved_candidates": len(batch.rows), "skipped": batch.skipped,
                        "stopped": batch.stopped, "issues": batch.issue_notes})
        print(f"Prepared {min(offset + len(notes), len(texts))}/{len(texts)} texts", file=sys.stderr, flush=True)
    return rows, batches


def persist_rows(rows, media):
    if offline.snapshot_storage(media, audio=True).files:
        raise ValueError("Native measurement requires empty snapshot storage")
    started = time.perf_counter()
    written = saved = 0
    names = set()
    remaining = offline.MAX_TOTAL_BYTES
    batch_index = None
    stopped = None
    for captured, preparation in rows:
        if captured.note_id // offline_ui.MAX_SELECTED_NOTES != batch_index:
            batch_index = captured.note_id // offline_ui.MAX_SELECTED_NOTES
            remaining = offline.MAX_TOTAL_BYTES
        try:
            stored = offline.persist_snapshot(media, preparation, remaining_bytes=remaining)
        except offline.SnapshotError as error:
            if error.code != "storage_limit":
                raise
            stopped = {"note_index": captured.note_id, "reason": error.code}
            break
        assert offline.read_snapshot(media, stored.filename) == preparation.snapshot
        written += stored.bytes_used
        remaining -= stored.bytes_used
        names.add(stored.filename)
        saved += 1
    storage = offline.snapshot_storage(media, audio=True)
    assert storage.bytes_used == written and storage.files == len(names)
    return {"verified_bytes": written, "verified_files": len(names), "saved_notes": saved,
            "stopped": stopped, "collection_budget_bytes": offline.MAX_TOTAL_BYTES,
            "persist_ms": round((time.perf_counter() - started) * 1000, 3), "samples": 1,
            "scope": "Production media persistence and read-back in a fresh Anki collection; no notes, GUI or sync"}


def measure(texts, metadata, transport, *, rounds=1, media=None):
    samples = []
    for _ in range(rounds):
        transport.calls.clear()
        transport.controller_samples.clear()
        transport.replay_hits = 0
        started = time.perf_counter()
        rows, batches = prepare(texts, metadata, transport)
        samples.append(round((time.perf_counter() - started) * 1000, 3))
    notes = []
    candidates = []
    unique_candidates = {}
    unique_snapshots = {}
    unique_media = {}
    for captured, preparation in rows:
        snapshot = preparation.snapshot
        assert offline.validate_snapshot(snapshot) == snapshot
        assert len(offline._json(snapshot)) == preparation.bytes_used <= offline.MAX_SNAPSHOT_BYTES
        unique_snapshots[hashlib.sha256(offline._json(snapshot)).hexdigest()] = preparation.bytes_used
        name, stored = offline.snapshot_file(snapshot)
        unique_media[name] = len(stored)
        count = senses = definitions = 0
        for segment in snapshot["segments"]:
            for result in segment["results"]:
                count += len(result["candidates"])
                for candidate in result["candidates"]:
                    encoded = wire(candidate)
                    candidates.append(len(encoded))
                    unique_candidates[hashlib.sha256(encoded).hexdigest()] = len(encoded)
                    senses += sum(len(entry["senses"]) for entry in candidate["entries"])
                    definitions += sum(len(sense["definitions"]) for entry in candidate["entries"] for sense in entry["senses"])
        notes.append({"index": captured.note_id, "text": texts[captured.note_id], "snapshot_bytes": preparation.bytes_used,
                      "snapshot_media_bytes": len(stored),
                      "tokens": preparation.coverage.tokens, "prepared": preparation.coverage.prepared,
                      "candidates": count, "senses": senses, "definitions": definitions,
                      "complete": preparation.complete, "issues": list(preparation.issues)})
    report = {"metadata": metadata, "selected_notes": len(texts), "text_characters": sum(map(len, texts)),
            "preparation_timestamp_ms": 1_700_000_000_000,
            "snapshot_bytes": distribution([row["snapshot_bytes"] for row in notes]),
            "unique_snapshot_bytes": distribution(list(unique_snapshots.values())),
            "snapshot_media_bytes": distribution([row["snapshot_media_bytes"] for row in notes]),
            "unique_snapshot_media_bytes": distribution(list(unique_media.values())),
            "candidate_bytes": distribution(candidates), "unique_candidate_bytes": distribution(list(unique_candidates.values())),
            "complete_notes": sum(row["complete"] for row in notes), "batches": batches, "notes": notes,
            "requests": dict(Counter(path for path, _ in transport.calls)),
            "recorded_controller_ms_for_logical_requests": distribution([value for _, value in transport.calls]),
            "controller_executions": distribution(transport.controller_samples), "replay_hits": transport.replay_hits,
            "preparation_samples_ms": samples, "preparation_ms": distribution(samples),
            "source_sha256": {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                              for module in (offline, offline_ui, offline_audio)},
            "limits": {"snapshot_bytes_per_note": offline.MAX_SNAPSHOT_BYTES, "candidate_bytes": offline.MAX_ENTRY_BYTES,
                       "candidates_per_word": offline.MAX_CANDIDATES, "default_collection_bytes": offline.MAX_TOTAL_BYTES},
            "timing_scope": "Capture misses invoke local controller actions; replay uses recorded responses. No HTTP, WAN, DOM, sync or audio.",
            "storage_scope": "JSON metrics are decoded payloads; media metrics use the production storage encoding. No shared JS, note fields, audio, database overhead or export compression."}
    if media is not None:
        from anki.buildinfo import version
        report["native_anki_version"] = version
        report["native_storage"] = persist_rows(rows, media)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", type=Path)
    mode.add_argument("--replay", type=Path)
    parser.add_argument("--indexes", help="Comma-separated zero-based corpus indexes; default includes every row")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-output", type=Path, help="New isolated Anki collection; preserves the default 25 MiB storage limit")
    options = parser.parse_args()
    if options.output.exists() or not 1 <= options.rounds <= 100 or options.capture and options.rounds != 1:
        parser.error("Use a new output, 1–100 replay rounds and exactly one capture round")
    if options.corpus.stat().st_size > 64 * 1024 * 1024:
        parser.error("Corpus exceeds 64 MiB")
    corpus = json.loads(options.corpus.read_text())
    indexes = [int(value) for value in options.indexes.split(",")] if options.indexes else list(range(len(corpus)))
    texts = [corpus[i] if isinstance(corpus[i], str) else corpus[i]["text"] for i in indexes]
    if not 0 < len(texts) <= 10_000 or any(not isinstance(value, str) or not value for value in texts):
        parser.error("Use 1–10,000 nonempty text rows")
    col = None
    if options.native_output:
        from fixture_deck import require_anki
        require_anki()
        from anki.collection import Collection
        options.native_output.mkdir(parents=True, exist_ok=False)
        col = Collection(str(options.native_output / "collection.anki2"))
        assert not col.find_notes("")
    try:
        if options.capture:
            with options.capture.open("xb") as journal, controller(options.capture.with_suffix(".log")) as (metadata, fetch):
                metadata["corpus_sha256"] = hashlib.sha256(options.corpus.read_bytes()).hexdigest()
                journal.write(wire({"metadata": metadata}) + b"\n")
                transport = Responses(fetch=fetch, journal=journal)
                report = measure(texts, metadata, transport, media=col.media if col else None)
                journal.write(wire({"complete": True, "requests": len(transport.records)}) + b"\n")
        else:
            if options.replay.stat().st_size > MAX_CAPTURE_BYTES:
                parser.error("Capture exceeds 256 MiB")
            with options.replay.open("rb") as source:
                records = [json.loads(line) for line in source]
            metadata = records.pop(0)["metadata"]
            if metadata.get("capture_version") != 2:
                parser.error("Recapture with original response bytes (capture version 2)")
            complete = records.pop()
            if complete != {"complete": True, "requests": len(records)}:
                parser.error("Capture is incomplete")
            transport = Responses({wire(row["request"]): row for row in records})
            report = measure(texts, metadata, transport, rounds=options.rounds, media=col.media if col else None)
    finally:
        if col:
            assert not col.find_notes("")
            col.close()
    report["corpus_indexes"] = indexes
    report["mode"] = "capture" if options.capture else "replay"
    with options.output.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({key: report[key] for key in ("selected_notes", "complete_notes", "snapshot_bytes", "candidate_bytes", "requests", "preparation_ms")}))


if __name__ == "__main__":
    main()
