"""Check imported symbols, hooks and an explicit runtime API inventory in Anki.

    python3 tests/check_against_anki.py [--python /path/to/python]

Defaults to an installed Anki's Python. Opens no collection or window. This
checks availability, not signatures or behavior; native tests cover those.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "addon/src"

def anki_python() -> Path | None:
    return Path(sys.executable) if importlib.util.find_spec("anki") else None


def imported_names() -> dict[str, set[str]]:
    """`from aqt.utils import tooltip` becomes {"aqt.utils": {"tooltip"}}."""
    wanted: dict[str, set[str]] = {}
    for module in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"aqt", "anki"}:
                wanted.setdefault(node.module, set()).update(a.name for a in node.names)
    return wanted


def hook_names() -> set[str]:
    """Discover gui_hooks.<name> references in the add-on source."""
    found: set[str] = set()
    for module in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "gui_hooks"):
                found.add(node.attr)
    return found


RUNTIME = [
    ("anki.collection", "Collection", [
        "add_custom_undo_entry", "all_config", "build_search_string", "find_notes", "get_config", "get_note",
        "merge_undo_entries", "new_note", "remove_config", "set_config", "undo", "undo_status", "update_notes",
    ]),
    ("anki.collection", "SearchNode", ["IdList"]),
    ("anki.models", "ModelManager", [
        "_clear_cache", "add_dict", "add_field", "add_template", "all", "by_name", "get", "new", "new_field", "new_template", "update_dict",
    ]),
    ("anki.decks", "DeckManager", ["all_config", "all_names_and_ids", "get", "is_filtered"]),
    ("anki.media", "MediaManager", ["dir", "trash_files", "write_data"]),
    ("anki.cards", "Card", ["answer", "id", "ivl", "nid", "note", "ord", "question", "type"]),
    ("anki.notes", "Note", ["cards", "fields", "guid", "id", "keys", "mid", "mod", "note_type", "tags", "usn"]),
    ("aqt", "DialogManager", ["open"]),
    ("aqt.addcards", "AddCards", ["set_note"]),
    ("aqt.addons", "AddonManager", [
        "addonFromModule", "addon_meta", "getConfig", "setConfigAction", "setWebExports", "writeConfig",
    ]),
    ("aqt.browser", "Browser", ["search_for", "selected_notes"]),
    ("aqt.deckchooser", "DeckChooser", ["selected_deck_id"]),
    ("aqt.editor", "Editor", ["addButton", "call_after_note_saved", "set_note"]),
    ("aqt.operations", "CollectionOp", ["failure", "run_in_background", "success"]),
    ("aqt.operations", "QueryOp", ["failure", "run_in_background", "with_progress", "without_collection"]),
    ("aqt.taskman", "TaskManager", ["run_on_main"]),
    ("aqt.webview", "AnkiWebView", ["cleanup", "eval", "evalWithCallback", "hide", "selectedText", "stdHtml"]),
]


RECEIVERS = {
    "col": "Collection", "models": "ModelManager", "media": "MediaManager",
    "decks": "DeckManager", "taskman": "TaskManager", "addonManager": "AddonManager",
    "note": "Note", "card": "Card", "browser": "Browser", "editor": "Editor",
    "web": "AnkiWebView", "dialogs": "DialogManager",
}


class NativeCalls(ast.NodeVisitor):
    def __init__(self):
        self.calls = set()
        self.media_bindings = {}
        self.dictionary_bindings = set()

    def receiver(self, node):
        if isinstance(node, ast.Name):
            if node.id in self.dictionary_bindings:
                return None
            if node.id in self.media_bindings:
                return "MediaManager" if self.media_bindings[node.id] else None
            return RECEIVERS.get(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr == "media":
                return "MediaManager" if self.receiver(node.value) == "Collection" else None
            if node.attr not in {"note", "card"}:
                return RECEIVERS.get(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "MediaManager":
            return "MediaManager"
        return None

    def bind_media(self, target, native):
        if isinstance(target, ast.Name) and (native or target.id == "media" or target.id in self.media_bindings):
            self.media_bindings[target.id] = native

    def visit_Assign(self, node):
        self.generic_visit(node)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if isinstance(node.value, (ast.Dict, ast.DictComp)):
                    self.dictionary_bindings.add(target.id)
                else:
                    self.dictionary_bindings.discard(target.id)
            self.bind_media(target, self.receiver(node.value) == "MediaManager")

    @staticmethod
    def media_annotation(annotation):
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            annotation = ast.parse(annotation.value, mode="eval").body
        return ((isinstance(annotation, ast.Name) and annotation.id == "MediaManager")
                or (isinstance(annotation, ast.Attribute) and annotation.attr == "MediaManager"))

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        self.bind_media(node.target, self.media_annotation(node.annotation))

    def visit_FunctionDef(self, node):
        for value in [*getattr(node, "decorator_list", []), *node.args.defaults, *node.args.kw_defaults]:
            if value is not None:
                self.visit(value)
        previous = self.media_bindings
        previous_dictionaries = self.dictionary_bindings
        self.media_bindings = previous.copy()
        self.dictionary_bindings = previous_dictionaries.copy()
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                     node.args.vararg, node.args.kwarg]
        for argument in arguments:
            if argument is not None:
                self.dictionary_bindings.discard(argument.arg)
                native = (self.media_annotation(argument.annotation) if argument.annotation is not None
                          else argument.arg == "media")
                self.bind_media(ast.Name(id=argument.arg), native)
        for statement in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(statement)
        self.media_bindings = previous
        self.dictionary_bindings = previous_dictionaries

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            owner = node.func.value
            klass = self.receiver(owner)
            if klass and not (klass == "Card" and node.func.attr in {"get", "update"}):
                self.calls.add((node.lineno, klass, node.func.attr))
            while isinstance(owner, (ast.Call, ast.Attribute)):
                if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name) and owner.func.id in {"CollectionOp", "QueryOp"}:
                    self.calls.add((node.lineno, owner.func.id, node.func.attr))
                    break
                owner = owner.func if isinstance(owner, ast.Call) else owner.value
        self.generic_visit(node)


def observed_native_calls(source):
    """Find conventional native receivers, resolving media bindings within each function."""
    visitor = NativeCalls()
    visitor.visit(ast.parse(source))
    return visitor.calls


PROBE = """
import anki.collection
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, help="Python environment containing the Anki version to check")
    python = parser.parse_args().python or anki_python()
    if not python:
        print("No Anki runtime is active, so nothing to check against.")
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
        print(f"Against Anki {answer['version']}, {len(answer['missing'])} of {checked} checked API entries are missing:")
        print(*[f"  {name}" for name in answer["missing"]], sep="\n")
        return 1
    print(f"All {checked} checked API entries exist in Anki {answer['version']} (symbol availability only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
