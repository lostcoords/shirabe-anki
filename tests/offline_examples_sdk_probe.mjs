import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(new URL("../../package.json", import.meta.url));
const { Window } = await import(pathToFileURL(require.resolve("happy-dom")).href);
const window = new Window({ url: "https://fixture.invalid/" });
window.TextDecoder = TextDecoder;
window.TextEncoder = TextEncoder;
window.Blob = Blob;
window.DecompressionStream = DecompressionStream;
window.fetch = async () => { throw new Error("Offline lookup must not request the network"); };
const bundle = readFileSync(new URL("../src/web/shirabe-card.js", import.meta.url));
window.eval(bundle.toString());
const api = window.ShirabeCard;
const { snapshot, expectedExamples, media } = JSON.parse(readFileSync(0, "utf8"));
const bytes = media ? Buffer.from(media.base64, "base64") : Buffer.from(JSON.stringify(snapshot));
const filename = media?.filename ?? `_shirabe_snapshot_v1_${createHash("sha256").update(bytes).digest("hex")}.json`;
const loaded = await api.loadSnapshot(filename, { fetch: async (name) => {
  assert.equal(name, filename);
  return new Response(bytes, { headers: { "Content-Length": String(bytes.length) } });
} });
assert.deepEqual(JSON.parse(JSON.stringify(loaded)), snapshot);
const segment = loaded.segments[0];
const candidate = segment.results[0].candidates[0];
const host = api.snapshotHost(undefined, loaded, { apiBase: "https://fixture.invalid", contentVersion: loaded.contentVersion, texts: [segment.text] });
const result = await host.lookupEntry(`${candidate.dictionary}:${candidate.sourceId}`, segment.tokens[0].surface);
assert.equal(result.status, "found");
assert.equal(result.definition.offline, true);
assert.equal(result.definition.preparedAt, snapshot.createdAt);
if (expectedExamples) {
  assert.deepEqual(JSON.parse(JSON.stringify(result.definition.examples.map(({ japanese, translation }) => ({ japanese, translation })))), expectedExamples);
  const notice = loaded.sources.find((source) => source.slug === "tatoeba");
  const links = notice.attribution.split("\n").filter((line) => line.startsWith("https://"));
  for (const example of result.definition.examples) {
    assert.ok(example.source.includes("Tatoeba contributors"));
    assert.ok(example.source.includes("CC BY 2.0 FR"));
    for (const link of [...links, notice.source.licenseUrl]) assert.ok(example.reference.includes(link));
  }
} else {
  assert.equal(result.definition.examples[0].japanese, "猫が好きです。");
  assert.equal(result.definition.examples[0].translation, "Me gustan los gatos.");
}
assert.ok(loaded.sources.some((source) => source.slug === "tatoeba" && source.source.license === "CC BY 2.0 FR"));
await window.happyDOM.close();
process.stdout.write(JSON.stringify({ offlineExamples: true, snapshotBytes: bytes.length,
  bundle: fileURLToPath(new URL("../src/web/shirabe-card.js", import.meta.url)), bundleHash: createHash("sha256").update(bundle).digest("hex") }));
