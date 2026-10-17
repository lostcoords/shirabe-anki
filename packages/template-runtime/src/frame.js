;(() => {
  const config = JSON.parse(document.documentElement.dataset.studyFrame)
  const origin = new URL(document.baseURI).origin
  const bridge = document.currentScript
  let operation = config.operation || 0
  const send = (kind, detail = {}) =>
    parent.postMessage({ channel: config.channel, kind, ...detail }, origin)
  const reportedResources = new Set()
  const resourceWarning = (message) => {
    const text = message.slice(0, 2000)
    if (reportedResources.size >= 40 || reportedResources.has(text)) return
    reportedResources.add(text)
    send("resource-warning", { operation, message: text })
  }
  document.addEventListener("securitypolicyviolation", (event) => {
    resourceWarning(`blocked_resource: ${event.effectiveDirective} ${event.blockedURI}`)
  })
  document.addEventListener(
    "error",
    (event) => {
      const element = event.target
      if (["IMG", "AUDIO", "VIDEO", "SOURCE", "LINK"].includes(element?.tagName))
        resourceWarning(
          `resource_load_failed: ${element.tagName} ${element.getAttribute("src") || element.getAttribute("href") || ""}`,
        )
    },
    true,
  )
  function start() {
    const nodePattern = /^study-node-\d+-\d+$/
    const fixed = new Set([bridge, ...document.head.querySelectorAll("meta, base")])
    let composing = false
    let compositionInput = null
    let active = true
    let sequence = 0
    let capturePending = false
    const audio = config.playback
      ? window.studyCardAudio({
          ...config.playback,
          send,
          warn: resourceWarning,
          preserve: (media) => fixed.add(media),
        })
      : null
    const typing = () => document.querySelector("input#typeans")
    const ready = () => {
      send("ready", { operation, typing: Boolean(typing()) })
      for (const message of reportedResources) send("resource-warning", { operation, message })
    }
    const capture = () => {
      capturePending = composing
      if (!composing) send("typed", { value: (typing()?.value || "").slice(0, 8192) })
    }
    const focusState = () => {
      const element = document.activeElement
      const node = element?.getAttribute("data-study-node")
      return nodePattern.test(node)
        ? {
            start: typeof element.selectionStart === "number" ? element.selectionStart : null,
            end: typeof element.selectionEnd === "number" ? element.selectionEnd : null,
          }
        : null
    }
    const forward = (event) => {
      if (!active || !event.isTrusted) return
      if (composing || event.isComposing) {
        if (event.type === "input") compositionInput = event
        return
      }
      publish(event)
    }
    const publish = (event) => {
      const element = event.target.closest?.("[data-study-node]")
      const target = element?.getAttribute("data-study-node")
      if (!nodePattern.test(target)) return
      if (element.tagName === "SELECT" && event.type === "change") return
      if (event.type === "click" && element.closest("a")) event.preventDefault()
      if (
        event.type === "click" &&
        element.tagName === "INPUT" &&
        ["checkbox", "radio"].includes(element.type)
      )
        event.preventDefault()
      send("event", {
        target,
        type: event.type === "compositionend" ? "input" : event.type,
        value: typeof element.value === "string" ? element.value.slice(0, 8192) : null,
        checked: typeof element.checked === "boolean" ? element.checked : null,
        selected:
          element.tagName === "SELECT"
            ? [...element.selectedOptions].map((option) => option.getAttribute("data-study-node"))
            : null,
        sequence: ++sequence,
      })
    }
    for (const type of ["click", "input", "change"]) document.addEventListener(type, forward, true)
    document.addEventListener("keydown", (event) => {
      if (
        !config.review ||
        !active ||
        !event.isTrusted ||
        composing ||
        event.isComposing ||
        event.repeat ||
        event.altKey ||
        event.metaKey
      )
        return
      if (event.target === typing() && event.key === "Enter" && !event.ctrlKey) {
        event.preventDefault()
        send("key", { key: "Enter" })
        return
      }
      if (
        event.target.closest(
          "input, textarea, select, button, a, summary, audio, video, [contenteditable]",
        )
      )
        return
      if (event.ctrlKey && event.key !== "z") return
      if (![" ", "Enter", "1", "2", "3", "4", "z", "e", "s", "t", "r"].includes(event.key)) return
      event.preventDefault()
      send("key", { key: event.key, ctrlKey: event.ctrlKey })
    })
    document.addEventListener("compositionstart", (event) => {
      if (!event.isTrusted) return
      composing = true
      compositionInput = null
    })
    document.addEventListener("compositionend", (event) => {
      if (!active || (!event.isTrusted && event.target !== compositionInput?.target)) return
      composing = false
      const committed = compositionInput
      compositionInput = null
      if (committed) publish(committed)
      else forward(event)
      if (capturePending) capture()
    })
    function attributes(target, source) {
      for (const attribute of [...target.attributes]) {
        if (attribute.name === "data-study-frame") continue
        if (!source.hasAttributeNS(attribute.namespaceURI, attribute.localName))
          target.removeAttributeNS(attribute.namespaceURI, attribute.localName)
      }
      for (const attribute of source.attributes) {
        if (attribute.name === "data-study-frame") continue
        if (target.getAttributeNS(attribute.namespaceURI, attribute.localName) !== attribute.value)
          target.setAttributeNS(attribute.namespaceURI, attribute.name, attribute.value)
      }
    }

    function children(target, source) {
      const previous = [...target.childNodes].filter((node) => !fixed.has(node))
      const keyed = new Map(previous.map((node) => [node.getAttribute?.("data-study-node"), node]))
      let cursor = previous[0] || null
      const kept = new Set()
      for (const incoming of source.childNodes) {
        if (incoming.nodeType === 1 && ["SCRIPT", "META", "BASE"].includes(incoming.tagName))
          continue
        const key = incoming.getAttribute?.("data-study-node")
        let node = key ? keyed.get(key) : cursor
        if (
          !node ||
          fixed.has(node) ||
          kept.has(node) ||
          node.nodeType !== incoming.nodeType ||
          node.nodeName !== incoming.nodeName ||
          node.namespaceURI !== incoming.namespaceURI ||
          (!key && node.getAttribute?.("data-study-node"))
        )
          node = document.importNode(incoming, false)
        if (node !== cursor) {
          if (node.isConnected && target.moveBefore) target.moveBefore(node, cursor)
          else target.insertBefore(node, cursor)
        }
        kept.add(node)
        if (node.nodeType === 1) {
          attributes(node, incoming)
          children(node, incoming)
          if (["INPUT", "TEXTAREA"].includes(node.tagName) && node.value !== incoming.value)
            node.value = incoming.value
          control(node, incoming)
        } else if (node.nodeValue !== incoming.nodeValue) node.nodeValue = incoming.nodeValue
        cursor = node.nextSibling
        while (fixed.has(cursor)) cursor = cursor.nextSibling
      }
      for (const node of previous) if (!kept.has(node)) node.remove()
    }

    function control(node, incoming) {
      if (node.tagName === "INPUT" && ["checkbox", "radio"].includes(node.type)) {
        const checked = incoming.hasAttribute("data-study-checked")
          ? incoming.getAttribute("data-study-checked") === "true"
          : incoming.checked
        if (node.checked !== checked) node.checked = checked
        node.indeterminate = incoming.getAttribute("data-study-indeterminate") === "true"
      }
      if (node.tagName === "SELECT") {
        const selected = [...incoming.options].map((option) =>
          option.hasAttribute("data-study-selected")
            ? option.getAttribute("data-study-selected") === "true"
            : option.selected,
        )
        if (node.multiple)
          [...node.options].forEach((option, index) => {
            if (option.selected !== selected[index]) option.selected = selected[index]
          })
        else if (node.selectedIndex !== selected.indexOf(true))
          node.selectedIndex = selected.indexOf(true)
      }
    }

    window.addEventListener("message", (event) => {
      const message = event.data
      if (event.source !== parent || event.origin !== origin || message?.channel !== config.channel)
        return
      if (message.kind === "deactivate") {
        active = false
        audio?.stop()
        for (const media of document.querySelectorAll("audio, video")) media.pause()
        return
      }
      if (message.kind === "activate") {
        active = true
        return
      }
      if (message.kind === "replay" && active && config.review) {
        audio?.play(message.automatic === true)
        return
      }
      if (message.kind === "focus" && active && config.review) {
        const target = typing() || document.body
        if (target === document.body) target.tabIndex = -1
        target.focus({ preventScroll: true })
        return
      }
      if (message.kind === "capture" && active) {
        capture()
        return
      }
      if (
        !active ||
        composing ||
        message.kind !== "snapshot" ||
        !Number.isSafeInteger(message.operation) ||
        message.operation <= operation ||
        !Number.isSafeInteger(message.sequence) ||
        message.sequence < sequence ||
        typeof message.html !== "string" ||
        message.html.length > 3 * 1024 * 1024
      )
        return
      try {
        const source = new DOMParser().parseFromString(message.html, "text/html")
        const focused = document.activeElement
        const focus = focusState()
        attributes(document.documentElement, source.documentElement)
        attributes(document.head, source.head)
        attributes(document.body, source.body)
        children(document.head, source.head)
        children(document.body, source.body)
        if (focused.isConnected && document.activeElement !== focused) {
          focused.focus({ preventScroll: true })
          if (focused.setSelectionRange && Number.isInteger(focus?.start)) {
            try {
              focused.setSelectionRange(focus.start, focus.end)
            } catch {}
          }
        }
        operation = message.operation
        ready()
      } catch (error) {
        send("error", {
          operation: message.operation,
          message: String(error.message).slice(0, 2000),
        })
      }
    })
    for (const element of document.querySelectorAll("input, select")) control(element, element)
    ready()
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", start, { once: true })
  else start()
})()
