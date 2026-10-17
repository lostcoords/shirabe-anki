import { pathToFileURL } from "node:url";
import { resolve as resolveCheckout } from "node:path";
const shirabe = resolveCheckout(process.env.SHIRABE_PATH ?? new URL("../../shirabe", import.meta.url).pathname);
import { readFile, writeFile } from "node:fs/promises"
import { resolve } from "node:path"
import { chromium } from "playwright"
const { CardRuntime } = await import(pathToFileURL(resolveCheckout(shirabe, "packages/study-runtime/dist/runtime.mjs")))

const root = resolve(process.argv[2])
const corpus = JSON.parse(await readFile(resolve(root, "expected.json"), "utf8"))
const browser = await chromium.launch({ headless: true, args: ["--mute-audio"] })
const runtime = await CardRuntime.create()
try {
  const page = await browser.newPage()
  await page.addScriptTag({ path: "app/javascript/study_card_typed_answer.js" })
  const failures = await page.evaluate((cases) => {
    const parts = (node) => [...node.childNodes].map((child) =>
      child.nodeType === Node.TEXT_NODE ? child.textContent : [child.tagName, child.className, child.id, child.textContent])
    return cases.flatMap((entry) => {
      const reference = document.createElement("template")
      reference.innerHTML = entry.html
      const actual = studyTypedAnswer(entry.expected, entry.provided, entry.combining)
      const expectedParts = parts(reference.content.firstElementChild)
      const rendered = document.createElement("template")
      rendered.innerHTML = actual.outerHTML
      const actualParts = parts(rendered.content.firstElementChild)
      return JSON.stringify(expectedParts) === JSON.stringify(actualParts) ? [] : [{ ...entry, actual: actual.outerHTML }]
    })
  }, corpus.cases)
  const safety = await page.evaluate(() => {
    const value = "猫".repeat(10000)
    const equal = studyTypedAnswer(value, value)
    const different = studyTypedAnswer(value, "犬".repeat(10000))
    return {
      long_equal: equal.querySelector(".typeGood")?.textContent === value,
      long_mismatch_preserved: different.dataset.highlightLimited === "true" && different.textContent === "犬".repeat(10000) + "↓" + value,
      long_mismatch_not_miscolored: !different.querySelector(".typeGood, .typeBad, .typeMissed"),
    }
  })
  const escape = value => value.replace(/[&"<>]/g, character => ({ "&": "&amp;", '"': "&quot;", "<": "&lt;", ">": "&gt;" })[character])
  const runtimeResults = corpus.cases.map(entry => ({ ...entry, result: runtime.render(
    `<code data-typed-expected="${escape(entry.expected)}" data-typed-combining="${entry.combining}"></code>`, {}, entry.provided) }))
  const runtimeFailures = await page.evaluate(cases => {
    const parts = html => {
      const fragment = document.createElement("template")
      fragment.innerHTML = html
      return [...fragment.content.querySelector("code").childNodes].map(child =>
        child.nodeType === Node.TEXT_NODE ? child.textContent : [child.tagName, child.className, child.id, child.textContent])
    }
    return cases.filter(entry => entry.result.error || JSON.stringify(parts(entry.result.html)) !== JSON.stringify(parts(entry.html)))
  }, runtimeResults)
  const report = { anki_version: corpus.anki_version, cases: corpus.cases.length, failures, runtime_failures: runtimeFailures, safety }
  await writeFile(resolve(root, "comparison.json"), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ ...report, failures: failures.slice(0, 8), runtime_failures: runtimeFailures.slice(0, 8), failure_count: failures.length, runtime_failure_count: runtimeFailures.length }, null, 2))
  process.exitCode = failures.length || runtimeFailures.length || Object.values(safety).some((passed) => !passed) ? 1 : 0
} finally {
  runtime.dispose()
  await browser.close()
}
