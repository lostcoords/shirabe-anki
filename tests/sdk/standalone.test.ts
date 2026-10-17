import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { afterEach, test } from "node:test";
import { Window, type HTMLElement as HappyElement, type Element as HappyNodeElement } from "happy-dom";
import { fakeHost } from "./fake-host.js";
import { taberu } from "./fixtures.js";
import { legendTerm } from "@shirabe-org/card/word-card/legend";
import type { AttachOptions } from "@shirabe-org/card/standalone";
import type { Link } from "@shirabe-org/card/word-card/link";
import type { CardToken, LookupResult } from "@shirabe-org/card/word-card/host";

const window = new Window({ url: "https://example.test" });
Object.assign(globalThis, {
  window, document: window.document, HTMLElement: window.HTMLElement,
  Document: window.Document, Node: window.Node, NodeFilter: window.NodeFilter,
  DOMRect: window.DOMRect, CSS: window.CSS, AbortController: window.AbortController,
  getComputedStyle: window.getComputedStyle.bind(window),
});
const sdk = () => import("@shirabe-org/card/standalone").then((module) => module.default);
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const requestBody = async (input: string | URL | Request, init?: RequestInit): Promise<unknown> => {
  const body = input instanceof Request ? await input.clone().text() : String(init?.body ?? "");
  return body ? JSON.parse(body) : null;
};

test("a prepared definition renders before an unresolved online study-state read", async (t) => {
  const SDK = await sdk();
  const snapshot = JSON.parse(execFileSync("python3", ["../offline_fixture.py"], { encoding: "utf8" }));
  const calls: string[] = [];
  let release!: (response: Response) => void;
  const pending = new Promise<Response>(resolve => { release = resolve; });
  const link: Link = {
    linked: () => true, signIn: async () => {}, signOut() {},
    fetch: async (input) => {
      const path = new URL((input instanceof Request ? input.url : String(input))).pathname;
      calls.push(path);
      if (path === "/api/v1/dictionary-stack") return new Response(JSON.stringify({
        dictionaryStack: { fingerprint: "test", sources: [{ dictionary: "jmdict", languages: ["en"], reveal: "show" }] },
      }));
      assert.equal(path, "/api/v1/srs/states/lookup");
      return pending;
    },
  };
  const apiBase = "https://shirabe.test";
  const fallback = SDK.createAnkiHost({ apiBase, link });
  fallback.tokenize = undefined;
  const root = window.document.createElement("div");
  root.innerHTML = "<p>😀猫は猫</p><p>犬</p>";
  window.document.body.append(root);
  const host = SDK.snapshotHost(fallback, snapshot, { apiBase, contentVersion: snapshot.contentVersion,
    texts: SDK.collectReadableRuns(root as unknown as HTMLElement) });
  const attachment = SDK.attach({ root: root as unknown as HTMLElement, host, parse: true, studyColors: false,
    canWrite: false, connected: true, styles: false, trigger: "click", options: { lookupDelay: 0, hideAutomatically: false } });
  let before = "";
  const content = () => window.document.querySelector(".sb-host")?.shadowRoot?.querySelector(".sb-popup-content")?.textContent ?? "";
  try {
    await settle();
    assert.deepEqual(calls, [], "Offline wrapping must not fetch study state");
    attachment.show(root.querySelector(".sb-word") as unknown as HTMLElement);
    await settle();
    assert.deepEqual(calls, ["/api/v1/dictionary-stack", "/api/v1/srs/states/lookup"]);
    before = content();
  } finally {
    release(new Response(JSON.stringify({ states: [] })));
    await settle();
  }
  assert.match(content(), /\bcat\b/, "The prepared definition must render once the study request settles");
  t.diagnostic(JSON.stringify({ beforeStudySettled: before, definitionVisibleAfterStudySettled: /\bcat\b/.test(content()), calls }));
  assert.match(before, /\bcat\b/, "A cached definition must not wait for online study metadata");
});

test("the actual native collection study bridge bounds a missing callback at three seconds", async () => {
  const SDK = await sdk();
  const timers: Array<{ callback(): void; delay: number }> = [];
  const commands: string[] = [];
  let attached: AttachOptions | undefined;
  const nativeSDK = { ...SDK,
    createLink: () => ({ linked: () => false, fetch: async () => { throw new Error("Unexpected online read"); }, signIn: async () => {}, signOut() {} }),
    attach: (options: AttachOptions) => { attached = options; return { detach() {}, reread() {}, show() {} }; },
  };
  const view = { ShirabeCard: nativeSDK } as { ShirabeCard: typeof nativeSDK; ShirabeAnki?: { open(options: object): Promise<void> } };
  const execute = new Function("window", "document", "ShirabeCard", "pycmd", "setTimeout", readFileSync("../../addon/src/web/attach.js", "utf8"));
  execute(view, window.document, nativeSDK, (command: string) => commands.push(command),
    (callback: () => void, delay: number) => { timers.push({ callback, delay }); return timers.length; });
  await view.ShirabeAnki!.open({ apiBase: "https://shirabe.test", clientId: "anki", native: true, useCollectionStates: true });
  assert.ok(attached?.host?.study);
  let settled = false;
  const read = attached.host.study.states(["猫"]).then(result => { settled = true; return result; });
  await settle();
  assert.equal(settled, false);
  assert.deepEqual(commands, ["shirabe:states:猫"]);
  assert.deepEqual(timers.map(timer => timer.delay), [3000]);
  timers[0]!.callback();
  assert.equal(await read, null);
});

for (const studyColors of [false, true, undefined]) {
  test(`prepared wrapping keeps automatic study reads separate from explicit popup lookup (studyColors=${studyColors})`, async () => {
    const SDK = await sdk();
    const snapshot = JSON.parse(execFileSync("python3", ["../offline_fixture.py"], { encoding: "utf8" }));
    const calls: Array<{ path: string; body: unknown }> = [];
    const apiBase = "https://shirabe.test";
    const link = {
      linked: () => true, signIn: async () => {}, signOut() {},
      fetch: async (input: string | URL | Request, init?: RequestInit) => {
        const path = new URL((input instanceof Request ? input.url : String(input))).pathname;
        calls.push({ path, body: await requestBody(input, init) });
        if (path === "/api/v1/dictionary-stack") return new Response(JSON.stringify({
          dictionaryStack: { fingerprint: "test", sources: [{ dictionary: "jmdict", languages: ["en"], reveal: "show" }] },
        }));
        assert.equal(path, "/api/v1/srs/states/lookup", "Snapshot definitions must not fall back online");
        return new Response(JSON.stringify({ states: [{ lemma: "猫", state: "none" }] }));
      },
    } as Link;
    const fallback = SDK.createAnkiHost({ apiBase, link });
    fallback.tokenize = undefined;
    const root = window.document.createElement("div");
    root.innerHTML = '<p>😀<ruby>猫<rt>ねこ</rt></ruby>は猫</p><p>犬</p><p hidden>秘密</p>';
    window.document.body.append(root);
    const host = SDK.snapshotHost(fallback, snapshot, { apiBase, contentVersion: snapshot.contentVersion,
      texts: SDK.collectReadableRuns(root as unknown as HTMLElement) });
    const attachment = SDK.attach({ root: root as unknown as HTMLElement, host, parse: true, studyColors,
      connected: true, trigger: "click", styles: false, options: { lookupDelay: 0, hideAutomatically: false } });
    await settle();
    assert.equal(root.querySelectorAll(".sb-word").length, 3);
    if (studyColors !== false) {
      assert.deepEqual(calls.map(call => call.path), ["/api/v1/dictionary-stack", "/api/v1/srs/states/lookup"]);
      return;
    }
    assert.equal(calls.length, 0, "Preparing an offline snapshot must not read an account or upload its lemmas");
    attachment.reread();
    await settle();
    assert.equal(calls.length, 0, "Reread must preserve the automatic study-color opt-out");
    attachment.show(root.querySelector(".sb-word") as unknown as HTMLElement);
    await settle();
    assert.deepEqual(calls.map(call => call.path), ["/api/v1/dictionary-stack", "/api/v1/srs/states/lookup"]);
    assert.deepEqual(calls[1]!.body, { words: ["猫"] });
  });
}

test("fixture performance budgets reject latency, listener and request regressions", () => {
  const readableRoot = "document.getElementById('qa')";
  const measurement = () => ({
    mode: "desktop-mouse", flips: 100, hostLookups: 100,
    requestCounts: { "/api/v1/me": 1, "/api/v1/words/%E7%8C%AB": 1, "/api/v1/srs/states/lookup": 100 },
    retained: {
      attached: { window: ["click:capture"], document: [], [readableRoot]: ["click:capture"] },
      afterFlips: { window: ["click:capture"], document: [], [readableRoot]: ["click:capture"] },
      detached: { window: [], document: [], [readableRoot]: [] as string[] },
    },
    lookupEventToRenderedCard: {
      cold: { samples: 12, p50ms: 10, p95ms: 50, maxMs: 50 },
      warm: { samples: 12, p50ms: 8, p95ms: 25, maxMs: 25 },
    },
  });
  const check = (modes: ReturnType<typeof measurement>[]) => spawnSync(process.execPath,
    ["attach-browser.mjs", "--check-performance-report", "-"],
    { input: JSON.stringify({ modes }), encoding: "utf8", timeout: 5000 });
  const good = [measurement(), { ...measurement(), mode: "mobile-touch" }];
  assert.equal(check(good).status, 0);
  for (const change of [
    (mode: ReturnType<typeof measurement>) => { mode.lookupEventToRenderedCard.cold.p95ms = 51; },
    (mode: ReturnType<typeof measurement>) => { mode.lookupEventToRenderedCard.warm.p95ms = 26; },
    (mode: ReturnType<typeof measurement>) => { mode.lookupEventToRenderedCard.warm.samples = 1; },
    (mode: ReturnType<typeof measurement>) => { mode.lookupEventToRenderedCard.warm.p95ms = Number.NaN; },
    (mode: ReturnType<typeof measurement>) => { mode.flips = 99; },
    (mode: ReturnType<typeof measurement>) => { mode.hostLookups++; },
    (mode: ReturnType<typeof measurement>) => { mode.requestCounts["/api/v1/me"]++; },
    (mode: ReturnType<typeof measurement>) => { mode.requestCounts["/api/v1/srs/states/lookup"]++; },
    (mode: ReturnType<typeof measurement>) => { mode.retained.afterFlips.window.push("click:capture"); },
    (mode: ReturnType<typeof measurement>) => { mode.retained.detached[readableRoot].push("click:capture"); },
  ]) {
    const bad = structuredClone(good);
    change(bad[1]!);
    const result = check(bad);
    assert.equal(result.status, 1, `Budget accepted a regression: ${JSON.stringify(bad[1])}\n${result.stderr}`);
    assert.match(result.stderr, /AssertionError/, "The budget must fail, not command startup");
  }
  assert.equal(check([]).status, 1);
  assert.equal(check([good[0]!]).status, 1);
});

async function mount(options: Partial<AttachOptions> = {}) {
  const root = window.document.createElement("div");
  root.innerHTML = '<span class="sb-word" data-lemma="猫">猫</span>';
  window.document.body.append(root);
  const host = fakeHost({ study: false });
  const attachment = (await sdk()).attach({
    root: root as unknown as HTMLElement, host, parse: false, scan: false,
    styles: false, trigger: "click", ...options,
  });
  return { root, host, attachment };
}

afterEach(async () => {
  (await sdk()).detach();
  window.getSelection()?.removeAllRanges();
  window.document.body.replaceChildren();
});

function caretOn(node: globalThis.Node, offset = 0) {
  const doc = window.document;
  const caret = Object.getOwnPropertyDescriptor(doc, "caretPositionFromPoint");
  const rect = Object.getOwnPropertyDescriptor(window.Range.prototype, "getBoundingClientRect");
  Object.defineProperty(doc, "caretPositionFromPoint", { configurable: true, value: () => ({ offsetNode: node, offset }) });
  Object.defineProperty(window.Range.prototype, "getBoundingClientRect", { configurable: true, value: () => new window.DOMRect(10, 10, 20, 20) });
  return () => {
    if (caret) Object.defineProperty(doc, "caretPositionFromPoint", caret);
    else Reflect.deleteProperty(doc, "caretPositionFromPoint");
    if (rect) Object.defineProperty(window.Range.prototype, "getBoundingClientRect", rect);
    else Reflect.deleteProperty(window.Range.prototype, "getBoundingClientRect");
  };
}

function selectRange(start: HappyNodeElement, end = start, endOffset = end.firstChild!.textContent!.length) {
  const range = window.document.createRange();
  range.setStart(start.firstChild!, 0);
  range.setEnd(end.firstChild!, endOffset);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  window.document.dispatchEvent(new window.Event("selectionchange"));
  return range;
}

const selectionButton = () => window.document.querySelector(".sb-selection-host")?.shadowRoot?.querySelector("button");

test("selected text waits for an explicit lookup action and preserves ordinary selection", async () => {
  const { root, host } = await mount({ options: { lookupDelay: 0 } });
  root.innerHTML = '<p>前。<ruby>勉<rt>べん</rt></ruby><em>強する</em>。<span hidden>秘密</span></p>';
  const range = selectRange(root.querySelector("ruby")!, root.querySelector("em")!, 2);
  await settle();
  assert.deepEqual(host.journal.lookups, []);
  const button = selectionButton();
  assert.ok(button, "A visible explicit selection action must be offered");
  assert.equal(button.textContent, "Look up selection");
  const selection = window.getSelection()!.toString();
  const events: string[] = [];
  const onReview = (event: Event) => events.push(event.type);
  for (const type of ["pointerdown", "pointerup", "mousedown", "mouseup", "click"]) window.document.addEventListener(type, onReview as never, true);
  try {
    for (const type of ["pointerdown", "pointerup", "mousedown", "mouseup", "click"]) {
      button.dispatchEvent(new window.MouseEvent(type, { bubbles: true, composed: true, cancelable: true }));
    }
    await settle();
    assert.deepEqual(host.journal.lookups, ["勉強す"]);
    assert.equal(window.getSelection()!.toString(), selection);
    assert.equal(range.endOffset, 2);
    assert.deepEqual(events, [], "Selection lookup must not reveal or grade the reviewer");
  } finally {
    for (const type of ["pointerdown", "pointerup", "mousedown", "mouseup", "click"]) (window.document as unknown as Document).removeEventListener(type, onReview, true);
  }
});

for (const key of ["Enter", " "]) {
  test(`the selection action supports ${JSON.stringify(key)} without forwarding reviewer keys`, async () => {
    const { root, host } = await mount({ options: { lookupDelay: 0 } });
    selectRange(root.firstElementChild!);
    await settle();
    const button = selectionButton();
    assert.ok(button);
    const events: string[] = [];
    const onReview = (event: Event) => events.push(event.type);
    for (const type of ["keydown", "keyup"]) window.document.addEventListener(type, onReview as never, true);
    try {
      for (const type of ["keydown", "keyup"]) button.dispatchEvent(new window.KeyboardEvent(type, { key, bubbles: true, composed: true, cancelable: true }));
      await settle();
      assert.deepEqual(host.journal.lookups, ["猫"]);
      assert.equal(window.getSelection()!.toString(), "猫");
      assert.deepEqual(events, []);
    } finally {
      for (const type of ["keydown", "keyup"]) (window.document as unknown as Document).removeEventListener(type, onReview, true);
    }
  });
}

for (const change of ["detach", "replace", "hide", "reread"] as const) {
  test(`a stale selection action cannot look up a previous side after ${change}`, async () => {
    const { root, host, attachment } = await mount({ options: { lookupDelay: 0 } });
    selectRange(root.firstElementChild!);
    await settle();
    const button = selectionButton();
    assert.ok(button);
    if (change === "detach") attachment.detach();
    if (change === "replace") root.innerHTML = "犬";
    if (change === "hide") root.hidden = true;
    if (change === "reread") attachment.reread();
    await settle();
    button.click();
    await settle();
    assert.deepEqual(host.journal.lookups, []);
    assert.equal(selectionButton(), undefined);
  });
}

test("selection actions exclude hidden cloze boundaries, controls and overlong text", async () => {
  const { root, host } = await mount();
  for (const html of ['<p>猫<span hidden>秘密</span>犬</p>', '<button>猫</button>', `<p>${"猫".repeat(33)}</p>`]) {
    root.innerHTML = html;
    const range = window.document.createRange();
    range.selectNodeContents(root);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    window.document.dispatchEvent(new window.Event("selectionchange"));
    await settle();
    assert.equal(selectionButton(), undefined, html);
  }
  assert.deepEqual(host.journal.lookups, []);
});

test("explicit selection identifies the whole chosen span without parsing it again", async () => {
  const SDK = await sdk();
  const calls: Array<{ path: string; body: unknown }> = [];
  const kinds: Array<{ query: string; kind: unknown }> = [];
  const host = SDK.apiHost({ apiBase: "https://example.test", fetch: async (input, init) => {
    const path = new URL((input instanceof Request ? input.url : String(input))).pathname;
    calls.push({ path, body: await requestBody(input, init) });
    return new Response(JSON.stringify(path.endsWith("/parse") ? { stackFingerprint: "test", results: [{ tokens: [
      { surface: "勉強", lemma: "勉強", startOffset: 0, endOffset: 2, kind: "word" },
    ] }] } : { words: [{ candidates: [] }] }));
  } });
  const lookup = host.lookup.bind(host);
  host.lookup = (query, kind, context) => { kinds.push({ query, kind }); return lookup(query, kind, context); };
  const { root } = await mount({ host, options: { lookupDelay: 0 } });
  root.innerHTML = '<p><ruby>勉<rt>べん</rt></ruby><em>強する</em></p>';
  selectRange(root.querySelector("ruby")!, root.querySelector("em")!);
  await settle();
  assert.equal(calls.length, 0);
  selectionButton()!.click();
  await settle();
  assert.deepEqual(kinds, [{ query: "勉強する", kind: "word" }]);
  assert.equal(calls.filter(call => call.path.endsWith("/parse")).length, 0);
  assert.deepEqual(calls, [{ path: "/api/v1/words/identify", body: {
    tokens: [{ lemma: "勉強する", surface: "勉強する" }],
    include: ["pitch", "frequency", "furigana", "jlpt", "forms", "notes", "relations", "parts"],
  } }]);
});

test("Escape dismisses the selection action, and a fresh selection can offer it again", async () => {
  const { root, host } = await mount();
  selectRange(root.firstElementChild!);
  await settle();
  selectionButton()!.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, composed: true, cancelable: true }));
  assert.equal(selectionButton(), undefined);
  assert.equal(window.getSelection()!.toString(), "猫");
  window.getSelection()!.removeAllRanges();
  window.document.dispatchEvent(new window.Event("selectionchange"));
  selectRange(root.firstElementChild!);
  await settle();
  assert.ok(selectionButton(), "A new selection must not inherit the previous dismissal");
  assert.deepEqual(host.journal.lookups, []);
});

test("selecting text cancels the pending hover lookup until explicit activation", async () => {
  const { root, host } = await mount({ trigger: "hover", options: { lookupDelay: 25 } });
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  selectRange(root.firstElementChild!);
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.deepEqual(host.journal.lookups, [], "An earlier hover must not bypass explicit selection");
  assert.ok(selectionButton());
});

test("editing a later selected fragment cancels its pending response", async () => {
  let resolve!: (result: LookupResult) => void;
  const host = { ...fakeHost({ study: false }), lookup: () => new Promise<LookupResult>(done => { resolve = done; }) };
  const { root } = await mount({ host, options: { lookupDelay: 0 } });
  root.innerHTML = '<p><span>勉</span><em>強する</em></p>';
  selectRange(root.querySelector("span")!, root.querySelector("em")!);
  await settle();
  selectionButton()!.click();
  await settle();
  root.querySelector("em")!.textContent = "犬";
  await settle();
  resolve({ status: "found", definition: taberu });
  await settle();
  assert.equal(window.document.querySelector(".sb-host")?.shadowRoot?.querySelector(".sb-visible"), null);
});

test("raw text in click mode waits for a click", async () => {
  const { root, host } = await mount({ scan: true, options: { lookupDelay: 0 } });
  root.textContent = "猫";
  const restore = caretOn(root.firstChild as unknown as Node);
  try {
    root.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true }));
    await settle();
    assert.deepEqual(host.journal.lookups, [], "moving the pointer must not bypass the click trigger");
    root.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
    await settle();
    assert.deepEqual(host.journal.lookups, ["猫"]);
  } finally { restore(); }
});

for (const parse of [true, false]) {
  test(`a faded reviewer retries its initial read once when visible (parse=${parse})`, async () => {
    const root = window.document.createElement("div");
    root.style.opacity = "0";
    root.innerHTML = '<p>勉<ruby>強<rt>きょう</rt></ruby></p><p hidden>答え</p><button>操作</button><p style="opacity:0">秘密</p>';
    window.document.body.append(root);
    const parsed: string[] = [];
    let resolve!: (tokens: CardToken[]) => void;
    const host = { ...fakeHost({ study: false }), tokenize: (text: string) => {
      parsed.push(text);
      return new Promise<CardToken[]>(done => { resolve = done; });
    } };
    (await sdk()).attach({ root: root as unknown as HTMLElement, host, parse, styles: false });
    await settle();
    assert.deepEqual(parsed, [], "hidden reviewer content must not be uploaded");
    root.style.opacity = "0.2";
    await settle();
    assert.deepEqual(parsed, parse ? ["勉強"] : [], "the permitted initial read must resume after fade-in");
    for (const opacity of ["0.4", "0.8", "1"]) {
      root.style.opacity = opacity;
      await settle();
    }
    assert.deepEqual(parsed, parse ? ["勉強"] : [], "animation frames must not duplicate an in-flight parse");
    if (parse) resolve([{ surface: "勉強", lemma: "勉強", start: 0, length: 2 }]);
    await settle();
    assert.equal(root.querySelectorAll(".sb-word").length, parse ? 2 : 0);
    root.classList.add("settled");
    root.dispatchEvent(new window.Event("transitionend", { bubbles: true }));
    await settle();
    assert.deepEqual(parsed, parse ? ["勉強"] : []);
  });
}

for (const change of ["detach", "replace"] as const) {
  test(`a deferred visibility read is cancelled on ${change}`, async () => {
    const root = window.document.createElement("div");
    root.style.opacity = "0";
    root.innerHTML = "猫";
    window.document.body.append(root);
    const parsed: string[] = [];
    const host = { ...fakeHost({ study: false }), tokenize: async (text: string) => { parsed.push(text); return []; } };
    const attachment = (await sdk()).attach({ root: root as unknown as HTMLElement, host, parse: true, styles: false });
    await settle();
    if (change === "detach") attachment.detach();
    else root.innerHTML = "犬";
    root.style.opacity = "1";
    root.dispatchEvent(new window.Event("transitionend", { bubbles: true }));
    await settle();
    assert.deepEqual(parsed, []);
  });
}

test("a document attachment defers against its hidden body", async () => {
  const body = window.document.body;
  body.style.opacity = "0";
  body.textContent = "猫";
  const parsed: string[] = [];
  try {
    const host = { ...fakeHost({ study: false }), tokenize: async (text: string) => { parsed.push(text); return []; } };
    (await sdk()).attach({ host, parse: true, styles: false });
    await settle();
    assert.deepEqual(parsed, []);
    body.style.opacity = "1";
    await settle();
    assert.deepEqual(parsed, ["猫"]);
    body.dispatchEvent(new window.Event("transitionend", { bubbles: true }));
    await settle();
    assert.deepEqual(parsed, ["猫"], "an empty parse result must not cause an automatic retry loop");
  } finally { body.style.removeProperty("opacity"); }
});

test("web and Anki attach preserve the same parse input and resolve Spanish senses on lookup", async () => {
  const SDK = await sdk();
  const observed: unknown[] = [];
  for (const anki of [false, true]) {
    const calls: Array<{ path: string; body: unknown }> = [];
    const detail = {
      id: "勉強", dictionary: "personal", sourceId: "mine:1", headword: "勉強", reading: "ベンキョウ",
      common: false, forms: [], pitch: [], entries: [{
        dictionary: "personal", type: "definition", sourceId: "mine:1", senses: [{
          position: 3, definitions: [{ lang: "en", text: "study" }, { lang: "es", text: "estudio" }], tags: [],
        }],
      }],
    };
    const http: typeof fetch = async (input, init) => {
      const url = new URL((input instanceof Request ? input.url : String(input)));
      calls.push({ path: `${url.pathname}${url.search}`, body: await requestBody(input, init) });
      let payload: unknown = { words: [{ candidates: [detail] }] };
      if (url.pathname.endsWith("/dictionary-stack")) payload = {
        dictionaryStack: { fingerprint: "test", sources: [{ dictionary: "personal", languages: ["es", "en"], reveal: "hover" }] },
      };
      else if (url.pathname.endsWith("/parse")) payload = { stackFingerprint: "test", results: [{ tokens: [
        { surface: "😀", startOffset: 0, endOffset: 2, kind: "symbol" },
        { surface: "勉強", lemma: "勉強", reading: "ベンキョウ", startOffset: 2, endOffset: 4, kind: "word" },
        { surface: "する。", startOffset: 4, endOffset: 7, kind: "symbol" },
      ] }] };
      else if (url.pathname.endsWith("/states/lookup")) payload = { states: [] };
      return new Response(JSON.stringify(payload));
    };
    const link: Link = { linked: () => true, fetch: http, signIn: async () => {}, signOut() {} };
    const apiBase = "https://example.test";
    const host = anki ? SDK.createAnkiHost({ apiBase, link }) : SDK.apiHost({ apiBase, fetch: http, languages: ["spa", "eng"] });
    const root = window.document.createElement("div");
    root.innerHTML = '<p>😀勉<ruby><em>強</em><rt>きょう</rt></ruby>する。</p><p hidden>答え</p>';
    window.document.body.append(root);
    const original = root.querySelector("em");
    const texts = SDK.collectReadableRuns(root as unknown as HTMLElement);
    const attachment = SDK.attach({ root: root as unknown as HTMLElement, host, parse: true, styles: false, trigger: "click" });
    await settle();
    const fragments = [...root.querySelectorAll(".sb-word")];
    assert.equal(fragments.length, 2);
    assert.equal(root.querySelector("em"), original);
    for (const fragment of fragments) {
      assert.equal(fragment.getAttribute("data-entry"), null, "parse supplies spans without dictionary entry IDs");
      assert.equal(fragment.getAttribute("data-surface"), "勉強");
      attachment.show(fragment as unknown as HTMLElement);
      await settle();
      assert.match(window.document.querySelector(".sb-host")!.shadowRoot!.textContent!, /estudio/);
    }
    const found = await host.lookupEntry("personal:mine:1", "勉強");
    assert.equal(found.status, "found");
    if (found.status !== "found") throw new Error("Expected the pinned private entry");
    assert.equal(found.definition?.senses[0]?.covered, anki ? true : undefined);
    observed.push({
      texts, parse: calls.filter(call => new URL(call.path, apiBase).pathname === "/api/v1/parse"),
      pinned: calls.filter(call => call.path.includes("/words/")),
      entry: found.definition?.entry, ref: found.definition?.senses[0]?.ref, glosses: found.definition?.senses[0]?.glosses,
    });
    attachment.detach();
    root.remove();
  }
  assert.deepEqual(observed[1], observed[0]);
  assert.deepEqual(observed[0], {
    texts: ["😀勉強する。"], parse: [{ path: "/api/v1/parse", body: { texts: ["😀勉強する。"] } }],
    pinned: [
      { path: "/api/v1/words/identify", body: {
        tokens: [{ lemma: "勉強", surface: "勉強", reading: "ベンキョウ" }],
        include: ["pitch", "frequency", "furigana", "jlpt", "forms", "notes", "relations", "parts"],
      } },
      { path: "/api/v1/words/identify", body: {
        tokens: [{ lemma: "勉強", surface: "勉強" }],
        include: ["pitch", "frequency", "furigana", "jlpt", "forms", "notes", "relations", "parts"],
      } },
    ],
    entry: "personal:mine:1", ref: "personal#3", glosses: [{ text: "estudio", lang: "spa", tag: "ES" }, { text: "study", lang: "eng", tag: "EN" }],
  });
});

test("raw modifier lookup waits for the modifier, including a stationary pointer", async () => {
  const { root, host } = await mount({ scan: true, trigger: "alt", options: { lookupDelay: 0 } });
  root.textContent = "猫";
  const restore = caretOn(root.firstChild as unknown as Node);
  try {
    root.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true }));
    await settle();
    assert.deepEqual(host.journal.lookups, []);
    window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Alt", altKey: true }));
    await settle();
    assert.deepEqual(host.journal.lookups, ["猫"]);
  } finally { restore(); }
});

test("a tap opens once in click mode and a second tap closes without revealing the answer", async () => {
  const { root, host } = await mount();
  const word = root.firstElementChild!;
  const events: string[] = [];
  const onReview = (event: { type: string }) => events.push(event.type);
  for (const type of ["pointerdown", "pointerup", "mousedown", "mouseup", "click"]) root.addEventListener(type, onReview);
  const tap = () => {
    word.dispatchEvent(new window.PointerEvent("pointerdown", { bubbles: true, cancelable: true, pointerType: "touch", pointerId: 1 }));
    word.dispatchEvent(new window.PointerEvent("pointerup", { bubbles: true, cancelable: true, pointerType: "touch", pointerId: 1 }));
    word.dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    word.dispatchEvent(new window.MouseEvent("mouseup", { bubbles: true, cancelable: true }));
    word.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
  };
  tap();
  await settle();
  assert.deepEqual(host.journal.lookups, ["猫"]);
  assert.deepEqual(events, []);
  tap();
  await settle();
  assert.equal(window.document.querySelector(".sb-host")!.shadowRoot!.querySelector(".sb-visible"), null);
  assert.deepEqual(host.journal.lookups, ["猫"]);
});

test("touch scrolling and cancelled pointers never open a word", async () => {
  const { root, host } = await mount({ trigger: "hover" });
  const word = root.firstElementChild!;
  word.dispatchEvent(new window.PointerEvent("pointerdown", { bubbles: true, pointerType: "touch", pointerId: 1, clientY: 10 }));
  const move = new window.PointerEvent("pointermove", { bubbles: true, cancelable: true, pointerType: "touch", pointerId: 1, clientY: 60 });
  word.dispatchEvent(move);
  word.dispatchEvent(new window.PointerEvent("pointerup", { bubbles: true, pointerType: "touch", pointerId: 1, clientY: 60 }));
  await settle();
  assert.equal(move.defaultPrevented, false, "scrolling remains native");
  assert.deepEqual(host.journal.lookups, []);
  word.dispatchEvent(new window.PointerEvent("pointerdown", { bubbles: true, pointerType: "touch", pointerId: 2 }));
  word.dispatchEvent(new window.PointerEvent("pointercancel", { bubbles: true, pointerType: "touch", pointerId: 2 }));
  word.dispatchEvent(new window.PointerEvent("pointerup", { bubbles: true, pointerType: "touch", pointerId: 2 }));
  await settle();
  assert.deepEqual(host.journal.lookups, []);
});

test("a raw tap survives delayed compatibility mouse events after its popup opens", async () => {
  const { root, host } = await mount({ scan: true, options: { lookupDelay: 0 } });
  root.textContent = "猫";
  const restore = caretOn(root.firstChild as unknown as Node);
  try {
    root.dispatchEvent(new window.PointerEvent("pointerdown", { bubbles: true, cancelable: true, pointerType: "touch", pointerId: 1 }));
    root.dispatchEvent(new window.PointerEvent("pointerup", { bubbles: true, cancelable: true, pointerType: "touch", pointerId: 1 }));
    await settle();
    const popup = window.document.querySelector(".sb-host")!.shadowRoot!;
    assert.ok(popup.querySelector(".sb-visible"));
    for (const type of ["mousedown", "mouseup", "click"]) {
      root.dispatchEvent(new window.MouseEvent(type, { bubbles: true, cancelable: true }));
    }
    await settle();
    assert.ok(popup.querySelector(".sb-visible"), "compatibility mousedown must not dismiss a completed raw tap");
    assert.deepEqual(host.journal.lookups, ["猫"]);
  } finally { restore(); }
});

test("wrapped words are keyboard reachable without forwarding activation to the reviewer", async () => {
  const { root, host } = await mount();
  const word = root.firstElementChild as HappyElement;
  assert.equal(word.tabIndex, 0);
  assert.equal(word.getAttribute("role"), "button");
  word.focus();
  const keys: string[] = [];
  root.addEventListener("keydown", (event) => keys.push((event as unknown as KeyboardEvent).key));
  root.addEventListener("keyup", (event) => keys.push((event as unknown as KeyboardEvent).key));
  for (const type of ["keydown", "keyup"]) {
    const event = new window.KeyboardEvent(type, { bubbles: true, cancelable: true, key: " " });
    word.dispatchEvent(event);
    assert.equal(event.defaultPrevented, true);
  }
  await settle();
  assert.deepEqual(host.journal.lookups, ["猫"]);
  assert.deepEqual(keys, []);
  const grade = new window.KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "1" });
  root.dispatchEvent(grade);
  assert.equal(grade.defaultPrevented, false);
  assert.deepEqual(keys, ["1"]);
});

test("native controls and selected text retain their normal clicks", async () => {
  const { root, host } = await mount({ scan: true, options: { lookupDelay: 0 } });
  root.innerHTML = '<button><span class="sb-word" data-lemma="猫">猫</span></button><span class="sb-word" data-lemma="犬">犬</span>';
  const button = root.querySelector("button")!;
  let clicked = 0;
  button.addEventListener("click", () => clicked++);
  const click = new window.MouseEvent("click", { bubbles: true, cancelable: true });
  button.firstElementChild!.dispatchEvent(click);
  assert.equal(clicked, 1);
  assert.equal(click.defaultPrevented, false);
  const word = root.lastElementChild!;
  const range = window.document.createRange();
  range.selectNodeContents(word);
  window.getSelection()!.addRange(range);
  word.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
  await settle();
  assert.deepEqual(host.journal.lookups, []);
});

test("mouse pointer gestures are contained before reviewer handlers run", async () => {
  const { root, host } = await mount();
  const word = root.firstElementChild!;
  const leaked: string[] = [];
  for (const type of ["pointerdown", "pointerup", "click"]) {
    root.addEventListener(type, () => leaked.push(type));
    word.dispatchEvent(new window.PointerEvent(type, { bubbles: true, pointerType: "mouse" }));
  }
  await settle();
  assert.deepEqual(leaked, []);
  assert.deepEqual(host.journal.lookups, ["猫"]);
});

test("a cancelled gesture does not disable the next tap", async () => {
  const { root, host } = await mount();
  const word = root.firstElementChild!;
  for (const [type, id] of [["pointerdown", 1], ["pointercancel", 1], ["pointerdown", 2], ["pointerup", 2]] as const) {
    word.dispatchEvent(new window.PointerEvent(type, { bubbles: true, pointerType: "touch", pointerId: id }));
  }
  await settle();
  assert.deepEqual(host.journal.lookups, ["猫"]);
});

test("a two-finger gesture cannot become a lookup on the last pointerup", async () => {
  const { root, host } = await mount();
  const word = root.firstElementChild!;
  for (const [type, id] of [["pointerdown", 1], ["pointerdown", 2], ["pointerup", 1], ["pointerup", 2]] as const) {
    word.dispatchEvent(new window.PointerEvent(type, { bubbles: true, pointerType: "touch", pointerId: id }));
  }
  await settle();
  assert.deepEqual(host.journal.lookups, []);
});

test("tapping a nested root does not leave the outer root's gesture unfinished", async () => {
  const outer = await mount();
  const inner = await mount();
  outer.root.append(inner.root);
  for (const root of [inner.root, outer.root]) {
    for (const type of ["pointerdown", "pointerup"]) root.firstElementChild!.dispatchEvent(new window.PointerEvent(type, { bubbles: true, pointerType: "touch", pointerId: 1 }));
    await settle();
  }
  assert.deepEqual(inner.host.journal.lookups, ["猫"]);
  assert.deepEqual(outer.host.journal.lookups, ["猫"]);
});

test("raw scans keep their field origin and never include hidden answer text", async () => {
  let remembered: Element | null | undefined;
  let sentence: string | undefined;
  const host = { ...fakeHost({ study: false }), rememberOrigin(word: HTMLElement, origin: Element | null | undefined) {
    remembered = origin;
    sentence = word.dataset.sentence;
  } };
  const { root } = await mount({ host, scan: true, skip: ".excluded", options: { lookupDelay: 0 } });
  root.innerHTML = '<p>猫<span hidden>秘密</span><span class="excluded">秘密</span></p>';
  const line = root.firstElementChild!;
  const restore = caretOn(line.firstChild as unknown as Node);
  try {
    line.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
    assert.deepEqual(host.journal.lookups, ["猫"]);
    assert.equal(sentence, "猫");
    assert.equal(remembered, line);
  } finally { restore(); }
});

test("raw lookup joins mixed inline text and either parsed fragment resolves the complete token", async () => {
  const host = { ...fakeHost({ study: false }), tokenize: async () => [
    { surface: "勉強", lemma: "勉強", reading: "ベンキョウ", entry: "jmdict:study", start: 0, length: 2 },
  ] };
  const { root } = await mount({ host, scan: true, options: { lookupDelay: 0 } });
  root.innerHTML = '<p>勉<span><ruby>強<rt>きょう</rt></ruby></span>する。</p>';
  const line = root.firstElementChild!;
  const restore = caretOn(line.firstChild as unknown as Node);
  try {
    line.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
    assert.deepEqual(host.journal.lookups, ["勉強する。"]);
  } finally { restore(); }
  (await sdk()).attach({ root: root as unknown as HTMLElement, host, parse: true, styles: false, trigger: "click" });
  await settle();
  const fragments = [...root.querySelectorAll(".sb-word")];
  assert.equal(fragments.length, 2);
  for (const fragment of fragments) {
    fragment.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
  }
  assert.deepEqual(host.journal.lookups, ["勉強する。", "jmdict:study", "jmdict:study"]);
});

test("a raw scan can move its highlight from the caret back to the whole word", async () => {
  const { scanTargetAt } = await import("@shirabe-org/card/word-card/scan");
  const { root } = await mount({ options: { lookupDelay: 0 } });
  root.innerHTML = "<p>勉<a>強</a>する。</p>";
  const tail = root.querySelector("p")!.lastChild as unknown as Node;
  const restore = caretOn(tail, 1);
  try {
    let target!: NonNullable<ReturnType<typeof scanTargetAt>>;
    const line = root.querySelector("p")!;
    line.addEventListener("mousemove", (event) => {
      target = scanTargetAt(event as unknown as MouseEvent, undefined, root as unknown as HTMLElement)!;
    }, { once: true });
    line.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true }));
    assert.ok(target);
    assert.equal(target?.targetStart, 3);
    assert.equal(target?.at(2)?.node, tail);
    assert.equal(target?.at(2)?.offset, 0);
    assert.equal(target?.at(1)?.node, root.querySelector("a")!.firstChild);
  } finally { restore(); }
});

test("sign-in from the previous side cannot parse the new contents of a reused root", async () => {
  let signedIn = false;
  let approve!: () => void;
  const parsed: string[] = [];
  const host = { ...fakeHost({ study: false }),
    async tokenize(text: string) { parsed.push(text); return []; },
    async lookup(): Promise<LookupResult> { return signedIn ? { status: "found", definition: null } : { status: "pending", message: "Sign in" }; },
    resolvePending: () => new Promise<void>((resolve) => { approve = () => { signedIn = true; resolve(); }; }),
  };
  const { root, attachment } = await mount({ host, parse: true, scan: true, options: { lookupDelay: 0 } });
  root.textContent = "猫";
  attachment.reread();
  await settle();
  const restore = caretOn(root.firstChild as unknown as Node);
  try {
    root.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
    root.textContent = "犬";
    await settle();
    approve();
    await settle();
    assert.deepEqual(parsed, ["猫"]);
  } finally { restore(); }
});

test("an excluded or hidden wrapped word never opens", async () => {
  const { root, host } = await mount({ skip: ".excluded" });
  root.innerHTML = '<span class="excluded"><span class="sb-word" data-lemma="猫">猫</span></span><span hidden><span class="sb-word" data-lemma="犬">犬</span></span>';
  for (const word of root.querySelectorAll(".sb-word")) word.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.deepEqual(host.journal.lookups, []);
});

test("replacing the contents of a persistent review root dismisses the previous card", async () => {
  const { root } = await mount();
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  root.innerHTML = '<span class="sb-word" data-lemma="犬">犬</span>';
  await settle();
  assert.equal(window.document.querySelector(".sb-host")!.shadowRoot!.querySelector(".sb-visible"), null);
});

test("the standalone bundle exposes the prepared snapshot host", async () => {
  assert.equal(typeof (await sdk()).snapshotHost, "function");
  assert.equal(typeof (await sdk()).loadSnapshot, "function");
  assert.equal(typeof (await sdk()).collectReadableRuns, "function");
  assert.equal(typeof (await sdk()).createAnkiHost, "function");
});

test("the extension's direct scanner can still hover a Japanese link", async () => {
  const { scanPointer } = await import("@shirabe-org/card/word-card/scan");
  const { root, host, attachment } = await mount({ options: { lookupDelay: 0 } });
  attachment.show(root.firstElementChild as unknown as HTMLElement);
  await settle();
  attachment.detach();
  root.innerHTML = '<a href="/word">犬</a>';
  const link = root.firstElementChild!;
  const restore = caretOn(link.firstChild as unknown as Node);
  const event = new window.MouseEvent("mousemove", { bubbles: true });
  link.addEventListener("mousemove", () => scanPointer(event as unknown as MouseEvent));
  try {
    link.dispatchEvent(event);
    await settle();
    assert.deepEqual(host.journal.lookups, ["猫", "犬"]);
  } finally { restore(); }
});

test("rereading a card cancels the earlier parse and popup response", async () => {
  const cuts: Array<(tokens: CardToken[]) => void> = [];
  let answer!: (result: LookupResult) => void;
  const host = { ...fakeHost({ study: false }),
    tokenize: () => new Promise<CardToken[]>((resolve) => cuts.push(resolve)),
    lookup: () => new Promise<LookupResult>((resolve) => { answer = resolve; }),
  };
  const { root, attachment } = await mount({ host, parse: true });
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  root.innerHTML = "犬";
  attachment.reread();
  attachment.reread();
  cuts[0]!([{ surface: "犬", lemma: "old", start: 0, length: 1 }]);
  cuts[1]!([{ surface: "犬", lemma: "new", start: 0, length: 1 }]);
  answer({ status: "found", definition: taberu });
  await settle();
  assert.equal(root.querySelector<HappyElement>(".sb-word")!.dataset.lemma, "new");
  assert.equal(window.document.querySelector(".sb-host")!.shadowRoot!.querySelector(".sb-visible"), null);
});

test("removing a review root dismisses its pending popup", async () => {
  let answer!: (result: LookupResult) => void;
  const host = { ...fakeHost({ study: false }), lookup: () => new Promise<LookupResult>((resolve) => { answer = resolve; }) };
  const { root } = await mount({ host });
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  root.remove();
  await settle();
  answer({ status: "found", definition: taberu });
  await settle();
  assert.equal(window.document.querySelector(".sb-host")!.shadowRoot!.querySelector(".sb-visible"), null);
});

for (const parse of [true, false]) {
  test(`sign-in retries permitted parsing (parse=${parse})`, async () => {
    let signedIn = false;
    const parsed: string[] = [];
    const host = { ...fakeHost({ study: false }),
      async tokenize(text: string) {
        parsed.push(text);
        if (!signedIn) throw new Error("Sign in");
        return [{ surface: text, lemma: text, start: 0, length: text.length }];
      },
      async lookup(): Promise<LookupResult> {
        return signedIn ? { status: "found", definition: taberu } : { status: "pending", message: "Sign in" };
      },
      async resolvePending() { signedIn = true; },
    };
    const { root, attachment } = await mount({ host, parse, scan: true, options: { lookupDelay: 0 } });
    root.textContent = "猫";
    attachment.reread();
    await settle();
    const restore = caretOn(root.firstChild as unknown as Node);
    try {
      root.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      await settle();
      assert.equal(signedIn, true);
      assert.deepEqual(parsed, parse ? ["猫", "猫"] : []);
      assert.equal(root.querySelectorAll(".sb-word").length, parse ? 1 : 0);
      assert.ok(window.document.querySelector(".sb-host")!.shadowRoot!.querySelector(".sb-visible"), "the retried lookup stays open");
    } finally { restore(); }
  });
}

test("the popup reconnects after a page body is replaced", async () => {
  const first = await mount();
  first.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.ok(window.document.querySelector(".sb-host"));
  (await sdk()).detach();
  window.document.body.replaceWith(window.document.createElement("body"));
  const second = await mount();
  second.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.ok(window.document.querySelector(".sb-host"), "popup belongs to the current body");
});

test("two roots use their own hosts and clean up independently", async () => {
  const first = await mount();
  const second = await mount();
  first.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.deepEqual(first.host.journal.lookups, ["猫"]);
  assert.deepEqual(second.host.journal.lookups, []);
  assert.ok(first.attachment, "attach returns an owned handle");
  (first.attachment as unknown as { detach(): void }).detach();
  second.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.deepEqual(second.host.journal.lookups, ["猫"]);
});

test("per-root themes and custom styles follow the active root", async () => {
  const first = await mount({ options: { theme: "light", customCss: ".sb-popup { border-radius: 3px; }" } });
  const second = await mount({ options: { theme: "dark", definitionSize: "large" } });
  first.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  let popup = window.document.querySelector<HappyElement>(".sb-host")!;
  assert.equal(popup.dataset.sbTheme, "light");
  assert.match(popup.shadowRoot!.querySelector("#sb-custom-popup-css")!.textContent!, /3px/);
  second.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  popup = window.document.querySelector<HappyElement>(".sb-host")!;
  assert.equal(popup.dataset.sbTheme, "dark");
  assert.equal(popup.style.getPropertyValue("--sb-definition-size"), "16px");
  assert.equal(popup.shadowRoot!.querySelector("#sb-custom-popup-css"), null);
});

test("legend explanations use the attachment's languages", async () => {
  const { root } = await mount({ languages: ["spa"], host: fakeHost({ definitions: { "猫": taberu }, study: false }) });
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  const shadow = window.document.querySelector(".sb-host")!.shadowRoot!;
  shadow.querySelector<HappyElement>(".sb-sense-chip-open")!.click();
  assert.ok(shadow.querySelector(".sb-legend")!.textContent!.includes(legendTerm("v1", "pos", ["spa"])!.body));
});

test("detaching during tokenization leaves the old page untouched", async () => {
  let resolve!: (value: CardToken[]) => void;
  const host = { ...fakeHost({ study: false }), tokenize: () => new Promise<CardToken[]>((done) => { resolve = done; }) };
  const root = window.document.createElement("div");
  root.textContent = "猫";
  window.document.body.append(root);
  const attachment = (await sdk()).attach({ root: root as unknown as HTMLElement, host, styles: false });
  attachment.detach();
  resolve([{ surface: "猫", lemma: "猫", start: 0, length: 1, content: true }]);
  await settle();
  assert.equal(root.innerHTML, "猫");
});

test("an old cleanup handle cannot detach a replacement on the same root", async () => {
  const { root, attachment } = await mount();
  attachment.detach();
  const host = fakeHost({ study: false });
  (await sdk()).attach({ root: root as unknown as HTMLElement, host, parse: false, scan: false, styles: false, trigger: "click" });
  attachment.detach();
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.deepEqual(host.journal.lookups, ["猫"]);
});

test("a nested hover attachment owns its words", async () => {
  const first = await mount({ trigger: "hover", options: { lookupDelay: 0 } });
  const second = await mount({ trigger: "hover", options: { lookupDelay: 0 } });
  first.root.append(second.root);
  second.root.firstElementChild!.dispatchEvent(new window.MouseEvent("mouseover", { bubbles: true }));
  await settle();
  assert.deepEqual(second.host.journal.lookups, ["猫"]);
  assert.deepEqual(first.host.journal.lookups, []);
});

test("reattaching replaces settings, host and permissions without retaining old listeners", async () => {
  const first = await mount({ options: { theme: "light" } });
  const host = fakeHost({ definitions: { "猫": taberu } });
  const options = { root: first.root as unknown as HTMLElement, host, parse: false, scan: false, styles: false, trigger: "click" as const };
  (await sdk()).attach({ ...options, canWrite: true, options: { theme: "dark" } });
  first.attachment.detach();
  first.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.deepEqual(first.host.journal.lookups, []);
  assert.deepEqual(host.journal.lookups, ["猫"]);
  const popup = window.document.querySelector<HappyElement>(".sb-host")!;
  assert.equal(popup.dataset.sbTheme, "dark");
  assert.ok(popup.shadowRoot!.querySelector(".sb-btn-save"));

  (await sdk()).attach({ ...options, canWrite: false });
  first.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.equal(popup.shadowRoot!.querySelector(".sb-btn-save"), null);
  assert.equal(popup.shadowRoot!.querySelector(".sb-study-unavailable"), null);
  assert.ok(host.study!.save, "mounting read-only does not remove the host's own methods");
});

test("reattachment removes listeners even when the WebView ignores the signal option", async () => {
  const root = window.document.createElement("div");
  root.innerHTML = '<span class="sb-word" data-lemma="猫">猫</span>';
  window.document.body.append(root);
  const add = root.addEventListener;
  root.addEventListener = function(type, listener, options) {
    return add.call(this, type, listener, typeof options === "object" ? !!options.capture : options);
  };
  const first = fakeHost({ study: false });
  const second = fakeHost({ study: false });
  const options = { root: root as unknown as HTMLElement, parse: false, scan: false, styles: false, trigger: "click" as const };
  (await sdk()).attach({ ...options, host: first });
  root.firstElementChild!.dispatchEvent(new window.PointerEvent("pointerup", { bubbles: true, pointerType: "touch" }));
  await settle();
  (await sdk()).attach({ ...options, host: second });
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.deepEqual(first.journal.lookups, ["猫"]);
  assert.deepEqual(second.journal.lookups, ["猫"]);
});

test("class hosts retain prototype methods, getters and private state", async () => {
  class Host {
    #lookups: string[] = [];
    get lookups() { return this.#lookups; }
    async lookup(query: string): Promise<LookupResult> {
      this.#lookups.push(query);
      return { status: "found", definition: taberu };
    }
    async lookupEntry(): Promise<LookupResult> { return { status: "found", definition: null }; }
  }
  const host = new Host();
  const { root } = await mount({ host });
  root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.deepEqual(host.lookups, ["猫"]);
});

test("an outer raw-text scanner cannot dismiss a nested word popup", async () => {
  const outer = await mount({ scan: true });
  const inner = await mount({ options: { hideDelay: 0 } });
  outer.root.append(inner.root);
  inner.root.firstElementChild!.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  inner.root.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true }));
  await settle();
  assert.ok(window.document.querySelector(".sb-host")!.shadowRoot!.querySelector(".sb-visible"));
});

test("a scanner does not activate its host for excluded words", async () => {
  const { attachScanner } = await import("@shirabe-org/card/word-card/scan");
  const { root } = await mount();
  let activated = 0;
  const stop = attachScanner({ root: root as unknown as HTMLElement, onScan: () => { activated++; } });
  try {
    root.firstElementChild!.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true }));
    assert.equal(activated, 0);
  } finally { stop(); }
});

test("touch scanning respects the same eligibility check as mouse scanning", async () => {
  const { attachScanner } = await import("@shirabe-org/card/word-card/scan");
  const { root } = await mount();
  let checked = 0;
  const stop = attachScanner({ root: root as unknown as HTMLElement, shouldScan: () => { checked++; return false; } });
  try {
    root.dispatchEvent(new window.PointerEvent("pointerup", { bubbles: true, pointerType: "touch" }));
    assert.equal(checked, 1);
  } finally { stop(); }
});

test("nested raw-text scans use only the inner host for mouse and touch", async () => {
  const outer = await mount({ scan: true, trigger: "hover", options: { lookupDelay: 0 } });
  const inner = await mount({ scan: true, trigger: "hover", options: { lookupDelay: 0 } });
  inner.root.textContent = "猫";
  outer.root.append(inner.root);
  const doc = window.document;
  const caretDescriptor = Object.getOwnPropertyDescriptor(doc, "caretPositionFromPoint");
  const rectDescriptor = Object.getOwnPropertyDescriptor(window.Range.prototype, "getBoundingClientRect");
  Object.defineProperty(doc, "caretPositionFromPoint", { configurable: true, value: () => ({ offsetNode: inner.root.firstChild, offset: 0 }) });
  Object.defineProperty(window.Range.prototype, "getBoundingClientRect", { configurable: true, value: () => new window.DOMRect(10, 10, 20, 20) });
  try {
    inner.root.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true }));
    await settle();
    assert.deepEqual(inner.host.journal.lookups, ["猫"]);
    (await sdk()).dismiss();
    inner.root.dispatchEvent(new window.PointerEvent("pointerup", { bubbles: true, pointerType: "touch" }));
    await settle();
    assert.deepEqual(inner.host.journal.lookups, ["猫", "猫"]);
    assert.deepEqual(outer.host.journal.lookups, []);
  } finally {
    if (caretDescriptor) Object.defineProperty(doc, "caretPositionFromPoint", caretDescriptor);
    else Reflect.deleteProperty(doc, "caretPositionFromPoint");
    if (rectDescriptor) Object.defineProperty(window.Range.prototype, "getBoundingClientRect", rectDescriptor);
    else Reflect.deleteProperty(window.Range.prototype, "getBoundingClientRect");
  }
});
