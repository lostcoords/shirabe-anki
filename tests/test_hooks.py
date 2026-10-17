import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import support
from shirabe_addon import offline
from test_bootstrap import settings_from


@unittest.skipUnless(importlib.util.find_spec("aqt"), "Run with Anki's Python to check hook integration")
class NativeHooks(unittest.TestCase):
    def setUp(self):
        from aqt import gui_hooks

        hooks = SimpleNamespace(**{name: [] for name in (
            "profile_did_open", "card_will_show", "webview_did_receive_js_message",
            "main_window_did_init", "browser_menus_did_init", "editor_did_init_buttons",
            "profile_will_close", "operation_did_execute", "sync_did_finish",
            "reviewer_did_answer_card", "state_did_change",
        )})
        for name in vars(hooks):
            self.assertTrue(hasattr(gui_hooks, name), f"Anki is missing {name}")
        self.gui_hooks = hooks
        self.mw = Mock()
        self.mw.addonManager.addonFromModule.return_value = "shirabe-test"
        self.mw.addonManager.getConfig.return_value = {}
        self.original_unload = Mock()
        self.unload_profile = lambda onsuccess: self.original_unload(onsuccess)
        self.mw.unloadProfile = self.unload_profile
        name = "shirabe_addon.hooks_probe"
        path = Path(__file__).resolve().parents[1] / "src" / "__init__.py"
        spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(path)), is_package=False)
        self.hooks = importlib.util.module_from_spec(spec)
        with patch("aqt.mw", self.mw), patch("aqt.gui_hooks", hooks):
            spec.loader.exec_module(self.hooks)
        from anki.notes import Note

        self.note = MagicMock(spec=Note)
        self.note.mid = 7
        self.card = SimpleNamespace(id=42, nid=12, col=self.mw.col, note=lambda: self.note)
        self.mw.col.get_config.return_value = None

    def test_sync_hooks_request_work_and_profile_close_releases_the_session(self):
        session = self.mw._shirabe_study_sync
        for name, arguments in (
            ("operation_did_execute", ("changes", "initiator")),
            ("sync_did_finish", ()),
            ("reviewer_did_answer_card", ("reviewer", "card", 3)),
            ("state_did_change", ("review", "overview")),
        ):
            with self.subTest(hook=name):
                session.request.reset_mock()
                callbacks = getattr(self.gui_hooks, name)
                self.assertEqual(1, len(callbacks))
                callbacks[0](*arguments)
                expected = arguments if name == "operation_did_execute" else (None, None)
                session.request.assert_called_once_with(*expected)
        self.assertEqual([self.hooks.close_study_sync], self.gui_hooks.profile_will_close)
        self.gui_hooks.profile_will_close[0]()
        session.close.assert_called_once_with()
        self.assertIsNone(self.mw._shirabe_study_sync)
        self.gui_hooks.sync_did_finish[0]()
        self.gui_hooks.profile_will_close[0]()
        session.close.assert_called_once_with()

    def test_native_profile_close_passes_through_the_shirabe_sync_gate(self):
        callback = Mock()
        with patch("shirabe_addon.sync_closing.unload_profile") as closing:
            self.mw.unloadProfile(callback)
        closing.assert_called_once_with(self.mw, callback, self.unload_profile)
        self.original_unload.assert_not_called()

    def test_native_bootstrap_decodes_note_snapshot_media_reference(self):
        filename = "_shirabe_snapshot_v1_" + "a" * 64 + ".json"
        fields = {}
        self.note.__contains__.side_effect = fields.__contains__
        self.note.__getitem__.side_effect = fields.__getitem__
        for value, expected in ((offline.snapshot_field(filename), filename), (filename, filename),
                                ('<img src="../private.json">', None), ("x" * 2048, None), (None, "")):
            with self.subTest(value=str(value)[:100]):
                fields.clear()
                if value is not None:
                    fields["ShirabeLookup"] = value
                html = self.hooks.on_card_will_show("<div>猫</div>", self.card, "reviewAnswer")
                settings = settings_from(html[html.rfind("window.__shirabe = "):])
                self.assertEqual(settings["snapshot"], expected)
                self.assertEqual(settings["cardId"], self.card.id)
                self.assertTrue(settings["native"])
                self.assertIn("<div>猫</div>", html)

    def test_sign_out_invalidates_credentials_captured_by_offline_preparation(self):
        credentials = json.dumps({"access": "fixture-token", "expiresAt": 2000})
        self.mw.addonManager.getConfig.return_value = {"cardToken": credentials}
        self.mw.findChildren.return_value = []
        backend = offline.DictionaryBackend("https://fixture.invalid", credentials, now=lambda: 1)
        self.assertEqual(backend._access(), "fixture-token")
        with patch.object(self.hooks, "tooltip"):
            self.hooks.sign_out()
        with self.assertRaises(offline.SnapshotError) as caught:
            backend._access()
        self.assertEqual(caught.exception.code, "reconnect_required")

    def test_non_connection_preferences_do_not_interrupt_preparation(self):
        credentials = json.dumps({"access": "fixture-token", "expiresAt": 2000})
        self.mw.addonManager.getConfig.return_value = {"cardToken": credentials}
        backend = offline.DictionaryBackend("https://fixture.invalid", credentials, now=lambda: 1)
        self.hooks.save_config({"cardToken": credentials, "offlineStorageMiB": 50})
        self.assertEqual(backend._access(), "fixture-token")

    def test_changing_account_or_server_invalidates_captured_preparation(self):
        credentials = json.dumps({"access": "fixture-token", "expiresAt": 2000})
        for changed in ({"apiBase": "https://another.invalid"}, {"cardToken": "new account"}):
            with self.subTest(changed=changed):
                self.mw.addonManager.getConfig.return_value = {"cardToken": credentials}
                backend = offline.DictionaryBackend("https://fixture.invalid", credentials, now=lambda: 1)
                self.hooks.save_config({"cardToken": credentials, **changed})
                with self.assertRaises(offline.SnapshotError) as caught:
                    backend.check_access()
                self.assertEqual(caught.exception.code, "reconnect_required")

    def test_native_states_receive_configured_identity_mappings(self):
        from shirabe_addon import authoring
        profiles = [object()]
        with patch.object(authoring, "profiles_from_config", return_value=profiles), \
                patch.object(self.hooks.collection, "states_for", return_value={"猫": "new"}) as states:
            handled, value = self.hooks.on_js_message((False, None), "shirabe:states:猫", object())
        self.assertTrue(handled)
        self.assertEqual(json.loads(value), {"猫": "new"})
        states.assert_called_once_with(self.mw.col, ["猫"], profiles)

    def test_storage_review_is_accessible_from_tools_while_signed_out(self):
        from shirabe_addon import offline_ui
        actions = {}

        def action(label, parent):
            actions[label] = Mock()
            return actions[label]

        with patch.object(self.hooks, "QAction", side_effect=action), patch.object(self.hooks, "QMenu"), \
                patch.object(offline_ui, "review_cleanup") as cleanup:
            self.hooks.install_menu()
            self.assertIn("Review offline storage…", actions)
            callback = actions["Review offline storage…"].triggered.connect.call_args.args[0]
            callback()
        cleanup.assert_called_once_with(self.mw)
