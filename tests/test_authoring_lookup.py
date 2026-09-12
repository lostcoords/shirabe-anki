import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import support
from offline_fixture import candidate

from shirabe_addon.authoring import Lookup
from shirabe_addon.authoring_batch import LookupIdentity
from shirabe_addon.authoring_lookup import candidates_from_response, resolver_from_config


class NativeLookup(unittest.TestCase):
    def test_no_match_is_a_normal_empty_result(self):
        self.assertEqual(candidates_from_response({"words": [None]}, surface="未知"), ())

    def test_preferences_order_languages_and_dictionaries_and_keep_monolingual_senses(self):
        row = candidate()
        row["entries"][0]["order"] = 2
        row["entries"].append({"order": 1, "dictionary": "kokugo", "sourceId": "cat", "senses": [
            {"position": 0, "definitions": [{"lang": "ja", "text": "ネコ科の動物。"}]}]})
        result = candidates_from_response({"words": [{"candidates": [row]}]}, surface="猫", languages=("es", "en"))
        self.assertEqual(result[0].meanings, ("ネコ科の動物。", "gato; cat"))
        self.assertEqual(result[0].sense_refs, ("kokugo#0", "jmdict#3"))

    def test_pinned_identity_uses_word_detail_instead_of_candidate_window(self):
        requests = []

        class Backend:
            def __init__(self, *args, **kwargs):
                pass
            def _access(self):
                return "valid"
            def me(self):
                requests.append("me")
                return {"preferences": {"languages": ["es", "en"]}}
            def _request(self, path):
                requests.append(path)
                return candidate(source_id="pinned")
            def identify(self, token):
                raise AssertionError("Pinned entry must not depend on identify candidates")

        with patch("shirabe_addon.offline.DictionaryBackend", Backend):
            resolve = resolver_from_config({})
            result = resolve(LookupIdentity("猫", "ねこ", "jmdict:pinned"))
            self.assertEqual(result[0].entry, "jmdict:pinned")
            self.assertEqual(result[0].meanings, ("gato; cat",))
            resolve(LookupIdentity("猫", "ねこ", "jmdict:pinned"))
        self.assertEqual(requests.count("me"), 1)
        self.assertIn("entry=jmdict%3Apinned", requests[1])

    def test_server_opaque_entry_contract_reaches_the_pinned_detail_unchanged(self):
        with patch("shirabe_addon.offline.DictionaryBackend") as backend:
            backend.return_value.me.return_value = {"preferences": {"languages": ["en"]}}
            resolve = resolver_from_config({})
            for source_id in ("word:cat-1", "../猫?part=Ａ&sense=2#noun "):
                with self.subTest(source_id=source_id):
                    backend.return_value._request.return_value = candidate(source_id=source_id)
                    result = resolve(LookupIdentity("old-spelling", entry="jmdict:" + source_id))
                    path = backend.return_value._request.call_args.args[0]
                    self.assertEqual(urlsplit(path).path, "/words/old-spelling")
                    self.assertEqual(parse_qs(urlsplit(path).query)["entry"], ["jmdict:" + source_id])
                    self.assertEqual(result[0].entry, "jmdict:" + source_id)
                    self.assertEqual(parse_qs(urlsplit(result[0].source).query)["entry"], [result[0].entry])
            backend.return_value.identify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
