import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createLink } from "@shirabe-org/card/word-card/link";
import { Window } from "happy-dom";

// The Anki desktop add-on cannot use the browser's storage: Anki serves its
// webviews from a port it picks fresh each launch, and storage is scoped to an
// origin. So `attach.js` implements `Storage` over the add-on's config instead.
//
// That shim is the only implementation of this contract we write ourselves, and
// it was wrong in a way nothing here could see: it ignored the key. This runs it.
function desktopStorage(stored: string): Storage {
  const source = readFileSync("../../addon/src/web/attach.js", "utf8");
  const opens = source.indexOf("function addonStorage()");
  assert.ok(opens > 0, "found the shim");
  const body = source.slice(opens, source.indexOf("\n  }", opens) + 4);
  const build = new Function("settings", "pycmd", `${body}\nreturn addonStorage();`);
  return build({ stored }, () => {}) as Storage;
}

test("the desktop storage keeps the two things link.ts stores apart", () => {
  const store = desktopStorage("");

  store.setItem("shirabe.link", JSON.stringify({ access: "at_1", refresh: "rt_1", expiresAt: 1 }));
  store.setItem("shirabe.pending", JSON.stringify({ user_code: "DVE3-Q4PQ" }));

  assert.match(String(store.getItem("shirabe.link")), /at_1/,
    "starting a sign-in must not overwrite the session it is renewing");
  assert.match(String(store.getItem("shirabe.pending")), /DVE3-Q4PQ/);

  store.removeItem("shirabe.pending");
  assert.equal(store.getItem("shirabe.pending"), null);
  assert.match(String(store.getItem("shirabe.link")), /at_1/, "and finishing one must not sign the reader out");
});

test("an unknown key is absent rather than whatever was stored last", () => {
  const store = desktopStorage(JSON.stringify({ "shirabe.pending": "{}" }));

  assert.equal(store.getItem("shirabe.link"), null);
});

// The exact value found in a reader's Anki config after this bug: a pending
// device code in the session's slot. It must read as signed out, so the card
// offers the sign-in rather than sending a bearer that is not one.
test("a config written by the single-slot version recovers to signed out", () => {
  const stranded = JSON.stringify({
    device_code: "dc_x", user_code: "DVE3-Q4PQ",
    verification_uri: "http://127.0.0.1:4818/link", interval: 5, expiresAt: Date.now() + 600_000,
  });

  const link = createLink({
    apiBase: "http://127.0.0.1:4818",
    clientId: "shirabe-anki-card",
    storage: desktopStorage(stranded),
  });

  assert.equal(link.linked(), false);
});

function phoneLoader(portable = false) {
  const assets = portable ? "{'shirabe-card.js':'_card.js','attach.js':'_attach.js'}" : "None";
  const html = execFileSync("python3", ["-c", `import sys; sys.path.insert(0, '../../addon/src'); import template; print(template.block('https://shirabe.org', template.CLIENT_ID, 'click', assets=${assets}))`], { encoding: "utf8" });
  const source = html.match(/<script>([\s\S]*?)<\/script>/)![1]!;
  const scripts: Array<{ onload?: () => void; onerror?: () => void; src?: string; remove(): void }> = [];
  let attached = 0;
  const window: Record<string, unknown> = {};
  const document = {
    createElement: () => ({ remove() {} }),
    head: { appendChild: (script: typeof scripts[number]) => scripts.push(script) },
  };
  return {
    run: () => new Function("window", "document", source)(window, document),
    loaded: () => {
      window.ShirabeCard = { attach: () => { attached++; } };
      window.ShirabeAnki = { open: () => { attached++; } };
      scripts.at(-1)!.onload!();
    },
    window, scripts, attached: () => attached,
  };
}

test("a phone retries the SDK download on the next card after a network failure", () => {
  const loader = phoneLoader();
  loader.run();
  loader.scripts[0]!.onerror?.();
  loader.run();
  assert.equal(loader.scripts.length, 2);
  loader.loaded();
  assert.equal(loader.attached(), 1);
});

test("a late phone bundle cannot replace the desktop collection host", () => {
  const loader = phoneLoader();
  loader.run();
  loader.window.__shirabe = { useCollectionStates: true };
  loader.loaded();
  assert.equal(loader.attached(), 0);
});

test("a native render marker prevents both phone loaders from starting before the native host", () => {
  for (const portable of [false, true]) {
    const loader = phoneLoader(portable);
    loader.window.__shirabeNativeRender = true;
    loader.run();
    assert.equal(loader.scripts.length, 0);
    assert.equal(loader.attached(), 0);
  }
});

test("the portable phone bootstrap uses local media and rejects a late load from the previous side", () => {
  const loader = phoneLoader(true);
  loader.run();
  assert.equal(loader.scripts[0].src, "_attach.js");
  loader.window.__shirabePhone = null;
  loader.loaded();
  assert.equal(loader.attached(), 0);
  loader.run();
  assert.equal(loader.attached(), 1);
});

test("Anki's sign-in window redeems a pending approval through the SDK and saves it to Python", async () => {
  const window = new Window();
  const previousDocument = globalThis.document;
  Object.assign(globalThis, { document: window.document });
  window.document.body.innerHTML = '<main id="shirabe-sign-in"></main>';
  const settings = {
    signIn: true, apiBase: "https://shirabe.org", clientId: "shirabe-anki-card",
    stored: JSON.stringify({ "shirabe.pending": JSON.stringify({ device_code: "approved", expiresAt: Date.now() + 600_000 }) }),
  };
  const commands: string[] = [];
  const sdk = {
    createLink: (options: Parameters<typeof createLink>[0]) => createLink({ ...options, fetch: async () => new Response(JSON.stringify({ access_token: "connected", refresh_token: "refresh", expires_in: 3600 })) }),
    apiHost: () => { throw new Error("Signing in must not mount a dictionary host"); },
  };
  Object.assign(window, { __shirabe: settings, ShirabeCard: sdk });
  try {
    const source = readFileSync("../../addon/src/web/attach.js", "utf8");
    new Function("window", "document", "ShirabeCard", "pycmd", source)(window, window.document, sdk, (command: string) => commands.push(command));
    for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
    assert.match(window.document.body.textContent, /Connected/);
    const stored = JSON.parse(commands.filter((command) => command.startsWith("shirabe:store:")).at(-1)!.slice("shirabe:store:".length));
    assert.equal(JSON.parse(stored["shirabe.link"]).access, "connected");
    assert.equal(stored["shirabe.pending"], undefined);
  } finally {
    Object.assign(globalThis, { document: previousDocument });
    await window.happyDOM.close();
  }
});
