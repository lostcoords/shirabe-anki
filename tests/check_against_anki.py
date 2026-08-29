"""Every Anki symbol this add-on uses, checked against a real Anki.

WHY THIS IS SEPARATE FROM THE SUITE: `test_addon.py` deliberately never imports
`aqt`, because a test that needs a GUI is a test nobody runs. That leaves one
whole class of bug uncovered, and it is the worst class: a name that does not
exist (`chooseList` moved, a hook renamed) raises at IMPORT time, so the add-on
does not half-work, it fails to load at all, with a traceback in a dialog the
reader cannot act on.

Anki's own bundled Python has `aqt` and `anki` in it, and importing them needs
no window. So the names can be checked even though the behaviour cannot.

Run it against an installed Anki:

    anki-addon/tests/check_against_anki.py

It finds Anki's Python itself, and says so and stops if there is none: this is a
check you run on a machine that has Anki, not a suite gate.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

CANDIDATES = [
    Path.home() / "Library/Application Support/AnkiProgramFiles/.venv/bin/python",
    Path("/usr/local/share/anki/bin/python"),
    Path.home() / ".local/share/AnkiProgramFiles/.venv/bin/python",
]


def anki_python() -> Path | None:
    return next((p for p in CANDIDATES if p.exists()), None)


def imported_names() -> dict[str, set[str]]:
    """`from aqt.utils import tooltip` becomes {"aqt.utils": {"tooltip"}}."""
    wanted: dict[str, set[str]] = {}
    for module in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"aqt", "anki"}:
                wanted.setdefault(node.module, set()).update(a.name for a in node.names)
    return wanted


def hook_names() -> set[str]:
    """`gui_hooks.card_will_show.append(...)`. A renamed hook is silent: the
    add-on loads, and simply never runs."""
    found: set[str] = set()
    for module in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "gui_hooks"):
                found.add(node.attr)
    return found


# Methods reached through a runtime object, which no import statement names and
# so nothing above would notice going missing. Anki has renamed several of these
# (`models.save` became `update_dict`), and every one is a crash the first time
# a reader uses the feature rather than at load.
RUNTIME = [
    ("anki.models", "ModelManager", ["all", "update_dict"]),
    ("anki.collection", "Collection", ["build_search_string", "find_notes", "get_note"]),
    ("anki.cards", "Card", ["type", "ivl"]),
    ("anki.notes", "Note", ["cards"]),
    ("aqt.addons", "AddonManager", ["setWebExports", "getConfig", "writeConfig", "addonFromModule"]),
]

PROBE = """
import sys, json
wanted = json.loads(sys.argv[1])
hooks = json.loads(sys.argv[2])
runtime = json.loads(sys.argv[3])
missing = []
for module, names in wanted.items():
    try:
        loaded = __import__(module, fromlist=["*"])
    except Exception as error:
        missing.append(f"{module}: will not import ({error})")
        continue
    for name in names:
        if not hasattr(loaded, name):
            missing.append(f"{module}.{name}")
from aqt import gui_hooks
for hook in hooks:
    if not hasattr(gui_hooks, hook):
        missing.append(f"gui_hooks.{hook}")
for module, klass, members in runtime:
    try:
        owner = getattr(__import__(module, fromlist=["*"]), klass)
    except Exception as error:
        missing.append(f"{module}.{klass}: {error}")
        continue
    try:
        import inspect
        body = inspect.getsource(owner)
    except Exception:
        body = ""
    for member in members:
        # Three places a member can be declared, and Anki uses all three:
        # `Note.cards` is a method, `Card.type` is a bare annotation, and
        # `Card.ivl` is only ever `self.ivl = card.interval` in `__init__`.
        if (hasattr(owner, member)
                or member in getattr(owner, "__annotations__", {})
                or f"self.{member} =" in body):
            continue
        missing.append(f"{klass}.{member}")
from anki.buildinfo import version
print(json.dumps({"version": version, "missing": missing}))
"""


def main() -> int:
    python = anki_python()
    if not python:
        print("No installed Anki found, so nothing to check against.")
        print("Looked in:", *[f"  {p}" for p in CANDIDATES], sep="\n")
        return 0

    import json

    wanted = {module: sorted(names) for module, names in imported_names().items()}
    hooks = sorted(hook_names())
    result = subprocess.run(
        [str(python), "-c", PROBE, json.dumps(wanted), json.dumps(hooks), json.dumps(RUNTIME)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("Could not ask Anki:", result.stderr.strip()[-400:])
        return 1

    answer = json.loads(result.stdout)
    checked = (sum(len(names) for names in wanted.values()) + len(hooks)
               + sum(len(members) for _, _, members in RUNTIME))
    if answer["missing"]:
        print(f"Against Anki {answer['version']}, {len(answer['missing'])} of {checked} names are gone:")
        print(*[f"  {name}" for name in answer["missing"]], sep="\n")
        return 1
    print(f"All {checked} Anki names the add-on uses exist in Anki {answer['version']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
