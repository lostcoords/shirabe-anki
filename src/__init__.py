"""Shirabe for Anki: the dictionary card, on every card you review.

What this file is: the seams Anki gives an add-on, and nothing else. The card is
`packages/card`, bundled into `web/`; the reader's own words come from
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
from pathlib import Path

from anki.hooks import wrap
from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu, QDialog, QVBoxLayout, QDialogButtonBox, QTimer
from aqt.webview import AnkiWebView
from aqt.utils import showInfo, showWarning, tooltip

from . import analytics, bridge, collection, inject, offline, settings as settings_ui, template

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
AUTHORING_GATE = bridge.ProposalGate()


def config() -> dict:
    return mw.addonManager.getConfig(__name__) or {}


def save_config(values: dict) -> None:
    previous = config()
    if any(previous.get(key, default) != values.get(key, default) for key, default in (("cardToken", ""), ("apiBase", "https://shirabe.org"))):
        offline.invalidate_preparations()
    mw.addonManager.writeConfig(__name__, values)


def on_card_will_show(html: str, card, kind: str) -> str:
    # Previews and the card layout screen render through here too. They are worth
    # including: someone editing a note type is exactly who wants to see what the
    # card will look like.
    values = inject.card_settings(config(), card)
    note = card.note()
    if "ShirabeLookup" in note:
        values["snapshot"] = offline.snapshot_filename(note["ShirabeLookup"])
    return '<script>window.__shirabeNativeRender = true;</script>' + template.DETACH + inject.with_card(html, values, WEB_BASE, kind=kind)


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
    if command == "author":
        return (True, request_authoring(payload, context))
    if command == "report":
        # The card reports through Python rather than posting for itself: one
        # sender, and one place the reader's opt-out is honoured.
        event, _, detail = payload.partition(":")
        if event in REPORTABLE:
            analytics.capture(config(), event, bridge.report_details(detail), on_config_change=save_config)
        return (True, "")
    if command == "store":
        # The card's sign-in, kept where a changing port cannot orphan it. See
        # `inject.py` for why the browser's own storage will not do.
        try:
            stored = bridge.session_slots(payload)
        except bridge.BridgeError:
            return (True, "")
        save_config({**config(), "cardToken": stored})
        return (True, "")
    if command == "states":
        from .authoring import profiles_from_config

        if not config().get("useCollectionStates", True):
            return (True, json.dumps({}))
        if len(payload) > 32768 or mw.col is None:
            return (True, json.dumps({}))
        words = list(dict.fromkeys(word for word in payload.split(" ") if 0 < len(word) <= 256))[:128]
        return (True, json.dumps(collection.states_for(mw.col, words, profiles_from_config(config()))))
    return handled


def sign_out() -> None:
    """Clear this computer's sign-in and active popup sessions."""
    save_config({**config(), "cardToken": ""})
    for web in mw.findChildren(AnkiWebView):
        web.eval("if (window.ShirabeAnki) window.ShirabeAnki.signOut();")
    tooltip("Signed out of Shirabe on this computer.")


def context_card(context):
    from aqt.reviewer import Reviewer
    from aqt.browser.previewer import Previewer

    if isinstance(context, Reviewer):
        if context.card is None:
            return None
        if inject.card_settings(config(), context.card).get("answerOnly", True) and context.state != "answer":
            return None
        return context.card
    if isinstance(context, Previewer):
        card = context.card()
        if card is None:
            return None
        if inject.card_settings(config(), card).get("answerOnly", True) and context.state() != "answer":
            return None
        return card
    return None


def request_authoring(raw: str, context) -> str:
    from . import authoring

    try:
        proposal = bridge.Proposal.parse(raw)
        card = context_card(context)
        if not config().get("enabled", True) or card is None or int(card.id) != proposal.card_id:
            raise bridge.BridgeError("This card has changed. Look the word up again.")
        lookup = authoring.Lookup.from_selection(proposal.selection)
        note_id = int(card.nid)
        AUTHORING_GATE.enter()
    except (bridge.BridgeError, authoring.AuthoringError) as error:
        return json.dumps({"ok": False, "message": str(error)})

    def open_native():
        delegated = False
        try:
            current = context_card(context)
            if current is None or int(current.id) != proposal.card_id or mw.col is None:
                return
            values = config()
            if proposal.action == "add":
                delegated = True
                authoring.open_add_note(mw, lookup, config=values, save_config=save_config,
                                        on_closed=AUTHORING_GATE.leave)
            elif proposal.action == "update":
                delegated = True
                authoring.preview_update(mw, note_id, lookup, config=values, save_config=save_config,
                                         on_closed=AUTHORING_GATE.leave)
            else:
                from aqt import dialogs
                note_ids = authoring.find_existing_notes(mw.col, lookup, authoring.profiles_from_config(values))
                if not note_ids:
                    return showInfo("No matching Anki notes. Map your expression and reading fields in Shirabe's note settings.")
                browser = dialogs.open("Browser", mw)
                browser.search_for("nid:" + ",".join(str(nid) for nid in note_ids))
        except (authoring.AuthoringError, ValueError) as error:
            delegated = False
            showWarning(str(error))
        finally:
            if not delegated:
                AUTHORING_GATE.leave()

    QTimer.singleShot(0, open_native)
    return json.dumps({"ok": True})


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
        on_sign_in=open_sign_in,
        on_note_settings=open_note_settings,
        on_study_sync=open_study_sync,
    )
    dialog.exec()


def open_study_sync() -> None:
    if not mw.col:
        return showInfo("Open an Anki profile before configuring study sync.")
    from .sync_ui import StudySyncDialog
    from .sync_client import SyncError
    try:
        dialog = StudySyncDialog(mw, config().get("apiBase", "https://shirabe.org"))
    except (ValueError, OSError, SyncError) as error:
        return showWarning(str(error))
    dialog.exec()


def open_note_settings() -> None:
    from . import authoring
    authoring.configure_profiles(mw, config(), save_config)


def open_sign_in() -> None:
    dialog = QDialog(mw)
    dialog.setWindowTitle("Connect to Shirabe")
    dialog.resize(480, 360)
    layout = QVBoxLayout(dialog)
    web = AnkiWebView(dialog)
    layout.addWidget(web)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    web.stdHtml('<main id="shirabe-sign-in" style="padding:24px"></main>' +
                inject.attachment(config(), WEB_BASE, sign_in=True), context=dialog)
    try:
        dialog.exec()
    finally:
        web.cleanup()


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
    from . import template_ui
    template_ui.install_dialog(mw, config())


def remove_from_note_types() -> None:
    from . import template_ui
    template_ui.remove_dialog(mw)


def review_offline_storage() -> None:
    from . import offline_ui
    if mw.col is None:
        return showInfo("Open a collection before reviewing its offline storage.")
    offline_ui.review_cleanup(mw)


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
    note_settings = QAction("Note fields, deck and tags…", mw)
    note_settings.triggered.connect(open_note_settings)
    menu.addAction(note_settings)
    study_sync = QAction("Study sync…", mw)
    study_sync.triggered.connect(open_study_sync)
    menu.addAction(study_sync)
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
    storage = QAction("Review offline storage…", mw)
    storage.triggered.connect(review_offline_storage)
    menu.addAction(storage)
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
    if (Path(mw.pm.profileFolder()) / "shirabe-sync" / "state.sqlite3").exists():
        from .sync_session import session_for
        session_for(mw, settings.get("apiBase", "https://shirabe.org")).request()


def request_study_sync(changes=None, initiator=None) -> None:
    session = getattr(mw, "_shirabe_study_sync", None)
    if session:
        session.request(changes, initiator)


def close_study_sync() -> None:
    session = getattr(mw, "_shirabe_study_sync", None)
    if session:
        session.close()
        mw._shirabe_study_sync = None


def sync_before_profile_close(onsuccess, _old) -> None:
    from .sync_closing import unload_profile

    unload_profile(mw, onsuccess, _old)


def install_browser_menu(browser) -> None:
    from . import authoring_ui, offline_ui

    menu = QMenu("Shirabe", browser)
    for label, callback in (
        ("Fill missing fields in selected notes…", lambda: authoring_ui.enrich_browser_selection(browser, config(), save_config)),
        ("Prepare selected notes for offline lookup…", lambda: offline_ui.prepare_selected(browser, config(), save_config)),
        ("Note fields, deck and tags…", open_note_settings),
    ):
        action = QAction(label, browser)
        action.triggered.connect(callback)
        menu.addAction(action)
    browser.form.menubar.addMenu(menu)


def install_editor_button(buttons, editor):
    from . import authoring_ui

    buttons.append(editor.addButton(
        icon=None, cmd="shirabe-lookup", func=lambda current: authoring_ui.open_editor_lookup(current, config(), save_config),
        tip="Look up Japanese and fill note fields with Shirabe", label="調べる",
    ))
    return buttons


def _addon_version() -> str:
    meta = mw.addonManager.addon_meta(ADDON)
    return str(getattr(meta, "human_version", None) or "unknown")


gui_hooks.profile_did_open.append(on_profile_open)
mw.unloadProfile = wrap(mw.unloadProfile, sync_before_profile_close, "around")
gui_hooks.profile_will_close.append(close_study_sync)
gui_hooks.operation_did_execute.append(request_study_sync)
gui_hooks.sync_did_finish.append(request_study_sync)
gui_hooks.reviewer_did_answer_card.append(lambda *_: request_study_sync())
gui_hooks.state_did_change.append(lambda *_: request_study_sync())
gui_hooks.card_will_show.append(on_card_will_show)
gui_hooks.webview_did_receive_js_message.append(on_js_message)
gui_hooks.main_window_did_init.append(install_menu)
gui_hooks.browser_menus_did_init.append(install_browser_menu)
gui_hooks.editor_did_init_buttons.append(install_editor_button)
mw.addonManager.setConfigAction(__name__, on_configured)
