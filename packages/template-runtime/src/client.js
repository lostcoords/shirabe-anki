export class ScriptCard {
  constructor(url) {
    this.worker = new Worker(url, { type: "module" })
    this.sequence = 0
    this.ready = new Promise((resolve) => {
      this.start = resolve
    })
    this.queue = this.ready
    this.timer = setTimeout(() => this.stop("script_start_timeout"), 10000)
    this.worker.addEventListener("error", () => this.stop("script_worker_failed"))
    this.worker.addEventListener("messageerror", () => this.stop("script_message_failed"))
    this.worker.addEventListener("message", ({ data }) => {
      if (data?.kind === "ready" && this.start) {
        clearTimeout(this.timer)
        this.start()
        this.start = null
      } else if (this.pending && data?.id === this.pending.id) {
        clearTimeout(this.timer)
        const result = data.result
        if (
          !result ||
          !(result.html === null || typeof result.html === "string") ||
          (result.html?.length || 0) > 2 * 1024 * 1024 ||
          !Array.isArray(result.diagnostics) ||
          (!result.error &&
            result.nextTimer !== null &&
            (!Number.isFinite(result.nextTimer) || result.nextTimer < 0))
        ) {
          this.stop("script_result_invalid")
          return
        }
        this.pending.resolve(result)
        this.pending = null
        if (result.error) this.stop(result.error.code)
      }
    })
  }

  render(html, resources = {}, typedAnswer = null) {
    return this.send({ kind: "render", html, resources, typedAnswer })
  }
  dispatch(target, type, value = null, checked = null, selected = null) {
    return this.send({ kind: "dispatch", target, type, value, checked, selected })
  }
  advance(time) {
    return this.send({ kind: "advance", time })
  }

  send(message) {
    const operation = this.queue.then(() => {
      if (this.closed) return this.failure(this.closed)
      return new Promise((resolve) => {
        const id = ++this.sequence
        this.pending = { id, resolve }
        this.timer = setTimeout(() => this.stop("script_worker_timeout"), 2000)
        try {
          this.worker.postMessage({ ...message, id })
        } catch {
          this.stop("script_message_failed")
        }
      })
    })
    this.queue = operation
    return operation
  }

  stop(code) {
    if (this.closed) return
    this.closed = code
    clearTimeout(this.timer)
    this.worker.terminate()
    this.start?.()
    this.start = null
    this.pending?.resolve(this.failure(code))
    this.pending = null
  }

  failure(code) {
    return { error: { code }, html: null, diagnostics: [] }
  }
  dispose() {
    this.stop("runtime_closed")
  }
}
