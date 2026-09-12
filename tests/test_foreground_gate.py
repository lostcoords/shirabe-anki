from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import authoring_desktop_check
import desktop_check
import desktop_saved_audio_check


class ForegroundGate(unittest.TestCase):
    def test_other_cli_probes_refuse_before_native_imports_or_server_setup(self):
        for script in ("offline_consent_probe.py", "asset_cleanup_ui_probe.py", "authoring_editor_probe.py",
                       "template_ui_webengine_probe.py", "offline_webengine_probe.py"):
            with self.subTest(script=script):
                result = self.probe_boundary(script, [], "cocoa")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--allow-foreground", result.stderr)
                self.assertNotIn("Native boundary reached", result.stderr)

    def test_isolated_probe_modes_and_help_stop_at_the_guarded_native_boundary(self):
        for script in ("offline_consent_probe.py", "asset_cleanup_ui_probe.py", "authoring_editor_probe.py",
                       "template_ui_webengine_probe.py", "offline_webengine_probe.py"):
            for flags, platform in (([], "offscreen"), (["--allow-foreground"], "cocoa")):
                with self.subTest(script=script, flags=flags, platform=platform):
                    result = self.probe_boundary(script, flags, platform)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("Native boundary reached", result.stderr)
                    self.assertNotIn("Server setup reached", result.stderr)
                    if platform == "offscreen":
                        self.assertIn("not a native-client run", result.stderr)
            with self.subTest(script=script, mode="help"):
                result = self.probe_boundary(script, ["--help"], "cocoa")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--allow-foreground", result.stdout)

    def test_offscreen_unit_helper_checks_an_existing_application_without_creating_one(self):
        from gui_safety import offscreen_test_application
        app = SimpleNamespace(platformName=lambda: "cocoa")
        factory = Mock(return_value=app)
        factory.instance.return_value = app
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}), \
             patch.dict(sys.modules, {"aqt.qt": SimpleNamespace(QApplication=factory)}):
            with self.assertRaisesRegex(unittest.SkipTest, "existing QApplication"):
                offscreen_test_application([])
            factory.assert_not_called()
            app.platformName = lambda: "offscreen"
            self.assertIs(offscreen_test_application([]), app)
            factory.assert_not_called()

    def test_regular_qt_classes_skip_before_touching_qapplication(self):
        classes = (("test_authoring_ui", "AuthoringDialogs"), ("test_settings_window", "SettingsWindow"),
                   ("test_template_ui_window", "TemplateWindows"), ("test_template_reviewer_check", "NativePreparationTiming"))
        for module_name, class_name in classes:
            cls = getattr(importlib.import_module(module_name), class_name)
            qt = SimpleNamespace(QApplication=SimpleNamespace(instance=Mock(side_effect=AssertionError("QApplication boundary reached"))))
            with self.subTest(module=module_name), patch.dict(os.environ, {"QT_QPA_PLATFORM": "cocoa"}), \
                 patch.dict(sys.modules, {"aqt.qt": qt}):
                with self.assertRaisesRegex(unittest.SkipTest, "offscreen"):
                    cls.setUpClass()
                qt.QApplication.instance.assert_not_called()

    def test_regular_consent_subprocesses_do_not_inherit_foreground_platform(self):
        module = importlib.import_module("test_offline_ui")
        cases = (("test_native_consent_requires_upload_permission_and_defaults_uploaded_dictionaries_off", "native consent passed"),
                 ("test_native_consent_large_font_layout_buttons_and_scrolling", '{"cases": [1, 2, 3, 4, 5, 6], "failures": []}'))
        for name, output in cases:
            result = SimpleNamespace(returncode=0, stdout=output, stderr="")
            with self.subTest(test=name), patch.dict(os.environ, {"QT_QPA_PLATFORM": "cocoa"}), \
                 patch.object(module.subprocess, "run", return_value=result) as run:
                getattr(module.BatchTests(), name)()
                self.assertEqual(run.call_args.kwargs["env"]["QT_QPA_PLATFORM"], "offscreen")

    def probe_boundary(self, script, flags, platform):
        guard = '''
import builtins, runpy, socket, sys
original = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split(".")[0] in {"aqt", "anki", "PyQt6"}:
        raise RuntimeError("Native boundary reached: " + name)
    return original(name, *args, **kwargs)
class NoSocket(socket.socket):
    def __new__(cls, *args, **kwargs):
        raise RuntimeError("Server setup reached")
socket.socket = NoSocket
builtins.__import__ = checked_import
sys.path.insert(0, sys.argv[1])
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        directory = Path(__file__).parent
        return subprocess.run([sys.executable, "-c", guard, str(directory), str(directory / script), *flags],
                              env={**os.environ, "QT_QPA_PLATFORM": platform}, capture_output=True, text=True, timeout=5)

    def test_gui_entrypoints_refuse_without_permission_before_any_setup(self):
        cases = ((desktop_check, []), (desktop_check, ["--keep-open", "--prepare-offline"]),
                 (desktop_saved_audio_check, ["--manifest=fixture.json"]),
                 (authoring_desktop_check, []), (authoring_desktop_check, ["--startup-only"]),
                 (authoring_desktop_check, ["--live-test-recording"]))
        for module, flags in cases:
            with self.subTest(module=module.__name__, flags=flags), ExitStack() as stack:
                stack.enter_context(patch.object(sys, "argv", [module.__file__, *flags]))
                guards = [stack.enter_context(patch.object(module, name, side_effect=AssertionError(f"Setup reached: {name}")))
                          for name in ("require_anki", "output_directory", "fixture_api") if hasattr(module, name)]
                if hasattr(module, "prepare"):
                    guards.append(stack.enter_context(patch.object(module, "prepare", side_effect=AssertionError("Collection setup reached"))))
                error = stack.enter_context(redirect_stderr(io.StringIO()))
                with self.assertRaises(SystemExit) as refusal:
                    module.main()
                self.assertEqual(refusal.exception.code, 2)
                self.assertIn("--allow-foreground", error.getvalue())
                self.assertIn("focus", error.getvalue())
                for guard in guards:
                    guard.assert_not_called()

    def test_help_does_not_require_anki_or_initialize_any_runtime(self):
        for module in (desktop_check, desktop_saved_audio_check, authoring_desktop_check):
            with self.subTest(module=module.__name__), patch.object(sys, "argv", [module.__file__, "--help"]), \
                 patch.object(module, "require_anki", side_effect=AssertionError("Runtime setup reached")) as setup, \
                 redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    module.main()
                self.assertEqual(result.exception.code, 0)
                self.assertIn("--allow-foreground", output.getvalue())
                setup.assert_not_called()

    def test_explicit_permission_reaches_only_the_stubbed_runtime_boundary(self):
        class RuntimeBoundary(Exception):
            pass

        for module, positional in ((desktop_check, []), (desktop_saved_audio_check, ["--manifest=fixture.json"]),
                                   (authoring_desktop_check, [])):
            with self.subTest(module=module.__name__), ExitStack() as stack:
                stack.enter_context(patch.object(sys, "argv", [module.__file__, *positional, "--allow-foreground"]))
                setup = stack.enter_context(patch.object(module, "require_anki", side_effect=RuntimeBoundary))
                server = stack.enter_context(patch.object(module, "fixture_api", side_effect=AssertionError("Server must not start"))) if hasattr(module, "fixture_api") else None
                with self.assertRaises(RuntimeBoundary):
                    module.main()
                setup.assert_called_once_with()
                if server:
                    server.assert_not_called()

    def test_environment_and_abbreviated_flags_cannot_grant_permission(self):
        for module, positional in ((desktop_check, []), (desktop_saved_audio_check, ["--manifest=fixture.json"]),
                                   (authoring_desktop_check, [])):
            for flags in ([], ["--allow-fore"]):
                with self.subTest(module=module.__name__, flags=flags), \
                     patch.object(sys, "argv", [module.__file__, *positional, *flags]), \
                     patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}), \
                     patch.object(module, "require_anki", side_effect=AssertionError("Runtime setup reached")) as setup, \
                     redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as refusal:
                        module.main()
                    self.assertEqual(refusal.exception.code, 2)
                    self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")
                    setup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
