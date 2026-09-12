import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import test_offline_rich_content as rich_tests
import offline_rich_content_fixture as fixture
from offline_rich_content_fixture import prepare_rich
from shirabe_addon import offline


class NativePersistenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.buildinfo = types.ModuleType("anki.buildinfo")
        self.buildinfo.version = "24.04"
        self.buildinfo.__file__ = "/suite/lib/anki/buildinfo.py"
        self.anki = types.ModuleType("anki")
        self.anki.buildinfo = self.buildinfo
        prepared = prepare_rich()
        filename, encoded = offline.snapshot_file(prepared.snapshot)
        self.report = {"snapshot": prepared.snapshot, "rows": {"notes": 0, "cards": 0, "revlog": 0},
                       "decoded_bytes": prepared.bytes_used, "encoded_bytes": len(encoded),
                       "filename": filename, "sha256": filename.removesuffix(".json").rsplit("_", 1)[1],
                       "anki_version": "24.04", "anki_buildinfo": self.buildinfo.__file__,
                       "python": "/suite/bin/python"}

    def invoke(self, run):
        case = rich_tests.RichContentTests("test_actual_native_persistence_retains_rich_content_and_exact_media")
        with patch.dict(sys.modules, {"anki": self.anki, "anki.buildinfo": self.buildinfo}), \
             patch.object(sys, "executable", "/suite/bin/python"), \
             patch.object(rich_tests, "anki_python", return_value=Path("/installed/default/python")) as fallback, \
             patch.object(rich_tests.subprocess, "run", side_effect=run):
            case.test_actual_native_persistence_retains_rich_content_and_exact_media()
            return fallback.call_count

    def response(self):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(self.report), stderr="")

    def test_importable_anki_uses_parent_interpreter_and_library_not_installed_default(self):
        def run(command, **options):
            self.assertEqual(command[0], "/suite/bin/python")
            self.assertEqual(options["env"]["QT_QPA_PLATFORM"], "offscreen")
            expected_paths = [str(Path(path or os.getcwd()).resolve()) for path in sys.path]
            self.assertEqual(options["env"]["PYTHONPATH"].split(os.pathsep), expected_paths)
            self.assertEqual(command[command.index("--expected-anki-version") + 1], "24.04")
            self.assertEqual(command[command.index("--expected-anki-buildinfo") + 1], self.buildinfo.__file__)
            return self.response()

        self.assertEqual(self.invoke(run), 0)

    def test_native_report_from_another_version_is_rejected(self):
        self.report["anki_version"] = "25.09.4"
        with self.assertRaisesRegex(AssertionError, "25.09.4"):
            self.invoke(lambda *args, **kwargs: self.response())

    def test_native_report_from_another_library_copy_is_rejected(self):
        self.report["anki_buildinfo"] = "/installed/default/anki/buildinfo.py"
        with self.assertRaisesRegex(AssertionError, "/installed/default/anki/buildinfo.py"):
            self.invoke(lambda *args, **kwargs: self.response())

    def test_present_but_broken_anki_does_not_fall_back(self):
        del self.anki.buildinfo

        def missing(name):
            if name == "buildinfo":
                raise ModuleNotFoundError("Missing Anki dependency", name="_rsbridge")
            raise AttributeError(name)

        self.anki.__getattr__ = missing
        with self.assertRaisesRegex(ModuleNotFoundError, "Missing Anki dependency"):
            self.invoke(lambda *args, **kwargs: self.fail("Broken Anki must not launch another runtime"))

    def test_absent_anki_may_use_installed_default_without_mutating_environment(self):
        case = rich_tests.RichContentTests("test_actual_native_persistence_retains_rich_content_and_exact_media")
        self.report.update(python="/installed/default/python", anki_version="25.09.4")
        before = dict(os.environ)
        with patch.dict(sys.modules, {"anki": None}), \
             patch.object(rich_tests, "anki_python", return_value=Path("/installed/default/python")) as fallback, \
             patch.object(rich_tests.subprocess, "run", return_value=self.response()) as run:
            case.test_actual_native_persistence_retains_rich_content_and_exact_media()
        fallback.assert_called_once_with()
        self.assertEqual(run.call_args.args[0][0], "/installed/default/python")
        self.assertNotIn("--expected-anki-version", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"], {**before, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(dict(os.environ), before)

    def test_fixture_rejects_version_and_library_mismatch_before_collection_setup(self):
        with patch.dict(sys.modules, {"anki": self.anki, "anki.buildinfo": self.buildinfo}), \
             patch.object(fixture.tempfile, "TemporaryDirectory", side_effect=AssertionError("No collection setup allowed")):
            with self.assertRaisesRegex(RuntimeError, "Expected Anki 25.09.4, loaded 24.04"):
                fixture.persist_fixture(expected_anki_version="25.09.4")
            with self.assertRaisesRegex(RuntimeError, "Expected Anki library"):
                fixture.persist_fixture(expected_anki_buildinfo="/another/library/buildinfo.py")


if __name__ == "__main__":
    unittest.main()
