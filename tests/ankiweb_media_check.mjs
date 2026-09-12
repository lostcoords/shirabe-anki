#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const manifest = JSON.parse(readFileSync(process.argv[2], "utf8"));
const expected = manifest.media;
if (!manifest.deck.startsWith("Shirabe Test ") || !Object.keys(expected).length || Object.keys(expected).length > 32 ||
    Object.entries(expected).some(([name, file]) => !/^_?shirabe_[a-z0-9_.-]+$/.test(name) ||
      !/^[a-f0-9]{64}$/.test(file.sha256) || !Number.isInteger(file.bytes) || file.bytes <= 0 || file.bytes > 4 * 1024 * 1024)) {
  throw new Error("Expected a bounded Shirabe test fixture manifest.");
}

async function verify({ media: expected, noteIds }) {
  if (location.origin !== "https://ankiuser.net" || location.pathname !== "/study") {
    throw new Error("Open the temporary test deck in AnkiWeb first.");
  }
  const editing = Array.from(document.querySelectorAll("a[href]")).map(a => new URL(a.href).pathname)
    .map(path => /^\/edit\/(\d+)$/.exec(path)).find(Boolean);
  if (!editing || !noteIds.includes(Number(editing[1]))) throw new Error("The current card is not part of the test fixture.");
  const results = [];
  for (const [name, file] of Object.entries(expected)) {
    if (new URL(name, document.baseURI).origin !== location.origin) throw new Error("Unexpected media origin.");
    try {
      const response = await fetch(name, { credentials: "same-origin", redirect: "error", signal: AbortSignal.timeout(10000) });
      if (!response.ok || Number(response.headers.get("content-length")) > 4 * 1024 * 1024) {
        results.push({ name, status: response.status, ok: false });
        continue;
      }
      const bytes = await response.arrayBuffer();
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), x => x.toString(16).padStart(2, "0")).join("");
      results.push({ name, status: response.status, bytes: bytes.byteLength, sha256: hash,
        ok: bytes.byteLength === file.bytes && hash === file.sha256 });
    } catch {
      results.push({ name, ok: false, error: "Media request failed" });
    }
  }
  return { ok: results.every(x => x.ok), origin: location.origin, files: results };
}

const code = `async (page) => await page.evaluate(${verify.toString()}, ${JSON.stringify({ media: expected, noteIds: manifest.notes.map(n => n.id) })})`;
const output = execFileSync(join(homedir(), "bin/playwright-cli"), ["run-code", code, "--raw"], {
  encoding: "utf8", timeout: 60000, maxBuffer: 1024 * 1024,
});
process.stdout.write(output);
const report = JSON.parse(output);
if (process.argv[3]) writeFileSync(process.argv[3], JSON.stringify(report, null, 2) + "\n", { flag: "wx" });
process.exitCode = report.ok === true ? 0 : 1;
