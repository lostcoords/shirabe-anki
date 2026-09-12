import copy
import unittest

from offline_fixture import candidate, parse, prepared
from shirabe_addon import offline


def pinned(text):
    answer = parse(text)
    for token in answer["tokens"]:
        token["entry"] = {"dictionary": "jmdict", "sourceId": "selected"}
    return answer


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
    def test_each_token_gets_its_pinned_definition_before_earlier_alternatives(self):
        for texts in (("猫犬",), ("猫", "犬")):
            with self.subTest(texts=texts):
                full = prepared(texts, parse=pinned, identify=alternatives)
                expected = only_primary(full.snapshot)
                budget = offline.Budget(max_bytes=len(offline._json(expected)))
                result = prepared(texts, parse=pinned, identify=alternatives, budget=budget)
                self.assertEqual(result.coverage.prepared, 2)
                self.assertEqual(result.snapshot, expected)
                self.assertEqual(result.bytes_used, budget.max_bytes)
                self.assertEqual(result.requests, full.requests)
                self.assertEqual(result.issues, ("byte_limit",))
                self.assertFalse(result.complete)
                self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)

    def test_unpinned_tokens_get_first_candidate_without_inventing_entry_identity(self):
        full = prepared(("猫犬",), identify=alternatives)
        expected = only_primary(full.snapshot)
        result = prepared(("猫犬",), identify=alternatives,
                          budget=offline.Budget(max_bytes=len(offline._json(expected))))
        self.assertEqual(result.snapshot, expected)
        self.assertEqual(result.coverage.prepared, 2)
        self.assertTrue(all("entry" not in token for token in result.snapshot["segments"][0]["tokens"]))

    def test_remaining_space_restores_alternatives_in_original_order_without_truncating_senses(self):
        full = prepared(("猫犬",), parse=pinned, identify=alternatives)
        expected = only_primary(full.snapshot)
        expected["segments"][0]["results"][0]["candidates"] = full.snapshot["segments"][0]["results"][0]["candidates"][:2]
        result = prepared(("猫犬",), parse=pinned, identify=alternatives,
                          budget=offline.Budget(max_bytes=len(offline._json(expected))))
        self.assertEqual(result.snapshot, expected)
        self.assertEqual(result.coverage.prepared, 2)
        self.assertEqual(result.issues, ("byte_limit",))

    def test_one_byte_short_does_not_truncate_or_replace_the_later_primary(self):
        full = prepared(("猫犬",), parse=pinned, identify=alternatives)
        limit = len(offline._json(only_primary(full.snapshot))) - 1
        result = prepared(("猫犬",), parse=pinned, identify=alternatives,
                          budget=offline.Budget(max_bytes=limit))
        self.assertLessEqual(result.bytes_used, limit)
        self.assertEqual(result.coverage.prepared, 1)
        self.assertIn(full.snapshot["segments"][0]["results"][0]["candidates"][1],
                      result.snapshot["segments"][0]["results"][0]["candidates"])
        self.assertEqual(result.snapshot["segments"][0]["tokens"], full.snapshot["segments"][0]["tokens"])

    def test_complete_results_retain_order_and_every_candidate(self):
        result = prepared(("猫犬",), parse=pinned, identify=alternatives)
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
