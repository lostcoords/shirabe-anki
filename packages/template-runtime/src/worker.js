import { CardRuntime } from "./runtime.js"

const runtime = await CardRuntime.create()
globalThis.postMessage({ kind: "ready" })
globalThis.addEventListener("message", ({ data }) => {
  if (!Number.isSafeInteger(data?.id) || data.id < 1) return
  let result
  if (data.kind === "render") result = runtime.render(data.html, data.resources, data.typedAnswer)
  else if (data.kind === "dispatch")
    result = runtime.dispatch(data.target, data.type, data.value, data.checked, data.selected)
  else if (data.kind === "advance") result = runtime.advance(data.time)
  else result = { error: { code: "script_message_invalid" }, html: null, diagnostics: [] }
  globalThis.postMessage({ id: data.id, result })
})
