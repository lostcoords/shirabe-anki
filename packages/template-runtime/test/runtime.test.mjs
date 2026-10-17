import assert from "node:assert/strict"
import { join } from "node:path"
import test from "node:test"
import { pathToFileURL } from "node:url"

const moduleURL = process.env.STUDY_RUNTIME_BUILD_DIR
  ? pathToFileURL(join(process.env.STUDY_RUNTIME_BUILD_DIR, "runtime.mjs"))
  : new URL("../dist/runtime.mjs", import.meta.url)
const { CardRuntime } = await import(moduleURL)

test("binary Base64 strings retain null bytes and HTML entities decode once", async () => {
  const card = await CardRuntime.create()
  try {
    const result =
      card.render(`<p id="entity" data-value="&lt;b&gt;">&amp; &#x732b; &amp;lt;</p><pre id="result"></pre><script>
      document.getElementById('result').textContent = JSON.stringify([
        Array.from(atob('QQBCAP8='), c => c.charCodeAt(0)),
        document.getElementById('entity').getAttribute('data-value'),
        document.getElementById('entity').textContent
      ]);
    </script>`)
    assert.equal(result.error, null)
    assert.match(result.html, /\[65,0,66,0,255\]/)
    assert.match(result.html, /&lt;b&gt;/)
    assert.match(result.html, /&amp; 猫 &amp;lt;/)
  } finally {
    card.dispose()
  }
})

test("input strings crossing into the runtime retain embedded null bytes", async () => {
  const card = await CardRuntime.create()
  try {
    assert.equal(
      card.render(
        '<input id="answer" oninput="document.getElementById(\'result\').textContent = JSON.stringify(Array.from(this.value, c => c.charCodeAt(0)))"><pre id="result"></pre>',
      ).error,
      null,
    )
    const result = card.dispatch("answer", "input", "A\0B")
    assert.equal(result.error, null)
    assert.match(result.html, /\[65,0,66\]/)
  } finally {
    card.dispose()
  }
})

test("typed comparisons are bounded and available to answer scripts before execution", async () => {
  const card = await CardRuntime.create()
  try {
    const result = card.render(
      '<code data-typed-expected="&lt;b&gt;猫&lt;/b&gt;"></code><pre id="read"></pre><script>document.getElementById("read").textContent = document.querySelector(".typeMissed").textContent</script>',
      {},
      "犬",
    )
    assert.equal(result.error, null)
    assert.match(result.html, /class="typeMissed">猫/)
    assert.match(result.html, /id="read">猫/)
    assert.equal(
      card.render("<p>answer</p>", {}, "x".repeat(8193)).error.code,
      "script_input_limit",
    )
  } finally {
    card.dispose()
  }
})

test("Anki's implicit jQuery updates card DOM and handles clicks across faces", async () => {
  const card = await CardRuntime.create()
  try {
    let result = card.render(`<button id="word">猫</button><p id="result"></p><script>
      window.originalDocument = document;
      $.fn.label = function(value) { return this.text(value); };
      $(document).ready(function() {
        $('#result').label($.fn.jquery);
        $('#word').on('click', function() { $(this).label('犬'); });
      });
    </script>`)
    assert.equal(result.error, null)
    result = card.advance(1)
    assert.equal(result.error, null)
    assert.match(result.html, />3\.7\.1<\/p>/)
    assert.match(card.dispatch("word", "click").html, />犬<\/button>/)
    result = card.render(`<p id="result"></p><script>
      if (document !== originalDocument) throw new Error('document changed');
      $(function() { $('#result').label('answer'); });
    </script>`)
    assert.equal(result.error, null)
    result = card.advance(1)
    assert.equal(result.error, null)
    assert.match(result.html, />answer<\/p>/)
  } finally {
    card.dispose()
  }
})

test("jQuery ready failures cannot produce a gradeable initial snapshot", async () => {
  const card = await CardRuntime.create()
  try {
    const result = card.render(
      '<p>unfinished</p><script>$(function() { throw new Error("ready failed") })</script>',
    )
    assert.equal(result.error?.code, "script_error")
    assert.match(result.error.message, /ready failed/)
    assert.equal(result.html, null)
  } finally {
    card.dispose()
  }
})

test("jQuery cannot acquire host networking or share plugins between cards", async () => {
  const first = await CardRuntime.create()
  const second = await CardRuntime.create()
  try {
    const result = first.render(`<p id="result"></p><script>
      $.fn.privatePlugin = function() {};
      $.ajax('https://untrusted.invalid/').fail(function(_, status, error) {
        $('#result').text(status + ':' + error + ':' + $.constructor('return typeof fetch')());
      });
    </script>`)
    assert.equal(result.error, null)
    assert.match(result.html, />error:No Transport:undefined<\/p>/)
    assert.match(
      second.render(
        '<p id="result"></p><script>$("#result").text(typeof $.fn.privatePlugin)</script>',
      ).html,
      />undefined<\/p>/,
    )
  } finally {
    first.dispose()
    second.dispose()
  }
})

test("control properties preserve live state separately from default attributes", async () => {
  const card = await CardRuntime.create()
  try {
    const result =
      card.render(`<input id="check" type="checkbox" checked><select id="pick"><option>猫</option><option value="dog">犬</option></select>
      <p id="result"></p><script>
        const check = document.getElementById('check'), pick = document.getElementById('pick');
        const initial = [check.checked, check.defaultChecked, check.value, pick.value, pick.selectedIndex];
        check.checked = false; pick.value = 'dog';
        document.getElementById('result').textContent = JSON.stringify([initial, check.checked, check.hasAttribute('checked'), pick.selectedIndex, pick.selectedOptions[0].textContent]);
      </script>`)
    assert.equal(result.error, null)
    assert.match(result.html, /\[\[true,true,"on","猫",0\],false,true,1,"犬"\]/)
    assert.match(result.html, /data-study-checked="false"/)
  } finally {
    card.dispose()
  }
})

test("checkbox activation emits ordered events and cancellation restores checkedness", async () => {
  for (const cancel of [false, true]) {
    const card = await CardRuntime.create()
    try {
      assert.equal(
        card.render(`<input id="check" type="checkbox"><p id="events"></p><script>
        window.events = [];
        const check = document.getElementById('check');
        for (const type of ['click', 'input', 'change']) check.addEventListener(type, event => {
          events.push(type + ':' + check.checked);
          if (${cancel} && type === 'click') event.preventDefault();
          document.getElementById('events').textContent = events.join(',');
        });
      </script>`).error,
        null,
      )
      const result = card.dispatch("check", "click", "on", true)
      assert.equal(result.error, null)
      assert.match(
        result.html,
        cancel ? />click:true<\/p>/ : />click:true,input:true,change:true<\/p>/,
      )
      assert.ok(result.html.includes(`data-study-checked="${!cancel}"`))
    } finally {
      card.dispose()
    }
  }
})

test("select input handlers finish before change and foreign option identities are refused", async () => {
  const card = await CardRuntime.create()
  try {
    assert.equal(
      card.render(`<select id="pick"><option value="cat">猫</option><option value="dog">犬</option></select>
      <select id="other"><option>別</option></select><p id="result"></p><script>
        const pick = document.getElementById('pick');
        pick.oninput = function() { this.value = 'cat' };
        pick.onchange = function() { document.getElementById('result').textContent = this.value };
      </script>`).error,
      null,
    )
    const changed = card.dispatch("pick", "input", "dog")
    assert.equal(changed.error, null)
    assert.match(changed.html, />cat<\/p>/)
    const options = [...changed.html.matchAll(/<option\b[^>]*data-study-node="([^"]+)"/g)].map(
      (match) => match[1],
    )
    assert.equal(options.length, 3)
    assert.equal(
      card.dispatch("pick", "input", null, null, [options[2]]).error.code,
      "script_event_invalid",
    )
  } finally {
    card.dispose()
  }
})

test("anonymous elements have stable event targets and inline handlers run only in the VM", async () => {
  const card = await CardRuntime.create()
  try {
    const front = card.render("<button onclick=\"this.innerText = 'clicked'\">Tap</button>")
    const id = front.html.match(/<button[^>]*data-study-node="([^"]+)"/)[1]
    const changed = card.dispatch(id, "click")
    assert.equal(changed.error, null)
    assert.match(changed.html, />clicked<\/button>/)
    assert.ok(changed.html.includes(`data-study-node="${id}"`))
    assert.equal(card.render("<button>Another face</button>").error, null)
    assert.equal(card.dispatch(id, "click").error.code, "script_event_target")
  } finally {
    card.dispose()
  }
})

test("dynamic inline handlers and input state work without replacing explicit property handlers", async () => {
  const card = await CardRuntime.create()
  try {
    assert.equal(
      card.render(
        '<div id="holder"></div><input id="word" oninput="document.getElementById(\'holder\').innerText = this.value"><script>document.getElementById("holder").innerHTML = \'<button id="tap" onclick="this.innerText = 1">Tap</button>\'; document.getElementById("tap").onclick = function () { this.innerText = "property" }</script>',
      ).error,
      null,
    )
    assert.match(card.dispatch("tap", "click").html, />property<\/button>/)
    assert.match(card.dispatch("word", "input", "日本語").html, />日本語<\/div>/)
  } finally {
    card.dispose()
  }
})

test("bubbling handlers receive the current element as this and inline false cancels defaults", async () => {
  const card = await CardRuntime.create()
  try {
    const first = card.render(
      '<button id="button" onclick="this.setAttribute(\'data-clicked\', \'yes\'); return false"><span id="child">Tap</span></button><p id="cancelled"></p><script>document.getElementById("button").addEventListener("click", function(event) { document.getElementById("cancelled").innerText = this.id + ":" + event.defaultPrevented })</script>',
    )
    assert.equal(first.error, null)
    const result = card.dispatch("child", "click")
    assert.match(result.html, />button:true<\/p>/)
    assert.match(result.html, /<button[^>]*data-clicked="yes"/)
  } finally {
    card.dispose()
  }
})

test("discarded promise failures cannot leave an apparently usable card", async () => {
  for (const source of [
    'void (async () => { throw new Error("discarded failure") })()',
    'void Promise.reject(new Error("discarded failure"))',
    'void Promise.resolve().then(() => { throw new Error("discarded failure") })',
    'void (async () => { await Promise.resolve(); throw new Error("discarded failure") })()',
  ]) {
    const card = await CardRuntime.create()
    try {
      const result = card.render(`<p>Incomplete answer</p><script>${source}</script>`)
      assert.equal(result.error?.code, "script_error", source)
      assert.match(result.error.message, /discarded failure/)
      assert.equal(result.html, null)
      assert.equal(card.render("next").error.code, "runtime_closed")
    } finally {
      card.dispose()
    }
  }
})

test("handled promise failures retain normal JavaScript behavior", async () => {
  for (const source of [
    'void Promise.reject("caught").catch(() => document.body.innerText = "handled")',
    'void (async () => { try { await Promise.reject("caught") } catch { document.body.innerText = "handled" } })()',
    'void Promise.resolve().then(() => { throw "caught" }).catch(() => document.body.innerText = "handled")',
  ]) {
    const card = await CardRuntime.create()
    try {
      const result = card.render(`<script>${source}</script>`)
      assert.equal(result.error, null, source)
      assert.match(result.html, /handled/)
    } finally {
      card.dispose()
    }
  }
})

test("async event and timer failures are reported by the operation that runs them", async () => {
  const click = await CardRuntime.create()
  const timer = await CardRuntime.create()
  try {
    assert.equal(
      click.render(
        '<button id="tap">Tap</button><script>document.getElementById("tap").addEventListener("click", async () => { await Promise.resolve(); throw new Error("click failed") })</script>',
      ).error,
      null,
    )
    assert.match(click.dispatch("tap", "click").error.message, /click failed/)
    assert.equal(
      timer.render(
        '<script>setTimeout(async () => { await Promise.resolve(); throw new Error("timer failed") }, 50)</script>',
      ).error,
      null,
    )
    assert.match(timer.advance(50).error.message, /timer failed/)
  } finally {
    click.dispose()
    timer.dispose()
  }
})

test("snapshot-side rejections and replaced globals cannot bypass the tracker", async () => {
  for (const source of [
    'document.toString = () => { void Promise.reject(new Error("snapshot failed")); return "<p>partial</p>" }',
    'globalThis.__studyCheckRejections = () => {}; globalThis.Promise = null; void (async () => { throw new Error("native async failed") })()',
  ]) {
    const card = await CardRuntime.create()
    try {
      const result = card.render(`<script>${source}</script>`)
      assert.equal(result.error?.code, "script_error", source)
      assert.match(result.error.message, /failed/)
    } finally {
      card.dispose()
    }
  }
})

test("rejection storage is bounded and another card retains its own pending work", async () => {
  const failing = await CardRuntime.create()
  const working = await CardRuntime.create()
  try {
    const result = failing.render(
      "<script>for (let i = 0; i < 129; i++) void Promise.reject(i)</script>",
    )
    assert.equal(result.error.code, "script_rejection_limit")
    const other = working.render(
      '<p id="word"></p><script>void Promise.resolve().then(() => document.getElementById("word").innerText = "猫")</script>',
    )
    assert.equal(other.error, null)
    assert.match(other.html, />猫<\/p>/)
  } finally {
    failing.dispose()
    working.dispose()
  }
})

test("settled script promises do not corrupt handles and rejected results are reported", async () => {
  const fulfilled = await CardRuntime.create()
  const rejected = await CardRuntime.create()
  try {
    assert.equal(fulfilled.render("<p>answer</p><script>Promise.resolve(42)</script>").error, null)
    const result = rejected.render(
      '<script>(async () => { throw new Error("async failure") })()</script>',
    )
    assert.equal(result.error.code, "script_error")
    assert.match(result.error.message, /async failure/)
  } finally {
    fulfilled.dispose()
    rejected.dispose()
  }
})

test("inert JSON script blocks remain readable by card scripts", async () => {
  const card = await CardRuntime.create()
  try {
    const result = card.render(
      '<script id="options" type="application/json">{"label":"猫"}</script><p id="word"></p><script>document.getElementById("word").textContent = JSON.parse(document.getElementById("options").textContent).label</script>',
    )
    assert.equal(result.error, null)
    assert.match(result.html, />猫<\/p>/)
  } finally {
    card.dispose()
  }
})

test("each script has an eval lexical scope while explicit window state survives", async () => {
  const card = await CardRuntime.create()
  try {
    const first = card.render('<script>const label = "front"; window.shared = label</script>')
    assert.equal(first.error, null)
    const back = card.render(
      '<p id="label"></p><script>const label = "back"; document.getElementById("label").textContent = shared + label</script>',
    )
    assert.equal(back.error, null)
    assert.match(back.html, />frontback<\/p>/)
  } finally {
    card.dispose()
  }
})

test("throwing null still returns a failure and closes the runtime", async () => {
  const card = await CardRuntime.create()
  try {
    assert.equal(card.render("<script>throw null</script>").error.code, "script_error")
    assert.equal(card.render("next").error.code, "runtime_closed")
  } finally {
    card.dispose()
  }
})

test("scripts change actual card text, including innerText writes", async () => {
  const card = await CardRuntime.create()
  try {
    const result = card.render(
      '<div id="pitch"></div><script>const p = document.getElementById("pitch"); for (const kana of "はれる") { const span = document.createElement("span"); span.innerText = kana; p.appendChild(span); }</script>',
    )
    assert.equal(result.error, null)
    assert.match(result.html, /<span[^>]*>は<\/span><span[^>]*>れ<\/span><span[^>]*>る<\/span>/)
    assert.doesNotMatch(result.html, /<script/)
  } finally {
    card.dispose()
  }
})

test("card-local storage survives front and back but never another card", async () => {
  const first = await CardRuntime.create()
  const second = await CardRuntime.create()
  try {
    first.render(
      '<script>localStorage.setItem("example", "猫"); sessionStorage.setItem("side", "front")</script>',
    )
    const answer = first.render(
      '<p id="answer"></p><script>document.getElementById("answer").textContent = localStorage.getItem("example") + sessionStorage.getItem("side")</script>',
    )
    assert.equal(answer.error, null)
    assert.match(answer.html, /猫front/)
    const other = second.render(
      '<p id="answer"></p><script>document.getElementById("answer").textContent = String(localStorage.getItem("example"))</script>',
    )
    assert.match(other.html, />null<\/p>/)
  } finally {
    first.dispose()
    second.dispose()
  }
})

test("click listeners execute inside the interpreter and return updated output", async () => {
  const card = await CardRuntime.create()
  try {
    const initial = card.render(
      '<button id="toggle">猫</button><script>document.getElementById("toggle").addEventListener("click", event => { event.target.innerText = "犬" })</script>',
    )
    assert.equal(initial.error, null)
    const changed = card.dispatch("toggle", "click")
    assert.equal(changed.error, null)
    assert.match(changed.html, />犬<\/button>/)
  } finally {
    card.dispose()
  }
})

test("timers run only when the host advances the clock and are cleared on a new face", async () => {
  const card = await CardRuntime.create()
  try {
    card.render(
      '<p id="value">before</p><script>setTimeout(() => document.getElementById("value").innerText = "after", 50)</script>',
    )
    assert.match(card.advance(49).html, />before<\/p>/)
    assert.match(card.advance(50).html, />after<\/p>/)
    card.render(
      '<p id="value">new</p><script>setTimeout(() => document.getElementById("value").innerText = "stale", 50)</script>',
    )
    card.render('<p id="value">next face</p>')
    assert.match(card.advance(100).html, />next face<\/p>/)
  } finally {
    card.dispose()
  }
})

test("network, browser, filesystem and host constructors are unavailable", async () => {
  const card = await CardRuntime.create()
  try {
    const result = card.render(`<pre id="result"></pre><script>
      const names = ['fetch','XMLHttpRequest','WebSocket','RTCPeerConnection','Worker','process','require','parent','top'];
      document.getElementById('result').textContent = names.map(name => typeof globalThis[name]).join(',') + ':' + atob.constructor('return typeof process')();
    </script>`)
    assert.equal(result.error, null)
    assert.match(result.html, new RegExp(`${Array(9).fill("undefined").join(",")}:undefined`))
  } finally {
    card.dispose()
  }
})

test("unsupported scripts report failure instead of returning a usable-looking card", async () => {
  const card = await CardRuntime.create()
  try {
    const result = card.render(
      "<b>Unchanged answer</b><script>getComputedStyle(document.body)</script>",
    )
    assert.equal(result.error.code, "script_error")
    assert.match(result.error.message, /getComputedStyle/)
    assert.equal(result.html, null)
    assert.equal(card.render("next").error.code, "runtime_closed")
  } finally {
    card.dispose()
  }
})

test("missing external scripts are explicit and supplied scripts execute in source order", async () => {
  const missing = await CardRuntime.create()
  const supplied = await CardRuntime.create()
  try {
    assert.equal(
      missing.render('<script src="_options.js"></script>').error.code,
      "script_resource_missing",
    )
    const result = supplied.render(
      '<script src="_options.js"></script><p id="value"></p><script>document.getElementById("value").textContent = options.label</script>',
      { "_options.js": 'globalThis.options = {label: "猫"}' },
    )
    assert.equal(result.error, null)
    assert.match(result.html, />猫<\/p>/)
  } finally {
    missing.dispose()
    supplied.dispose()
  }
})

test("infinite loops are interrupted and the card runtime is discarded", async () => {
  const card = await CardRuntime.create({ timeLimitMs: 25 })
  try {
    const result = card.render("<script>while(true) {}</script>")
    assert.equal(result.error.code, "script_timeout")
    assert.equal(card.render("next").error.code, "runtime_closed")
  } finally {
    card.dispose()
  }
})

test("memory, state and input limits refuse excess instead of leaking across cards", async () => {
  for (const [html, code] of [
    ["<script>new Uint8Array(128 * 1024 * 1024)</script>", "script_memory_limit"],
    ['<script>localStorage.setItem("large", "a".repeat(40000))</script>', "script_state_limit"],
    ["a".repeat(2 * 1024 * 1024 + 1), "script_input_limit"],
  ]) {
    const card = await CardRuntime.create()
    try {
      assert.equal(card.render(html).error.code, code)
    } finally {
      card.dispose()
    }
  }
})

test("caught console errors remain visible as compatibility diagnostics", async () => {
  const card = await CardRuntime.create()
  try {
    const result = card.render(
      "<p>Partial</p><script>try { missingBrowserAPI() } catch (error) { console.error(error.message) }</script>",
    )
    assert.equal(result.error, null)
    assert.equal(result.diagnostics[0].level, "error")
    assert.match(result.diagnostics[0].message, /missingBrowserAPI/)
  } finally {
    card.dispose()
  }
})
