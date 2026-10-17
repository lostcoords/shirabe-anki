import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { join } from "node:path"
import test from "node:test"
import { pathToFileURL } from "node:url"
import { browserEngines, launchBrowser } from "./browsers.mjs"

for (const engine of browserEngines) {
  test(`${engine.name()}: bundled jQuery card operations match the browser across faces`, async () => {
    const { CardRuntime } = await import(
      process.env.STUDY_RUNTIME_BUILD_DIR
        ? pathToFileURL(join(process.env.STUDY_RUNTIME_BUILD_DIR, "runtime.mjs"))
        : new URL("../dist/runtime.mjs", import.meta.url)
    )
    const browser = await launchBrowser(engine)
    const card = await CardRuntime.create()
    try {
      const page = await browser.newPage()
      await page.addScriptTag({
        content: await readFile(new URL(import.meta.resolve("jquery")), "utf8"),
      })
      const faces = [
        `<button id="word">猫</button><div id="holder"></div><pre id="result"></pre><script>
        window.firstDocument = document;
        $.fn.label = function(value) { return this.text(value); };
        $(function() {
          $('#holder').append($('<span>').addClass('label').label('日本語'));
          $('#word').data('reading', 'ねこ').on('click', function() { $(this).label('犬'); }).trigger('click');
          $('#result').text(JSON.stringify([$.fn.jquery, $('#holder .label').text(), $('#word').text(), $('#word').data('reading')]));
        });
      </script>`,
        `<p id="word"></p><pre id="result"></pre><script>
        $(function() {
          $('#word').label('answer');
          $('#result').text(JSON.stringify([document === firstDocument, $('#word').text(), $('#holder').length]));
        });
      </script>`,
      ]
      for (const html of faces) {
        await page.evaluate((html) => {
          document.body.innerHTML = html
        }, html)
        await page.evaluate(() => {
          for (const old of document.body.querySelectorAll("script")) {
            const script = document.createElement("script")
            script.textContent = old.textContent
            old.replaceWith(script)
          }
        })
        await page.waitForFunction(() => document.querySelector("#result").textContent !== "")
        const expected = await page.locator("#result").textContent()
        const result = card.render(html)
        assert.equal(result.error, null)
        const actual = await page.evaluate(
          (html) =>
            new DOMParser().parseFromString(html, "text/html").querySelector("#result").textContent,
          result.html,
        )
        assert.equal(actual, expected)
      }
    } finally {
      card.dispose()
      await browser.close()
    }
  })

  test(`${engine.name()}: supported controls retain the same runtime behavior across browser hosts`, async () => {
    const { CardRuntime } = await import(
      process.env.STUDY_RUNTIME_BUILD_DIR
        ? pathToFileURL(join(process.env.STUDY_RUNTIME_BUILD_DIR, "runtime.mjs"))
        : new URL("../dist/runtime.mjs", import.meta.url)
    )
    const browser = await launchBrowser(engine)
    try {
      const page = await browser.newPage()
      const cases = [
        [
          "checkbox defaults",
          '<input id="a" type="checkbox" checked>',
          `
        const a = document.getElementById('a');
        result.push([a.checked, a.defaultChecked, a.value]);
        a.checked = false; a.defaultChecked = false; a.defaultChecked = true;
        result.push([a.checked, a.defaultChecked]);`,
        ],
        [
          "cancelled mixed checkbox",
          '<input id="a" type="checkbox" checked>',
          `
        const a = document.getElementById('a'); a.indeterminate = true;
        for (const type of ['click','input','change']) a.addEventListener(type, event => {
          result.push([type, a.checked, a.indeterminate]); event.preventDefault();
        });
        a.click(); result.push([a.checked, a.indeterminate]);`,
        ],
        [
          "radio activation and cancellation",
          '<input id="a" name="group" type="radio" checked><input id="b" name="group" type="radio">',
          `
        const a = document.getElementById('a'), b = document.getElementById('b');
        for (const type of ['click','input','change']) b.addEventListener(type, () => result.push([type, a.checked, b.checked]));
        b.click(); b.click();
        a.onclick = event => event.preventDefault(); a.click();
        result.push([a.checked, b.checked]);`,
        ],
        [
          "single selection",
          '<select id="s"><option>猫</option><option value="dog">犬</option></select>',
          `
        const s = document.getElementById('s');
        result.push([s.value, s.selectedIndex]); s.value = 'dog';
        result.push([s.value, s.selectedIndex, s.options[1].defaultSelected]);
        s.selectedIndex = -1; result.push([s.value, s.selectedIndex]);
        s.value = 'missing'; result.push([s.value, s.selectedIndex]);
        s.options[0].selected = true; result.push([s.value, s.selectedIndex]);`,
        ],
        [
          "multiple duplicate values",
          '<select id="s" multiple><option value="same">一</option><option value="same">二</option><option value="other">三</option></select>',
          `
        const s = document.getElementById('s'); result.push([s.value, s.selectedIndex]);
        s.options[1].selected = true; s.options[2].selected = true;
        result.push([...s.selectedOptions].map(option => option.textContent));
        s.value = 'same'; result.push([...s.selectedOptions].map(option => option.textContent));
        s.selectedIndex = 2; result.push([...s.selectedOptions].map(option => option.textContent));`,
        ],
        [
          "disabled defaults and removed selection",
          '<select id="s"><optgroup disabled><option>猫</option></optgroup><option>犬</option><option>鳥</option></select>',
          `
        const s = document.getElementById('s'); result.push([s.value, s.selectedIndex]);
        s.value = '鳥'; s.options[2].remove(); result.push([s.value, s.selectedIndex]);`,
        ],
      ]
      for (const [name, html, code] of cases) {
        const source = `${html}<pre id="result"></pre><script>(() => { const result = []; ${code}; document.getElementById('result').textContent = JSON.stringify(result) })()</script>`
        await page.setContent(source)
        const expected = JSON.parse(await page.locator("#result").textContent())
        const card = await CardRuntime.create()
        try {
          const actual = card.render(source)
          assert.equal(actual.error, null, name)
          const text = await page.evaluate(
            (html) =>
              new DOMParser().parseFromString(html, "text/html").querySelector("#result")
                .textContent,
            actual.html,
          )
          if (name === "disabled defaults and removed selection") {
            const canonical = [
              ["犬", 1],
              ["犬", 1],
            ]
            assert.deepEqual(
              expected,
              engine.name() === "webkit"
                ? [
                    ["猫", 0],
                    ["犬", 1],
                  ]
                : canonical,
              name,
            )
            assert.deepEqual(JSON.parse(text), canonical, name)
          } else {
            assert.deepEqual(JSON.parse(text), expected, name)
          }
        } finally {
          card.dispose()
        }
      }
    } finally {
      await browser.close()
    }
  })

  test(`${engine.name()}: a real module worker renders, handles clicks, preserves face state and terminates`, async () => {
    const browser = await launchBrowser(engine)
    try {
      const context = await browser.newContext()
      const unexpected = []
      await context.route("**/*", async (route) => {
        const url = new URL(route.request().url())
        const name = url.pathname.slice(1)
        if (
          url.origin !== "http://study-runtime.test" ||
          !["", "client.mjs", "worker.mjs", "stuck-worker.mjs"].includes(name)
        ) {
          unexpected.push(url.href)
          return route.abort()
        }
        const body =
          name === "stuck-worker.mjs"
            ? 'postMessage({kind:"ready"}); addEventListener("message", () => { while(true){} })'
            : name
              ? await readFile(
                  process.env.STUDY_RUNTIME_BUILD_DIR
                    ? join(process.env.STUDY_RUNTIME_BUILD_DIR, name)
                    : new URL(`../dist/${name}`, import.meta.url),
                  "utf8",
                )
              : "<!doctype html><title>Study runtime test</title>"
        await route.fulfill({
          body,
          contentType: name ? "text/javascript" : "text/html",
          headers: {
            "Content-Security-Policy":
              name === "worker.mjs"
                ? "default-src 'none'; script-src 'wasm-unsafe-eval'; connect-src 'none'"
                : "default-src 'none'; script-src 'self'; worker-src 'self'; connect-src 'none'",
          },
        })
      })
      const page = await context.newPage()
      await page.goto("http://study-runtime.test")
      const parentCanCompileWasm = await page.evaluate(async () => {
        try {
          await WebAssembly.compile(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]))
          return true
        } catch {
          return false
        }
      })
      assert.equal(parentCanCompileWasm, false)
      const result = await page.evaluate(async () => {
        const { ScriptCard } = await import("/client.mjs")
        const card = new ScriptCard("/worker.mjs")
        const first = await card.render(
          '<button id="word">猫</button><script>localStorage.setItem("answer", "犬"); document.getElementById("word").addEventListener("click", () => document.getElementById("word").innerText = localStorage.getItem("answer"))</script>',
        )
        const clicked = await card.dispatch("word", "click")
        const back = await card.render(
          '<p id="back"></p><script>document.getElementById("back").innerText = localStorage.getItem("answer")</script>',
        )
        card.dispose()
        const closed = await card.advance(50)
        const looping = new ScriptCard("/worker.mjs")
        const timeout = await looping.render("<script>while(true){}</script>")
        looping.dispose()
        let heartbeats = 0
        const heartbeat = setInterval(() => heartbeats++, 10)
        const stuck = new ScriptCard("/stuck-worker.mjs")
        const stopped = await stuck.render("anything")
        clearInterval(heartbeat)
        stuck.dispose()
        const cancelled = new ScriptCard("/worker.mjs")
        const queued = cancelled.render("never rendered")
        cancelled.dispose()
        const asynchronous = new ScriptCard("/worker.mjs")
        const rejected = await asynchronous.render(
          '<script>void (async () => { await Promise.resolve(); throw new Error("detached worker failure") })()</script>',
        )
        asynchronous.dispose()
        return {
          first,
          clicked,
          back,
          closed,
          timeout,
          stopped,
          heartbeats,
          cancelled: await queued,
          rejected,
        }
      })
      assert.equal(result.first.error, null)
      assert.match(result.first.html, />猫<\/button>/)
      assert.match(result.clicked.html, />犬<\/button>/)
      assert.match(result.back.html, />犬<\/p>/)
      assert.equal(result.closed.error.code, "runtime_closed")
      assert.equal(result.timeout.error.code, "script_timeout")
      assert.equal(result.stopped.error.code, "script_worker_timeout")
      assert.ok(result.heartbeats > 10)
      assert.equal(result.cancelled.error.code, "runtime_closed")
      assert.equal(result.rejected.error.code, "script_error")
      assert.match(result.rejected.error.message, /detached worker failure/)
      assert.deepEqual(unexpected, [])
    } finally {
      await browser.close()
    }
  })
}
