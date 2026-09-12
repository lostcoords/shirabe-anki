import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify, parseArgs } from "node:util";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";
import { readFile, writeFile, appendFile } from "node:fs/promises";
import { gunzipSync } from "node:zlib";
import { createHash } from "node:crypto";
import { loadavg } from "node:os";
import { fileURLToPath } from "node:url";

const { values: options } = parseArgs({
  options: {
    "android-root": { type: "string" },
    "adb-port": { type: "string" },
    serial: { type: "string" },
    fixture: { type: "string" },
    output: { type: "string" },
  },
});
for (const name of [
  "android-root",
  "adb-port",
  "serial",
  "fixture",
  "output",
]) {
  assert(options[name], "Required option: --" + name);
}
assert(
  /^emulator-\d+$/.test(options.serial),
  "Use an isolated emulator serial",
);
assert(
  /^\d+$/.test(options["adb-port"]) &&
    Number(options["adb-port"]) > 1023 &&
    Number(options["adb-port"]) < 65536 &&
    options["adb-port"] !== "5037",
  "Use a private adb server",
);
const run = promisify(execFile);
const root = new URL("./", import.meta.url);
const output = pathToFileURL(resolve(options.output));
const log = new URL(output.href + "l");
await writeFile(log, "", { flag: "wx" });
const probe = fileURLToPath(new URL("android_probe.py", root));
const probeArgs = [
  "--android-root",
  resolve(options["android-root"]),
  "--adb-port",
  options["adb-port"],
  "--serial",
  options.serial,
];
const adb = join(resolve(options["android-root"]), "sdk/platform-tools/adb");
const adbArgs = ["-P", options["adb-port"], "-s", options.serial];
const fixtureUrl = pathToFileURL(resolve(options.fixture));
const fixtureBytes = await readFile(fixtureUrl);
const fixture = JSON.parse(fixtureBytes);
assert(fixture.notes.length >= 2 && fixture.notes.length <= 200);
fixture.notes.forEach((row, index) => assert.equal(row.index, index));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const candidates = [];
for (const row of fixture.notes) {
  const stored = await readFile(row.path);
  assert.equal(sha(stored), row.sha256);
  const envelope = JSON.parse(stored);
  const raw = gunzipSync(Buffer.from(envelope.data, "base64"));
  assert.equal(sha(raw), row.decodedSha256);
  const segment = JSON.parse(raw).segments[0];
  candidates[row.index] = segment.results.flatMap((result) => {
    const token = segment.tokens[result.tokenIndex];
    const candidate = result.candidates.find(
      (c) => c.dictionary + ":" + c.sourceId === token.entry,
    );
    return candidate && token.content !== false
      ? [
          {
            entry: token.entry,
            surface: token.surface,
            headword: candidate.headword,
          },
        ]
      : [];
  });
  assert(candidates[row.index].length);
}
const android = async (kind, ...args) => {
  const execution = { timeout: 45000, maxBuffer: 2 ** 20 };
  if (kind === "probe")
    return (await run("python3", [probe, ...probeArgs, ...args], execution))
      .stdout;
  assert.equal(kind, "adb");
  const avd = (await run(adb, [...adbArgs, "emu", "avd", "name"], execution))
    .stdout;
  assert.equal(
    avd.split(/\r?\n/)[0],
    "shirabe_phase0",
    "Unexpected AVD; refusing input",
  );
  return (await run(adb, [...adbArgs, ...args], execution)).stdout;
};
const port = (await android("probe", "debug-port")).trim();
assert(/^\d+$/.test(port));
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const visible = pages.filter(
  (p) => p.type === "page" && JSON.parse(p.description || "{}").visible,
);
assert.equal(visible.length, 1);
const page = visible[0];
assert.equal(page.title, "AnkiDroid");
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.onopen = resolve;
  socket.onerror = reject;
});
let sequence = 0;
const pending = new Map(),
  traffic = [],
  contextEvents = [];
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (message.id) {
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    clearTimeout(request.timer);
    message.error
      ? request.reject(Error(JSON.stringify(message.error)))
      : request.resolve(message.result);
  } else if (message.method === "Network.requestWillBeSent") {
    const url = new URL(message.params.request.url);
    traffic.push({
      requestId: message.params.requestId,
      origin: url.origin,
      path: url.pathname,
      type: message.params.type,
      timestamp: message.params.timestamp,
    });
  } else if (message.method?.startsWith("Runtime.executionContext")) {
    contextEvents.push({
      method: message.method,
      id: message.params?.context?.id || message.params?.executionContextId,
    });
  }
};
const rpc = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(Error(`CDP timeout: ${method}`));
    }, 30000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
const evaluate = async (fn, ...args) => {
  const reply = await rpc("Runtime.evaluate", {
    expression: `(${fn.toString()})(...${JSON.stringify(args)})`,
    awaitPromise: true,
    returnByValue: true,
  });
  if (reply.exceptionDetails)
    throw Error(JSON.stringify(reply.exceptionDetails));
  return reply.result.value;
};
const state = () =>
  evaluate(() => ({
    index: Number(
      document.querySelector("[data-probe-index]")?.dataset.probeIndex,
    ),
    snapshot:
      document.querySelector("[data-shirabe-snapshot]")?.textContent.trim() ||
      null,
    answer: !!document.querySelector("#answer"),
    words: document.querySelectorAll(".sb-word").length,
    visible: document.visibilityState,
    viewport: [innerWidth, innerHeight],
    online: navigator.onLine,
    marker: window.__probeEpoch || null,
  }));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function ready(index, answer) {
  const start = Date.now();
  while (Date.now() - start < 15000) {
    const s = await state();
    if (s.index === index && s.answer === answer && (!answer || s.words)) {
      assert.equal(s.visible, "visible");
      assert.equal(s.online, false);
      assert(s.viewport.every((n) => n > 0));
      if (answer) assert.equal(s.snapshot, fixture.notes[index].filename);
      return s;
    }
    await sleep(60);
  }
  throw Error(`Native card did not become ready: ${index}/${answer}`);
}
async function frames(count = 3) {
  return evaluate(async (count) => {
    let seen = 0;
    await new Promise((resolve, reject) => {
      let frame;
      const timer = setTimeout(() => {
        cancelAnimationFrame(frame);
        reject(Error("Native frames stalled"));
      }, 10000);
      const next = () => {
        seen++;
        if (seen < count) frame = requestAnimationFrame(next);
        else {
          clearTimeout(timer);
          resolve();
        }
      };
      frame = requestAnimationFrame(next);
    });
    if (document.hidden || !innerWidth || !innerHeight)
      throw Error("Hidden or zero-sized viewport");
    return seen;
  }, count);
}
async function collect() {
  await frames();
  for (let n = 0; n < 3; n++) {
    await rpc("HeapProfiler.collectGarbage");
    await frames();
  }
  return {
    heap: await rpc("Runtime.getHeapUsage"),
    dom: await rpc("Memory.getDOMCounters"),
  };
}
const samples = [],
  checkpoints = [];
const report = {
  surface: "AnkiDroid 2.24.1 native Previewer",
  webview: "124.0.6367.219",
  sdkSha256: fixture.sdkSha256,
  apkgSha256: fixture.apkgSha256,
  selectedCards: fixture.notes.length,
  requestedWarmupSweeps: 1,
  requestedMeasuredSweeps: 2,
  hostLoadAtStart: loadavg(),
  timingScope:
    "Shared loaded Mac, software-rendered emulator; diagnostic timings, not a regression budget",
  activation:
    "Native Android DPAD navigation; programmatic token click through the installed SDK handler",
  samples,
  checkpoints,
  traffic,
  contextEvents,
  pageId: page.id,
  port,
  fixturePath: fileURLToPath(fixtureUrl),
  fixtureSha256: sha(fixtureBytes),
  driverSha256: sha(await readFile(fileURLToPath(import.meta.url))),
};
async function checkpoint(label) {
  const metrics = await collect();
  const meminfo = await android(
    "adb",
    "shell",
    "dumpsys",
    "meminfo",
    "com.ichi2.anki",
  );
  const row = {
    label,
    ...metrics,
    state: await state(),
    hostLoad: loadavg(),
    appPssKb: Number(meminfo.match(/TOTAL PSS:\s+(\d+)/)?.[1] || 0),
    sampleCount: samples.length,
  };
  checkpoints.push(row);
  await appendFile(log, JSON.stringify({ checkpoint: row }) + "\n");
  console.log(
    JSON.stringify({
      checkpoint: label,
      heap: row.heap.usedSize,
      appPssKb: row.appPssKb,
      samples: samples.length,
    }),
  );
}
async function lookup(index, warm) {
  const result = await evaluate(async (choices) => {
    const word = [...document.querySelectorAll(".sb-word")].find((w) =>
      choices.some(
        (c) => c.entry === w.dataset.entry && c.surface === w.dataset.surface,
      ),
    );
    if (!word) throw Error("No exact prepared token wrapper");
    const expected = choices.find(
      (c) =>
        c.entry === word.dataset.entry && c.surface === word.dataset.surface,
    );
    word.scrollIntoView({ block: "center" });
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
    const before = getComputedStyle(word).backgroundColor;
    const transition = getComputedStyle(word).transition;
    const ends = [];
    const end = (e) => {
      if (e.propertyName === "background-color") ends.push(e.elapsedTime);
    };
    word.addEventListener("transitionend", end);
    const started = performance.now();
    const wait = async (predicate) => {
      const start = performance.now();
      while (performance.now() - start < 10000) {
        if (predicate()) return;
        await new Promise((r) => requestAnimationFrame(r));
      }
      throw Error("Popup content/transition did not settle");
    };
    try {
      word.click();
      let link;
      await wait(() => {
        link = document
          .querySelector(".sb-host")
          ?.shadowRoot?.querySelector(".sb-visible .sb-headword");
        return (
          link &&
          new URL(link.href).searchParams.get("entry") === expected.entry
        );
      });
      const visibleMs = performance.now() - started;
      const text = link.cloneNode(true);
      text.querySelectorAll("rt,rp").forEach((node) => node.remove());
      const rendered = text.textContent;
      if (rendered !== expected.headword)
        throw Error(
          `Rendered headword mismatch: ${JSON.stringify({ expected: expected.headword, rendered })}`,
        );
      await wait(() => ends.length >= 1);
      window.ShirabeCard.dismiss();
      await wait(() => ends.length >= 2);
      if (word.classList.contains("sb-word-open"))
        throw Error("Popup did not dismiss");
      return {
        entry: expected.entry,
        headword: expected.headword,
        rendered,
        visibleMs,
        transition,
        before,
        ends,
      };
    } finally {
      word.removeEventListener("transitionend", end);
    }
  }, candidates[index]);
  const row = { index, warm, ...result };
  samples.push(row);
  await appendFile(log, JSON.stringify({ sample: row }) + "\n");
  if (samples.length % 10 === 0)
    console.log(JSON.stringify({ lookups: samples.length, index }));
}
async function visit(index, direction) {
  const start = Date.now(),
    requestsBefore = traffic.length;
  await android(
    "adb",
    "shell",
    "input",
    "keyevent",
    direction === "forward" ? "KEYCODE_DPAD_RIGHT" : "KEYCODE_DPAD_LEFT",
  );
  const front = await ready(index, false);
  await android("adb", "shell", "input", "keyevent", "KEYCODE_DPAD_RIGHT");
  const answer = await ready(index, true);
  await appendFile(
    log,
    JSON.stringify({
      navigation: {
        index,
        direction,
        front,
        answer,
        wallMs: Date.now() - start,
        requests: traffic.slice(requestsBefore),
      },
    }) + "\n",
  );
  await lookup(index, false);
  await lookup(index, true);
}
try {
  await rpc("Runtime.enable");
  await rpc("Network.enable");
  report.androidInfo = JSON.parse(await android("probe", "info"));
  report.userAgent = await evaluate(() => navigator.userAgent);
  assert(report.androidInfo.ankidroid.includes("2.24.1"));
  assert(report.userAgent.includes("124.0.6367.219"));
  const sdkName = Object.keys(fixture.media).find((name) =>
    name.endsWith("_shirabe-card.js"),
  );
  report.servedSdkSha256 = await evaluate(async (name) => {
    if (location.hostname !== "127.0.0.1")
      throw Error("Expected private Android media origin");
    const bytes = await (
      await fetch(new URL(name, location.href))
    ).arrayBuffer();
    return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
      .map((n) => n.toString(16).padStart(2, "0"))
      .join("");
  }, sdkName);
  assert.equal(report.servedSdkSha256, fixture.sdkSha256);
  const initial = await state();
  assert.equal(initial.index, 0);
  if (!initial.answer) {
    await ready(0, false);
    await frames();
    await android("adb", "shell", "input", "keyevent", "KEYCODE_DPAD_RIGHT");
  }
  assert.deepEqual((await ready(0, true)).index, 0);
  const before = await collect();
  await evaluate(() => {
    window.__probeBallast = Array.from({ length: 100000 }, (_, i) => ({
      i,
      values: Array(12).fill(i),
    }));
  });
  const allocated = await collect();
  await evaluate(() => {
    delete window.__probeBallast;
  });
  const recovered = await collect();
  report.gcProof = {
    before: before.heap.usedSize,
    allocated: allocated.heap.usedSize,
    recovered: recovered.heap.usedSize,
  };
  assert(allocated.heap.usedSize - before.heap.usedSize > 4 * 1024 * 1024);
  assert(
    allocated.heap.usedSize - recovered.heap.usedSize >
      (allocated.heap.usedSize - before.heap.usedSize) * 0.9,
  );
  for (let sweep = 0; sweep < 3; sweep++) {
    await lookup(0, false);
    await lookup(0, true);
    for (let index = 1; index < fixture.notes.length; index++)
      await visit(index, "forward");
    for (let index = fixture.notes.length - 2; index >= 0; index--)
      await visit(index, "backward");
    await checkpoint(
      sweep === 0 ? "baseline-after-warmup" : `after-measured-sweep-${sweep}`,
    );
  }
  await frames(120);
  await checkpoint("after-idle-120-frames");
  assert.equal(new Set(samples.map((s) => s.index)).size, fixture.notes.length);
  report.completed = true;
} catch (error) {
  report.completed = false;
  report.error = String(error);
  console.error(report.error);
  process.exitCode = 1;
} finally {
  report.hostLoadAtEnd = loadavg();
  await writeFile(output, JSON.stringify(report, null, 2) + "\n", {
    flag: "wx",
  });
  socket.close();
}
