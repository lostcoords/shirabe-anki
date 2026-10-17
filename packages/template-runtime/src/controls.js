import { Event, HTMLInputElement, HTMLOptionElement, HTMLSelectElement } from "linkedom"

const checked = new WeakMap()
const indeterminate = new WeakMap()
const selected = new WeakMap()
const emptySelection = new WeakSet()
const checkable = (input) =>
  input.localName === "input" && ["checkbox", "radio"].includes(input.type)
const rawChecked = (input) => checked.get(input) ?? input.hasAttribute("checked")
const rawSelected = (option) => selected.get(option) ?? option.hasAttribute("selected")
const owner = (input) =>
  input.hasAttribute("form")
    ? input.ownerDocument.getElementById(input.getAttribute("form"))
    : input.closest("form")
const group = (input) =>
  input.type === "radio" && input.name
    ? [...input.ownerDocument.querySelectorAll('input[type="radio"]')].filter(
        (other) => other.name === input.name && owner(other) === owner(input),
      )
    : [input]
const options = (select) =>
  select.querySelectorAll("option").filter((option) => option.closest("select") === select)

function selection(select) {
  const all = options(select)
  const active = all.filter(rawSelected)
  if (select.multiple) return active
  if (active.length) return active.slice(-1)
  if (emptySelection.has(select) || select.size > 1) return []
  const first = all.find(
    (option) => !option.hasAttribute("disabled") && !option.closest("optgroup[disabled]"),
  )
  return first ? [first] : []
}

function choose(select, chosen) {
  for (const option of options(select)) selected.set(option, chosen.includes(option))
  if (chosen.length) emptySelection.delete(select)
  else emptySelection.add(select)
}

const inputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")
Object.defineProperties(HTMLInputElement.prototype, {
  value: {
    get() {
      return checkable(this) && !this.hasAttribute("value") ? "on" : inputValue.get.call(this)
    },
    set(value) {
      inputValue.set.call(this, value)
    },
    configurable: true,
  },
  checked: {
    get() {
      return this.type === "radio"
        ? group(this).filter(rawChecked).at(-1) === this
        : rawChecked(this)
    },
    set(value) {
      checked.set(this, Boolean(value))
      if (value && this.type === "radio")
        for (const other of group(this)) if (other !== this) checked.set(other, false)
    },
    configurable: true,
  },
  defaultChecked: {
    get() {
      return this.hasAttribute("checked")
    },
    set(value) {
      this.toggleAttribute("checked", Boolean(value))
    },
    configurable: true,
  },
  indeterminate: {
    get() {
      return indeterminate.get(this) || false
    },
    set(value) {
      indeterminate.set(this, Boolean(value))
    },
    configurable: true,
  },
})

Object.defineProperties(HTMLSelectElement.prototype, {
  options: {
    get() {
      return options(this)
    },
    configurable: true,
  },
  selectedOptions: {
    get() {
      return selection(this)
    },
    configurable: true,
  },
  multiple: {
    get() {
      return this.hasAttribute("multiple")
    },
    set(value) {
      this.toggleAttribute("multiple", Boolean(value))
    },
    configurable: true,
  },
  size: {
    get() {
      return Math.max(0, Number.parseInt(this.getAttribute("size"), 10) || 0)
    },
    set(value) {
      this.setAttribute("size", String(value))
    },
    configurable: true,
  },
  selectedIndex: {
    get() {
      return options(this).indexOf(selection(this)[0])
    },
    set(value) {
      const index = Math.trunc(Number(value)) || 0
      const option = options(this)[index]
      choose(this, option ? [option] : [])
    },
    configurable: true,
  },
  value: {
    get() {
      return selection(this)[0]?.value ?? ""
    },
    set(value) {
      const option = options(this).find((option) => option.value === String(value))
      choose(this, option ? [option] : [])
    },
    configurable: true,
  },
})

Object.defineProperties(HTMLOptionElement.prototype, {
  value: {
    get() {
      return this.hasAttribute("value")
        ? this.getAttribute("value")
        : this.textContent.replace(/[\t\n\r\f ]+/g, " ").trim()
    },
    set(value) {
      this.setAttribute("value", String(value))
    },
    configurable: true,
  },
  selected: {
    get() {
      const select = this.closest("select")
      return select ? selection(select).includes(this) : rawSelected(this)
    },
    set(value) {
      selected.set(this, Boolean(value))
      const select = this.closest("select")
      if (select && value) {
        emptySelection.delete(select)
        if (!select.multiple) choose(select, [this])
      }
    },
    configurable: true,
  },
  defaultSelected: {
    get() {
      return this.hasAttribute("selected")
    },
    set(value) {
      this.toggleAttribute("selected", Boolean(value))
    },
    configurable: true,
  },
})

export function snapshotControl(element) {
  if (checkable(element)) {
    element.setAttribute("data-study-checked", String(element.checked))
    element.setAttribute("data-study-indeterminate", String(element.indeterminate))
  } else if (element.localName === "option") {
    element.setAttribute("data-study-selected", String(element.selected))
  }
}

export function selectOptions(element, ids) {
  if (element.localName !== "select") throw new Error("script_event_invalid")
  const all = options(element)
  const chosen = ids.map((id) =>
    all.find((option) => option.getAttribute("data-study-node") === id),
  )
  if (
    chosen.some((option) => !option) ||
    new Set(chosen).size !== chosen.length ||
    (!element.multiple && chosen.length > 1)
  )
    throw new Error("script_event_invalid")
  choose(element, chosen)
}

export function activateCheckbox(element, nextChecked = !element.checked) {
  if (!checkable(element)) return false
  if (element.disabled) return true
  const before = element.checked
  const mixed = element.indeterminate
  const priorRadio = group(element).find((other) => other.checked)
  element.checked = element.type === "radio" ? true : nextChecked
  element.indeterminate = false
  const event = new Event("click", { bubbles: true, cancelable: true })
  element.dispatchEvent(event)
  if (event.defaultPrevented) {
    if (element.type === "radio") {
      if (priorRadio && group(element).includes(priorRadio)) priorRadio.checked = true
      else element.checked = false
    } else {
      element.checked = before
      element.indeterminate = mixed
    }
  } else if (element.isConnected && (element.type !== "radio" || !before)) {
    element.dispatchEvent(new Event("input", { bubbles: true }))
    element.dispatchEvent(new Event("change", { bubbles: true }))
  }
  return true
}

const click = HTMLInputElement.prototype.click
HTMLInputElement.prototype.click = function () {
  if (!activateCheckbox(this)) click.call(this)
}
