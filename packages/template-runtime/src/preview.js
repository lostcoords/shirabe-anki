import { ScriptCard } from "./client.js"
import { snapshotDocument } from "./snapshot.js"

const root = document.querySelector("#study-script-preview")
if (root) {
  const reviewing = root.dataset.review === "true"
  const embedded = root.dataset.embedded === "true"
  const origin = new URL(root.dataset.mediaBase).origin
  const status = root.querySelector("[data-status]")
  const loadingText = status.textContent
  const warnings = root.querySelector("[data-warnings]")
  const reportedWarnings = new Set()
  const buttons = [...root.querySelectorAll("button[data-side]")]
  const frames = [...root.querySelectorAll("iframe[data-side]")]
  const card = new ScriptCard(root.dataset.workerUrl)
  const controller = new AbortController()
  let input, current, channel, timer, readinessTimer
  let epoch = 0
  let request = 0
  let clockStart = 0
  let nextTimer = null
  let diagnosticCount = 0
  let eventSequence = 0
  let frameLoaded = false
  let closed = false
  let paused = false
  let pausedAt = 0
  let hiddenTime = 0
  let side = "question"
  let typing = false
  let capturePending = false
  let previewAnswer = ""

  const notify = (kind, detail = {}) => {
    if ((reviewing || embedded) && parent !== window)
      parent.postMessage(
        { channel: reviewing ? "shirabe-study" : "shirabe-import-preview", kind, side, ...detail },
        origin,
      )
  }

  const deactivate = () => {
    if (channel) current?.contentWindow.postMessage({ channel, kind: "deactivate" }, "*")
  }
  const close = () => {
    deactivate()
    closed = true
    clearTimeout(timer)
    clearTimeout(readinessTimer)
    controller.abort()
    card.dispose()
  }
  window.addEventListener("pagehide", close, { once: true })
  const now = () =>
    Math.min(
      86400000,
      (reviewing ? 0 : 1000) + Math.max(0, Math.floor(performance.now() - clockStart - hiddenTime)),
    )
  const pause = () => {
    if (paused || closed) return
    paused = true
    pausedAt = performance.now()
    clearTimeout(timer)
    deactivate()
  }
  const resume = () => {
    if (!paused || closed || document.hidden) return
    hiddenTime += performance.now() - pausedAt
    paused = false
    current?.contentWindow.postMessage({ channel, kind: "activate" }, "*")
    schedule()
  }
  document.addEventListener("visibilitychange", () => (document.hidden ? pause() : resume()))
  window.addEventListener("message", (event) => {
    const message = event.data
    if (
      (!reviewing && !embedded) ||
      closed ||
      event.source !== parent ||
      event.origin !== origin ||
      message?.channel !== (reviewing ? "shirabe-study" : "shirabe-import-preview")
    )
      return
    if (!reviewing) {
      if (message.kind === "side" && ["question", "answer"].includes(message.side))
        selectPreviewFace(message.side)
      return
    }
    if (message.kind === "side" && ["question", "answer"].includes(message.side))
      selectFace(message.side, message.value)
    if (message.kind === "capture" && frameLoaded && side === "question") {
      capturePending = true
      current.contentWindow.postMessage({ channel, kind: "capture" }, "*")
    }
    if (message.kind === "pause") pause()
    if (message.kind === "replay" && frameLoaded) {
      resume()
      if (!paused)
        current.contentWindow.postMessage(
          { channel, kind: "replay", automatic: message.automatic === true },
          "*",
        )
    }
    if (message.kind === "focus") {
      resume()
      current?.contentWindow.postMessage({ channel, kind: "focus" }, "*")
    }
    if (message.kind === "ping" && frameLoaded) notify("ready", { documentId: channel, typing })
  })

  function warn(message) {
    const text = String(message).slice(0, 2000)
    if (reportedWarnings.size >= 40 || reportedWarnings.has(text)) return
    reportedWarnings.add(text)
    const item = document.createElement("li")
    item.textContent = text
    warnings.append(item)
    status.textContent = root.dataset.warningsText
  }

  function fail(message) {
    warn(message)
    status.textContent = root.dataset.warningsText
    root.dataset.ready = "true"
    root.dataset.settledOperation = String(request)
    for (const button of buttons) button.disabled = true
    notify("unavailable", { error: "script_runtime_failed" })
    close()
  }

  function display(result, generation, operation, sequence = eventSequence) {
    if (closed || generation !== epoch || operation !== request) return
    for (const diagnostic of result.diagnostics.slice(diagnosticCount, 20))
      warn(`${current.title}: ${String(diagnostic.level)}: ${String(diagnostic.message)}`)
    diagnosticCount = result.diagnostics.length
    if (result.error) {
      fail(`${current.title}: ${String(result.error.code)}: ${String(result.error.message || "")}`)
      return
    }
    try {
      nextTimer = result.nextTimer
      root.dataset.ready = "false"
      const html = snapshotDocument(
        result.html,
        root.dataset.mediaBase,
        { channel, operation, review: reviewing, playback: input.playback[side] },
        (message) => warn(`${current.title}: ${message}`),
      )
      if (frameLoaded) {
        current.contentWindow.postMessage(
          { channel, kind: "snapshot", html, operation, sequence },
          "*",
        )
      } else {
        current.srcdoc = html
        clearTimeout(readinessTimer)
        readinessTimer = setTimeout(() => fail("script_frame_timeout"), 5000)
      }
    } catch (error) {
      fail(error.message)
    }
  }

  function schedule() {
    clearTimeout(timer)
    if (nextTimer === null || closed || paused || document.hidden) return
    const generation = epoch
    timer = setTimeout(
      async () => {
        const operation = ++request
        root.dataset.ready = "false"
        display(await card.advance(now()), generation, operation)
      },
      Math.max(100, nextTimer - now()),
    )
  }

  async function selectFace(selected, provided = "") {
    if (!input || closed) return
    if (typeof provided !== "string" || provided.length > 8192) return
    deactivate()
    side = selected
    paused = document.hidden
    hiddenTime = 0
    nextTimer = null
    const generation = ++epoch
    const operation = ++request
    clearTimeout(timer)
    clearTimeout(readinessTimer)
    channel = crypto.randomUUID()
    eventSequence = 0
    frameLoaded = false
    typing = false
    capturePending = false
    diagnosticCount = 0
    current =
      reviewing || embedded ? frames[0] : frames.find((frame) => frame.dataset.side === side)
    current.dataset.side = side
    for (const frame of frames) frame.hidden = frame !== current
    for (const button of buttons)
      button.setAttribute("aria-pressed", String(button.dataset.side === side))
    root.dataset.ready = "false"
    status.textContent = loadingText
    let result = await card.render(
      input.faces[side],
      input.resources,
      side === "answer" ? provided : null,
    )
    if (!result.error && !reviewing) result = await card.advance(1000)
    clockStart = performance.now()
    if (paused) pausedAt = clockStart
    display(result, generation, operation)
  }

  window.addEventListener("message", async (event) => {
    const message = event.data
    if (
      closed ||
      event.source !== current?.contentWindow ||
      event.origin !== "null" ||
      !channel ||
      message?.channel !== channel
    )
      return
    if (message.kind === "resource-warning" && message.operation === request) {
      warn(`${current.title}: ${String(message.message)}`)
      return
    }
    if (message.kind === "ready") {
      if (message.operation !== request) return
      frameLoaded = true
      typing = message.typing === true
      clearTimeout(readinessTimer)
      root.dataset.ready = "true"
      root.dataset.settledOperation = String(message.operation)
      notify("ready", { documentId: channel, typing })
      status.textContent = warnings.childElementCount
        ? root.dataset.warningsText
        : root.dataset.completeText
      for (const button of buttons) button.disabled = false
      schedule()
      return
    }
    if (
      message.kind === "typed" &&
      capturePending &&
      side === "question" &&
      typeof message.value === "string" &&
      message.value.length <= 8192
    ) {
      capturePending = false
      if (reviewing) notify("typed", { value: message.value })
      else {
        previewAnswer = message.value
        selectFace("answer", previewAnswer)
      }
      return
    }
    if (reviewing && message.kind === "key") {
      notify("key", { key: message.key, ctrlKey: message.ctrlKey === true })
      return
    }
    if (reviewing && message.kind === "audio-blocked") {
      notify("audio-blocked")
      return
    }
    if (message.kind === "error" && message.operation === request) {
      fail(`script_frame_update_failed: ${String(message.message).slice(0, 2000)}`)
      return
    }
    if (
      message.kind !== "event" ||
      !["click", "input", "change"].includes(message.type) ||
      !/^study-node-\d+-\d+$/.test(message.target) ||
      !Number.isSafeInteger(message.sequence) ||
      message.sequence <= eventSequence
    )
      return
    clearTimeout(timer)
    eventSequence = message.sequence
    const generation = epoch
    const operation = ++request
    root.dataset.ready = "false"
    const result = await card.dispatch(
      message.target,
      message.type,
      message.value,
      message.checked,
      message.selected,
    )
    display(result, generation, operation, message.sequence)
  })

  for (const button of buttons)
    button.addEventListener("click", () => selectPreviewFace(button.dataset.side))

  function selectPreviewFace(selected) {
    if (selected === side) return
    if (selected === "answer" && side === "question" && frameLoaded) {
      capturePending = true
      current.contentWindow.postMessage({ channel, kind: "capture" }, "*")
    } else selectFace(selected, previewAnswer)
  }

  async function preview() {
    try {
      const supplied = root.querySelector("template[data-input]")
      if (supplied) {
        input = JSON.parse(supplied.content.textContent)
      } else {
        const response = await fetch(root.dataset.inputUrl, {
          cache: "no-store",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        })
        input = await response.json()
        if (!response.ok) throw new Error([input.error, input.detail].filter(Boolean).join(": "))
      }
      for (const filter of input.unsupported_filters)
        warn(root.dataset.filterWarningText.replace("{filter}", () => filter))
      const initialSide = embedded ? root.dataset.initialSide : "question"
      if (initialSide === "answer") {
        const question = await card.render(input.faces.question, input.resources)
        if (question.error)
          throw new Error(`${question.error.code}: ${question.error.message || ""}`)
        const advanced = await card.advance(1000)
        for (const diagnostic of advanced.diagnostics || [])
          warn(`${frames[0].title}: ${String(diagnostic.level)}: ${String(diagnostic.message)}`)
        if (advanced.error)
          throw new Error(`${advanced.error.code}: ${advanced.error.message || ""}`)
      }
      await selectFace(initialSide)
    } catch (error) {
      if (!closed) fail(error.message)
    }
  }

  preview()
}
