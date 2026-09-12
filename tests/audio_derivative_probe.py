#!/usr/bin/env python3
"""Test offline MP3 derivatives with synthetic audio; never contacts storage or Rails."""

import hashlib
import json
from pathlib import Path
import subprocess

from fixture_deck import REPO, output_directory, save_json


def main():
    root = output_directory(None)
    report = {"scope": "Offline synthetic encoder experiment; no Rails boot, database, API, CDN or R2 access", "checks": {}}

    def run(command, *, required=True):
        result = subprocess.run(command, cwd=REPO, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        report.setdefault("commands", []).append({"args": command, "exit_code": result.returncode,
                                                 "stdout": result.stdout, "stderr": result.stderr})
        if required:
            result.check_returncode()
        return result

    def check(name, actual):
        report["checks"][name] = bool(actual)
        if not actual:
            raise AssertionError(name)

    def describe(path):
        details = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
                                  "-of", "json", str(path)]).stdout)
        return dict(path=str(path), bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), **details)

    try:
        ruby = 'clip = Audio::Clip.word("ねこ", accent: 1); puts JSON.generate({digest: clip.digest, path: clip.relative_path, url: clip.url})'
        clip = json.loads(run(["ruby", "-rjson", "-r./app/models/audio/voice", "-r./app/models/audio/clip", "-e", ruby]).stdout)
        report["clip"] = clip
        wav = root / "synthetic-source.wav"
        aac = root / clip["path"]
        direct = root / "from-wav" / Path(clip["path"]).with_suffix(".mp3")
        fallback = root / "from-aac" / Path(clip["path"]).with_suffix(".mp3")
        for target in (aac, direct, fallback):
            target.parent.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
             "-ac", "1", "-ar", "48000", str(wav)])
        run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000", str(aac)])
        before = hashlib.sha256(aac.read_bytes()).hexdigest()
        for source, target in ((wav, direct), (aac, fallback)):
            run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-vn", "-map_metadata", "-1",
                 "-c:a", "libmp3lame", "-b:a", "96k", "-ac", "1", "-ar", "48000", str(target)])
        report["files"] = [describe(path) for path in (aac, direct, fallback)]
        check("m4a_bytes_unchanged", hashlib.sha256(aac.read_bytes()).hexdigest() == before)
        check("same_clip_identity_in_both_formats", aac.stem == direct.stem == fallback.stem == clip["digest"])
        check("existing_url_stays_m4a", clip["url"].endswith("/" + clip["path"]))
        check("m4a_is_aac", report["files"][0]["streams"][0]["codec_name"] == "aac")
        for name, details in zip(("wav_derivative", "aac_derivative"), report["files"][1:]):
            stream = details["streams"][0]
            check(name + "_is_real_mp3", stream["codec_name"] == "mp3")
            check(name + "_is_mono_48khz", stream["channels"] == 1 and stream["sample_rate"] == "48000")
            check(name + "_duration_preserved_within_100ms", abs(float(details["format"]["duration"]) - 0.5) < 0.1)
        invalid = run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(wav), "-c:a", "aac", str(root / "extension-only.mp3")], required=False)
        check("changing_extension_without_codec_is_rejected", invalid.returncode != 0)
        report["passed"] = True
    except Exception as error:
        report.update(passed=False, error=str(error))
    save_json(root / "audio-derivative-report.json", report)
    print(root / "audio-derivative-report.json", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
