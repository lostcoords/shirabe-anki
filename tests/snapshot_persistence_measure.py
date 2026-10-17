"""Benchmark native persistence of verified snapshots already held under repo tmp/.

Run with installed Anki Python and QT_QPA_PLATFORM=offscreen. Both --output and
--native-output must be new paths. Every round opens a fresh empty collection;
no notes, schedules, GUI, account, network, audio or sync are used.
"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import statistics
import sys
import time
from types import SimpleNamespace

import support
from shirabe_addon import authoring_media, offline, offline_audio


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "tmp"
MAX_INPUTS = 10_000


def scratch_path(value):
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Parent traversal is not allowed")
    path = path.absolute()
    if not path.is_relative_to(SCRATCH) or path == SCRATCH or path == Path.cwd():
        raise ValueError("Use an isolated path below the repository tmp directory, never the current directory or a live profile")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Symlink paths are not allowed")
    return path


def validate_paths(source, output, native_output):
    source, output, native_output = map(scratch_path, (source, output, native_output))
    if not source.is_dir():
        raise ValueError("Input must be a snapshot directory")
    for path in (output, native_output):
        if os.path.lexists(path) or not path.parent.is_dir():
            raise ValueError("Use new outputs with existing parent directories")
        if path.is_relative_to(source) or source.is_relative_to(path):
            raise ValueError("Outputs cannot overlap the input directory")
    if output.is_relative_to(native_output) or native_output.is_relative_to(output):
        raise ValueError("Report and native outputs must be separate")
    return source, output, native_output


def owned_bytes(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Input must be a regular file, not a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= offline.MAX_SNAPSHOT_BYTES:
            raise ValueError("Input is not a bounded regular snapshot")
        data = source.read(offline.MAX_SNAPSHOT_BYTES + 1)
    match = offline.MEDIA_NAME.fullmatch(path.name)
    if not match or len(data) > offline.MAX_SNAPSHOT_BYTES or hashlib.sha256(data).hexdigest() != match[1]:
        raise ValueError("Input snapshot hash could not be verified")
    return data


@dataclass(frozen=True)
class LoadedSnapshot:
    input_filename: str
    input_sha256: str
    input_bytes: int
    preparation: offline.Preparation
    decoded_sha256: str
    filename: str
    data: bytes


def preload(source):
    source = scratch_path(source)
    media = SimpleNamespace(dir=lambda: str(source))
    paths = sorted(path for path in source.iterdir() if offline.MEDIA_NAME.fullmatch(path.name))
    if not 1 <= len(paths) <= MAX_INPUTS:
        raise ValueError("Use between 1 and 10,000 owned snapshots")
    rows = []
    total = 0
    for path in paths:
        data = owned_bytes(path)
        total += len(data)
        if total > offline.MAX_TOTAL_BYTES:
            raise ValueError("Input exceeds the default 25 MiB budget")
        snapshot = offline.read_snapshot(media, path.name)
        if snapshot is None:
            raise ValueError("Input snapshot could not be verified")
        if offline_audio.recordings(snapshot):
            raise ValueError("This persistence benchmark does not accept recording metadata")
        raw = offline._json(snapshot, sort_keys=True)
        preparation = offline.Preparation(snapshot=snapshot, coverage=offline.coverage(snapshot),
                                          bytes_used=len(raw), requests=0, issues=())
        filename, encoded = offline.snapshot_file(snapshot)
        rows.append(LoadedSnapshot(path.name, hashlib.sha256(data).hexdigest(), len(data), preparation,
                                   hashlib.sha256(raw).hexdigest(), filename, encoded))
    return rows


def source_hashes():
    paths = [Path(module.__file__) for module in (offline, offline_audio, authoring_media)] + [Path(__file__)]
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def distribution(values):
    return {"count": len(values), "min": min(values), "median": statistics.median(values),
            "p95": sorted(values)[math.ceil(len(values) * .95) - 1], "max": max(values), "total": sum(values)}


def empty_rows(col):
    rows = {table: col.db.scalar(f"select count(*) from {table}") for table in ("notes", "cards", "revlog")}
    if any(rows.values()):
        raise AssertionError("Persistence must not create notes, cards or review history")
    return rows


def measure_round(rows, destination, index):
    from anki.collection import Collection

    directory = destination / f"round-{index:02d}"
    directory.mkdir(exist_ok=False)
    original_cwd = Path.cwd()
    col = None
    try:
        col = Collection(str(directory / "collection.anki2"))
        empty_rows(col)
        if offline.snapshot_storage(col.media, audio=True).files:
            raise AssertionError("Every round must start with empty snapshot storage")
        remaining = offline.MAX_TOTAL_BYTES
        results, samples, cpu_samples = [], [], []
        for row in rows:
            started = time.perf_counter()
            cpu_started = time.process_time()
            stored = offline.persist_snapshot(col.media, row.preparation, remaining_bytes=remaining)
            cpu_elapsed = time.process_time() - cpu_started
            samples.append((time.perf_counter() - started) * 1000)
            cpu_samples.append(cpu_elapsed * 1000)
            remaining -= stored.bytes_used
            results.append(stored)
        started = time.perf_counter()
        expected = {}
        for row, stored in zip(rows, results):
            if stored.filename != row.filename:
                raise AssertionError("Persistence changed the expected encoded filename")
            if stored.bytes_used != (0 if row.filename in expected else len(row.data)):
                raise AssertionError("Persistence charged incorrect physical media bytes")
            expected[row.filename] = row.data
            if owned_bytes(Path(col.media.dir()) / stored.filename) != row.data:
                raise AssertionError("Native media differs from the preloaded encoding")
            if offline.read_snapshot(col.media, stored.filename) != row.preparation.snapshot:
                raise AssertionError("Native snapshot readback changed its decoded value")
            if hashlib.sha256(offline._json(row.preparation.snapshot, sort_keys=True)).hexdigest() != row.decoded_sha256:
                raise AssertionError("Persistence mutated a prepared snapshot")
        readback_ms = (time.perf_counter() - started) * 1000
        storage = offline.snapshot_storage(col.media, audio=True)
        written = sum(len(data) for data in expected.values())
        if storage != offline.SnapshotStorage(written, len(expected)) or remaining != offline.MAX_TOTAL_BYTES - written:
            raise AssertionError("Actual media files or bytes differ from persistence accounting")
        if set(path.name for path in Path(col.media.dir()).iterdir()) != set(expected):
            raise AssertionError("Persistence wrote unexpected media")
        return {"directory": directory.name, "persist_calls": len(results), "persist_ms": sum(samples),
                "persist_cpu_ms": sum(cpu_samples), "per_snapshot_persist_cpu_ms": cpu_samples,
                "per_snapshot_persist_ms": samples, "outer_readback_ms": readback_ms,
                "verified_bytes": storage.bytes_used, "verified_files": storage.files, "rows_after": empty_rows(col)}
    finally:
        try:
            if col is not None:
                col.close()
        finally:
            os.chdir(original_cwd)


def run(source, output, native_output, *, rounds=1):
    if type(rounds) is not int or not 1 <= rounds <= 12:
        raise ValueError("Use 1–12 rounds")
    source, output, native_output = validate_paths(source, output, native_output)
    hashes = source_hashes()
    started = time.perf_counter()
    rows = preload(source)
    preload_ms = (time.perf_counter() - started) * 1000
    expected = {row.filename: row.data for row in rows}
    if sum(map(len, expected.values())) > offline.MAX_TOTAL_BYTES:
        raise ValueError("Encoded snapshots exceed the default collection budget")
    from anki.buildinfo import version

    native_output.mkdir(exist_ok=False)
    with output.open("x") as report_file:
        samples = []
        print(json.dumps({"event": "baseline_started", "pid": os.getpid(), "input_count": len(rows),
                          "rounds": rounds, "output": str(output), "source_sha256": hashes}), flush=True)
        for index in range(1, rounds + 1):
            sample = measure_round(rows, native_output, index)
            samples.append(sample)
            print(json.dumps({"event": "round_complete", "round": index, **sample}), flush=True)
        if source_hashes() != hashes:
            raise AssertionError("Measured sources changed during the benchmark")
        if sorted(path.name for path in source.iterdir() if offline.MEDIA_NAME.fullmatch(path.name)) != [row.input_filename for row in rows]:
            raise AssertionError("Input snapshot inventory changed during the benchmark")
        for row in rows:
            if hashlib.sha256(owned_bytes(source / row.input_filename)).hexdigest() != row.input_sha256:
                raise AssertionError("Input snapshots changed during the benchmark")
        report = {"format_version": 1, "native_anki_version": version, "python": sys.version,
                  "python_executable": sys.executable, "input_directory": str(source), "input_count": len(rows),
                  "inputs": [{"filename": row.input_filename, "sha256": row.input_sha256, "bytes": row.input_bytes,
                              "decoded_sha256": row.decoded_sha256, "decoded_bytes": row.preparation.bytes_used,
                              "created_at": row.preparation.snapshot["createdAt"], "output_filename": row.filename,
                              "output_bytes": len(row.data)} for row in rows],
                  "source_sha256": hashes, "sources_unchanged": True, "inputs_unchanged": True,
                  "collection_budget_bytes": offline.MAX_TOTAL_BYTES, "preload_ms": preload_ms,
                  "rounds": samples, "persist_ms": distribution([row["persist_ms"] for row in samples]),
                  "persist_cpu_ms": distribution([row["persist_cpu_ms"] for row in samples]),
                  "outer_readback_ms": distribution([row["outer_readback_ms"] for row in samples]),
                  "timing_scope": "Wall and process CPU samples time only production persist_snapshot, including its internal verification. CPU uses process_time across all process threads, excluding waiting. Preload, collection setup/close, outer readback/equality and inventory checks are excluded.",
                  "scope": "Fresh native collection per round; same preloaded Preparation objects, timestamps and filename order. No notes, schedules, GUI, account, network, audio or sync. This is not end-to-end preparation latency."}
        json.dump(report, report_file, ensure_ascii=False, indent=2)
        report_file.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    options = parser.parse_args()
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        parser.error("Set QT_QPA_PLATFORM=offscreen")
    from fixture_deck import require_anki
    require_anki()
    try:
        report = run(options.source, options.output, options.native_output, rounds=options.rounds)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps({"event": "complete", "persist_ms": report["persist_ms"],
                      "persist_cpu_ms": report["persist_cpu_ms"],
                      "outer_readback_ms": report["outer_readback_ms"]}), flush=True)


if __name__ == "__main__":
    main()
