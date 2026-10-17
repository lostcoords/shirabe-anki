window.studyTypedAnswer = (expected, provided, combining = true) => {
  const field = document.createElement("template")
  field.innerHTML = expected
    .replace(/<br\s*\/?\s*>|<\/div>/gi, "\n")
    .replace(/\n/g, " ")
    .replace(/\r/g, "&#13;")
  for (const node of field.content.querySelectorAll("script, style")) node.remove()
  expected = [...field.content.childNodes]
    .filter((node) => node.nodeType === 1 || node.nodeType === 3)
    .map((node) => node.textContent)
    .join("")
    .replace(/\[sound:[^\]]+\]/g, "")
    .replace(/\u00a0/g, " ")
    .trim()
    .normalize("NFC")
  provided = provided.normalize("NFC")
  const code = document.createElement("code")
  code.id = "typeans"
  if (!provided) {
    code.textContent = expected
    return code
  }

  const units = (text, preserveMarks) => {
    const result = []
    let leadingMarks = ""
    for (const character of combining ? text : text.normalize("NFKD")) {
      if (!combining && /\p{M}/u.test(character)) {
        if (preserveMarks) {
          if (result.length) result[result.length - 1].text += character
          else leadingMarks += character
        }
      } else {
        result.push({ key: character, text: leadingMarks + character })
        leadingMarks = ""
      }
    }
    return result
  }
  const wanted = units(expected, true)
  const typed = units(provided, false)
  if (
    typed.length === wanted.length &&
    typed.every((item, index) => item.key === wanted[index].key)
  ) {
    const good = document.createElement("span")
    good.className = "typeGood"
    good.textContent = expected
    code.append(good)
    return code
  }
  const matches = []
  const pending = [[0, typed.length, 0, wanted.length]]
  let work = 0
  while (pending.length) {
    const [a, endA, b, endB] = pending.pop()
    work += (endA - a) * (endB - b)
    if (work > 2000000) {
      code.dataset.highlightLimited = "true"
      code.append(
        document.createTextNode(provided),
        document.createElement("br"),
        document.createTextNode("↓"),
        document.createElement("br"),
        document.createTextNode(expected),
      )
      return code
    }
    let bestA = a
    let bestB = b
    let length = 0
    let previous = new Uint32Array(endB - b + 1)
    for (let i = a; i < endA; i++) {
      const current = new Uint32Array(endB - b + 1)
      for (let j = b; j < endB; j++) {
        if (typed[i].key !== wanted[j].key) continue
        const size = previous[j - b] + 1
        current[j - b + 1] = size
        if (size > length) {
          length = size
          bestA = i - size + 1
          bestB = j - size + 1
        }
      }
      previous = current
    }
    if (length) {
      matches.push([bestA, bestB, length])
      if (a < bestA && b < bestB) pending.push([a, bestA, b, bestB])
      if (bestA + length < endA && bestB + length < endB)
        pending.push([bestA + length, endA, bestB + length, endB])
    }
  }
  matches.sort((left, right) => left[0] - right[0])
  matches.push([typed.length, wanted.length, 0])

  const top = document.createDocumentFragment()
  const bottom = document.createDocumentFragment()
  const span = (parent, className, text) => {
    if (!text) return
    const node = document.createElement("span")
    node.className = className
    node.textContent = /^\p{M}/u.test(text) ? `\u00a0${text}` : text
    parent.append(node)
  }
  const text = (items, start, end) =>
    items
      .slice(start, end)
      .map((item) => item.text)
      .join("")
  let a = 0
  let b = 0
  let different = false
  for (const [nextA, nextB, length] of matches) {
    if (a !== nextA || b !== nextB) {
      different = true
      span(
        top,
        a === nextA ? "typeMissed" : "typeBad",
        a === nextA ? "-".repeat(nextB - b) : text(typed, a, nextA),
      )
      span(bottom, "typeMissed", text(wanted, b, nextB))
    }
    const good = text(wanted, nextB, nextB + length)
    span(top, "typeGood", text(typed, nextA, nextA + length))
    span(bottom, "typeGood", good)
    a = nextA + length
    b = nextB + length
  }
  code.append(top)
  if (different) {
    const arrow = document.createElement("span")
    arrow.id = "typearrow"
    arrow.textContent = "↓"
    code.append(document.createElement("br"), arrow, document.createElement("br"), bottom)
  }
  return code
}
