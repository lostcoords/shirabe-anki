"""Native card settings and script-safe bootstrap markup."""

from __future__ import annotations

import json
import time

from . import template

MARKER = "shirabe-card-attached"


def card_settings(config: dict, card) -> dict:
    from .template_ui import settings_key

    values = {**config, "cardId": int(card.id)}
    saved = card.col.get_config(settings_key(card.note().mid))
    if isinstance(saved, dict):
        for key in ("answerOnly", "onlineParsing"):
            if isinstance(saved.get(key), bool):
                values[key] = saved[key]
    return values


def account_state(config: dict, now_ms: float | None = None) -> str:
    try:
        slots = json.loads(config.get("cardToken") or "{}")
        session = json.loads(slots.get("shirabe.link") or "{}")
        if isinstance(session.get("access"), str) and session["access"]:
            return "connected"
        pending = json.loads(slots.get("shirabe.pending") or "{}")
        if pending.get("expiresAt", 0) > (time.time() * 1000 if now_ms is None else now_ms):
            return "waiting"
    except (ValueError, TypeError, AttributeError):
        pass
    return "disconnected"


def attachment(config: dict, web_base: str, sign_in: bool = False) -> str:
    settings = {
        "signIn": sign_in,
        "trigger": config.get("trigger", "click"),
        "onlineParsing": config.get("onlineParsing") is True,
        "native": True,
        "cardId": config.get("cardId", 0),
        "snapshot": config.get("snapshot", ""),
        "apiBase": config.get("apiBase", "https://shirabe.org"),
        "clientId": template.CLIENT_ID,
        # `attach.js` loads this itself. Two `src` tags would be inserted
        # together and load independently, so the one that needs the bundle
        # could win the race against it, and did.
        "bundle": f"{web_base}/shirabe-card.js",
        # Anki's desktop loopback port changes between launches, so WebView
        # localStorage cannot retain the login at a predictable origin.
        "stored": config.get("cardToken", ""),
        "useCollectionStates": bool(config.get("useCollectionStates", True)),
        "definitionSize": config.get("definitionSize", "medium"),
        "theme": config.get("theme", "system"),
    }
    return (
        f'\n<script>window.__shirabe = {template.script_json(settings)};</script>'
        f'\n<script src="{web_base}/attach.js"></script>'
    )


def with_card(html: str, config: dict, web_base: str, kind: str = "reviewAnswer") -> str:
    """One card's HTML, with the reader attached to it.

    Idempotent: Anki re-renders the same card as you flip between question and
    answer, and a second copy of the bundle would fetch and evaluate it twice.
    """
    # Off means the card is not on the page at all, rather than on the page and
    # declining to open: no scripts, no parse request, no spans.
    if not config.get("enabled", True):
        return html
    if config.get("answerOnly", True) and kind.lower().endswith("question"):
        return html
    if MARKER in html:
        return html
    return html + f"<!-- {MARKER} -->" + attachment(config, web_base)
