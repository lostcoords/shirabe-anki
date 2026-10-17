import { execFileSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

const archive = resolve(process.argv[2] ?? "artifacts/shirabe-org-anki-runtime-0.1.0.tgz");
const directory = await mkdtemp(join(tmpdir(), "shirabe-runtime-consumer-"));
try {
  await writeFile(join(directory, "package.json"), JSON.stringify({ private: true, type: "module" }));
  execFileSync("npm", ["install", "--ignore-scripts", "--no-audit", "--no-fund", archive], { cwd: directory, stdio: "inherit" });
  await writeFile(join(directory, "verify.mjs"), `
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { CardRuntime } from "@shirabe-org/anki-runtime";
const runtime = await CardRuntime.create();
try {
  const result = runtime.render('<p id="value"></p><script>document.getElementById("value").textContent = "Japanese: 猫";</script>');
  assert.equal(result.error, null);
  assert.match(result.html, /Japanese: 猫/);
} finally { runtime.dispose(); }
const root = new URL("node_modules/@shirabe-org/anki-runtime/dist/", import.meta.url);
const manifest = JSON.parse(await readFile(new URL("manifest.json", root), "utf8"));
const frame = await readFile(new URL("frame.js", root));
assert.equal(manifest.frameHash, "sha256-" + createHash("sha256").update(frame).digest("base64"));
for (const name of ["worker.mjs", "preview.mjs", "bridges/study_card_typed_answer.js"]) assert.ok((await readFile(new URL(name, root))).length);
console.log("Installed runtime archive executes card scripts and includes matching web assets.");
`);
  execFileSync(process.execPath, [join(directory, "verify.mjs")], { cwd: directory, stdio: "inherit" });
} finally {
  await rm(directory, { recursive: true, force: true });
}
