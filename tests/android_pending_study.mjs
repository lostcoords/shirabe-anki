#!/usr/bin/env node
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { promisify } from "node:util";

const [port, androidRoot, output, sdkHash] = process.argv.slice(2);
assert(/^\d+$/.test(port || "") && androidRoot && output && /^[a-f0-9]{64}$/.test(sdkHash || ""),
  "Usage: node android_pending_study.mjs <CDP-port> <android-root> <new-output-directory> <SDK-sha256>");
const run = promisify(execFile);
const adbPath = resolve(androidRoot, "sdk/platform-tools/adb");
const adb = (...args) => run(adbPath, ["-P", "5041", "-s", "emulator-5580", ...args], { encoding: "buffer", maxBuffer: 8_000_000 });
assert.equal((await adb("emu", "avd", "name")).stdout.toString().split("\n")[0].trim(), "shirabe_phase0");
await mkdir(output);
const save = (name, value) => writeFile(resolve(output, name), JSON.stringify(value, null, 2) + "\n", { flag: "wx" });
const delay = (ms) => new Promise((resolveDelay) => setTimeout(resolveDelay, ms));
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = pages.find((candidate) => candidate.type === "page" && candidate.title === "AnkiDroid Flashcard");
assert(page, "AnkiDroid reviewer must be open");
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((opened, failed) => { socket.onopen = opened; socket.onerror = failed; });
let nextId = 0;
const commands = new Map();
const held = new Set();
const report = { sdkHash, startedAt: new Date().toISOString(), network: [], preflights: [], pauses: [], errors: [], passed: false };
function cdp(method, params = {}) {
  const id = ++nextId;
  return new Promise((done, failed) => {
    commands.set(id, { done, failed });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (message.id) {
    const command = commands.get(message.id);
    commands.delete(message.id);
    if (message.error) command?.failed(new Error(JSON.stringify(message.error)));
    else command?.done(message.result);
  } else if (message.method === "Fetch.requestPaused") {
    const { requestId, request } = message.params;
    const url = new URL(request.url);
    if (url.hostname === "127.0.0.1" && url.pathname === "/api/v1/me" && request.method === "GET") {
      held.add(requestId);
      report.pauses.push({ requestId, url: request.url, method: request.method, at: new Date().toISOString() });
    } else if (url.hostname === "127.0.0.1" && url.pathname === "/api/v1/me" && request.method === "OPTIONS") {
      report.preflights.push({ requestId, url: request.url, at: new Date().toISOString(), fixtureResponse: 204 });
      void cdp("Fetch.fulfillRequest", { requestId, responseCode: 204, responseHeaders: [
        { name: "Access-Control-Allow-Origin", value: report.before.origin },
        { name: "Access-Control-Allow-Methods", value: "GET, OPTIONS" },
        { name: "Access-Control-Allow-Headers", value: "authorization, content-type" },
        { name: "Access-Control-Allow-Private-Network", value: "true" }
      ] });
    } else {
      void cdp("Fetch.continueRequest", { requestId });
    }
  } else if (message.method === "Network.requestWillBeSent") {
    const { requestId, request, timestamp } = message.params;
    report.network.push({ kind: "request", requestId, url: request.url, method: request.method, timestamp });
  } else if (["Network.responseReceived", "Network.loadingFailed"].includes(message.method)) {
    const { requestId, timestamp, errorText, response } = message.params;
    report.network.push({ kind: message.method, requestId, timestamp, errorText, status: response?.status });
  } else if (message.method === "Runtime.exceptionThrown") {
    report.errors.push(message.params.exceptionDetails.text);
  }
};
async function evaluate(expression) {
  const value = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  assert(!value.exceptionDetails, JSON.stringify(value.exceptionDetails));
  return value.result.value;
}
const readReviewer = `(() => ({
  at: new Date().toISOString(), origin: location.origin,
  dummyTokenRetained: JSON.parse(localStorage.getItem("shirabe.link") || "null")?.access === "phase0-access-only-dummy",
  events: window.ShirabePhase0?.events || [],
  scripts: [...document.scripts].map(s => s.src).filter(Boolean),
  popup: [...document.querySelectorAll(".sb-host")].flatMap(host => [...host.shadowRoot.querySelectorAll(".sb-popup")].map(box => ({
    text: box.innerText, visible: box.classList.contains("sb-visible"), opacity: getComputedStyle(box).opacity,
    rect: box.getBoundingClientRect().toJSON()
  })))
}))()`;
let failure;
try {
  await cdp("Runtime.enable");
  await cdp("Network.enable");
  await cdp("Fetch.enable", { patterns: [{ urlPattern: "http://127.0.0.1:*/api/v1/me", requestStage: "Request" }] });
  await delay(1000);
  report.before = await evaluate(readReviewer);
  assert(report.before.dummyTokenRetained, "Expected the native OAuth fixture's retained dummy token");
  assert(report.before.scripts.some((src) => src.endsWith(`_shirabe_${sdkHash.slice(0, 20)}_shirabe-card.js`)), "Wrong SDK loaded");
  assert(report.before.events.some((event) => /_shirabe_snapshot_v[12]_.*\.json$/.test(event.url || "") && event.status === 200));
  assert(!report.before.events.some((event) => /\/api\//.test(event.url || "")), "API request before gesture");
  assert(!report.network.some((event) => /\/api\//.test(event.url || "")), "Network API request before gesture");
  assert.equal(held.size, 0);
  assert(!report.before.popup.some((popup) => popup.visible), "Start with the popup closed");
  await save("before-tap.json", report.before);
  report.tapCommandAt = new Date().toISOString();
  await adb("shell", "input", "tap", "61", "526");
  const deadline = Date.now() + 4000;
  while (!held.size && Date.now() < deadline) await delay(20);
  assert.equal(held.size, 1, "Expected one genuinely intercepted /me request");
  let reviewer;
  while (Date.now() < deadline) {
    reviewer = await evaluate(readReviewer);
    if (reviewer.popup.some((popup) => popup.visible && popup.opacity === "1" && /\bcat\b/.test(popup.text) && popup.text.includes("Saved for offline use"))) break;
    await delay(20);
  }
  assert(reviewer.popup.some((popup) => popup.visible && popup.opacity === "1" && /\bcat\b/.test(popup.text) && popup.text.includes("Saved for offline use")), "Saved definition must render before /me completes or times out");
  report.whileHeld = { ...reviewer, heldRequestIds: [...held] };
  assert(!reviewer.events.some((event) => /\/api\//.test(event.url || "")), "An API response completed while /me should be held");
  await save("popup-while-me-pending.json", report.whileHeld);
  await delay(500);
  const screenshot = await adb("exec-out", "screencap", "-p");
  await writeFile(resolve(output, "popup-while-me-pending.png"), screenshot.stdout, { flag: "wx" });
  report.screenshotAt = new Date().toISOString();
  report.heldAtScreenshot = [...held];
  assert.equal(report.heldAtScreenshot.length, 1);
  assert.equal(report.errors.length, 0);
  report.passed = true;
} catch (error) {
  failure = error;
  report.failure = String(error);
} finally {
  report.releaseAt = new Date().toISOString();
  for (const requestId of held) await cdp("Fetch.failRequest", { requestId, errorReason: "Aborted" });
  held.clear();
  await cdp("Fetch.disable");
  await delay(300);
  report.afterRelease = await evaluate(readReviewer);
  await save("pending-study-report.json", report);
  socket.close();
}
console.log(JSON.stringify({ passed: report.passed, output: resolve(output), pausedRequests: report.pauses, error: report.failure }, null, 2));
if (failure) throw failure;
