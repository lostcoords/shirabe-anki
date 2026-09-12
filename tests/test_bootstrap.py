import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import support
from shirabe_addon import inject, template


def settings_from(html):
    return json.loads(re.search(r"window\.__shirabe = (.*?);</script>", html).group(1))


class PrivateBootstrap(unittest.TestCase):
    def test_native_card_uses_only_note_type_review_preferences(self):
        col = Mock()
        col.get_config.return_value = {"answerOnly": False, "onlineParsing": True, "enabled": True,
                                       "cardToken": "not-a-credential-source"}
        card = SimpleNamespace(id=42, col=col, note=lambda: SimpleNamespace(mid=7))
        values = inject.card_settings({"enabled": False, "cardToken": "account"}, card)
        self.assertEqual(values["cardId"], 42)
        self.assertFalse(values["answerOnly"])
        self.assertTrue(values["onlineParsing"])
        self.assertFalse(values["enabled"])
        self.assertEqual(values["cardToken"], "account")
        col.get_config.assert_called_once_with("shirabe.template.7")

    def test_native_card_without_note_preferences_keeps_global_choices(self):
        col = Mock()
        col.get_config.return_value = None
        card = SimpleNamespace(id=42, col=col, note=lambda: SimpleNamespace(mid=7))
        values = inject.card_settings({"answerOnly": False, "onlineParsing": True}, card)
        self.assertFalse(values["answerOnly"])
        self.assertTrue(values["onlineParsing"])

    def test_click_only_without_full_card_upload_is_the_default(self):
        settings = settings_from(inject.attachment({}, "/w"))
        self.assertEqual(settings["trigger"], "click")
        self.assertFalse(settings["onlineParsing"])

    def test_full_card_upload_requires_explicit_preference(self):
        settings = settings_from(inject.attachment({"onlineParsing": True}, "/w"))
        self.assertTrue(settings["onlineParsing"])
        settings = settings_from(inject.attachment({"cardToken": "connected"}, "/w"))
        self.assertFalse(settings["onlineParsing"])

    def test_question_is_untouched_by_default_but_answer_has_lookup(self):
        html = "<div>猫</div>"
        self.assertEqual(inject.with_card(html, {}, "/w", kind="reviewQuestion"), html)
        self.assertIn("attach.js", inject.with_card(html, {}, "/w", kind="reviewAnswer"))
        self.assertIn("attach.js", inject.with_card(html, {"answerOnly": False}, "/w", kind="reviewQuestion"))

    def test_stored_card_data_cannot_close_its_script_element(self):
        value = '</script><script>alert("deck")</script>'
        html = inject.attachment({"cardToken": value}, "/w")
        self.assertEqual(html.count("</script>"), 2)
        self.assertEqual(settings_from(html)["stored"], value)

    def test_template_settings_cannot_inject_scripts(self):
        value = 'https://shirabe.org/</script><script>alert(1)</script>'
        html = template.block(value, template.CLIENT_ID, "click")
        self.assertEqual(html.count("</script>"), 1)

    def test_mobile_template_does_not_upload_whole_cards_by_default(self):
        html = template.block("https://shirabe.org", template.CLIENT_ID, "click")
        self.assertNotIn("parse: true", html)


if __name__ == "__main__":
    unittest.main()
