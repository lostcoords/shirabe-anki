// The add-on's half of the card.
//
// The card is the same one a phone runs, signing in the same way (`link.ts`).
// What only an add-on can add is the reader's own collection: "do I already
// know this word" is better answered from their notes than from a Shirabe
// account, and on Anki it is the answer that matters.
//
// Nothing here is a credential. The card holds its own, as a read-only client,
// and a webview shared with every deck in the collection is exactly why that
// client cannot be granted a write.
(function () {
  var settings = window.__shirabe || {};

  // The bundle is loaded here rather than by a second `<script src>` beside
  // this one. Anki re-executes the scripts in the HTML it writes by creating
  // fresh elements, and those load independently of document order: this file
  // could run before the bundle it needs, and did. Loading it explicitly is the
  // only ordering that holds. `template.py` does the same, for the same reason.
  if (window.ShirabeCard) {
    start();
  } else {
    var pending = document.createElement("script");
    pending.src = settings.bundle;
    pending.onload = start;
    document.head.appendChild(pending);
  }

  function start() {
    // Built here rather than by `attach`, because the collection answer below
    // replaces one of its methods. A link is a `fetch` with the reader on it,
    // so the host needs nothing else to carry an account.
    var link = ShirabeCard.createLink({
      apiBase: settings.apiBase,
      clientId: settings.clientId,
      storage: bridged() ? addonStorage() : undefined,
    });
    if (settings.signIn) return signInPanel(link);
    var host = ShirabeCard.apiHost({ apiBase: settings.apiBase, link: link });

    if (settings.useCollectionStates && bridged()) {
      host.study = Object.assign({}, host.study, { states: statesFromCollection });
    }

    // Reporting goes through Python, which owns the reader's opt-out. The card
    // itself never posts anywhere: it runs inside other people's decks.
    host.report = function (event, detail) {
      if (!bridged()) return;
      pycmd("shirabe:report:" + event + ":" + JSON.stringify(detail || {}));
    };

    ShirabeCard.attach({
      host: host,
      // Parse first: every word gets its own span, so hovering 強制的に asks
      // about 強制的 rather than about whatever starts under the pointer.
      // Scanning is the fallback the SDK takes when a host cannot cut text,
      // which is also what it does before the reader has signed in.
      parse: true,
      trigger: settings.trigger || "hover",
      // Reading only. The collection is where an Anki reader's reviews live,
      // and the client this signs in as is refused a write by the server
      // whatever it asks for. `attach` takes the write methods off the host.
      canWrite: false,
      options: { definitionSize: settings.definitionSize, theme: settings.theme },
    });
  }

  function signInPanel(link) {
    var panel = document.getElementById("shirabe-sign-in");
    var retry = document.createElement("button");
    retry.textContent = "Try again";
    retry.onclick = function () { signInPanel(link); };
    panel.textContent = "Starting sign-in…";
    link.signIn(function (message) {
      panel.replaceChildren(message);
    }).then(function () {
      panel.textContent = "Connected. You can close this window and return to your cards.";
    }).catch(function (error) {
      panel.textContent = error.message || "Couldn't connect. Please try again.";
      panel.appendChild(retry);
    });
  }

  // Where a desktop sign-in lives, because the browser's own storage cannot
  // hold one here: Anki serves its webviews from `http://127.0.0.1:<port>` with
  // a fresh port every launch, and storage is scoped to an origin, port
  // included. `inject.py` writes what Python is holding into the page, so
  // reading is synchronous, and writing hands it back to be kept.
  //
  // A phone gets none of this and needs none: its webview has a stable origin,
  // and no Python to ask.
  // IT MUST HONOUR THE KEY. `link.ts` keeps two things here: the reader's
  // session, and the device code of a sign-in they have started and not
  // finished. Python holds one opaque string, so the keys live inside it.
  // Ignoring them put both in one slot, and the pending code overwrote the
  // session: the card then reported itself signed in, sent a bearer that was
  // not one, and burned the server's unauthenticated rate limit instead of
  // offering the sign-in again.
  //
  // A value written by the older single-slot version parses to an object with
  // none of these keys, so it reads as signed out and the reader signs in once
  // more. That is the whole migration.
  function addonStorage() {
    var slots = {};
    try {
      var parsed = JSON.parse(settings.stored);
      if (parsed && typeof parsed === "object") slots = parsed;
    } catch (error) {
      slots = {};
    }
    function flush() { pycmd("shirabe:store:" + JSON.stringify(slots)); }
    return {
      getItem: function (key) {
        return Object.prototype.hasOwnProperty.call(slots, key) ? slots[key] : null;
      },
      setItem: function (key, value) {
        slots[key] = value;
        flush();
      },
      removeItem: function (key) {
        delete slots[key];
        flush();
      },
    };
  }

  // Whether there is a Python add-on on the other end of `pycmd`. There is on
  // the desktop client and there is not on AnkiDroid or AnkiMobile, which run
  // the card template without add-ons: everything here degrades there rather
  // than throwing or hanging.
  function bridged() {
    return typeof pycmd === "function";
  }

  // `pycmd` is Anki's bridge into Python. Its callback carries the answer, so a
  // request is a promise around one call.
  function statesFromCollection(words) {
    return new Promise(function (resolve) {
      // A bridge that never answers would leave this pending forever, and the
      // page paint waits on it. Answering null means "no states", which the
      // card already handles: the words render uncoloured rather than not at
      // all.
      var settled = false;
      var give = function (value) { if (!settled) { settled = true; resolve(value); } };
      setTimeout(function () { give(null); }, 3000);

      pycmd("shirabe:states:" + words.join(" "), function (answer) {
        var states = {};
        try {
          states = JSON.parse(answer || "{}");
        } catch (error) {
          give(null); // the card treats null as "no answer" and renders on
          return;
        }
        var out = {};
        Object.keys(states).forEach(function (word) {
          out[word] = { state: states[word], decks: [] };
        });
        give(out);
      });
    });
  }
})();
