import assert from "node:assert/strict";
import { build } from "esbuild";
import { Window } from "happy-dom";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
if (args.length && (args.length !== 2 || args[0] !== "--bundle" || !args[1])) {
  throw new Error("Usage: offline_rich_content_probe.mjs [--bundle PATH] < snapshot.json");
}
const bundlePath = args.length ? resolve(args[1]) : null;
const chunks = [];
let inputBytes = 0;
for await (const chunk of process.stdin) {
  inputBytes += chunk.length;
  if (inputBytes > 128 * 1024) throw new Error("Probe input exceeds the snapshot limit");
  chunks.push(chunk);
}
const snapshot = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
const window = new Window({ url: "https://fixture.invalid/card" });
let networkCalls = 0;
const rejectNetwork = async () => { networkCalls++; throw new Error("This probe cannot access the network"); };
window.fetch = rejectNetwork;
Object.assign(globalThis, {
  window, document: window.document, HTMLElement: window.HTMLElement,
  Node: window.Node, DOMRect: window.DOMRect, getComputedStyle: window.getComputedStyle.bind(window),
  fetch: rejectNetwork,
  Audio: class { constructor() { throw new Error("This probe cannot play audio"); } },
});
Object.assign(window, { TextEncoder, TextDecoder, Audio: globalThis.Audio });
let snapshotHost, validateSnapshot, open, artifact;
let close = () => {};
try {
  if (bundlePath) {
    const bytes = readFileSync(bundlePath);
    window.eval(bytes.toString("utf8"));
    const sdk = window.ShirabeCard;
    assert.equal(typeof sdk?.snapshotHost, "function", "The artifact must export snapshotHost");
    assert.equal(typeof sdk?.attach, "function", "The artifact must export attach");
    snapshotHost = sdk.snapshotHost;
    open = (host, word) => {
      const attachment = sdk.attach({ root: window.document.body, host, parse: false, scan: false,
        studyColors: false, styles: false, trigger: "click", canWrite: false, connected: false, languages: ["jpn", "eng"] });
      close = () => attachment.detach();
      attachment.show(word);
    };
    artifact = { sdk_input: "built-standalone", bundle_path: bundlePath,
      bundle_sha256: createHash("sha256").update(bytes).digest("hex") };
  } else {
    const built = await build({
      stdin: {
        contents: 'export * as card from "./src/word-card/card.ts"; export { snapshotHost, validateSnapshot } from "./src/word-card/snapshot-host.ts";',
        resolveDir: fileURLToPath(new URL("../../packages/card/", import.meta.url)),
      },
      bundle: true, write: false, platform: "node", format: "esm", logLevel: "silent",
    });
    const sdk = await import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString("base64")}`);
    ({ snapshotHost, validateSnapshot } = sdk);
    open = (host, word) => {
      sdk.card.setCardHost(host);
      sdk.card.setWriteState({ canWrite: false, connected: false, readOnly: true, languages: ["jpn", "eng"] });
      sdk.card.showNow(word);
    };
    close = () => sdk.card.dismissPopup();
    artifact = { sdk_input: "current-typescript-not-built-addon",
      source_sha256: Object.fromEntries(["card.ts", "api-host.ts", "snapshot-host.ts"].map(name => [name,
        createHash("sha256").update(readFileSync(new URL(`../../packages/card/src/word-card/${name}`, import.meta.url))).digest("hex")])) };
    assert.deepEqual(validateSnapshot(snapshot), snapshot, "The SDK must preserve the Python snapshot exactly");
  }
  const host = snapshotHost(undefined, snapshot, {
    contentVersion: snapshot.contentVersion, apiBase: "https://fixture.invalid", texts: ["猫"],
  });
  const lookup = await host.lookupEntry("publisher:rich:1", "猫");
  assert.equal(lookup.status, "found");
  const word = window.document.createElement("span");
  word.className = "sb-word";
  word.textContent = "猫";
  Object.assign(word.dataset, { surface: "猫", lemma: "猫", reading: "ネコ", entry: "publisher:rich:1" });
  window.document.body.append(word);
  open(host, word);
  for (let index = 0; index < 5; index++) await new Promise(resolve => setTimeout(resolve, 0));
  const shadow = window.document.querySelector(".sb-host")?.shadowRoot;
  assert.ok(shadow, "The actual shared card must open");
  const texts = selector => [...shadow.querySelectorAll(selector)].map(node => node.textContent.trim());
  const senses = [...shadow.querySelectorAll(".sb-senses > li")];
  const report = {
    ...artifact,
    sense_count: senses.length,
    gloss_titles: texts(".sb-gloss-title"),
    usage_titles: texts(".sb-usage-title"),
    usage_texts: texts(".sb-usage-text"),
    usage_only_visible: senses.some(sense => !sense.querySelector(".sb-gloss-row") && sense.querySelector(".sb-usage-text")?.textContent === "猫が寝る。"),
    source_tags: texts(".sb-source-tag"),
    source_headwords: texts(".sb-source-tags-label"),
    source_notes: [...shadow.querySelectorAll(".sb-source-tag")].map(node => node.getAttribute("title")),
    cross_references: texts(".sb-xref"),
    cross_reference_titles: [...shadow.querySelectorAll(".sb-xref")].map(node => node.getAttribute("title")),
    network_calls: networkCalls,
  };
  console.log(JSON.stringify(report));
  assert.equal(report.sense_count, 2, "Native preparation must not erase a usage-only sense");
  assert.deepEqual(report.gloss_titles, ["基本義", "Meaning"]);
  assert.deepEqual(report.usage_titles, ["使用例", "Usage", "用例"]);
  assert.deepEqual(report.usage_texts, ["猫がいる。", "There is a cat.", "猫が寝る。"]);
  assert.equal(report.usage_only_visible, true);
  assert.deepEqual(report.source_tags, ["arch", "arch"]);
  assert.equal(report.source_headwords.length, 1);
  assert.match(report.source_headwords[0], /publisher.*猫.*ねこ/);
  assert.deepEqual(report.source_notes, ["Publisher note", "Publisher note"]);
  assert.deepEqual(report.cross_references, ["子猫子供の猫", "イエネコ"]);
  assert.deepEqual(report.cross_reference_titles, ["子猫 (コネコ)", "Look up イエネコ"]);
  assert.equal(networkCalls, 0);
} finally {
  close();
  await window.happyDOM.close();
}
