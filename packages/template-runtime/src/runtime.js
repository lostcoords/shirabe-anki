import { newQuickJSWASMModuleFromVariant } from "quickjs-emscripten-core"
import domSource from "study-runtime-dom"

const engine = newQuickJSWASMModuleFromVariant({
  type: "sync",
  importFFI: () =>
    import("@jitl/quickjs-singlefile-browser-release-sync/ffi").then((module) => module.QuickJSFFI),
  importModuleLoader: () =>
    import("../../../vendor/study-runtime/quickjs-engine.mjs").then((module) => module.default),
})
const maxInput = 2 * 1024 * 1024
const errors = new Set([
  "script_state_limit",
  "script_timer_limit",
  "script_timer_callback",
  "script_dom_limit",
  "script_output_limit",
  "script_event_target",
  "script_event_invalid",
  "script_clock_order",
  "script_rejection_limit",
])

export class CardRuntime {
  static async create(options = {}) {
    return new CardRuntime(await engine, options)
  }

  constructor(quickJS, { timeLimitMs = 1000 } = {}) {
    this.timeLimitMs = Number.isFinite(timeLimitMs)
      ? Math.min(1000, Math.max(1, timeLimitMs))
      : 1000
    this.runtime = quickJS.newRuntime()
    this.runtime.setMemoryLimit(64 * 1024 * 1024)
    this.runtime.setMaxStackSize(512 * 1024)
    this.deadline = performance.now() + 2000
    this.runtime.setInterruptHandler(() => performance.now() > this.deadline)
    this.context = this.runtime.newContext()
    this.functions = new Map()
    try {
      this.rejectionCheck = this.context.getProp(this.context.global, "__studyCheckRejections")
      if (this.context.typeof(this.rejectionCheck) !== "function")
        throw new Error("script_engine_missing_rejection_tracker")
      this.evaluate("delete globalThis.__studyCheckRejections", "study-rejections.js")
      const atob = this.context.newFunction("atob", (value) => {
        const text = this.context.getString(value)
        if (text.length > 65536) throw new Error("script_input_limit")
        return this.string(globalThis.atob(text))
      })
      this.context.setProp(this.context.global, "atob", atob)
      atob.dispose()
      this.evaluate(domSource, "study-dom.js")
      this.api = this.context.getProp(this.context.global, "__studyRuntime")
      for (const name of ["load", "ready", "dispatch", "advance", "snapshot"])
        this.functions.set(name, this.context.getProp(this.api, name))
      this.evaluate("delete globalThis.__studyRuntime", "study-initialize.js")
      this.evaluator = this.context.unwrapResult(
        this.context.evalCode(
          "(() => { const evaluate = eval; return source => evaluate(source); })()",
        ),
      )
    } catch (error) {
      this.dispose()
      throw error
    }
  }

  render(html, resources = {}, typedAnswer = null) {
    return this.perform(() => {
      if (typeof html !== "string" || html.length > maxInput) throw { code: "script_input_limit" }
      if (typedAnswer !== null && (typeof typedAnswer !== "string" || typedAnswer.length > 8192))
        throw { code: "script_input_limit" }
      const scripts = this.call("load", html, typedAnswer)
      let inputSize = html.length
      scripts.forEach((script) => {
        if (script.type && !["text/javascript", "application/javascript"].includes(script.type))
          throw { code: "script_type_unsupported", message: script.type }
        const code = script.source ? resources[script.source] : script.code
        if (typeof code !== "string")
          throw { code: "script_resource_missing", message: script.source }
        inputSize += script.source ? code.length : 0
        if (inputSize > maxInput) throw { code: "script_input_limit" }
        const source = this.string(code)
        try {
          const result = this.context.callFunction(this.evaluator, this.context.undefined, source)
          if (result.error) this.read(result)
          try {
            this.jobs()
            const state = this.context.getPromiseState(result.value)
            if (state.type === "rejected") {
              try {
                throw this.context.dump(state.error)
              } finally {
                state.error.dispose()
              }
            }
            if (state.type === "pending") throw { code: "script_async_pending" }
            if (!state.notAPromise) state.value.dispose()
          } finally {
            result.value.dispose()
          }
        } finally {
          source.dispose()
        }
        this.jobs()
      })
      this.call("ready")
      this.call("advance", 0)
    })
  }

  dispatch(id, type, value = null, checked = null, selected = null) {
    return this.perform(() => {
      if (
        typeof id !== "string" ||
        id.length > 256 ||
        !["click", "input", "change"].includes(type) ||
        (value !== null && (typeof value !== "string" || value.length > 8192)) ||
        (checked !== null && typeof checked !== "boolean") ||
        (selected !== null &&
          (!Array.isArray(selected) ||
            selected.length > 20000 ||
            selected.some(
              (id) => typeof id !== "string" || id.length > 256 || !/^study-node-\d+-\d+$/.test(id),
            )))
      )
        throw { code: "script_event_invalid" }
      this.call(
        "dispatch",
        id,
        type,
        value,
        checked === null ? null : Number(checked),
        selected === null ? null : JSON.stringify(selected),
      )
    })
  }

  advance(milliseconds) {
    return this.perform(() => {
      if (!Number.isFinite(milliseconds) || milliseconds < 0 || milliseconds > 86400000)
        throw { code: "script_clock_invalid" }
      this.call("advance", milliseconds)
    })
  }

  perform(operation) {
    if (!this.context) return { error: { code: "runtime_closed" }, html: null, diagnostics: [] }
    this.deadline = performance.now() + this.timeLimitMs
    try {
      operation()
      this.jobs()
      this.read(this.context.callFunction(this.rejectionCheck, this.context.undefined))
      const snapshot = this.call("snapshot")
      this.jobs()
      this.read(this.context.callFunction(this.rejectionCheck, this.context.undefined))
      return { ...snapshot, error: null }
    } catch (error) {
      const message = String(error?.message || error?.code || error || "Script failed").slice(
        0,
        2000,
      )
      const code =
        error?.code ||
        (message.includes("interrupted")
          ? "script_timeout"
          : message.includes("out of memory")
            ? "script_memory_limit"
            : errors.has(message)
              ? message
              : "script_error")
      this.dispose()
      return { error: { code, message }, html: null, diagnostics: [] }
    }
  }

  evaluate(source, filename) {
    return this.read(this.context.evalCode(source, filename))
  }

  string(value) {
    return value.includes("\0")
      ? this.context.unwrapResult(this.context.evalCode(JSON.stringify(value)))
      : this.context.newString(value)
  }

  call(name, ...values) {
    const handles = values.map((value) =>
      typeof value === "string"
        ? this.string(value)
        : value === null
          ? this.context.null
          : this.context.newNumber(value),
    )
    try {
      return this.read(this.context.callFunction(this.functions.get(name), this.api, ...handles))
    } finally {
      for (const handle of handles) handle.dispose()
    }
  }

  read(result) {
    if (result.error) {
      const error = this.context.dump(result.error)
      result.error.dispose()
      throw error
    }
    try {
      return this.context.dump(result.value)
    } finally {
      result.value.dispose()
    }
  }

  jobs() {
    const result = this.runtime.executePendingJobs(128)
    if (result.error) {
      const error = this.context.dump(result.error)
      result.error.dispose()
      throw error
    }
    if (this.runtime.hasPendingJob()) throw { code: "script_job_limit" }
  }

  dispose() {
    if (!this.context) return
    for (const handle of this.functions.values()) handle.dispose()
    this.functions.clear()
    this.api?.dispose()
    this.evaluator?.dispose()
    this.rejectionCheck?.dispose()
    this.context.dispose()
    this.runtime.dispose()
    this.context = null
  }
}
