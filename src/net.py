"""Every request this add-on makes goes out through here.

BECAUSE OF THE USER AGENT. `urllib` sends `Python-urllib/3.14` by default, and
Cloudflare answers that with 1010 (access denied) before the request reaches
anything behind it, `t.shirabe.org` included. Not a rule we can change: a
scripting library's default UA is what those rules are for, and naming ourselves
is both the fix and the honest thing to do.

There is one caller today (`analytics.py`), which is not a reason to inline it:
a missing header here fails silently and in production only, and the test that
no module builds its own `Request` is what keeps the next caller from
rediscovering that.
"""

from __future__ import annotations

import json
import urllib.request

VERSION = "0.1.0"
USER_AGENT = f"Shirabe-Anki/{VERSION} (+https://shirabe.org)"


def request(url: str, *, data: bytes | None = None, method: str = "GET", headers: dict | None = None):
    return urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
    )


def json_request(url: str, fields: dict | None = None, *, method: str = "POST", headers: dict | None = None):
    return request(
        url,
        data=json.dumps(fields).encode() if fields is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
