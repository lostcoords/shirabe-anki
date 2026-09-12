import json
import unittest

import support
from shirabe_addon import bridge
from shirabe_addon.bridge import BridgeError, Proposal, ProposalGate, session_slots


class NativeProposals(unittest.TestCase):
    def test_only_typed_proposals_are_accepted(self):
        payload = {"action": "add", "cardId": 10, "selection": {"lemma": "猫"}}
        self.assertEqual(Proposal.parse(json.dumps(payload)).action, "add")
        for change in ({"action": "delete"}, {"cardId": True}, {"cardId": 0}, {"selection": "bad"}, {"path": "/collection"}):
            with self.assertRaises(BridgeError):
                Proposal.parse(json.dumps({**payload, **change}))

    def test_limits_count_utf8_bytes_and_invalid_json_is_refused(self):
        with self.assertRaises(BridgeError):
            Proposal.parse(json.dumps({"action": "add", "cardId": 1, "selection": {"text": "猫" * 23000}}, ensure_ascii=False))
        for raw in ("{", "[]", "null"):
            with self.assertRaises(BridgeError):
                Proposal.parse(raw)

    def test_repeated_deck_scripts_cannot_stack_native_dialogs(self):
        now = [1]
        gate = ProposalGate(lambda: now[0])
        gate.enter()
        now[0] = 3
        with self.assertRaises(BridgeError):
            gate.enter()
        gate.leave()
        gate.enter()
        gate.leave()
        with self.assertRaises(BridgeError):
            gate.enter()

    def test_malformed_unicode_and_deep_json_are_typed_rejections(self):
        nested = '{"action":"add","cardId":1,"selection":' + '[' * 10000 + '0' + ']' * 10000 + '}'
        for raw in (nested, '\ud800'):
            with self.subTest(raw=raw[:40]), self.assertRaises(BridgeError):
                Proposal.parse(raw)

    def test_nested_storage_json_is_rejected_without_recursing_out_of_the_bridge(self):
        with self.assertRaises(BridgeError):
            session_slots('[' * 2000 + '0' + ']' * 2000)

    def test_storage_bridge_cannot_overwrite_other_settings(self):
        self.assertEqual(json.loads(session_slots('{"shirabe.link":"{}"}')), {"shirabe.link": "{}"})
        for raw in ('{"onlineParsing":true}', '{"shirabe.link":{}}', "x" * 17000):
            with self.assertRaises(BridgeError):
                session_slots(raw)

    def test_diagnostic_details_allow_counts_and_enums_not_card_content(self):
        self.assertEqual(bridge.report_details('{"kind":"word","senses":3,"note":"private","query":"猫"}'),
                         {"kind": "word", "senses": 3})
        self.assertEqual(bridge.report_details('{"reason":"refused"}'), {"reason": "refused"})
        for raw in ('{"kind":"猫","reason":"private text","senses":true}',
                    '{"senses":-1}', '{"senses":10001}', '{"senses":0.5}', "[]", "null", "{", "x" * 1025):
            with self.subTest(raw=raw[:80]):
                self.assertEqual(bridge.report_details(raw), {})
