from concurrent.futures import ThreadPoolExecutor
import json
import unittest

import support
from shirabe_addon import offline


VALUES = [None, True, False, 0, -1, 2**53 - 1, 1.25, -0.0,
          "猫😀\"\\\n\t\b\f\r\x00\u2028", [], {},
          {"z": ["日本語", False], "a": {"意味": "猫"}}]


class SnapshotJsonTests(unittest.TestCase):
    def test_encoding_matches_the_strict_standard_encoder_for_both_key_orders(self):
        for value in VALUES:
            for sorted_keys in (False, True):
                with self.subTest(value=value, sorted_keys=sorted_keys):
                    expected = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                          allow_nan=False, sort_keys=sorted_keys).encode("utf-8")
                    self.assertEqual(offline._json(value, sort_keys=sorted_keys), expected)

    def test_byte_guards_keep_exact_utf8_boundaries(self):
        for value in VALUES:
            length = len(offline._json(value))
            with self.subTest(value=value):
                self.assertEqual(offline._bounded(value, length), 0)
                with self.assertRaises(offline.SnapshotError):
                    offline._bounded(value, length - 1)

    def test_invalid_numbers_cycles_and_surrogates_remain_rejected(self):
        cycle = []
        cycle.append(cycle)
        for value in (float("nan"), float("inf"), -float("inf"), "\ud800", cycle):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                offline._json(value)
        nested = 0
        for _ in range(18):
            nested = [nested]
        with self.assertRaises(offline.SnapshotError):
            offline._bounded(nested, 1024)

    def test_parallel_preparation_keeps_encoder_configuration_isolated(self):
        cases = [(value, sort_keys) for _ in range(30) for value in VALUES for sort_keys in (False, True)]
        expected = [json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                               sort_keys=sort_keys).encode("utf-8") for value, sort_keys in cases]
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(lambda pair: offline._json(pair[0], sort_keys=pair[1]), cases))
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
