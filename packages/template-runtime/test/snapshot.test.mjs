import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { readFile } from "node:fs/promises"
import { join } from "node:path"
import test from "node:test"
import { browserEngines, launchBrowser } from "./browsers.mjs"

for (const engine of browserEngines) {
  for (const [interactive, mediaRoute] of [false, true].flatMap((interactive) =>
    ["card-media", "import-media"].map((route) => [interactive, route]),
  )) {
    test(`${engine.name()}: untrusted ${mediaRoute} snapshots cannot execute or navigate with interactions ${interactive}`, async () => {
      const browser = await launchBrowser(engine)
      try {
        const context = await browser.newContext()
        await context.addCookies([
          {
            name: "session_id",
            value: "private-session",
            url: "http://study-runtime.test/",
            httpOnly: true,
            sameSite: "Lax",
          },
        ])
        const unexpected = []
        const mediaRequests = []
        const mediaBase = `http://study-runtime.test/study/${mediaRoute}/capability/`
        const bridgeHash = createHash("sha256")
          .update(
            await readFile(
              new URL("../bridges/study_card_speech.js", import.meta.url),
            ),
          )
          .update("\n")
          .update(
            await readFile(new URL("../bridges/study_card_audio.js", import.meta.url)),
          )
          .update("\n")
          .update(await readFile(new URL("../src/frame.js", import.meta.url)))
          .digest("base64")
        await context.route("**/*", async (route) => {
          const url = new URL(route.request().url())
          // WebKit requests blob images for native video controls even on script-free pages.
          const nativeVideoIcon =
            engine.name() === "webkit" &&
            url.protocol === "blob:" &&
            route.request().resourceType() === "image" &&
            !route.request().isNavigationRequest()
          if (nativeVideoIcon) return route.continue()
          const styles = {
            "_outer.css":
              '@import url("_inner.css"); @import url("https://untrusted.invalid/nested.css");',
            "_inner.css": "#styled { color: rgb(12, 34, 56); }",
            "_updated.css": "#patched { font-size: 23px; }",
          }
          const filename = url.href.startsWith(mediaBase) && url.href.slice(mediaBase.length)
          if (Object.hasOwn(styles, filename)) {
            mediaRequests.push({
              filename,
              cookie: route.request().headers().cookie,
              frame: route.request().frame().url(),
            })
            return route.fulfill({
              body: styles[filename],
              contentType: "text/css",
              headers: { "Access-Control-Allow-Origin": "*" },
            })
          }
          if (
            url.origin !== "http://study-runtime.test" ||
            !["/", "/snapshot.mjs"].includes(url.pathname)
          ) {
            unexpected.push({
              url: url.href,
              type: route.request().resourceType(),
              frame: route.request().frame().url(),
            })
            return route.abort()
          }
          const script = url.pathname === "/snapshot.mjs"
          const body = script
            ? await readFile(
                process.env.STUDY_RUNTIME_BUILD_DIR
                  ? join(process.env.STUDY_RUNTIME_BUILD_DIR, "snapshot.mjs")
                  : new URL("../dist/snapshot.mjs", import.meta.url),
                "utf8",
              )
            : `<!doctype html><title>Snapshot boundary</title><iframe id="card" sandbox="${interactive ? "allow-scripts" : ""}" referrerpolicy="no-referrer"></iframe>`
          await route.fulfill({
            body,
            contentType: script ? "text/javascript" : "text/html",
            headers: {
              "Content-Security-Policy": `default-src 'none'; script-src 'self' 'sha256-${bridgeHash}'; style-src 'unsafe-inline' ${mediaBase}; img-src data: ${mediaBase}; media-src data: ${mediaBase}; font-src data: ${mediaBase}; base-uri ${mediaBase}; frame-src 'none'; connect-src 'none'`,
            },
          })
        })
        const page = await context.newPage()
        await page.goto("http://study-runtime.test")
        const clean = await page.evaluate(
          async ({ base, interactive }) => {
            const { snapshotDocument } = await import("/snapshot.mjs")
            window.escaped = false
            window.bridgeMessages = []
            window.addEventListener("message", (event) => {
              window.bridgeMessages.push({ origin: event.origin, data: event.data })
            })
            const source = `<!doctype html><html><head>
        <base href="https://untrusted.invalid/">
        <meta http-equiv="refresh" content="0; url=https://untrusted.invalid/refresh">
        <link rel="preconnect" href="https://untrusted.invalid">
        <link rel="dns-prefetch" href="//untrusted.invalid">
        <link rel="stylesheet" href="_outer.css">
        <link rel="stylesheet" href="https://untrusted.invalid/remote.css">
        <link rel="stylesheet" href="../other-profile.css">
        <link rel="stylesheet prefetch" href="${base}not-a-stylesheet.css">
        <style>@import url("https://untrusted.invalid/import"); .card { color: rgb(128, 0, 128); background-image: url("/private-side-effect"); }</style>
        </head><body class="card" onload="parent.escaped=true">
        <ruby>猫<rt>ねこ</rt></ruby><svg><path d="M0 0 L10 10"/><a xlink:href="https://untrusted.invalid/svg"><text>svg</text></a><set attributeName="href" to="https://untrusted.invalid/set" /></svg>
        <p id="styled">Local stylesheet</p>
        <select id="selection"><optgroup disabled><option data-study-selected="false">猫</option></optgroup><option data-study-selected="true">犬</option><option data-study-selected="false">鳥</option></select>
        <script>parent.escaped=true</script>
        <img src="https://untrusted.invalid/image" onerror="parent.escaped=true">
        <img src="/private-side-effect">
        <a data-study-node="study-node-1-1" href="https://untrusted.invalid/link" ping="https://untrusted.invalid/ping">external</a>
        <a href="#kept">internal</a><div id="kept">はれる</div>
        <iframe src="https://untrusted.invalid/frame"></iframe>
        <form action="https://untrusted.invalid/form"><button formaction="https://untrusted.invalid/button">send</button></form>
        <video autoplay poster="https://untrusted.invalid/poster"></video>
        <math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=parent.escaped=true>">
        </body></html>`
            const html = snapshotDocument(
              source,
              base,
              interactive ? { channel: "boundary-test" } : null,
            )
            const frame = document.getElementById("card")
            await new Promise((resolve) => {
              frame.onload = resolve
              frame.srcdoc = html
            })
            const parsed = new DOMParser().parseFromString(html, "text/html")
            parsed.querySelector("script[data-study-frame-bridge]")?.remove()
            return parsed.documentElement.outerHTML
          },
          { base: mediaBase, interactive },
        )
        assert.doesNotMatch(
          clean,
          /<script|onerror=|onload=|<iframe|http-equiv="refresh"|<form|formaction=|autoplay|<set\b/,
        )
        assert.match(clean, /<ruby>猫<rt>ねこ<\/rt><\/ruby>/)
        assert.match(clean, /<path d="M0 0 L10 10"/)
        assert.match(clean, /href="#kept"/)
        assert.doesNotMatch(clean, /href="https:|src="https:|poster="https:|src="\/private/)
        const frame = page.frameLocator("#card")
        assert.equal(await frame.locator('link[rel="stylesheet"]').count(), 1)
        assert.equal(await frame.locator("link:not([rel='stylesheet'])").count(), 0)
        assert.equal(
          await frame.locator("#styled").evaluate((element) => getComputedStyle(element).color),
          "rgb(12, 34, 56)",
        )
        assert.equal(
          await frame.locator("body").evaluate((body) => getComputedStyle(body).color),
          "rgb(128, 0, 128)",
        )
        await frame.getByText("external", { exact: true }).click()
        if (interactive) {
          assert.equal(await frame.locator("#selection").inputValue(), "犬")
          await page.waitForFunction(() =>
            window.bridgeMessages.some(({ data }) => data.kind === "event"),
          )
          const reports = await page.evaluate(() => window.bridgeMessages)
          assert.ok(
            reports.some(
              ({ data }) =>
                data.kind === "resource-warning" &&
                data.message.startsWith("blocked_resource: style-src"),
            ),
            JSON.stringify(reports),
          )
          const messages = reports.filter(({ data }) => data.kind !== "resource-warning")
          assert.deepEqual(
            messages.map(({ origin, data }) => [origin, data.channel, data.kind]),
            [
              ["null", "boundary-test", "ready"],
              ["null", "boundary-test", "event"],
            ],
          )
          assert.equal(messages[1].data.target, "study-node-1-1")
          assert.equal(messages[1].data.type, "click")
          await page.evaluate(async (base) => {
            const { snapshotDocument } = await import("/snapshot.mjs")
            const frame = document.getElementById("card")
            const html = snapshotDocument(
              `<html><head><style>body { color: rgb(0, 128, 0) }</style><link rel="stylesheet" href="_updated.css"></head><body>
            <p id="patched">Patched safely</p><script>parent.escaped = true</script>
            <select id="selection"><optgroup disabled><option data-study-selected="false">猫</option></optgroup><option data-study-selected="false">犬</option><option data-study-selected="true">鳥</option></select>
            <img src="https://untrusted.invalid/patched" onerror="parent.escaped = true">
            <meta http-equiv="refresh" content="0; url=https://untrusted.invalid/refresh">
            </body></html>`,
              base,
              { channel: "boundary-test", operation: 1 },
            )
            frame.contentWindow.postMessage(
              { channel: "wrong", kind: "snapshot", html, operation: 2, sequence: 1 },
              "*",
            )
            frame.contentWindow.postMessage(
              { channel: "boundary-test", kind: "snapshot", html, operation: 1, sequence: 1 },
              "*",
            )
          }, mediaBase)
          await frame.locator("#patched").waitFor()
          await page
            .frames()[1]
            .waitForFunction(
              () => getComputedStyle(document.getElementById("patched")).fontSize === "23px",
            )
          assert.equal(await frame.locator("#selection").inputValue(), "鳥")
          assert.equal(
            await frame.locator("body").evaluate((body) => getComputedStyle(body).color),
            "rgb(0, 128, 0)",
          )
          assert.equal(await frame.locator("script").count(), 1)
          assert.equal(await frame.locator("meta[http-equiv='Content-Security-Policy']").count(), 1)
          assert.equal(await frame.locator("[onerror], meta[http-equiv=refresh]").count(), 0)
          await page.waitForFunction(() =>
            window.bridgeMessages.some(({ data }) => data.kind === "ready" && data.operation === 1),
          )
        } else {
          assert.deepEqual(await page.evaluate(() => window.bridgeMessages), [])
        }
        assert.equal(await page.evaluate(() => window.escaped), false)
        assert.equal(page.frames().length, 2)
        assert.equal(page.frames()[1].url(), "about:srcdoc")
        assert.deepEqual(unexpected, [])
        assert.deepEqual(
          [...new Set(mediaRequests.map(({ filename }) => filename))].sort(),
          interactive ? ["_inner.css", "_outer.css", "_updated.css"] : ["_inner.css", "_outer.css"],
        )
        assert.ok(
          mediaRequests.every(({ cookie }) => !cookie),
          JSON.stringify(mediaRequests),
        )
        const refused = await page.evaluate(async () => {
          const { snapshotDocument } = await import("/snapshot.mjs")
          return [null, "x".repeat(2 * 1024 * 1024 + 1)]
            .map((input) => {
              try {
                snapshotDocument(input, "/study/card-media/test/")
                return false
              } catch {
                return true
              }
            })
            .concat(
              [
                "https://untrusted.invalid/study/card-media/test/",
                "https://untrusted.invalid/study/import-media/test/",
                "/unscoped/",
                "/study/import-media/",
                "/study/import-media/test/nested/",
                "/study/import-media/test/?query=1",
                "/study/import-media/test/#fragment",
              ].map((base) => {
                try {
                  snapshotDocument("test", base)
                  return false
                } catch {
                  return true
                }
              }),
            )
        })
        assert.deepEqual(refused, Array(9).fill(true))
      } finally {
        await browser.close()
      }
    })
  }
}
