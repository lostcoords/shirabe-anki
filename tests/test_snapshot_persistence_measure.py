import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import support
from check_against_anki import anki_python
from offline_fixture import prepared
from shirabe_addon import offline


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).with_name("snapshot_persistence_measure.py")


class PersistenceMeasurementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="anki-persistence-test-", dir=ROOT / "tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.item = prepared()
        self.name, self.data = offline.snapshot_file(self.item.snapshot)
        (self.source / self.name).write_bytes(self.data)

    def harness(self):
        return importlib.import_module("snapshot_persistence_measure")

    def test_preload_verifies_hashes_and_preserves_preparation_timestamp_and_decoded_size(self):
        loaded = self.harness().preload(self.source)
        self.assertEqual(len(loaded), 1)
        row = loaded[0]
        self.assertEqual(row.preparation.snapshot, self.item.snapshot)
        self.assertEqual(row.preparation.coverage, self.item.coverage)
        self.assertEqual(row.preparation.bytes_used, len(offline._json(self.item.snapshot)))
        self.assertEqual((row.preparation.requests, row.preparation.issues, row.preparation.media), (0, (), {}))
        self.assertEqual((row.filename, row.data), (self.name, self.data))
        self.assertEqual(row.input_sha256, hashlib.sha256(self.data).hexdigest())

    def test_corrupt_owned_files_and_symlinked_files_are_refused(self):
        source = self.source / self.name
        source.write_bytes(b"invalid")
        with self.assertRaisesRegex(ValueError, "verified"):
            self.harness().preload(self.source)
        source.unlink()
        target = self.root / "target"
        target.write_bytes(self.data)
        source.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink|regular"):
            self.harness().preload(self.source)

    def test_empty_input_and_recording_metadata_are_refused(self):
        (self.source / self.name).unlink()
        with self.assertRaisesRegex(ValueError, "snapshot"):
            self.harness().preload(self.source)
        from test_offline_audio import first_candidate, recording
        snapshot = prepared(("猫",)).snapshot
        first_candidate(snapshot)["pitch"][0]["recording"] = recording()
        name, data = offline.snapshot_file(snapshot)
        (self.source / name).write_bytes(data)
        with self.assertRaisesRegex(ValueError, "recording"):
            self.harness().preload(self.source)

    def test_paths_reject_existing_targets_symlink_ancestors_current_and_live_profiles(self):
        harness = self.harness()
        native = self.root / "native"
        output = self.root / "report.json"
        link = self.root / "alias"
        link.symlink_to(self.source, target_is_directory=True)
        for source, report, destination in (
            (link, output, native),
            (self.source, link / "report.json", native),
            (self.source, output, link / "native"),
            (self.source, output, Path.cwd()),
            (self.source, output, self.source),
            (self.source, self.source / self.name, native),
            (self.source, output, Path.home() / "Library/Application Support/Anki2/new-benchmark"),
            (self.source, output, self.root / ".." / "new-benchmark"),
            (self.source, self.source / "report.json", native),
        ):
            with self.subTest(source=source, report=report, destination=destination), self.assertRaises(ValueError):
                harness.validate_paths(source, report, destination)
        self.assertFalse(output.exists())
        self.assertFalse(native.exists())

    def test_rounds_and_default_budget_are_bounded_before_creating_output(self):
        harness = self.harness()
        for rounds in (0, 13, True):
            with self.subTest(rounds=rounds), self.assertRaises(ValueError):
                harness.run(self.source, self.root / "report.json", self.root / "native", rounds=rounds)
        rows = harness.preload(self.source)
        with patch.object(harness, "preload", return_value=rows), patch.object(offline, "MAX_TOTAL_BYTES", len(self.data) - 1):
            with self.assertRaisesRegex(ValueError, "budget"):
                harness.run(self.source, self.root / "report.json", self.root / "native", rounds=1)
        self.assertFalse((self.root / "report.json").exists())
        self.assertFalse((self.root / "native").exists())

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_cli_uses_fresh_native_collections_and_reports_separate_timing_and_exact_readback(self):
        raw = offline._json(self.item.snapshot, sort_keys=True)
        legacy = f"_shirabe_snapshot_v1_{hashlib.sha256(raw).hexdigest()}.json"
        (self.source / legacy).write_bytes(raw)
        report_path = self.root / "report.json"
        native = self.root / "native"
        result = subprocess.run([str(anki_python()), str(SCRIPT), str(self.source), "--output", str(report_path),
                                 "--native-output", str(native), "--rounds", "2"],
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["input_count"], 2)
        self.assertEqual(report["collection_budget_bytes"], offline.MAX_TOTAL_BYTES)
        self.assertTrue(report["sources_unchanged"])
        self.assertTrue(report["inputs_unchanged"])
        self.assertIn("offline.py", report["source_sha256"])
        self.assertEqual(report["persist_ms"]["count"], 2)
        self.assertEqual(report["persist_cpu_ms"]["count"], 2)
        self.assertEqual(report["outer_readback_ms"]["count"], 2)
        self.assertEqual([row["filename"] for row in report["inputs"]], sorted([legacy, self.name]))
        for row in report["rounds"]:
            self.assertEqual((row["persist_calls"], row["verified_files"], row["verified_bytes"]), (2, 1, len(self.data)))
            self.assertEqual(row["rows_after"], {"notes": 0, "cards": 0, "revlog": 0})
            self.assertEqual(len(row["per_snapshot_persist_ms"]), 2)
            self.assertEqual(len(row["per_snapshot_persist_cpu_ms"]), 2)
            self.assertTrue(all(sample > 0 for sample in row["per_snapshot_persist_cpu_ms"]))
            self.assertEqual(row["persist_cpu_ms"], sum(row["per_snapshot_persist_cpu_ms"]))
            self.assertGreater(row["outer_readback_ms"], 0)
            self.assertEqual((native / row["directory"] / "collection.media" / self.name).read_bytes(), self.data)
        self.assertNotEqual(report["rounds"][0]["directory"], report["rounds"][1]["directory"])

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_exact_budget_fits_and_same_preparation_objects_are_reused_without_changing_cwd(self):
        try:
            import anki.collection
        except ImportError:
            self.skipTest("Run this check using installed Anki Python")
        harness = self.harness()
        loaded = harness.preload(self.source)
        original_cwd = Path.cwd()
        persist = offline.persist_snapshot
        cpu_clock = Mock(side_effect=[1, 1.125, 2, 2.25])
        with patch.object(harness, "preload", return_value=loaded), patch.object(offline, "MAX_TOTAL_BYTES", len(self.data)), \
                patch.object(harness, "time", SimpleNamespace(perf_counter=time.perf_counter, process_time=cpu_clock)), \
                patch.object(offline, "persist_snapshot", wraps=persist) as calls:
            report = harness.run(self.source, self.root / "report.json", self.root / "native", rounds=2)
        self.assertEqual(Path.cwd(), original_cwd)
        self.assertEqual(calls.call_count, 2)
        self.assertTrue(all(call.args[1] is loaded[0].preparation for call in calls.call_args_list))
        self.assertTrue(all(call.kwargs == {"remaining_bytes": len(self.data)} for call in calls.call_args_list))
        self.assertTrue(all(row["verified_bytes"] == len(self.data) for row in report["rounds"]))
        self.assertEqual(cpu_clock.call_count, 4)
        self.assertEqual([row["per_snapshot_persist_cpu_ms"] for row in report["rounds"]], [[125], [250]])
        self.assertEqual(report["persist_cpu_ms"],
                         {"count": 2, "min": 125, "median": 187.5, "p95": 250, "max": 250, "total": 375})


if __name__ == "__main__":
    unittest.main()
