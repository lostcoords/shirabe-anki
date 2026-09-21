import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.request import Request

from offline_corpus_measure import Responses, distribution, measure, wire
from offline_measure import FixtureTransport, PREFERENCES, SOURCES
from check_against_anki import anki_python
from shirabe_addon import offline


class CorpusMeasurementTests(unittest.TestCase):
    def test_canonical_keys_ignore_object_order_but_preserve_japanese(self):
        self.assertEqual(wire({"text": "猫", "x": 1}), wire({"x": 1, "text": "猫"}))
        self.assertIn("猫".encode(), wire({"text": "猫"}))

    def test_distribution_reports_actual_sizes_and_nearest_rank_percentiles(self):
        self.assertEqual(distribution(list(range(1, 21))),
                         {"count": 20, "total": 210, "min": 1, "median": 10.5, "p95": 19, "max": 20})
        self.assertEqual(distribution([])["total"], 0)

    def test_native_preparation_consumes_and_replays_the_exact_capture(self):
        fixture = FixtureTransport()
        def fetch(query):
            request = Request("https://fixture.invalid" + query["path"], data=wire(query["body"]), method=query["method"])
            with fixture.open(request, timeout=15) as response:
                return {"response_body": response.read().decode(), "controller_ms": 1.5}
        journal = io.BytesIO()
        captured = Responses(fetch=fetch, journal=journal)
        metadata = {"preferences": PREFERENCES, "sources": SOURCES}
        report = measure(["猫", "猫"], metadata, captured)
        self.assertEqual(report["selected_notes"], 2)
        self.assertEqual(report["complete_notes"], 2)
        self.assertEqual(report["requests"], {"/api/v1/parse": 2, "/api/v1/words/identify": 1})
        self.assertEqual(len(captured.records), 2)
        self.assertEqual(len(journal.getvalue().splitlines()), 2)
        self.assertEqual(report["controller_executions"]["count"], 2)
        self.assertEqual(report["controller_executions"]["total"], 3)
        self.assertEqual(report["replay_hits"], 1)
        self.assertEqual(report["recorded_controller_ms_for_logical_requests"]["total"], 4.5)
        self.assertEqual(report["snapshot_bytes"]["total"], sum(row["snapshot_bytes"] for row in report["notes"]))
        self.assertEqual(report["unique_snapshot_bytes"]["count"], 1)
        self.assertEqual(report["unique_snapshot_bytes"]["total"] * 2, report["snapshot_bytes"]["total"])
        self.assertEqual(report["unique_snapshot_media_bytes"]["total"] * 2, report["snapshot_media_bytes"]["total"])
        self.assertLess(report["snapshot_media_bytes"]["total"], report["snapshot_bytes"]["total"])
        replayed = measure(["猫", "猫"], metadata, Responses(captured.records), rounds=2)
        self.assertEqual(replayed["snapshot_bytes"], report["snapshot_bytes"])
        self.assertEqual(replayed["snapshot_media_bytes"], report["snapshot_media_bytes"])
        self.assertEqual(replayed["unique_snapshot_media_bytes"], report["unique_snapshot_media_bytes"])
        self.assertEqual(replayed["requests"], report["requests"])
        self.assertEqual(replayed["controller_executions"]["count"], 0)
        self.assertEqual(replayed["replay_hits"], 3)
        self.assertEqual(len(replayed["preparation_samples_ms"]), 2)
        self.assertIn("No HTTP, WAN", replayed["timing_scope"])
        self.assertLess(report["unique_candidate_bytes"]["total"], report["candidate_bytes"]["total"])

    def test_missing_capture_fails_instead_of_becoming_an_apparently_valid_partial_note(self):
        transport = Responses()
        with self.assertRaisesRegex(RuntimeError, "absent"):
            measure(["猫"], {"preferences": PREFERENCES, "sources": SOURCES}, transport)
        self.assertEqual(len(transport.failures), 1)

    def test_production_response_cap_remains_active_during_replay(self):
        query = {"method": "POST", "path": "/api/v1/parse", "body": {"texts": ["猫"]}}
        transport = Responses({wire(query): {"response_body": json.dumps({"padding": "x" * offline.MAX_RESPONSE_BYTES}), "controller_ms": 1}})
        report = measure(["猫"], {"preferences": PREFERENCES, "sources": SOURCES}, transport)
        self.assertEqual(report["complete_notes"], 0)
        self.assertEqual(report["batches"][0]["issues"], {"response_limit": 1})
        self.assertEqual(report["snapshot_bytes"]["total"], 0)

    def test_transport_preserves_original_wire_bytes_before_the_native_cap(self):
        query = {"method": "POST", "path": "/api/v1/parse", "body": {"texts": ["猫"]}}
        raw = json.dumps({"padding": "猫" * 60_000}, ensure_ascii=True)
        self.assertGreater(len(raw.encode()), offline.MAX_RESPONSE_BYTES)
        transport = Responses({wire(query): {"payload": json.loads(raw), "response_body": raw, "controller_ms": 1}})
        with transport.open(Request("https://fixture.invalid" + query["path"], data=wire(query["body"])), timeout=15) as response:
            self.assertEqual(response.read(), raw.encode())

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_native_replay_verifies_media_and_counts_duplicate_snapshots_once(self):
        fixture = FixtureTransport()
        with tempfile.TemporaryDirectory(prefix="shirabe-corpus-native-") as temporary:
            root = Path(temporary)
            corpus, capture = root / "corpus.json", root / "capture.jsonl"
            corpus.write_text(json.dumps(["猫", "猫"]))
            with capture.open("wb") as journal:
                metadata = {"capture_version": 2, "preferences": PREFERENCES, "sources": SOURCES}
                journal.write(wire({"metadata": metadata}) + b"\n")
                def fetch(query):
                    request = Request("https://fixture.invalid" + query["path"], data=wire(query["body"]), method=query["method"])
                    with fixture.open(request, timeout=15) as response:
                        return {"response_body": response.read().decode(), "controller_ms": 1}
                transport = Responses(fetch=fetch, journal=journal)
                expected = measure(["猫", "猫"], metadata, transport)
                journal.write(wire({"complete": True, "requests": len(transport.records)}) + b"\n")
            result = subprocess.run([str(anki_python()), str(Path(__file__).with_name("offline_corpus_measure.py")), str(corpus),
                                     "--replay", str(capture), "--output", str(root / "report.json"),
                                     "--native-output", str(root / "native")], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["native_storage"]["verified_bytes"], expected["unique_snapshot_media_bytes"]["total"])
            self.assertEqual(report["native_storage"]["verified_files"], 1)
            self.assertEqual(report["native_storage"]["saved_notes"], 2)
            self.assertIsNone(report["native_storage"]["stopped"])
            self.assertEqual(report["native_storage"]["collection_budget_bytes"], offline.MAX_TOTAL_BYTES)
            self.assertIn("no notes, GUI or sync", report["native_storage"]["scope"])


if __name__ == "__main__":
    unittest.main()
