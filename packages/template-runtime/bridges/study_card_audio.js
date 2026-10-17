window.studyCardAudio = ({ clips, options, side, send, preserve = () => {}, warn }) => {
  let playback = null
  let currentMedia = null
  const speech = window.studyCardSpeech(warn)

  const stop = (except = null) => {
    playback?.abort()
    playback = null
    speech.stop()
    currentMedia = null
    for (const media of document.querySelectorAll("audio, video")) {
      if (media !== except) media.pause()
    }
  }

  const mediaFor = (clip) => {
    const existing = [...document.querySelectorAll(clip.type)].find(
      (media) => media.getAttribute("src") === clip.src,
    )
    if (existing) return existing
    const media = document.createElement(clip.type)
    media.hidden = true
    media.crossOrigin = "anonymous"
    media.preload = "none"
    media.src = clip.src
    document.body.append(media)
    preserve(media)
    return media
  }

  const play = async (automatic, only = null) => {
    stop()
    if (automatic && !options.autoplay) return
    const selected = only
      ? [only]
      : side === "a" && !automatic && options.replay_question
        ? [...clips.q, ...clips.a]
        : clips[side]
    const mediaList =
      clips.q.length || clips.a.length
        ? selected.map((clip) => (clip.type === "tts" ? clip : mediaFor(clip)))
        : [...document.querySelectorAll("audio, video")]
    const run = new AbortController()
    playback = run
    for (const media of mediaList) {
      if (run.signal.aborted) break
      if (media.type === "tts") {
        await speech.play(media, run.signal)
        continue
      }
      if (!media.isConnected) continue
      currentMedia = media
      await new Promise((resolve) => {
        const finish = () => {
          media.removeEventListener("ended", finish)
          media.removeEventListener("error", finish)
          media.removeEventListener("pause", paused)
          run.signal.removeEventListener("abort", finish)
          resolve()
        }
        const paused = () => {
          if (media.paused && !media.ended) run.abort()
        }
        media.addEventListener("ended", finish)
        media.addEventListener("error", finish)
        media.addEventListener("pause", paused)
        run.signal.addEventListener("abort", finish, { once: true })
        media.currentTime = 0
        media.play().catch((error) => {
          if (!run.signal.aborted && error.name === "NotAllowedError") {
            send("audio-blocked")
            run.abort()
          }
          finish()
        })
      })
    }
    if (playback === run) {
      playback = null
      currentMedia = null
    }
  }

  document.addEventListener(
    "play",
    (event) => {
      if (event.target !== currentMedia) stop(event.target)
    },
    true,
  )
  document.addEventListener("click", (event) => {
    if (!event.isTrusted) return
    const marker = event.target.closest("[data-study-speech]")?.dataset.studySpeech
    const match = /^([qa]):(\d+)$/.exec(marker || "")
    const clip = match && clips[match[1]][Number(match[2])]
    if (clip?.type === "tts") play(false, clip)
  })
  window.addEventListener("pagehide", () => stop())
  return { play, stop }
}

if (document.body?.dataset.studyAudio) {
  window.studyCardPlayback = window.studyCardAudio({
    clips: JSON.parse(document.body.dataset.studyAudio),
    options: JSON.parse(document.body.dataset.studyAudioOptions),
    side: document.body.dataset.studyAudioSide,
    send: (kind) => parent.postMessage({ channel: "shirabe-study", kind }, "*"),
    warn: (message) =>
      document.dispatchEvent(new CustomEvent("study-audio-warning", { detail: message })),
  })
}
