"""Marked template blocks and loaders for portable Anki cards."""

from __future__ import annotations

import json
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

CLIENT_ID = "shirabe-anki-card"

OPEN = "<!-- shirabe:start -->"
CLOSE = "<!-- shirabe:end -->"

# Everything between the markers, and nothing outside them, is ours. Non-greedy
# and DOTALL: a template can hold more than one of anything, and the block spans
# lines.
BLOCK = re.compile(re.escape(OPEN) + ".*?" + re.escape(CLOSE) + r"\n?", re.DOTALL)


def script_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def block(api_base: str, client_id: str, trigger: str, online_parsing: bool = False,
          assets: dict[str, str] | None = None, snapshot_field: bool = False) -> str:
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
    if assets is not None:
        return portable_block(api_base, client_id, trigger, online_parsing, assets, snapshot_field)
    return f"""{OPEN}
<script>
  (function () {{
    // The desktop add-on injects its own copy and sets this. There, it has the
    // reader's token in Python and this block would be the weaker of the two.
    if (window.__shirabe || window.__shirabeNativeRender) return;
    var open = function () {{
      if (window.__shirabe || window.__shirabeNativeRender) return;
      window.ShirabeCard.attach({{
        api: {{ apiBase: {script_json(api_base)} }},
        link: {{ clientId: {script_json(client_id)}, scope: "user:dictionary:read user:preferences:read user:study:read user:profile:read" }},
        parse: {script_json(online_parsing is True)},
        trigger: {script_json(trigger)},
        canWrite: false
      }});
    }};
    if (window.ShirabeCard) return open();
    if (window.__shirabeLoading) return;
    window.__shirabeLoading = true;
    var script = document.createElement("script");
    script.src = {script_json(bundle_url(api_base))};
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


def portable_block(api_base: str, client_id: str, trigger: str, online_parsing: bool,
                   assets: dict[str, str], snapshot_field: bool = False) -> str:
    settings = {
        "apiBase": api_base,
        "clientId": client_id,
        "trigger": trigger,
        "onlineParsing": online_parsing is True,
        "native": False,
        "bundle": assets["shirabe-card.js"],
        "canWrite": False,
    }
    reference = '<span hidden data-shirabe-snapshot>{{text:ShirabeLookup}}</span>' if snapshot_field else ""
    return f"""{OPEN}
{reference}
<script>
(function () {{
  if (window.__shirabe || window.__shirabeNativeRender) return;
  var settings = {script_json(settings)};
  var generation = {{}};
  window.__shirabePhone = generation;
  function open() {{
    if (window.__shirabe || window.__shirabeNativeRender || window.__shirabePhone !== generation) return;
    window.ShirabeAnki.open(settings);
  }}
  if (window.ShirabeAnki) return open();
  var script = document.createElement("script");
  script.src = {script_json(assets['attach.js'])};
  script.onload = open;
  script.onerror = function () {{
    script.remove();
    if (window.__shirabePhone !== generation) return;
    var message = document.createElement("p");
    message.setAttribute("role", "status");
    message.textContent = "Shirabe files are missing. Finish syncing media, then reopen this card.";
    (document.getElementById("qa") || document.body).appendChild(message);
  }};
  document.head.appendChild(script);
}})();
</script>
{CLOSE}"""


QUESTION_OPEN = "<!-- shirabe:question -->"
QUESTION_CLOSE = "<!-- shirabe:question-end -->"
QUESTION_BLOCK = re.compile(re.escape(QUESTION_OPEN) + ".*?" + re.escape(QUESTION_CLOSE) + r"\n?", re.DOTALL)
DETACH = '<script>window.__shirabePhone = null; if (window.ShirabeAnki) window.ShirabeAnki.detach(); window.__shirabe = null;</script>'


def with_question_guard(value: str) -> str:
    clean = QUESTION_BLOCK.sub("", value).rstrip()
    return clean + "\n" + QUESTION_OPEN + DETACH + QUESTION_CLOSE + "\n"


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


def with_block(template: str, api_base: str, client_id: str, trigger: str, online_parsing: bool = False,
               assets: dict[str, str] | None = None, snapshot_field: bool = False) -> str:
    """Add the block, or replace the one already there.

    Replacing rather than appending is what makes the menu item safe to run
    twice, and what upgrades a template written by an older version.
    """
    fresh = block(api_base, client_id, trigger, online_parsing, assets, snapshot_field)
    if BLOCK.search(template):
        return BLOCK.sub(lambda _: fresh, template, count=1)
    return template.rstrip() + "\n\n" + fresh + "\n"


def without_block(template: str) -> str:
    """Take it back out, leaving the reader's own template exactly as it was."""
    return QUESTION_BLOCK.sub("", BLOCK.sub("", template)).rstrip() + "\n"


def installed(template: str) -> bool:
    return bool(BLOCK.search(template))
