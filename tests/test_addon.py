"""What can be checked without Anki running.

`inject` is pure string work and `collection` needs only something shaped like a
collection, so both are testable here. The hooks themselves are not: they need
`aqt`, and a test that imports it is a test that needs a GUI.
"""

import unittest
import json
from pathlib import Path

import support

from shirabe_addon import collection, inject, net, template  # noqa: E402


class AccountState(unittest.TestCase):
    def test_pending_approval_is_not_a_connection(self):
        config = {"cardToken": json.dumps({"shirabe.pending": json.dumps({"expiresAt": 2000})})}
        self.assertEqual(inject.account_state(config, now_ms=1000), "waiting")
        self.assertEqual(inject.account_state(config, now_ms=3000), "disconnected")

    def test_only_a_real_session_is_connected(self):
        for stored in ("", "{}", "broken", '{"shirabe.link":"{}"}'):
            self.assertEqual(inject.account_state({"cardToken": stored}), "disconnected")
        config = {"cardToken": json.dumps({"shirabe.link": json.dumps({"access": "live"})})}
        self.assertEqual(inject.account_state(config), "connected")


class Attachment(unittest.TestCase):
    def test_adds_scripts_and_leaves_the_card_alone(self):
        html = "<div>猫が窓に座る。</div>"
        out = inject.with_card(html, {}, "/_addons/shirabe/web")
        self.assertIn(html, out, "the card's own HTML is never rewritten")
        self.assertIn("attach.js", out)

    def test_only_one_script_tag_is_added_because_two_would_race(self):
        # Anki re-executes the scripts in the HTML it writes by creating fresh
        # elements, which load independently of document order. Two `src` tags
        # meant `attach.js` could run before the bundle it needs, and did.
        out = inject.with_card("<div>猫</div>", {}, "/w")
        self.assertEqual(out.count("<script src="), 1)
        self.assertIn('"bundle": "/w/shirabe-card.js"', out, "attach.js has nothing to load")

    def test_adds_no_markup_of_its_own(self):
        # The card reads raw text; a span per Japanese run would define the
        # clause instead of the word (see inject.py).
        out = inject.with_card("<div>猫が窓に座る。</div>", {}, "/w")
        self.assertNotIn("sb-word", out)

    def test_is_idempotent_because_anki_rerenders_the_same_card(self):
        once = inject.with_card("<div>猫</div>", {}, "/w")
        twice = inject.with_card(once, {}, "/w")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("shirabe-card.js"), 1)

    def test_python_writes_no_credential_into_the_page(self):
        # It used to write the access token here, and hid it behind a `pycmd`
        # proxy when that turned out to be readable by every other deck. The
        # card holds its own now, as a client the server will not grant a write
        # to, so there is nothing here worth hiding.
        out = inject.with_card(
            "<div>猫</div>",
            {"apiBase": "https://x.test", "refreshToken": "shrr_secret", "accessToken": "shra_live"},
            "/w",
        )
        self.assertNotIn("shra_live", out)
        self.assertNotIn("shrr_secret", out)
        self.assertNotIn("Token", out)
        self.assertIn("https://x.test", out, "the card still needs to know where the API is")

    def test_both_surfaces_sign_in_as_the_same_read_only_client(self):
        # The desktop injection and the note-type block are two bootstraps for
        # one card. A client id that disagreed would give a reader two accounts
        # on one machine.
        injected = inject.with_card("<div>猫</div>", {}, "/w")
        in_template = template.with_block("", "https://shirabe.org", template.CLIENT_ID, "hover")
        self.assertIn(template.CLIENT_ID, injected)
        self.assertIn(template.CLIENT_ID, in_template)


class FakeCard:
    def __init__(self, type_, ivl):
        self.type = type_
        self.ivl = ivl


class FakeNote:
    def __init__(self, cards):
        self._cards = cards

    def cards(self):
        return self._cards


class FakeCollection:
    """Only the three methods `collection.states_for` uses."""

    def __init__(self, notes):
        self.notes = notes
        self.searched = []

    def build_search_string(self, word):
        return f'"{word}"'

    def find_notes(self, query):
        self.searched.append(query)
        word = query.strip('"')
        return list(range(len(self.notes.get(word, []))))

    def get_note(self, note_id):
        for cards in self.notes.values():
            if note_id < len(cards):
                return FakeNote([cards[note_id]])
        return FakeNote([])


class CollectionStates(unittest.TestCase):
    def test_whole_note_text_is_not_used_without_explicit_heuristic_opt_in(self):
        col = FakeCollection({"猫": [FakeCard(2, 21)]})
        self.assertEqual(collection.states_for(col, ["猫"]), {"猫": "none"})
        self.assertEqual(col.searched, [])

    def test_a_word_with_no_note_is_untracked(self):
        col = FakeCollection({})
        self.assertEqual(collection.states_for(col, ["猫"]), {"猫": "none"})

    def test_a_note_never_studied_is_new(self):
        col = FakeCollection({"猫": [FakeCard(collection.CARD_TYPE_NEW, 0)]})
        self.assertEqual(collection.states_for(col, ["猫"], allow_heuristic=True)["猫"], "new")

    def test_a_young_card_is_learning_and_a_mature_one_is_known(self):
        young = FakeCollection({"猫": [FakeCard(2, 5)]})
        self.assertEqual(collection.states_for(young, ["猫"], allow_heuristic=True)["猫"], "learning")
        mature = FakeCollection({"猫": [FakeCard(2, 21)]})
        self.assertEqual(collection.states_for(mature, ["猫"], allow_heuristic=True)["猫"], "retired")

    def test_the_word_is_escaped_by_anki_rather_than_by_us(self):
        # A word can contain Anki search syntax; a hand-built query would either
        # fail or match the wrong notes.
        col = FakeCollection({})
        collection.states_for(col, ["ア*イ"], allow_heuristic=True)
        self.assertEqual(col.searched, ['"ア*イ"'])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PopupSwitch(unittest.TestCase):
    def test_off_puts_nothing_on_the_card_at_all(self):
        # Not "on the page and declining to open": no scripts, no parse request,
        # no spans. Off should leave a card exactly as its author wrote it.
        html = "<div>猫が窓に座る。</div>"
        self.assertEqual(inject.with_card(html, {"enabled": False}, "/w"), html)

    def test_on_is_the_default_for_a_config_that_has_not_been_written_yet(self):
        self.assertIn("shirabe-card.js", inject.with_card("<div>猫</div>", {}, "/w"))

    def test_the_trigger_reaches_the_card(self):
        out = inject.with_card("<div>猫</div>", {"trigger": "shift"}, "/w")
        self.assertIn('"trigger": "shift"', out)

    def test_a_config_with_no_trigger_uses_click(self):
        self.assertIn('"trigger": "click"', inject.with_card("<div>猫</div>", {}, "/w"))


class WithoutPython(unittest.TestCase):
    """A card template runs on AnkiDroid and AnkiMobile, where no add-on does.

    Nothing here can assume `pycmd` reaches Python, because on a phone there is
    no Python: the same template renders with the add-on's scripts and none of
    its Python behind them.
    """

    def test_the_bridge_is_checked_before_it_is_used(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "web" / "attach.js").read_text()
        self.assertIn("typeof pycmd === \"function\"", source)
        # Every call site goes through the check rather than trusting it once.
        for call in ("shirabe:report:", "shirabe:states:"):
            self.assertIn(call, source)

    def test_the_card_asks_for_no_writes(self):
        # `WRITE_SRS` is one coarse scope covering deletes, bulk edits and
        # reviews, so the card's client is not given it. Taking the buttons away
        # is the SDK's job (`attach` strips what `canWrite: false` denies, and
        # its own suite proves the card then draws neither the buttons nor an
        # upgrade offer); saying so is this file's.
        source = (Path(__file__).resolve().parent.parent / "src" / "web" / "attach.js").read_text()
        self.assertIn("canWrite: false", source)
        self.assertNotIn("canWrite: true", source)

    def test_a_bridge_that_never_answers_gives_up_rather_than_hanging(self):
        # The page paint waits on this promise; unsettled, the words never
        # colour and nothing says why.
        source = (Path(__file__).resolve().parent.parent / "src" / "web" / "attach.js").read_text()
        self.assertIn("setTimeout(function () { give(null); }", source)


class Outbound(unittest.TestCase):
    """Cloudflare answers `Python-urllib/3.14` with 1010 before Rails sees the
    request, so a module that builds its own `Request` is a module that stops
    working in production while every test here still passes."""

    def test_every_request_carries_a_user_agent_we_own(self):
        request = net.json_request("https://x.test/api/v1/parse", {"text": "猫"})
        self.assertIn("Shirabe-Anki/", request.get_header("User-agent"))

    def test_no_module_builds_a_request_of_its_own(self):
        for module in sorted(support.SRC.glob("*.py")):
            if module.name == "net.py":
                continue
            self.assertNotIn(
                "urllib.request.Request(", module.read_text(),
                f"{module.name} builds a Request outside net.py, so it sends no User-Agent",
            )


class NoteType(unittest.TestCase):
    """Writing the card into a note type, which is the only thing that reaches a
    phone. This edits data the reader made, so what it does not touch matters as
    much as what it does."""

    THEIRS = "{{Front}}\n\n<hr id=answer>\n\n{{Back}}\n<div class='notes'>{{Notes}}</div>\n"

    def added(self, text, trigger="click"):
        return template.with_block(text, "https://shirabe.org", "shirabe-anki-card", trigger)

    def test_running_it_twice_leaves_one_copy(self):
        once = self.added(self.THEIRS)
        self.assertEqual(self.added(once).count(template.OPEN), 1)

    def test_running_it_again_upgrades_the_block_in_place(self):
        # This is how a reader gets a fixed card: re-run the menu item, not
        # hand-edit a template they did not write.
        again = self.added(self.added(self.THEIRS, "click"), "hover")
        self.assertIn('trigger: "hover"', again)
        self.assertNotIn('trigger: "click"', again)

    def test_removing_it_gives_back_exactly_what_they_wrote(self):
        self.assertEqual(template.without_block(self.added(self.THEIRS)).strip(), self.THEIRS.strip())

    def test_it_never_touches_a_line_of_theirs(self):
        added = self.added(self.THEIRS)
        for line in self.THEIRS.strip().splitlines():
            self.assertIn(line, added)

    def test_the_desktop_add_on_wins_where_both_are_present(self):
        # Both can end up on one card: the add-on injects at render time and the
        # note type carries the block through a sync. The add-on has the token in
        # Python, so it is the one that should run.
        self.assertIn("if (window.__shirabe || window.__shirabeNativeRender) return;", self.added(self.THEIRS))

    def test_the_script_is_loaded_by_hand_because_the_page_is_already_loaded(self):
        # Anki writes a card into a page that loaded when the reviewer opened, so
        # a DOMContentLoaded listener is registered after the event it wants.
        added = self.added(self.THEIRS)
        self.assertNotIn("DOMContentLoaded", added)
        self.assertIn("script.onload", added)

    def test_the_card_signs_in_as_the_read_only_client(self):
        # On a phone the token lives in localStorage, where every other deck can
        # read it. The client is what makes that survivable.
        self.assertIn("shirabe-anki-card", self.added(self.THEIRS))
        self.assertIn("canWrite: false", self.added(self.THEIRS))


class ReachableFromAPhone(unittest.TestCase):
    """A note type syncs. The address written into it has to work where it lands.

    A loopback address means the machine Anki is running on, so on a phone it
    means the phone, where the bundle is not and never will be. The block has no
    error branch on purpose (a phone with no signal should render the note as it
    always did), so a wrong address here is silent: the reader syncs, taps a
    word, and nothing at all happens.
    """

    def test_a_phone_cannot_reach_this_machine(self):
        for address in [
            "http://127.0.0.1:4818",
            "http://localhost:3000",
            "https://shirabe.localhost",
            "http://[::1]:4818",
            "http://0.0.0.0:8080",
        ]:
            self.assertFalse(template.reaches_a_phone(address), address)

    def test_a_real_host_does(self):
        for address in ["https://shirabe.org", "https://www.shirabe.org", "http://192.168.1.10:3000"]:
            self.assertTrue(template.reaches_a_phone(address), address)


class WhereTheBundleComesFrom(unittest.TestCase):
    """Production reads the CDN. Anything else serves its own copy.

    Without this there is no way to try the phone path at all: the block is
    pinned to a CDN URL, so a note type synced from a dev machine loads a
    bundle that is not there and does nothing, silently.
    """

    def test_production_uses_the_cdn(self):
        self.assertEqual(template.bundle_url("https://shirabe.org"), template.CDN)
        self.assertEqual(template.bundle_url("https://shirabe.org/"), template.CDN)

    def test_any_other_server_serves_its_own(self):
        self.assertEqual(
            template.bundle_url("http://mb.local:4818"),
            "http://mb.local:4818/card/v1/shirabe-card.js",
        )

    def test_the_path_matches_the_cdn_so_only_the_host_differs(self):
        self.assertTrue(template.CDN.endswith(template.BUNDLE_PATH))


class SignInSurvivesARestart(unittest.TestCase):
    """Anki serves its webviews from `http://127.0.0.1:<port>` and picks the
    port fresh on every launch, so the browser's own storage is scoped to an
    origin that stops existing when Anki closes. The sign-in is kept in the
    add-on's config instead, and handed to the page at render time."""

    def test_what_python_holds_reaches_the_card(self):
        out = inject.with_card("<div>猫</div>", {"cardToken": "stored-blob"}, "/w")
        self.assertIn('"stored": "stored-blob"', out)

    def test_a_reader_who_has_never_signed_in_gets_an_empty_one(self):
        self.assertIn('"stored": ""', inject.with_card("<div>猫</div>", {}, "/w"))

    def test_the_card_asks_python_to_keep_it_rather_than_the_browser(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "web" / "attach.js").read_text()
        self.assertIn("shirabe:store:", source)
        # Only where there is a Python to ask. A phone's webview has a stable
        # origin and keeps its own.
        self.assertIn("bridged() ? addonStorage() : undefined", source)
