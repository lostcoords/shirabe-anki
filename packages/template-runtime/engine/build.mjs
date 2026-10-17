import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
import { copyFile, mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const root = fileURLToPath(new URL("../../../", import.meta.url))
const inputs = await Promise.all(
  [import.meta.url, new URL("./rejections.h", import.meta.url)].map((path) =>
    readFile(new URL(path)),
  ),
)
const commit = "df4efb9ef2cb25c417ecb57986da462d11b244ed"
const image =
  "emscripten/emsdk@sha256:c89732ef63a56de5a96395c5a8c1c7904f7420131a045406e6fedc4cbe1cc198"
const directory = await mkdtemp(join(root, "tmp/study-quickjs-"))
const source = join(directory, "source")
await mkdir(source)
const response = await fetch(
  `https://codeload.github.com/justjake/quickjs-emscripten/tar.gz/${commit}`,
)
if (!response.ok) throw new Error(`Source download failed: ${response.status}`)
const archive = Buffer.from(await response.arrayBuffer())
const checksum = createHash("sha256").update(archive).digest("hex")
if (checksum !== "b6f342ad8e18bcff29ab4e78467d6e33c53c9e1a47986e58b91a1462beb31a8e")
  throw new Error("QuickJS source checksum changed")
await writeFile(join(directory, "source.tar.gz"), archive)
process.stdout.write(`QuickJS source SHA256: ${checksum}\n`)

async function run(command, args, options = {}) {
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit", ...options })
    child.on("error", reject)
    child.on("exit", (code) =>
      code === 0 ? resolve() : reject(new Error(`${command} exited ${code}`)),
    )
  })
}

await run("tar", ["-xzf", join(directory, "source.tar.gz"), "--strip-components=1", "-C", source])
let wrapper = await readFile(join(source, "c/interface.c"), "utf8")
if (wrapper.includes("JS_SetContextOpaque"))
  throw new Error("Upstream now uses the context opaque slot")
function replaceOnce(before, after) {
  if (wrapper.split(before).length !== 2)
    throw new Error(`Upstream patch anchor changed: ${before}`)
  wrapper = wrapper.replace(before, after)
}
replaceOnce(
  '#define PKG "quickjs-emscripten: "',
  '#include "study-rejections.h"\n#define PKG "quickjs-emscripten: "',
)
replaceOnce(
  "  return ctx;\n}\n\nvoid QTS_FreeContext(JSContext *ctx) {\n  JS_FreeContext(ctx);",
  "  if (study_init_rejections(ctx) < 0) {\n    JS_FreeContext(ctx);\n    return NULL;\n  }\n  return ctx;\n}\n\nvoid QTS_FreeContext(JSContext *ctx) {\n  study_free_rejections(ctx);\n  JS_FreeContext(ctx);",
)
await writeFile(join(source, "c/interface.c"), wrapper)
await writeFile(join(source, "c/study-rejections.h"), inputs[1])
const ffi = await readFile(
  fileURLToPath(import.meta.resolve("@jitl/quickjs-singlefile-browser-release-sync/ffi")),
  "utf8",
)
const symbols = [...ffi.matchAll(/\.cwrap\("([^"]+)"/g)].map((match) => `_${match[1]}`)
if (symbols.length < 50) throw new Error("Missing upstream FFI exports")
await writeFile(
  join(source, "study-symbols.json"),
  JSON.stringify(["_malloc", "_free", ...new Set(symbols)]),
)
const version = (await readFile(join(source, "vendor/quickjs/VERSION"), "utf8")).trim()
const args = [
  "c/interface.c",
  ...["quickjs", "dtoa", "libregexp", "libunicode", "cutils", "quickjs-libc"].map(
    (name) => `vendor/quickjs/${name}.c`,
  ),
  "-Oz",
  "-flto",
  "-D_GNU_SOURCE",
  `-DCONFIG_VERSION="${version}"`,
  "-DCONFIG_STACK_CHECK",
  "--no-entry",
  "-s",
  "MODULARIZE=1",
  "-s",
  "EXPORT_ES6=1",
  "-s",
  "EXPORT_NAME=QuickJSRaw",
  "-s",
  "ENVIRONMENT=web,worker",
  "-s",
  "IMPORTED_MEMORY=1",
  "-s",
  "ALLOW_MEMORY_GROWTH=1",
  "-s",
  "ALLOW_TABLE_GROWTH=1",
  "-s",
  "STACK_SIZE=5MB",
  "-s",
  "FILESYSTEM=0",
  "-s",
  "SINGLE_FILE=1",
  "-s",
  "INVOKE_RUN=0",
  "-s",
  "SUPPORT_ERRNO=0",
  "-s",
  "EXPORTED_FUNCTIONS=@study-symbols.json",
  "-s",
  "EXPORTED_RUNTIME_METHODS=@exportedRuntimeMethods.json",
  "--pre-js",
  "templates/pre-extension.js",
  "--pre-js",
  "templates/pre-wasmMemory.js",
  "-o",
  "study-engine.mjs",
]
await run("docker", [
  "run",
  "--rm",
  "--network=none",
  "--mount",
  `type=bind,source=${source},target=/build`,
  "--workdir",
  "/build",
  image,
  "emcc",
  ...args,
])
const target = join(root, "vendor/study-runtime/quickjs-engine.mjs")
await mkdir(dirname(target), { recursive: true })
const licenses = await Promise.all(
  ["LICENSE", "vendor/quickjs/LICENSE"].map((path) => readFile(join(source, path), "utf8")),
)
const compiled = await readFile(join(source, "study-engine.mjs"), "utf8")
const inputHash = createHash("sha256").update(Buffer.concat(inputs)).digest("hex")
await writeFile(
  target,
  `/*! Study engine sources SHA256: ${inputHash}\n${licenses.join("\n")}\n*/\n${compiled}`,
)
await copyFile(
  join(source, "LICENSE"),
  join(root, "vendor/study-runtime/quickjs-emscripten-LICENSE"),
)
await copyFile(
  join(source, "vendor/quickjs/LICENSE"),
  join(root, "vendor/study-runtime/quickjs-LICENSE"),
)
process.stdout.write(`Built ${target}\nSource/build scratch: ${directory}\n`)
