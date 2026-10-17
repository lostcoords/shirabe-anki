"""What is sent, and more importantly what is not.

The privacy claims in `analytics.py` are the kind that read as true and quietly
stop being: a property added later, a detail forwarded from the card. These
assert them rather than trusting the docstring.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "addon/src"
sys.path.insert(0, str(SRC))

from shirabe_addon import analytics  # noqa: E402


def sent(config, event, properties=None):
    """Capture the payload without a network or a thread."""
    captured = {}

    def fake_thread(target=None, args=(), daemon=None):
        captured["payload"] = args[0]
        return mock.MagicMock()

    with mock.patch("threading.Thread", side_effect=fake_thread):
        analytics.capture(config, event, properties)
    return captured.get("payload")


class WhatIsSent(unittest.TestCase):
    def test_an_event_carries_the_versions_that_make_it_answerable(self):
        payload = sent({"installId": "abc"}, "anki_addon_started", {"addon_version": "0.1.0"})
        props = payload["batch"][0]["properties"]
        # Without these a crash on 24.06 and a crash on 25.09 are one pile.
        self.assertEqual(props["surface"], "anki")
        self.assertEqual(props["addon_version"], "0.1.0")
        self.assertIn("os", props)

    def test_the_reader_is_a_random_install_id_and_nothing_else(self):
        payload = sent({"installId": "abc"}, "card_opened")
        props = payload["batch"][0]["properties"]
        self.assertEqual(props["distinct_id"], "abc")
        flat = json.dumps(payload)
        for forbidden in ("@", "profile", "deck", "note"):
            self.assertNotIn(forbidden, flat, f"{forbidden!r} reached an analytics payload")

    def test_an_install_id_is_minted_once_and_kept(self):
        written = {}
        analytics._install_id({}, on_config_change=lambda c: written.update(c))
        self.assertEqual(len(written["installId"]), 32)
        self.assertEqual(analytics._install_id({"installId": "kept"}), "kept")

    def test_the_public_token_goes_through_our_own_domain(self):
        # Not *.i.posthog.com: content blockers filter that by name, and a
        # swallowed report reads as "no crashes".
        self.assertTrue(analytics.HOST.endswith("shirabe.org"))
        self.assertTrue(analytics.TOKEN.startswith("phc_"))


class OptOut(unittest.TestCase):
    def test_reporting_off_sends_nothing_at_all(self):
        self.assertIsNone(sent({"reportUsage": False, "installId": "abc"}, "card_opened"))

    def test_reporting_is_on_unless_the_reader_turned_it_off(self):
        self.assertIsNotNone(sent({"installId": "abc"}, "card_opened"))


class NeverRaises(unittest.TestCase):
    def test_a_dead_host_is_silent_rather_than_an_error_on_a_card(self):
        # An add-on that fails a card render because a metrics host was slow has
        # done more damage than the metric was worth.
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            analytics._post({"api_key": "x", "batch": []})  # must not raise

    def test_a_platform_lookup_outside_anki_degrades_instead_of_failing(self):
        with mock.patch.dict(sys.modules, {"anki.buildinfo": None, "aqt.qt": None}):
            found = analytics._platform()
        self.assertIn("os", found)
        self.assertNotIn("anki_version", found, "there is no Anki here to ask")


if __name__ == "__main__":
    unittest.main(verbosity=2)
