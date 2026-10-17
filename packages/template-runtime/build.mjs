import { createHash } from "node:crypto"
import { readFile, copyFile, mkdir, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { build } from "esbuild"

const root = dirname(fileURLToPath(import.meta.url))
const frame = (
  await Promise.all(
    [
      "bridges/study_card_speech.js",
      "bridges/study_card_audio.js",
      "src/frame.js",
    ].map((path) => readFile(join(root, path), "utf8")),
  )
).join("\n")
const frameHash = `sha256-${createHash("sha256").update(frame).digest("base64")}`
const typedAnswer = await readFile(
  join(root, "bridges/study_card_typed_answer.js"),
  "utf8",
)
const dom = await build({
  entryPoints: [join(root, "src/dom.js")],
  bundle: true,
  platform: "browser",
  format: "iife",
  write: false,
  legalComments: "inline",
  plugins: [
    {
      name: "typed-answer",
      setup(plugin) {
        plugin.onResolve({ filter: /^study-runtime-typed-answer$/ }, (args) => ({
          path: args.path,
          namespace: "typed-answer",
        }))
        plugin.onLoad({ filter: /.*/, namespace: "typed-answer" }, () => ({
          contents: `const window = {}; ${typedAnswer}; export default window.studyTypedAnswer`,
          loader: "js",
        }))
      },
    },
  ],
})
const options = {
  bundle: true,
  platform: "browser",
  format: "esm",
  legalComments: "inline",
  plugins: [
    {
      name: "isolated-dom",
      setup(plugin) {
        plugin.onResolve({ filter: /^study-runtime-(dom|frame)$/ }, (args) => ({
          path: args.path,
          namespace: "isolated-dom",
        }))
        plugin.onLoad({ filter: /.*/, namespace: "isolated-dom" }, (args) => ({
          contents:
            args.path === "study-runtime-dom"
              ? `export default ${JSON.stringify(dom.outputFiles[0].text)}`
              : `export const source = ${JSON.stringify(frame)}; export const hash = ${JSON.stringify(frameHash)}`,
          loader: "js",
        }))
      },
    },
  ],
}
const output = process.env.STUDY_RUNTIME_BUILD_DIR || join(root, "dist")
await mkdir(join(output, "bridges"), { recursive: true })
await build({
  ...options,
  entryPoints: ["runtime", "worker", "client", "preview", "snapshot"].map((name) => join(root, `src/${name}.js`)),
  outdir: output,
  outExtension: { ".js": ".mjs" },
  minify: true,
})
for (const name of ["speech", "audio", "typed_answer"]) {
  await copyFile(join(root, `bridges/study_card_${name}.js`), join(output, `bridges/study_card_${name}.js`))
}
await writeFile(join(output, "frame.js"), frame)
await writeFile(join(output, "manifest.json"), JSON.stringify({ version: "0.1.0", frameHash }, null, 2) + "\n")
for (const name of ["quickjs-LICENSE", "quickjs-emscripten-LICENSE"]) {
  await copyFile(join(root, "../../vendor/study-runtime", name), join(output, name))
}
