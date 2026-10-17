import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import test from "node:test"
import { browserEngines, launchBrowser } from "./browsers.mjs"

for (const engine of browserEngines) {
  test(`${engine.name()}: speech playback is local, ordered and cancellable without speaking during tests`, async () => {
    const browser = await launchBrowser(engine)
    try {
      const page = await browser.newPage()
      for (const name of ["speech", "audio"]) {
        await page.addScriptTag({
          content: await readFile(
            new URL(`../bridges/study_card_${name}.js`, import.meta.url),
            "utf8",
          ),
        })
      }
      await page.evaluate(() => {
        window.trace = []
        window.warnings = []
        window.available = [
          { name: "Otoya", lang: "ja-JP", localService: false },
          { name: "Kyoko", lang: "ja-JP", localService: true },
          { name: "Otoya", lang: "ja-JP", localService: true },
        ]
        window.automaticEnd = true
        window.synthesis = new EventTarget()
        synthesis.getVoices = () => available
        synthesis.cancel = () => trace.push("cancel")
        synthesis.speak = (utterance) => {
          trace.push([
            utterance.text,
            utterance.voice.name,
            utterance.voice.localService,
            utterance.rate,
          ])
          window.lastUtterance = utterance
          if (automaticEnd) queueMicrotask(() => utterance.onend?.())
        }
        Object.defineProperty(window, "speechSynthesis", { configurable: true, value: synthesis })
        Object.defineProperty(window, "SpeechSynthesisUtterance", {
          configurable: true,
          value: function (text) {
            this.text = text
          },
        })
        HTMLMediaElement.prototype.play = function () {
          trace.push(this.getAttribute("src"))
          queueMicrotask(() => this.dispatchEvent(new Event("ended")))
          return Promise.resolve()
        }
        window.clip = (text, properties = {}) => ({
          type: "tts",
          field_text: text,
          lang: "ja_JP",
          voices: ["Apple_Otoya"],
          speed: 0.8,
          other_args: [],
          ...properties,
        })
        window.player = window.studyCardAudio({
          clips: {
            q: [clip("question"), { type: "audio", src: "recording.mp3" }],
            a: [clip("answer")],
          },
          options: { autoplay: false, replay_question: true },
          side: "a",
          send: () => {},
          warn: (message) => warnings.push(message),
        })
      })
      await page.evaluate(() => player.play(true))
      assert.deepEqual(await page.evaluate(() => trace), [])
      await page.evaluate(() => player.play(false))
      assert.deepEqual(await page.evaluate(() => trace), [
        ["question", "Otoya", true, 0.8],
        "recording.mp3",
        ["answer", "Otoya", true, 0.8],
      ])
      await page.evaluate(async () => {
        trace.length = 0
        automaticEnd = false
        player.play(false)
        await new Promise((resolve) => setTimeout(resolve, 0))
        player.stop()
      })
      assert.deepEqual(await page.evaluate(() => trace), [
        ["question", "Otoya", true, 0.8],
        "cancel",
      ])
      await page.evaluate(async () => {
        automaticEnd = true
        available = [
          { name: "Cloud", lang: "ja-JP", localService: false },
          { name: "English", lang: "en-US", localService: true },
        ]
        trace.length = 0
        await player.play(false, clip("private text"))
      })
      assert.deepEqual(await page.evaluate(() => trace), [])
      assert.match(await page.evaluate(() => warnings.at(-1)), /tts_voice_unavailable/)
      await page.evaluate(async () => {
        available = []
        const waiting = player.play(false, clip("delayed", { voices: ["Missing"] }))
        setTimeout(() => {
          available = [{ name: "Kyoko", lang: "ja-JP", localService: true }]
          synthesis.dispatchEvent(new Event("voiceschanged"))
        }, 0)
        await waiting
      })
      assert.deepEqual(await page.evaluate(() => trace), [["delayed", "Kyoko", true, 0.8]])
      await page.evaluate(async () => {
        trace.length = 0
        await player.play(false, clip("unsupported", { other_args: ["vendor=cloud"] }))
        await player.play(false, clip("bad speed", { speed: -1 }))
      })
      assert.deepEqual(await page.evaluate(() => trace), [])
      assert.match(await page.evaluate(() => warnings.at(-1)), /tts_options_unsupported/)
      await page.evaluate(async () => {
        automaticEnd = false
        player.play(false)
        await new Promise((resolve) => setTimeout(resolve, 0))
        window.dispatchEvent(new Event("pagehide"))
      })
      assert.deepEqual(await page.evaluate(() => trace), [
        ["question", "Kyoko", true, 0.8],
        "cancel",
      ])
    } finally {
      await browser.close()
    }
  })
}
