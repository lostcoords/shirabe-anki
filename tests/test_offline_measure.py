import os
import math
import json
import copy
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from check_against_anki import anki_python
from offline_measure import check_budgets, measure
from test_offline_audio import M4A


class OfflineMeasurementTests(unittest.TestCase):
    def test_production_preparation_reports_payload_bytes_and_labels_simulated_network(self):
        report = measure(rounds=2, latency_ms=25)
        self.assertEqual(report["timing"]["network"], "simulated serial requests; no network or sleeps")
        self.assertEqual(report["timing"]["modeled_network_ms"], 10 * 25)
        self.assertEqual(report["requests"], 10)
        self.assertEqual(report["selected_dictionaries"], ["jmdict"])
        self.assertEqual(report["complete_notes"], 5)
        self.assertGreater(report["timing"]["measured_local_median_ms"], 0)
        self.assertEqual([row["tokens"] for row in report["notes"]], [2, 3, 12, 64, 70])
        self.assertEqual([row["prepared"] for row in report["notes"]], [2, 3, 12, 64, 70])
        self.assertEqual(report["notes"][-1]["issues"], [])
        self.assertEqual(sum(row["snapshot_bytes"] for row in report["notes"]), report["batch_payload_bytes"])
        self.assertTrue(all(0 < row["snapshot_bytes"] <= 128 * 1024 for row in report["notes"]))
        self.assertTrue(all(row["export_reference_bytes"] == 213 for row in report["notes"]))

    def test_optional_media_is_charged_once_across_repeated_cards_and_requests_are_counted(self):
        report = measure(rounds=2, include_examples=True, audio_data=M4A, copies=2)
        self.assertEqual(len(report["notes"]), 10)
        self.assertEqual(report["unique_audio_files"], 1)
        self.assertEqual(report["unique_audio_bytes"], len(M4A))
        self.assertEqual(report["request_counts"]["audio_download"], 1)
        self.assertEqual(report["request_counts"]["/api/v1/words/identify"], 4)
        self.assertEqual(report["request_counts"]["/api/v1/sentences"], 16)
        self.assertEqual(report["requests"], sum(report["request_counts"].values()))
        self.assertEqual(report["batch_budget_bytes"], report["batch_payload_bytes"] + len(M4A))
        self.assertEqual(report["batch_stored_bytes"], report["unique_snapshot_bytes"] + len(M4A))
        self.assertEqual(report["unique_snapshot_files"], 5)
        self.assertLess(report["batch_stored_bytes"], report["batch_budget_bytes"])
        self.assertEqual(sum(row["recording_bytes_referenced"] for row in report["notes"]), 10 * len(M4A))
        self.assertGreater(report["batch_reference_bytes"], 10 * 213)
        self.assertTrue(any(row["examples"] for row in report["notes"]))
        self.assertTrue(all(row["examples"] <= 8 for row in report["notes"]))
        self.assertFalse(report["stopped"])

    def test_modes_keep_unrequested_optional_content_and_requests_absent(self):
        for examples, audio in ((True, None), (False, M4A)):
            with self.subTest(examples=examples):
                report = measure(rounds=1, include_examples=examples, audio_data=audio)
                self.assertEqual(any(row["examples"] for row in report["notes"]), examples)
                self.assertEqual(bool(report["request_counts"].get("/api/v1/sentences")), examples)
                self.assertEqual(report["unique_audio_files"], int(audio is not None))
                self.assertEqual(bool(report["request_counts"].get("audio_download")), audio is not None)

    def test_percentiles_match_raw_local_samples_and_never_include_modeled_network_time(self):
        report = measure(rounds=12, latency_ms=100)
        timing = report["timing"]
        samples = timing["measured_local_samples_ms"]
        self.assertEqual(len(samples), 12)
        self.assertEqual(timing["measured_local_p95_ms"], sorted(samples)[math.ceil(len(samples) * .95) - 1])
        self.assertEqual(timing["modeled_network_ms"], 1000)
        self.assertAlmostEqual(timing["modeled_prepare_batch_ms"], timing["measured_local_median_ms"] + 1000, places=3)
        self.assertIn("real server processing and network", timing["excludes"])

    def test_invalid_or_oversized_audio_is_rejected_before_the_benchmark(self):
        for audio in (b"", b"not audio", M4A + b"x" * 1048576):
            with self.assertRaises(ValueError):
                measure(rounds=1, audio_data=audio)

    def test_regression_budgets_reject_slow_extra_requests_lost_coverage_and_invalid_timings(self):
        ticks = iter(range(24))
        with patch("offline_measure.time", SimpleNamespace(perf_counter=lambda: next(ticks) / 1000)):
            report = measure(rounds=12)
        self.assertEqual(report["timing"]["measured_local_samples_ms"], [1] * 12)
        limits = check_budgets(report)
        for field, value in (("requests", limits["requests"] + 1), ("batch_payload_bytes", limits["snapshot_json_bytes"] + 1),
                             ("complete_notes", limits["minimum_complete_notes"] - 1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_budgets({**report, field: value})
        for samples in ([limits["local_p95_ms"] + 1] * 12, [True] * 12, [float("nan")] * 12, [1] * 11):
            changed = copy.deepcopy(report)
            changed["timing"]["measured_local_samples_ms"] = samples
            changed["timing"]["measured_local_p95_ms"] = samples[-1]
            with self.assertRaises(ValueError):
                check_budgets(changed)
        changed = copy.deepcopy(report)
        changed["notes"][0]["prepared"] = 1
        with self.assertRaises(ValueError):
            check_budgets(changed)

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_measurement_verifies_unique_files_and_keeps_a_rerunnable_report(self):
        with tempfile.TemporaryDirectory(prefix="shirabe-preparation-measure-") as temporary:
            root = Path(temporary)
            recording = root / "fixture.m4a"
            recording.write_bytes(M4A)
            result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("offline_measure.py")),
                                     "--rounds", "1", "--copies", "2", "--examples", "--recording", str(recording),
                                     "--output", str(root / "report.json"), "--native-output", str(root / "native")],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(json.loads(result.stdout), report)
            self.assertEqual(report["native_storage"]["verified_files"], 6)
            self.assertEqual(report["native_storage"]["verified_bytes"], report["batch_stored_bytes"])
            self.assertLess(report["batch_stored_bytes"], report["batch_budget_bytes"])
            self.assertGreater(report["native_storage"]["measured_persist_ms"], 0)
            self.assertEqual(report["native_storage"]["samples"], 1)
            self.assertIn("anki", report["runtime"])
            self.assertEqual(set(report["source_sha256"]), {"offline.py", "offline_ui.py", "offline_audio.py", "authoring_media.py"})

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_collection_counts_duplicates_generations_and_batch_bytes(self):
        result = subprocess.run(
            [str(anki_python()), str(Path(__file__).with_name("snapshot_storage_probe.py"))],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native snapshot storage passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
