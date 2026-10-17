"""Qt approval and native editor handoffs for authoring.py."""

from __future__ import annotations

from dataclasses import replace
import sys
from threading import Event
from time import monotonic

from aqt import dialogs, gui_hooks
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QScrollArea,
    QInputDialog, QLabel, QLineEdit, QPushButton, QProgressDialog, QSpinBox, QTableWidget,
    QTableWidgetItem, QTextEdit, QTimer, QVBoxLayout, QWidget, Qt,
)
from aqt.utils import showInfo, showWarning

from .authoring import (
    AuthoringError, FieldProfile, Lookup, NoteChoices, NoteDraft, NoteSnapshot,
    ROLE_ORDER, apply_previews, build_preview, create_preset, field_is_empty, plain_text, profiles_from_config,
    provenance_for, record_created_note, remember_profile, suggest_mapping,
)


def _label(text, parent=None):
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(True)
    return label


def _buttons(dialog, layout, save_label):
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText(save_label)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return buttons


def _check_recording_attribution(preview, profile, media):
    if media is None:
        return
    values = preview.field_values()
    if f"[sound:{media.filename}]" not in values.get(profile.fields.get("audio"), ""):
        return
    source = plain_text(values.get(profile.fields.get("source"), ""))
    if media.attribution not in source or media.license not in source:
        raise AuthoringError("Keep the recording attribution in Source. Choose Append or Replace for that field.")


class MappingDialog(QDialog):
    def __init__(self, mw, config, *, note_type_id=None, parent=None, profile=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Shirabe field mappings")
        available = self.screen().availableGeometry()
        self.resize(min(700, available.width() - 40), min(680, available.height() - 40))
        self.mw, self.config, self.profile = mw, config, None
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        layout.addWidget(_label("Choose where each value belongs. Suggested mappings are saved only when you choose Save mapping."))
        self.mapping_warning = _label("")
        self.mapping_warning.hide()
        layout.addWidget(self.mapping_warning)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(form)
        self.saved = QComboBox()
        self.saved.addItem("New profile", None)
        for p in profiles_from_config(config):
            self.saved.addItem(p.name, p)
        form.addRow("Saved profile", self.saved)
        self.name = QLineEdit("Japanese")
        form.addRow("Profile name", self.name)
        self.model = QComboBox()
        for model in sorted(mw.col.models.all(), key=lambda m: m["name"]):
            self.model.addItem(model["name"], model["id"])
        form.addRow("Note type", self.model)
        self.deck = QComboBox()
        self.deck.addItem("Anki's last deck for this note type", None)
        for deck in mw.col.decks.all_names_and_ids():
            if not mw.col.decks.is_filtered(deck.id):
                self.deck.addItem(deck.name, deck.id)
        form.addRow("Deck", self.deck)
        self.tags = QLineEdit()
        form.addRow("Tags (space separated)", self.tags)
        self.fields = {}
        for role in ROLE_ORDER:
            if role == "pitch":
                form.addRow(_label("Optional fields"))
            box = QComboBox()
            form.addRow(role.capitalize(), box)
            self.fields[role] = box
        self.model.currentIndexChanged.connect(self.populate_fields)
        self.saved.currentIndexChanged.connect(self.load_profile)
        if note_type_id is not None:
            self.model.setCurrentIndex(self.model.findData(note_type_id))
        self.populate_fields()
        for box in (self.saved, self.model, self.deck, *self.fields.values()):
            box.setMinimumContentsLength(8)
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        preset = QPushButton("Create Japanese preset")
        preset.setToolTip("Create the Shirabe Japanese note type with matching fields and card templates")
        preset.clicked.connect(self.create_preset)
        layout.addWidget(preset)
        _buttons(self, outer, "Save mapping")
        if profile is not None:
            index = next((i for i in range(self.saved.count()) if self.saved.itemData(i) == profile), 0)
            self.saved.setCurrentIndex(index)

    def populate_fields(self, *_):
        model = self.mw.col.models.get(self.model.currentData())
        names = [f["name"] for f in model["flds"]] if model else []
        previous_mapping = {role: box.currentData() for role, box in self.fields.items() if box.currentData() in names}
        mapping = suggest_mapping(model, previous_mapping) if model else {}
        for role, box in self.fields.items():
            box.clear()
            box.addItem("Do not use", None)
            for name in names:
                box.addItem(name, name)
            box.setCurrentIndex(max(0, box.findData(mapping.get(role))))

    def create_preset(self):
        def created(result):
            self.model.addItem("Shirabe Japanese", result.profile.note_type_id)
            self.model.setCurrentIndex(self.model.findData(result.profile.note_type_id))
            self.name.setText(result.profile.name)
        CollectionOp(self, create_preset).success(created).run_in_background()

    def load_profile(self, *_):
        self.mapping_warning.hide()
        if profile := self.saved.currentData():
            self.name.setText(profile.name)
            self.model.setCurrentIndex(self.model.findData(profile.note_type_id))
            self.deck.setCurrentIndex(max(0, self.deck.findData(profile.deck_id)))
            self.tags.setText(" ".join(profile.tags))
            for role, box in self.fields.items():
                box.setCurrentIndex(max(0, box.findData(profile.fields.get(role))))
            try:
                profile.validate(self.mw.col.models.get(profile.note_type_id))
            except AuthoringError as error:
                self.mapping_warning.setText(f"{error} Choose the note type and fields below, then save the repaired mapping.")
                self.mapping_warning.show()

    def accept(self):
        try:
            self.profile = FieldProfile(self.name.text().strip(), self.model.currentData(),
                                        {role: box.currentData() for role, box in self.fields.items() if box.currentData()},
                                        self.deck.currentData(), tuple(self.tags.text().split()))
            self.profile.validate(self.mw.col.models.get(self.profile.note_type_id))
        except AuthoringError as error:
            showWarning(str(error), parent=self)
            return
        super().accept()


def configure_profiles(mw, config, save_config, *, note_type_id=None, parent=None, profile=None):
    dialog = MappingDialog(mw, config, note_type_id=note_type_id, parent=parent, profile=profile)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    save_config(remember_profile(_latest_config(config), dialog.profile, previous=dialog.saved.currentData()))
    return dialog.profile


def configure(mw, config, save_config):
    return configure_profiles(mw, config, save_config)


def choose_profile(mw, config, save_config, *, note_type_id=None, parent=None):
    profiles = [p for p in profiles_from_config(config) if note_type_id is None or p.note_type_id == note_type_id]
    if not profiles:
        return configure_profiles(mw, config, save_config, note_type_id=note_type_id, parent=parent)
    if len(profiles) == 1:
        chosen = profiles[0]
    else:
        labels = [f"{p.name} ({mw.col.models.get(p.note_type_id)['name'] if mw.col.models.get(p.note_type_id) else 'missing note type'})" for p in profiles]
        last = config.get("authoringLastProfile")
        index = next((i for i, p in enumerate(profiles) if [p.note_type_id, p.name] == last), 0)
        label, accepted = QInputDialog.getItem(parent or mw, "Shirabe", "Field mapping", labels, index, False)
        if not accepted:
            return None
        chosen = profiles[labels.index(label)]
    try:
        chosen.validate(mw.col.models.get(chosen.note_type_id))
    except AuthoringError:
        return configure_profiles(mw, config, save_config, parent=parent, profile=chosen)
    save_config(remember_profile(config, chosen))
    return chosen


def utf16_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Convert Qt/JavaScript UTF-16 offsets into Python character offsets."""
    raw = text.encode("utf-16-le")
    if not 0 <= start < end <= len(raw) // 2:
        raise AuthoringError("Select the text to hide.")
    try:
        return len(raw[:start * 2].decode("utf-16-le")), len(raw[:end * 2].decode("utf-16-le"))
    except UnicodeDecodeError as error:
        raise AuthoringError("Select whole characters for the cloze.") from error


class RecordingDialog(QDialog):
    def __init__(self, parent, lookup, config):
        super().__init__(parent)
        from .authoring_media import ANKI_PERMISSION
        self.setWindowTitle("Choose a Shirabe recording")
        self.lookup, self.config, self.imported = lookup, config, None
        self.closed = Event()
        self.finished.connect(lambda _: self.closed.set())
        self.destroyed.connect(lambda _=None, closed=self.closed: closed.set())
        if isinstance(parent, QDialog):
            parent.finished.connect(self.reject)
        outer = QVBoxLayout(self)
        content = QScrollArea()
        content.setWidgetResizable(True)
        content.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        content.setWidget(body)
        outer.addWidget(content)
        layout.addWidget(_label("Choose one reading and accent. Audio is downloaded only when you confirm below."))
        self.recordings = QComboBox()
        self.recordings.setMinimumContentsLength(16)
        self.recordings.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.recordings.currentIndexChanged.connect(self.select_recording)
        layout.addWidget(self.recordings)
        self.format_notice = _label("")
        layout.addWidget(self.format_notice)
        self.allow_m4a = QCheckBox("Use M4A anyway")
        self.allow_m4a.setVisible(False)
        self.allow_m4a.toggled.connect(lambda _: self.update_download_button())
        layout.addWidget(self.allow_m4a)
        self.permission = _label("Shirabe\n" + ANKI_PERMISSION)
        layout.addWidget(self.permission)
        self.status = _label("")
        layout.addWidget(self.status)
        self.retry = QPushButton("Retry recording search")
        self.retry.clicked.connect(self.load_recordings)
        self.retry.hide()
        layout.addWidget(self.retry)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.download = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.download.setText("Download selected recording")
        self.download.setEnabled(False)
        self.download.clicked.connect(self.download_recording)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        available = self.screen().availableGeometry()
        width = min(max(560, buttons.sizeHint().width() + 40), available.width() - 40)
        self.resize(width, min(480, available.height() - 40))
        self.load_recordings()

    def select_recording(self):
        self.allow_m4a.setChecked(False)
        recording = self.recordings.currentData()
        m4a_only = recording is not None and recording.download_url is None
        self.allow_m4a.setVisible(m4a_only)
        self.format_notice.setText("An MP3 copy is not available. AnkiWeb does not show a replay button for M4A." if m4a_only else
                                   "MP3 audio for Anki and AnkiWeb." if recording else "")
        self.update_download_button()

    def update_download_button(self):
        recording = self.recordings.currentData()
        self.download.setEnabled(recording is not None and (recording.download_url is not None or self.allow_m4a.isChecked()))
        self.download.setText("Download MP3" if recording and recording.download_url else
                              "Download M4A" if recording else "Download selected recording")

    def load_recordings(self):
        from .authoring_media import recordings_from_config
        self.retry.hide()
        self.recordings.clear()
        self.recordings.addItem("Choose a recording", None)
        self.status.setText("Finding recordings for this entry...")
        def ready(recordings):
            if self.closed.is_set():
                return
            for recording in recordings:
                self.recordings.addItem(recording.label, recording)
            self.status.setText("" if recordings else "No eligible Shirabe recording is available for this entry.")
        def failed(error):
            if not self.closed.is_set():
                self.status.setText(str(error))
                self.retry.show()
        QueryOp(parent=self, op=lambda _: recordings_from_config(self.config, self.lookup), success=ready).without_collection().failure(failed).run_in_background()

    def download_recording(self):
        from .authoring_media import import_recording
        recording = self.recordings.currentData()
        if recording is None or self.closed.is_set() or not self.download.isEnabled():
            return
        self.download.setEnabled(False)
        self.recordings.setEnabled(False)
        self.allow_m4a.setEnabled(False)
        self.status.setText("Downloading recording... You can cancel without changing your note.")
        def ready(media):
            if not self.closed.is_set():
                self.imported = media
                self.accept()
        def failed(error):
            if not self.closed.is_set():
                self.recordings.setEnabled(True)
                self.allow_m4a.setEnabled(True)
                self.download.setText("Retry download")
                self.download.setEnabled(True)
                self.status.setText(str(error))
        import_recording(self, recording, on_ready=ready, on_error=failed, cancelled=self.closed.is_set,
                         parent=self, allow_m4a=self.allow_m4a.isChecked())


class ChoiceDialog(QDialog):
    def __init__(self, parent, lookup, profile, model, choices=None, *, note=None, provenance=None, duplicates=(), config=None, media=None):
        super().__init__(parent)
        self.setWindowTitle("Add Japanese study material")
        self.resize(740, 640)
        self.setMinimumSize(560, 480)
        self.lookup, self.profile, self.model = lookup, profile, model
        self.media = media
        self.creating, self.config = note is None or not note.id, config
        self.generation = 0
        self.finished.connect(lambda _: setattr(self, "generation", self.generation + 1))
        self.chosen_lookup, self.choices = None, choices or NoteChoices()
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        heading = _label(lookup.lemma or lookup.surface)
        font = heading.font()
        font.setPointSize(max(21, font.pointSize() + 6))
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        if lookup.reading:
            layout.addWidget(_label(lookup.reading))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(form)
        self.kind = QComboBox()
        for label, value in (("Vocabulary", "vocabulary"), ("Sentence", "sentence"), ("Cloze", "cloze")):
            self.kind.addItem(label, value)
        self.kind.setCurrentIndex(self.kind.findData("cloze" if model.get("type") == 1 and self.creating else self.choices.kind))
        form.addRow("Note", self.kind)
        self.headword = QComboBox()
        self.headword.setMinimumContentsLength(10)
        self.headword.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for label, value in ((f"Dictionary form: {lookup.lemma}", "lemma"), (f"As encountered: {lookup.surface}", "surface")):
            self.headword.addItem(label, value)
        if lookup.expression:
            self.headword.addItem(f"Selected expression: {lookup.expression}", "expression")
        self.headword.setCurrentIndex(max(0, self.headword.findData(self.choices.headword)))
        form.addRow("Headword", self.headword)
        self.headword_label = form.labelForField(self.headword)
        self.senses = []
        for i, meaning in enumerate(lookup.meanings):
            row = QHBoxLayout()
            checkbox = QCheckBox()
            checkbox.setAccessibleName(meaning)
            checkbox.setChecked(self.choices.sense_indexes is None or i in self.choices.sense_indexes)
            label = _label(meaning)
            label.setBuddy(checkbox)
            row.addWidget(checkbox)
            row.addWidget(label, 1)
            layout.addLayout(row)
            self.senses.append(checkbox)
        self.example = QComboBox()
        self.example.setMinimumContentsLength(10)
        self.example.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.example.addItem("No sentence", -2)
        if lookup.sentence:
            self.example.addItem("Current sentence", -1)
        for i, example in enumerate(lookup.examples):
            self.example.addItem(f"{example.japanese}  {example.translation}", i)
        self.example.setCurrentIndex(1 if lookup.sentence else 0)
        if self.choices.example_index is not None:
            self.example.setCurrentIndex(self.example.findData(self.choices.example_index))
        form.addRow("Sentence source", self.example)
        if config is not None:
            more = QPushButton("Find more examples...")
            more.setCheckable(True)
            layout.addWidget(more)
            self.example_options = QWidget()
            self.example_options.hide()
            more.toggled.connect(self.example_options.setVisible)
            examples_form = QFormLayout(self.example_options)
            examples_form.setContentsMargins(0, 0, 0, 0)
            examples_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            self.example_language = QComboBox()
            for label, code in (("Account language", None), ("English", "en"), ("Español", "es"), ("日本語", "ja")):
                self.example_language.addItem(label, code)
            examples_form.addRow("Translation", self.example_language)
            self.example_length = QSpinBox()
            self.example_length.setRange(10, 300)
            self.example_length.setValue(120)
            self.example_length.setSuffix(" characters max")
            examples_form.addRow("Sentence length", self.example_length)
            self.search_examples_button = QPushButton("Find examples")
            self.search_examples_button.clicked.connect(self.search_examples)
            examples_form.addRow("", self.search_examples_button)
            layout.addWidget(self.example_options)
            self.example_status = _label("")
            self.example_status.hide()
            layout.addWidget(self.example_status)
        self.sentence = QTextEdit()
        self.sentence.setFixedHeight(68)
        self.sentence.setAcceptRichText(False)
        self.sentence.setPlaceholderText("Choose a sentence above. For cloze, select the exact text to hide here.")
        layout.addWidget(self.sentence)
        self.translation = _label("")
        self.source = _label("")
        layout.addWidget(self.translation)
        layout.addWidget(self.source)
        row = QHBoxLayout()
        self.include_sentence = QCheckBox("Insert sentence")
        self.include_sentence.setChecked(self.choices.include_sentence)
        self.include_translation = QCheckBox("Insert translation")
        self.include_translation.setChecked(self.choices.include_translation)
        row.addWidget(self.include_sentence)
        row.addWidget(self.include_translation)
        layout.addLayout(row)
        audio_row = QHBoxLayout()
        self.audio_choose = QPushButton("Choose recording...")
        self.audio_choose.clicked.connect(self.choose_recording)
        self.audio_clear = QPushButton("Remove selected recording")
        self.audio_clear.clicked.connect(lambda: self.set_recording(None))
        self.audio_clear.setVisible(media is not None)
        audio_row.addWidget(self.audio_choose)
        audio_row.addWidget(self.audio_clear)
        audio_row.addStretch(1)
        layout.addLayout(audio_row)
        self.audio_status = _label("Existing audio is kept. You can append or replace it in the field preview.")
        if not {"audio", "source"} <= profile.fields.keys() or note is None:
            self.audio_status.setText("To add recordings, map Audio and Source in Shirabe's note settings, then reopen this preview. Source keeps the attribution with your exported cards.")
            self.audio_choose.setEnabled(False)
        elif not lookup.entry.startswith("jmdict:"):
            self.audio_status.setText("Choose a JMdict entry to add a Shirabe recording. Other audio sources are not included.")
            self.audio_choose.setEnabled(False)
        layout.addWidget(self.audio_status)
        self.number = QSpinBox()
        self.number.setRange(1, 499)
        self.number.setValue(self.choices.cloze_number)
        form.addRow("Cloze number", self.number)
        self.number_label = form.labelForField(self.number)
        self.example.currentIndexChanged.connect(self.load_example)
        self.load_example()
        self.problem = _label("")
        layout.addWidget(self.problem)
        self.fields = FieldPreview(body, note, profile, lookup, choices=self.choices, provenance=provenance, media=media) if note else None
        if self.fields:
            layout.addWidget(self.fields)
            self.fields.table.setMinimumHeight(150)
            self.fields.table.setMaximumHeight(250)
        if duplicates:
            layout.addWidget(_label(f"{len(duplicates)} possible existing note(s). Continuing creates a separate note."))
            layout.addWidget(_label("\n".join(f"{d.expression} ({d.reading}) · {d.match} · note {d.note_id}" for d in duplicates[:8])))
            browse = QPushButton("View existing notes")
            browse.clicked.connect(lambda: _browse_ids(parent.mw if hasattr(parent, "mw") else parent, [d.note_id for d in duplicates]))
            layout.addWidget(browse)
        layout.addStretch(1)
        self.buttons = _buttons(self, outer, "Apply changes" if note and note.id else "Open in Anki editor")
        for box in (self.kind, self.headword):
            box.currentIndexChanged.connect(self.refresh_fields)
        for checkbox in (*self.senses, self.include_sentence, self.include_translation):
            checkbox.toggled.connect(self.refresh_fields)
        self.sentence.textChanged.connect(self.refresh_fields)
        self.sentence.selectionChanged.connect(self.refresh_fields)
        self.example.currentIndexChanged.connect(self.refresh_fields)
        self.number.valueChanged.connect(self.refresh_fields)
        if self.fields:
            for box in self.fields.actions.values():
                box.currentIndexChanged.connect(self.refresh_fields)
        self.refresh_fields()

    def choose_recording(self):
        if not self.audio_choose.isEnabled():
            return
        generation = self.generation
        picker = RecordingDialog(self, self.lookup, _latest_config(self.config or {}))
        if picker.exec() == QDialog.DialogCode.Accepted and self.generation == generation:
            self.set_recording(picker.imported)

    def set_recording(self, media):
        self.media = media
        self.audio_clear.setVisible(media is not None)
        self.audio_status.setText("Recording downloaded. Review the Audio and Source fields before saving." if media else
                                 "Existing audio is kept. You can append or replace it in the field preview.")
        if self.fields:
            self.fields.media = media
            if media is not None and "source" in self.fields.actions:
                source = self.fields.actions["source"]
                if source.currentData() == "fill":
                    source.setCurrentIndex(source.findData("append"))
        self.refresh_fields()

    def load_example(self, *_):
        index = self.example.currentData()
        if index >= 0:
            example = self.lookup.examples[index]
            sentence, translation = example.japanese, example.translation
            source = "\n".join(s for s in (self.lookup.source, example.source, example.reference) if s)
        else:
            sentence = self.lookup.sentence if index == -1 else ""
            translation = self.lookup.translation if index == -1 else ""
            source = self.lookup.source
        self.sentence.setPlainText(sentence)
        self.translation.setText(translation)
        self.translation.setVisible(bool(translation))
        self.source.setText(source)
        self.source.setVisible(bool(source))

    def search_examples(self):
        from .authoring_examples import search_examples_from_config
        self.search_examples_button.setEnabled(False)
        generation = self.generation
        config = _latest_config(self.config or {})
        lookup = self.lookup
        language, limit = self.example_language.currentData(), self.example_length.value()
        def ready(examples):
            if self.generation != generation:
                return
            self.search_examples_button.setEnabled(True)
            existing = {(e.japanese, e.translation) for e in self.lookup.examples}
            added = tuple(e for e in examples if (e.japanese, e.translation) not in existing)
            start = len(self.lookup.examples)
            self.lookup = replace(self.lookup, examples=(*self.lookup.examples, *added)[:50])
            for index, example in enumerate(self.lookup.examples[start:], start):
                self.example.addItem(f"{example.japanese}  {example.translation}", index)
            count = len(self.lookup.examples) - start
            noun = "example" if count == 1 else "examples"
            self.example_status.setText(f"Found {count} new {noun}. Choose a sentence above." if count else "No additional examples match these filters.")
            self.example_status.show()
        def failed(error):
            if self.generation != generation:
                return
            self.search_examples_button.setEnabled(True)
            _connection_error(self, error)
        QueryOp(parent=self, op=lambda _: search_examples_from_config(config, lookup, language=language, max_length=limit),
                success=ready).without_collection().failure(failed).run_in_background()

    def refresh_fields(self, *_):
        cloze = self.kind.currentData() == "cloze"
        sentence = self.kind.currentData() == "sentence"
        self.headword.setVisible(not sentence)
        self.headword_label.setVisible(not sentence)
        self.number.setVisible(cloze)
        self.number_label.setVisible(cloze)
        try:
            self.chosen_lookup = replace(self.lookup, sentence=self.sentence.toPlainText(),
                                         translation=self.translation.text(), source=self.source.text())
            span = None
            if self.kind.currentData() == "cloze":
                cursor = self.sentence.textCursor()
                span = utf16_span(self.chosen_lookup.sentence, cursor.selectionStart(), cursor.selectionEnd())
                self.chosen_lookup = replace(self.chosen_lookup, surface=self.chosen_lookup.sentence[slice(*span)])
            self.choices = NoteChoices(kind=self.kind.currentData(), headword=self.headword.currentData(),
                                       sense_indexes=tuple(i for i, box in enumerate(self.senses) if box.isChecked()),
                                       cloze_span=span, cloze_number=self.number.value(),
                                       include_sentence=self.include_sentence.isChecked(),
                                       include_translation=self.include_translation.isChecked())
            from .authoring import proposed_fields
            proposed_fields(self.chosen_lookup, self.profile, self.model, self.choices, creating=self.creating)
            if self.fields:
                self.fields.lookup, self.fields.choices = self.chosen_lookup, self.choices
                self.fields.update_preview()
                if self.fields.preview is None:
                    raise AuthoringError("The selected material could not be previewed. Remove the recording or try again.")
                _check_recording_attribution(self.fields.preview, self.profile, self.media)
            self.problem.setText("")
            self.problem.hide()
            self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(True)
        except AuthoringError as error:
            self.problem.setText(str(error))
            self.problem.show()
            self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)

    @property
    def preview(self):
        return self.fields.preview if self.fields else None

    def accept(self):
        self.refresh_fields()
        if not self.problem.text():
            super().accept()


class FieldPreview(QWidget):
    def __init__(self, parent, note, profile, lookup, *, choices=None, provenance=None, policies=None, media=None):
        super().__init__(parent)
        self.note, self.profile, self.lookup = note, profile, lookup
        self.choices, self.provenance = choices, provenance
        self.media = media
        self.policies = dict(policies or {})
        self.preview = None
        layout = QVBoxLayout(self)
        layout.addWidget(_label("Fill empty keeps your text. Append or replace fields individually."))
        self.show_html = QCheckBox("Show HTML")
        self.show_html.toggled.connect(self.update_preview)
        layout.addWidget(self.show_html)
        self.table = QTableWidget(len(profile.fields), 4)
        self.table.setHorizontalHeaderLabels(["Field", "Current", "Action", "After"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.actions = {}
        for row, (role, name) in enumerate(profile.fields.items()):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(note[name]))
            box = QComboBox()
            for label, value in (("Keep", "keep"), ("Fill empty", "fill"), ("Append", "append"),
                                 ("Replace", "replace"), ("Refresh generated", "refresh")):
                box.addItem(label, value)
            box.setCurrentIndex(box.findData(self.policies.get(role, "fill")))
            self.table.setCellWidget(row, 2, box)
            self.actions[role] = box
            box.currentIndexChanged.connect(self.update_preview)
        self.tags = _label("")
        layout.addWidget(self.tags)
        self.update_preview()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.resizeColumnsToContents()

    def update_preview(self, *_):
        self.policies = {role: box.currentData() for role, box in self.actions.items()}
        try:
            self.preview = build_preview(self.note, self.profile, self.lookup, choices=self.choices,
                                         policies=self.policies, provenance=self.provenance, media=self.media)
        except AuthoringError:
            self.preview = None
            return
        for row, change in enumerate(self.preview.changes):
            display = (lambda value: value) if self.show_html.isChecked() else plain_text
            self.table.setItem(row, 1, QTableWidgetItem(display(change.before)))
            item = QTableWidgetItem(display(change.after))
            if change.reason:
                item.setToolTip("Your correction is preserved." if change.reason == "user-edited" else "This field has no generated-field history.")
            self.table.setItem(row, 3, item)
        self.table.resizeRowsToContents()
        self.tags.setText("Tags after update: " + " ".join(self.preview.tags))


class PreviewDialog(QDialog):
    def __init__(self, parent, note, profile, lookup, **kwargs):
        super().__init__(parent)
        self.setWindowTitle("Preview Anki fields")
        available = self.screen().availableGeometry()
        self.resize(min(1000, available.width() - 40), min(560, available.height() - 40))
        layout = QVBoxLayout(self)
        self.fields = FieldPreview(self, note, profile, lookup, **kwargs)
        layout.addWidget(self.fields)
        _buttons(self, layout, "Apply changes")

    @property
    def preview(self):
        return self.fields.preview

    def accept(self):
        if self.preview is None:
            showWarning("Choose valid fields before applying.", parent=self)
            return
        super().accept()


class BatchPreviewDialog(QDialog):
    def __init__(self, parent, rows, *, policies=None, config=None):
        super().__init__(parent)
        self.setWindowTitle("Preview selected-note changes")
        available = self.screen().availableGeometry()
        self.resize(min(920, available.width() - 40), min(620, available.height() - 40))
        self.config = config or {}
        self.rows = [r for r in rows if r.status == "ready"]
        self.previews = []
        self.choices = {r.snapshot.note_id: NoteChoices() for r in self.rows}
        self.lookups = {r.snapshot.note_id: r.candidates[r.selected] for r in self.rows}
        self.media = {r.snapshot.note_id: None for r in self.rows}
        self.policies = {r.snapshot.note_id: {role: (policies or {}).get(role, "fill") for role in r.profile.fields} for r in self.rows}
        layout = QVBoxLayout(self)
        layout.addWidget(_label("Review the selection together. Anki Undo restores this whole update."))
        top = QVBoxLayout()
        self.note = QComboBox()
        self.note.setMinimumContentsLength(10)
        self.note.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for row in self.rows:
            self.note.addItem(f"{row.identity.expression} · note {row.snapshot.note_id}", row.snapshot.note_id)
        top.addWidget(self.note)
        material = QPushButton("Choose meanings or example")
        material.clicked.connect(self.choose_material)
        top.addWidget(material)
        refresh = QPushButton("Refresh generated fields")
        refresh.clicked.connect(self.refresh_generated)
        top.addWidget(refresh)
        layout.addLayout(top)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Note", "Field", "Current", "Action", "After"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.problem = _label("")
        layout.addWidget(self.problem)
        self.buttons = _buttons(self, layout, "Apply selected-note changes")
        self.draw()

    def draw(self):
        self.previews = []
        self.table.setRowCount(sum(len(row.profile.fields) + 1 for row in self.rows))
        index = 0
        for row in self.rows:
            nid = row.snapshot.note_id
            preview = build_preview(row.note_copy(), row.profile, self.lookups[nid], choices=self.choices[nid],
                                    provenance=row.provenance, policies=self.policies[nid], media=self.media[nid])
            self.previews.append(preview)
            for change in preview.changes:
                self.table.setItem(index, 0, QTableWidgetItem(f"{row.identity.expression} · {nid}"))
                self.table.setItem(index, 1, QTableWidgetItem(change.field))
                self.table.setItem(index, 2, QTableWidgetItem(plain_text(change.before)))
                self.table.setItem(index, 4, QTableWidgetItem(plain_text(change.after)))
                box = QComboBox()
                for label, policy in (("Keep", "keep"), ("Fill empty", "fill"), ("Append", "append"), ("Replace", "replace"), ("Refresh generated", "refresh")):
                    box.addItem(label, policy)
                box.setCurrentIndex(box.findData(change.policy))
                def changed(_, nid=nid, role=change.role, box=box):
                    self.policies[nid][role] = box.currentData()
                    self.update_values()
                box.currentIndexChanged.connect(changed)
                self.table.setCellWidget(index, 3, box)
                index += 1
            self.table.setItem(index, 1, QTableWidgetItem("Tags"))
            self.table.setItem(index, 2, QTableWidgetItem(" ".join(preview.snapshot.tags)))
            self.table.setItem(index, 4, QTableWidgetItem(" ".join(preview.tags)))
            index += 1
        self.table.resizeColumnsToContents()
        self.table.resizeRowsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.check_approval()

    def update_values(self):
        previews, index = [], 0
        for row in self.rows:
            nid = row.snapshot.note_id
            preview = build_preview(row.note_copy(), row.profile, self.lookups[nid], choices=self.choices[nid],
                                    provenance=row.provenance, policies=self.policies[nid], media=self.media[nid])
            previews.append(preview)
            for change in preview.changes:
                self.table.setItem(index, 4, QTableWidgetItem(plain_text(change.after)))
                index += 1
            index += 1
        self.previews = previews
        self.table.resizeRowsToContents()
        self.check_approval()

    def check_approval(self):
        try:
            for row, preview in zip(self.rows, self.previews):
                _check_recording_attribution(preview, row.profile, self.media[row.snapshot.note_id])
            self.problem.setText("")
        except AuthoringError as error:
            self.problem.setText(str(error))
        valid = not self.problem.text()
        self.problem.setVisible(not valid)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(valid)
        return valid

    def accept(self):
        if self.check_approval():
            super().accept()

    def choose_material(self):
        row = next(r for r in self.rows if r.snapshot.note_id == self.note.currentData())
        nid = row.snapshot.note_id
        dialog = ChoiceDialog(self, self.lookups[nid], row.profile, row.model, self.choices[nid],
                              note=row.note_copy(), provenance=row.provenance, config=self.config, media=self.media[nid])
        for role, policy in self.policies[nid].items():
            box = dialog.fields.actions[role]
            box.setCurrentIndex(box.findData(policy))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.lookups[nid], self.choices[nid] = dialog.chosen_lookup, dialog.choices
            self.media[nid] = dialog.media
            self.policies[nid] = dialog.fields.policies
            self.draw()

    def refresh_generated(self):
        for policies in self.policies.values():
            for role in policies:
                policies[role] = "refresh"
        self.draw()


class _Completion:
    def __init__(self, parent, on_closed=None, on_error=None):
        self.parent, self.on_closed, self.on_error = parent, on_closed, on_error
        self.closed = False

    def finish(self):
        if not self.closed:
            self.closed = True
            if self.on_closed:
                self.on_closed()

    def fail(self, error):
        try:
            if self.on_error:
                self.on_error(error)
            else:
                showWarning(str(error), parent=self.parent)
        finally:
            self.finish()

    def guard(self, callback):
        def guarded(*args):
            try:
                return callback(*args)
            except Exception as error:
                self.fail(error)
        return guarded


def _after_editor_ready(editor, callback, *, on_error, on_closed=lambda: None):
    web, note = editor.web, editor.note
    if note is None:
        callback()
        return
    timer = QTimer(web)
    timer.setInterval(50)
    deadline = monotonic() + 15
    finished, pending = False, False

    def finish(action):
        nonlocal finished
        if finished:
            return
        finished = True
        timer.stop()
        timer.deleteLater()
        web.destroyed.disconnect(closed)
        action()

    def closed(*_):
        nonlocal finished
        if not finished:
            finished = True
            on_closed()

    def received(ready):
        nonlocal pending
        pending = False
        if finished:
            return
        if editor.note is not note:
            finish(on_closed)
        elif ready is True:
            finish(callback)

    def check():
        nonlocal pending
        if finished:
            return
        if monotonic() >= deadline:
            finish(lambda: on_error(AuthoringError("The Anki editor is still loading. Close and reopen it, then try again.")))
        elif editor.note is not note:
            finish(on_closed)
        elif not pending:
            pending = True
            # Anki's DOM-ready signal precedes its async Editor mount, which defines saveNow.
            try:
                web.evalWithCallback(f'typeof saveNow === "function" && typeof getNoteId === "function" && getNoteId() === {int(note.id)}', received)
            except Exception as error:
                finish(lambda: on_error(error))

    web.destroyed.connect(closed)
    timer.timeout.connect(check)
    timer.start()
    check()


def _open_add_draft(mw, note, profile, completion, *, preview, config, save_config):
    window = dialogs.open("AddCards", mw)

    def fill():
        try:
            if window.editor.note is None:
                return
            if any(not field_is_empty(value) for value in window.editor.note.fields):
                showWarning("The Add window already has a draft. Save or clear it before adding this word.", parent=window)
                return
            window.set_note(note, deck_id=profile.deck_id)
            tracked = window.editor.note

            def cleanup(*_):
                try:
                    gui_hooks.add_cards_did_add_note.remove(added)
                except ValueError:
                    pass

            def added(saved):
                if saved is not tracked:
                    return
                cleanup()
                undo_step = mw.col.undo_status().last_step
                if saved.mid == profile.note_type_id:
                    remembered = replace(profile, deck_id=window.deck_chooser.selected_deck_id, tags=tuple(saved.tags))
                    save_config(remember_profile(_latest_config(config or {}), remembered))
                CollectionOp(window, lambda col: record_created_note(col, saved.id, preview, undo_step=undo_step)).run_in_background()

            gui_hooks.add_cards_did_add_note.append(added)
            window.destroyed.connect(cleanup)
            window.activateWindow()
        finally:
            completion.finish()
    _flush_editors([window.editor], completion.guard(fill), on_error=completion.fail, on_closed=completion.finish)
    return window


def open_add_note(mw, lookup, profile=None, *, config=None, save_config=lambda _: None, parent=None,
                  choices=None, on_closed=None, on_error=None):
    completion = _Completion(parent or mw, on_closed, on_error)
    try:
        result = _prepare_add_note(mw, lookup, profile, config=config, save_config=save_config,
                                   parent=parent, choices=choices, completion=completion)
        if result is None:
            completion.finish()
        return result
    except Exception as error:
        completion.fail(error)


def _prepare_add_note(mw, lookup, profile, *, config, save_config, parent, choices, completion):
    parent = parent or mw
    profile = profile or choose_profile(mw, config or {}, save_config, parent=parent)
    if profile is None:
        return None
    model = mw.col.models.get(profile.note_type_id)
    note = mw.col.new_note(model)
    from .collection import find_existing_notes
    duplicates = find_existing_notes(mw.col, lookup, profiles_from_config(config or {}) or [profile])
    preview = ChoiceDialog(parent, lookup, profile, model, choices, note=note, duplicates=duplicates, config=config or {})
    if preview.exec() != QDialog.DialogCode.Accepted:
        return None
    for name, value in preview.preview.field_values().items():
        note[name] = value
    note.tags = list(preview.preview.tags)
    return _open_add_draft(mw, note, profile, completion, preview=preview.preview, config=config, save_config=save_config)


def _all_note_editors(mw, note_id, editor=None):
    editors = [editor] if editor is not None else []
    for window in mw.app.topLevelWidgets():
        candidate = getattr(window, "editor", None)
        if candidate and getattr(candidate, "note", None) and candidate.note.id == note_id and candidate not in editors:
            editors.append(candidate)
    return editors


def _flush_editors(editors, callback, *, on_error=None, on_closed=lambda: None):
    if not editors:
        callback()
    else:
        editor = editors[0]
        on_error = on_error or (lambda error: showWarning(str(error), parent=editor.parentWindow))
        def save():
            closed = lambda *_: on_closed()
            editor.web.destroyed.connect(closed)
            def saved():
                editor.web.destroyed.disconnect(closed)
                _flush_editors(editors[1:], callback, on_error=on_error, on_closed=on_closed)
            try:
                editor.call_after_note_saved(saved)
            except Exception as error:
                editor.web.destroyed.disconnect(closed)
                on_error(error)
        _after_editor_ready(editor, save, on_error=on_error, on_closed=on_closed)


def preview_update(mw, note_id, lookup, profile=None, *, config=None, save_config=lambda _: None,
                   parent=None, editor=None, choices=None, on_done=None, on_closed=None, on_error=None):
    parent = parent or mw
    completion = _Completion(parent, on_closed, on_error)
    if not note_id and editor is None:
        completion.fail(AuthoringError("Use the Add window's editor for an unsaved note."))
        return
    editors = _all_note_editors(mw, note_id, editor)

    def start():
        def read(col):
            return NoteDraft.capture(col.get_note(note_id)), provenance_for(col, note_id)

        def ready(result):
            note, provenance = result
            selected = profile or choose_profile(mw, config or {}, save_config, note_type_id=note.mid, parent=parent)
            if selected is None:
                completion.finish()
                return
            drafts = [(e, NoteSnapshot.capture(e.note)) for e in editors if e.note is not None]
            preview = ChoiceDialog(parent, lookup, selected, note.note_type(), choices, note=note, provenance=provenance, config=config or {})
            if preview.exec() != QDialog.DialogCode.Accepted:
                completion.finish()
                return

            def apply():
                if any(e.note is None or not snapshot.matches(e.note) for e, snapshot in drafts):
                    showWarning("The editor changed during lookup. Review a fresh preview before applying.", parent=parent)
                    completion.finish()
                    return
                if not note_id:
                    if not preview.preview.snapshot.matches(editor.note):
                        showWarning("The draft changed. Review a fresh preview.", parent=parent)
                        completion.finish()
                        return
                    for name, value in preview.preview.field_values().items():
                        editor.note[name] = value
                    editor.note.tags = list(preview.preview.tags)
                    editor.set_note(editor.note)
                    completion.finish()
                    return
                operation = CollectionOp(parent, lambda col: apply_previews(col, [preview.preview]))
                def done(result):
                    try:
                        (on_done or (lambda r: _show_result(parent, r)))(result)
                    finally:
                        completion.finish()
                operation.success(completion.guard(done)).failure(completion.fail)
                operation.run_in_background()
            _flush_editors(editors, completion.guard(apply), on_error=completion.fail, on_closed=completion.finish)

        if note_id:
            QueryOp(parent=parent, op=read, success=completion.guard(ready)).failure(completion.fail).run_in_background()
        elif editor.note is not None:
            completion.guard(ready)((NoteDraft.capture(editor.note), {}))
        else:
            completion.finish()
    try:
        _flush_editors(editors, completion.guard(start), on_error=completion.fail, on_closed=completion.finish)
    except Exception as error:
        completion.fail(error)


def _show_result(parent, result):
    message = f"Updated {len(result.applied)} note(s). Undo restores these fields and tags."
    if result.conflicts:
        message += f"\n{len(result.conflicts)} note(s) changed since preview and were skipped."
    showInfo(message, parent=parent)


def _browse_ids(mw, ids):
    from anki.collection import SearchNode
    browser = dialogs.open("Browser", mw)
    browser.search_for(mw.col.build_search_string(SearchNode(nids=SearchNode.IdList(ids=ids))))
    return browser


def show_existing_notes(mw, lookup, profiles, *, parent=None):
    from .collection import find_existing_notes
    matches = find_existing_notes(mw.col, lookup, profiles)
    if not matches:
        showInfo("No matching notes found in your mapped note types.", parent=parent or mw)
        return None
    return _browse_ids(mw, [match.note_id for match in matches])


class EditorLookupDialog(QDialog):
    def __init__(self, editor, config, save_config):
        super().__init__(editor.parentWindow)
        self.editor, self.config, self.save_config = editor, config, save_config
        self.setWindowTitle("Look up Japanese")
        self.candidates, self.snapshot = (), None
        self.generation = 0
        outer = QVBoxLayout(self)
        content = QScrollArea()
        content.setWidgetResizable(True)
        content.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        content.setWidget(body)
        outer.addWidget(content)
        layout.addWidget(_label("Only the text you enter here is sent to Shirabe when you choose Search."))
        self.query = QLineEdit()
        layout.addWidget(self.query)
        self.search = QPushButton("Search")
        self.search.clicked.connect(self.search_word)
        self.query.returnPressed.connect(self.search_word)
        layout.addWidget(self.search)
        self.status = _label("")
        layout.addWidget(self.status)
        self.connect = QPushButton("Connect to Shirabe")
        self.connect.clicked.connect(lambda: _connect_to_shirabe(self))
        self.connect.hide()
        layout.addWidget(self.connect)
        self.results = QComboBox()
        self.results.setMinimumContentsLength(10)
        self.results.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.results)
        self.definition = _label("")
        self.results.currentIndexChanged.connect(self.show_definition)
        layout.addWidget(self.definition)
        self.use = QPushButton("Preview in this note")
        self.use.setEnabled(False)
        self.use.clicked.connect(self.use_candidate)
        layout.addWidget(self.use)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        outer.addWidget(close)
        available = self.screen().availableGeometry()
        self.resize(min(640, available.width() - 40), min(520, available.height() - 40))
        self.finished.connect(lambda _: setattr(self, "generation", self.generation + 1))
        def selection():
            if self.generation == 0 and not self.query.isModified():
                self.query.setText(editor.web.selectedText()[:256])
        _after_editor_ready(editor, selection, on_error=lambda error: self.failed(0, error))

    def search_word(self):
        from .authoring import _text
        from .authoring_batch import LookupIdentity
        from .authoring_lookup import resolver_from_config
        try:
            query = _text(self.query.text().strip(), "word", 256, required=True)
            if "{{" in query or "}}" in query:
                raise AuthoringError("Enter the visible word without cloze markup.")
            self.config = _latest_config(self.config)
            resolve = resolver_from_config(self.config)
        except Exception as error:
            self.failed(self.generation, error)
            return
        self.connect.hide()
        self.search.setEnabled(False)
        self.use.setEnabled(False)
        self.generation += 1
        generation = self.generation
        self.status.setText("Looking up your selection...")

        def start():
            if self.editor.note is None or self.generation != generation:
                return
            self.snapshot = NoteSnapshot.capture(self.editor.note)
            QueryOp(parent=self, op=lambda _: resolve(LookupIdentity(query)),
                    success=lambda candidates: self.received(generation, candidates)).without_collection().failure(
                        lambda error: self.failed(generation, error)).run_in_background()
        _flush_editors([self.editor], start, on_error=lambda error: self.failed(generation, error))

    def received(self, generation, candidates):
        if self.generation != generation:
            return
        self.candidates = candidates
        self.results.clear()
        self.results.addItem("Choose a dictionary entry", None)
        for index, candidate in enumerate(candidates):
            self.results.addItem(f"{candidate.lemma} ({candidate.reading}) {candidate.entry}", index)
        self.search.setEnabled(True)
        self.status.setText("Choose the entry you meant." if candidates else "No dictionary matches. Try the dictionary form.")

    def failed(self, generation, error):
        if self.generation == generation:
            self.search.setEnabled(True)
            reconnect = getattr(error, "code", "") == "reconnect_required"
            existing = bool(self.config.get("cardToken"))
            message = ("Use Tools → Shirabe → Settings → Reconnect, then search again." if existing
                       else "Connect to Shirabe, then search again.")
            self.status.setText(message if reconnect else str(error))
            self.connect.setText("Reconnect to Shirabe" if existing else "Connect to Shirabe")
            self.connect.setVisible(reconnect)

    def show_definition(self, *_):
        index = self.results.currentData()
        self.use.setEnabled(index is not None)
        self.definition.setText("\n".join(self.candidates[index].meanings) if index is not None else "")

    def use_candidate(self):
        index = self.results.currentData()
        if index is None:
            return
        def use():
            if self.editor.note is None or self.snapshot is None or not self.snapshot.matches(self.editor.note):
                self.status.setText("The editor changed during lookup. Search again before updating.")
                self.use.setEnabled(False)
                return
            candidate = self.candidates[index]
            self.accept()
            preview_update(self.editor.mw, self.editor.note.id, candidate, config=self.config,
                           save_config=self.save_config, editor=self.editor, parent=self.editor.parentWindow)
        _flush_editors([self.editor], use, on_error=lambda error: self.failed(self.generation, error))


def open_editor_lookup(editor, config, save_config):
    if editor.note is None:
        return None
    dialog = EditorLookupDialog(editor, config, save_config)
    dialog.exec()
    return dialog


def enrich_browser_selection(browser, config, save_config):
    from .authoring_lookup import resolver_from_config
    profiles = profiles_from_config(config)
    if not profiles:
        profile = configure_profiles(browser.mw, config, save_config, parent=browser)
        if profile is None:
            return None
        profiles = [profile]
    try:
        resolve = resolver_from_config(_latest_config(config))
    except Exception as error:
        _connection_error(browser, error)
        return None
    return enrich_selected_notes(browser, profiles, resolve, config=config, configure_profile=lambda mid: configure_profiles(
        browser.mw, _latest_config(config), save_config, note_type_id=mid, parent=browser))


def _latest_config(fallback):
    read = getattr(sys.modules.get(__package__), "config", None)
    return read() if read else fallback


def _connect_to_shirabe(parent):
    connect = getattr(sys.modules.get(__package__), "open_sign_in", None)
    if connect:
        connect()
    else:
        showInfo("Open Shirabe settings and choose Sign in, then try again.", parent=parent)


def _connection_error(parent, error):
    if getattr(error, "code", "") != "reconnect_required":
        showWarning(str(error), parent=parent)
        return
    dialog = QDialog(parent)
    dialog.setWindowTitle("Connect to Shirabe")
    layout = QVBoxLayout(dialog)
    layout.addWidget(_label("Connect to Shirabe before looking up your selected words. If you are already connected, use Tools → Shirabe → Settings → Reconnect, then run enrichment again."))
    connect = QPushButton("Connect to Shirabe")
    connect.clicked.connect(lambda: (dialog.accept(), _connect_to_shirabe(parent)))
    layout.addWidget(connect)
    close = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    close.rejected.connect(dialog.reject)
    layout.addWidget(close)
    dialog.exec()


def enrich_selected_notes(browser, profiles, resolve, *, on_done=None, policies=None, cancelled=None, configure_profile=None, config=None):
    from .authoring_batch import Batch
    batch = Batch()
    ids = tuple(browser.selected_notes())
    mw = browser.mw

    def captured(rows):
        batch.rows = rows
        if any(row.status in {"choose-profile", "unmapped", "missing-identity"} for row in rows) and not choose_mappings():
            return
        run_lookups()

    def choose_mappings():
        dialog = QDialog(browser)
        dialog.setWindowTitle("Choose field mappings for selected notes")
        layout = QVBoxLayout(dialog)
        layout.addWidget(_label("Choose the word fields for each note. Only mapped identities will be looked up."))
        rows = [r for r in batch.rows if r.status in {"choose-profile", "unmapped", "missing-identity"}]
        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(["Note", "Field mapping", "Status"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(table)
        for index, row in enumerate(rows):
            table.setItem(index, 0, QTableWidgetItem(f"{row.snapshot.note_id}: {row.model['name']}"))
            table.setItem(index, 2, QTableWidgetItem(row.status))
            box = QComboBox()
            box.addItem("Skip this note", None)
            for profile in profiles:
                if profile.note_type_id == row.snapshot.note_type_id:
                    box.addItem(profile.name, profile)
            if configure_profile:
                box.addItem("Configure fields...", "configure")
            def chosen(_, row=row, box=box, index=index):
                profile = box.currentData()
                if profile == "configure":
                    profile = configure_profile(row.snapshot.note_type_id)
                    if profile is not None:
                        box.insertItem(box.count() - 1, profile.name, profile)
                        box.setCurrentIndex(box.count() - 2)
                        return
                    box.setCurrentIndex(0)
                if profile is None:
                    row.status = "skipped"
                else:
                    try:
                        row.set_profile(profile)
                    except AuthoringError as error:
                        row.status = "choose-profile"
                        showWarning(str(error), parent=dialog)
                table.setItem(index, 2, QTableWidgetItem(row.status))
            box.currentIndexChanged.connect(chosen)
            table.setCellWidget(index, 1, box)
        table.resizeColumnsToContents()
        available = dialog.screen().availableGeometry()
        dialog.resize(min(800, available.width() - 40), min(350, available.height() - 40))
        _buttons(dialog, layout, "Look up mapped notes")
        return dialog.exec() == QDialog.DialogCode.Accepted

    def run_lookups():
        progress = QProgressDialog("Looking up selected words", "Cancel", 0, len(batch.rows), browser)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.canceled.connect(batch.cancel)
        def update(done, total):
            mw.taskman.run_on_main(lambda: progress.setValue(done))
        def finished(rows):
            progress.reset()
            prepared(rows)
        def failed(error):
            progress.reset()
            showWarning(str(error), parent=browser)
        QueryOp(parent=browser, op=lambda _: batch.prepare(resolve, progress=update), success=finished).without_collection().failure(failed).run_in_background()

    def prepared(_):
        if batch.cancelled():
            showInfo("Enrichment cancelled. No fields were changed.", parent=browser)
            return
        review = QDialog(browser)
        review.setWindowTitle("Selected-note enrichment")
        layout = QVBoxLayout(review)
        table = QTableWidget(len(batch.rows), 3)
        table.setHorizontalHeaderLabels(["Note", "Status", "Dictionary match"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(table)
        for index, row in enumerate(batch.rows):
            table.setItem(index, 0, QTableWidgetItem(f"{row.snapshot.note_id}: {row.identity.expression if row.identity else ''}"))
            table.setItem(index, 1, QTableWidgetItem(row.status))
            if row.candidates:
                box = QComboBox()
                box.addItem("Skip this note", None)
                for i, candidate in enumerate(row.candidates):
                    box.addItem(f"{candidate.lemma} ({candidate.reading}) {candidate.entry}", i)
                box.setCurrentIndex(row.selected + 1 if row.selected is not None else 0)
                def choose(_, row=row, box=box):
                    index = box.currentData()
                    if index is None:
                        row.status, row.selected = "skipped", None
                    else:
                        row.choose(index)
                box.currentIndexChanged.connect(choose)
                table.setCellWidget(index, 2, box)
        table.resizeColumnsToContents()
        available = review.screen().availableGeometry()
        review.resize(min(900, available.width() - 40), min(400, available.height() - 40))
        retry = QPushButton("Retry failed lookups")
        retry.setEnabled(any(row.status == "failed" for row in batch.rows))
        retry.clicked.connect(lambda: review.done(2))
        layout.addWidget(retry)
        mapping = QPushButton("Choose missing field mappings")
        mapping.setEnabled(any(row.status in {"choose-profile", "unmapped", "missing-identity"} for row in batch.rows))
        mapping.clicked.connect(lambda: review.done(3))
        layout.addWidget(mapping)
        _buttons(review, layout, "Review fields")
        outcome = review.exec()
        if outcome == 2:
            run_lookups()
            return
        if outcome == 3:
            if choose_mappings():
                run_lookups()
            return
        if outcome != QDialog.DialogCode.Accepted:
            return
        if not any(row.status == "ready" for row in batch.rows):
            return
        preview = BatchPreviewDialog(browser, batch.rows, policies=policies, config=config)
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = preview.previews
        editors = list(dict.fromkeys(e for p in chosen for e in _all_note_editors(mw, p.snapshot.note_id)))
        _flush_editors(editors, lambda: CollectionOp(browser, lambda col: apply_previews(col, chosen, cancelled=batch.cancelled)).success(
            on_done or (lambda result: _show_result(browser, result))).run_in_background())

    if cancelled is not None:
        batch.external_cancelled = cancelled
    _flush_editors([browser.editor], lambda: QueryOp(parent=browser, op=lambda col: batch.capture(col, ids, profiles), success=captured).run_in_background())
    return batch
