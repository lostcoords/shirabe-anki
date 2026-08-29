"""What the settings window can be checked on without Qt.

The widgets need PyQt6, which lives inside Anki. What does not need Qt, and is
what actually goes wrong, is whether the window and the rest of the add-on agree
about the config: a dialog writing a key nothing reads is a control that appears
to work and does nothing.
"""

import ast
import json
import re
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


def _assigned_keys(path: Path, function: str) -> set[str]:
    """The string keys a function builds into a dict literal."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return {
                key.value
                for dict_node in ast.walk(node)
                if isinstance(dict_node, ast.Dict)
                for key in dict_node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError(f"{function} not found in {path.name}")


def _constant_values(name: str) -> set[str]:
    """The second half of each pair in a module-level `[(label, value), ...]`.

    These strings are spent directly as `PopupOptions` in the SDK, so a fourth
    option here is a silent fallback to the default there.
    """
    tree = ast.parse((SRC / "settings.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return {
                element.elts[1].value
                for element in node.value.elts
                if isinstance(element, ast.Tuple) and isinstance(element.elts[1], ast.Constant)
            }
    raise AssertionError(f"{name} not found")


class ConfigAgreement(unittest.TestCase):
    def test_the_window_can_reach_every_setting_that_has_no_other_control(self):
        # The window replaced Anki's JSON editor, so a preference it cannot
        # write is a preference nobody can change. Exempt are the keys the
        # add-on writes for itself rather than for the reader: signing in owns
        # the card's token, and reporting owns the install id and the last
        # version it saw. A control for those would be a control for nothing.
        shipped = set(json.loads((SRC / "config.json").read_text()))
        written = _assigned_keys(SRC / "settings.py", "_save")
        ours = {"cardToken", "installId", "lastVersion"}
        self.assertEqual(shipped - written - ours, set(), "a setting with no control")

    def test_every_key_the_window_writes_is_a_key_the_add_on_ships(self):
        written = _assigned_keys(SRC / "settings.py", "_save")
        shipped = set(json.loads((SRC / "config.json").read_text()))
        self.assertTrue(written, "the window saves something")
        self.assertEqual(written - shipped, set(), "the window writes a key config.json does not declare")

    def test_every_preference_the_window_writes_is_one_the_card_is_told_about(self):
        # `inject.attachment` is what reaches a rendered card. A preference the
        # window can change but injection never sends is a dead control.
        sent = _assigned_keys(SRC / "inject.py", "attachment")
        for preference in ("useCollectionStates", "definitionSize", "theme"):
            self.assertIn(preference, sent, f"{preference} never reaches the card")

    def test_the_window_offers_only_values_the_card_understands(self):
        # Read rather than imported: the module reaches for Qt at import time,
        # and Qt lives inside Anki.
        self.assertEqual(_constant_values("DEFINITION_SIZES"), {"small", "medium", "large"})
        self.assertEqual(_constant_values("THEMES"), {"system", "light", "dark"})

    def test_the_window_never_offers_a_field_for_a_token(self):
        # Not "no text fields" (Advanced has one), but that no field is bound to
        # a credential: a box someone can paste a refresh token into is what
        # signing in from the card exists to avoid.
        source = (SRC / "settings.py").read_text()
        fields = re.findall(r"self\.(\w+) = QLineEdit", source)
        self.assertEqual(sorted(fields), ["api_base"])
        for credential in ("refreshToken", "accessToken", "accessTokenExpiresAt", "installId"):
            self.assertNotIn(credential, _assigned_keys(SRC / "settings.py", "_save"))
            self.assertNotIn(f'QLineEdit(self._config.get("{credential}"', source)


class Imports(unittest.TestCase):
    def test_the_window_imports_qt_through_anki_rather_than_pyqt_directly(self):
        # aqt.qt is the shim that survives Anki's Qt version moving.
        tree = ast.parse((SRC / "settings.py").read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertIn("aqt.qt", modules)
        self.assertFalse({m for m in modules if m.startswith("PyQt")}, "never import PyQt directly")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SigningIn(unittest.TestCase):
    """Signing in happens on the card, and nowhere else.

    It used to be a menu item backed by 355 lines of OAuth in Python, so the
    add-on could hold the token out of the page's reach. A phone has no Python
    to hold anything, so the card had to learn to sign itself in anyway — and
    the desktop path was hiding a credential behind a `pycmd` command that any
    deck could call, which was worse than not hiding it. One way in now.
    """

    def test_python_offers_no_sign_in_of_its_own(self):
        source = (SRC / "__init__.py").read_text()
        labels = re.findall(r'QAction\("([^"]+)"', source)
        self.assertFalse([label for label in labels if "onnect" in label], f"menu offers {labels}")

    def test_the_window_sends_the_reader_to_the_card_to_sign_in(self):
        source = (SRC / "settings.py").read_text()
        self.assertIn("use the Sign in link on it", source)
        self.assertNotIn("Sign in", re.findall(r'QPushButton\("([^"]+)"', source))

    def test_signing_out_is_here_because_the_card_is_the_wrong_place_for_it(self):
        # A dictionary popup with a sign-out button on it is clutter on every
        # lookup, for something done twice a year.
        source = (SRC / "settings.py").read_text()
        self.assertEqual(re.findall(r'QPushButton\("([^"]+)"', source), ["Sign out on this computer"])
        self.assertIn("cardToken", _assigned_keys(SRC / "__init__.py", "sign_out"))


class Naming(unittest.TestCase):
    def test_both_places_anki_reads_a_name_from_agree(self):
        # `manifest.json` names a packaged add-on; `meta.json` names an installed
        # one. Two names is two names in the wild.
        manifest = json.loads((SRC / "manifest.json").read_text())
        meta = json.loads((SRC / "meta.json").read_text())
        self.assertEqual(manifest["name"], meta["name"])
        self.assertIn("shirabe.org", manifest["name"], "the name says where it comes from")


class Triggers(unittest.TestCase):
    def test_the_window_offers_only_triggers_the_card_implements(self):
        # These strings are passed straight to `ShirabeCard.attach`; one the SDK
        # does not know silently falls back to hover, which reads as the setting
        # being ignored.
        offered = _constant_values("TRIGGERS")
        self.assertEqual(offered, {"hover", "click", "shift", "alt", "ctrl", "meta"})

    def test_the_menu_carries_the_one_switch_worth_not_opening_a_window_for(self):
        source = (SRC / "__init__.py").read_text()
        self.assertIn("setCheckable(True)", source, "the menu should say which state you are in")
        self.assertIn("enabled.toggled.connect(toggle_enabled)", source)
