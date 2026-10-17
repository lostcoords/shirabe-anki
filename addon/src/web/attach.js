(function () {
  if (window.ShirabeAnki) {
    if (window.__shirabe) window.ShirabeAnki.open(window.__shirabe);
    return;
  }
  var settings = {};
  var attachment = null;
  var generation = 0;
  var loading = null;
  var runtime = null;

  function detach() {
    generation++;
    if (attachment) attachment.detach();
    attachment = null;
    document.querySelectorAll("[data-shirabe-notice]").forEach(function (notice) { notice.remove(); });
  }

  function signOut() {
    if (runtime) {
      if (runtime.host.account) runtime.host.account.signOut();
      else if (runtime.link.signOut) runtime.link.signOut();
    }
    runtime = null;
    (document.getElementById("qa") || document.body).querySelectorAll(".sb-word").forEach(function (word) {
      word.dataset.state = "none";
      ["cardId", "cardUrl", "deckNames", "decks", "intervals"].forEach(function (key) { delete word.dataset[key]; });
    });
    var next = Object.assign({}, settings, { stored: "" });
    if (next.signIn) return detach();
    return open(next);
  }

  function open(options) {
    detach();
    settings = options;
    var current = generation;
    function failed() {
      loading = null;
      if (current !== generation) return;
      showNotice("Shirabe couldn't load its popup files. Finish syncing media, then tap to retry.", function () { open(options); });
    }
    if (window.ShirabeCard) return start(current).catch(failed);
    if (!loading) {
      loading = new Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = options.bundle;
        script.onload = resolve;
        script.onerror = function () { script.remove(); reject(new Error("Popup files unavailable")); };
        document.head.appendChild(script);
      });
    }
    return loading.then(function () { return start(current); }).catch(failed);
  }

  function showNotice(message, retry) {
    var notice = document.createElement(retry ? "button" : "p");
    notice.setAttribute("data-shirabe-notice", "");
    notice.setAttribute("data-shirabe-ignore", "");
    notice.setAttribute("role", "status");
    notice.textContent = message;
    notice.style.cssText = "font:inherit;font-size:14px;padding:12px;margin:12px 0;border:1px solid currentColor;border-radius:8px;background:transparent;color:inherit";
    if (retry) {
      notice.type = "button";
      notice.style.cursor = "pointer";
      notice.onmouseenter = function () { notice.style.opacity = "0.7"; };
      notice.onmouseleave = function () { notice.style.opacity = "1"; };
      notice.onclick = function (event) { event.stopPropagation(); retry(); };
    }
    (document.getElementById("qa") || document.body).appendChild(notice);
  }

  async function start(current) {
    if (current !== generation) return;
    // Built here rather than by `attach`, because the collection answer below
    // replaces one of its methods. A link is a `fetch` with the reader on it,
    // so the host needs nothing else to carry an account.
    var key = JSON.stringify([settings.apiBase, settings.clientId, settings.stored, settings.native]);
    var link = runtime && runtime.key === key ? runtime.link : ShirabeCard.createLink({
      apiBase: settings.apiBase,
      clientId: settings.clientId,
      scope: "user:dictionary:read user:study:read",
      storage: bridged() ? addonStorage() : undefined,
    });
    if (settings.signIn) return signInPanel(link);
    var accountHost = runtime && runtime.key === key ? runtime.host : ShirabeCard.createAnkiHost({ apiBase: settings.apiBase, link: link });
    runtime = { key: key, link: link, host: accountHost };
    var host = Object.assign({}, accountHost);
    var root = document.getElementById("qa") || document.body;
    var reference = root.querySelector("[data-shirabe-snapshot]");
    var filename = settings.snapshot || (reference && reference.textContent.trim());
    var prepared = null;
    if (filename && ShirabeCard.loadSnapshot) {
      prepared = await ShirabeCard.loadSnapshot(filename);
      if (current !== generation) return;
      if (!prepared) showNotice("Offline definitions are missing or incomplete. Sync media or prepare this card again. Online lookup is still available when connected.");
    }
    if (prepared) {
      var fallback = Object.assign({}, host);
      if (settings.onlineParsing !== true) fallback.tokenize = undefined;
      host = ShirabeCard.snapshotHost(fallback, prepared, {
        contentVersion: prepared.contentVersion,
        apiBase: settings.apiBase,
        texts: ShirabeCard.collectReadableRuns(root),
      });
    }

    if (!(bridged() && Number.isSafeInteger(settings.cardId) && settings.cardId > 0) &&
        typeof AnkiDroidJS === "function" && typeof AnkiDroidJS.prototype.ankiSearchCard === "function") {
      if (host.resolvePending) {
        var connect = host.resolvePending.bind(host);
        host.resolvePending = function (onProgress) {
          return connect(function (message) {
            var panel = document.createElement("div");
            var setup = document.createElement("p");
            setup.textContent = "Before your first sign-in, enable AnkiDroid Settings → Advanced → localStorage in Study Screen, then restart AnkiDroid. Prepared offline definitions do not need a login.";
            panel.append(setup, message);
            onProgress(panel);
          });
        };
      }
    }

    if (settings.useCollectionStates && bridged()) {
      host.study = Object.assign({}, host.study, {
        states: statesFromCollection,
        source: "Anki collection",
        urlForWord: async function () { return undefined; },
      });
    }
    if (host.account) {
      var connected = host.account.connected && host.account.connected.bind(host.account);
      host.account = { signOut: signOut, connected: connected };
    }

    // Reporting goes through Python, which owns the reader's opt-out. The card
    // itself never posts anywhere: it runs inside other people's decks.
    host.report = function (event, detail) {
      if (!bridged()) return;
      pycmd("shirabe:report:" + event + ":" + JSON.stringify(detail || {}));
    };

    attachment = ShirabeCard.attach({
      root: root,
      host: host,
      parse: settings.onlineParsing === true || !!prepared,
      studyColors: settings.onlineParsing === true || !!(settings.useCollectionStates && bridged()),
      trigger: settings.trigger || "click",
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
  // A phone has no Python storage bridge. AnkiDroid needs its fixed-port
  // localStorage setting for the browser origin to survive a restart.
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
    return settings.native !== false && typeof pycmd === "function";
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
  window.ShirabeAnki = { open: open, detach: detach, signOut: signOut };
  if (window.__shirabe) open(window.__shirabe);
})();
