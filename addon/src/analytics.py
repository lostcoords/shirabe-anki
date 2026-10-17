"""What we can see about this add-on working, and nothing else.

The question this exists to answer is the one nothing else can: **does it work on
the reader's Anki?** The add-on runs inside a client we do not ship, on a Qt and
a Chromium we do not choose, across versions that change under us. A crash on
Anki 24.06 and a crash on 25.09 are different bugs, and without the version on
the report they are one indistinguishable pile.

Everything goes to PostHog, through `t.shirabe.org` rather than
`*.i.posthog.com`, because content blockers filter the latter by name and a
report that is silently swallowed is worse than none: the absence reads as "no
crashes". The project token is public by design, which is why it can sit in an
add-on people download.

WHAT IS NEVER SENT: a word, a note, a field, a deck name, a card, or anything
identifying the reader. The events here are about the software, not about the
person using it. `distinct_id` is a random id stored in this add-on's own config
and is not the Anki profile, not a sync account, and not stable across a
reinstall.
"""

from __future__ import annotations

import json
import platform
import threading
import urllib.error
import urllib.request
import uuid

from . import net

HOST = "https://t.shirabe.org"
TOKEN = "phc_pSuNmWggekTFkaciunGioJmRtrkL3TSe8XHi7jePmQKA"

# The same value the other surfaces send, so one chart can separate them.
SURFACE = "anki"


def capture(config: dict, event: str, properties: dict | None = None, on_config_change=None) -> None:
    """Send one event, off the UI thread, and never raise into the caller.

    Reporting is best-effort by definition: an add-on that fails a card render
    because a metrics host was slow has done more damage than the metric was
    ever worth.
    """
    if not config.get("reportUsage", True):
        return

    payload = {
        "api_key": TOKEN,
        "batch": [{
            "event": event,
            "properties": {
                "distinct_id": _install_id(config, on_config_change),
                "$lib": "shirabe-anki",
                "surface": SURFACE,
                **_platform(),
                **(properties or {}),
            },
        }],
    }
    threading.Thread(target=_post, args=(payload,), daemon=True).start()


def _post(payload: dict) -> None:
    request = net.json_request(f"{HOST}/batch/", payload)
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except (urllib.error.URLError, OSError, ValueError):
        # Never turn a failed report into anything the reader sees, and never
        # report the failure: that is how a loop starts.
        pass


def _install_id(config: dict, on_config_change=None) -> str:
    """A random id for this installation, minted once.

    Not the Anki profile name, not a sync account, not a machine id: none of
    those are ours to send, and a random id answers "how many installs" just as
    well as anything that could identify somebody.
    """
    existing = config.get("installId")
    if existing:
        return existing
    minted = uuid.uuid4().hex
    if on_config_change:
        on_config_change({**config, "installId": minted})
    return minted


def _platform() -> dict:
    """Where this is running, which is the whole point of reporting at all."""
    versions: dict[str, str] = {"os": platform.system(), "os_version": platform.release()}
    try:
        # Read from where Anki itself reads them (`aqt/main.py` imports the same
        # `buildinfo.version`, and `aqt.qt` is the shim that survives Qt moving),
        # rather than from an attribute that happens to exist today.
        from anki.buildinfo import version as anki_version  # noqa: PLC0415
        from aqt.qt import qtmajor, qtminor  # noqa: PLC0415

        versions["anki_version"] = anki_version
        versions["qt_version"] = f"{qtmajor}.{qtminor}"
    except Exception:
        # Outside Anki (a test, a lint) there is no version to report, and that
        # is not a reason to fail.
        pass
    return {k: v for k, v in versions.items() if v}
