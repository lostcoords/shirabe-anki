import copy
import json
import unittest

import support
from offline_fixture import candidate, identify, parse
from shirabe_addon import offline, offline_ui


PREFERENCES = {"stackFingerprint": "v1", "dictionaries": ["jmdict:en"], "languages": ["en"], "uiLocale": "en"}


class Backend:
    def __init__(self):
        self.queries = []
        self.answer = identify

    parse = staticmethod(parse)

    def check_access(self):
        pass

    def identify(self, query):
        self.queries.append(copy.deepcopy(query))
        return self.answer(query)


def batch(backend, texts, **options):
    notes = [offline_ui.CapturedNote(i, "fixture", (), None, None) for i in range(len(texts))]
    return offline_ui.prepare_batch(notes, dict(enumerate(texts)), backend,
                                    preferences=PREFERENCES, approved={"jmdict"}, **options)


class OfflineLookupReuse(unittest.TestCase):
    def test_same_query_is_requested_once_across_tokens_and_selected_notes(self):
        backend = Backend()
        result = batch(backend, [["猫猫"], ["猫犬"], ["猫"]])
        self.assertEqual(len(backend.queries), 2)
        self.assertEqual([query["lemma"] for query in backend.queries], ["猫", "犬"])
        self.assertEqual(sum(item.requests for _, item in result.rows), 5)
        for captured, item in result.rows:
            original = offline.prepare_snapshot([segment["text"] for segment in item.snapshot["segments"]],
                                                parse=parse, identify=identify, allowed_dictionaries={"jmdict"},
                                                content_version=item.snapshot["contentVersion"],
                                                preferences=item.snapshot["preferences"], now=lambda: item.snapshot["createdAt"] / 1000)
            self.assertEqual(item.snapshot, original.snapshot)
        first = result.rows[0][1].snapshot["segments"][0]["results"]
        first[0]["candidates"][0]["entries"][0]["senses"][0]["definitions"][0]["text"] = "changed"
        self.assertNotEqual(first[0]["candidates"], first[1]["candidates"])

    def test_repeated_words_do_not_spend_the_request_budget_on_cached_results(self):
        backend = Backend()
        result = batch(backend, [["猫" * 70]])
        item = result.rows[0][1]
        self.assertTrue(item.complete, item.issues)
        self.assertEqual(item.coverage.prepared, 70)
        self.assertEqual(item.requests, 2)
        self.assertEqual(len(backend.queries), 1)

    def test_a_new_batch_does_not_reuse_an_old_backend_response(self):
        backend = Backend()
        first = batch(backend, [["猫"]]).rows[0][1]
        updated = candidate()
        updated["entries"][0]["senses"][0]["definitions"][0]["text"] = "updated definition"
        backend.answer = lambda _: {"words": [{"candidates": [updated]}]}
        second = batch(backend, [["猫"]]).rows[0][1]
        self.assertEqual(len(backend.queries), 2)
        self.assertNotEqual(first.snapshot["segments"], second.snapshot["segments"])

    def test_pinned_entries_remain_distinct(self):
        backend = Backend()
        proper_name = candidate(source_id="2")
        proper_name["name"] = True
        backend.answer = lambda _: {"words": [{"candidates": [candidate(), proper_name]}]}
        def parsed(text):
            answer = parse(text)
            for index, token in enumerate(answer["tokens"]):
                token["entry"] = {"dictionary": "jmdict", "sourceId": str(index + 1)}
            return answer
        backend.parse = parsed
        item = batch(backend, [["猫猫"]]).rows[0][1]
        self.assertTrue(item.complete, item.issues)
        self.assertEqual(len(backend.queries), 2)
        first, second = item.snapshot["segments"][0]["results"]
        self.assertEqual([c["sourceId"] for c in first["candidates"]], ["1"])
        self.assertEqual([c["sourceId"] for c in second["candidates"]], ["1", "2"])

    def test_lemma_surface_reading_and_part_of_speech_are_all_part_of_the_query(self):
        for field, value in (("lemma", "ねこ"), ("surface", "犬"), ("reading", "ビョウ"), ("pos", "verb")):
            with self.subTest(field=field):
                backend = Backend()
                def parsed(text):
                    answer = parse(text)
                    for token in answer["tokens"]:
                        token.update(lemma="猫", reading="ネコ", pos="noun")
                    answer["tokens"][1][field] = value
                    return answer
                backend.parse = parsed
                result = batch(backend, [["猫犬" if field == "surface" else "猫猫"]])
                self.assertTrue(result.rows[0][1].complete)
                self.assertEqual(len(backend.queries), 2)
                self.assertNotEqual(backend.queries[0][field], backend.queries[1][field])

    def test_failures_and_partial_candidates_are_retried_not_frozen(self):
        for failed in (None, {"words": [{"candidates": [candidate(), {"dictionary": "unapproved"}]}]}):
            with self.subTest(failed=failed):
                backend = Backend()
                def answer(query):
                    if len(backend.queries) == 1:
                        if failed is None:
                            raise OSError("unavailable")
                        return failed
                    return identify(query)
                backend.answer = answer
                result = batch(backend, [["猫"], ["猫"]])
                self.assertEqual(len(backend.queries), 2)
                self.assertTrue(result.rows[-1][1].complete)

    def test_cancellation_before_a_cached_lookup_does_not_prepare_the_next_note(self):
        backend = Backend()
        stopped = False
        calls = 0
        def parsed(text):
            nonlocal calls, stopped
            calls += 1
            stopped = calls == 2
            return parse(text)
        backend.parse = parsed
        result = batch(backend, [["猫"], ["猫"]], cancelled=lambda: stopped)
        self.assertTrue(result.stopped)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(len(backend.queries), 1)

    def test_scope_changes_cannot_reuse_dictionary_results(self):
        for changed in ("version", "allowlist", "preferences"):
            with self.subTest(changed=changed):
                source = Backend()
                cache = offline.LookupCache(source)
                options = {"parse": source.parse, "identify": source.identify, "content_version": "v1",
                           "allowed_dictionaries": {"jmdict"}, "preferences": {"languages": ["en"], "dictionaryReveal": {}}, "lookup_cache": cache}
                first = offline.prepare_snapshot(["猫"], **options)
                self.assertTrue(first.complete)
                self.assertTrue(offline.prepare_snapshot(["猫"], **options).complete)
                self.assertEqual(len(source.queries), 1)
                if changed == "version":
                    options["content_version"] = "v2"
                elif changed == "allowlist":
                    options["allowed_dictionaries"] = {"jmdict", "uploaded"}
                else:
                    options["preferences"]["languages"] = ["es"]
                self.assertTrue(offline.prepare_snapshot(["猫"], **options).complete)
                self.assertEqual(len(source.queries), 2)

    def test_a_cache_cannot_be_rebound_to_a_different_backends_access_guard(self):
        for primed in (False, True):
            with self.subTest(primed=primed):
                first, second = Backend(), Backend()
                cache = offline.LookupCache(first)
                options = dict(parse=parse, content_version="v1", allowed_dictionaries={"jmdict"}, lookup_cache=cache)
                if primed:
                    self.assertTrue(offline.prepare_snapshot(["猫"], identify=first.identify, **options).complete)
                with self.assertRaises(ValueError):
                    offline.prepare_snapshot(["猫"], identify=second.identify, **options)
                self.assertEqual(second.queries, [])

    def test_cache_entry_limit_evicts_the_least_recently_used_query(self):
        source = Backend()
        cache = offline.LookupCache(source, max_entries=2)
        for word in ("猫", "犬", "猫", "鳥", "犬"):
            item = offline.prepare_snapshot([word], parse=source.parse, identify=source.identify, content_version="v1",
                                            allowed_dictionaries={"jmdict"}, lookup_cache=cache)
            self.assertTrue(item.complete)
        self.assertEqual([q["lemma"] for q in source.queries], ["猫", "犬", "鳥", "犬"])

    def test_cache_byte_limit_includes_queries_and_bypasses_oversized_entries(self):
        one = offline.prepare_snapshot(["猫"], parse=parse, identify=identify, content_version="v1", allowed_dictionaries={"jmdict"})
        candidates = one.snapshot["segments"][0]["results"][0]["candidates"]
        size = len(offline._json(candidates)) + len(offline._json([{"lemma": "猫", "surface": "猫", "reading": "ネコ"}, None]))
        for maximum in (size - 1, size * 2 - 1):
            with self.subTest(maximum=maximum):
                source = Backend()
                cache = offline.LookupCache(source, max_bytes=maximum)
                for word in ("猫", "犬", "猫"):
                    item = offline.prepare_snapshot([word], parse=source.parse, identify=source.identify, content_version="v1",
                                                    allowed_dictionaries={"jmdict"}, lookup_cache=cache)
                    self.assertTrue(item.complete)
                    self.assertLessEqual(cache.bytes_used, maximum)
                self.assertEqual(len(source.queries), 3)

    def test_cached_data_excludes_private_fields_and_optional_enrichment(self):
        source = Backend()
        cache = offline.LookupCache(source)
        offline.prepare_snapshot(["猫"], parse=source.parse, identify=source.identify, content_version="v1",
                                 allowed_dictionaries={"jmdict"}, lookup_cache=cache)
        retained = repr(cache.__dict__)
        for field in ("private.test", "secret", "cardId", "study", "audioUrl", "examples"):
            self.assertNotIn(field, retained)

    def test_expired_or_invalidated_access_cannot_serve_a_cached_result(self):
        for changed in ("expiry", "signout"):
            with self.subTest(changed=changed):
                credentials = json.dumps({"access": "fixture", "expiresAt": 2000})
                backend = offline.DictionaryBackend("https://fixture.invalid", credentials, now=lambda: 1)
                source = Backend()
                source.check_access = backend.check_access
                cache = offline.LookupCache(source)
                options = dict(parse=source.parse, identify=source.identify, content_version="v1",
                               allowed_dictionaries={"jmdict"}, lookup_cache=cache)
                self.assertTrue(offline.prepare_snapshot(["猫"], **options).complete)
                if changed == "expiry":
                    backend._now = lambda: 2
                else:
                    offline.invalidate_preparations()
                item = offline.prepare_snapshot(["猫"], **options)
                self.assertEqual(item.coverage.prepared, 0)
                self.assertIn("reconnect_required", item.issues)
                self.assertEqual(len(source.queries), 1)

    def test_connection_change_discards_the_queued_batch(self):
        source = Backend()
        signed_out = False
        def check():
            if signed_out:
                raise offline.SnapshotError("Signed out", code="reconnect_required")
        def progress(*_args):
            nonlocal signed_out
            signed_out = True
        source.check_access = check
        with self.assertRaises(offline.SnapshotError):
            batch(source, [["猫"], ["猫"]], progress=progress)
        self.assertEqual(len(source.queries), 1)

    def test_cache_bounds_reject_unbounded_or_boolean_limits(self):
        for options in ({"max_bytes": 0}, {"max_bytes": True}, {"max_bytes": offline.MAX_LOOKUP_CACHE_BYTES + 1},
                        {"max_entries": 0}, {"max_entries": True}, {"max_entries": offline.MAX_LOOKUP_CACHE_ENTRIES + 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                offline.LookupCache(Backend(), **options)

    def test_enriching_a_previous_result_does_not_change_another_notes_cached_definition(self):
        source = Backend()
        cache = offline.LookupCache(source)
        options = dict(parse=source.parse, identify=source.identify, content_version="v1",
                       allowed_dictionaries={"jmdict"}, lookup_cache=cache)
        first = offline.prepare_snapshot(["猫"], **options)
        word = first.snapshot["segments"][0]["results"][0]["candidates"][0]
        word["examples"] = [{"japanese": "first note only"}]
        word["entries"][0]["senses"][0]["definitions"][0]["text"] = "mutated"
        second = offline.prepare_snapshot(["猫"], **options)
        original = offline.prepare_snapshot(["猫"], parse=source.parse, identify=identify, content_version="v1",
                                            allowed_dictionaries={"jmdict"}, now=lambda: second.snapshot["createdAt"] / 1000)
        self.assertEqual(second.snapshot, original.snapshot)
        self.assertEqual(len(source.queries), 1)

    def test_a_previous_note_fallback_is_not_reused_by_another_note(self):
        source = Backend()
        previous = offline.prepare_snapshot(["猫"], parse=parse, identify=identify, content_version="v1", allowed_dictionaries={"jmdict"})
        def answer(query):
            if len(source.queries) == 1:
                raise OSError("unavailable")
            return identify(query)
        source.answer = answer
        cache = offline.LookupCache(source)
        options = dict(parse=source.parse, identify=source.identify, content_version="v1",
                       allowed_dictionaries={"jmdict"}, lookup_cache=cache)
        first = offline.prepare_snapshot(["猫"], previous=previous.snapshot, **options)
        self.assertEqual(first.coverage.prepared, 1)
        self.assertFalse(first.complete)
        self.assertTrue(offline.prepare_snapshot(["猫"], **options).complete)
        self.assertEqual(len(source.queries), 2)


if __name__ == "__main__":
    unittest.main()
