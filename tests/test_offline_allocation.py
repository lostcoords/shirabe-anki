import copy
import unittest

from offline_fixture import candidate, parse, prepared
from shirabe_addon import offline


def alternatives(query):
    return {"words": [{"candidates": [candidate(query["surface"], source_id)
                                      for source_id in ("first", "selected", "last")]}]}


def only_primary(snapshot):
    snapshot = copy.deepcopy(snapshot)
    snapshot["complete"] = False
    for segment in snapshot["segments"]:
        for result in segment["results"]:
            token = segment["tokens"][result["tokenIndex"]]
            candidates = result["candidates"]
            result["candidates"] = [next((value for value in candidates
                                         if offline._entry_id(value) == token.get("entry")), candidates[0])]
    return snapshot


class AllocationTests(unittest.TestCase):
    def test_tokens_use_the_first_identified_candidate(self):
        full = prepared(("猫犬",), identify=alternatives)
        expected = only_primary(full.snapshot)
        result = prepared(("猫犬",), identify=alternatives,
                          budget=offline.Budget(max_bytes=len(offline._json(expected))))
        self.assertEqual(result.snapshot, expected)
        self.assertEqual(result.coverage.prepared, 2)
        self.assertTrue(all(token["entry"].endswith(":first") for token in result.snapshot["segments"][0]["tokens"]))

    def test_complete_results_retain_order_and_every_candidate(self):
        result = prepared(("猫犬",), identify=alternatives)
        self.assertTrue(result.complete)
        for item in result.snapshot["segments"][0]["results"]:
            self.assertEqual([value["sourceId"] for value in item["candidates"]], ["first", "selected", "last"])
        self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)

    def test_api_empty_result_is_retained_and_not_a_storage_failure(self):
        result = prepared(("猫犬",), identify=lambda query: alternatives(query) if query["surface"] == "猫" else {"words": [None]})
        self.assertEqual(result.coverage.prepared, 2)
        self.assertEqual(result.snapshot["segments"][0]["results"][1], {"tokenIndex": 1, "candidates": []})
        self.assertTrue(result.complete)


if __name__ == "__main__":
    unittest.main()
