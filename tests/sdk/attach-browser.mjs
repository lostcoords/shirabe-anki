import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const performanceBudgets = {
  minimumFlips: 100,
  minimumSamples: 12,
  coldP95ms: 50,
  warmP95ms: 25,
};

function assertPerformanceBudgets(report) {
  assert.deepEqual(report.modes.map((mode) => mode.mode).sort(), [
    "desktop-mouse",
    "mobile-touch",
  ]);
  for (const mode of report.modes) {
    assert.ok(
      Number.isInteger(mode.flips) &&
        mode.flips >= performanceBudgets.minimumFlips,
      "At least 100 flips required",
    );
    assert.equal(mode.hostLookups, mode.flips, "One lookup per flip gesture");
    for (const temperature of ["cold", "warm"]) {
      const latency = mode.lookupEventToRenderedCard[temperature];
      assert.ok(
        Number.isInteger(latency.samples) &&
          latency.samples >= performanceBudgets.minimumSamples,
        "Too few latency samples",
      );
      assert.ok(
        Number.isFinite(latency.p95ms) && latency.p95ms >= 0,
        "Invalid latency measurement",
      );
      assert.ok(
        latency.p95ms <= performanceBudgets[`${temperature}P95ms`],
        `${mode.mode} ${temperature} p95 ${latency.p95ms}ms exceeded budget`,
      );
    }
    const counts = mode.requestCounts;
    assert.equal(
      counts["/api/v1/me"],
      1,
      "Account preferences refetched over flips",
    );
    assert.equal(
      counts["/api/v1/words/%E7%8C%AB"],
      1,
      "Cached definition refetched over flips",
    );
    for (const [path, count] of Object.entries(counts)) {
      assert.ok(Number.isInteger(count) && count >= 0, "Invalid request count");
      if (["/api/v1/me", "/api/v1/words/%E7%8C%AB"].includes(path)) continue;
      assert.ok(
        path === "/api/v1/srs/states/lookup" && count <= mode.flips,
        `Unexpected or repeated background request: ${path}`,
      );
    }
    assert.deepEqual(
      mode.retained.afterFlips,
      mode.retained.attached,
      "Listeners grew over reused-root flips",
    );
    assert.equal(
      mode.retained.detached["document.getElementById('qa')"].length,
      0,
      "Root listeners retained after detach",
    );
  }
}

const performanceArgument = process.argv.indexOf("--check-performance-report");
if (performanceArgument >= 0) {
  const path = process.argv[performanceArgument + 1];
  assert.ok(
    path,
    "--check-performance-report requires a report path or - for stdin",
  );
  assertPerformanceBudgets(
    JSON.parse(readFileSync(path === "-" ? 0 : path, "utf8")),
  );
  console.log("Fixture performance budgets passed");
  process.exit(0);
}

const flips = Number(process.env.ATTACH_FLIPS ?? 100);
const memoryFlipsArgument = process.argv.indexOf("--memory-flips");
const memoryFlipCount =
  memoryFlipsArgument >= 0
    ? Number(process.argv[memoryFlipsArgument + 1])
    : 300;
const memorySettleArgument = process.argv.indexOf("--memory-settle-ms");
const memorySettleMs =
  memorySettleArgument >= 0
    ? Number(process.argv[memorySettleArgument + 1])
    : 0;
assert.ok(
  Number.isInteger(memoryFlipCount) &&
    memoryFlipCount >= 100 &&
    memoryFlipCount <= 1000 &&
    memoryFlipCount % 100 === 0,
  "--memory-flips must be a multiple of 100 between 100 and 1000",
);
assert.ok(
  Number.isInteger(memorySettleMs) &&
    memorySettleMs >= 0 &&
    memorySettleMs <= 60000,
  "--memory-settle-ms must be between 0 and 60000",
);
const probeHiddenHost = process.argv.includes("--probe-hidden-host");
const bundleArgument = process.argv.indexOf("--sdk-bundle");
const sdkBundleFile =
  bundleArgument < 0 ? undefined : process.argv[bundleArgument + 1];
assert.ok(
  bundleArgument < 0 || (sdkBundleFile && !sdkBundleFile.startsWith("--")),
  "--sdk-bundle requires a file path",
);
assert.ok(
  Number.isInteger(flips) && flips >= 100,
  "ATTACH_FLIPS must be at least 100",
);
const artifacts = mkdtempSync(join(tmpdir(), "shirabe-attach-browser-"));
const snapshot = JSON.parse(
  execFileSync(
    "python3",
    [
      fileURLToPath(
        new URL("../offline_fixture.py", import.meta.url),
      ),
    ],
    { encoding: "utf8" },
  ),
);
const mixedSnapshot = JSON.parse(
  execFileSync(
    "python3",
    [
      "-c",
      'import json; from offline_fixture import prepared; print(json.dumps(prepared(("勉強",), parse=lambda text: {"tokens": [{"surface": text, "lemma": text, "reading": "ベンキョウ", "position": 0, "length": 2, "kind": "word", "entry": {"dictionary": "jmdict", "sourceId": "2"}}]}).snapshot))',
    ],
    {
      cwd: fileURLToPath(new URL("../", import.meta.url)),
      encoding: "utf8",
    },
  ),
);
const cat = snapshot.segments[0].results[0].candidates[0];
const snapshotJSON = JSON.stringify(snapshot);
const snapshotName = `_shirabe_snapshot_v1_${createHash("sha256").update(snapshotJSON).digest("hex")}.json`;
const bootstrap = readFileSync(
  new URL("../../addon/src/web/attach.js", import.meta.url),
  "utf8",
);
const bundleText = sdkBundleFile
  ? readFileSync(sdkBundleFile, "utf8")
  : readFileSync(new URL("./shirabe-card.js", import.meta.resolve("@shirabe-org/card/standalone")), "utf8");
const requests = [];
let heldLookup;
let releaseLookup;
let snapshotLoads = 0;
const server = createServer(async (request, response) => {
  const url = new URL(request.url, "http://localhost");
  if (url.pathname === `/${snapshotName}`) {
    snapshotLoads++;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(snapshotJSON);
    return;
  }
  if (!url.pathname.startsWith("/api/")) {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end(
      '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Shared attachment fixture</title><style>body{margin:24px;font:24px sans-serif;background:#faf9f6}#qa{line-height:2;padding:24px 0}.cloze{color:#145b8c}.hidden{display:none}button{font:inherit}rt{font-size:12px}</style><main id="qa"></main><button id="grade">Grade fixture</button>',
    );
    return;
  }
  let body = "";
  for await (const chunk of request) body += chunk;
  const data = body ? JSON.parse(body) : {};
  requests.push({ method: request.method, path: url.pathname, body: data });
  let payload;
  if (url.pathname === "/api/v1/me")
    payload = {
      preferences: { languages: ["en"], dictionaryReveal: {} },
      credential: { scopes: ["user:account:read", "user:dictionary:read", "user:study:read"] },
    };
  else if (url.pathname === "/api/v1/words/identify")
    payload = { words: [{ candidates: [cat] }] };
  else if (url.pathname.startsWith("/api/v1/words/")) payload = cat;
  else if (url.pathname === "/api/v1/parse")
    payload = {
      stackFingerprint: "test",
      results: [{ tokens: [
        {
          surface: "猫",
          lemma: "猫",
          reading: "ネコ",
          startOffset: 0,
          endOffset: 1,
          kind: "word",
        },
      ] }],
    };
  else if (url.pathname === "/api/v1/srs/states/lookup")
    payload = { states: data.words.map((lemma) => ({ lemma, state: "none" })) };
  else {
    response.writeHead(404, { "Content-Type": "application/json" });
    response.end(
      JSON.stringify({ title: `Unexpected fixture request: ${url.pathname}` }),
    );
    return;
  }
  if (url.searchParams.get("entry") === "jmdict:held") {
    await new Promise((resolve) => {
      releaseLookup = resolve;
      heldLookup?.();
    });
  }
  response.writeHead(200, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify(payload));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const apiBase = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium
  .launch({ headless: true })
  .catch(async (error) => {
    await new Promise((resolve) => server.close(resolve));
    throw error;
  });
const report = {
  browser: browser.version(),
  sdkBundleSource:
    sdkBundleFile ?? "released @shirabe-org/card bundle",
  sdkBundleSha256: createHash("sha256").update(bundleText).digest("hex"),
  fixture: "tests/offline_fixture.py",
  api: "local HTTP fixture, not production latency",
  timing:
    "Input event to card_opened callback (DOM ready), excluding reveal animation",
  modes: [],
  failures: [],
  probeHiddenHost,
  performanceBudgets,
};
let currentPage;

function latencies(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const at = (percentile) =>
    Number(
      sorted[
        Math.min(sorted.length - 1, Math.ceil(sorted.length * percentile) - 1)
      ].toFixed(2),
    );
  return {
    samples: sorted.length,
    p50ms: at(0.5),
    p95ms: at(0.95),
    maxMs: at(1),
  };
}

function requestCounts(since) {
  const counts = {};
  for (const request of requests.slice(since))
    counts[request.path] = (counts[request.path] ?? 0) + 1;
  return counts;
}

const dictionaryRequests = (since) =>
  requests
    .slice(since)
    .filter((request) => !request.path.startsWith("/api/v1/srs/")).length;

async function retainedListeners(session) {
  const counts = {};
  for (const expression of [
    "window",
    "document",
    "document.getElementById('qa')",
  ]) {
    const { result } = await session.send("Runtime.evaluate", {
      expression,
      objectGroup: "listeners",
    });
    const { listeners } = await session.send("DOMDebugger.getEventListeners", {
      objectId: result.objectId,
    });
    counts[expression] = listeners
      .map(
        (listener) =>
          `${listener.type}:${listener.useCapture ? "capture" : "bubble"}`,
      )
      .sort();
  }
  await session.send("Runtime.releaseObjectGroup", {
    objectGroup: "listeners",
  });
  return counts;
}

async function retainedMemory(session, page) {
  await page.evaluate(() => {
    const fixture = window.fixture;
    fixture.attachment?.detach();
    fixture.attachment = null;
    fixture.root.textContent = "[question]";
    fixture.timings = [];
    fixture.trace = [];
    fixture.grades = [];
  });
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  await session.send("HeapProfiler.collectGarbage");
  const heap = await session.send("Runtime.getHeapUsage");
  const dom = await session.send("Memory.getDOMCounters");
  return {
    jsHeapBytes: heap.usedSize,
    embedderHeapBytes: heap.embedderHeapUsedSize,
    backingStorageBytes: heap.backingStorageSize,
    domDocuments: dom.documents,
    domNodes: dom.nodes,
    jsEventListeners: dom.jsEventListeners,
  };
}

async function settledMemory(session, page, baseline) {
  if (!memorySettleMs) return undefined;
  await page.waitForTimeout(memorySettleMs);
  const samples = [];
  for (let gc = 1; gc <= 3; gc++) {
    const sample = await retainedMemory(session, page);
    samples.push({
      gc,
      ...sample,
      jsHeapGrowthBytes: sample.jsHeapBytes - baseline.jsHeapBytes,
    });
  }
  return { idleMs: memorySettleMs, completedFlips: memoryFlipCount, samples };
}

try {
  for (const mobile of [false, true]) {
    const mode = mobile ? "mobile-touch" : "desktop-mouse";
    const context = await browser.newContext({
      viewport: mobile
        ? { width: 390, height: 844 }
        : { width: 1100, height: 800 },
      isMobile: mobile,
      hasTouch: mobile,
    });
    const page = await context.newPage();
    currentPage = page;
    try {
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.setDefaultTimeout(5000);
      await page.goto(apiBase);
      await page.addScriptTag({ content: bundleText });
      await page.evaluate(
        ({ apiBase, snapshot }) => {
          const SDK = window.ShirabeCard;
          for (const method of [
            "attach",
            "createAnkiHost",
            "loadSnapshot",
            "snapshotHost",
            "collectReadableRuns",
          ]) {
            if (typeof SDK[method] !== "function")
              throw new Error(`Missing global: ${method}`);
          }
          const root = document.getElementById("qa");
          const fixture = (window.fixture = {
            root,
            opens: 0,
            lookupCalls: 0,
            timings: [],
            grades: [],
            snapshot,
          });
          const link = {
            linked: () => true,
            fetch: window.fetch.bind(window),
            signIn: async () => {},
            signOut() {},
          };
          const report = (event) => {
            if (event !== "card_opened") return;
            fixture.opens++;
            if (fixture.started != null)
              fixture.timings.push(performance.now() - fixture.started);
            fixture.started = null;
            fixture.armed = false;
          };
          fixture.instrument = (host) =>
            new Proxy(host, {
              get(target, property) {
                if (property === "report") return report;
                if (property === "lookup" || property === "lookupEntry")
                  return (...args) => {
                    fixture.lookupCalls++;
                    return target[property](...args);
                  };
                const value = Reflect.get(target, property, target);
                return typeof value === "function" ? value.bind(target) : value;
              },
            });
          fixture.newHost = () =>
            fixture.instrument(SDK.createAnkiHost({ apiBase, link }));
          fixture.host = fixture.newHost();
          fixture.install = (
            html,
            host = fixture.host,
            parse = false,
            replaceFirst = false,
          ) => {
            const previous = fixture.attachment;
            if (!replaceFirst) previous?.detach();
            root.innerHTML = html;
            if (replaceFirst) previous?.detach();
            fixture.attachment = SDK.attach({
              root,
              host,
              parse,
              trigger: "click",
              connected: true,
              options: { hideAutomatically: false, lookupDelay: 0 },
            });
            previous?.detach();
          };
          fixture.word =
            '<span class="sb-word" data-lemma="猫" data-surface="猫" data-entry="jmdict:1">猫</span>';
          fixture.trace = [];
          fixture.arm = () => {
            fixture.armed = true;
            fixture.started = null;
          };
          for (const type of ["click", "keydown", "pointerup"]) {
            window.addEventListener(
              type,
              (event) => {
                fixture.trace.push({
                  type,
                  target: event.target.id || event.target.className,
                  pointer: event.pointerType,
                  x: event.clientX,
                  y: event.clientY,
                });
                if (fixture.trace.length > 10) fixture.trace.shift();
                if (
                  !fixture.armed ||
                  fixture.started != null ||
                  !root.contains(event.target)
                )
                  return;
                if (type === "pointerup" && event.pointerType === "mouse")
                  return;
                fixture.started = performance.now();
              },
              true,
            );
            document.addEventListener(type, (event) => {
              if (type !== "keydown" || ["Enter", " ", "1"].includes(event.key))
                fixture.grades.push(type === "keydown" ? event.key : type);
            });
          }
        },
        { apiBase, snapshot },
      );
      const cdp = await context.newCDPSession(page);
      const detached = await retainedListeners(cdp);
      const open = async (selector = ".sb-word", key) => {
        const previous = await page.evaluate(() => {
          window.fixture.arm();
          return window.fixture.opens;
        });
        const word = page.locator(`#qa ${selector}`).first();
        if (key) {
          await word.focus();
          await word.press(key);
        } else if (mobile) await word.tap({ position: { x: 4, y: 14 } });
        else await word.click({ position: { x: 4, y: 14 } });
        await page.waitForFunction(
          (count) => window.fixture.opens === count + 1,
          previous,
        );
        assert.equal(
          await page
            .getByRole("dialog", { name: "Shirabe dictionary" })
            .isVisible(),
          true,
        );
      };
      const requestStart = requests.length;
      await page.evaluate(() => window.fixture.install(window.fixture.word));
      await open();
      const attached = await retainedListeners(cdp);
      const flipStart = performance.now();
      for (let flip = 1; flip < flips; flip++) {
        await page.evaluate(
          (flip) =>
            window.fixture.install(
              window.fixture.word,
              window.fixture.host,
              false,
              flip % 2 === 0,
            ),
          flip,
        );
        await open();
      }
      const flipMs = performance.now() - flipStart;
      const afterFlips = await retainedListeners(cdp);
      assert.deepEqual(
        afterFlips,
        attached,
        "Listeners grew over reused-root flips",
      );
      assert.equal(
        await page.evaluate(() => window.fixture.lookupCalls),
        flips,
      );
      assert.deepEqual(
        await page.evaluate(() => window.fixture.grades),
        [],
        "Lookup gestures reached reviewer grading",
      );
      const flipRequests = requests.slice(requestStart);
      assert.equal(
        dictionaryRequests(requestStart),
        2,
        JSON.stringify(requestCounts(requestStart)),
      );
      assert.ok(
        flipRequests.length <= flips + 2,
        "More than one state request per lookup",
      );
      assert.equal(
        flipRequests.filter((request) => request.path === "/api/v1/parse")
          .length,
        0,
      );
      await page.evaluate(() => {
        window.fixture.root.innerHTML =
          '<p id="question">[... hidden answer ...]</p>';
        window.fixture.attachment.detach();
      });
      const afterDetach = await retainedListeners(cdp);
      assert.deepEqual(
        afterDetach["document.getElementById('qa')"],
        detached["document.getElementById('qa')"],
      );
      assert.equal(
        await page
          .getByRole("dialog", { name: "Shirabe dictionary" })
          .isVisible(),
        false,
      );
      assert.equal(
        await page.locator("#question").textContent(),
        "[... hidden answer ...]",
      );

      const pending = new Promise((resolve) => {
        heldLookup = resolve;
      });
      await page.evaluate(() => {
        const f = window.fixture;
        f.install(f.word.replace("jmdict:1", "jmdict:held"), f.newHost());
      });
      const opensBeforeStale = await page.evaluate(() => window.fixture.opens);
      if (mobile) await page.locator("#qa .sb-word").tap();
      else await page.locator("#qa .sb-word").click();
      await pending;
      await page.evaluate(() => {
        window.fixture.root.innerHTML = "犬";
        window.fixture.attachment.detach();
      });
      const completed = page.waitForResponse((response) =>
        response.url().includes("entry=jmdict%3Aheld"),
      );
      releaseLookup();
      await (await completed).finished();
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          ),
      );
      assert.equal(
        await page.evaluate(() => window.fixture.opens),
        opensBeforeStale,
        "Old lookup rendered on the next side",
      );
      assert.equal(await page.locator("#qa").textContent(), "犬");
      assert.equal(
        await page
          .getByRole("dialog", { name: "Shirabe dictionary" })
          .isVisible(),
        false,
      );
      if (mobile) {
        await page.evaluate(() => {
          window.fixture.install(window.fixture.word);
          document.body.style.minHeight = "200vh";
        });
        const word = await page.locator("#qa .sb-word").boundingBox();
        const x = word.x + word.width / 2,
          y = word.y + word.height / 2;
        const beforeScroll = await page.evaluate(() => window.fixture.opens);
        const beforeScrollRequests = requests.length;
        await cdp.send("Input.dispatchTouchEvent", {
          type: "touchStart",
          touchPoints: [{ x, y }],
        });
        for (const distance of [15, 30, 45]) {
          await cdp.send("Input.dispatchTouchEvent", {
            type: "touchMove",
            touchPoints: [{ x, y: y - distance }],
          });
        }
        await cdp.send("Input.dispatchTouchEvent", {
          type: "touchEnd",
          touchPoints: [],
        });
        await page.waitForFunction(() => scrollY > 0);
        assert.equal(
          await page.evaluate(() => window.fixture.opens),
          beforeScroll,
          "Touch scrolling opened a dictionary card",
        );
        assert.equal(
          requests.length,
          beforeScrollRequests,
          "Touch scrolling requested a lookup",
        );
        await page.evaluate(() => {
          window.fixture.attachment.detach();
          document.body.style.minHeight = "";
          window.scrollTo(0, 0);
        });
      }

      const cold = [],
        warm = [];
      for (let sample = 0; sample < 12; sample++) {
        const before = requests.length;
        await page.evaluate(() => {
          window.fixture.host = window.fixture.newHost();
          window.fixture.install(window.fixture.word);
        });
        await open();
        cold.push(await page.evaluate(() => window.fixture.timings.at(-1)));
        assert.equal(
          dictionaryRequests(before),
          2,
          "Cold Anki host should fetch preferences and definition",
        );
        await page.evaluate(() => window.fixture.install(window.fixture.word));
        await open();
        warm.push(await page.evaluate(() => window.fixture.timings.at(-1)));
        assert.equal(
          dictionaryRequests(before),
          2,
          "Warm lookup refetched preferences or definition",
        );
      }
      await page.screenshot({
        path: join(artifacts, `${mode}.png`),
        animations: "disabled",
      });
      const dialog = page.getByRole("dialog", { name: "Shirabe dictionary" });
      const layout = await dialog.evaluate((element) => ({
        width: element.clientWidth,
        scroll: element.scrollWidth,
        right: element.getBoundingClientRect().right,
        viewport: innerWidth,
      }));
      assert.ok(
        layout.scroll <= layout.width && layout.right <= layout.viewport,
        JSON.stringify(layout),
      );
      const result = {
        mode,
        flips,
        flipWallMs: Math.round(flipMs),
        hostLookups: flips,
        flipRequests,
        requestCounts: Object.fromEntries(
          [...new Set(flipRequests.map((request) => request.path))].map(
            (path) => [
              path,
              flipRequests.filter((request) => request.path === path).length,
            ],
          ),
        ),
        retained: { attached, afterFlips, detached: afterDetach },
        lookupEventToRenderedCard: {
          cold: latencies(cold),
          warm: latencies(warm),
        },
      };
      report.modes.push(result);
      console.log(
        JSON.stringify({
          mode,
          flips,
          requests: result.requestCounts,
          cold: result.lookupEventToRenderedCard.cold,
          warm: result.lookupEventToRenderedCard.warm,
        }),
      );

      const beforeFade = requests.length;
      for (const parse of [false, true]) {
        await page.evaluate((parse) => {
          const f = window.fixture;
          f.attachment.detach();
          f.root.style.transition = "none";
          f.root.style.opacity = "0";
          getComputedStyle(f.root).opacity;
          f.root.style.transition = "opacity 100ms linear";
          f.install(
            "<p>猫</p><p hidden>答え</p><button>操作</button>",
            f.host,
            parse,
          );
        }, parse);
        assert.equal(
          await page
            .locator("#qa")
            .evaluate((root) => getComputedStyle(root).opacity),
          "0",
        );
        assert.equal(
          requests
            .slice(beforeFade)
            .filter((request) => request.path === "/api/v1/parse").length,
          0,
        );
        await page.evaluate(
          () =>
            new Promise((resolve) =>
              requestAnimationFrame(() => requestAnimationFrame(resolve)),
            ),
        );
        await page.locator("#qa").evaluate((root) => {
          root.style.opacity = "1";
        });
        await page.waitForFunction(
          () => getComputedStyle(document.getElementById("qa")).opacity === "1",
        );
        if (parse)
          await page.waitForFunction(
            () => document.querySelectorAll("#qa .sb-word").length === 1,
          );
        else assert.equal(await page.locator("#qa .sb-word").count(), 0);
      }
      await page.evaluate(() => {
        const root = window.fixture.root;
        for (let index = 0; index < 10; index++)
          root.dispatchEvent(
            new TransitionEvent("transitionend", { bubbles: true }),
          );
        root.style.opacity = "1";
      });
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          ),
      );
      const fadeRequests = requests
        .slice(beforeFade)
        .filter((request) => request.path === "/api/v1/parse");
      assert.equal(
        fadeRequests.length,
        1,
        "Fade-in should parse once, only with consent",
      );
      assert.equal(
        fadeRequests[0].body.text,
        "猫",
        "Fade-in uploaded hidden answers or controls",
      );
      await page.evaluate(() => {
        const f = window.fixture;
        f.attachment.detach();
        f.root.style.transition = "none";
        f.root.style.opacity = "0";
        f.install("<p>猫</p>", f.host, true);
        f.root.innerHTML = "<p>犬</p>";
        f.attachment.detach();
        f.root.style.opacity = "1";
        f.root.dispatchEvent(
          new TransitionEvent("transitionend", { bubbles: true }),
        );
      });
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          ),
      );
      assert.equal(
        requests
          .slice(beforeFade)
          .filter((request) => request.path === "/api/v1/parse").length,
        1,
      );
      assert.equal(await page.locator("#qa").textContent(), "犬");
      result.fadeIn = {
        parseRequests: fadeRequests.length,
        texts: fadeRequests.map((request) => request.body.text),
        detachedReadCancelled: true,
      };

      await page.evaluate(() => {
        window.fixture.install(
          '<p id="raw">猫</p><button id="template-button">猫</button>',
        );
        window.fixture.templateClicks = 0;
        document
          .getElementById("template-button")
          .addEventListener("click", () => {
            window.fixture.templateClicks++;
          });
      });
      const beforeRaw = await page.evaluate(() => window.fixture.opens);
      await page.locator("#raw").hover();
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          ),
      );
      assert.equal(
        await page.evaluate(() => window.fixture.opens),
        beforeRaw,
        "Click trigger opened on hover",
      );
      await open("#raw");
      await dialog.focus();
      await dialog.press("Escape");
      await dialog.waitFor({ state: "hidden" });
      const beforeControl = await page.evaluate(
        () => window.fixture.lookupCalls,
      );
      result.dismissedHitTest = await page
        .locator("#template-button")
        .evaluate((button) => {
          const rect = button.getBoundingClientRect();
          const hit = document.elementFromPoint(
            rect.x + rect.width / 2,
            rect.y + rect.height / 2,
          );
          const host = document.querySelector(".sb-host");
          return {
            templateButton: hit === button,
            interceptedBy: hit?.id || hit?.className,
            hostPointerEvents: getComputedStyle(host).pointerEvents,
            popupVisibility: getComputedStyle(
              host.shadowRoot.querySelector(".sb-popup"),
            ).visibility,
          };
        });
      try {
        assert.equal(
          result.dismissedHitTest.templateButton,
          true,
          JSON.stringify(result.dismissedHitTest),
        );
        if (mobile)
          await page.locator("#template-button").tap({ timeout: 1000 });
        else await page.locator("#template-button").click({ timeout: 1000 });
        assert.equal(
          await page.evaluate(() => window.fixture.templateClicks),
          1,
          "Template control did not receive the gesture",
        );
      } catch (error) {
        report.failures.push({
          mode,
          check: "Dismissed popup must not intercept template controls",
          error: error.message,
        });
        await page.screenshot({
          path: join(artifacts, `${mode}-dismiss-failure.png`),
          animations: "disabled",
        });
        if (probeHiddenHost) {
          await page.evaluate(() => {
            const host = document.querySelector(".sb-host");
            host.style.pointerEvents = "none";
            host.shadowRoot.querySelector(".sb-popup").style.pointerEvents =
              "auto";
          });
          await page.locator("#template-button").click();
          result.rendererProbe =
            "Browser-only hit-test correction; production source unchanged and failure retained";
        }
      }
      assert.equal(
        await page.evaluate(() => window.fixture.lookupCalls),
        beforeControl,
      );
      await page.evaluate(() => {
        window.fixture.grades.length = 0;
        window.fixture.install(window.fixture.word);
      });
      await open(".sb-word", "Enter");
      await page.keyboard.press("Escape");
      await dialog.waitFor({ state: "hidden" });
      await open(".sb-word", "Space");
      assert.deepEqual(await page.evaluate(() => window.fixture.grades), []);
      await page.keyboard.press("Escape");
      await page.locator("#grade").focus();
      await page.keyboard.press("1");
      assert.deepEqual(await page.evaluate(() => window.fixture.grades), ["1"]);

      const selectionRequests = requests.length;
      for (const key of [null, "Enter", "Space"]) {
        const before = await page.evaluate(() => {
          const f = window.fixture;
          f.install(
            "<p><ruby>勉<rt>べん</rt></ruby><em>強する</em>。<span hidden>秘密</span></p>",
          );
          f.grades.length = 0;
          const range = document.createRange();
          range.setStart(f.root.querySelector("ruby").firstChild, 0);
          range.setEnd(f.root.querySelector("em").firstChild, 3);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          document.dispatchEvent(new Event("selectionchange"));
          return {
            opens: f.opens,
            calls: f.lookupCalls,
            selection: getSelection().toString(),
          };
        });
        const action = page.getByRole("button", {
          name: "Look up selection",
          exact: true,
        });
        await action.waitFor({ state: "visible" });
        assert.equal(
          await page.evaluate(() => window.fixture.lookupCalls),
          before.calls,
          "Selection alone triggered lookup",
        );
        const bounds = await action.boundingBox();
        assert.ok(
          bounds.x >= 0 &&
            bounds.y >= 0 &&
            bounds.x + bounds.width <= (mobile ? 390 : 1100),
        );
        if (!key)
          await page.screenshot({
            path: join(artifacts, `${mode}-selection.png`),
            animations: "disabled",
          });
        if (key) {
          await action.focus();
          assert.equal(
            await page.evaluate(() => getSelection().toString()),
            before.selection,
            "Focusing action changed selection",
          );
          await action.press(key);
        } else if (mobile) await action.tap();
        else await action.click();
        await page.waitForFunction(
          (count) => window.fixture.opens === count + 1,
          before.opens,
        );
        assert.equal(
          await page.evaluate(() => window.fixture.lookupCalls),
          before.calls + 1,
        );
        assert.equal(
          await page.evaluate(() => getSelection().toString()),
          before.selection,
        );
        assert.deepEqual(
          await page.evaluate(() => window.fixture.grades),
          [],
          "Selection action reached reviewer handlers",
        );
        await page.keyboard.press("Escape");
        await page.evaluate(() => getSelection().removeAllRanges());
      }
      const selectedRequests = requests.slice(selectionRequests);
      assert.equal(
        selectedRequests.filter((request) => request.path === "/api/v1/parse")
          .length,
        0,
        "Explicit selection was retokenized",
      );
      const selectedTokens = selectedRequests
        .filter((request) => request.path === "/api/v1/words/identify")
        .flatMap((request) => request.body.tokens);
      assert.ok(selectedTokens.length > 0);
      assert.ok(
        selectedTokens.every(
          (token) => token.surface === "勉強する" && token.lemma === "勉強する",
        ),
        JSON.stringify(selectedTokens),
      );
      result.selection = {
        activations: 3,
        parseRequests: 0,
        tokens: selectedTokens,
        selectionPreserved: true,
        reviewerEvents: 0,
      };

      const offlineRequestStart = requests.length;
      const collected = await page.evaluate(
        ({ apiBase }) => {
          const f = window.fixture,
            SDK = window.ShirabeCard;
          f.attachment.detach();
          f.root.innerHTML =
            '<p>😀<ruby>猫<rt>ねこ</rt></ruby>は<span class="cloze">猫</span></p><p hidden>犬</p><p style="visibility:hidden">秘密</p>';
          const before = f.root.innerHTML;
          const texts = SDK.collectReadableRuns(f.root);
          if (f.root.innerHTML !== before)
            throw new Error("Collector mutated the reviewer DOM");
          const host = f.instrument(
            SDK.snapshotHost(undefined, f.snapshot, {
              contentVersion: f.snapshot.contentVersion,
              apiBase,
              texts,
            }),
          );
          f.attachment = SDK.attach({
            root: f.root,
            host,
            trigger: "click",
            parse: true,
            options: { hideAutomatically: false },
          });
          return texts;
        },
        { apiBase },
      );
      assert.deepEqual(collected, ["😀猫は猫"]);
      await page.waitForFunction(
        () => document.querySelectorAll("#qa .sb-word").length === 2,
      );
      await open("ruby .sb-word");
      assert.equal(await page.locator("#qa rt").textContent(), "ねこ");
      assert.equal(
        requests.length,
        offlineRequestStart,
        "Prepared lookup reached the network",
      );
      await page.screenshot({
        path: join(artifacts, `${mode}-offline.png`),
        animations: "disabled",
      });
      const mixed = await page.evaluate(
        ({ snapshot, apiBase }) => {
          const f = window.fixture,
            SDK = window.ShirabeCard;
          f.attachment.detach();
          f.root.innerHTML = "<p><ruby>勉<rt>べん</rt></ruby><em>強</em></p>";
          const texts = SDK.collectReadableRuns(f.root);
          f.originalEm = f.root.querySelector("em");
          const host = f.instrument(
            SDK.snapshotHost(undefined, snapshot, {
              contentVersion: snapshot.contentVersion,
              apiBase,
              texts,
            }),
          );
          f.attachment = SDK.attach({
            root: f.root,
            host,
            trigger: "click",
            parse: true,
            options: { hideAutomatically: false },
          });
          return texts;
        },
        { snapshot: mixedSnapshot, apiBase },
      );
      assert.deepEqual(mixed, ["勉強"]);
      await page.waitForFunction(
        () => document.querySelectorAll("#qa .sb-word").length === 2,
      );
      const fragments = await page
        .locator("#qa .sb-word")
        .evaluateAll((words) =>
          words.map((word) => ({
            text: word.textContent,
            surface: word.dataset.surface,
            lemma: word.dataset.lemma,
            entry: word.dataset.entry,
          })),
        );
      assert.deepEqual(
        fragments,
        ["勉", "強"].map((text) => ({
          text,
          surface: "勉強",
          lemma: "勉強",
          entry: "jmdict:2",
        })),
      );
      assert.equal(
        await page.evaluate(
          () =>
            window.fixture.originalEm ===
            window.fixture.root.querySelector("em"),
        ),
        true,
      );
      await open("em .sb-word");
      assert.equal(
        await dialog
          .locator(".sb-headword")
          .first()
          .evaluate((element) => {
            const copy = element.cloneNode(true);
            copy
              .querySelectorAll("rt,rp")
              .forEach((reading) => reading.remove());
            return copy.textContent;
          }),
        "勉強",
      );
      await page.evaluate(() => window.ShirabeCard.dismiss());
      await open("ruby .sb-word");
      assert.equal(
        await dialog
          .locator(".sb-headword")
          .first()
          .evaluate((element) => {
            const copy = element.cloneNode(true);
            copy
              .querySelectorAll("rt,rp")
              .forEach((reading) => reading.remove());
            return copy.textContent;
          }),
        "勉強",
      );
      assert.equal(
        requests.length,
        offlineRequestStart,
        "Mixed inline snapshot reached the network",
      );
      await page.evaluate(() => window.fixture.attachment.detach());
      assert.deepEqual(
        await retainedListeners(cdp),
        afterDetach,
        "Detach left additional listeners behind",
      );
      const beforeBootstrap = snapshotLoads;
      await page.evaluate(
        ({ apiBase, snapshotName, mobile }) => {
          window.fixture.root.innerHTML =
            '<p>😀<ruby>猫<rt>ねこ</rt></ruby>は<span class="cloze">猫</span></p><p hidden>犬</p>';
          window.__shirabe = {
            apiBase,
            clientId: "browser-fixture",
            native: !mobile,
            onlineParsing: false,
            theme: "light",
          };
          if (mobile) {
            const marker = document.createElement("span");
            marker.hidden = true;
            marker.dataset.shirabeSnapshot = "";
            marker.textContent = snapshotName;
            window.fixture.root.append(marker);
          } else window.__shirabe.snapshot = snapshotName;
        },
        { apiBase, snapshotName, mobile },
      );
      await page.addScriptTag({ content: bootstrap });
      await page.waitForFunction(
        () => document.querySelectorAll("#qa .sb-word").length === 2,
      );
      const preparedWord = page.locator("#qa ruby .sb-word");
      if (mobile) await preparedWord.tap();
      else await preparedWord.click();
      await page.waitForFunction(() =>
        document
          .querySelector(".sb-host")
          ?.shadowRoot?.querySelector(".sb-visible .sb-headword")
          ?.textContent.includes("猫"),
      );
      assert.equal(
        snapshotLoads - beforeBootstrap,
        1,
        "Shared bootstrap did not load its hashed snapshot",
      );
      assert.equal(
        requests.length,
        offlineRequestStart,
        "Shared offline bootstrap uploaded text or requested an online definition",
      );
      await page.evaluate(() => {
        window.fixture.root.textContent = "[question]";
        window.ShirabeAnki.detach();
      });
      assert.equal(await page.locator("#qa").textContent(), "[question]");
      assert.equal(await dialog.isVisible(), false);
      assert.deepEqual(
        await retainedListeners(cdp),
        afterDetach,
        "Shared bootstrap teardown retained root listeners",
      );
      assert.deepEqual(errors, []);
      result.offlineRequests = requests.length - offlineRequestStart;
      result.checks = [
        "actual click/tap",
        "trigger",
        "replaced-root teardown",
        "duplicate handlers",
        "stale HTTP response after side replacement",
        "keyboard",
        "review keys",
        "visible ruby/cloze collector",
        "cross-inline token fragments",
        "Python offline snapshot",
        "actual shared Anki bootstrap with hashed snapshot",
        "popup overflow",
      ];
      if (mobile)
        result.checks.push(
          "native browser touch scrolling does not request a lookup",
        );
      const memoryFlips = async (count) => {
        for (let flip = 0; flip < count; flip++) {
          await page.evaluate(
            (flip) =>
              window.fixture.install(
                window.fixture.word,
                window.fixture.host,
                false,
                flip % 2 === 0,
              ),
            flip,
          );
          await open();
        }
      };
      await memoryFlips(10);
      const memoryBaseline = await retainedMemory(cdp, page);
      const memorySamples = [];
      for (
        let completedFlips = 100;
        completedFlips <= memoryFlipCount;
        completedFlips += 100
      ) {
        await memoryFlips(100);
        const sample = await retainedMemory(cdp, page);
        memorySamples.push({
          completedFlips,
          ...sample,
          jsHeapGrowthBytes: sample.jsHeapBytes - memoryBaseline.jsHeapBytes,
          domNodeGrowth: sample.domNodes - memoryBaseline.domNodes,
          listenerGrowth:
            sample.jsEventListeners - memoryBaseline.jsEventListeners,
        });
      }
      result.retainedMemory = {
        method:
          "10 warm-up flips; detach, clear harness logs, settle two frames, force GC before each sample",
        scope:
          "Isolated Chromium page heap and DOM counters; not process RSS, Anki memory, or proof of leak attribution",
        warmupFlips: 10,
        baseline: memoryBaseline,
        samples: memorySamples,
        settled: await settledMemory(cdp, page, memoryBaseline),
      };
      const controlFlips = async (count) => {
        for (let flip = 0; flip < count; flip++) {
          await page.evaluate(() => {
            const f = window.fixture;
            f.root.innerHTML = '<button id="memory-control">猫</button>';
            f.root.firstElementChild.addEventListener("click", async () => {
              const response = await fetch("/api/v1/srs/states/lookup", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ words: ["猫"] }),
              });
              await response.json();
              f.controlClicks = (f.controlClicks ?? 0) + 1;
            });
          });
          const previous = await page.evaluate(
            () => window.fixture.controlClicks ?? 0,
          );
          const control = page.locator("#memory-control");
          if (mobile) await control.tap();
          else await control.click();
          await page.waitForFunction(
            (count) => window.fixture.controlClicks === count + 1,
            previous,
          );
          assert.equal(await control.isVisible(), true);
        }
      };
      await controlFlips(10);
      const controlBaseline = await retainedMemory(cdp, page);
      const controlSamples = [];
      for (
        let completedFlips = 100;
        completedFlips <= memoryFlipCount;
        completedFlips += 100
      ) {
        await controlFlips(100);
        const sample = await retainedMemory(cdp, page);
        controlSamples.push({
          completedFlips,
          ...sample,
          jsHeapGrowthBytes: sample.jsHeapBytes - controlBaseline.jsHeapBytes,
        });
      }
      result.retainedMemory.noSdkControl = {
        method:
          "Replace and click/tap an ordinary button plus one equivalent local HTTP read, without SDK attach or lookup",
        baseline: controlBaseline,
        samples: controlSamples,
        settled: await settledMemory(cdp, page, controlBaseline),
      };
      assert.deepEqual(
        await retainedListeners(cdp),
        afterDetach,
        "Memory probe retained attachment listeners",
      );
      assert.deepEqual(errors, []);
      console.log(
        JSON.stringify({ mode, retainedMemory: result.retainedMemory }),
      );
    } catch (error) {
      const state = await page.evaluate(() => ({
        opens: window.fixture?.opens,
        lookups: window.fixture?.lookupCalls,
        selected: getSelection()?.toString(),
        trace: window.fixture?.trace,
      }));
      report.failures.push({
        mode,
        error: String(error.stack ?? error),
        state,
      });
      await page.screenshot({
        path: join(artifacts, `${mode}-failure.png`),
        animations: "disabled",
      });
    } finally {
      releaseLookup?.();
      await context.close();
    }
  }
  assertPerformanceBudgets(report);
  report.performanceBudgetsPassed = true;
  if (report.failures.length) {
    throw new Error(
      report.failures
        .map(
          (failure) =>
            `${failure.mode}: ${failure.check ?? failure.error.split("\n")[0]}`,
        )
        .join("\n"),
    );
  }
} catch (error) {
  report.failure = String(error.stack ?? error);
  if (currentPage && !currentPage.isClosed())
    await currentPage.screenshot({
      path: join(artifacts, "failure.png"),
      animations: "disabled",
    });
  throw error;
} finally {
  writeFileSync(
    join(artifacts, "report.json"),
    JSON.stringify(report, null, 2),
  );
  console.log(`Attachment browser report: ${artifacts}`);
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
