import copy
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import support
from check_against_anki import anki_python
from offline_rich_content_fixture import candidate_with_size, prepare_rich, rich_candidate, rich_entry
from shirabe_addon import offline


HERE = Path(__file__).resolve().parent


def replace_field(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def field(value, path):
    for key in path:
        if isinstance(value, dict) and key not in value:
            return None
        value = value[key]
    return value


class RichContentTests(unittest.TestCase):
    def test_native_preparation_and_validation_preserve_all_public_rich_content(self):
        result = prepare_rich()
        self.assertTrue(result.complete)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.snapshot["segments"][0]["results"][0]["candidates"], [rich_candidate()])
        self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)
        self.assertEqual(result.bytes_used, len(offline._json(result.snapshot)))

    def test_rich_field_limits_match_the_sdk_without_increasing_aggregate_limits(self):
        compact_tag = {"name": "t", "category": "c", "order": 0, "score": -1}
        headword = {"text": "猫", "reading": "ねこ", "tags": [], "rules": [], "score": -1}
        cases = [
            (("senses", 0, "definitions", 0, "title"), "x" * 2048, "x" * 2049),
            (("senses", 0, "usages", 0, "title"), "x" * 2048, "x" * 2049),
            (("senses", 0, "usages", 0, "text"), "😀" * 2048, "😀" * 2048 + "x"),
            (("senses", 0, "label"), "x" * 512, "x" * 513),
            (("senses", 0, "tags", 0, "scope"), "x" * 512, "x" * 513),
            (("senses", 0, "usages"), [{"lang": "ja", "text": "例"}] * 32, [{"lang": "ja", "text": "例"}] * 33),
            (("senses", 0, "sourceTags"), [compact_tag] * 128, [compact_tag] * 129),
            (("senses", 0, "sourceTags", 0, "notes"), "x" * 2048, "x" * 2049),
            *[(("senses", 0, kind), [{"text": "猫"}] * 32, [{"text": "猫"}] * 33)
              for kind in ("related", "antonyms", "seeAlso")],
            (("senses", 0, "related", 0, "text"), "😀" * 1024, "😀" * 1024 + "x"),
            (("senses", 0, "related", 0, "reading"), "x" * 2048, "x" * 2049),
            (("senses", 0, "related", 0, "label"), "x" * 256, "x" * 257),
            (("sourceHeadwords",), [headword] * 128, [headword] * 129),
            (("sourceHeadwords", 0, "tags"), [compact_tag] * 32, [compact_tag] * 33),
            (("sourceHeadwords", 0, "rules"), ["n"] * 16, ["n"] * 17),
            (("sourceHeadwords", 0, "rules", 0), "x" * 128, "x" * 129),
            (("sourceHeadwords", 0, "text"), "x" * 512, "x" * 513),
            (("sourceHeadwords", 0, "reading"), "x" * 512, "x" * 513),
        ]
        for path, accepted, rejected in cases:
            with self.subTest(path=path):
                entry = rich_entry()
                replace_field(entry, path, accepted)
                self.assertEqual(field(offline._clean(offline.ENTRY, entry), path), accepted)
                replace_field(entry, path, rejected)
                with self.assertRaises(offline.SnapshotError):
                    offline._clean(offline.ENTRY, entry)
        self.assertEqual(offline.MAX_ENTRY_BYTES, 24 * 1024)
        self.assertEqual(offline.MAX_SNAPSHOT_BYTES, 128 * 1024)

    def test_signed_finite_publisher_numbers_survive_and_malformed_values_are_rejected(self):
        for path in (("sourceHeadwords", 0, "score"), ("sourceHeadwords", 0, "tags", 0, "order"),
                     ("senses", 0, "sourceTags", 0, "score")):
            for number in (-3.5, 0, 7.25):
                with self.subTest(path=path, number=number):
                    entry = rich_entry()
                    replace_field(entry, path, number)
                    self.assertEqual(field(offline._clean(offline.ENTRY, entry), path), number)
            for invalid in (True, None, "1", float("inf"), float("-inf"), float("nan")):
                with self.subTest(path=path, invalid=repr(invalid)):
                    entry = rich_entry()
                    replace_field(entry, path, invalid)
                    with self.assertRaises(offline.SnapshotError):
                        offline._clean(offline.ENTRY, entry)

    def test_malformed_rich_entry_cannot_discard_a_separate_healthy_word(self):
        cases = [
            (("senses", 0, "usages"), None),
            (("senses", 0, "definitions", 0, "title"), None),
            (("senses", 0, "depth"), True),
            (("senses", 0, "depth"), -1),
            (("senses", 0, "depth"), 1.5),
            (("senses", 0, "sourceTags"), {}),
            (("senses", 0, "related"), {}),
            (("senses", 0, "related", 0, "reading"), 17),
            (("sourceHeadwords", 0, "tags", 0, "score"), "wrong"),
            (("sourceHeadwords", 0, "rules"), "n"),
        ]
        for path, invalid in cases:
            with self.subTest(path=path, invalid=invalid):
                candidate = rich_candidate()
                replace_field(candidate["entries"][0], path, invalid)
                result = prepare_rich(candidate, texts=("猫", "犬"))
                self.assertFalse(result.complete)
                self.assertEqual(result.coverage.prepared, 1)
                self.assertTrue(result.issues)
                self.assertEqual(result.snapshot["segments"][0]["results"], [])
                self.assertEqual(result.snapshot["segments"][1]["results"][0]["candidates"][0]["headword"], "犬")

    def test_unknown_private_fields_are_stripped_at_every_rich_object_boundary(self):
        candidate = rich_candidate()
        entry = candidate["entries"][0]
        sense = entry["senses"][0]
        headword = entry["sourceHeadwords"][0]
        for value in (candidate, entry, sense, sense["definitions"][0], sense["usages"][0], sense["tags"][0],
                      sense["sourceTags"][0], sense["related"][0], sense["seeAlso"][0], headword, headword["tags"][0], candidate["forms"][0]["tags"][0]):
            value["account"] = {"access": "PRIVATE_SENTINEL", "decks": ["PRIVATE_SENTINEL"]}
        result = prepare_rich(candidate)
        self.assertTrue(result.complete)
        self.assertNotIn("PRIVATE_SENTINEL", offline._json(result.snapshot).decode())
        self.assertEqual(result.snapshot["segments"][0]["results"][0]["candidates"], [rich_candidate()])

    def test_candidate_exact_byte_limit_fits_and_one_more_byte_is_rejected(self):
        candidate = candidate_with_size(offline.MAX_ENTRY_BYTES)
        fitted = prepare_rich(candidate)
        self.assertTrue(fitted.complete)
        self.assertEqual(fitted.snapshot["segments"][0]["results"][0]["candidates"], [candidate])
        rejected = prepare_rich(candidate_with_size(offline.MAX_ENTRY_BYTES + 1), texts=("猫", "犬"))
        self.assertFalse(rejected.complete)
        self.assertIn("entry_limit", rejected.issues)
        self.assertEqual(rejected.coverage.prepared, 1)

    def test_snapshot_exact_decoded_limit_fits_and_one_more_byte_is_rejected(self):
        snapshot = copy.deepcopy(prepare_rich().snapshot)
        template = snapshot["segments"][0]
        snapshot["segments"] = []
        for index in range(6):
            segment = copy.deepcopy(template)
            text = "猫" * (index + 1)
            segment.update(text=text, contentHash=offline.snapshot_hash(text))
            segment["tokens"] = [{"surface": text, "lemma": "猫", "start": 0, "length": len(text), "content": True}]
            segment["results"][0]["candidates"] = [candidate_with_size(21 * 1024) if index < 5 else rich_candidate()]
            snapshot["segments"].append(segment)
        last = snapshot["segments"][-1]["results"][0]["candidates"]
        size = offline.MAX_SNAPSHOT_BYTES - len(offline._json(snapshot)) + len(offline._json(last[0]))
        last[0] = candidate_with_size(size)
        self.assertEqual(len(offline._json(snapshot)), offline.MAX_SNAPSHOT_BYTES)
        self.assertEqual(offline.validate_snapshot(snapshot), snapshot)
        last[0]["entries"][0]["senses"][0]["notes"][-1] += "x"
        self.assertEqual(len(offline._json(snapshot)), offline.MAX_SNAPSHOT_BYTES + 1)
        self.assertIsNone(offline.validate_snapshot(snapshot))

    def test_actual_native_persistence_retains_rich_content_and_exact_media(self):
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        expected_version = expected_buildinfo = None
        try:
            from anki import buildinfo
        except ModuleNotFoundError as error:
            if error.name != "anki":
                raise
            python = anki_python()
            if python is None:
                self.skipTest("An importable Anki library or installed Anki Python is required")
        else:
            python = sys.executable
            expected_version = buildinfo.version
            expected_buildinfo = str(Path(buildinfo.__file__).resolve())
            env["PYTHONPATH"] = os.pathsep.join(str(Path(path or os.getcwd()).resolve()) for path in sys.path)
        command = [str(python), str(HERE / "offline_rich_content_fixture.py"), "--persist"]
        if expected_version is not None:
            command += ["--expected-anki-version", expected_version, "--expected-anki-buildinfo", expected_buildinfo]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["python"], str(python))
        self.assertTrue(report["anki_version"])
        if expected_version is not None:
            self.assertEqual(report["anki_version"], expected_version)
            self.assertEqual(report["anki_buildinfo"], expected_buildinfo)
        self.assertEqual(report["rows"], {"notes": 0, "cards": 0, "revlog": 0})
        self.assertEqual(report["decoded_bytes"], len(offline._json(report["snapshot"])))
        self.assertIn(report["sha256"], report["filename"])
        self.assertGreater(report["encoded_bytes"], 0)
        self.assertEqual(report["snapshot"]["segments"][0]["results"][0]["candidates"], [rich_candidate()])

    def test_sdk_renders_python_prepared_titles_usage_only_sense_and_publisher_metadata(self):
        result = subprocess.run(["node", str(HERE / "offline_rich_content_probe.mjs")],
                                input=offline._json(prepare_rich().snapshot).decode(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-3000:])
        report = json.loads(result.stdout)
        self.assertEqual(report["network_calls"], 0)
        self.assertEqual(report["sdk_input"], "current-typescript-not-built-addon")

    def test_sdk_probe_accepts_the_complete_public_contract_without_python_projection(self):
        snapshot = prepare_rich().snapshot
        snapshot["segments"][0]["results"][0]["candidates"] = [rich_candidate()]
        result = subprocess.run(["node", str(HERE / "offline_rich_content_probe.mjs")],
                                input=offline._json(snapshot).decode(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-3000:])

    def test_sdk_probe_never_silently_ignores_a_requested_bundle(self):
        for args in (["--bundle"], ["--bundle", str(HERE / "missing-rich-artifact.js")], ["--unknown"]):
            with self.subTest(args=args):
                result = subprocess.run(["node", str(HERE / "offline_rich_content_probe.mjs"), *args],
                                        input=offline._json(prepare_rich().snapshot).decode(),
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)

    def test_sdk_probe_preserves_utf8_across_input_chunk_boundaries(self):
        snapshot = prepare_rich().snapshot
        snapshot["segments"][0]["results"][0]["candidates"] = [rich_candidate()]
        script = """
import { Readable } from 'node:stream';
import { pathToFileURL } from 'node:url';
const data = Buffer.from(process.argv[1], 'base64');
const boundary = data.indexOf(Buffer.from('猫')) + 1;
const probe = process.argv[2];
Object.defineProperty(process, 'stdin', { value: Readable.from([data.subarray(0, boundary), data.subarray(boundary)]) });
process.argv = [process.argv[0], probe];
await import(pathToFileURL(probe));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script,
                                 base64.b64encode(offline._json(snapshot)).decode(), str(HERE / "offline_rich_content_probe.mjs")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-3000:])

    @unittest.skipUnless(os.environ.get("SHIRABE_RICH_BUNDLE"), "Set SHIRABE_RICH_BUNDLE to test an exact built artifact")
    def test_requested_built_artifact_preserves_python_rich_content(self):
        result = subprocess.run(["node", str(HERE / "offline_rich_content_probe.mjs"),
                                 "--bundle", os.environ["SHIRABE_RICH_BUNDLE"]],
                                input=offline._json(prepare_rich().snapshot).decode(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-3000:])
        report = json.loads(result.stdout)
        self.assertEqual(report["sdk_input"], "built-standalone")
        self.assertEqual(report["network_calls"], 0)


if __name__ == "__main__":
    unittest.main()
