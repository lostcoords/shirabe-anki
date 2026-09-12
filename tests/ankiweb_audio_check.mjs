#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const manifest = JSON.parse(readFileSync(process.argv[2], "utf8"));
const filenames = [manifest.mp3, manifest.audio];
if (!manifest.deck.startsWith("Shirabe Test ") || !Number.isSafeInteger(manifest.audio_note_id) ||
    !manifest.notes.some(n => n.id === manifest.audio_note_id) ||
    filenames.some(name => !/^shirabe_[a-f0-9]{64}\.(mp3|m4a)$/.test(name))) {
  throw new Error("Expected the isolated audio comparison fixture.");
}

async function verify(page, { noteId, filenames }) {
  if (page.url() !== "https://ankiuser.net/study") {
    throw new Error("Open the audio comparison answer in the temporary deck first.");
  }
  const edit = page.getByRole("link", { name: "Edit", exact: true });
  const guard = async () => {
    if (await edit.getAttribute("href") !== `/edit/${noteId}`) throw new Error("The current card changed.");
  };
  await guard();
  const players = await page.locator("#qa audio").evaluateAll(nodes => nodes.map(a => ({
    filename: new URL(a.getAttribute("src") || a.querySelector("source")?.getAttribute("src"), document.baseURI).pathname.split("/").pop(),
    controls: a.controls, width: a.clientWidth, height: a.clientHeight,
  })));
  if (players.length !== 2 || players.some((p, i) => p.filename !== filenames[i] || !p.controls || p.width < 250 || p.height < 40)) {
    throw new Error("The expected native audio controls are not displayed.");
  }
  const results = [];
  for (let index = 0; index < 2; index++) {
    await guard();
    const player = page.locator("#qa audio").nth(index);
    await player.click({ position: { x: 25, y: 27 }, timeout: 5000 });
    await page.waitForFunction(i => {
      const audio = document.querySelectorAll("#qa audio")[i];
      return audio && (audio.ended || audio.error);
    }, index, { timeout: 8000 });
    const result = await player.evaluate(a => ({
      filename: a.currentSrc.split("/").pop(), duration: a.duration,
      currentTime: a.currentTime, ended: a.ended, error: a.error?.code ?? null,
    }));
    results.push(result);
    await guard();
  }
  return { ok: results.every((r, i) => r.filename === filenames[i] && r.ended &&
    r.error === null && r.duration > 0 && r.currentTime >= r.duration), sameCard: true, results };
}

const code = `async (page) => await (${verify.toString()})(page, ${JSON.stringify({ noteId: manifest.audio_note_id, filenames })})`;
const output = execFileSync(join(homedir(), "bin/playwright-cli"), ["run-code", code, "--raw"], {
  encoding: "utf8", timeout: 30000, maxBuffer: 1024 * 1024,
});
process.stdout.write(output);
const report = JSON.parse(output);
if (process.argv[3]) writeFileSync(process.argv[3], JSON.stringify(report, null, 2) + "\n", { flag: "wx" });
process.exitCode = report.ok ? 0 : 1;
