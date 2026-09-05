"""The Shirabe settings window.

Anki will show a JSON editor for any add-on that does not offer something better,
and for this one that would be the wrong surface twice over: half the fields are
written by the sign-in rather than typed, and a reader pasting a refresh token
into a text box is the exact thing the device and loopback flows exist to avoid.

So this window shows the account as a STATE with a button, and the preferences as
the three controls they are.
"""

from __future__ import annotations

from typing import Callable
from .inject import account_state

from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    Qt,
)

DEFAULT_API_BASE = "https://shirabe.org"

# What opens the card. A modifier is Yomitan's model and the browser
# extension's: it keeps a card from appearing under a pointer that is only
# passing through. Touch always taps, whichever of these is chosen.
TRIGGERS = [
    ("Hovering a word", "hover"),
    ("Clicking a word", "click"),
    ("Hovering with Shift held", "shift"),
    ("Hovering with Alt / Option held", "alt"),
    ("Hovering with Ctrl held", "ctrl"),
    ("Hovering with Cmd held", "meta"),
]

DEFINITION_SIZES = [("Medium", "medium"), ("Small", "small"), ("Large", "large")]
THEMES = [("Follow the system", "system"), ("Light", "light"), ("Dark", "dark")]


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent,
        config: dict,
        on_save: Callable[[dict], None],
        on_sign_out: Callable[[], None],
        on_sign_in: Callable[[], None],
    ):
        super().__init__(parent)
        self.setWindowTitle("Shirabe")
        self.setMinimumWidth(460)
        self._config = config
        self._on_save = on_save
        self._on_sign_out = on_sign_out
        self._on_sign_in = on_sign_in

        layout = QVBoxLayout(self)
        layout.addWidget(self._account_box())
        layout.addWidget(_rule())
        layout.addLayout(self._preferences())
        layout.addWidget(self._advanced())
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _account_box(self) -> QFrame:
        box = QFrame()
        row = QVBoxLayout(box)
        state = account_state(self._config)
        signed_in = state == "connected"

        labels = {"connected": "Connected", "waiting": "Waiting for approval", "disconnected": "Not signed in"}
        row.addWidget(QLabel(f"<b>{labels[state]}</b>"))
        detail = QLabel(
            "Signing out here covers this computer. To sign out everywhere, including "
            "phones, remove the device on your shirabe.org account page."
            if signed_in
            else "Connect this computer to Shirabe. Approve access in your browser, then return here. "
            "Phones sign in separately; syncing a note type does not sync your login."
        )
        detail.setWordWrap(True)
        detail.setStyleSheet("color: palette(mid)")
        row.addWidget(detail)

        if signed_in:
            buttons = QHBoxLayout()
            sign_out = QPushButton("Sign out on this computer")
            sign_out.clicked.connect(self._sign_out)
            buttons.addWidget(sign_out)
            buttons.addStretch(1)
            row.addLayout(buttons)
        else:
            sign_in = QPushButton("Continue sign-in" if state == "waiting" else "Sign in")
            sign_in.clicked.connect(self._sign_in)
            row.addWidget(sign_in)
        return box

    def _sign_in(self) -> None:
        self._save()
        self._on_sign_in()

    def _sign_out(self) -> None:
        # The card reads what Python holds when it renders, so clearing it here
        # is the whole of it: the next card comes up signed out.
        self.accept()
        self._on_sign_out()

    # ── Preferences ──────────────────────────────────────────────────────────

    def _preferences(self) -> QFormLayout:
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.enabled = QCheckBox("Show the popup on cards")
        self.enabled.setChecked(bool(self._config.get("enabled", True)))
        self.enabled.setToolTip("Off leaves your cards exactly as they were: no scripts, no lookups.")
        form.addRow("", self.enabled)

        self.trigger = _choice(TRIGGERS, self._config.get("trigger", "hover"))
        form.addRow("Open the card by", self.trigger)

        self.collection_states = QCheckBox("Colour words using my own Anki notes")
        self.collection_states.setChecked(bool(self._config.get("useCollectionStates", True)))
        self.collection_states.setToolTip(
            "On: a word is coloured by whether this collection already has a note for it.\n"
            "Off: by what your Shirabe account knows."
        )
        form.addRow("", self.collection_states)

        self.report_usage = QCheckBox("Send anonymous crash and usage reports")
        self.report_usage.setChecked(bool(self._config.get("reportUsage", True)))
        self.report_usage.setToolTip(
            "What is sent: that a card opened or a save failed, plus your Anki, Qt and\n"
            "operating system versions, which is how we tell whether a release broke a\n"
            "particular Anki. What is never sent: a word, a note, a deck, or anything\n"
            "identifying you."
        )
        form.addRow("", self.report_usage)

        self.definition_size = _choice(DEFINITION_SIZES, self._config.get("definitionSize", "medium"))
        form.addRow("Definition size", self.definition_size)

        self.theme = _choice(THEMES, self._config.get("theme", "system"))
        form.addRow("Popup theme", self.theme)
        return form

    # ── Advanced ─────────────────────────────────────────────────────────────
    # Folded away because almost nobody needs it, and present because the window
    # replaced Anki's JSON editor: a setting with no control is a setting nobody
    # can reach.

    def _advanced(self) -> QGroupBox:
        box = QGroupBox("Advanced")
        box.setCheckable(True)
        # Open only when it already holds something other than the default, so a
        # dev pointing at their own server sees why it is behaving differently.
        box.setChecked(self._config.get("apiBase") not in (None, "", DEFAULT_API_BASE))
        box.toggled.connect(lambda shown: [w.setVisible(shown) for w in box.findChildren(QFrame) + [inner]])

        inner = QFormLayout()
        self.api_base = QLineEdit(self._config.get("apiBase", DEFAULT_API_BASE))
        self.api_base.setPlaceholderText(DEFAULT_API_BASE)
        self.api_base.setToolTip("The Shirabe server to look words up on. Change this to test against your own.")
        inner.addRow("Server", self.api_base)
        box.setLayout(inner)
        return box

    def _save(self) -> None:
        self._on_save({
            **self._config,
            "enabled": self.enabled.isChecked(),
            "trigger": self.trigger.currentData(),
            "useCollectionStates": self.collection_states.isChecked(),
            "reportUsage": self.report_usage.isChecked(),
            "definitionSize": self.definition_size.currentData(),
            "theme": self.theme.currentData(),
            "apiBase": self.api_base.text().strip() or DEFAULT_API_BASE,
        })
        self.accept()


def _choice(options: list[tuple[str, str]], current: str) -> QComboBox:
    box = QComboBox()
    for label, value in options:
        box.addItem(label, value)
    index = box.findData(current)
    box.setCurrentIndex(index if index >= 0 else 0)
    return box


def _rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line
