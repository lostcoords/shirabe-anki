"""The script tags that turn a rendered card into a reading surface.

The desktop half of getting the card onto a card. It runs on every note type
with no setup, which is the whole reason an add-on is worth installing: nothing
to add to a template, nothing to sync, and it covers decks the reader downloaded
and never opened.

The phone half is `template.py`, and the two agree on what they configure. What
they do not share is where the bundle comes from: here it is on disk, so the
card works with no network for everything that does not need the server.

NO TOKEN IS WRITTEN HERE, and one used to be. See the note at the top of
`__init__.py`: the card signs itself in now, as a client the server will not
give a write to.
"""

from __future__ import annotations

import json

from . import template

MARKER = "shirabe-card-attached"


def attachment(config: dict, web_base: str) -> str:
    settings = {
        "trigger": config.get("trigger", "hover"),
        "apiBase": config.get("apiBase", "https://shirabe.org"),
        "clientId": template.CLIENT_ID,
        # `attach.js` loads this itself. Two `src` tags would be inserted
        # together and load independently, so the one that needs the bundle
        # could win the race against it, and did.
        "bundle": f"{web_base}/shirabe-card.js",
        # WHERE THE SIGN-IN IS KEPT ON A DESKTOP, and why it is not
        # `localStorage`. Anki serves its webviews from `http://127.0.0.1:<port>`
        # and picks the port fresh every launch (`ANKI_API_PORT or 0`, in
        # `aqt/mediasrv.py`), and a browser scopes storage to an origin, port
        # included: whatever the card kept last time is under an origin that no
        # longer exists. Signing in would have to happen every time Anki opened.
        #
        # This is storage, not authentication. Nothing here reads the value,
        # renews it, or sends it anywhere: `attach.js` hands it to the card and
        # the card does the rest. A phone keeps its own, because a phone's
        # webview has a stable origin and no Python.
        "stored": config.get("cardToken", ""),
        "useCollectionStates": bool(config.get("useCollectionStates", True)),
        "definitionSize": config.get("definitionSize", "medium"),
        "theme": config.get("theme", "system"),
    }
    return (
        f'\n<script>window.__shirabe = {json.dumps(settings)};</script>'
        f'\n<script src="{web_base}/attach.js"></script>'
    )


def with_card(html: str, config: dict, web_base: str) -> str:
    """One card's HTML, with the reader attached to it.

    Idempotent: Anki re-renders the same card as you flip between question and
    answer, and a second copy of the bundle would fetch and evaluate it twice.
    """
    # Off means the card is not on the page at all, rather than on the page and
    # declining to open: no scripts, no parse request, no spans.
    if not config.get("enabled", True):
        return html
    if MARKER in html:
        return html
    return html + f"<!-- {MARKER} -->" + attachment(config, web_base)
