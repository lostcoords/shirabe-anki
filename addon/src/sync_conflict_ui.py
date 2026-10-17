import json

from aqt.qt import QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget

from .sync_cards import auxiliary
from .sync_changes import MappingRequired, SyncChanges
from .sync_snapshot import SnapshotConflict
from .sync_ui import label


class TemplateChecksDialog(QDialog):
    def __init__(self, parent, reports):
        super().__init__(parent)
        self.setWindowTitle("Template source checks")
        layout = QVBoxLayout(self)
        layout.addWidget(label("These reports describe the last accepted note-type revision. "
                               "They do not run JavaScript, inspect actual note values or check media. "
                               "Reports show up to 20 source issues per note type and do not block sync. "
                               "Preview actual cards in Shirabe before relying on their answers."))
        source = QPlainTextEdit()
        source.setReadOnly(True)
        lines = []
        for report in reports:
            lines.append(f"{report['name']} (Anki ID {report['id']}, revision {report['revision']})")
            if report["issues"] is None:
                lines.append("No report for this revision.")
            elif not report["issues"]:
                lines.append("No source issues reported. This does not establish script or card compatibility.")
            else:
                for issue in report["issues"]:
                    lines.append(f"Template {issue['template'] + 1}, {issue['side']}: {issue['code']} - {issue['detail']}")
            lines.append("")
        source.setPlainText("\n".join(lines) if lines else "No accepted note types yet.")
        layout.addWidget(source)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        screen = self.screen().availableGeometry()
        self.resize(min(720, screen.width() - 40), min(600, screen.height() - 40))


class TypeMappingDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Match fields and templates")
        self.prepared = prepared
        self.choices = {}
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = QVBoxLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        content.addWidget(label(prepared["current"]["name"]))
        content.addWidget(label("Match each current Anki item to its previous Shirabe item. "
                               "Choose New only when it is not a rename. Unmatched fields lose their current values in Shirabe; "
                               "unmatched templates retire their cards there. Existing history is retained. Anki is not changed by this choice."))
        tabs = QTabWidget()
        content.addWidget(tabs)
        for name, group, title in (("field_map", "flds", "Fields"), ("template_map", "tmpls", "Templates")):
            previous, current = prepared["previous"][group], prepared["current"][group]
            try:
                suggested = SyncChanges._mapping(previous, current)
            except MappingRequired:
                suggested = ["choose"] * len(current)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            body = QWidget()
            items = QVBoxLayout(body)
            scroll.setWidget(body)
            choices = []
            for index, item in enumerate(current):
                items.addWidget(label(f"Anki {index + 1}: {item['name']}"))
                choice = QComboBox()
                choice.setAccessibleName(f"Previous {title.lower()}: {item['name']}")
                choice.addItem("Choose previous item…", "choose")
                choice.addItem("New item (not a rename)", None)
                for ordinal, old in enumerate(previous):
                    choice.addItem(f"{ordinal + 1}: {old['name']}", ordinal)
                choice.setCurrentIndex(choice.findData(suggested[index]))
                items.addWidget(choice)
                choices.append(choice)
            self.choices[name] = choices
            tabs.addTab(scroll, title)
        self.removals = label("")
        content.addWidget(self.removals)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm mapping")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave pending")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for choices in self.choices.values():
            for choice in choices:
                choice.currentIndexChanged.connect(self._validate)
        self.accepted.connect(lambda: submit(self._mappings()))
        self._validate()
        screen = self.screen().availableGeometry()
        self.resize(min(720, screen.width() - 40), min(700, screen.height() - 40))

    def _mappings(self):
        return {name: [choice.currentData() for choice in choices] for name, choices in self.choices.items()}

    def _validate(self, *_):
        removals, valid = [], True
        for name, group, title in (("field_map", "flds", "Fields"), ("template_map", "tmpls", "Templates")):
            mapping = self._mappings()[name]
            previous = self.prepared["previous"][group]
            try:
                SyncChanges.validate_mapping(previous, self.prepared["current"][group], mapping)
            except SnapshotConflict:
                valid = False
            removed = [f"{index + 1}: {item['name']}" for index, item in enumerate(previous) if index not in mapping]
            if removed:
                removals.append(f"{title} not retained: {', '.join(removed)}")
        self.removals.setText("\n".join(removals) or "All previous items are retained.")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)


class MediaConflictDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Resolve media file")
        layout = QVBoxLayout(self)
        layout.addWidget(label(prepared["filename"]))
        for name, value in (("Anki", prepared["local"]), ("Shirabe", prepared["remote"])):
            detail = "File is absent" if value is None or value.get("deleted") else f"{value['bytes']:,} bytes, SHA-256 {value['digest'][:16]}"
            layout.addWidget(label(f"{name}: {detail}"))
        layout.addWidget(label("Choose which version to keep. The other app will receive that file or deletion. "
                               "Anki replacements go through its media trash; Shirabe deletions retain the private bytes. "
                               "A later conflicting edit will pause this choice."))
        self.choice = QComboBox()
        self.choice.setAccessibleName("Media version to keep")
        self.choice.addItem("Choose a version…", None)
        self.choice.addItem("Keep Anki's file" if prepared["local"] else "Confirm deletion from Shirabe", "anki")
        self.choice.addItem("Confirm deletion from Anki" if prepared["remote"]["deleted"] else "Keep Shirabe's file", "shirabe")
        layout.addWidget(self.choice)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText("Confirm media choice")
        confirm.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave pending")
        self.choice.currentIndexChanged.connect(lambda _: confirm.setEnabled(self.choice.currentData() in ("anki", "shirabe")))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accepted.connect(lambda: submit(self.choice.currentData()))


class RejectedRecordDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Retry rejected sync record")
        layout = QVBoxLayout(self)
        problem = prepared["problem"]
        layout.addWidget(label(f"{problem['kind']} {problem['id']}: {problem['code']}"))
        layout.addWidget(label("Choose which data to retry. The original comparison base is kept, so Shirabe can still detect conflicting edits. "
                               "Accepted records are not resent, and the original receipt is kept."))
        self.source = QComboBox()
        self.source.addItem("Saved record", "saved")
        if prepared.get("current_entry") is not None:
            self.source.addItem("Current Anki data", "current")
        if prepared.get("mapping_required"):
            self.source.addItem("Map current Anki fields and templates…", "mapping")
        layout.addWidget(self.source)
        if prepared.get("current_error"):
            layout.addWidget(label(prepared["current_error"]))
        history_count = sum(len(batch["history"]) for batch in prepared.get("dependencies", []))
        if history_count:
            layout.addWidget(label(f"Current Anki data will also upload {history_count} new or updated review history records before this card."))
        tabs = QTabWidget()
        for title, value in (("Saved record to retry", prepared["entry"]), ("Anki now", prepared["current"])):
            source = QPlainTextEdit()
            source.setReadOnly(True)
            source.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            tabs.addTab(source, title)
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Retry saved record")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave pending")
        self.source.currentIndexChanged.connect(lambda _: buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Review mapping" if self.source.currentData() == "mapping" else
            "Send current Anki data" if self.source.currentData() == "current" else "Retry saved record"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accepted.connect(lambda: submit(self.source.currentData()))
        screen = self.screen().availableGeometry()
        self.resize(min(720, screen.width() - 40), min(640, screen.height() - 40))


class ReviewUndoDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Review Undo request")
        layout = QVBoxLayout(self)
        layout.addWidget(label(f"Shirabe requested Undo for card {prepared['change']['payload']['anki_card_id']}."))
        layout.addWidget(label("Undo removes this review and restores its previous schedule. Anki checks again before applying it. "
                               "Withdrawing the request leaves the card and history unchanged."))
        self.action = QComboBox()
        self.action.addItem("Withdraw Undo request", "withdraw")
        if prepared.get("undo_error"):
            layout.addWidget(label(prepared["undo_error"]))
        else:
            self.action.addItem("Undo this review in Anki", "undo")
        self.action.setAccessibleName("Review Undo action")
        layout.addWidget(self.action)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Withdraw Undo request")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave pending")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.action.currentIndexChanged.connect(lambda _: buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.action.currentText()))
        self.accepted.connect(lambda: submit(self.action.currentData()))


class NoteDeletionDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Resolve note deletion")
        layout = QVBoxLayout(self)
        absent = prepared["local"] is None
        layout.addWidget(label("The note and its requested cards are already absent in Anki. Confirm their deletion in Shirabe. No Anki note will be deleted by this action."
                              if absent else "Keep the current Anki note and withdraw Shirabe's deletion request. Resume sync to send Anki changes back to Shirabe."))
        if prepared["attempted"] and not absent:
            layout.addWidget(label("An earlier deletion attempt was interrupted. The note exists now, possibly after Undo. It will not be deleted again automatically."))
        layout.addWidget(label(f"Current Anki cards: {prepared['card_count']}"))
        tabs = QTabWidget()
        for name, value in zip(prepared["fields"], prepared["local"]["flds"].split("\x1f") if not absent else []):
            field = QPlainTextEdit()
            field.setReadOnly(True)
            field.setPlainText(value)
            tabs.addTab(field, name)
        if not absent:
            layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm deletion" if absent else "Keep note in Anki")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave pending")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accepted.connect(submit)
        screen = self.screen().availableGeometry()
        self.resize(min(720, screen.width() - 40), min(640, screen.height() - 40))


class SourceDeletionDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Resolve source deletion")
        layout = QVBoxLayout(self)
        layout.addWidget(label("Shirabe changed after this card was deleted in Anki. Confirm the recorded deletion against the current Shirabe version shown below. The note and review history will be retained."))
        layout.addWidget(label("The Anki card exists again. This confirms the earlier deletion; the next scan will send its restoration. No native card will be deleted."
                              if prepared["local"] else "The card is still absent in Anki. This action will not delete any native card."))
        tabs = QTabWidget()
        note = prepared["detail"]["deletion"]["note"]
        values = list(zip(note["field_names"], note["fields"]))
        values.extend([("Tags", " ".join(note["tags"])), ("Card state", json.dumps(prepared["detail"]["card"], ensure_ascii=False, indent=2))])
        for name, value in values:
            field = QPlainTextEdit()
            field.setReadOnly(True)
            field.setPlainText(value)
            tabs.addTab(field, name)
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply source deletion")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave pending")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accepted.connect(submit)
        screen = self.screen().availableGeometry()
        self.resize(min(720, screen.width() - 40), min(640, screen.height() - 40))


class HistoryRemovalDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Resolve Anki Undo")
        layout = QVBoxLayout(self)
        count = len(prepared["entry"]["removed_history"])
        noun = "record" if count == 1 else "records"
        restored = prepared["entry"].get("restored_history", [])
        incoming = prepared.get("problem", {}).get("direction") == "incoming"
        if restored:
            self.setWindowTitle("Resolve Anki history")
            restored_noun = "record" if len(restored) == 1 else "records"
            layout.addWidget(label(f"Remove {count} history {noun} and restore {len(restored)} archived history {restored_noun}. Other reviews will be kept. Choose which schedule to use; due dates will not be recalculated."))
        else:
            layout.addWidget(label(f"Remove {count} history {noun} undone in Anki. Other reviews will be kept. Choose which schedule to use; due dates will not be recalculated."))
        if incoming:
            layout.addWidget(label("Confirm one removal attempt using the history shown here. If Anki changes afterward, reopen this chooser. The incoming schedule is the recorded sync result, not a live view of Shirabe."))
        self.schedule = QComboBox()
        self.schedule.addItem("Use the incoming schedule" if incoming else "Keep Shirabe's current schedule", "shirabe")
        self.schedule.addItem("Use Anki's current schedule", "anki")
        self.schedule.setAccessibleName("Schedule after Undo")
        layout.addWidget(self.schedule)
        layout.addWidget(label("Using Anki's schedule keeps the other Shirabe reviews in history, but does not apply their resulting schedules."))
        tabs = QTabWidget()
        comparisons = [("Anki now", prepared["local"]), ("Incoming schedule" if incoming else "Shirabe now", prepared["detail"]["anki"]["card_after"]),
                       ("Removed history", prepared["entry"]["removed_history"])]
        if restored:
            comparisons.append(("Restored history", restored))
        for name, value in comparisons:
            source = QPlainTextEdit()
            source.setReadOnly(True)
            source.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            tabs.addTab(source, name)
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm history and schedule" if restored else "Confirm Undo and schedule")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave pending")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accepted.connect(lambda: submit(self.schedule.currentData()))
        screen = self.screen().availableGeometry()
        self.resize(min(760, screen.width() - 40), min(680, screen.height() - 40))


class NoteConflictDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Resolve note fields")
        layout = QVBoxLayout(self)
        layout.addWidget(label("Edit the chosen values or copy a side, then apply. Fields are shown as source text; HTML and scripts do not run here."))
        tabs = QTabWidget()
        self.fields, self.use_shirabe = [], []
        local = prepared["local"]
        remote = prepared["detail"]["note"]
        for name, anki, shirabe in zip(remote["field_names"], local["flds"].split("\x1f"), remote["fields"]):
            page = QWidget()
            field_layout = QVBoxLayout(page)
            sources = QHBoxLayout()
            chosen = QPlainTextEdit()
            chosen.setPlainText(anki)
            chosen.setAccessibleName(f"Chosen {name}")
            self.fields.append(chosen)
            for title, value in (("Anki", anki), ("Shirabe", shirabe)):
                column = QVBoxLayout()
                column.addWidget(label(title))
                source = QPlainTextEdit()
                source.setReadOnly(True)
                source.setPlainText(value)
                column.addWidget(source)
                copy = QPushButton(f"Use {title}")
                copy.clicked.connect(lambda _, field=chosen, text=value: field.setPlainText(text))
                column.addWidget(copy)
                sources.addLayout(column)
                if title == "Shirabe":
                    self.use_shirabe.append(copy)
            field_layout.addLayout(sources)
            field_layout.addWidget(label("Chosen value"))
            field_layout.addWidget(chosen)
            tabs.addTab(page, name)
        layout.addWidget(tabs)
        layout.addWidget(label("Chosen tags (separated by spaces)"))
        self.tags = QLineEdit(" ".join(local["tags"].split()))
        layout.addWidget(self.tags)
        tag_sources = QHBoxLayout()
        tag_sources.addWidget(label("Shirabe tags: " + " ".join(remote["tags"])))
        use_tags = QPushButton("Use Shirabe tags")
        use_tags.clicked.connect(lambda: self.tags.setText(" ".join(remote["tags"])))
        tag_sources.addWidget(use_tags)
        layout.addLayout(tag_sources)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply chosen values")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accepted.connect(lambda: submit([field.toPlainText() for field in self.fields], self.tags.text().split()))
        screen = self.screen().availableGeometry()
        self.resize(min(820, screen.width() - 40), min(720, screen.height() - 40))


class CardConflictDialog(QDialog):
    def __init__(self, parent, prepared, submit):
        super().__init__(parent)
        self.setWindowTitle("Resolve card state")
        layout = QVBoxLayout(self)
        if prepared.get("undo_accepted"):
            layout.addWidget(label("Shirabe already accepted this Undo. Choose which card state to keep before acknowledging its saved result. This action will not remove any review history."))
        if prepared.get("schedule_confirmed"):
            layout.addWidget(label("The schedule you confirmed in the Undo chooser will be applied. Choose which deck, suspension and properties to keep. Other review history is preserved."))
        elif prepared.get("schedule_choice_required"):
            layout.addWidget(label("The schedules differ. Choose Anki's current schedule or the saved result accepted by Shirabe. This is not a live view of Shirabe. Due dates will not be recalculated; other review history is preserved."))
        else:
            layout.addWidget(label("Choose a side for each part of this card. Review history is kept; this choice does not replace a review schedule."))
        if prepared.get("pending_reviews"):
            count = len(prepared["pending_reviews"])
            noun = "review" if count == 1 else "reviews"
            layout.addWidget(label(f"Upload {count} new Anki {noun} before reconciling this card. Both apps' reviews are kept; the latest recorded review supplies the schedule."))
        elif not prepared["schedule_matches"] and not prepared.get("schedule_confirmed") and not prepared.get("schedule_choice_required"):
            layout.addWidget(label("The review schedules differ. Review-history reconciliation is not available yet, so this card must stay pending."))
        tabs = QTabWidget()
        self.choices = {}
        sources = {"anki": prepared["local"], "shirabe": prepared["detail"]["anki"]["card_after"]}
        dimensions = [("deck", "Deck"), ("lifecycle", "Suspension or burial"), ("properties", "Flags and other data")]
        if prepared.get("schedule_choice_required"):
            dimensions.insert(0, ("schedule", "Review schedule"))
        for dimension, title in dimensions:
            page = QWidget()
            content = QVBoxLayout(page)
            columns = QHBoxLayout()
            for side, name in (("anki", "Anki"), ("shirabe", "Shirabe")):
                column = QVBoxLayout()
                column.addWidget(label("Accepted Shirabe result" if side == "shirabe" and prepared.get("undo_accepted") else name))
                row = sources[side]
                if dimension == "deck":
                    value = prepared["decks"].get(row["did"], str(row["did"]))
                elif dimension == "lifecycle":
                    value = {-1: "Suspended", -2: "Buried (Anki queue -2)", -3: "Buried (Anki queue -3)"}.get(row["queue"], "Active")
                elif dimension == "schedule":
                    value = json.dumps(row, ensure_ascii=False, indent=2)
                else:
                    value = json.dumps(auxiliary(row), ensure_ascii=False, indent=2)
                source = QPlainTextEdit()
                source.setReadOnly(True)
                source.setPlainText(value)
                column.addWidget(source)
                columns.addLayout(column)
            content.addLayout(columns)
            choice = QComboBox()
            if dimension == "schedule":
                choice.addItem("Choose a schedule", None)
            choice.addItem("Use Anki", "anki")
            choice.addItem("Use Shirabe", "shirabe")
            choice.setAccessibleName(f"Chosen {title}")
            content.addWidget(choice)
            self.choices[dimension] = choice
            tabs.addTab(page, title)
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Sync new reviews and apply state" if prepared.get("pending_reviews") else "Apply chosen state")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(prepared["schedule_matches"] or prepared.get("schedule_confirmed", False) or bool(prepared.get("pending_reviews")))
        if prepared.get("schedule_choice_required"):
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self.choices["schedule"].currentIndexChanged.connect(lambda _: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                self.choices["schedule"].currentData() in ("anki", "shirabe")))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accepted.connect(lambda: submit({name: choice.currentData() for name, choice in self.choices.items()}))
        screen = self.screen().availableGeometry()
        self.resize(min(820, screen.width() - 40), min(680, screen.height() - 40))
