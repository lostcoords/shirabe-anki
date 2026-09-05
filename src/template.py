"""Putting Shirabe into a note type, so it works on a phone.

The desktop add-on injects the card at render time and nothing needs to be in
the note. AnkiDroid and AnkiMobile have no add-ons at all: the only thing that
crosses the sync is the note type itself, so on a phone the card can only come
from the TEMPLATE. That is what this writes.

WHY IT EDITS THE READER'S OWN NOTE TYPE rather than shipping one. Nobody restarts
their collection to try a dictionary popup: the decks people actually study came
from somewhere else, with note types they have already styled. Handing them a new
one means "re-make your deck first", which is not an offer. So the block goes
into whichever note type they point at, between two markers, and comes out again
without touching a line they wrote.

The script is loaded from a versioned CDN path rather than bundled into the
template, because a template is one string in a collection that syncs to every
device: shipping 90KB of JavaScript through it would bloat every note and pin
every phone to whatever we shipped that day.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# `v1` is the shape of the `attach` call below, not the build behind it. This
# string syncs into the reader's collection and stays there, so a fix ships by
# replacing the file at this path rather than by asking them to re-run anything.
# Bump it only when the call below changes in a way an old bundle cannot serve.
BUNDLE_PATH = "/card/v1/shirabe-card.js"
CDN = "https://cdn.shirabe.org" + BUNDLE_PATH
PRODUCTION = "https://shirabe.org"


def bundle_url(api_base: str) -> str:
    """Where the note type loads the card from.

    Production reads the CDN: it is cached at the edge and costs the app
    nothing. Every other server serves its own copy at the same path, which is
    what makes the phone path testable at all. Without it a note type synced
    from a dev machine points at a CDN that does not have that build, and the
    block fails the way it is designed to fail: silently.

    `bin/rails card_bundle:local` puts the file where this expects it.
    """
    base = api_base.rstrip("/")
    return CDN if base == PRODUCTION else base + BUNDLE_PATH

# Who the card says it is when it signs in, on a phone and on the desktop alike.
#
# READ-ONLY BY DESIGN, and the server enforces it: asked for a write scope, it
# grants `READ_DICTIONARY READ_SRS` and nothing more. The card keeps its token
# where the page can reach it, because on a phone there is nowhere else, and a
# webview is shared with every deck in the collection. So the token is worth
# nothing to steal: it reads a dictionary that is public and a word list that is
# the reader's own.
CLIENT_ID = "shirabe-anki-card"

OPEN = "<!-- shirabe:start -->"
CLOSE = "<!-- shirabe:end -->"

# Everything between the markers, and nothing outside them, is ours. Non-greedy
# and DOTALL: a template can hold more than one of anything, and the block spans
# lines.
BLOCK = re.compile(re.escape(OPEN) + ".*?" + re.escape(CLOSE) + r"\n?", re.DOTALL)


def block(api_base: str, client_id: str, trigger: str) -> str:
    """The lines that turn a rendered note into a reading surface.

    THE SCRIPT IS LOADED BY HAND, not with a `src` tag and not on
    `DOMContentLoaded`. Anki does not navigate to a card: it writes the rendered
    template into a page that has been loaded since the reviewer opened, so
    `DOMContentLoaded` fired long ago and a listener for it never runs. Loading
    it ourselves works whatever state the document is in, and `onload` is a
    signal that arrives either way.

    Rendering a card runs this again, and so does flipping one over. `attach`
    replaces the previous attachment; the loading flag prevents duplicate
    downloads while one is pending.
    """
    return f"""{OPEN}
<script>
  (function () {{
    // The desktop add-on injects its own copy and sets this. There, it has the
    // reader's token in Python and this block would be the weaker of the two.
    if (window.__shirabe) return;
    var open = function () {{
      if (window.__shirabe) return;
      window.ShirabeCard.attach({{
        api: {{ apiBase: "{api_base}" }},
        link: {{ clientId: "{client_id}" }},
        parse: true,
        trigger: "{trigger}",
        canWrite: false
      }});
    }};
    if (window.ShirabeCard) return open();
    if (window.__shirabeLoading) return;
    window.__shirabeLoading = true;
    var script = document.createElement("script");
    script.src = "{bundle_url(api_base)}";
    script.onload = function () {{
      window.__shirabeLoading = false;
      open();
    }};
    script.onerror = function () {{
      window.__shirabeLoading = false;
      script.remove();
    }};
    document.head.appendChild(script);
  }})();
</script>
{CLOSE}"""


# A phone reaching "localhost" reaches the phone. `.localhost` is the same
# promise by name (RFC 6761), which is what the dev proxy uses.
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def reaches_a_phone(api_base: str) -> bool:
    """Whether a phone could load the card from this address.

    The block has no error branch on purpose, so an address that resolves to
    nothing is silent: the reader syncs, taps a word, and nothing happens. This
    is the only place that can be caught, because by the time it is wrong it is
    inside a note type on someone else's device.
    """
    host = (urlsplit(api_base).hostname or "").lower()
    return host not in LOOPBACK and not host.endswith(".localhost")


def with_block(template: str, api_base: str, client_id: str, trigger: str) -> str:
    """Add the block, or replace the one already there.

    Replacing rather than appending is what makes the menu item safe to run
    twice, and what upgrades a template written by an older version.
    """
    fresh = block(api_base, client_id, trigger)
    if BLOCK.search(template):
        return BLOCK.sub(lambda _: fresh, template, count=1)
    return template.rstrip() + "\n\n" + fresh + "\n"


def without_block(template: str) -> str:
    """Take it back out, leaving the reader's own template exactly as it was."""
    return BLOCK.sub("", template).rstrip() + "\n"


def installed(template: str) -> bool:
    return bool(BLOCK.search(template))
