import copy
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

import support
from check_against_anki import anki_python
from offline_fixture import candidate, identify, inflected, parse, prepared
from shirabe_addon import offline


class PreparationTests(unittest.TestCase):
    def test_parser_inflection_labels_and_base_survive_as_flat_bounded_token_fields(self):
        result = inflected()
        self.assertTrue(result.complete)
        first, second = [segment["tokens"][0] for segment in result.snapshot["segments"]]
        self.assertEqual(first["inflection"], ["past", "polite"])
        self.assertEqual(second["inflection"], ["negative"])
        self.assertEqual(first["inflectionBase"], "食べる")
        self.assertEqual(second["inflectionBase"], "食べる")
        self.assertNotIn("secret", json.dumps(result.snapshot))
        self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)

    def test_malformed_or_oversized_inflections_do_not_disable_other_prepared_segments(self):
        for field, value in (("inflection", ["past"] * 17), ("inflection", ["x" * 129]),
                             ("inflection", [None]), ("inflection", "past"), ("inflectionBase", "x" * 513)):
            with self.subTest(field=field, value=str(value)[:50]):
                result = inflected()
                result.snapshot["segments"][0]["tokens"][0][field] = value
                clean = offline.validate_snapshot(result.snapshot)
                self.assertFalse(clean["complete"])
                self.assertEqual([segment["text"] for segment in clean["segments"]], ["食べない"])

    def test_parser_inflection_bounds_precede_array_processing_and_missing_metadata_remains_optional(self):
        class OversizedForms(list):
            def __iter__(self):
                raise AssertionError("Oversized forms must not be walked")
        def parsed(text):
            result = parse(text)
            result["results"][0]["tokens"][0]["inflection"] = {
                "forms": OversizedForms([{"code": "past", "label": "past"}] * 17), "base": "猫"}
            return result
        limited = prepared(("猫", "犬"), parse=lambda text: parsed(text) if text == "猫" else parse(text))
        self.assertFalse(limited.complete)
        self.assertEqual(limited.issues, ("entry_limit",))
        self.assertEqual([segment["text"] for segment in limited.snapshot["segments"]], ["犬"])
        self.assertNotIn("inflection", limited.snapshot["segments"][0]["tokens"][0])


    def test_snapshot_field_is_a_bounded_safe_media_reference(self):
        name = "_shirabe_snapshot_v1_" + "a" * 64 + ".json"
        field = offline.snapshot_field(name)
        self.assertEqual(field, f'{name}<img src="{name}" style="display:none">')
        self.assertEqual(offline.snapshot_filename(field), name)
        self.assertEqual(offline.snapshot_filename(name), name)
        self.assertEqual(offline.snapshot_filename(f" {name}<img style='display: none;' src='{name}' /> "), name)
        for value in (None, {}, "", "x" * 1000000, name + "<img", "../" + name,
                      field.replace('display:none', 'display:block'),
                      field.replace('<img', '<img onerror="alert(1)"'),
                      field.replace('src="', 'src="https://example.org/'),
                      field.replace('src="', 'src="duplicate" src="'),
                      field.replace('src="' + name, 'src="' + name.replace("a" * 64, "b" * 64)),
                      field + field, field + "<script>alert(1)</script>"):
            with self.subTest(value=str(value)[:100]):
                self.assertIsNone(offline.snapshot_filename(value))
        for value in (None, field, "../" + name, 'bad" onerror="alert(1)'):
            with self.assertRaises(offline.SnapshotError):
                offline.snapshot_field(value)

    def test_exact_unicode_offsets_candidates_and_private_data_removed(self):
        result = prepared()
        self.assertTrue(result.complete)
        segment = result.snapshot["segments"][0]
        self.assertEqual([t["start"] for t in segment["tokens"]], [0, 2, 3, 4])
        self.assertEqual(segment["contentHash"], hashlib.sha256("😀猫は猫".encode()).hexdigest())
        self.assertEqual(result.coverage.tokens, 3)
        self.assertEqual(result.coverage.prepared, 3)
        self.assertEqual(result.requests, 5)
        wire = json.dumps(result.snapshot)
        for private in ("secret", "study", "audioUrl", "cardId", "state", "decks", "credentials"):
            self.assertNotIn(private, wire)
        self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)

    def test_safe_preferences_and_export_notices_survive_without_account_details(self):
        result = prepared(preferences={"languages": ["es", "en"], "dictionaryReveal": {"jmdict:es": "show", "jmdict:en": "hover"}, "name": "private"},
                          sources=[{"slug": "jmdict", "name": "JMdict", "entryCount": 900000,
                                    "source": {"name": "EDRDG", "license": "CC BY-SA 4.0", "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/"}}])
        self.assertEqual(result.snapshot["preferences"]["languages"], ["es", "en"])
        self.assertEqual(result.snapshot["preferences"]["dictionaryReveal"], {"jmdict:es": "show", "jmdict:en": "hover"})
        self.assertEqual(result.snapshot["sources"][0]["source"]["license"], "CC BY-SA 4.0")
        self.assertNotIn("private", json.dumps(result.snapshot))
        self.assertNotIn("entryCount", result.snapshot["sources"][0])
        self.assertEqual(offline.validate_snapshot(result.snapshot), result.snapshot)
        invalid = copy.deepcopy(result.snapshot)
        invalid["preferences"]["dictionaryReveal"]["jmdict:es"] = "always"
        self.assertIsNone(offline.validate_snapshot(invalid))

    def test_oversized_uploaded_dictionary_cannot_expand_a_card(self):
        huge = candidate()
        huge["entries"][0]["senses"] *= 100_000
        result = prepared(("猫",), identify=lambda _: {"words": [{"candidates": [huge]}]})
        self.assertFalse(result.complete)
        self.assertEqual(result.coverage.prepared, 0)
        self.assertLess(result.bytes_used, 1024)
        self.assertEqual(result.issues, ("entry_limit",))

    def test_unapproved_huge_overlay_is_filtered_before_payload_processing(self):
        small = candidate()
        overlay = copy.deepcopy(small["entries"][0])
        overlay["dictionary"] = "private-upload"
        overlay["senses"] = [{"private": "secret" * 1000000}]
        small["entries"].insert(0, overlay)
        result = prepared(("猫",), identify=lambda _: {"words": [{"candidates": [small]}]})
        self.assertEqual(result.coverage.prepared, 1)
        entries = result.snapshot["segments"][0]["results"][0]["candidates"][0]["entries"]
        self.assertEqual([entry["dictionary"] for entry in entries], ["jmdict"])
        self.assertEqual(result.issues, ("dictionary_not_approved",))
        self.assertFalse(result.complete)
        self.assertNotIn("secret", json.dumps(result.snapshot))
        self.assertLess(result.bytes_used, offline.MAX_ENTRY_BYTES)

    def test_one_unapproved_or_oversized_candidate_does_not_discard_usable_candidates(self):
        unapproved = candidate(source_id="private")
        unapproved["dictionary"] = "private-upload"
        unapproved["entries"][0]["senses"] *= 100000
        oversized = candidate(source_id="too-large")
        oversized["entries"][0]["senses"] *= 100000
        for bad, issue in ((unapproved, "dictionary_not_approved"), (oversized, "entry_limit"), (None, "lookup_unavailable")):
            for candidates in ([bad, candidate()], [candidate(), bad], [bad] * 4 + [candidate()]):
                with self.subTest(issue=issue, count=len(candidates)):
                    result = prepared(("猫",), identify=lambda _: {"words": [{"candidates": candidates}]})
                    self.assertEqual(result.coverage.prepared, 1)
                    kept = result.snapshot["segments"][0]["results"][0]["candidates"]
                    self.assertEqual([item["sourceId"] for item in kept], ["1"])
                    self.assertIn(issue, result.issues)
                    self.assertFalse(result.complete)
                    self.assertFalse(result.snapshot["complete"])

    def test_oversized_approved_overlay_keeps_small_entries_but_reports_partial_coverage(self):
        small = candidate()
        overlay = copy.deepcopy(small["entries"][0])
        overlay["dictionary"] = "uploaded"
        overlay["senses"] *= 100000
        small["entries"].insert(0, overlay)
        result = prepared(("猫",), allowed_dictionaries={"jmdict", "uploaded"}, identify=lambda _: {"words": [{"candidates": [small]}]})
        self.assertEqual(result.coverage.prepared, 1)
        self.assertEqual(result.issues, ("entry_limit",))
        self.assertFalse(result.complete)
        self.assertEqual([e["dictionary"] for e in result.snapshot["segments"][0]["results"][0]["candidates"][0]["entries"]], ["jmdict"])

    def test_loading_partial_candidates_preserves_approved_results_and_marks_incomplete(self):
        snapshot = prepared(("猫",)).snapshot
        result = snapshot["segments"][0]["results"][0]
        result["candidates"].insert(0, {"dictionary": "unapproved", "entries": "large" * 10000})
        clean = offline.validate_snapshot(snapshot, allowed_dictionaries={"jmdict"})
        self.assertEqual(offline.coverage(clean).prepared, 1)
        self.assertFalse(clean["complete"])
        self.assertEqual([c["dictionary"] for c in clean["segments"][0]["results"][0]["candidates"]], ["jmdict"])

    def test_filtered_overlay_contents_are_never_walked(self):
        class NeverRead(list):
            def __iter__(self):
                raise AssertionError("Unapproved dictionary contents were traversed")
        small = candidate()
        small["entries"].append({"dictionary": "unapproved", "senses": NeverRead(["secret"])})
        self.assertIsNotNone(offline._candidate(small, {"jmdict"}, []))


    def test_entry_bytes_cap_and_malformed_candidate_leave_other_tokens_available(self):
        large = candidate()
        large["entries"][0]["senses"][0]["definitions"] = [{"lang": "en", "text": "a" * 4000}] * 7
        for bad in (large, {"headword": "猫"}, None):
            result = prepared(("猫犬",), identify=lambda t: {"words": [{"candidates": [bad]}]} if t["lemma"] == "猫" else identify(t))
            self.assertFalse(result.complete)
            self.assertEqual(result.coverage.prepared, 1)
            self.assertLessEqual(result.bytes_used, offline.MAX_SNAPSHOT_BYTES)

    def test_candidate_request_text_segment_and_snapshot_budgets(self):
        many = prepared(("猫",), identify=lambda _: {"words": [{"candidates": [candidate(source_id=str(i)) for i in range(10)]}]})
        self.assertEqual(len(many.snapshot["segments"][0]["results"][0]["candidates"]), 3)
        self.assertIn("candidate_limit", many.issues)
        limited = prepared(budget=offline.Budget(max_requests=2))
        self.assertEqual(limited.requests, 2)
        self.assertFalse(limited.complete)
        self.assertLessEqual(prepared(budget=offline.Budget(max_bytes=512)).bytes_used, 512)
        self.assertEqual(prepared(("猫" * 4097,)).requests, 0)
        self.assertEqual(prepared((str(i) for i in range(1000))).coverage.segments, 16)
        with self.assertRaises(ValueError):
            offline.Budget(max_bytes=offline.MAX_SNAPSHOT_BYTES + 1)

    def test_no_redistribution_allowlist_produces_no_identify_requests(self):
        def forbidden(_):
            self.fail("No dictionary request without export permission")
        result = prepared(("猫",), allowed_dictionaries=set(), identify=forbidden)
        self.assertEqual(result.requests, 1)
        self.assertEqual(result.coverage.prepared, 0)

    def test_offsets_hashes_versions_and_partial_payloads(self):
        original = prepared().snapshot
        for mutate in (
            lambda s: s.update(contentHash="0" * 64),
            lambda s: s["tokens"][1].update(start=1),
            lambda s: s["tokens"][-1].update(length=100),
            lambda s: s["tokens"][1].update(start=True),
            lambda s: s.update(tokens=None),
        ):
            value = copy.deepcopy(original)
            mutate(value["segments"][0])
            clean = offline.validate_snapshot(value)
            self.assertEqual([s["text"] for s in clean["segments"]], ["犬"])
            self.assertFalse(clean["complete"])
        for malformed in (None, "{", " " * (offline.MAX_SNAPSHOT_BYTES + 1), {**original, "formatVersion": 2}):
            self.assertIsNone(offline.validate_snapshot(malformed))
        partial = copy.deepcopy(original)
        partial["segments"][0]["results"][0]["candidates"] = [None]
        self.assertFalse(offline.validate_snapshot(partial)["complete"])

    def test_cancel_and_refresh_keep_good_results_but_never_reuse_changed_text_or_version(self):
        previous = prepared().snapshot
        def failed(_):
            raise OSError("could contain secret text")
        partial = prepared(previous=previous, identify=failed)
        self.assertEqual(partial.coverage.prepared, 3)
        self.assertFalse(partial.complete)
        self.assertNotIn("secret", repr(partial))
        self.assertEqual(prepared(("鳥",), previous=previous, identify=failed).coverage.prepared, 0)
        self.assertEqual(prepared(previous=previous, content_version="v2", identify=failed).coverage.prepared, 0)
        cancelled = prepared(cancelled=lambda: True)
        self.assertEqual(cancelled.requests, 0)
        self.assertFalse(cancelled.complete)
        count = 0
        def cancelling(token):
            nonlocal count
            count += 1
            return identify(token)
        mid = prepared(identify=cancelling, cancelled=lambda: count == 1)
        self.assertEqual(mid.coverage.prepared, 1)
        self.assertIn("cancelled", mid.issues)

    @unittest.skipUnless(anki_python(), "Installed Anki Python required")
    def test_supported_media_operations_with_a_real_anki_collection(self):
        probe = r'''
import hashlib, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import support
from offline_fixture import prepared
from shirabe_addon.offline import *
from anki.collection import Collection
with tempfile.TemporaryDirectory(prefix="shirabe-offline-anki-") as directory:
    col = Collection(str(Path(directory) / "collection.anki2"))
    try:
        complete = prepared()
        first = persist_snapshot(col.media, complete)
        assert MEDIA_NAME.fullmatch(first.filename)
        assert first.bytes_used == (Path(col.media.dir()) / first.filename).stat().st_size
        assert first.bytes_used < complete.bytes_used
        assert read_snapshot(col.media, first.filename) == complete.snapshot
        assert persist_snapshot(col.media, complete).bytes_used == 0
        usage = snapshot_storage(col.media)
        assert usage.bytes_used == first.bytes_used and usage.files == 1
        next_preparation = prepared(("犬",))
        try:
            persist_snapshot(col.media, next_preparation, total_budget_bytes=first.bytes_used + len(snapshot_file(next_preparation.snapshot)[1]) - 1)
            raise AssertionError("collection storage budget bypassed")
        except SnapshotError as error:
            assert error.code == "storage_limit"
        assert snapshot_storage(col.media) == usage
        partial = prepared(budget=Budget(max_requests=1))
        kept = persist_snapshot(col.media, partial, previous_name=first.filename)
        assert kept.filename == first.filename and kept.kept_previous and kept.bytes_used == 0
        try:
            persist_snapshot(col.media, partial, remaining_bytes=0)
            raise AssertionError("budget bypassed")
        except SnapshotError:
            pass
        partial_stored = persist_snapshot(col.media, partial)
        assert not read_snapshot(col.media, partial_stored.filename)["complete"]
        corrupt = "_shirabe_snapshot_v1_" + "0" * 64 + ".json"
        col.media.write_data(corrupt, b"{}")
        assert snapshot_storage(col.media).files == 3
        unrelated = col.media.write_data("photo.jpg", b"unrelated")
        assert read_snapshot(col.media, corrupt) is None
        assert read_snapshot(col.media, "../collection.anki2") is None
        removed = cleanup_snapshots(col.media, [first.filename, partial_stored.filename, corrupt, unrelated], [first.filename])
        assert removed == [partial_stored.filename]
        assert col.media.have(first.filename) and col.media.have(unrelated) and col.media.have(corrupt)
        assert not col.media.have(partial_stored.filename)
        col.media.restore_trash()
        assert col.media.have(partial_stored.filename)
        print(json.dumps({"bytes": first.bytes_used, "coverage": first.coverage.prepared, "restored": True}))
    finally:
        col.close()
'''
        result = subprocess.run([str(anki_python()), "-c", probe, str(Path(__file__).parent)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"restored": true', result.stdout)


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.mode = "normal"
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.calls.append((self.path, body, dict(self.headers)))
                if cls.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/stolen")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                if cls.mode == "large":
                    data = b" " * (offline.MAX_RESPONSE_BYTES + 2)
                elif urlsplit(self.path).path == "/api/v1/parse":
                    parsed = parse(body["texts"][0])
                    data = json.dumps(parsed).encode()
                else:
                    found = identify(body["tokens"][0])
                    if body.get("include") == ["pitch"]:
                        found["words"][0]["candidates"][0]["pitch"][0]["audioUrl"] = \
                            "https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a"
                    if cls.mode == "private_overlay" and body.get("dictionaries") != ["jmdict"]:
                        found["words"][0]["candidates"][0]["entries"].append({"dictionary": "uploaded", "senses": "private" * offline.MAX_RESPONSE_BYTES})
                    data = json.dumps({**found, "stackFingerprint": "dictionary-v1-en-es"}).encode()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                cls.calls.append((self.path, None, dict(self.headers)))
                if cls.mode == "denied":
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == "/api/v1/me":
                    data = {"user": {"name": "private"}, "credential": {"scopes": ["user:dictionary:read", "user:account:read"]}}
                elif self.path == "/api/v1/preferences":
                    data = {"preferences": {"uiLocale": "en"}}
                elif self.path == "/api/v1/dictionary-stack":
                    data = {"dictionaryStack": {"fingerprint": "dictionary-v1-en-es", "sources": [
                        {"dictionary": "uploaded", "languages": ["en"], "reveal": "hover"},
                        {"dictionary": "jmdict", "languages": ["en"], "reveal": "show"},
                    ]}}
                elif self.path == "/api/v1/dictionaries":
                    data = {"dictionaries": [{"slug": "uploaded", "name": "Huge custom dictionary", "kind": "definition",
                                              "languages": ["en"], "visibility": "personal", "entryCount": 10_000_000,
                                              "capabilities": ["search", "definitions", "offline"], "attribution": None}]}
                    if cls.mode == "missing_count":
                        del data["dictionaries"][0]["entryCount"]
                    elif cls.mode == "null_count":
                        data["dictionaries"][0]["entryCount"] = None
                elif urlsplit(self.path).path.startswith("/api/v1/words/"):
                    data = candidate()
                    data["pitch"][0]["audioUrl"] = "https://cdn.shirabe.org/audio/words/" + "a" * 64 + ".m4a"
                    if parse_qs(urlsplit(self.path).query).get("dictionaries") != ["jmdict"]:
                        data["entries"].append({"dictionary": "uploaded", "senses": "private" * offline.MAX_RESPONSE_BYTES})
                else:
                    raise AssertionError("Unexpected metadata route")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).calls.clear()
        type(self).mode = "normal"
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.credentials = json.dumps({"access": "shra_test", "refresh": "shrr_never_sent", "expiresAt": 2_000_000})

    def test_real_http_only_posts_bounded_parse_identify_with_existing_device_token(self):
        backend = offline.DictionaryBackend(self.base, self.credentials, now=lambda: 1000)
        result = prepared(("猫",), parse=backend.parse, identify=backend.identify)
        self.assertTrue(result.complete)
        self.assertEqual([c[0] for c in self.calls], ["/api/v1/parse", "/api/v1/words/identify"])
        for _, body, headers in self.calls:
            self.assertEqual(headers["Authorization"], "Bearer shra_test")
            self.assertEqual(headers["User-Agent"], offline.net.USER_AGENT)
            self.assertNotIn("shrr", json.dumps(body))

    def test_recording_metadata_identify_is_pinned_and_excludes_unselected_uploaded_overlay(self):
        backend = offline.DictionaryBackend(self.base, self.credentials, now=lambda: 1000, dictionaries=["uploaded", "jmdict"])
        recordings = backend.recordings(candidate())
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0].reading, "ねこ")
        self.assertEqual(len(self.calls), 1)
        path, body, headers = self.calls[0]
        self.assertEqual(path, "/api/v1/words/identify")
        self.assertEqual(body, {"tokens": [{"lemma": "猫", "surface": "猫", "reading": "ネコ"}],
                                "include": ["pitch"], "dictionaries": ["jmdict"]})
        self.assertEqual(headers["Authorization"], "Bearer shra_test")
        self.assertNotIn("refresh", str(headers))

    def test_selected_dictionaries_bound_response_and_parser_identity_on_actual_http(self):
        type(self).mode = "private_overlay"
        unscoped = offline.DictionaryBackend(self.base, self.credentials, now=lambda: 1000)
        partial = prepared(("猫",), parse=unscoped.parse, identify=unscoped.identify)
        self.assertFalse(partial.complete)
        self.assertIn("response_limit", partial.issues)
        self.calls.clear()
        scoped = offline.DictionaryBackend(self.base, self.credentials, dictionaries=["jmdict"], now=lambda: 1000)
        result = prepared(("猫",), parse=scoped.parse, identify=scoped.identify)
        self.assertTrue(result.complete)
        self.assertEqual(result.snapshot["segments"][0]["tokens"][0]["entry"], "jmdict:1")
        self.assertNotIn("dictionaries", self.calls[0][1])
        self.assertEqual(self.calls[1][1]["dictionaries"], ["jmdict"])
        self.assertEqual([path for path, _, _ in self.calls], ["/api/v1/parse", "/api/v1/words/identify"])
        self.assertEqual(self.calls[1][1]["include"], ["pitch", "frequency", "furigana", "jlpt", "forms", "notes", "relations"])
        scoped.me()
        scoped.dictionaries()
        self.assertEqual([(path, body) for path, body, _ in self.calls[-3:]],
                         [("/api/v1/preferences", None), ("/api/v1/dictionary-stack", None), ("/api/v1/dictionaries", None)])

    def test_dictionary_selection_is_bounded_copied_and_preserves_order(self):
        selection = ["uploaded", "jmdict"]
        backend = offline.DictionaryBackend(self.base, self.credentials, dictionaries=selection, now=lambda: 1000)
        selection.reverse()
        backend.parse("猫")
        backend.identify({"lemma": "猫", "surface": "猫"})
        self.assertNotIn("dictionaries", self.calls[0][1])
        self.assertEqual(self.calls[1][1]["dictionaries"], ["uploaded", "jmdict"])
        for selection in ([], [""], [" "], "jmdict", ["x"] * 129, ["x" * 129], [None]):
            with self.subTest(selection=selection):
                with self.assertRaises(offline.SnapshotError):
                    offline.DictionaryBackend(self.base, self.credentials, dictionaries=selection, now=lambda: 1000)

    def test_parse_and_identify_both_request_the_saved_label_locale(self):
        backend = offline.DictionaryBackend(self.base, self.credentials, locale="es", dictionaries=["jmdict"], now=lambda: 1000)
        backend.parse("猫")
        backend.identify({"surface": "猫", "lemma": "猫"})
        self.assertEqual([path for path, _, _ in self.calls], ["/api/v1/parse", "/api/v1/words/identify"])
        self.assertTrue(all("locale" not in body for _, body, _ in self.calls))

    def test_size_cap_redirect_and_expired_malformed_credentials(self):
        for mode in ("large", "redirect"):
            type(self).mode = mode
            with self.assertRaises(offline.SnapshotError) as raised:
                offline.DictionaryBackend(self.base, self.credentials, now=lambda: 1000).parse("猫")
            if mode == "large":
                self.assertEqual(raised.exception.code, "response_limit")
        self.assertEqual(len(self.calls), 2)
        for credentials in ("{", "[]", "{}", self.credentials, json.dumps({"access": "a\r\nb", "expiresAt": 9_000_000})):
            with self.assertRaises(offline.SnapshotError):
                offline.DictionaryBackend(self.base, credentials, now=lambda: 3000).parse("猫")
        self.assertEqual(len(self.calls), 2)
        for base in ("https://user:secret@example.test", "https://example.test/api", "http://example.test", "file:///tmp"):
            with self.assertRaises(offline.SnapshotError):
                offline.DictionaryBackend(base, self.credentials)

    def test_nested_bridge_storage_and_metadata_gets_never_download_dictionary_packs(self):
        slots = json.dumps({"shirabe.link": self.credentials, "shirabe.pending": "private"})
        backend = offline.DictionaryBackend(self.base, slots, now=lambda: 1000)
        me = backend.me()
        self.assertEqual(me["preferences"]["stackFingerprint"], "dictionary-v1-en-es")
        self.assertEqual(me["preferences"]["dictionaryReveal"], {"uploaded": "hover", "jmdict": "show"})
        self.assertEqual(me["credential"]["scopes"], ["user:dictionary:read", "user:account:read"])
        self.assertNotIn("user", me)
        dictionaries = backend.dictionaries()["dictionaries"]
        self.assertEqual(dictionaries[0]["entryCount"], 10_000_000)
        self.assertEqual([call[0] for call in self.calls],
                         ["/api/v1/me", "/api/v1/preferences", "/api/v1/dictionary-stack", "/api/v1/dictionaries"])
        type(self).mode = "denied"
        with self.assertRaises(offline.SnapshotError) as raised:
            backend.me()
        self.assertEqual(raised.exception.code, "reconnect_required")

    def test_dictionary_metadata_allows_omitted_or_null_entry_counts(self):
        backend = offline.DictionaryBackend(self.base, self.credentials, now=lambda: 1000)
        for mode in ("missing_count", "null_count"):
            type(self).mode = mode
            dictionary = backend.dictionaries()["dictionaries"][0]
            self.assertIsNone(dictionary.get("entryCount"))
            self.assertEqual(dictionary["slug"], "uploaded")


if __name__ == "__main__":
    unittest.main()
