// Bundle the suite and run it under node:test, the way the extension does: the
// card is TypeScript with `.js` import specifiers, which Node cannot run and
// esbuild can.
import { build } from "esbuild";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

process.chdir(fileURLToPath(new URL(".", import.meta.url)));

const dir = mkdtempSync(join(tmpdir(), "shirabe-card-test-"));
const requested = process.argv.slice(2);
const tests = readdirSync(".").filter((name) => name.endsWith(".test.ts"))
  .sort().map((name) => `./${name}`)
  .filter((path) => requested.length ? requested.includes(path) : true);
if (!tests.length) throw new Error("No matching card tests");

try {
  await build({
    entryPoints: tests,
    absWorkingDir: process.cwd(),
    bundle: true, platform: "node", format: "cjs", outdir: dir, logLevel: "silent",
  });
  execFileSync(process.execPath, ["--test", "--test-concurrency=1", `${dir}/*.js`], { stdio: "inherit" });
} finally {
  rmSync(dir, { recursive: true, force: true });
}
