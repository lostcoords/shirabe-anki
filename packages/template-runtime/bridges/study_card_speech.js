window.studyCardSpeech = (warn) => {
  let current = null
  const stop = () => {
    if (!current) return
    current = null
    window.speechSynthesis?.cancel()
  }
  const voices = (engine, signal) => {
    if (engine.getVoices().length) return Promise.resolve(engine.getVoices())
    return new Promise((resolve) => {
      const finish = () => {
        clearTimeout(timer)
        engine.removeEventListener("voiceschanged", finish)
        signal.removeEventListener("abort", finish)
        resolve(engine.getVoices())
      }
      const timer = setTimeout(finish, 1500)
      engine.addEventListener("voiceschanged", finish, { once: true })
      signal.addEventListener("abort", finish, { once: true })
    })
  }
  const play = async (clip, signal) => {
    const engine = window.speechSynthesis
    if (!engine || !window.SpeechSynthesisUtterance) {
      warn("tts_unavailable: This browser has no speech synthesis.")
      return
    }
    if (
      clip.other_args.length ||
      !Number.isFinite(clip.speed) ||
      clip.speed < 0.1 ||
      clip.speed > 10
    ) {
      warn("tts_options_unsupported: This browser cannot use these speech options.")
      return
    }
    const language = clip.lang.replaceAll("_", "-")
    const normalized = (name) =>
      name
        .replace(/^(Apple|Microsoft)_/, "")
        .replaceAll("_", " ")
        .toLowerCase()
    const available = (await voices(engine, signal)).filter(
      (voice) =>
        voice.localService === true &&
        voice.lang.split(/[-_]/)[0].toLowerCase() === language.split("-")[0].toLowerCase(),
    )
    if (signal.aborted) return
    const voice =
      clip.voices
        .map((name) =>
          available.find((candidate) => normalized(candidate.name) === normalized(name)),
        )
        .find(Boolean) ||
      available.find(
        (candidate) => candidate.lang.replaceAll("_", "-").toLowerCase() === language.toLowerCase(),
      ) ||
      available[0]
    if (!voice) {
      warn(`tts_voice_unavailable: ${language} (local browser voices only).`)
      return
    }
    const utterance = new SpeechSynthesisUtterance(clip.field_text)
    utterance.voice = voice
    utterance.lang = language
    utterance.rate = clip.speed
    current = utterance
    await new Promise((resolve) => {
      const finish = () => {
        signal.removeEventListener("abort", cancel)
        utterance.onend = null
        utterance.onerror = null
        if (current === utterance) current = null
        resolve()
      }
      const cancel = () => {
        stop()
        finish()
      }
      utterance.onend = finish
      utterance.onerror = (event) => {
        if (!signal.aborted) warn(`tts_playback_failed: ${event.error}`)
        finish()
      }
      signal.addEventListener("abort", cancel, { once: true })
      try {
        engine.speak(utterance)
      } catch {
        warn("tts_playback_failed: This browser could not start speech.")
        finish()
      }
    })
  }
  return { play, stop }
}
