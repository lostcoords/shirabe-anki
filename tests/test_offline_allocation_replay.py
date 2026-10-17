from copy import deepcopy
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

from offline_allocation_replay import CANDIDATE, assert_equivalent, checked_bytes, digest, parsed_tokens, project


def snapshot():
    candidate = {"id": "開く", "dictionary": "jmdict", "sourceId": "1", "headword": "開く", "common": True,
                 "pitch": [], "forms": [], "entries": [{"dictionary": "jmdict", "sourceId": "1", "type": "definition",
                 "senses": [{"position": 0, "definitions": [{"lang": "en", "text": "open"}, {"lang": "es", "text": "abrir"}], "tags": []}]}]}
    other = deepcopy(candidate)
    other["sourceId"] = "2"
    other["entries"][0]["sourceId"] = "2"
    return {"formatVersion": 1, "createdAt": 1, "contentVersion": "fixture", "complete": False,
            "segments": [{"text": "開いた", "contentHash": digest("開いた".encode()),
                          "tokens": [{"surface": "開いた", "start": 0, "length": 3, "content": True, "entry": "jmdict:1",
                                      "lemma": "開く", "reading": "ヒライタ", "pos": "verb", "inflection": ["past"], "inflectionBase": "開く"}],
                          "results": [{"tokenIndex": 0, "candidates": [candidate, other]}]}]}


class AllocationReplayTests(unittest.TestCase):
    def test_exact_candidates_and_parser_pins_are_counted(self):
        value = snapshot()
        self.assertEqual(assert_equivalent(deepcopy(value), value, note=5),
                         {"tokens_compared": 1, "candidate_occurrences_compared": 2, "parser_pins_compared": 1})

    def test_same_counts_cannot_hide_changed_senses_or_alternative_order(self):
        expected = snapshot()
        for change in ("definition", "order", "identity", "missing_alternative", "duplicate_result"):
            actual = deepcopy(expected)
            result = actual["segments"][0]["results"][0]
            if change == "definition":
                result["candidates"][0]["entries"][0]["senses"][0]["definitions"][1]["text"] = "cerrar"
            elif change == "order":
                result["candidates"].reverse()
            elif change == "identity":
                result["candidates"][0]["sourceId"] = "9"
            elif change == "missing_alternative":
                result["candidates"].pop()
            else:
                actual["segments"][0]["results"].append(deepcopy(result))
            with self.subTest(change=change), self.assertRaises(AssertionError):
                assert_equivalent(actual, expected, note=5)

    def test_token_span_inflection_pin_and_metadata_changes_fail(self):
        expected = snapshot()
        for field, value in (("start", 1), ("entry", "jmdict:2"), ("inflection", ["negative"]), ("inflectionBase", "違う")):
            actual = deepcopy(expected)
            actual["segments"][0]["tokens"][0][field] = value
            with self.subTest(field=field), self.assertRaises(AssertionError):
                assert_equivalent(actual, expected, note=5)
        actual = deepcopy(expected)
        actual["createdAt"] = 2
        with self.assertRaises(AssertionError):
            assert_equivalent(actual, expected, note=5)

    def test_equal_snapshots_still_require_saved_pinned_identity(self):
        value = snapshot()
        value["segments"][0]["results"][0]["candidates"].pop(0)
        with self.assertRaisesRegex(AssertionError, "pinned candidate missing"):
            assert_equivalent(deepcopy(value), value, note=5)

    def test_frozen_projection_retains_all_definitions_but_not_online_private_fields(self):
        raw = snapshot()["segments"][0]["results"][0]["candidates"][0]
        raw["access"] = "not-exportable"
        raw["study"] = {"due": 55}
        raw["pitch"] = [{"downstep": 0, "pattern": "heiban", "audioUrl": "https://private.invalid"}]
        raw["entries"][0]["senses"][0]["depth"] = 1
        value = project(raw, CANDIDATE)
        self.assertNotIn("access", value)
        self.assertNotIn("study", value)
        self.assertEqual(value["pitch"], [{"downstep": 0, "pattern": "heiban"}])
        self.assertEqual(value["entries"][0]["senses"][0]["definitions"], raw["entries"][0]["senses"][0]["definitions"])
        value["entries"][0]["senses"][0]["definitions"][0]["text"] = "mutated"
        self.assertEqual(raw["entries"][0]["senses"][0]["definitions"][0]["text"], "open")

    def test_reference_parser_preserves_utf16_offsets_and_inflection(self):
        tokens = parsed_tokens("😀開いた", {"results": [{"tokens": [
            {"surface": "😀", "startOffset": 0, "endOffset": 2, "kind": "symbol"},
            {"surface": "開いた", "startOffset": 2, "endOffset": 5, "kind": "inflected", "lemma": "開く",
             "inflection": {"forms": [{"code": "past", "label": "past"}], "base": "開く"}}]}]})
        self.assertFalse(tokens[0]["content"])
        self.assertEqual(tokens[1]["start"], 2)
        self.assertEqual(tokens[1]["inflection"], ["past"])
        self.assertEqual(tokens[1]["inflectionBase"], "開く")

    def test_changed_frozen_hash_or_size_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="shirabe-allocation-test-") as directory:
            path = Path(directory) / "input"
            path.write_bytes(b"fixture")
            spec = {"bytes": 7, "sha256": digest(b"fixture")}
            self.assertEqual(checked_bytes(path, spec), b"fixture")
            for changed in ({**spec, "bytes": 6}, {**spec, "sha256": "0" * 64}):
                with self.assertRaises(ValueError):
                    checked_bytes(path, changed)

    @unittest.skipUnless(shutil.which("node"), "Node required for windowless SDK probe")
    def test_sdk_probe_resolves_repeated_unpinned_words_with_partial_alternatives(self):
        root = Path(__file__).resolve().parents[1]
        bundle = root / "src/web/shirabe-card.js"
        before = snapshot()
        segment = before["segments"][0]
        segment["text"] = "猫猫"
        segment["contentHash"] = digest("猫猫".encode())
        segment["tokens"] = [{"surface": "猫", "start": index, "length": 1, "content": True,
                              "lemma": "猫", "reading": "ネコ", "pos": "noun"} for index in range(2)]
        candidates = segment["results"][0]["candidates"]
        segment["results"] = [{"tokenIndex": index, "candidates": deepcopy(candidates)} for index in range(2)]
        after = deepcopy(before)
        after["segments"][0]["results"][1]["candidates"].pop()
        request = {"index": "synthetic-repeated-unpinned", "before": before, "after": after,
                   "checks": [{"token": index, "candidates": candidates} for index in range(2)]}
        result = subprocess.run(["node", str(Path(__file__).with_name("offline_allocation_sdk.mjs")), digest(bundle.read_bytes())],
                                input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["networkAttempts"], 0)
        self.assertTrue(report["tokenSpansEqual"])
        self.assertEqual(len(report["rows"]), 2)
        for row in report["rows"]:
            self.assertFalse(row["pinned"])
            self.assertTrue(row["repeatedContext"])
            for side in ("before", "after"):
                self.assertTrue(row[side]["primaryExpectedDefinition"])
                self.assertTrue(row[side]["contextualExpectedDefinition"])
                self.assertTrue(row[side]["primaryExpectedAlternatives"])
                self.assertTrue(row[side]["contextualExpectedAlternatives"])


if __name__ == "__main__":
    unittest.main()
