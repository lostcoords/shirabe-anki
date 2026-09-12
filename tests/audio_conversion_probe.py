#!/usr/bin/env python3
"""Probe Anki's bundled encoder without opening UI or playing sound."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import wave

from fixture_deck import output_directory, require_anki, save_json


def main():
    require_anki()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = output_directory(args.output)
    from anki.buildinfo import version
    from aqt.sound import _packagedCmd

    command, environment = _packagedCmd(["mpv"])
    report = {"anki": version, "mpv": command[0], "input": str(args.input.resolve()),
              "scope": "Synthetic local audio; bundled mpv only; no UI, playback, collection or network"}
    if not Path(command[0]).is_absolute() or not Path(command[0]).is_file():
        report.update(passed=False, unavailable="This Anki runtime does not resolve a bundled mpv executable.")
    else:
        def run(options):
            process = subprocess.run(command + ["--no-config", "--no-video", "--audio-display=no", "--force-window=no"] + options,
                                     env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            report.setdefault("commands", []).append({"args": process.args, "exit_code": process.returncode,
                                                     "stdout": process.stdout, "stderr": process.stderr})
            process.check_returncode()
            return process.stdout

        report["version"] = run(["--version"])
        report["mp3_encoder_available"] = "--oac=libmp3lame" in run(["--oac=help"])
        encoded, decoded = root / "native-converted.mp3", root / "native-decoded.wav"
        run(["--of=mp3", "--oac=libmp3lame", "--oacopts=b=96000", f"--o={encoded}", "--", str(args.input.resolve())])
        run(["--of=wav", "--oac=pcm_s16le", f"--o={decoded}", "--", str(encoded)])
        with wave.open(str(decoded)) as sound:
            report["decoded"] = {"channels": sound.getnchannels(), "sample_rate": sound.getframerate(),
                                 "frames": sound.getnframes(), "sample_width": sound.getsampwidth(),
                                 "nonzero_samples": any(sound.readframes(sound.getnframes()))}
        data = encoded.read_bytes()
        report.update(passed=bool(data) and report["decoded"]["nonzero_samples"], output=str(encoded),
                      bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    save_json(root / "audio-conversion-report.json", report)
    print(root / "audio-conversion-report.json", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
