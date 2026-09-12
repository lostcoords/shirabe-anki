#!/usr/bin/env python3
"""Inspect the isolated Shirabe emulator or an explicitly verified physical device."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Optional
import xml.etree.ElementTree as ET


ANKIDROID_PACKAGE = "com.ichi2.anki"
DUMP_PATH = "/sdcard/phase0-window.xml"


@dataclass(frozen=True)
class AndroidTarget:
    kind: str
    serial: str
    adb_port: str
    expected_model: Optional[str]
    allow_physical: bool

    @classmethod
    def from_args(cls, args):
        serial = args.serial or ("emulator-5580" if args.target == "emulator" else "")
        adb_port = args.adb_port or ("5041" if args.target == "emulator" else "5037")
        if args.target == "physical":
            if not serial or serial.startswith("emulator-"):
                raise SystemExit("Physical mode requires the exact non-emulator --serial from adb devices -l.")
            if args.command == "tap" and not args.allow_physical:
                raise SystemExit("Pass --allow-physical to send a tap to a verified physical device.")
        return cls(args.target, serial, adb_port, args.expected_model, args.allow_physical)

    def verify(self, run, command):
        if self.kind == "emulator":
            if not self.serial.startswith("emulator-") or run("emu", "avd", "name").splitlines()[0] != "shirabe_phase0":
                raise SystemExit("Refusing to operate on anything except the shirabe_phase0 emulator.")
            return run("shell", "getprop", "ro.product.model").strip() if command == "info" else None
        if run("get-state").strip() != "device":
            raise SystemExit(f"Physical device {self.serial!r} is not ready for ADB commands.")
        model = run("shell", "getprop", "ro.product.model").strip()
        if command != "info":
            if not self.expected_model:
                raise SystemExit("Run info first, then pass its exact model with --expected-model.")
            if model != self.expected_model:
                raise SystemExit(f"Expected model {self.expected_model!r}, found {model!r}; refusing to continue.")
        return model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android-root", type=Path, required=True)
    parser.add_argument("--target", choices=("emulator", "physical"), default="emulator")
    parser.add_argument("--serial")
    parser.add_argument("--adb-port")
    parser.add_argument("--expected-model")
    parser.add_argument("--allow-physical", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info")
    sub.add_parser("ui")
    tap = sub.add_parser("tap")
    tap.add_argument("text")
    tap.add_argument("--count", type=int, default=1)
    shot = sub.add_parser("screenshot")
    shot.add_argument("output", type=Path)
    sub.add_parser("debug-port")
    args = parser.parse_args()
    target = AndroidTarget.from_args(args)
    adb = [str(args.android_root.resolve() / "sdk/platform-tools/adb"), "-P", target.adb_port, "-s", target.serial]

    def run(*parts: str, binary: bool = False):
        return subprocess.check_output([*adb, *parts], text=not binary, timeout=30)

    model = target.verify(run, args.command)

    def visible_nodes():
        dump = run("shell", "uiautomator", "dump", DUMP_PATH)
        if f"UI hierchary dumped to: {DUMP_PATH}" not in dump:
            raise SystemExit("Android UI dump failed; refusing to use the previous screen.")
        tree = ET.fromstring(run("shell", "cat", DUMP_PATH))
        nodes = [dict(node.attrib) for node in tree.iter("node")]
        if target.kind == "physical" and not any(node.get("package") == ANKIDROID_PACKAGE for node in nodes):
            raise SystemExit("AnkiDroid is not the foreground app; refusing to inspect or tap another app.")
        return nodes

    if args.command == "info":
        package = run("shell", "dumpsys", "package", ANKIDROID_PACKAGE)
        print(json.dumps({
            "target": target.kind,
            "serial": target.serial,
            "model": model,
            "manufacturer": run("shell", "getprop", "ro.product.manufacturer").strip(),
            "android": run("shell", "getprop", "ro.build.version.release").strip(),
            "sdk": run("shell", "getprop", "ro.build.version.sdk").strip(),
            "ankidroid": re.findall(r"versionName=(\S+)", package),
            "webview": run("shell", "dumpsys", "webviewupdate"),
        }, indent=2))
    elif args.command in ("ui", "tap"):
        nodes = visible_nodes()
        if args.command == "ui":
            print(json.dumps([{k: n[k] for k in ("text", "resource-id", "content-desc", "bounds", "clickable", "checked", "checkable") if n.get(k)} for n in nodes if n.get("text") or n.get("content-desc") or n.get("checkable") == "true"], ensure_ascii=False, indent=2))
        else:
            matches = [n for n in nodes if args.text in (n.get("text"), n.get("content-desc"), n.get("resource-id"))]
            if target.kind == "physical":
                matches = [n for n in matches if n.get("package") == ANKIDROID_PACKAGE]
            clickable = [n for n in matches if n.get("clickable") == "true"]
            if len(clickable) == 1:
                matches = clickable
            if len(matches) != 1:
                raise SystemExit(f"Expected one visible match, found {len(matches)} for {args.text!r}")
            x1, y1, x2, y2 = map(int, re.findall(r"\d+", matches[0]["bounds"]))
            for _ in range(args.count):
                run("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
    elif args.command == "screenshot":
        if target.kind == "physical":
            visible_nodes()
        with args.output.open("xb") as output:
            output.write(run("exec-out", "screencap", "-p", binary=True))
        print(args.output.resolve())
    elif args.command == "debug-port":
        pid = run("shell", "pidof", "com.ichi2.anki").strip()
        if not pid.isdigit():
            raise SystemExit("Expected one AnkiDroid process.")
        sockets = run("shell", "cat", "/proc/net/unix")
        name = f"webview_devtools_remote_{pid}"
        if name not in sockets:
            raise SystemExit("Enable AnkiDroid developer options / WebView debugging in this fixture app first.")
        port = run("forward", "tcp:0", f"localabstract:{name}").strip()
        print(port)


if __name__ == "__main__":
    main()
