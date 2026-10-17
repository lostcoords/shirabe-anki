"""Compare production allocation with the frozen public-corpus prototype, offline.

--check-inputs verifies provenance without importing preparation or starting Node.
--output NEW.json runs exact replay; --sdk also measures the existing bundle on
every token in allocation-changed notes. --indexes 0,941 selects a short replay.
The frozen corpus and capture are external evidence, not required by unit tests.
"""

import argparse
import ast
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("offline_allocation_inputs.json")
STAMP = 1_700_000_000
LIMIT = 128 * 1024
VARIANT = "cap3_pin_first_128KiB"
TAG = dict.fromkeys(("category", "code", "label"))
SENSE = {"position": None, "definitions": [{"lang": None, "text": None}], "tags": [TAG], "notes": None}
ENTRY = {**dict.fromkeys(("order", "dictionary", "dictionaryName", "type", "sourceId")), "senses": [SENSE]}
CANDIDATE = {
    **dict.fromkeys(("id", "dictionary", "sourceId", "headword", "reading", "common", "name", "jlpt", "frequency")),
    "furigana": [{"text": None, "ruby": None}], "pitch": [{"downstep": None, "pattern": None}],
    "forms": [{"text": None, "script": None, "common": None, "tags": [TAG]}], "entries": [ENTRY],
}
SOURCE = {"slug": None, "name": None, "attribution": None,
          "source": dict.fromkeys(("name", "url", "license", "licenseUrl"))}


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def checked_bytes(path, expected):
    if path.stat().st_size != expected["bytes"] or expected["bytes"] > 256 * 1024 * 1024:
        raise ValueError(f"Frozen input size changed: {path}")
    data = path.read_bytes()
    if digest(data) != expected["sha256"]:
        raise ValueError(f"Frozen input hash changed: {path}")
    return data


def project(value, shape):
    if value is None or shape is None:
        return deepcopy(value)
    if isinstance(shape, list):
        return [project(item, shape[0]) for item in value]
    return {key: project(value[key], child) for key, child in shape.items() if key in value}


def entry_id(candidate):
    return f"{candidate['dictionary']}:{candidate['sourceId']}"


def entry_available(token, candidates):
    return not token.get("entry") or any(entry_id(candidate) == token["entry"] for candidate in candidates)


def frozen_allocator(source):
    names = {"choose", "empty", "arrange"}
    nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != names or len(nodes) != len(names):
        raise ValueError("Frozen prototype allocation functions are missing")
    namespace = {"wire": wire, "deepcopy": deepcopy,
                 "offline": SimpleNamespace(_entry_id=entry_id, _entry_available=entry_available)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<hash-verified allocation prototype>", "exec"), namespace)
    return namespace["arrange"], namespace["choose"]


def parsed_tokens(text, answer):
    tokens = []
    encoded = text.encode("utf-16-le")
    end = 0
    for raw in answer["results"][0]["tokens"]:
        token = {"surface": raw["surface"], "start": raw["startOffset"], "length": raw["endOffset"] - raw["startOffset"],
                 "content": raw.get("kind") not in ("function", "symbol")}
        token.update({key: raw[source] for key, source in (("lemma", "lemma"), ("reading", "reading"), ("pos", "partOfSpeech"))
                      if raw.get(source) is not None})
        if raw.get("inflection") is not None:
            token.update(inflection=[form["label"] for form in raw["inflection"]["forms"]], inflectionBase=raw["inflection"]["base"])
        assert token["start"] >= end
        end = token["start"] + token["length"]
        assert encoded[token["start"] * 2:end * 2] == token["surface"].encode("utf-16-le")
        tokens.append(token)
    assert len(tokens) <= 128 and len(encoded) <= 4096 * 2
    return tokens


def query(token):
    return {"lemma": token.get("lemma") or token["surface"], "surface": token["surface"],
            **{key: token[key] for key in ("reading", "pos") if token.get(key)}}


def captured_answers(records):
    answers = {}
    for record in records.values():
        request = record["request"]
        if urlsplit(request["path"]).path != "/api/v1/words/identify":
            continue
        value = json.loads(record["response_body"])["words"][0]
        candidates = []
        for raw in value["candidates"] if value else []:
            assert raw["dictionary"] == "jmdict"
            assert all(entry["dictionary"] == "jmdict" for entry in raw["entries"])
            candidate = project(raw, CANDIDATE)
            assert len(wire(candidate)) <= 24 * 1024
            assert [s["definitions"] for e in raw["entries"] for s in e["senses"]] == [s["definitions"] for e in candidate["entries"] for s in e["senses"]]
            candidates.append(candidate)
        key = wire(request["body"]["tokens"][0])
        if key in answers:
            assert answers[key] == candidates
        answers[key] = candidates
    return answers


def note_reference(text, metadata, records, answers):
    preferences = metadata["preferences"]
    request = {"method": "POST", "path": "/api/v1/parse", "body": {"texts": [text]}}
    tokens = parsed_tokens(text, json.loads(records[wire(request)]["response_body"]))
    version = digest(wire({"stack": preferences["stackFingerprint"], "dictionaries": preferences["dictionaries"],
                           "languages": preferences["languages"], "locale": preferences["uiLocale"], "approved": ["jmdict"]}))
    snapshot = {"formatVersion": 1, "contentVersion": version, "createdAt": STAMP * 1000, "complete": False,
                "preferences": {"languages": preferences["languages"], "dictionaryReveal": preferences.get("dictionaryReveal", {})},
                "sources": [project(source, SOURCE) for source in metadata["sources"] if source["slug"] == "jmdict"],
                "segments": [{"text": text, "contentHash": digest(text.encode()), "tokens": tokens, "results": []}]}
    items = []
    for index, token in enumerate(tokens):
        if not token["content"]:
            continue
        candidates = answers[wire(query(token))]
        eligible = [candidate for candidate in candidates if candidate.get("name") is not True or entry_id(candidate) == token.get("entry")] or candidates
        items.append({"si": 0, "ti": index, "token": token, "eligible": eligible})
    return snapshot, items


def assert_equivalent(actual, expected, *, note):
    assert len(actual["segments"]) == len(expected["segments"]), f"note {note}: segments changed"
    tokens = candidates = pinned = 0
    for si, (left, right) in enumerate(zip(actual["segments"], expected["segments"])):
        assert left["text"] == right["text"] and left["contentHash"] == right["contentHash"], f"note {note}: exact text changed"
        assert left["tokens"] == right["tokens"], f"note {note}: token spans or parser pins changed"
        expected_results = {row["tokenIndex"]: row["candidates"] for row in right["results"]}
        actual_results = {row["tokenIndex"]: row["candidates"] for row in left["results"]}
        assert len(actual_results) == len(left["results"]), f"note {note}: duplicate token results"
        for ti, token in enumerate(right["tokens"]):
            assert actual_results.get(ti) == expected_results.get(ti), f"note {note}, segment {si}, token {ti}: ordered candidate payload differs"
            tokens += token.get("content") is not False
            pinned += bool(token.get("entry"))
            if ti in actual_results:
                assert entry_available(token, actual_results[ti]), f"note {note}, token {ti}: pinned candidate missing"
                candidates += len(actual_results[ti])
    assert actual == expected, f"note {note}: snapshot metadata or result ordering differs"
    assert len(wire(actual)) <= LIMIT, f"note {note}: decoded byte limit exceeded"
    return {"tokens_compared": tokens, "candidate_occurrences_compared": candidates, "parser_pins_compared": pinned}


@contextmanager
def sdk_probe(enabled, bundle_hash):
    if not enabled:
        yield None
        return
    process = subprocess.Popen(["node", str(Path(__file__).with_name("offline_allocation_sdk.mjs")), bundle_hash],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    def check(value):
        data = wire(value) + b"\n"
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("SDK probe input exceeds 4 MiB")
        process.stdin.write(data)
        process.stdin.flush()
        if not select.select([process.stdout], [], [], 45)[0]:
            raise TimeoutError("Windowless SDK did not respond")
        answer = process.stdout.readline(4 * 1024 * 1024 + 1)
        if len(answer) > 4 * 1024 * 1024 or not answer.endswith(b"\n"):
            raise ValueError("Windowless SDK response missing or oversized")
        return json.loads(answer)
    try:
        yield check
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
    assert process.returncode == 0, "Windowless SDK failed"


def replay(inputs, *, indexes=None, sdk=False):
    corpus = json.loads(inputs["corpus"])
    baseline = json.loads(inputs["baseline"])
    prototype = json.loads(inputs["prototype_report"])
    records = [json.loads(line) for line in inputs["capture"].splitlines()]
    metadata = records.pop(0)["metadata"]
    assert metadata["capture_version"] == 2
    assert records.pop() == {"complete": True, "requests": len(records)}
    assert metadata["corpus_sha256"] == digest(inputs["corpus"])
    assert prototype["capture_sha256"] == digest(inputs["capture"])
    captured = {wire(row["request"]): row for row in records}
    assert len(captured) == len(records)
    assert baseline["corpus_indexes"] == list(range(len(corpus)))
    assert len(corpus) == len(baseline["notes"]) == prototype["notes"] == 2464
    assert all(row["index"] == i and row["text"] == corpus[i]["text"] for i, row in enumerate(baseline["notes"]))
    arrange, choose = frozen_allocator(inputs["prototype_code"])
    answers = captured_answers(captured)
    source_paths = [ROOT / "src" / name for name in ("offline.py", "offline_ui.py", "offline_audio.py")]
    source_paths.append(Path(__file__).with_name("offline_corpus_measure.py"))
    source_hashes = {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in source_paths}
    import support
    from offline_corpus_measure import Responses
    from shirabe_addon import offline, offline_ui
    assert source_hashes == {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in source_paths}, "Source changed during import"
    assert offline.MAX_SNAPSHOT_BYTES == LIMIT and offline.MAX_CANDIDATES == 3 and offline_ui.MAX_SELECTED_NOTES == 200
    print(json.dumps({"loaded_source_sha256": source_hashes}), flush=True)
    selected = list(range(len(corpus))) if indexes is None else indexes
    assert selected and len(set(selected)) == len(selected) and all(0 <= i < len(corpus) for i in selected)
    compared, totals, notes, files, sdk_notes = Counter(), Counter(), [], {}, []
    preferences = metadata["preferences"]
    with sdk_probe(sdk, digest(inputs["sdk_bundle"])) as check_sdk:
        for offset, index in enumerate(selected):
            if offset % 200 == 0:
                backend = offline.DictionaryBackend("https://fixture.invalid", '{"access":"fixture","expiresAt":2000}', now=lambda: 1,
                                                    dictionaries=["jmdict"], locale="en", stack_fingerprint=preferences["stackFingerprint"])
                transport = Responses(captured)
                backend._opener = transport
                cache = offline.LookupCache(backend)
            snapshot, items = note_reference(corpus[index]["text"], metadata, captured, answers)
            before, _, _ = arrange(snapshot, items, 3, LIMIT, "sequential")
            expected, _, picked = arrange(snapshot, items, 3, LIMIT, "pin_first")
            old = baseline["notes"][index]
            assert len(wire(before)) == old["snapshot_bytes"] and before["complete"] == old["complete"], f"note {index}: baseline reconstruction changed"
            assert sum(len(row["candidates"]) for row in before["segments"][0]["results"]) == old["candidates"]
            assert len(before["segments"][0]["results"]) == old["prepared"]
            preparation = offline.prepare_snapshot([corpus[index]["text"]], parse=backend.parse, identify=backend.identify,
                                                   content_version=snapshot["contentVersion"], allowed_dictionaries={"jmdict"},
                                                   preferences=snapshot["preferences"], sources=metadata["sources"],
                                                   now=lambda: STAMP, lookup_cache=cache)
            assert not transport.failures, transport.failures
            compared.update(assert_equivalent(preparation.snapshot, expected, note=index))
            assert preparation.bytes_used == len(wire(expected))
            prepared = sum(value is not None and entry_available(item["token"], value) for item, value in zip(items, picked))
            assert preparation.coverage.prepared == prepared and preparation.coverage.tokens == len(items)
            assert preparation.complete == expected["complete"]
            assert offline.validate_snapshot(preparation.snapshot) == preparation.snapshot
            name, data = offline.snapshot_file(preparation.snapshot)
            files[name] = len(data)
            totals.update(tokens_prepared=prepared, candidate_occurrences=sum(len(value) for value in picked if value is not None),
                          notes_all_eligible_candidates_present=expected["complete"], notes_missing_tokens=prepared < len(items),
                          representation_bytes=len(wire(expected)), stored_envelope_bytes=len(data))
            changed = before != expected
            notes.append({"index": index, "changed": changed, "tokens": len(items), "prepared": prepared,
                          "baseline_sha256": digest(wire(before)), "prototype_sha256": digest(wire(expected)),
                          "production_sha256": digest(wire(preparation.snapshot)), "issues": preparation.issues})
            if check_sdk and changed:
                checks = [{"token": item["ti"], "candidates": choose(item["eligible"], item["token"], 3)} for item in items]
                result = check_sdk({"index": index, "before": before, "after": preparation.snapshot, "checks": checks})
                assert result["networkAttempts"] == 0 and result["tokenSpansEqual"] is True
                sdk_notes.append(result)
            if (offset + 1) % 100 == 0:
                print(json.dumps({"compared_notes": offset + 1, "selected": len(selected), "sdk_notes": len(sdk_notes)}), flush=True)
    totals.update(unique_stored_bytes=sum(files.values()), media_files=len(files))
    full = selected == list(range(len(corpus)))
    if full:
        expected_totals = prototype["variants"][VARIANT]
        for key in ("tokens_prepared", "candidate_occurrences", "notes_all_eligible_candidates_present", "notes_missing_tokens", "unique_stored_bytes", "media_files"):
            assert totals[key] == expected_totals[key], f"Prototype total differs: {key}"
        for key in ("representation_bytes", "stored_envelope_bytes"):
            assert totals[key] == expected_totals[key]["total"], f"Prototype byte total differs: {key}"
    sdk_totals = {side: Counter() for side in ("before", "after")}
    transitions, regressions = Counter(), []
    for note in sdk_notes:
        for row in note["rows"]:
            for side in sdk_totals:
                sdk_totals[side].update(row[side])
            lost = []
            for metric in ("primaryExpectedDefinition", "contextualExpectedDefinition", "primaryExpectedAlternatives", "contextualExpectedAlternatives"):
                before, after = row["before"][metric], row["after"][metric]
                if before and not after:
                    transitions[metric + "_lost"] += 1
                    lost.append(metric)
                    if not row["pinned"] and row["repeatedContext"]:
                        transitions[metric + "_lost_repeated_unpinned"] += 1
                elif after and not before:
                    transitions[metric + "_gained"] += 1
            if lost:
                regressions.append({"note": note["index"], "metrics": lost, **row})
    return {"full_corpus": full, "variant": VARIANT, "notes_compared": len(notes), "per_token_assertions": compared,
            "input_sha256": {key: digest(value) for key, value in inputs.items()}, "loaded_source_sha256": source_hashes,
            "fixed_timestamp_seconds": STAMP, "totals": totals, "notes": notes, "sdk_totals": sdk_totals, "sdk_notes": sdk_notes,
            "sdk_transitions": transitions, "sdk_regressions": regressions,
            "scope": "Captured responses only; no network, native collection, GUI or persistence. Exact candidates, parser pins, spans, metadata and per-note object equality are asserted independently of production projection. SDK metrics cover every content token in allocation-changed notes, against the captured three-candidate result, not linguistic truth. Stored bytes exclude code, audio and prior generations."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-inputs", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--indexes", help="Comma-separated zero-based corpus indexes; default all")
    parser.add_argument("--sdk", action="store_true")
    args = parser.parse_args()
    if bool(args.check_inputs) == bool(args.output) or args.output and args.output.exists():
        parser.error("Choose --check-inputs or --output with a new report path")
    if args.check_inputs and (args.indexes or args.sdk):
        parser.error("--check-inputs does not run preparation or the SDK")
    manifest = json.loads(MANIFEST.read_bytes())
    inputs = {key: checked_bytes(ROOT / spec["path"], spec) for key, spec in manifest.items()}
    if args.check_inputs:
        print(json.dumps({"verified_inputs": manifest, "preparation_started": False, "sdk_started": False}, indent=2))
        return
    report = replay(inputs, indexes=[int(value) for value in args.indexes.split(",")] if args.indexes else None, sdk=args.sdk)
    with args.output.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"report": str(args.output), "notes": report["notes_compared"], "totals": report["totals"], "sdk": report["sdk_totals"]}, indent=2))


if __name__ == "__main__":
    main()
