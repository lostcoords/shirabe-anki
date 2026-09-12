"""Audit native Previewer runs against the measured, fixed-workload memory ceiling."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
from urllib.parse import urlsplit


MAX_RETAINED_HEAP_GROWTH = 128 * 1024


def distribution(values):
    ordered = sorted(values)
    return {"count": len(values), "medianMs": statistics.median(values),
            "p95Ms": ordered[(len(values) * 95 + 99) // 100 - 1], "maximumMs": max(values)}


def summarize(report):
    assert report["completed"] is True, "Native run did not complete"
    assert report["servedSdkSha256"] == report["sdkSha256"], "Served SDK differs"
    count = report["selectedCards"]
    assert count == 33, "The retained-memory ceiling was measured on 33 cards; rebaseline other workloads"
    assert report["requestedWarmupSweeps"] == 1 and report["requestedMeasuredSweeps"] == 2
    path = [*range(count), *range(count - 2, -1, -1)]
    sweep = [(index, warm) for index in path for warm in (False, True)]
    samples = report["samples"]
    assert [(row["index"], row["warm"]) for row in samples] == sweep * 3, "Incomplete card/lookup sequence"
    for row in samples:
        assert row["rendered"] == row["headword"], "Wrong headword"
        assert row["ends"] == [.12, .12], "Incomplete real transitions"
        assert row["entry"] and 0 <= row["visibleMs"] < 10000
    proof = report["gcProof"]
    allocated = proof["allocated"] - proof["before"]
    assert allocated > 4 * 1024 * 1024 and proof["allocated"] - proof["recovered"] > .9 * allocated, "GC was not effective"
    checkpoints = report["checkpoints"]
    assert [row["label"] for row in checkpoints] == ["baseline-after-warmup", "after-measured-sweep-1",
                                                     "after-measured-sweep-2", "after-idle-120-frames"]
    baseline = checkpoints[0]
    for index, row in enumerate(checkpoints):
        assert row["sampleCount"] == len(sweep) * min(index + 1, 3)
        assert row["state"]["index"] == 0 and row["state"]["answer"] is True
        assert row["state"]["online"] is False and row["state"]["visible"] == "visible"
        assert all(n > 0 for n in row["state"]["viewport"])
        assert row["dom"] == baseline["dom"], "Retained DOM or listeners grew"
        assert row["heap"]["usedSize"] - baseline["heap"]["usedSize"] <= MAX_RETAINED_HEAP_GROWTH, "Retained heap exceeded 128 KiB"
    assert all(event["method"] == "Runtime.executionContextCreated" for event in report["contextEvents"]), "Page reloaded during the run"
    assert len(report["contextEvents"]) == 1, "Expected one persistent JS context"
    traffic = report["traffic"]
    assert all(urlsplit(row["origin"]).hostname == "127.0.0.1" for row in traffic), "Nonlocal network request"
    measured = samples[len(sweep):]
    final = checkpoints[-1]
    return {"status": "pass", "cards": count, "openings": len(samples), "measuredOpenings": len(measured),
            "firstAfterNavigation": distribution([r["visibleMs"] for r in measured if not r["warm"]]),
            "immediateRepeat": distribution([r["visibleMs"] for r in measured if r["warm"]]),
            "retainedHeapGrowthBytes": final["heap"]["usedSize"] - baseline["heap"]["usedSize"],
            "retainedHeapGrowthCeilingBytes": MAX_RETAINED_HEAP_GROWTH,
            "retainedDom": final["dom"], "domGrowth": 0,
            "mainProcessPssKiB": [row["appPssKb"] for row in checkpoints],
            "requests": len(traffic), "snapshotRequests": sum(row["path"].startswith("/_shirabe_snapshot") for row in traffic),
            "nonlocalRequests": 0, "sdkSha256": report["sdkSha256"],
            "scope": "One warmup plus two measured native Previewer sweeps, same first card at checkpoints. First/repeat openings, not app cold starts. Shared-Mac emulator latency is diagnostic. PSS covers the app process, not every WebView renderer."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = args.report.read_bytes()
    summary = {**summarize(json.loads(raw)), "reportSha256": hashlib.sha256(raw).hexdigest()}
    encoded = json.dumps(summary, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as output:
            output.write(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
