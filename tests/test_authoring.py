import json
import unittest

import support

from shirabe_addon import authoring
from shirabe_addon.bridge import MAX_MESSAGE_BYTES, Proposal


class Note:
    def __init__(self, values=None, *, note_id=1, model_type=0):
        self.id, self.mid, self.guid, self.mod, self.usn = note_id, 10, "guid", 20, 0
        self.tags = ["mine", "Japanese"]
        self.names = ["Expression", "Reading", "Meaning", "Sentence", "Entry", "Other"]
        self.fields = ["", "", "", "", "", "<b>my formatting</b>[sound:mine.mp3]"]
        self.model_type = model_type
        for name, value in (values or {}).items():
            self[name] = value

    def keys(self):
        return self.names[:]

    def __getitem__(self, name):
        return self.fields[self.names.index(name)]

    def __setitem__(self, name, value):
        self.fields[self.names.index(name)] = value

    def note_type(self):
        return {"id": self.mid, "type": self.model_type, "flds": [{"name": n} for n in self.names],
                "tmpls": [{"qfmt": "{{cloze:Sentence}}" if self.model_type else "{{Expression}}"}]}


def profile(**kwargs):
    return authoring.FieldProfile("Japanese", 10, {
        "expression": "Expression", "reading": "Reading", "meanings": "Meaning",
        "sentence": "Sentence", "entry": "Entry",
    }, **kwargs)


def lookup(**kwargs):
    values = dict(surface="食べました", lemma="食べる", reading="たべる", entry="jmdict:1358280",
                  meanings=("to eat", "to live on"), sentence="昨日は寿司を食べました。")
    return authoring.Lookup(**(values | kwargs))


class Authoring(unittest.TestCase):
    def test_fill_only_preserves_the_exact_user_html_and_unmapped_fields(self):
        note = Note({"Meaning": "<b>my meaning</b>", "Reading": "<br>"})
        preview = authoring.build_preview(note, profile(), lookup())
        after = preview.field_values()
        self.assertEqual(after["Meaning"], "<b>my meaning</b>")
        self.assertEqual(after["Other"], note["Other"])
        self.assertEqual(after["Reading"], "たべる")
        self.assertEqual(after["Entry"], "jmdict:1358280")
        self.assertEqual(note["Expression"], "")

    def test_replace_and_append_need_explicit_field_choices(self):
        note = Note({"Meaning": "<i>my meaning</i>", "Reading": "タベル"})
        preview = authoring.build_preview(note, profile(), lookup(), policies={"meanings": "append"})
        self.assertEqual(preview.field_values()["Meaning"], "<i>my meaning</i><br>to eat<br>to live on")
        self.assertEqual(preview.field_values()["Reading"], "タベル")
        with self.assertRaises(authoring.AuthoringError):
            authoring.build_preview(note, profile(), lookup(), policies={"typo": "replace"})

    def test_selected_meanings_and_inflected_headword_survive(self):
        choices = authoring.NoteChoices(headword="surface", sense_indexes=(1,))
        preview = authoring.build_preview(Note(), profile(), lookup(), choices=choices)
        self.assertEqual(preview.field_values()["Expression"], "食べました")
        self.assertEqual(preview.field_values()["Meaning"], "to live on")

    def test_sentence_note_places_the_selected_sentence_in_the_expression_field(self):
        preview = authoring.build_preview(Note(note_id=0), profile(), lookup(), choices=authoring.NoteChoices(kind="sentence"))
        self.assertEqual(preview.field_values()["Expression"], "昨日は寿司を食べました。")
        self.assertEqual(preview.field_values()["Entry"], "jmdict:1358280")

    def test_untrusted_text_cannot_insert_html_media_or_hidden_clozes(self):
        value = lookup(meanings=("<img src=x onerror=alert(1)>[sound:evil.mp3]{{c1::answer}}",))
        rendered = authoring.build_preview(Note(), profile(), value).field_values()["Meaning"]
        self.assertNotIn("<img", rendered)
        self.assertNotIn("[sound:", rendered)
        self.assertNotIn("{{c1::", rendered)
        self.assertIn("&lt;img", rendered)

    def test_cloze_uses_only_the_explicit_span(self):
        value = lookup(sentence="猫と猫です。", surface="猫", lemma="猫")
        choices = authoring.NoteChoices(kind="cloze", cloze_span=(2, 3), cloze_number=2)
        preview = authoring.build_preview(Note(model_type=1), profile(), value, choices=choices)
        self.assertEqual(preview.field_values()["Sentence"], "猫と{{c2::猫}}です。")
        with self.assertRaises(authoring.AuthoringError):
            authoring.build_preview(Note(), profile(), value, choices=choices)
        with self.assertRaises(authoring.AuthoringError):
            authoring.build_preview(Note(model_type=1), profile(), value,
                                   choices=authoring.NoteChoices(kind="cloze"))

    def test_cloze_rejects_wrong_span_and_existing_hidden_deletions(self):
        for sentence, span in (("猫です", (1, 2)), ("{{c1::猫}}です", (6, 7))):
            with self.subTest(sentence=sentence), self.assertRaises(authoring.AuthoringError):
                authoring.build_preview(Note(model_type=1), profile(), lookup(sentence=sentence, surface="猫"),
                                        choices=authoring.NoteChoices(kind="cloze", cloze_span=span))

    def test_existing_cloze_can_gain_meanings_without_rewriting_any_deletion(self):
        note = Note({"Sentence": "{{c1::猫}}と{{c2::犬}}です。"}, model_type=1)
        preview = authoring.build_preview(note, profile(), lookup(), policies={"sentence": "replace"})
        self.assertEqual(preview.field_values()["Sentence"], note["Sentence"])
        self.assertEqual(preview.field_values()["Meaning"], "to eat<br>to live on")

    def test_profile_rejects_missing_fields_and_mapping_two_roles_to_one_field(self):
        for fields in ({"expression": "Gone"}, {"expression": "Expression", "reading": "Expression"}):
            with self.subTest(fields=fields), self.assertRaises(authoring.AuthoringError):
                authoring.FieldProfile("Bad", 10, fields).validate(Note().note_type())

    def test_refresh_leaves_user_corrections_alone(self):
        note = Note({"Meaning": "my correction", "Reading": "たべる"})
        provenance = {"Meaning": {"hash": authoring.content_hash("to eat")},
                      "Reading": {"hash": authoring.content_hash("たべる")}}
        preview = authoring.build_preview(note, profile(), lookup(reading="タベル"),
                                         policies={"meanings": "refresh", "reading": "refresh"},
                                         provenance=provenance)
        self.assertEqual(preview.field_values()["Meaning"], "my correction")
        self.assertEqual(preview.field_values()["Reading"], "タベル")
        self.assertEqual(next(c.reason for c in preview.changes if c.role == "meanings"), "user-edited")

    def test_refresh_does_not_claim_an_appended_user_prefix_as_generated(self):
        note = Note({"Meaning": "<b>my note</b><br>to eat"})
        provenance = {"Meaning": {"hash": authoring.content_hash(note["Meaning"]), "refreshable": False}}
        preview = authoring.build_preview(note, profile(), lookup(meanings=("new definition",)),
                                         policies={"meanings": "refresh"}, provenance=provenance)
        self.assertEqual(preview.field_values()["Meaning"], note["Meaning"])

    def test_sdk_adapter_retains_examples_and_sense_refs_without_choosing_an_example(self):
        value = authoring.Lookup.from_selection({"surface": "食べました", "lemma": "食べる", "expression": "食べる",
            "entry": "jmdict:1", "reading": "たべる", "contextSentence": None, "senseRefs": ["jmdict#3"],
            "definition": "to eat", "examples": [{"japanese": "ご飯を食べる。", "translation": "Eat rice.",
              "source": "Tatoeba", "reference": "tatoeba:123", "translationLanguage": "en"}],
            "sourceUrl": "https://shirabe.org/en/words/食べる", "pitch": [2], "audioUrls": ["https://deck.example/not-trusted.mp3"]})
        self.assertEqual(value.sentence, "")
        self.assertEqual(value.examples[0].translation, "Eat rice.")
        self.assertEqual(value.sense_refs, ("jmdict#3",))
        self.assertEqual(value.examples[0].source, "Tatoeba")
        self.assertEqual(value.examples[0].reference, "tatoeba:123")
        self.assertEqual(value.examples[0].translation_language, "en")
        self.assertEqual(value.audio_urls, ("https://deck.example/not-trusted.mp3",))

    def test_sdk_selected_definition_over_4000_characters_survives_field_preview(self):
        for length in (4001, 16000):
            with self.subTest(length=length):
                definition = "複数の意味\n" + "意味" * ((length - 6) // 2)
                definition += "義" * (length - len(definition))
                selection = {"lemma": "猫", "definition": definition, "examples": [], "senseRefs": ["jmdict#1", "jmdict#2"]}
                raw = json.dumps({"action": "add", "cardId": 1, "selection": selection}, ensure_ascii=False)
                value = authoring.Lookup.from_selection(Proposal.parse(raw).selection)
                self.assertEqual(value.meanings, (definition,))
                self.assertEqual(value.sense_refs, ("jmdict#1", "jmdict#2"))
                preview = authoring.build_preview(Note(), profile(), value)
                self.assertEqual(preview.field_values()["Meaning"], authoring.escaped_text(definition))

    def test_sdk_50_examples_can_fill_the_64_kib_bridge_budget(self):
        selection = {"lemma": "猫", "definition": "d" * 16000,
                     "examples": [{"japanese": "猫", "translation": "t" * 900} for _ in range(50)]}
        request = {"action": "add", "cardId": 1, "selection": selection}
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        selection["examples"][-1]["translation"] += "t" * (MAX_MESSAGE_BYTES - len(encode(request).encode("utf-8")))
        raw = encode(request)
        self.assertEqual(len(raw.encode("utf-8")), 64 * 1024)
        value = authoring.Lookup.from_selection(Proposal.parse(raw).selection)
        self.assertEqual(len(value.examples), 50)
        self.assertEqual(value.examples[-1].translation, selection["examples"][-1]["translation"])
        self.assertEqual(value.meanings, (selection["definition"],))

    def test_sdk_selection_budget_counts_utf8_bytes_and_explains_how_to_reduce_it(self):
        selection = {"lemma": "猫", "definition": "d" * 16000,
                     "examples": [{"japanese": "猫", "translation": "t" * 900} for _ in range(50)]}
        raw = json.dumps(selection, ensure_ascii=False, separators=(",", ":"))
        selection["examples"][-1]["translation"] += "t" * (64 * 1024 - len(raw.encode("utf-8")))
        self.assertEqual(len(authoring.Lookup.from_selection(selection).examples), 50)
        selection["examples"][-1]["translation"] += "猫"
        with self.assertRaisesRegex(authoring.AuthoringError, "Choose fewer meanings or examples"):
            authoring.Lookup.from_selection(selection)

    def test_selected_definition_limit_has_actionable_native_error_text(self):
        with self.assertRaisesRegex(authoring.AuthoringError, "Choose fewer meanings"):
            authoring.Lookup.from_selection({"lemma": "猫", "definition": "d" * 16001})

    def test_authoring_boundaries_reject_lone_surrogates_with_a_typed_error(self):
        for parse in (authoring.Lookup.from_selection, authoring.Lookup.from_payload):
            for invalid in ("\ud800", "\udfff", "猫\udc00"):
                with self.subTest(parser=parse.__name__, invalid=repr(invalid)):
                    with self.assertRaisesRegex(authoring.AuthoringError, "invalid text.*Select the word again"):
                        parse({"lemma": invalid})
            self.assertEqual(parse({"lemma": "猫😀"}).lemma, "猫😀")
        with self.assertRaisesRegex(authoring.AuthoringError, "invalid text"):
            authoring.Lookup.from_selection({"lemma": "猫", "examples": [{"japanese": "猫", "translation": "\ud800"}]})

    def test_entry_contract_preserves_an_opaque_source_id_after_the_first_colon(self):
        for entry in ("jmdict:word:cat-1", "uploaded:../猫?part=Ａ&sense=2#noun", "日本語辞典:語:猫 "):
            with self.subTest(entry=entry):
                value = authoring.Lookup.from_selection({"lemma": "猫", "entry": entry})
                self.assertEqual(value.entry, entry)
                preview = authoring.build_preview(Note(), profile(), value)
                self.assertEqual(preview.field_values()["Entry"], authoring.escaped_text(entry))
        for entry in ("no-prefix-separator", ":word:cat-1", "jmdict:", " :word", "jmdict: ",
                      "jmdict:cat\n1", "jmdict:cat\t1", "jmdict:cat\x7f", "jmdict:cat\x85"):
            with self.subTest(entry=entry), self.assertRaises(authoring.AuthoringError):
                authoring.Lookup.from_selection({"lemma": "猫", "entry": entry})

    def test_snapshot_detects_same_second_edits_tags_and_schema_changes(self):
        for edit in (lambda n: n.__setitem__("Other", "new"), lambda n: n.tags.append("new"),
                     lambda n: n.names.__setitem__(0, "Renamed")):
            note = Note()
            before = authoring.NoteSnapshot.capture(note)
            edit(note)
            self.assertFalse(before.matches(note))

    def test_config_round_trip_keeps_unrelated_settings(self):
        old = {"cardToken": "private", "enabled": True}
        saved = authoring.remember_profile(old, profile(deck_id=3, tags=("mined",)))
        self.assertEqual(saved["cardToken"], "private")
        self.assertEqual(authoring.profiles_from_config(saved)[0], profile(deck_id=3, tags=("mined",)))
        self.assertNotIn("authoringProfiles", old)

    def test_repairing_a_deleted_note_type_replaces_only_the_selected_profile(self):
        previous = authoring.FieldProfile("Japanese", 999999, {"expression": "Front"})
        unrelated = authoring.FieldProfile("Other", 20, {"expression": "Front"})
        config = authoring.remember_profile(authoring.remember_profile({"cardToken": "keep"}, previous), unrelated)
        saved = authoring.remember_profile(config, profile(), previous=previous)
        self.assertEqual(authoring.profiles_from_config(saved), [unrelated, profile()])
        self.assertEqual(saved["cardToken"], "keep")

    def test_bridge_payload_is_bounded_and_has_no_target_note_or_file_authority(self):
        for bad in ({"lemma": "猫", "note_id": 42}, {"lemma": "猫", "audio": "/etc/passwd"},
                    {"lemma": "猫" * 500}, {"lemma": 123}, {"lemma": "猫", "entry": "broken"}):
            with self.subTest(bad=bad), self.assertRaises(authoring.AuthoringError):
                authoring.Lookup.from_payload(bad)


if __name__ == "__main__":
    unittest.main()
