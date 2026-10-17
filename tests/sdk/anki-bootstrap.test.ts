import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { Window } from "happy-dom";

const source = readFileSync("../../src/web/attach.js", "utf8");

function bootstrap(android?: unknown) {
  const window = new Window();
  window.document.body.innerHTML = '<main id="qa">猫</main>';
  const attachments: Record<string, any>[] = [];
  const commands: string[] = [];
  let detached = 0;
  const sdk = {
    createLink: () => ({}),
    apiHost: () => ({}),
    createAnkiHost: () => ({}),
    attach: (options: Record<string, unknown>) => {
      attachments.push(options);
      return { detach: () => { detached++; } };
    },
  };
  Object.assign(window, { ShirabeCard: sdk });
  new Function("window", "document", "ShirabeCard", "pycmd", "AnkiDroidJS", source)(window, window.document, sdk,
    (command: string, reply?: (value: string) => void) => {
      commands.push(command);
      reply?.('{"ok":true}');
    }, android);
  const client = (window as any).ShirabeAnki;
  const settings = { apiBase: "https://shirabe.org", clientId: "shirabe-anki-card", native: false };
  return { window, client, settings, attachments, commands, sdk, detached: () => detached };
}

test("Anki attaches click-only to the review area without requesting a full parse", async () => {
  const fixture = bootstrap();
  fixture.client.open(fixture.settings);
  const options = fixture.attachments[0];
  assert.equal(options.parse, false);
  assert.equal(options.studyColors, false);
  assert.equal(options.trigger, "click");
  assert.equal(options.root.id, "qa");
  assert.equal(options.canWrite, false);
  assert.equal(options.host.authoring, undefined);
  assert.deepEqual(fixture.commands, []);
  await fixture.window.happyDOM.close();
});

test("card transitions detach the prior reader, including an answer followed by a question", async () => {
  const fixture = bootstrap();
  fixture.client.open(fixture.settings);
  fixture.client.open({ ...fixture.settings, onlineParsing: true });
  assert.equal(fixture.detached(), 1);
  assert.equal(fixture.attachments[1].parse, true);
  fixture.client.detach();
  assert.equal(fixture.detached(), 2);
  fixture.client.detach();
  assert.equal(fixture.detached(), 2);
  await fixture.window.happyDOM.close();
});

test("the word popup does not offer note actions from the card", async () => {
  const fixture = bootstrap();
  fixture.client.open({ ...fixture.settings, native: true, cardId: 42 });
  assert.equal(fixture.attachments[0].host.authoring, undefined);
  assert.deepEqual(fixture.commands, []);
  await fixture.window.happyDOM.close();
});

test("missing snapshot media explains how to recover without disabling online lookup", async () => {
  const fixture = bootstrap();
  Object.assign(fixture.sdk, { loadSnapshot: async () => null });
  await fixture.client.open({ ...fixture.settings, snapshot: "_shirabe_snapshot_v1_missing.json" });
  assert.equal(fixture.attachments.length, 1);
  assert.match(fixture.window.document.body.textContent ?? "", /offline.*sync/i);
  fixture.client.detach();
  assert.equal(fixture.window.document.querySelector("[data-shirabe-notice]"), null);
  await fixture.window.happyDOM.close();
});

test("a bootstrap failure offers retry instead of an unhandled rejection", async () => {
  const fixture = bootstrap();
  fixture.sdk.createAnkiHost = () => { throw new Error("bad bundle"); };
  await fixture.client.open(fixture.settings);
  const notice = fixture.window.document.querySelector("button[data-shirabe-notice]");
  assert.match(notice?.textContent ?? "", /retry/i);
  fixture.sdk.createAnkiHost = () => ({});
  (notice as unknown as HTMLElement).click();
  assert.equal(fixture.attachments.length, 1);
  await fixture.window.happyDOM.close();
});

test("changing collection-status preference cannot leave the cached host overridden", async () => {
  const fixture = bootstrap();
  const accountStates = async () => ({});
  fixture.sdk.createAnkiHost = () => ({ study: { states: accountStates } });
  await fixture.client.open({ ...fixture.settings, native: true, cardId: 42, useCollectionStates: true });
  assert.notEqual(fixture.attachments[0].host.study.states, accountStates);
  assert.equal(fixture.attachments[0].studyColors, true);
  await fixture.client.open({ ...fixture.settings, native: true, cardId: 42, useCollectionStates: false });
  assert.equal(fixture.attachments[1].host.study.states, accountStates);
  assert.equal(fixture.attachments[1].studyColors, false);
  await fixture.window.happyDOM.close();
});

test("prepared token display does not authorize online study requests before a click", async () => {
  const fixture = bootstrap();
  const accountStates = async () => ({});
  fixture.sdk.createAnkiHost = () => ({ study: { states: accountStates } });
  Object.assign(fixture.sdk, {
    loadSnapshot: async () => ({ contentVersion: "fixture" }),
    collectReadableRuns: () => ["猫"],
    snapshotHost: (fallback: unknown) => fallback,
  });
  await fixture.client.open({ ...fixture.settings, snapshot: "fixture.json" });
  assert.equal(fixture.attachments[0].parse, true);
  assert.equal(fixture.attachments[0].studyColors, false);
  assert.equal(fixture.attachments[0].host.study.states, accountStates);
  await fixture.client.open({ ...fixture.settings, snapshot: "fixture.json", onlineParsing: true });
  assert.equal(fixture.attachments[1].studyColors, true);
  await fixture.window.happyDOM.close();
});

test("sign-out clears live account caches and reattaches the current card signed out", async () => {
  const fixture = bootstrap();
  let cleared = 0;
  fixture.sdk.createAnkiHost = () => ({ account: { signOut: () => { cleared++; }, connected: () => true } });
  fixture.window.document.getElementById("qa")!.innerHTML = '<span class="sb-word" data-state="new" data-card-id="123" data-card-url="/private" data-deck-names="Private deck">猫</span>';
  await fixture.client.open({ ...fixture.settings, native: true, stored: "account", cardId: 42 });
  assert.equal(fixture.attachments[0].host.account.connected(), true);
  await fixture.client.signOut();
  assert.equal(cleared, 1);
  assert.equal(fixture.attachments.length, 2);
  assert.equal(fixture.detached(), 1);
  const word = fixture.window.document.querySelector(".sb-word")!;
  assert.equal(word.getAttribute("data-state"), "none");
  assert.equal(word.getAttribute("data-card-id"), null);
  assert.equal(word.getAttribute("data-card-url"), null);
  assert.equal(word.getAttribute("data-deck-names"), null);
  await fixture.window.happyDOM.close();
});

test("Android sign-in explains its persistent storage setting before approval", async () => {
  class Android { async ankiSearchCard() { return { success: true }; } }
  const fixture = bootstrap(Android);
  fixture.sdk.createAnkiHost = () => ({ resolvePending: async (show: (message: string) => void) => { show("Approve this device"); } });
  await fixture.client.open(fixture.settings);
  const messages: string[] = [];
  await fixture.attachments[0].host.resolvePending((message: string | Node) => {
    messages.push(typeof message === "string" ? message : message.textContent ?? "");
  });
  assert.match(messages[0], /localStorage in Study Screen/);
  assert.match(messages[0], /restart AnkiDroid/i);
  assert.match(messages[0], /Approve this device/);
  await fixture.window.happyDOM.close();
});
