import DOMPurify from "dompurify"
import { hash as frameHash, source as frameSource } from "study-runtime-frame"

export function snapshotDocument(html, mediaBase, interaction = null, report = () => {}) {
  if (typeof html !== "string" || html.length > 2 * 1024 * 1024)
    throw new Error("script_output_limit")
  const base = new URL(mediaBase, document.baseURI)
  if (
    base.origin !== new URL(document.baseURI).origin ||
    !/^\/study\/(?:card|import)-media\/[^/]+\/$/.test(base.pathname) ||
    base.username ||
    base.password ||
    base.search ||
    base.hash
  )
    throw new Error("script_media_base_invalid")

  const root = DOMPurify.sanitize(html, {
    WHOLE_DOCUMENT: true,
    RETURN_DOM: true,
    ADD_TAGS: ["link"],
    FORBID_TAGS: [
      "script",
      "meta",
      "base",
      "iframe",
      "object",
      "embed",
      "form",
      "foreignobject",
      "animate",
      "animatetransform",
      "animatemotion",
      "set",
    ],
    FORBID_ATTR: [
      "autoplay",
      "autofocus",
      "srcset",
      "ping",
      "action",
      "formaction",
      "target",
      "download",
      "popover",
      "popovertarget",
    ],
  })
  for (const element of root.querySelectorAll("*")) {
    if (element.tagName === "LINK") {
      let url
      try {
        url = new URL(element.getAttribute("href"), base)
      } catch {
        report(`blocked_resource: LINK href ${element.getAttribute("href")}`)
        element.remove()
        continue
      }
      if (
        element.getAttribute("rel")?.trim().toLowerCase() !== "stylesheet" ||
        !element.hasAttribute("href") ||
        !url.href.startsWith(base.href)
      ) {
        report(`blocked_resource: LINK href ${element.getAttribute("href")}`)
        element.remove()
        continue
      }
      element.setAttribute("href", url.href)
      element.setAttribute("crossorigin", "anonymous")
      element.setAttribute("referrerpolicy", "no-referrer")
      continue
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase()
      if (name === "href" || name === "xlink:href") {
        if (!/^#[^\s]*$/.test(attribute.value)) element.removeAttribute(attribute.name)
      } else if (name === "src" || name === "poster" || name === "background") {
        const url = new URL(attribute.value, base)
        if (!url.href.startsWith(base.href) && !url.href.startsWith("data:")) {
          report(`blocked_resource: ${element.tagName} ${name} ${attribute.value}`)
          element.removeAttribute(attribute.name)
        }
      }
    }
    if (["IMG", "AUDIO", "VIDEO"].includes(element.tagName)) {
      element.setAttribute("crossorigin", "anonymous")
      element.setAttribute("referrerpolicy", "no-referrer")
    }
    if (["AUDIO", "VIDEO"].includes(element.tagName)) {
      element.setAttribute("controls", "")
      element.setAttribute("preload", "none")
    }
  }
  const head = root.querySelector("head")
  const policy = `default-src 'none'; script-src '${interaction ? frameHash : "none"}'; style-src 'unsafe-inline' ${base.href}; img-src data: ${base.href}; media-src data: ${base.href}; font-src data: ${base.href}; connect-src 'none'; frame-src 'none'; child-src 'none'; worker-src 'none'; object-src 'none'; base-uri ${base.href}; form-action 'none'`
  const fixed = [
    ["meta", { charset: "utf-8" }],
    ["meta", { "http-equiv": "Content-Security-Policy", content: policy }],
    ["meta", { "http-equiv": "x-dns-prefetch-control", content: "off" }],
    ["meta", { name: "referrer", content: "no-referrer" }],
    ["meta", { name: "viewport", content: "width=device-width, initial-scale=1" }],
    ["base", { href: base.href }],
  ]
  for (const [name, attributes] of fixed.reverse()) {
    const element = root.ownerDocument.createElement(name)
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value)
    head.prepend(element)
  }
  if (interaction) {
    root.setAttribute("data-study-frame", JSON.stringify(interaction))
    const bridge = root.ownerDocument.createElement("script")
    bridge.setAttribute("data-study-frame-bridge", "")
    bridge.textContent = frameSource
    head.insertBefore(bridge, head.querySelector(":not(meta):not(base)"))
  }
  return `<!doctype html>${root.outerHTML}`
}
