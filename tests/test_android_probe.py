import contextlib
import io
import json
import unittest
from unittest.mock import patch

import android_probe


class AndroidProbeTest(unittest.TestCase):
    def invoke(self, arguments, output):
        with patch("sys.argv", ["android_probe", "--android-root", "/unused", *arguments]), \
             patch("subprocess.check_output", side_effect=output), \
             contextlib.redirect_stdout(io.StringIO()) as stdout, \
             contextlib.redirect_stderr(io.StringIO()):
            android_probe.main()
        return stdout.getvalue()

    def run_probe(self, command, dump_output):
        self.taps = []
        stale_xml = '<hierarchy><node text="Continue" bounds="[0,0][100,100]" clickable="true" /></hierarchy>'

        def output(arguments, **kwargs):
            if arguments[-3:] == ["emu", "avd", "name"]:
                return "shirabe_phase0\nOK\n"
            if "uiautomator" in arguments:
                return dump_output
            if "cat" in arguments:
                return stale_xml
            if "tap" in arguments:
                self.taps.append(arguments[-2:])
                return ""
            raise AssertionError(arguments)

        self.invoke(command, output)

    def test_failed_dump_does_not_report_previous_screen(self):
        with self.assertRaises(SystemExit):
            self.run_probe(["ui"], "")

    def test_failed_dump_does_not_tap_previous_screen(self):
        with self.assertRaises(SystemExit):
            self.run_probe(["tap", "Continue"], "ERROR: could not get idle state.\n")
        self.assertEqual([], self.taps)

    def test_successful_dump_can_tap_current_screen(self):
        self.run_probe(["tap", "Continue"], "UI hierchary dumped to: /sdcard/phase0-window.xml\n")
        self.assertEqual([["50", "50"]], self.taps)

    def test_physical_tap_requires_explicit_permission_before_adb(self):
        with self.assertRaisesRegex(SystemExit, "Pass --allow-physical"):
            self.invoke([
                "--target", "physical", "--serial", "PHONE123",
                "--expected-model", "Pixel 9", "tap", "Continue",
            ], lambda arguments, **kwargs: self.fail(arguments))

    def test_physical_mode_refuses_an_emulator_serial_before_adb(self):
        with self.assertRaisesRegex(SystemExit, "exact non-emulator --serial"):
            self.invoke([
                "--target", "physical", "--serial", "emulator-5580", "info",
            ], lambda arguments, **kwargs: self.fail(arguments))

    def test_physical_ui_requires_the_model_reported_by_info(self):
        def output(arguments, **kwargs):
            if arguments[-1] == "get-state":
                return "device\n"
            if arguments[-2:] == ["getprop", "ro.product.model"]:
                return "Pixel 9\n"
            self.fail(arguments)

        with self.assertRaisesRegex(SystemExit, "Run info first"):
            self.invoke([
                "--target", "physical", "--serial", "PHONE123", "ui",
            ], output)

    def test_physical_tap_refuses_a_different_model(self):
        calls = []

        def output(arguments, **kwargs):
            calls.append(arguments)
            if arguments[-1] == "get-state":
                return "device\n"
            if arguments[-2:] == ["getprop", "ro.product.model"]:
                return "Pixel 8\n"
            self.fail(arguments)

        with self.assertRaisesRegex(SystemExit, "Expected model 'Pixel 9', found 'Pixel 8'"):
            self.invoke([
                "--target", "physical", "--serial", "PHONE123",
                "--expected-model", "Pixel 9", "--allow-physical", "tap", "Continue",
            ], output)
        self.assertFalse(any("tap" in call for call in calls))

    def test_physical_tap_refuses_when_ankidroid_is_not_foreground(self):
        taps = []
        xml = '<hierarchy><node package="com.android.settings" text="Continue" bounds="[0,0][100,100]" clickable="true" /></hierarchy>'

        def output(arguments, **kwargs):
            if arguments[-1] == "get-state":
                return "device\n"
            if arguments[-2:] == ["getprop", "ro.product.model"]:
                return "Pixel 9\n"
            if "uiautomator" in arguments:
                return "UI hierchary dumped to: /sdcard/phase0-window.xml\n"
            if "cat" in arguments:
                return xml
            if "tap" in arguments:
                taps.append(arguments)
                return ""
            self.fail(arguments)

        with self.assertRaisesRegex(SystemExit, "AnkiDroid is not the foreground app"):
            self.invoke([
                "--target", "physical", "--serial", "PHONE123",
                "--expected-model", "Pixel 9", "--allow-physical", "tap", "Continue",
            ], output)
        self.assertEqual([], taps)

    def test_physical_tap_accepts_the_exact_device_and_ankidroid_node(self):
        taps = []
        xml = '<hierarchy><node package="com.ichi2.anki" text="Continue" bounds="[0,0][100,100]" clickable="true" /></hierarchy>'

        def output(arguments, **kwargs):
            if arguments[-1] == "get-state":
                return "device\n"
            if arguments[-2:] == ["getprop", "ro.product.model"]:
                return "Pixel 9\n"
            if "uiautomator" in arguments:
                return "UI hierchary dumped to: /sdcard/phase0-window.xml\n"
            if "cat" in arguments:
                return xml
            if "tap" in arguments:
                taps.append(arguments[-2:])
                return ""
            self.fail(arguments)

        self.invoke([
            "--target", "physical", "--serial", "PHONE123",
            "--expected-model", "Pixel 9", "--allow-physical", "tap", "Continue",
        ], output)
        self.assertEqual([["50", "50"]], taps)

    def test_physical_info_reports_identity_without_an_expected_model(self):
        def output(arguments, **kwargs):
            if arguments[-1] == "get-state":
                return "device\n"
            values = {
                "ro.product.model": "Pixel 9 Pro",
                "ro.product.manufacturer": "Google",
                "ro.build.version.release": "16",
                "ro.build.version.sdk": "36",
            }
            if "getprop" in arguments:
                return values[arguments[-1]] + "\n"
            if arguments[-3:] == ["dumpsys", "package", "com.ichi2.anki"]:
                return "versionName=2.24.1\n"
            if arguments[-2:] == ["dumpsys", "webviewupdate"]:
                return "Current WebView package: com.google.android.webview 124\n"
            self.fail(arguments)

        report = json.loads(self.invoke([
            "--target", "physical", "--serial", "PHONE123", "info",
        ], output))
        self.assertEqual("physical", report["target"])
        self.assertEqual("PHONE123", report["serial"])
        self.assertEqual("Pixel 9 Pro", report["model"])
        self.assertEqual("Google", report["manufacturer"])
