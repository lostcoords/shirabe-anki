# Shirabe for Anki

Hover a Japanese word on any card and get Shirabe's dictionary card over it:
reading, pitch, meanings, and what you already know about the word.

**Nothing to configure and no template to edit.** The add-on rewrites the card's
HTML as it is shown (`gui_hooks.card_will_show`), so it works on every note type
you already have, including ones you install later, and it changes nothing in
your collection.

## Which Anki it needs

**24.04 and later** (`min_point_version` 240400, in `manifest.json` and
`meta.json`). Anki refuses to load an add-on below its declared floor, and
`int_version()` reads modern versions as `YYMMPP`, so the old-looking `50` this
started with meant Anki 2.1.50 and promised support for a browser from 2022.

The floor is a CSS floor. The card is drawn by whatever Chromium the reader's
Anki bundles (25.09 is Qt WebEngine 6.9, which is Chromium 122), and the card
declares a baseline of Chromium 111 that it may not write past. Qt 6.6 was the
first Qt WebEngine on that Chromium, and Anki picked it up during the 24.x line:
24.04 is the floor with a margin rather than the exact release, because the Anki
to Qt map is not published anywhere we can check.

What enforces it is not this paragraph. `shirabe-sdk/test/css-baseline.test.ts`
fails if the card's stylesheet uses a feature newer than the declared baseline,
which is how `light-dark()` (Chromium 123) would have been caught before it made
every colour in the card invalid on a real reader's machine.

## Where it lives in Anki

**Tools → Shirabe**, with **Popup on cards** as a checkable item (the one switch
worth reaching without opening a window) and a **Settings…** window rather than Anki's JSON editor.
Half of what is stored is written by signing in rather than typed, and a text box
a refresh token can be pasted into is the thing the sign-in flows exist to avoid,
so the window shows the account as a state with a button and the preferences as
the three controls they are. Anki's own "Config" button on the add-on list opens
the same window, so there is one place these change instead of two that can
disagree.

## Connecting an account

Open **Tools → Shirabe → Settings → Sign in**. Follow the link in the connection
window, approve access in your browser, and return to Anki. Keep the connection
window open until it says Connected. If you closed it early, choose Continue
sign-in in Settings to resume. A failed request offers Try again.

On a phone, start from the Sign in link in a word popup. Each device signs in
separately; syncing a note type does not sync the desktop login.

Both entry points use the SDK's read-only device flow. The executable checks are
in [`link.test.ts`](../shirabe-sdk/test/link.test.ts) and
[`desktop-storage.test.ts`](../shirabe-sdk/test/desktop-storage.test.ts).

## What it is made of

The card itself is not written here. It is
[`shirabe-sdk`](../shirabe-sdk/README.md), the same card the browser extension
draws and the same one Nadeshiko will, loaded as one bundled script. This add-on
is the part only an add-on can do:

- **Put it on the card.** Wrap Japanese in the spans the card looks for, and
  attach it, with no help from the person reviewing.
- **Answer from your own collection.** An add-on can read your notes, so "you
  already have a card for this word" is answered by YOUR Anki deck rather than by
  a Shirabe account. Nothing else that renders this card can do that.

Lookups go to Shirabe's API, the way Nadeshiko's do. The offline dictionary pack
belongs to the browser extension, which already has the engine to read it;
re-implementing that in Python would be a second lookup engine to keep honest.

## Layout

```
src/__init__.py     what Anki loads: hooks, menu, the JS bridge
src/settings.py     the Settings window
src/collection.py   reading word states out of the user's own notes
src/inject.py       the HTML rewrite that puts the card on a rendered card
src/web/            the bundled card (built from ../../shirabe-sdk)
manifest.json       what AnkiWeb shows
```

## Pointing it at a different server

**Tools → Shirabe → Settings… → Advanced.** The window replaced Anki's JSON
editor, so everything reachable there has a control here.

For local work that means the Rails server directly rather than
`https://shirabe.localhost`: the dev proxy's certificate is signed by a CA that
neither Anki's Python nor its webview trusts, and the card does its own fetching
from the webview, so trusting it in one place would not be enough. `bin/dev`
prints the port, and `lsof -nP -iTCP -sTCP:LISTEN | grep ruby` finds it again
later. The port is assigned per run, so it changes when `bin/dev` restarts.

A server whose CA you do trust but Python does not (an internal one, or the dev
proxy) is what **Extra certificate** is for.

## What it reports

**Tools → Shirabe → Settings → Send anonymous crash and usage reports**, on by
default, off in one click.

It answers one question nothing else can: does this work on the reader's Anki?
The add-on runs inside a client we do not ship, on a Qt and a Chromium we do not
choose. A crash on 24.06 and a crash on 25.09 are different bugs, and without the
version attached they are one pile. So every event carries `anki_version`,
`qt_version` and the OS, and the events themselves are about the software: a card
opened, a lookup failed, a save was refused.

Never sent: a word, a note, a field, a deck, a card, or anything identifying the
reader. `distinct_id` is a random id minted into this add-on's own config, not
the Anki profile and not a sync account.

The card reports through Python rather than posting for itself, so there is one
sender, one token and one place the opt-out is honoured. Everything goes to
PostHog through `t.shirabe.org`, the same host the site and the browser
extension use.

## Developing it

The add-on folder Anki loads is a symlink to `src/`, so there is nothing to copy
and nothing to reinstall:

```sh
ln -s "$PWD/src" ~/Library/Application\ Support/Anki2/addons21/shirabe   # once
cd ../shirabe-sdk && npm run watch:anki                                   # rebuilds web/ on save
```

**What reloads and what does not**, because the difference will otherwise cost
you an afternoon:

| You changed                            | To see it                                            |
| -------------------------------------- | ---------------------------------------------------- |
| `web/*.js` (the card, `attach.js`)     | flip the card, or press `R` to replay it             |
| `config.json` via Anki's config screen | takes effect on the next card                        |
| any `.py` here                         | **restart Anki**: it imports add-ons once at startup |

The JS half is where nearly all the iteration is, and that half is free: Anki
re-reads the exported files on every card render, so a save and a card flip is
the whole loop. The Python half is three hooks that rarely change.

Its own tests need no Anki at all: `python3 tests/run.py`.

### Trying it on a phone

The note type is the only thing that reaches AnkiDroid and AnkiMobile, and it
loads the card from wherever `apiBase` points, so a phone needs an address it can
actually resolve:

```sh
bin/rails card_bundle:local                  # serve the bundle at /card/v1/
bin/rails server -b 0.0.0.0 -p 4900          # answer the network, not just loopback
```

Point **Settings… → Advanced → apiBase** at `http://<your LAN IP>:4900`, re-run
**Add to a note type…**, and sync.

**Not `<name>.local` through rb-portless.** `--lan` publishes that over mDNS,
which iOS resolves and Android does not, so Brave on a phone never finds the
host. The proxy routes by Host header and has no route for a bare IP, and
`alias` cannot make one that serves LAN clients, so the proxy has no way to
answer a phone that cannot resolve the name. Rails on `0.0.0.0` skips all of it.

Two more that cost an evening: a browser set to upgrade every request to HTTPS
will not load a plain-HTTP LAN server, and **Anki imports add-ons once at
startup**, so any change here needs a restart before the note type it writes
carries it.
