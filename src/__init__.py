"""Shirabe for Anki: the dictionary card, on every card you review.

What this file is: the seams Anki gives an add-on, and nothing else. The card is
`shirabe-sdk`, bundled into `web/`; the reader's own words come from
`collection.py`; the HTML rewrite is `inject.py`; the note type a phone reads is
`template.py`.

SIGNING IN IS NOT HERE, and used to be. The add-on held the reader's tokens and
made the card's API calls for it, so nothing reached a page that other decks can
read. It cost 600 lines, a second OAuth implementation, and a `pycmd` command
taking a path and a body — and that command is reachable from any deck's
JavaScript, with no way to tell it from ours, so hiding the credential bought a
capability that was worse than the credential. The card signs itself in now, as
a read-only client, the same way it does on a phone where there is no Python to
hide anything behind.

  card_will_show                  put the card on every rendered card
  setWebExports                   let the webview load the bundle off disk
  webview_did_receive_js_message  answer the card's questions from the collection

None of those exist on a phone. AnkiDroid and AnkiMobile run no add-ons, so the
only thing that reaches them is the collection itself: `template.py` writes the
card into a note type, which syncs.
"""

from __future__ import annotations

import json

from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu
from aqt.utils import askUser, chooseList, showInfo, showWarning, tooltip

from . import analytics, collection, inject, settings as settings_ui, template

ADDON = mw.addonManager.addonFromModule(__name__)
# Only what the webview must fetch. A pattern wider than this would publish the
# add-on's Python to any page Anki renders.
mw.addonManager.setWebExports(__name__, r"web/.*\.(js|css)")
WEB_BASE = f"/_addons/{ADDON}/web"

# Every message from the card is prefixed, because this hook is shared with every
# other add-on the reader has installed and an unprefixed `states` would be a
# collision waiting to happen.
BRIDGE_PREFIX = "shirabe:"

# Events the card is allowed to send through the bridge. A closed list, because
# the bridge is reachable from any JavaScript on a rendered card, including a
# shared deck's own.
REPORTABLE = {"card_opened", "card_missing", "lookup_failed", "word_saved", "word_known", "save_refused"}


def config() -> dict:
    return mw.addonManager.getConfig(__name__) or {}


def save_config(values: dict) -> None:
    mw.addonManager.writeConfig(__name__, values)


def on_card_will_show(html: str, card, kind: str) -> str:
    # Previews and the card layout screen render through here too. They are worth
    # including: someone editing a note type is exactly who wants to see what the
    # card will look like.
    return inject.with_card(html, config(), WEB_BASE)


def on_js_message(handled: tuple[bool, object], message: str, context: object) -> tuple[bool, object]:
    """The three things the card asks Python for: its own states, somewhere to
    keep a sign-in that outlives Anki's port, and a report.

    Returning `handled` untouched for anything else is not politeness, it is
    required: every add-on's handler sees every message, and swallowing one
    breaks whichever add-on it belonged to.
    """
    if not message.startswith(BRIDGE_PREFIX):
        return handled

    command, _, payload = message[len(BRIDGE_PREFIX):].partition(":")
    if command == "report":
        # The card reports through Python rather than posting for itself: one
        # sender, and one place the reader's opt-out is honoured.
        event, _, detail = payload.partition(":")
        if event in REPORTABLE:
            analytics.capture(config(), event, _detail(detail), on_config_change=save_config)
        return (True, "")
    if command == "store":
        # The card's sign-in, kept where a changing port cannot orphan it. See
        # `inject.py` for why the browser's own storage will not do.
        save_config({**config(), "cardToken": payload})
        return (True, "")
    if command == "states":
        if not config().get("useCollectionStates", True):
            return (True, json.dumps({}))
        words = [word for word in payload.split(" ") if word]
        return (True, json.dumps(collection.states_for(mw.col, words)))
    return handled


def sign_out() -> None:
    """Forget the card's sign-in on this computer.

    The card reads what Python holds when it renders, so this is the whole of
    it: the next card comes up signed out and offers the sign-in again.
    """
    save_config({**config(), "cardToken": ""})
    tooltip("Signed out of Shirabe on this computer.")


def open_settings() -> None:
    """The window, rather than Anki's JSON editor.

    Half of what is stored here is written by signing in rather than typed, and a
    text box a reader can paste a refresh token into is the thing the sign-in
    flows exist to avoid.
    """
    dialog = settings_ui.SettingsDialog(
        mw,
        config(),
        on_save=lambda values: (save_config(values), tooltip("Shirabe settings saved")),
        on_sign_out=sign_out,
    )
    dialog.exec()


def _detail(raw: str) -> dict:
    """The card's own properties, kept to scalars and a known size.

    Whatever arrives here came from JavaScript on a card, which a shared deck
    can author. It reaches an analytics payload, so it is bounded rather than
    forwarded.
    """
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key)[:40]: value
        for key, value in list(parsed.items())[:10]
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 100
    }


def on_configured() -> None:
    # Anki's gear menu offers "Config"; send that to the window too, so there is
    # one place these are changed rather than two that can disagree.
    open_settings()


def toggle_enabled(on: bool) -> None:
    save_config({**config(), "enabled": on})
    # The next card renders with it; the one on screen was built already.
    tooltip("Shirabe popup on" if on else "Shirabe popup off (reopen the card)")


def note_types_with_shirabe() -> list[str]:
    """Which note types already carry the block, by name."""
    return sorted(
        model["name"] for model in mw.col.models.all()
        if any(template.installed(card.get("qfmt", "") + card.get("afmt", "")) for card in model["tmpls"])
    )


def add_to_note_type() -> None:
    """Put the card into a note type, so it works where no add-on runs.

    Asks which one rather than guessing: a collection has note types the reader
    never studies, and rewriting all of them is a change to their data they did
    not ask for. It then asks again by name, because `chooseList` answers with a
    row whether or not anybody chose one, and the first row is somebody's note
    type.
    """
    models = sorted(mw.col.models.all(), key=lambda m: m["name"])
    if not models:
        return showWarning("This collection has no note types yet.")

    def has_it(model: dict) -> bool:
        return any(template.installed(c.get("qfmt", "") + c.get("afmt", "")) for c in model["tmpls"])

    labels = [f"{m['name']}{'  ✓' if has_it(m) else ''}" for m in models]
    picked = chooseList("Add the Shirabe card to which note type?", labels, parent=mw)
    if not 0 <= picked < len(models):
        return

    model = models[picked]
    if not askUser(f"Add the Shirabe card to the back of “{model['name']}”?"):
        return

    settings = config()
    api_base = settings.get("apiBase", "https://shirabe.org")
    # A note type syncs. An address that only resolves on this machine resolves
    # to the PHONE once it lands there, and the block has no error branch, so
    # the reader would sync, tap a word, and see nothing at all.
    if not template.reaches_a_phone(api_base):
        return showWarning(
            f"“{api_base}” only works on this computer, so the card would do nothing on a phone.\n\n"
            "Point Shirabe at an address the phone can reach (Settings… → Advanced): "
            "your machine's name on the network for testing, or https://shirabe.org."
        )

    for card in model["tmpls"]:
        # The answer side only. The question is usually the word being tested,
        # and turning that into a tappable dictionary entry hands the reader the
        # answer before they have recalled it.
        card["afmt"] = template.with_block(
            card["afmt"],
            api_base,
            template.CLIENT_ID,
            settings.get("trigger", "click"),
        )
    mw.col.models.update_dict(model)

    analytics.capture(settings, "note_type_linked", on_config_change=save_config)
    showInfo(
        f"Shirabe is now on the back of “{model['name']}”.\n\n"
        "Sync, and the same card works on AnkiDroid and AnkiMobile. Tap a word to "
        "look it up, and tap Sign in the first time to use your Shirabe account there."
    )


def remove_from_note_types() -> None:
    """Take it back out of every note type that has it."""
    touched = []
    for model in mw.col.models.all():
        if not any(template.installed(c.get("qfmt", "") + c.get("afmt", "")) for c in model["tmpls"]):
            continue
        for card in model["tmpls"]:
            card["qfmt"] = template.without_block(card["qfmt"])
            card["afmt"] = template.without_block(card["afmt"])
        mw.col.models.update_dict(model)
        touched.append(model["name"])

    tooltip(f"Removed from {len(touched)} note type(s)." if touched else "No note type had it.")


def install_menu() -> None:
    menu = QMenu("Shirabe", mw)

    # The one switch worth reaching without opening a window. Checkable rather
    # than two actions, so the menu says which state you are in.
    enabled = QAction("Popup on cards", mw)
    enabled.setCheckable(True)
    enabled.setChecked(bool(config().get("enabled", True)))
    enabled.toggled.connect(toggle_enabled)

    open_action = QAction("Settings…", mw)
    open_action.triggered.connect(open_settings)

    menu.addAction(enabled)
    menu.addSeparator()
    menu.addAction(open_action)
    menu.addSeparator()

    # The phone half. Separate from everything above it because it changes the
    # collection rather than a setting, and because it is the only thing here
    # that survives a sync.
    to_note_type = QAction("Add to a note type (for phones)…", mw)
    to_note_type.triggered.connect(add_to_note_type)
    from_note_types = QAction("Remove from note types", mw)
    from_note_types.triggered.connect(remove_from_note_types)
    menu.addAction(to_note_type)
    menu.addAction(from_note_types)
    mw.form.menuTools.addSeparator()
    mw.form.menuTools.addMenu(menu)


def on_profile_open() -> None:
    """Once per session, and once more when the version has moved.

    The version that changed is the whole signal: it is what turns "somebody
    crashed" into "this release broke Anki 24.06".
    """
    settings = config()
    known = settings.get("lastVersion")
    current = _addon_version()
    analytics.capture(settings, "anki_addon_started", {"addon_version": current}, on_config_change=save_config)
    if known != current:
        analytics.capture(settings, "anki_addon_installed",
                          {"addon_version": current, "reason": "update" if known else "install"},
                          on_config_change=save_config)
        save_config({**config(), "lastVersion": current})


def _addon_version() -> str:
    meta = mw.addonManager.addon_meta(ADDON)
    return str(getattr(meta, "human_version", None) or "unknown")


gui_hooks.profile_did_open.append(on_profile_open)
gui_hooks.card_will_show.append(on_card_will_show)
gui_hooks.webview_did_receive_js_message.append(on_js_message)
gui_hooks.main_window_did_init.append(install_menu)
mw.addonManager.setConfigAction(__name__, on_configured)
