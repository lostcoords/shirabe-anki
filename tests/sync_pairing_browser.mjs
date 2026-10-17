import { pathToFileURL } from "node:url";
import { resolve as resolveCheckout } from "node:path";
const shirabe = resolveCheckout(process.env.SHIRABE_PATH ?? new URL("../../shirabe", import.meta.url).pathname);
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { expect } from "@playwright/test";
import { browserEngines, launchBrowser } from "../packages/template-runtime/test/browsers.mjs";

const input = [];
for await (const chunk of process.stdin) input.push(chunk);
const config = JSON.parse(Buffer.concat(input).toString());
assert.equal(new URL(config.origin).hostname, "127.0.0.1");
const directory = path.resolve(config.directory);
assert(directory.startsWith(`${path.resolve("tmp")}${path.sep}anki-http-roundtrip-`));
const state = path.join(directory, "browser-state.json");
const browser = await launchBrowser(browserEngines[0]);
let page;
const errors = [];
const renderingErrors = [];
try {
  const context = await browser.newContext(config.action === "approve" ? {} : { storageState: JSON.parse(await readFile(state, "utf8")) });
  await context.addInitScript(() => {
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function(...args) {
      this.muted = true;
      return play.apply(this, args);
    };
  });
  page = await context.newPage();
  page.setDefaultTimeout(20000);
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  page.on("response", response => {
    const code = response.headers()["x-study-rendering-error"];
    if (code) {
      const url = new URL(response.url());
      renderingErrors.push({ code, path: url.pathname, revision: url.searchParams.get("revision") });
    }
  });
  if (config.action === "approve") {
    assert.equal(new URL(config.approval).origin, config.origin);
    assert.equal(new URL(config.login).origin, config.origin);
    await page.goto(config.approval);
    await expect(page).toHaveURL(/session/);
    await page.goto(config.login);
    await page.locator('form[action*="magic_link/confirm"] button').click();
    await expect(page.getByRole("heading", { name: "Connect a device", exact: true })).toBeVisible();
    await expect(page.getByText(config.code, { exact: true })).toBeVisible();
    await expect(page.getByText("Shirabe Anki sync", { exact: true })).toBeVisible();
    const consent = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === "/en/link");
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    assert.equal((await consent).status(), 200);
    await expect(page.getByRole("heading", { name: "You're connected", exact: true })).toBeVisible();
    await context.storageState({ path: state });
  } else if (config.action === "review") {
    await page.goto(`${config.origin}/en/study`);
    await expect(page.getByText("Japanese automatic sync", { exact: true })).toBeVisible();
    await page.goto(`${config.origin}/en/study/review`);
    await page.getByRole("button", { name: /^Show answer/ }).click();
    const response = page.waitForResponse(response => response.request().method() === "POST" && response.headers()["x-study-review-id"]);
    await page.locator('[data-review-target="grades"] button[value="3"]').click();
    const reviewed = await response;
    assert.equal(reviewed.status(), 200);
    assert(reviewed.headers()["x-study-review-id"]);
  } else if (config.action === "mixed_review") {
    await page.goto(`${config.origin}/en/study/review`);
    const seen = new Set();
    for (let index = 0; index < config.cards; index++) {
      const active = page.locator("[data-review-card-id-value]");
      await expect(active).toHaveCount(1);
      const cardId = await active.getAttribute("data-review-card-id-value");
      assert(!seen.has(cardId), `Card ${cardId} appeared twice before the sample finished`);
      await page.getByRole("button", { name: /^Show answer/ }).click();
      const response = page.waitForResponse(response => response.request().method() === "POST" && response.headers()["x-study-review-id"]);
      await page.locator('[data-review-target="grades"] button[value="4"]').click();
      const reviewed = await response;
      assert.equal(reviewed.status(), 200);
      seen.add(cardId);
      await expect(page.locator(`[data-review-card-id-value="${cardId}"]`)).toHaveCount(0);
    }
    assert.equal(seen.size, config.cards);
    await expect(page.locator("[data-review-card-id-value]")).toHaveCount(0);
  } else {
    throw new Error(`Unknown browser pairing action: ${config.action}`);
  }
  await page.screenshot({ path: path.join(directory, `pairing-${config.action}.png`) });
  console.log(JSON.stringify({ stage: config.action, passed: true }));
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(directory, `pairing-${config.action}-failed.png`) });
    console.error(JSON.stringify({ action: config.action, path: new URL(page.url()).pathname, errors, renderingErrors,
      headings: await page.getByRole("heading").allTextContents() }));
  }
  throw error;
} finally {
  await browser.close();
}
