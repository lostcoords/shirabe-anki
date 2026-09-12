from copy import deepcopy
import unittest

from android_review_report import summarize, MAX_RETAINED_HEAP_GROWTH


def completed_run():
    path = [*range(33), *range(31, -1, -1)]
    samples = [{"index": index, "warm": warm, "headword": "猫", "rendered": "猫",
                "ends": [.12, .12], "entry": "jmdict:cat", "visibleMs": 20}
               for _ in range(3) for index in path for warm in (False, True)]
    labels = ["baseline-after-warmup", "after-measured-sweep-1", "after-measured-sweep-2", "after-idle-120-frames"]
    return {"completed": True, "servedSdkSha256": "bundle", "sdkSha256": "bundle", "selectedCards": 33,
            "requestedWarmupSweeps": 1, "requestedMeasuredSweeps": 2, "samples": samples,
            "gcProof": {"before": 1_000_000, "allocated": 11_000_000, "recovered": 1_000_000},
            "checkpoints": [{"label": label, "sampleCount": min(index + 1, 3) * 130,
                             "state": {"index": 0, "answer": True, "online": False, "visible": "visible", "viewport": [396, 675]},
                             "dom": {"documents": 3, "nodes": 465, "jsEventListeners": 329},
                             "heap": {"usedSize": 1_000_000 + index * 100}, "appPssKb": 150_000}
                            for index, label in enumerate(labels)],
            "contextEvents": [{"method": "Runtime.executionContextCreated", "id": 1}],
            "traffic": [{"origin": "http://127.0.0.1:1234", "path": "/_shirabe_snapshot_v2_example.json"}]}


class NativeReportTests(unittest.TestCase):
    def test_complete_run_reports_only_post_warmup_latency(self):
        report = completed_run()
        report["samples"][0]["visibleMs"] = 500
        result = summarize(report)
        self.assertEqual(result["measuredOpenings"], 260)
        self.assertEqual(result["firstAfterNavigation"]["maximumMs"], 20)
        self.assertEqual(result["retainedHeapGrowthBytes"], 300)

    def test_rejects_incomplete_or_wrong_behavior(self):
        original = completed_run()
        for key, replacement in (("completed", False), ("servedSdkSha256", "wrong"),
                                 ("samples", original["samples"][:-1]), ("contextEvents", []), ("selectedCards", 32)):
            with self.subTest(key=key), self.assertRaises(AssertionError):
                summarize({**original, key: replacement})
        for key, replacement in (("ends", [.12]), ("rendered", "犬")):
            report = deepcopy(original)
            report["samples"][0][key] = replacement
            with self.subTest(key=key), self.assertRaises(AssertionError):
                summarize(report)

    def test_rejects_growth_and_nonlocal_requests(self):
        for metric in ("documents", "nodes", "jsEventListeners"):
            report = completed_run()
            report["checkpoints"][-1]["dom"][metric] += 1
            with self.subTest(metric=metric), self.assertRaisesRegex(AssertionError, "Retained DOM"):
                summarize(report)
        report = completed_run()
        report["checkpoints"][-1]["heap"]["usedSize"] = 1_000_000 + MAX_RETAINED_HEAP_GROWTH + 1
        with self.assertRaisesRegex(AssertionError, "128 KiB"):
            summarize(report)
        report = completed_run()
        report["traffic"][0]["origin"] = "https://example.com"
        with self.assertRaisesRegex(AssertionError, "Nonlocal"):
            summarize(report)


if __name__ == "__main__":
    unittest.main()
