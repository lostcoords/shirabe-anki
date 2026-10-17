import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { readFile } from "node:fs/promises"
import test from "node:test"
import { pathToFileURL } from "node:url"
import { join } from "node:path"

const directory = process.env.STUDY_RUNTIME_BUILD_DIR ? pathToFileURL(join(process.env.STUDY_RUNTIME_BUILD_DIR, "/")) : new URL("../dist/", import.meta.url)

test("the release frame hash describes the frame used by the worker", async () => {
  const manifest = JSON.parse(await readFile(new URL("manifest.json", directory), "utf8"))
  const frame = await readFile(new URL("frame.js", directory))
  assert.equal(manifest.frameHash, `sha256-${createHash("sha256").update(frame).digest("base64")}`)
  const worker = await readFile(new URL("worker.mjs", directory), "utf8")
  assert.equal(worker.match(/\\0asm|\\x00asm|AGFzbQEAAA/g)?.length, 1)
})

test("the patched engine records its exact build inputs", async () => {
  const inputs = await Promise.all(["build.mjs", "rejections.h"].map((name) => readFile(new URL(`../engine/${name}`, import.meta.url))))
  const hash = createHash("sha256").update(Buffer.concat(inputs)).digest("hex")
  const engine = await readFile(new URL("../../../vendor/study-runtime/quickjs-engine.mjs", import.meta.url), "utf8")
  assert.ok(engine.includes(`Study engine sources SHA256: ${hash}`))
})
