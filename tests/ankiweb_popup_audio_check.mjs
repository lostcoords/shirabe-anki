#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const manifest = JSON.parse(readFileSync(process.argv[2], "utf8"));
const recording = manifest.prepared_recording;
if (!manifest.deck.startsWith("Shirabe Test ") ||
    !Number.isSafeInteger(manifest.prepared_audio_note_id) ||
    !manifest.notes.some(note => note.id === manifest.prepared_audio_note_id && note.fields[0] === "猫がいます。") ||
    !/^[a-f0-9]{64}$/.test(manifest.sdk_sha256) ||
    !/^shirabe_[a-f0-9]{64}\.(m4a|mp3)$/.test(recording?.filename) ||
    manifest.media[recording.filename]?.sha256 !== recording.sha256) {
  throw new Error("Expected the prepared pronunciation fixture.");
}

async function verify(page, { noteId, filename, sdkHash, mimeType }) {
  const report = { ok: false, checks: {}, requests: { pronunciation: 0, shirabe: 0 } };
  const guard = async answer => {
    const showAnswer = page.getByRole("button", { name: "Show Answer", exact: true });
    if (page.url() !== "https://ankiuser.net/study" ||
        await page.getByRole("link", { name: "Edit", exact: true }).getAttribute("href") !== `/edit/${noteId}` ||
        await showAnswer.isVisible() === answer) {
      throw new Error(`Open the temporary cat card's ${answer ? "answer" : "question"}; no cards will be graded.`);
    }
  };
  await guard(false);
  const check = (name, passed) => {
    report.checks[name] = !!passed;
    if (!passed) throw new Error(`Failed: ${name}`);
  };
  const onRequest = request => {
    const url = request.url();
    if (url.split("?")[0].endsWith(`/${filename}`)) report.requests.pronunciation++;
    if (/^https:\/\/(?:[^/]+\.)?shirabe\.org(?:\/|$)/.test(url)) report.requests.shirabe++;
  };
  const routePattern = `**/${filename}`;
  let interception;
  page.on("request", onRequest);
  try {
    await page.reload();
    await guard(false);
    await page.getByRole("button", { name: "Show Answer", exact: true }).evaluate(button => button.click());
    await guard(true);
    const word = page.getByRole("button", { name: "猫", exact: true });
    await word.waitFor({ state: "visible", timeout: 10000 });
    check("frozen_bundle_loaded", await page.evaluate(hash => Array.from(document.scripts)
      .some(script => script.src.includes(`_shirabe_${hash.slice(0, 20)}_shirabe-card.js`)), sdkHash));
    await page.evaluate(expectedMime => {
      const OriginalAudio = window.Audio;
      const probe = { events: [], instances: [], original: OriginalAudio };
      probe.observer = new Proxy(OriginalAudio, {
        construct(target, args) {
          const audio = new target(...args);
          const id = probe.instances.push(audio) - 1;
          probe.events.push({ id, type: "created", expectedMime: audio.src.startsWith(`data:${expectedMime};base64,`) });
          for (const type of ["playing", "ended", "pause", "error"]) {
            audio.addEventListener(type, () => probe.events.push({ id, type,
              currentTime: audio.currentTime, duration: audio.duration, error: audio.error?.code ?? null }));
          }
          return audio;
        },
      });
      window.__shirabeAudioProbe = probe;
      window.Audio = probe.observer;
    }, mimeType);
    check("no_audio_before_lookup", report.requests.pronunciation === 0);
    check("word_is_visible", await word.isVisible());
    await word.click();
    const popup = page.getByRole("dialog", { name: "Shirabe dictionary" });
    const play = popup.getByRole("button", { name: "Play saved pronunciation", exact: true });
    await play.waitFor({ state: "visible", timeout: 10000 });
    check("saved_definition_visible", /ねこ/.test(await popup.innerText()) && /cat/.test(await popup.innerText()));
    await popup.getByText("Source details", { exact: true }).click();
    const sourceDetails = await popup.locator(".sb-example-source-details[open]").evaluate(details => ({
      text: details.innerText,
      links: [...details.querySelectorAll(".sb-example-reference-link")].map(link => ({
        text: link.textContent, href: link.href, target: link.target, rel: link.rel,
      })),
    }));
    check("source_details_visible", sourceDetails.text.includes("Sources saved with this card:")
      && !sourceDetails.text.includes("https://")
      && JSON.stringify(sourceDetails.links) === JSON.stringify([
        { text: "Tatoeba sentence 10098762", href: "https://tatoeba.org/en/sentences/show/10098762", target: "_blank", rel: "noopener noreferrer" },
        { text: "Tatoeba sentence 9032775", href: "https://tatoeba.org/en/sentences/show/9032775", target: "_blank", rel: "noopener noreferrer" },
        { text: "Tatoeba", href: "https://tatoeba.org/", target: "_blank", rel: "noopener noreferrer" },
        { text: "CC BY 2.0 FR", href: "https://creativecommons.org/licenses/by/2.0/fr/", target: "_blank", rel: "noopener noreferrer" },
      ]));
    await guard(true);
    check("no_audio_before_play", report.requests.pronunciation === 0);
    for (const [label, status, body] of [["missing", 404, "Not found"], ["corrupt", 200, "Invalid recording"]]) {
      interception = route => route.fulfill({ status, contentType: mimeType, body, headers: { "cache-control": "no-store" } });
      await page.route(routePattern, interception);
      await guard(true);
      await play.click();
      await popup.getByText("Couldn't load saved pronunciation. Sync media or prepare this card again.", { exact: true })
        .waitFor({ state: "visible", timeout: 8000 });
      check(`${label}_media_keeps_definition`, /cat/.test(await popup.innerText()));
      check(`${label}_media_can_retry`, await play.isEnabled());
      check(`${label}_media_never_plays`, await page.evaluate(() => window.__shirabeAudioProbe.instances.length === 0));
      await guard(true);
      await page.unroute(routePattern, interception);
      interception = undefined;
    }
    const ended = count => page.waitForFunction(n => window.__shirabeAudioProbe.events
      .filter(event => event.type === "ended").length >= n, count, { timeout: 8000 });
    await play.click();
    await ended(1);
    check("retry_clears_error", !await popup.getByText("Couldn't load saved pronunciation. Sync media or prepare this card again.", { exact: true }).isVisible());
    await guard(true);
    const requestsAfterFirstPlay = report.requests.pronunciation;
    await play.click();
    await ended(2);
    check("replay_uses_verified_cache", report.requests.pronunciation === requestsAfterFirstPlay);
    await guard(true);
    await play.click();
    await page.waitForFunction(() => window.__shirabeAudioProbe.events
      .some(event => event.id === 2 && event.type === "playing"), null, { timeout: 8000 });
    await popup.getByRole("button", { name: "Close", exact: true }).click();
    check("close_stops_and_unloads", await page.evaluate(() => {
      const probe = window.__shirabeAudioProbe, audio = probe.instances[2];
      return audio.paused && !audio.getAttribute("src") && !probe.events.some(event => event.id === 2 && event.type === "ended");
    }));
    await guard(true);
    report.events = await page.evaluate(() => window.__shirabeAudioProbe.events);
    check("real_playback_completed_twice", report.events.filter(event => event.type === "ended")
      .filter(event => event.error === null && event.duration > 0 && event.currentTime >= event.duration).length === 2);
    check("verified_data_url_used", report.events.filter(event => event.type === "created").every(event => event.expectedMime));
    check("no_playback_errors", !report.events.some(event => event.type === "error"));
    check("only_three_media_requests", report.requests.pronunciation === 3);
    check("no_shirabe_requests", report.requests.shirabe === 0);
    report.sameCard = true;
    report.ok = true;
  } catch (error) {
    report.error = error.message;
  } finally {
    if (interception) await page.unroute(routePattern, interception);
    page.off("request", onRequest);
    await page.evaluate(() => {
      const probe = window.__shirabeAudioProbe;
      if (!probe) return;
      for (const audio of probe.instances) audio.pause();
      if (window.Audio === probe.observer) window.Audio = probe.original;
      delete window.__shirabeAudioProbe;
    });
  }
  return report;
}

const args = { noteId: manifest.prepared_audio_note_id, filename: recording.filename,
  sdkHash: manifest.sdk_sha256, mimeType: recording.mimeType };
const code = `async (page) => await (${verify.toString()})(page, ${JSON.stringify(args)})`;
const output = execFileSync(join(homedir(), "bin/playwright-cli"), ["run-code", code, "--raw"], {
  encoding: "utf8", timeout: 60000, maxBuffer: 1024 * 1024,
});
process.stdout.write(output);
const report = JSON.parse(output);
if (process.argv[3]) writeFileSync(process.argv[3], JSON.stringify(report, null, 2) + "\n", { flag: "wx" });
process.exitCode = report.ok ? 0 : 1;
