import jquery from "jquery"
import { CustomEvent, Event, EventTarget, parseHTML } from "linkedom"
import compareTypedAnswer from "study-runtime-typed-answer"
import { activateCheckbox, selectOptions, snapshotControl } from "./controls.js"

globalThis.__studyRuntime = (() => {
  let document
  let clock = 0
  let sequence = 0
  let face = 0
  let nodeSequence = 0
  let nodeIds = new WeakMap()
  let targets = new Map()
  let handlers = new WeakMap()
  const compileHandler = Function
  const listeners = new WeakMap()
  const addListener = EventTarget.prototype.addEventListener
  const removeListener = EventTarget.prototype.removeEventListener
  EventTarget.prototype.addEventListener = function (type, listener, options) {
    if (typeof listener !== "function") return addListener.call(this, type, listener, options)
    if (!listeners.has(this)) listeners.set(this, new WeakMap())
    const bound = listeners.get(this)
    if (!bound.has(listener))
      bound.set(listener, (event) => listener.call(event.currentTarget, event))
    return addListener.call(this, type, bound.get(listener), options)
  }
  EventTarget.prototype.removeEventListener = function (type, listener, options) {
    return removeListener.call(this, type, listeners.get(this)?.get(listener) || listener, options)
  }
  const timers = new Map()
  const stores = [new Map(), new Map()]
  let diagnostics = []
  const storage = (entries) => ({
    get length() {
      return entries.size
    },
    key(index) {
      return [...entries.keys()][index] ?? null
    },
    getItem(key) {
      return entries.get(String(key)) ?? null
    },
    removeItem(key) {
      entries.delete(String(key))
    },
    clear() {
      entries.clear()
    },
    setItem(key, value) {
      key = String(key)
      value = String(value)
      const count = stores.reduce((sum, store) => sum + store.size, 0)
      const previous = entries.has(key) ? key.length + entries.get(key).length : 0
      const size = stores.reduce(
        (sum, store) => sum + [...store].reduce((total, [k, v]) => total + k.length + v.length, 0),
        0,
      )
      if (
        key.length > 1024 ||
        value.length > 32768 ||
        size - previous + key.length + value.length > 32768 ||
        (!entries.has(key) && count >= 128)
      )
        throw new Error("script_state_limit")
      entries.set(key, value)
    },
  })
  Object.assign(globalThis, {
    window: globalThis,
    self: globalThis,
    location: Object.freeze({ href: "about:blank" }),
    Event,
    CustomEvent,
    localStorage: storage(stores[0]),
    sessionStorage: storage(stores[1]),
    console: Object.fromEntries(
      ["log", "info", "debug", "warn", "error"].map((level) => [
        level,
        (...args) => {
          if (diagnostics.length < 20)
            diagnostics.push({ level, message: args.map(String).join(" ").slice(0, 2000) })
        },
      ]),
    ),
    setTimeout(callback, delay = 0, ...args) {
      if (typeof callback !== "function") throw new Error("script_timer_callback")
      if (timers.size >= 128) throw new Error("script_timer_limit")
      const id = ++sequence
      timers.set(id, { callback, args, at: clock + Math.max(0, Number(delay) || 0) })
      return id
    },
    clearTimeout(id) {
      timers.delete(id)
    },
  })
  let prototype = Object.getPrototypeOf(parseHTML("<html></html>").document.createElement("span"))
  while (prototype && !Object.getOwnPropertyDescriptor(prototype, "innerText"))
    prototype = Object.getPrototypeOf(prototype)
  const descriptor = Object.getOwnPropertyDescriptor(prototype, "innerText")
  Object.defineProperty(prototype, "innerText", {
    ...descriptor,
    set(value) {
      const lines = String(value).replace(/\r\n?/g, "\n").split("\n")
      this.replaceChildren()
      lines.forEach((line, index) => {
        if (index) this.appendChild(this.ownerDocument.createElement("br"))
        this.appendChild(this.ownerDocument.createTextNode(line))
      })
    },
  })

  const identifyNodes = () => {
    const elements = [...document.querySelectorAll("*")]
    if (elements.length > 20000) throw new Error("script_dom_limit")
    targets = new Map()
    for (const element of elements) {
      if (!nodeIds.has(element)) nodeIds.set(element, `study-node-${face}-${++nodeSequence}`)
      const id = nodeIds.get(element)
      targets.set(id, element)
      element.setAttribute("data-study-node", id)
      snapshotControl(element)
      if (!handlers.has(element)) handlers.set(element, new Map())
      const bindings = handlers.get(element)
      for (const type of ["click", "input", "change"]) {
        const name = `on${type}`
        const source = element.getAttribute(name)
        const previous = bindings.get(name)
        if (previous?.source === source) continue
        if (source === null) {
          if (element[name] === previous?.callback) element[name] = null
          bindings.delete(name)
        } else if (!element[name] || element[name] === previous?.callback) {
          const handler = compileHandler("event", source)
          const callback = function (event) {
            if (handler.call(this, event) === false) event.preventDefault()
          }
          element[name] = callback
          bindings.set(name, { source, callback })
        }
      }
    }
  }

  return {
    load(html, typedAnswer) {
      timers.clear()
      clock = 0
      diagnostics = []
      face++
      nodeIds = new WeakMap()
      handlers = new WeakMap()
      const complete = /^\s*(?:<!doctype[^>]*>\s*)?<html[\s>]/i.test(html)
      const parsed = parseHTML(
        complete ? html : `<!doctype html><html><head></head><body>${html}</body></html>`,
      ).document
      if (document) {
        document.replaceChild(
          document.importNode(parsed.documentElement, true),
          document.documentElement,
        )
      } else {
        document = parsed
        globalThis.document = document
        document.readyState = "loading"
        document.implementation = {
          createHTMLDocument(title = "") {
            const created = parseHTML(
              "<!doctype html><html><head><title></title></head><body></body></html>",
            ).document
            created.querySelector("title").textContent = title
            return created
          },
        }
        globalThis.addEventListener = document.defaultView.addEventListener.bind(
          document.defaultView,
        )
        globalThis.removeEventListener = document.defaultView.removeEventListener.bind(
          document.defaultView,
        )
        jquery(globalThis)
      }
      const active = [...document.querySelectorAll("script")].filter(
        (element) =>
          !["application/json", "application/ld+json"].includes(
            element.getAttribute("type")?.trim().toLowerCase(),
          ),
      )
      const scripts = active.map((element) => ({
        source: element.getAttribute("src"),
        code: element.textContent,
        type: element.getAttribute("type"),
      }))
      for (const element of active) element.remove()
      if (typedAnswer !== null) {
        for (const element of document.querySelectorAll("code[data-typed-expected]")) {
          element.replaceWith(
            compareTypedAnswer(
              element.getAttribute("data-typed-expected"),
              typedAnswer,
              element.getAttribute("data-typed-combining") !== "false",
            ),
          )
        }
      }
      identifyNodes()
      return scripts
    },
    ready() {
      document.readyState = "complete"
      document.dispatchEvent(new Event("DOMContentLoaded"))
    },
    dispatch(id, type, value, checked, selected) {
      const element = id.startsWith("study-node-") ? targets.get(id) : document.getElementById(id)
      if (!element) throw new Error("script_event_target")
      if (selected !== null) selectOptions(element, JSON.parse(selected))
      else if (value !== null) element.value = value
      if (
        type === "click" &&
        activateCheckbox(element, checked === null ? !element.checked : Boolean(checked))
      )
        return
      if (checked !== null) element.checked = Boolean(checked)
      element.dispatchEvent(new Event(type, { bubbles: true, cancelable: type === "click" }))
      if (type === "input" && element.localName === "select")
        element.dispatchEvent(new Event("change", { bubbles: true }))
    },
    advance(now) {
      if (now < clock) throw new Error("script_clock_order")
      clock = now
      let callbacks = 0
      while (true) {
        const entry = [...timers]
          .filter(([, timer]) => timer.at <= now)
          .sort((a, b) => a[1].at - b[1].at)[0]
        if (!entry) break
        if (++callbacks > 128) throw new Error("script_timer_limit")
        const [id, timer] = entry
        timers.delete(id)
        timer.callback(...timer.args)
      }
    },
    snapshot() {
      identifyNodes()
      const html = document.toString()
      if (html.length > 2 * 1024 * 1024) throw new Error("script_output_limit")
      return {
        html,
        diagnostics,
        nextTimer: timers.size ? Math.min(...[...timers.values()].map((timer) => timer.at)) : null,
      }
    },
  }
})()
