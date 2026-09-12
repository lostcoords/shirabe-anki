import assert from "node:assert/strict";
import { readFile, mkdtemp, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium, expect } from "@playwright/test";

const setup = await readFile(process.argv[2], "utf8");
const login = setup.split("\n").find(line => line.startsWith("STUDY_TRIAL_LOGIN_URL="))?.split("=").slice(1).join("=");
assert(login, "The development setup log must contain a normal login link");
const origin = new URL(login).origin;
const deckNames = setup.split("\n").find(line => line.startsWith("STUDY_TRIAL_DECK_NAMES_JSON="))?.slice("STUDY_TRIAL_DECK_NAMES_JSON=".length);
const deckName = deckNames ? JSON.parse(deckNames)[0] : "Mixed-card trial";
assert(["localhost", "127.0.0.1", "shirabe.localhost"].includes(new URL(origin).hostname));
const directory = await mkdtemp(path.resolve("tmp/study-development-trial-"));
const browser = await chromium.launch({ headless: true, args: ["--mute-audio", "--disable-audio-output"] });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(login);
  await page.locator('form[action*="magic_link/confirm"] button').click();
  await page.waitForURL(url => !url.pathname.includes("magic_link"));
  await page.goto(`${origin}/en/study`);
  await expect(page.getByText(deckName, { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(directory, "dashboard.png") });
  await page.goto(`${origin}/en/study/review`);
  await expect(page.getByRole("button", { name: /^Show answer/ })).toBeEnabled({ timeout: 20000 });
  await page.getByRole("button", { name: /^Show answer/ }).click();
  await expect(page.locator('[data-review-target="grades"] button[value="3"]')).toBeEnabled({ timeout: 20000 });
  await page.screenshot({ path: path.join(directory, "answer.png") });
  let reviewId = null;
  if (process.argv.includes("--review")) {
    assert(deckNames, "Only the retained linked trial may be graded by this probe");
    const response = page.waitForResponse(response => response.request().method() === "POST" && response.headers()["x-study-review-id"]);
    await page.locator('[data-review-target="grades"] button[value="3"]').click();
    const reviewed = await response;
    assert.equal(reviewed.status(), 200);
    reviewId = reviewed.headers()["x-study-review-id"];
    assert(reviewId);
  }
  await writeFile(path.join(directory, "results.json"), JSON.stringify({ origin, login: "passed", dashboard: "passed", reveal: "passed", grading_available: true, review_id: reviewId }, null, 2));
  console.log(`Development trial artifacts: ${directory}`);
} finally {
  await browser.close();
}
