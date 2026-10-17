import { chromium, firefox, webkit } from "playwright"

const engines = { chromium, firefox, webkit }
const selected = process.env.STUDY_BROWSER
if (selected && !Object.hasOwn(engines, selected)) {
  throw new Error(`Unknown STUDY_BROWSER: ${selected}`)
}
export const browserEngines = selected ? [engines[selected]] : Object.values(engines)

export function launchBrowser(engine) {
  return engine.launch({
    headless: true,
    ...(engine === chromium ? { args: ["--mute-audio", "--disable-audio-output"] } : {}),
    ...(engine === firefox ? { firefoxUserPrefs: { "media.volume_scale": "0.0" } } : {}),
  })
}
