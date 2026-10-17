import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"
import { basename, dirname, join, resolve } from "node:path"
import { parseHTML } from "linkedom"
import { CardRuntime } from "@shirabe-org/anki-runtime"

const [input, output] = process.argv.slice(2)
if (!input || !output || resolve(input) === resolve(output))
  throw new Error("Usage: node script/study_runtime_profile.mjs PROFILE_REPORT.json OUTPUT.json")
const source = JSON.parse(await readFile(input, "utf8"))
const resources = {}
for (const [name, metadata] of Object.entries(source.sample_media)) {
  if (!name.endsWith(".js") || basename(name) !== name || !metadata.sha256) continue
  const code = await readFile(join(dirname(input), "collection.media", name), "utf8")
  if (createHash("sha256").update(code).digest("hex") !== metadata.sha256)
    throw new Error(`Sample script changed: ${name}`)
  resources[name] = code
}
const results = []
for (const note of source.fixtures) {
  const type = source.note_types.find((row) => row.definition.id === note.mid).definition
  for (const card of note.cards) {
    const runtime = await CardRuntime.create()
    try {
      for (const side of ["question", "answer"]) {
        if (typeof card[side] !== "string") continue
        const html = `<!doctype html><html><head><style>${type.css}</style></head><body>${card[side]}</body></html>`
        const started = performance.now()
        let result = runtime.render(html, resources)
        if (!result.error) result = runtime.advance(1000)
        const document = result.html ? parseHTML(result.html).document : null
        results.push({
          type: type.name,
          note: note.id,
          card: card.id,
          side,
          elapsedMs: performance.now() - started,
          error: result.error,
          diagnostics: result.diagnostics,
          componentLinks: document?.querySelectorAll("#component a").length,
          pitchCharacters: document
            ? [...document.querySelectorAll(".pitch-char")].map((node) => node.textContent)
            : [],
          randomExamples: document?.querySelectorAll("#random-examples-container .example").length,
        })
      }
    } finally {
      runtime.dispose()
    }
  }
}
await writeFile(
  output,
  `${JSON.stringify(
    {
      sourceAnki: source.anki,
      scope:
        "Saved Anki-rendered faces executed by the built restricted runtime. This does not exercise Shirabe's template renderer, browser layout, media, the review UI, or complete script compatibility. Original profile unopened. Runtime source and output remain untrusted until the display boundary validates them.",
      faces: results.length,
      failures: results.filter((row) => row.error).length,
      facesWithConsoleErrors: results.filter((row) =>
        row.diagnostics.some((item) => item.level === "error"),
      ).length,
      results,
    },
    null,
    2,
  )}\n`,
)
