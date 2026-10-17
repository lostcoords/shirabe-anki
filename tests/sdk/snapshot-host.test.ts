import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import test from "node:test";
import { gzipSync } from "node:zlib";
import { apiHost } from "@shirabe-org/card/word-card/api-host";
import type { LookupResult, WordCardHost } from "@shirabe-org/card/word-card/host";
import { loadSnapshot, MAX_SNAPSHOT_BYTES, snapshotHash, snapshotHost, validateSnapshot } from "@shirabe-org/card/word-card/snapshot-host";
import type { PreparedSnapshot } from "@shirabe-org/card/word-card/snapshot-host";

const wire = execFileSync("python3", [resolve("../offline_fixture.py")], { encoding: "utf8" }).trim();
const fixture = (): PreparedSnapshot => JSON.parse(wire);
const options = { contentVersion: "dictionary-v1-en-es", apiBase: "https://shirabe.org" };
const filename = (text: string) => `_shirabe_snapshot_v1_${snapshotHash(text)}.json`;
const compressedEnvelope = (text: string, changes: Record<string, unknown> = {}) => JSON.stringify({
  formatVersion: 2, encoding: "gzip-base64", decodedBytes: Buffer.byteLength(text),
  data: gzipSync(text).toString("base64"), ...changes,
});
const compressedFilename = (text: string) => `_shirabe_snapshot_v2_${snapshotHash(text)}.json`;

test("compressed snapshot media retains the exact prepared lookup data", async () => {
  const encoded = compressedEnvelope(wire);
  const loaded = await loadSnapshot(compressedFilename(encoded), { fetch: async () => new Response(encoded) });
  assert.deepEqual(loaded, fixture());
  assert.deepEqual(await snapshotHost(undefined, loaded, options).tokenize!("😀猫は猫"), fixture().segments[0]!.tokens);
});

test("compression preserves multilingual definitions, preferences, examples and lookup ambiguity", async () => {
  const data = JSON.parse(wire);
  data.preferences = { languages: ["es", "en", "ja"], dictionaryReveal: { "jmdict:en": "hover" } };
  const candidate = data.segments[0]!.results[0]!.candidates[0]!;
  candidate.entries.push({
    dictionary: "uploaded", dictionaryName: "日本語辞典", sourceId: "opaque:1", type: "definition",
    senses: [{ position: 7, definitions: [{ lang: "ja", text: "猫の説明" }, { lang: "unknown", text: "Unknown language" }], tags: [], notes: ["Keep this note"] }],
  });
  candidate.examples = [{ japanese: "猫がいる。", translations: [{ lang: "en", text: "There is a cat." }, { lang: "es", text: "Hay un gato." }] }];
  data.sources = [{ slug: "uploaded", name: "日本語辞典", attribution: "Preserve attribution" }];
  const encoded = compressedEnvelope(JSON.stringify(data));
  const loaded = await loadSnapshot(compressedFilename(encoded), { fetch: async () => new Response(encoded) });
  assert.deepEqual(loaded, data);
  const settings = { ...options, texts: ["😀猫は猫"] };
  const before = snapshotHost(undefined, data, settings), after = snapshotHost(undefined, loaded, settings);
  assert.deepEqual(await after.lookupEntry("jmdict:1", "猫"), await before.lookupEntry("jmdict:1", "猫"));
  const ambiguous = await before.lookup("猫", "word");
  assert.equal(ambiguous.status, "failed");
  assert.deepEqual(await after.lookup("猫", "word"), ambiguous);
});

test("compressed media rejects corrupt streams, false lengths and expansion beyond the decoded limit", async () => {
  const corrupt = gzipSync(wire);
  corrupt[corrupt.length - 8] ^= 1;
  for (const encoded of [
    compressedEnvelope(wire, { decodedBytes: Buffer.byteLength(wire) + 1 }),
    compressedEnvelope(wire, { decodedBytes: wire.length }),
    compressedEnvelope(wire, { decodedBytes: true }),
    compressedEnvelope(wire, { decodedBytes: 0 }),
    compressedEnvelope(wire, { decodedBytes: -1 }),
    compressedEnvelope(wire, { decodedBytes: 1.5 }),
    compressedEnvelope(wire, { decodedBytes: MAX_SNAPSHOT_BYTES + 1 }),
    compressedEnvelope(wire, { encoding: "unknown" }),
    compressedEnvelope(wire, { data: "not-base64" }),
    compressedEnvelope(wire, { data: corrupt.toString("base64") }),
    compressedEnvelope(wire, { data: gzipSync(wire).subarray(0, -3).toString("base64") }),
    compressedEnvelope("x".repeat(MAX_SNAPSHOT_BYTES + 1), { decodedBytes: 1 }),
    compressedEnvelope(JSON.stringify({ ...fixture(), formatVersion: 2 })),
    compressedEnvelope(compressedEnvelope(wire)),
    compressedEnvelope(wire, { decodedBytes: 1, data: gzipSync(Buffer.from([0xff])).toString("base64") }),
  ]) {
    assert.equal(await loadSnapshot(compressedFilename(encoded), { fetch: async () => new Response(encoded) }), null);
  }
});

test("snapshot filenames must match their stored format and hash before decoding", async (t) => {
  const encoded = compressedEnvelope(wire);
  const decompress = t.mock.method(globalThis, "DecompressionStream", () => { throw new Error("must not decode"); });
  for (const [name, body] of [
    [filename(encoded), encoded], [compressedFilename(wire), wire],
    [compressedFilename(encoded), encoded + " "],
    [compressedFilename(encoded).replace("_v2_", "_v3_"), encoded],
  ]) {
    assert.equal(await loadSnapshot(name!, { fetch: async () => new Response(body) }), null);
  }
  assert.equal(decompress.mock.callCount(), 0);
});

test("compressed media needs native gzip support but legacy snapshots do not", async (t) => {
  const encoded = compressedEnvelope(wire);
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "DecompressionStream")!;
  t.after(() => Object.defineProperty(globalThis, "DecompressionStream", descriptor));
  Object.defineProperty(globalThis, "DecompressionStream", { ...descriptor, value: undefined });
  assert.equal(await loadSnapshot(compressedFilename(encoded), { fetch: async () => new Response(encoded) }), null);
  assert.deepEqual(await loadSnapshot(filename(wire), { fetch: async () => new Response(wire) }), fixture());
});

test("compressed envelopes require canonical standard base64", async () => {
  const text = [wire, wire + " ", wire + "  "].find((value) => gzipSync(value).length % 3 !== 0)!;
  assert.ok(text);
  const packed = gzipSync(text);
  const canonical = packed.toString("base64");
  const valid = compressedEnvelope(text);
  assert.deepEqual(await loadSnapshot(compressedFilename(valid), { fetch: async () => new Response(valid) }), fixture());
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const last = canonical.indexOf("=") - 1;
  const nonzeroPadding = canonical.slice(0, last) + alphabet[alphabet.indexOf(canonical[last]!) + 1] + canonical.slice(last + 1);
  assert.deepEqual(Buffer.from(nonzeroPadding, "base64"), packed);
  for (const data of [canonical + "\n", canonical.replace(/=+$/, ""), nonzeroPadding, canonical.replace(/\+/g, "-").replace(/\//g, "_")]) {
    assert.notEqual(data, canonical);
    const encoded = compressedEnvelope(text, { data });
    assert.equal(await loadSnapshot(compressedFilename(encoded), { fetch: async () => new Response(encoded) }), null);
  }
});

test("stored and decoded snapshot byte boundaries are inclusive and independent", async () => {
  const decoded = wire + " ".repeat(MAX_SNAPSHOT_BYTES - Buffer.byteLength(wire));
  const encoded = compressedEnvelope(decoded);
  assert.equal(Buffer.byteLength(decoded), MAX_SNAPSHOT_BYTES);
  assert.ok(Buffer.byteLength(encoded) < MAX_SNAPSHOT_BYTES);
  const stored = encoded + " ".repeat(MAX_SNAPSHOT_BYTES - Buffer.byteLength(encoded));
  assert.equal(Buffer.byteLength(stored), MAX_SNAPSHOT_BYTES);
  assert.deepEqual(await loadSnapshot(compressedFilename(stored), { fetch: async () => new Response(stored) }), fixture());
  assert.equal(await loadSnapshot(compressedFilename(stored + " "), { fetch: async () => new Response(stored + " ") }), null);
  const tooLarge = compressedEnvelope(decoded + " ");
  assert.equal(await loadSnapshot(compressedFilename(tooLarge), { fetch: async () => new Response(tooLarge) }), null);
});

test("the decoded stream stops at its declared length and cancels expansion", async (t) => {
  let pulled = 0, cancelled = false;
  t.mock.method(globalThis, "DecompressionStream", function () {
    return {
      writable: new WritableStream(),
      readable: new ReadableStream<Uint8Array>({
        pull(controller) { pulled++; controller.enqueue(new Uint8Array(1024)); },
        cancel() { cancelled = true; },
      }),
    } as DecompressionStream;
  });
  const encoded = compressedEnvelope(wire, { decodedBytes: 1 });
  let signal: AbortSignal | undefined;
  assert.equal(await loadSnapshot(compressedFilename(encoded), { fetch: async (_input, init) => {
    signal = init?.signal ?? undefined;
    return new Response(encoded);
  } }), null);
  assert.equal(signal?.aborted, true);
  assert.equal(cancelled, true);
  assert.ok(pulled <= 2);
});

test("the five-second snapshot deadline also cancels a stalled decoded stream", { timeout: 2000 }, async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let cancelled = false;
  let started!: () => void;
  const decoding = new Promise<void>((resolve) => { started = resolve; });
  t.mock.method(globalThis, "DecompressionStream", function () {
    started();
    return {
      writable: new WritableStream(),
      readable: new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } }),
    } as DecompressionStream;
  });
  const encoded = compressedEnvelope(wire);
  let signal: AbortSignal | undefined;
  const pending = loadSnapshot(compressedFilename(encoded), { fetch: async (_input, init) => {
    signal = init?.signal ?? undefined;
    return new Response(encoded);
  } });
  await Promise.race([decoding, pending.then(() => assert.fail("Snapshot loading finished without starting gzip decoding"))]);
  t.mock.timers.tick(4999);
  assert.equal(signal?.aborted, false);
  t.mock.timers.tick(1);
  assert.equal(await pending, null);
  assert.equal(signal?.aborted, true);
  assert.equal(cancelled, true);
});

test("a complete decoded buffer is rejected if gzip never finishes before cancellation", { timeout: 2000 }, async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let waiting!: () => void;
  const stalled = new Promise<void>((resolve) => { waiting = resolve; });
  let cancelled = false;
  t.mock.method(globalThis, "DecompressionStream", function () {
    return {
      writable: new WritableStream(),
      readable: new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(new TextEncoder().encode(wire)); },
        pull() { waiting(); },
        cancel() { cancelled = true; return new Promise<void>(() => {}); },
      }),
    } as DecompressionStream;
  });
  const encoded = compressedEnvelope(wire);
  const pending = loadSnapshot(compressedFilename(encoded), { fetch: async () => new Response(encoded) });
  await stalled;
  t.mock.timers.tick(5000);
  assert.equal(await pending, null);
  assert.equal(cancelled, true);
});
const recordingBytes = Uint8Array.from([0, 0, 0, 28, ...new TextEncoder().encode("ftypM4A "), 0, 0, 2, 0,
  ...new TextEncoder().encode("M4A isomiso2"), 0, 0, 0, 12, ...new TextEncoder().encode("mdatAAC!")]);
const recording = (bytes = recordingBytes, extension = "m4a") => {
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  return { filename: `shirabe_${sha256}.${extension}`, sha256, bytes: bytes.length,
    mimeType: extension === "m4a" ? "audio/mp4" as const : "audio/mpeg" as const, attribution: "Shirabe" as const,
    license: "Permitted for Anki deck synchronization and export" as const };
};
const recordedFixture = (clip: unknown = recording()) => {
  const data = fixture();
  Object.assign(data.segments[0]!.results[0]!.candidates[0]!.pitch[0]!, { recording: clip });
  return data;
};
const inflectedFixture = (): PreparedSnapshot => JSON.parse(execFileSync("python3", ["-c",
  "import sys,json;sys.path.insert(0,sys.argv[1]);from offline_fixture import inflected;print(json.dumps(inflected().snapshot))",
  resolve(".."),
], { encoding: "utf8" }));

function online() {
  const calls: unknown[][] = [];
  const found: LookupResult = { status: "found", definition: null };
  const host: WordCardHost = {
    lookup: async (...args) => { calls.push(["lookup", ...args]); return found; },
    lookupEntry: async (...args) => { calls.push(["entry", ...args]); return found; },
    tokenize: async (...args) => { calls.push(["tokenize", ...args]); return []; },
  };
  return { host, calls };
}

test("portable SHA-256 agrees with Node for Unicode and padding boundaries", () => {
  for (const text of ["", "abc", "😀猫は猫", ...[55, 56, 63, 64, 65, 127, 4096].map((n) => "a".repeat(n)), wire]) {
    assert.equal(snapshotHash(text), createHash("sha256").update(text).digest("hex"));
  }
});

test("prepared pronunciation keeps its entry, reading and accent and loads only verified local bytes on demand", async () => {
  const data = recordedFixture();
  assert.deepEqual(validateSnapshot(data), data);
  const calls: Array<{ input: unknown; init?: RequestInit }> = [];
  const audioOptions = { ...options, texts: ["😀猫は猫"], fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ input, init });
    return new Response(recordingBytes, { headers: { "Content-Type": "audio/mp4" } });
  } };
  const fallback = online();
  const host = snapshotHost(fallback.host, data, audioOptions);
  const found = await host.lookupEntry("jmdict:1", "猫");
  assert.equal(found.status, "found");
  if (found.status !== "found") return;
  assert.equal(found.definition!.pitch[0]!.audioUrl, recording().filename);
  assert.equal(calls.length, 0);
  assert.deepEqual(fallback.calls, []);
  assert.ok(host.audio);
  assert.deepEqual(await host.audio.clip(recording().filename), { dataUrl: `data:audio/mp4;base64,${Buffer.from(recordingBytes).toString("base64")}` });
  assert.equal(calls.length, 1);
  assert.equal(calls[0]!.input, recording().filename);
  assert.equal(calls[0]!.init?.credentials, "same-origin");
  assert.equal(calls[0]!.init?.redirect, "error");
  assert.equal(calls[0]!.init?.headers, undefined);
  assert.ok(calls[0]!.init?.signal);
  assert.deepEqual(fallback.calls, []);
});

test("missing or corrupt prepared recordings cannot fall back to online pronunciation", async () => {
  for (const response of [() => new Response("missing", { status: 404 }),
    () => new Response(recordingBytes.slice(1)), () => new Response(new Uint8Array(recordingBytes.length)),
    () => new Response(recordingBytes, { headers: { "Content-Length": "1048577" } }),
    () => { throw new Error("offline"); }]) {
    let localReads = 0, fallbackReads = 0;
    const fallback = online();
    fallback.host.audio = { clip: async () => { fallbackReads++; return { dataUrl: "data:audio/mp4;base64,online" }; } };
    const audioOptions = { ...options, texts: ["😀猫は猫"], fetch: async () => { localReads++; return response(); } };
    const host = snapshotHost(fallback.host, recordedFixture(), audioOptions);
    await host.lookupEntry("jmdict:1", "猫");
    assert.ok(host.audio);
    const result = await host.audio.clip(recording().filename);
    assert.ok("error" in result);
    if ("error" in result) assert.match(result.error, /Sync media|prepare/i);
    assert.equal(localReads, 1);
    assert.equal(fallbackReads, 0);
  }
});

test("a recording from an inactive answer or a different reading or accent cannot be played", async () => {
  let fetched = false;
  const audioOptions = { ...options, texts: ["犬"], fetch: async () => { fetched = true; return new Response(recordingBytes); } };
  const host = snapshotHost(undefined, recordedFixture(), audioOptions);
  assert.ok(host.audio);
  assert.ok("error" in await host.audio.clip(recording().filename));
  assert.equal(fetched, false);
  const data = recordedFixture();
  const original = data.segments[0]!.results[0]!.candidates[0]!;
  original.pitch.push({ downstep: 0, pattern: "heiban" });
  const homophone = structuredClone(original);
  homophone.sourceId = "homophone";
  homophone.reading = "ビョウ";
  homophone.pitch = [{ downstep: 0, pattern: "heiban" }];
  data.segments[0]!.results[0]!.candidates.push(homophone);
  const visible = snapshotHost(undefined, data, { ...options, texts: ["😀猫は猫"] });
  const first = await visible.lookupEntry("jmdict:1", "猫");
  assert.equal(first.status, "found");
  if (first.status === "found") assert.deepEqual(first.definition!.pitch.map(p => p.audioUrl), [recording().filename, null]);
  const second = await visible.lookupEntry("jmdict:homophone", "猫");
  assert.equal(second.status === "found" && second.definition!.pitch[0]!.audioUrl, null);
});

test("invalid optional recording metadata never removes a usable definition", async () => {
  const good = recording();
  for (const change of [{ filename: `../${good.filename}` }, { filename: `/${good.filename}` },
    { filename: `https://cdn.shirabe.org/audio/${good.filename}` }, { filename: `${good.filename}?x=1` },
    { filename: `${good.filename}#x` }, { filename: `${good.filename}\n` }, { filename: good.filename.toUpperCase() },
    { sha256: "0".repeat(64) }, { mimeType: "audio/mpeg" }, { bytes: 0 }, { bytes: 1048577 }, { bytes: 1.5 },
    { attribution: "" }, { license: "unapproved" }, { license: "x".repeat(25000) }]) {
    const data = recordedFixture({ ...good, ...change });
    const clean = validateSnapshot(data)!;
    assert.equal(clean.complete, true, Object.keys(change).join(", "));
    const host = snapshotHost(undefined, clean, { ...options, texts: ["😀猫は猫"] });
    const found = await host.lookupEntry("jmdict:1", "猫");
    assert.equal(found.status, "found");
    if (found.status === "found") {
      assert.equal(found.definition!.senses[0]!.glosses[0]!.text, "cat");
      assert.equal(found.definition!.pitch[0]!.audioUrl, null);
    }
  }
});

test("the recording budget counts unique files and removes only audio when exceeded", () => {
  for (const [count, bytes, allowed] of [[16, 1, true], [17, 1, false], [4, 1048576, true], [5, 1048576, false]] as const) {
    const data = fixture();
    data.segments[0]!.results[0]!.candidates[0]!.pitch = Array.from({ length: count }, (_, index) => {
      const sha256 = index.toString(16).padStart(64, "0");
      return { downstep: index, pattern: "atamadaka", recording: { ...recording(), sha256, filename: `shirabe_${sha256}.m4a`, bytes } };
    });
    const clean = validateSnapshot(data)!;
    assert.equal(clean.complete, true);
    assert.equal(JSON.stringify(clean).includes('"recording"'), allowed);
  }
  const data = recordedFixture();
  data.segments[0]!.results[0]!.candidates[0]!.pitch = Array(32).fill(data.segments[0]!.results[0]!.candidates[0]!.pitch[0]);
  assert.equal(JSON.stringify(validateSnapshot(data)).match(/"recording"/g)?.length, 32);
});

test("local MP3 playback shares verified cache reads, retries failures and preserves online host audio", async () => {
  const bytes = new TextEncoder().encode("ID3fixture");
  const clip = recording(bytes, "mp3");
  let reads = 0;
  const host = snapshotHost(online().host, recordedFixture(clip), { ...options, texts: ["😀猫は猫"],
    fetch: async () => { reads++; return reads === 1 ? new Response(null, { status: 404 }) : new Response(bytes); } });
  await host.lookupEntry("jmdict:1", "猫");
  assert.ok(host.audio);
  assert.ok("error" in await host.audio.clip(clip.filename));
  const expected = { dataUrl: `data:audio/mpeg;base64,${Buffer.from(bytes).toString("base64")}` };
  assert.deepEqual(await Promise.all([host.audio.clip(clip.filename), host.audio.clip(clip.filename)]), [expected, expected]);
  assert.deepEqual(await host.audio.clip(clip.filename), expected);
  assert.equal(reads, 2);
  const remote = "https://cdn.shirabe.org/audio/remote.m4a";
  assert.deepEqual(await host.audio.clip(remote), { dataUrl: remote });
  assert.equal(reads, 2);
});

test("recording streams stop at their declared size and reject mislabeled bytes even with a matching hash", async () => {
  let pulled = 0, cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) { pulled++; controller.enqueue(new Uint8Array(16)); },
    cancel() { cancelled = true; },
  });
  const host = snapshotHost(undefined, recordedFixture(), { ...options, texts: ["😀猫は猫"], fetch: async () => new Response(stream) });
  await host.lookupEntry("jmdict:1", "猫");
  assert.ok("error" in await host.audio!.clip(recording().filename));
  assert.ok(pulled <= 4);
  assert.equal(cancelled, true);
  for (const [bytes, extension] of [[recordingBytes, "mp3"], [new TextEncoder().encode("ID3fixture"), "m4a"],
    [new TextEncoder().encode("<html>login</html>"), "mp3"]] as const) {
    const clip = recording(bytes, extension);
    const host = snapshotHost(undefined, recordedFixture(clip), { ...options, texts: ["😀猫は猫"], fetch: async () => new Response(bytes) });
    await host.lookupEntry("jmdict:1", "猫");
    assert.ok("error" in await host.audio!.clip(clip.filename));
  }
});

test("local recording requests have a five-second abort deadline and can retry after it", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let signal: AbortSignal | null | undefined;
  let reads = 0;
  const host = snapshotHost(undefined, recordedFixture(), { ...options, texts: ["😀猫は猫"], fetch: async (_input, init) => {
    if (++reads > 1) return new Response(recordingBytes);
    signal = init?.signal;
    return new Promise<Response>((_resolve, reject) => signal!.addEventListener("abort", () => reject(new Error("aborted")), { once: true }));
  } });
  await host.lookupEntry("jmdict:1", "猫");
  const pending = host.audio!.clip(recording().filename);
  t.mock.timers.tick(4999);
  assert.equal(signal?.aborted, false);
  t.mock.timers.tick(1);
  assert.equal(signal?.aborted, true);
  assert.ok("error" in await pending);
  assert.ok("dataUrl" in await host.audio!.clip(recording().filename));
  assert.equal(reads, 2);
});

test("unapproved local names never fetch or reach an online audio port", async () => {
  let reads = 0;
  const fallback = online();
  fallback.host.audio = { clip: async () => { reads++; return { dataUrl: "unexpected" }; } };
  const host = snapshotHost(fallback.host, recordedFixture(), { ...options, texts: ["😀猫は猫"], fetch: async () => { reads++; return new Response(recordingBytes); } });
  await host.lookupEntry("jmdict:1", "猫");
  for (const name of ["../" + recording().filename, "https://attacker.test/clip.mp3", recording().filename + "?x=1",
    recording().filename + "\n", "shirabe_" + "0".repeat(64) + ".m4a"]) {
    assert.ok("error" in await host.audio!.clip(name));
  }
  assert.equal(reads, 0);
  assert.deepEqual(await host.audio!.clip("https://cdn.shirabe.org/audio/remote.m4a"), { dataUrl: "unexpected" });
  assert.equal(reads, 1);
});

test("optional recording availability does not make identical dictionary candidates ambiguous", async () => {
  const host = snapshotHost(undefined, recordedFixture(), { ...options, texts: ["😀猫は猫"] });
  const result = await host.lookup("猫", "word", { surface: "猫", reading: "ネコ" });
  assert.equal(result.status, "found");
  if (result.status === "found") assert.equal(result.definition!.pitch[0]!.audioUrl, recording().filename);
});

function partialAlternatives() {
  const data = fixture();
  data.complete = false;
  const segment = data.segments[0]!;
  const candidate = structuredClone(segment.results[0]!.candidates[0]!);
  candidate.sourceId = "alternative";
  candidate.entries[0]!.sourceId = "alternative";
  candidate.entries[0]!.senses[0]!.definitions = [{ lang: "en", text: "another meaning" }];
  segment.results[0]!.candidates.push(candidate);
  return data;
}

test("repeated offline words reuse an intact ordered superset of their saved alternatives", async () => {
  for (const reverse of [false, true]) {
    const data = partialAlternatives();
    const segment = data.segments[0]!;
    const expected = await snapshotHost(undefined, data, { ...options, texts: [segment.text] }).lookupEntry("jmdict:1", "猫");
    if (reverse) [segment.results[0]!.candidates, segment.results[1]!.candidates] =
      [segment.results[1]!.candidates, segment.results[0]!.candidates];
    const host = snapshotHost(undefined, data, { ...options, texts: [segment.text] });
    assert.deepEqual(await host.lookup("猫", "word", { surface: "猫", reading: "ネコ" }), expected);
    assert.deepEqual(await host.lookup("猫", "word"), expected);
  }
});

test("a shared parser pin permits reuse without changing the original candidate order", async () => {
  const data = partialAlternatives();
  const segment = data.segments[0]!;
  for (const result of segment.results) segment.tokens[result.tokenIndex]!.entry = "jmdict:alternative";
  segment.results[1]!.candidates = [structuredClone(segment.results[0]!.candidates[1]!)];
  const host = snapshotHost(undefined, data, { ...options, texts: [segment.text] });
  const result = await host.lookup("猫", "word");
  assert.equal(result.status, "found");
  if (result.status === "found") {
    assert.equal(result.definition!.entry, "jmdict:1");
    assert.deepEqual(result.candidates!.map((candidate) => candidate.entry), ["jmdict:1", "jmdict:alternative"]);
  }
  const pinned = await host.lookupEntry("jmdict:alternative", "猫");
  assert.equal(pinned.status, "found");
  if (pinned.status === "found") assert.equal(pinned.definition!.entry, "jmdict:alternative");
});

test("partial-result reuse rejects conflicting context, hidden content and selected meanings", async () => {
  const changes: Array<(data: PreparedSnapshot) => void> = [
    (data) => { data.segments[0]!.tokens[3]!.reading = "ビョウ"; },
    (data) => { data.segments[0]!.tokens[3]!.pos = "verb"; },
    (data) => { data.segments[0]!.tokens[3]!.entry = "jmdict:alternative"; },
    (data) => { data.segments[0]!.tokens[3]!.inflection = ["past"]; },
    (data) => { data.segments[0]!.tokens[3]!.inflectionBase = "猫"; },
    (data) => { data.segments[0]!.results[1]!.candidates[0]!.entries[0]!.senses[0]!.definitions[1]!.text = "otro significado"; },
    (data) => { data.segments[0]!.results[1]!.candidates[0]!.entries[0]!.senses[0]!.usages = [{ lang: "ja", text: "猫がいる。" }]; },
    (data) => { data.segments[0]!.results[1]!.candidates[0]!.entries[0]!.senses[0]!.definitions[0]!.title = "別の見出し"; },
    (data) => { data.segments[0]!.results[1]!.candidates = []; },
    (data) => { data.segments[0]!.results[1]!.candidates = [structuredClone(data.segments[0]!.results[0]!.candidates[1]!)]; },
    (data) => { data.segments[0]!.results[1]!.candidates = structuredClone(data.segments[0]!.results[0]!.candidates).reverse(); },
    (data) => { data.segments[0]!.results[0]!.candidates.push(structuredClone(data.segments[0]!.results[0]!.candidates[0]!)); },
  ];
  for (const [index, change] of changes.entries()) {
    const data = partialAlternatives();
    change(data);
    assert.deepEqual(validateSnapshot(data), data);
    const settings = { ...options, texts: [data.segments[0]!.text] };
    const host = snapshotHost(undefined, data, settings);
    assert.equal((await host.lookup("猫", "word")).status, "failed", `unsafe partial reuse ${index}`);
    const calls: string[] = [];
    const fallback: WordCardHost = {
      lookup: async (query) => { calls.push(query); return { status: "failed", message: "online fallback" }; },
      lookupEntry: async () => { throw new Error("must preserve the contextual lookup"); },
    };
    assert.deepEqual(await snapshotHost(fallback, data, settings).lookup("猫", "word"),
      { status: "failed", message: "online fallback" });
    assert.deepEqual(calls, ["猫"]);
  }
});

test("Python parser inflections survive flat snapshot tokens and each lookup uses its own surface", async () => {
  const data = inflectedFixture();
  const first = data.segments[0]!.tokens[0]!;
  assert.deepEqual(first.inflection, ["past", "polite"]);
  assert.equal(first.inflectionBase, "食べる");
  assert.deepEqual(validateSnapshot(data), data);
  const host = snapshotHost(undefined, data, { ...options, texts: ["食べました", "食べない"] });
  for (const [surface, labels] of [["食べました", ["past", "polite"]], ["食べない", ["negative"]]] as const) {
    assert.deepEqual((await host.tokenize!(surface))[0]!.inflection, labels);
    for (const result of [await host.lookup(surface, "text"), await host.lookupEntry("jmdict:1", surface),
      await host.lookup("食べる", "word", { surface })]) {
      assert.equal(result.status, "found");
      if (result.status === "found") {
        assert.deepEqual(result.definition!.inflection, labels);
        assert.equal(result.definition!.inflectionBase, "食べる");
        assert.equal(result.definition!.sourceForm, surface);
        result.definition!.inflection!.push("mutation");
      }
    }
  }
  assert.equal((await host.lookup("食べる", "word")).status, "failed", "ambiguous source forms must not borrow the first occurrence's explanation");
  const exposed = (await host.tokenize!("食べました"))[0]!;
  exposed.inflection!.push("poison");
  assert.deepEqual((await host.tokenize!("食べました"))[0]!.inflection, ["past", "polite"]);
  const base = await host.lookupEntry("jmdict:1", "食べる");
  assert.equal(base.status, "found");
  if (base.status === "found") assert.equal(base.definition!.inflection, undefined, "the base form must not borrow an inflected occurrence's explanation");
});

test("snapshot inflection metadata has finite shape and byte bounds", () => {
  for (const [field, value] of [["inflection", Array(17).fill("past")], ["inflection", ["x".repeat(129)]],
    ["inflection", [null]], ["inflection", "past"], ["inflectionBase", "x".repeat(513)]] as const) {
    const data = inflectedFixture();
    Object.assign(data.segments[0]!.tokens[0]!, { [field]: value });
    const clean = validateSnapshot(data)!;
    assert.equal(clean.complete, false);
    assert.deepEqual(clean.segments.map((segment) => segment.text), ["食べない"]);
  }
});

test("snapshot validation preserves source tags on senses and matching headword pairs", () => {
  const data = fixture();
  const entry = data.segments[0]!.results[0]!.candidates[0]!.entries[0]! as unknown as {
    sourceHeadwords?: unknown;
    senses: Array<{ sourceTags?: unknown }>;
  };
  const sourceTag = { name: "arch", category: "register", notes: "publisher note", order: 2, score: -3 };
  entry.senses[0]!.sourceTags = [sourceTag];
  entry.sourceHeadwords = [{ text: "猫", reading: "ねこ", tags: [sourceTag], rules: ["v1"], score: 7 }];

  assert.deepEqual(validateSnapshot(data), data);
});

test("snapshot validation preserves titled definitions and separate usage rows", () => {
  const data = fixture();
  const sense = data.segments[0]!.results[0]!.candidates[0]!.entries[0]!.senses[0]! as unknown as Record<string, unknown>;
  sense.definitions = [{ lang: "ja", text: "自慢する", title: "意味" }];
  sense.usages = [{ lang: "ja", text: "学歴を鼻にかける", title: "使用例" }];
  const tag = { category: "misc", code: "uk", label: "Usually kana", scope: "sense" };
  sense.label = "一㋐";
  sense.depth = 2;
  sense.tags = [tag, { category: "partOfSpeech", code: "v1", label: "Ichidan verb", scope: "group" }];
  sense.related = [
    { text: "器械" },
    { text: "器械", reading: "きかい", label: "①" },
  ];
  sense.antonyms = [{ text: "手動", reading: null }];
  sense.seeAlso = [{ text: "機構" }];

  assert.deepEqual(validateSnapshot(data), data);
});

test("snapshot lookup preserves qualified and unqualified references to the same target", async () => {
  const data = fixture();
  const sense = data.segments[0]!.results[0]!.candidates[0]!.entries[0]!.senses[0]! as unknown as Record<string, unknown>;
  sense.related = [
    { text: "器械" },
    { text: "器械", reading: "きかい", label: "①" },
  ];

  const clean = validateSnapshot(data);
  assert.ok(clean);
  assert.deepEqual(
    clean.segments[0]!.results[0]!.candidates[0]!.entries[0]!.senses[0]!.related,
    sense.related,
  );
  const result = await snapshotHost(undefined, clean, { ...options, texts: ["😀猫は猫"] }).lookupEntry("jmdict:1", "猫");
  assert.equal(result.status, "found");
  if (result.status === "found") assert.deepEqual(result.definition?.senses[0]?.xrefs, [
    { kind: "related", text: "器械" },
    { kind: "related", text: "器械", reading: "きかい", label: "①" },
  ]);
});

test("Python's prepared spans and candidates produce the same content as the API host", async () => {
  const data = fixture();
  assert.deepEqual(validateSnapshot(data), data);
  const fallback = online();
  const host = snapshotHost(fallback.host, data, options);
  assert.deepEqual(await host.tokenize!("😀猫は猫"), data.segments[0]!.tokens);
  const candidates = data.segments[0]!.results[0]!.candidates;
  const api = apiHost({ ...options, fetch: async () => new Response(JSON.stringify({ words: [{ candidates }] })) });
  const context = { surface: "猫", reading: "ネコ" };
  const expected = await api.lookup("猫", "word", context);
  if (expected.status === "found" && expected.definition) {
    Object.assign(expected.definition, { offline: true, preparedAt: data.createdAt });
    expected.definition.senses = expected.definition.senses.map((sense) => ({ ...sense, covered: false }));
    for (const alternative of expected.alternatives ?? []) {
      Object.assign(alternative, { offline: true, preparedAt: data.createdAt });
      alternative.senses = alternative.senses.map((sense) => ({ ...sense, covered: false }));
    }
  }
  assert.deepEqual(await host.lookup("猫", "word", context), expected);
  const pinned = await host.lookupEntry("jmdict:1", "猫");
  assert.equal(pinned.status, "found");
  if (pinned.status === "found") {
    assert.equal(pinned.definition?.senses[0]?.position, 3);
    assert.equal(pinned.definition?.pitch[0]?.audioUrl, null);
  }
  assert.deepEqual(fallback.calls, []);
});

test("answer definitions require exact visible text activation and a fresh host on each side", async () => {
  const fallback = online();
  const host = snapshotHost(fallback.host, fixture(), { ...options, texts: ["😀猫は猫"] });
  await host.lookupEntry("jmdict:2", "犬");
  await host.tokenize!("犬");
  assert.deepEqual(fallback.calls, [["entry", "jmdict:2", "犬"], ["tokenize", "犬"]]);
  const back = snapshotHost(undefined, fixture(), { ...options, texts: ["犬"] });
  assert.equal((await back.lookupEntry("jmdict:2", "犬")).status, "found");
  assert.equal((await back.lookupEntry("jmdict:1", "猫")).status, "failed");
});

test("edited text, dictionary versions and reading contexts fall back online", async () => {
  const fallback = online();
  const host = snapshotHost(fallback.host, fixture(), { ...options, texts: ["😀猫は猫"] });
  await host.tokenize!("😀猫は犬");
  await host.lookup("猫", "word", { reading: "ビョウ" });
  await host.lookup("😀猫は犬", "text");
  await snapshotHost(fallback.host, fixture(), { ...options, contentVersion: "v2", texts: ["😀猫は猫"] }).lookupEntry("jmdict:1", "猫");
  assert.equal(fallback.calls.length, 4);
  const offline = snapshotHost(undefined, fixture(), options);
  await assert.rejects(offline.tokenize!("edited"), /unavailable/);
  assert.equal((await offline.lookup("犬", "word")).status, "failed");
});

test("an unavailable pinned identity is never rebound to another prepared candidate or counted complete", async () => {
  const data = fixture();
  data.segments[0]!.tokens[1]!.entry = "private-upload:original";
  assert.equal(validateSnapshot(data)!.complete, false);
  const offline = snapshotHost(undefined, data, options);
  assert.equal((await offline.tokenize!("😀猫は猫"))[1]!.entry, "private-upload:original");
  assert.equal((await offline.lookupEntry("private-upload:original", "猫")).status, "failed");
  assert.equal((await offline.lookupEntry("jmdict:1", "猫")).status, "found");
  const fallback = online();
  const host = snapshotHost(fallback.host, data, options);
  await host.tokenize!("😀猫は猫");
  await host.lookupEntry("private-upload:original", "猫");
  assert.deepEqual(fallback.calls, [["entry", "private-upload:original", "猫"]]);
});

test("Python filters huge unapproved overlays while the SDK retains the authorized offline definition", async () => {
  const script = `import sys,json
sys.path.insert(0,sys.argv[1])
from offline_fixture import candidate,prepared
value=candidate()
value['entries'].insert(0,{'dictionary':'unapproved','senses':['private' * 1000000]})
result=prepared(('猫',),identify=lambda _: {'words':[{'candidates':[{'dictionary':'unapproved','entries':['private' * 1000000]},value]}]})
print(json.dumps(result.snapshot))`;
  const data = JSON.parse(execFileSync("python3", ["-c", script, resolve("..")], { encoding: "utf8" }));
  assert.equal(validateSnapshot(data)!.complete, false);
  const host = snapshotHost(undefined, data, options);
  await host.tokenize!("猫");
  const found = await host.lookupEntry("jmdict:1", "猫");
  assert.equal(found.status, "found");
  if (found.status === "found") assert.equal(found.definition!.senses[0]!.glosses[0]!.text, "cat");
  assert.ok(!JSON.stringify(data).includes("private"));
});

test("invalid hashes and UTF-16 spans disable only the bad segment", async () => {
  for (const alter of [
    (data: PreparedSnapshot) => { data.segments[0]!.contentHash = "0".repeat(64); },
    (data: PreparedSnapshot) => { data.segments[0]!.tokens[1]!.start = 1; },
    (data: PreparedSnapshot) => { data.segments[0]!.tokens[3]!.length = 200; },
    (data: PreparedSnapshot) => { data.segments[0]!.tokens[3]!.start = 2; },
  ]) {
    const data = fixture();
    alter(data);
    const clean = validateSnapshot(data)!;
    assert.deepEqual(clean.segments.map((s) => s.text), ["犬"]);
    assert.equal(clean.complete, false);
    const host = snapshotHost(undefined, data, { ...options, texts: ["犬"] });
    assert.equal((await host.lookupEntry("jmdict:2", "犬")).status, "found");
  }
});

test("malformed, oversized, missing and partial data never prevent fallback", async () => {
  for (const data of [null, "{", " ".repeat(MAX_SNAPSHOT_BYTES + 1), { ...fixture(), formatVersion: 5 }, { ...fixture(), segments: Array(17).fill(fixture().segments[0]) }]) {
    assert.equal(validateSnapshot(data), null);
    const fallback = online();
    await snapshotHost(fallback.host, data, options).lookup("猫", "word");
    assert.equal(fallback.calls.length, 1);
  }
  const partial = fixture();
  partial.segments[0]!.results[0]!.candidates[0]!.entries[0]!.senses[0]!.definitions[0]!.text = "x".repeat(25 * 1024);
  const clean = validateSnapshot(partial)!;
  assert.equal(clean.complete, false);
  assert.equal(clean.segments[0]!.results.length, 1);
  assert.equal((await snapshotHost(undefined, partial, { ...options, texts: ["犬"] }).lookupEntry("jmdict:2", "犬")).status, "found");
});

test("no-match is a prepared result and caller mutations cannot poison another lookup", async () => {
  const data = fixture();
  for (const r of data.segments[0]!.results) r.candidates = [];
  const fallback = online();
  const host = snapshotHost(fallback.host, data, { ...options, texts: ["😀猫は猫"] });
  assert.deepEqual(await host.lookup("猫", "word"), { status: "found", definition: null, candidates: [] });
  assert.deepEqual(fallback.calls, []);
  const fresh = snapshotHost(undefined, fixture(), { ...options, texts: ["😀猫は猫"] });
  const tokens = await fresh.tokenize!("😀猫は猫");
  tokens[1]!.start = 900;
  const answer = await fresh.lookupEntry("jmdict:1", "猫");
  if (answer.status === "found") answer.definition!.senses[0]!.glosses[0]!.text = "changed";
  const next = await fresh.lookupEntry("jmdict:1", "猫");
  assert.equal(next.status === "found" && next.definition!.senses[0]!.glosses[0]!.text, "cat");
  assert.equal((await fresh.tokenize!("😀猫は猫"))[1]!.start, 2);
});

test("live study and host methods come only from the supplied fallback", async () => {
  const fallback = online();
  const study: WordCardHost["study"] = { states: async () => ({}), urlForWord: async () => undefined, apply() {} };
  fallback.host.study = study;
  let reports = 0;
  fallback.host.report = () => { reports++; };
  const data = { ...fixture(), study: { access: "private" }, access: "private" };
  const host = snapshotHost(fallback.host, data, options);
  assert.equal(host.study, study);
  host.report!("card_opened");
  assert.equal(reports, 1);
  assert.equal(snapshotHost(undefined, data, options).study, undefined);
  assert.ok(!JSON.stringify(validateSnapshot(data)).includes("private"));
});

test("wrapping a class host preserves prototype methods, getters and their private state", async () => {
  class OnlineHost implements WordCardHost {
    #base = "https://shirabe.org";
    #study: WordCardHost["study"] = { states: async () => ({}), urlForWord: async () => undefined, apply() {} };
    get study() { return this.#study; }
    lookup = async (): Promise<LookupResult> => ({ status: "found", definition: null });
    lookupEntry = this.lookup;
    async pricingUrl() { return this.#base + "/pricing"; }
  }
  const fallback = new OnlineHost();
  const host = snapshotHost(fallback, fixture(), options);
  assert.equal(host.study, fallback.study);
  assert.equal(await host.pricingUrl!("test"), "https://shirabe.org/pricing");
  assert.equal("pricingUrl" in host, true);
});

test("canonical show and hover preferences survive snapshot validation without an always alias", async () => {
  for (const mode of ["show", "hover"] as const) {
    const data = fixture();
    data.preferences = { languages: ["en"], dictionaryReveal: { "jmdict:en": mode } };
    assert.deepEqual(validateSnapshot(data)?.preferences, data.preferences);
    const host = snapshotHost(undefined, data, { ...options, texts: ["😀猫は猫"] });
    const found = await host.lookupEntry("jmdict:1", "猫");
    assert.equal(found.status, "found");
    if (found.status === "found") assert.equal(found.definition!.senses[0]!.covered, mode === "hover");
  }
  assert.equal(validateSnapshot({ ...fixture(), preferences: { languages: ["en"], dictionaryReveal: { "jmdict:en": "always" } } }), null);
});

test("saved language order, reveal preferences, preparation date and license notices survive offline", async () => {
  const data = fixture();
  data.preferences = { languages: ["es", "en"], dictionaryReveal: { "jmdict:en": "hover" } };
  data.sources = [{ slug: "jmdict", name: "JMdict", source: { license: "CC BY-SA 4.0" } }];
  assert.deepEqual(validateSnapshot(data), data);
  const host = snapshotHost(undefined, data, { ...options, texts: ["😀猫は猫"], languages: ["eng"] });
  const result = await host.lookupEntry("jmdict:1", "猫");
  assert.equal(result.status, "found");
  if (result.status === "found") {
    assert.equal(result.definition!.senses[0]!.glosses[0]!.text, "gato");
    assert.equal(result.definition!.senses[0]!.covered, true);
    assert.equal(result.definition!.offline, true);
    assert.equal(result.definition!.preparedAt, data.createdAt);
  }
});

test("media loading permits same-origin sessions without forwarding API authorization", async () => {
  let request!: Request;
  const loaded = await loadSnapshot(filename(wire), { fetch: async (input, init) => {
    request = new Request(new URL((input instanceof Request ? input.url : String(input)), "https://ankiuser.net/study/"), init);
    return new Response(wire);
  } });
  assert.equal(request.credentials, "same-origin");
  assert.equal(request.url, "https://ankiuser.net/study/" + filename(wire));
  assert.equal(request.redirect, "error");
  assert.equal(request.method, "GET");
  assert.deepEqual([...request.headers], []);
  assert.equal(request.body, null);
  assert.equal(request.signal.aborted, false);
  assert.deepEqual(loaded, fixture());
});

test("media loading verifies its hash, reads JSON data and refuses paths or URLs", async () => {
  let calls = 0;
  const fetch: typeof globalThis.fetch = async (input, init) => {
    calls++;
    assert.equal(input, filename(wire));
    assert.equal(init?.credentials, "same-origin");
    assert.equal(init?.redirect, "error");
    return new Response(wire);
  };
  assert.deepEqual(await loadSnapshot(filename(wire), { fetch }), fixture());
  for (const name of ["../" + filename(wire), "https://example.test/" + filename(wire), "//example.test/" + filename(wire),
    "/" + filename(wire), "file:///tmp/a", filename(wire) + "?token=secret", filename(wire) + "#fragment"]) {
    assert.equal(await loadSnapshot(name, { fetch }), null);
  }
  assert.equal(calls, 1);
  assert.equal(await loadSnapshot(filename(wire), { fetch: async () => new Response(wire + " ") }), null);
  assert.equal(await loadSnapshot(filename(wire), { fetch: async () => new Response("missing", { status: 404 }) }), null);
  assert.equal(await loadSnapshot(filename(wire), { fetch: async () => { throw new Error("offline"); } }), null);
});

test("the media byte cap stops a stream before buffering its full body", async () => {
  let pulled = 0, cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) { pulled++; controller.enqueue(new Uint8Array(16 * 1024)); },
    cancel() { cancelled = true; },
  });
  assert.equal(await loadSnapshot(filename(wire), { fetch: async () => new Response(stream) }), null);
  assert.ok(pulled <= 10);
  assert.equal(cancelled, true);
});

test("prepared popup keeps dictionary HTML inert and plays verified local audio only on click", async () => {
  const { Window } = await import("happy-dom");
  const window = new Window({ url: "https://example.test" });
  Object.assign(globalThis, {
    window, document: window.document, HTMLElement: window.HTMLElement, Node: window.Node,
    DOMRect: window.DOMRect, getComputedStyle: window.getComputedStyle.bind(window),
  });
  const payload = '<img src="https://attacker.test" onerror="alert(1)"><script>alert(1)</script>';
  const data = recordedFixture();
  for (const result of data.segments[0]!.results) result.candidates[0]!.entries[0]!.senses[0]!.definitions[0]!.text = payload;
  const card = await import("@shirabe-org/card/word-card/card");
  let reads = 0;
  const played: string[] = [];
  const originalAudio = globalThis.Audio;
  (globalThis as unknown as { Audio: unknown }).Audio = class {
    constructor(src: string) { played.push(src); }
    play() { return Promise.resolve(); }
  };
  card.setCardHost(snapshotHost(undefined, data, { ...options, texts: ["😀猫は猫"], fetch: async () => { reads++; return new Response(recordingBytes); } }));
  card.setWriteState({ readOnly: true, connected: false, canWrite: false });
  card.setPopupOptions({ ...card.DEFAULT_CARD_OPTIONS, audioAutoplay: true });
  const word = window.document.createElement("span");
  word.className = "sb-word";
  word.textContent = "猫";
  word.dataset.lemma = "猫";
  word.dataset.surface = "猫";
  word.dataset.entry = "jmdict:1";
  window.document.body.append(word);
  try {
    card.showNow(word as unknown as HTMLElement);
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
    const shadow = window.document.querySelector(".sb-host")!.shadowRoot!;
    assert.ok(shadow.textContent!.includes(payload));
    assert.equal(shadow.querySelectorAll("script, img").length, 0);
    assert.equal(reads, 0);
    const audio = shadow.querySelector(".sb-audio") as unknown as HTMLButtonElement;
    assert.ok(audio);
    assert.equal(audio.getAttribute("aria-label"), "Play saved pronunciation");
    assert.match(audio.title, /Shirabe/);
    audio.click();
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
    assert.equal(reads, 1);
    assert.deepEqual(played, [`data:audio/mp4;base64,${Buffer.from(recordingBytes).toString("base64")}`]);
    assert.ok(shadow.querySelector(".sb-popup.sb-visible"));
    assert.ok(shadow.textContent!.includes(payload));
  } finally {
    globalThis.Audio = originalAudio;
    card.setPopupOptions(card.DEFAULT_CARD_OPTIONS);
    card.dismissPopup();
    await window.happyDOM.close();
  }
});
