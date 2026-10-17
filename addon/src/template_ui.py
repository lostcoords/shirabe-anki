"""Native phone setup and reversible, per-note-type lookup exclusions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from html.parser import HTMLParser
import re

from . import assets, template


class TemplateError(ValueError):
    pass


FIELD = re.compile(r"{{([^{}]+)}}")
IGNORE_OPEN = '<span data-shirabe-ignore data-sb-skip data-shirabe-field-exclusion style="display:contents">'
IGNORE = re.compile(re.escape(IGNORE_OPEN) + r"({{[^{}]+}})</span>")
VOID_TAGS = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
RAW_TAGS = frozenset('script style textarea title xmp iframe noembed noframes plaintext svg math select'.split())
STRUCTURAL_TAGS = frozenset('table thead tbody tfoot tr colgroup select optgroup option svg math'.split())


def without_excluded_fields(value: str) -> str:
    return IGNORE.sub(r'\1', value)


class _FieldLocations(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.lines = [0]
        self.lines.extend(match.end() for match in re.finditer('\n', source))
        self.stack = []
        self.text = []
        self.comments = []
        self.feed(source)
        self.close()

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if not RAW_TAGS.intersection(self.stack) and not STRUCTURAL_TAGS.intersection(self.stack[-1:]):
            self.text.append((self.source_offset(), self.source_offset() + len(data)))

    def handle_comment(self, data):
        self.comments.append((self.source_offset(), self.source_offset() + len(data) + 7))


def with_excluded_fields(value: str, fields) -> str:
    """Wrap exact text substitutions; refuse contexts where HTML would change meaning."""
    clean = without_excluded_fields(value)
    if not fields:
        return clean
    names = set(fields)
    locations = _FieldLocations(clean)
    replacements = []
    for match in FIELD.finditer(clean):
        expression = match.group(1).strip()
        if expression.startswith(('#', '^', '/', '!')):
            continue
        field = expression if expression in names else expression.rsplit(':', 1)[-1].strip()
        if field not in names:
            continue
        if any(start <= match.start() and match.end() <= end for start, end in locations.comments):
            continue
        if not any(start <= match.start() and match.end() <= end for start, end in locations.text):
            raise TemplateError(
                f'Cannot safely skip "{field}": it is used inside an HTML attribute, script, or structural element. '
                'Move its visible substitution into a normal text element in Anki\'s Cards editor, then try again.'
            )
        replacements.append(match)
    for match in reversed(replacements):
        clean = clean[:match.start()] + IGNORE_OPEN + match.group() + '</span>' + clean[match.end():]
    return clean


def settings_key(note_type_id: int) -> str:
    return f'shirabe.template.{note_type_id}'


@dataclass(frozen=True)
class TemplateOptions:
    answer_only: bool = True
    online_parsing: bool = False
    excluded_fields: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings):
        settings = settings or {}
        return cls(settings.get('answerOnly', True) is not False,
                   settings.get('onlineParsing') is True,
                   tuple(settings.get('excludedFields', ())))


@dataclass(frozen=True)
class TemplateTarget:
    model: dict
    settings: dict | None

    @classmethod
    def capture(cls, col, model):
        return cls(deepcopy(model), deepcopy(col.get_config(settings_key(model['id']))))

    def check_current(self, col):
        if (col.models.get(self.model['id']) != self.model
                or col.get_config(settings_key(self.model['id'])) != self.settings):
            raise TemplateError(f'"{self.model["name"]}" changed while this window was open. Reopen setup to review the current templates.')

    @property
    def installed(self):
        return any(template.installed(card.get(side, '')) for card in self.model['tmpls'] for side in ('qfmt', 'afmt'))


def _clean_sides(target, card):
    records = (target.settings or {}).get('templates', ())
    record = next((row for row in records if row['ord'] == card['ord'] and row['name'] == card['name']), None)
    result = {}
    for side in ('qfmt', 'afmt'):
        value = card.get(side, '')
        if record and value == record['after'][side]:
            result[side] = record['before'][side]
        else:
            result[side] = without_excluded_fields(template.QUESTION_BLOCK.sub('', template.BLOCK.sub('', value)))
    return result


def _excluded_sides(target, options):
    missing = set(options.excluded_fields) - {field['name'] for field in target.model['flds']}
    if missing:
        raise TemplateError('These fields no longer exist: ' + ', '.join(sorted(missing)))
    if not target.model['tmpls']:
        raise TemplateError('This note type has no card templates.')
    result = []
    for card in target.model['tmpls']:
        before = _clean_sides(target, card)
        try:
            after = {side: with_excluded_fields(value, options.excluded_fields) for side, value in before.items()}
        except TemplateError as error:
            raise TemplateError(f'{card["name"]}: {error}') from error
        result.append((before, after))
    return result


def _write_installation(col, target, options, config, local_assets, sides):
    api_base = config.get('apiBase', template.PRODUCTION)
    updated = deepcopy(target.model)
    snapshot = any(field['name'] == 'ShirabeLookup' for field in updated['flds'])
    records = []
    for card, (before, after) in zip(updated['tmpls'], sides):
        for side in ('qfmt', 'afmt'):
            card[side] = (template.with_question_guard(after[side]) if side == 'qfmt' and options.answer_only else
                          template.with_block(after[side], api_base, template.CLIENT_ID, config.get('trigger', 'click'),
                                              options.online_parsing, assets=local_assets, snapshot_field=snapshot))
        records.append({'ord': card['ord'], 'name': card['name'], 'before': before,
                        'after': {side: card[side] for side in ('qfmt', 'afmt')}})
    changes = col.models.update_dict(updated)
    col.set_config(settings_key(updated['id']), {
        'answerOnly': options.answer_only, 'onlineParsing': options.online_parsing,
        'excludedFields': list(options.excluded_fields), 'templates': records,
    }, undoable=True)
    return changes


def refresh_installation(col, model: dict, config: dict, local_assets: dict):
    """Refresh snapshot templates in the caller's Undo group, retaining options and removal originals.

    Pass the detached model after adding ShirabeLookup. This saves the model and
    its baseline record; the caller merges its Undo group and rolls it back on failure.
    """
    target = TemplateTarget.capture(col, model)
    options = TemplateOptions.from_settings(target.settings)
    return _write_installation(col, target, options, config, local_assets, _excluded_sides(target, options))


def apply_install(col, target: TemplateTarget, options: TemplateOptions, config: dict):
    """Install one reviewed target inside CollectionOp; return native OpChanges."""
    target.check_current(col)
    if not template.reaches_a_phone(config.get('apiBase', template.PRODUCTION)):
        raise TemplateError('The server address only works on this computer. Set a phone-accessible server in Shirabe Settings, then reopen setup.')
    sides = _excluded_sides(target, options)
    local_assets = assets.install(col.media)
    undo = col.add_custom_undo_entry('Shirabe: install phone lookup')
    try:
        _write_installation(col, target, options, config, local_assets, sides)
        return col.merge_undo_entries(undo)
    except Exception:
        col.merge_undo_entries(undo)
        col.undo()
        raise


def apply_remove(col, targets: tuple[TemplateTarget, ...]):
    """Remove only reviewed targets inside CollectionOp, as one native Undo step."""
    from anki.collection import OpChanges

    for target in targets:
        target.check_current(col)
    if not targets:
        return OpChanges()
    updates = []
    for target in targets:
        updated = deepcopy(target.model)
        for card in updated['tmpls']:
            card.update(_clean_sides(target, card))
        updates.append(updated)
    undo = col.add_custom_undo_entry('Shirabe: remove phone lookup')
    try:
        for updated in updates:
            col.models.update_dict(updated)
            col.remove_config(settings_key(updated['id']))
            col.merge_undo_entries(undo)
        return col.merge_undo_entries(undo)
    except Exception:
        col.merge_undo_entries(undo)
        col.undo()
        raise


def _label(text):
    from aqt.qt import QLabel, Qt

    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def build_install_dialog(parent, targets: tuple[TemplateTarget, ...], config: dict):
    """Build the native setup window without reading or changing the collection."""
    from aqt.qt import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                        QFrame, QGroupBox, QListWidget, QListWidgetItem, QScrollArea,
                        QVBoxLayout, QWidget, Qt)

    class SetupDialog(QDialog):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle('Shirabe on your phone')
            self.setMinimumWidth(480)
            available = self.screen().availableGeometry()
            self.resize(min(660, available.width() - 40), min(620, available.height() - 40))
            outer = QVBoxLayout(self)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            body = QWidget()
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, 0, 0, 0)
            scroll.setWidget(body)
            outer.addWidget(scroll)
            layout.addWidget(_label('Add word lookup to a note type you already study. Sync your collection and media to use it on your phone.'))
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            layout.addLayout(form)
            self.note_type = QComboBox()
            self.note_type.setAccessibleName('Note type')
            self.note_type.setMinimumContentsLength(10)
            self.note_type.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            for target in targets:
                self.note_type.addItem(target.model['name'], target.model['id'])
            form.addRow('&Note type', self.note_type)
            self.scope = _label('')
            layout.addWidget(self.scope)
            self.side = QComboBox()
            self.side.setAccessibleName('Lookup availability')
            self.side.setMinimumContentsLength(22)
            self.side.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.side.addItem('After showing the answer', True)
            self.side.addItem('On both sides', False)
            form.addRow('&Allow lookup', self.side)
            group = QGroupBox('Fields to skip (optional)')
            group_layout = QVBoxLayout(group)
            group_layout.addWidget(_label('Checked fields still appear on your cards. Shirabe will leave their text alone.'))
            self.fields = QListWidget()
            self.fields.setAccessibleName('Fields excluded from lookup')
            self.fields.setMinimumHeight(90)
            self.fields.setMaximumHeight(140)
            group_layout.addWidget(self.fields)
            layout.addWidget(group)
            self.online = QCheckBox('Identify visible Japanese online')
            layout.addWidget(self.online)
            api_base = config.get('apiBase', template.PRODUCTION)
            consent = _label(f'Sends visible Japanese text to {api_base} to identify words. Skipped fields are excluded. '
                             'When off, only a word you choose and nearby text are sent for lookup. '
                             'Prepared lookups work offline. Phones sign in separately.')
            self.online.setAccessibleDescription(consent.text())
            layout.addWidget(consent)
            self.error = _label('')
            self.error.setAccessibleName('Setup issue')
            self.error.hide()
            layout.addWidget(self.error)
            layout.addStretch()
            self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            self.buttons.accepted.connect(self.accept)
            self.buttons.rejected.connect(self.reject)
            outer.addWidget(self.buttons)
            self.drafts = {}
            self.previous = None
            self.note_type.currentIndexChanged.connect(self.load_target)
            self.load_target()

        @property
        def target(self):
            return targets[self.note_type.currentIndex()]

        @property
        def options(self):
            return TemplateOptions(self.side.currentData(), self.online.isChecked(),
                                   tuple(self.fields.item(i).text() for i in range(self.fields.count())
                                         if self.fields.item(i).checkState() == Qt.CheckState.Checked))

        def load_target(self, *_):
            if self.previous is not None:
                self.drafts[self.previous] = self.options
            target = self.target
            options = self.drafts.get(target.model['id'], TemplateOptions.from_settings(target.settings))
            self.previous = target.model['id']
            self.side.setCurrentIndex(self.side.findData(options.answer_only))
            self.online.setChecked(options.online_parsing)
            self.fields.clear()
            for field in target.model['flds']:
                item = QListWidgetItem(field['name'], self.fields)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if field['name'] in options.excluded_fields else Qt.CheckState.Unchecked)
            count = len(target.model['tmpls'])
            self.scope.setText(f'{count} card template{"s" if count != 1 else ""} will be updated. '
                               'Your notes, decks and review schedule stay as they are.')
            self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('Update' if target.installed else 'Install')
            self.error.hide()

        def accept(self):
            try:
                if not template.reaches_a_phone(config.get('apiBase', template.PRODUCTION)):
                    raise TemplateError('Set a phone-accessible server address in Shirabe Settings before installing.')
                _excluded_sides(self.target, self.options)
            except TemplateError as error:
                self.error.setText(str(error))
                self.error.show()
                return
            super().accept()

    return SetupDialog()


def build_remove_dialog(parent, targets: tuple[TemplateTarget, ...]):
    """Confirm explicit removal targets, initially selecting none."""
    from aqt.qt import (QDialog, QDialogButtonBox, QFrame, QListWidget, QListWidgetItem,
                        QScrollArea, QVBoxLayout, QWidget, Qt)

    class RemoveDialog(QDialog):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle('Remove Shirabe from phone templates')
            self.resize(480, 360)
            outer = QVBoxLayout(self)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            body = QWidget()
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, 0, 0, 0)
            scroll.setWidget(body)
            outer.addWidget(scroll)
            layout.addWidget(_label('Choose the note types to remove phone lookup from. Their notes, decks and review schedule will be kept.'))
            self.note_types = QListWidget()
            self.note_types.setAccessibleName('Note types to remove phone lookup from')
            for target in targets:
                item = QListWidgetItem(target.model['name'], self.note_types)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
            layout.addWidget(self.note_types)
            self.summary = _label('No note types selected.')
            layout.addWidget(self.summary)
            layout.addWidget(_label('You can restore these templates with Edit > Undo. Sync afterward to update your phone. Shared media files are kept.'))
            self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            self.remove = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.remove.setText('Remove')
            self.remove.setEnabled(False)
            self.buttons.accepted.connect(self.accept)
            self.buttons.rejected.connect(self.reject)
            outer.addWidget(self.buttons)
            self.note_types.itemChanged.connect(self.selection_changed)

        @property
        def targets(self):
            return tuple(target for i, target in enumerate(targets)
                         if self.note_types.item(i).checkState() == Qt.CheckState.Checked)

        def selection_changed(self, *_):
            selected = self.targets
            self.remove.setEnabled(bool(selected))
            self.summary.setText(f'Remove from {len(selected)} note type{"s" if len(selected) != 1 else ""}.'
                                 if selected else 'No note types selected.')

        def accept(self):
            if self.targets:
                super().accept()

    return RemoveDialog()


def install_dialog(mw, config: dict):
    """Open setup for one existing note type; apply with Anki's undoable CollectionOp."""
    from aqt.operations import CollectionOp, QueryOp
    from aqt.qt import QDialog
    from aqt.utils import showWarning, tooltip

    settings = deepcopy(config)

    def choose(targets):
        if not targets:
            showWarning('This collection has no note types yet.', parent=mw)
            return
        dialog = build_install_dialog(mw, targets, settings)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target, options = dialog.target, dialog.options
        CollectionOp(parent=mw, op=lambda col: apply_install(col, target, options, settings)).success(
            lambda _: tooltip(f'Shirabe installed on "{target.model["name"]}". Sync your collection and media, then open a card on your phone. '
                              'Edit > Undo restores the templates.', parent=mw, period=8000)
        ).failure(lambda error: showWarning(str(error), parent=mw)).run_in_background()

    QueryOp(parent=mw, op=_capture_targets, success=choose).run_in_background()


def remove_dialog(mw):
    """Choose exact installed note types and remove them in one native Undo step."""
    from aqt.operations import CollectionOp, QueryOp
    from aqt.qt import QDialog
    from aqt.utils import showWarning, tooltip

    def choose(targets):
        installed = tuple(target for target in targets if target.installed)
        if not installed:
            tooltip('No note type has Shirabe phone lookup installed.', parent=mw)
            return
        dialog = build_remove_dialog(mw, installed)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.targets
        CollectionOp(parent=mw, op=lambda col: apply_remove(col, selected)).success(
            lambda _: tooltip(f'Removed phone lookup from {len(selected)} note type(s). Edit > Undo restores it.', parent=mw, period=6000)
        ).failure(lambda error: showWarning(str(error), parent=mw)).run_in_background()

    QueryOp(parent=mw, op=_capture_targets, success=choose).run_in_background()


def _capture_targets(col):
    return tuple(TemplateTarget.capture(col, model) for model in sorted(col.models.all(), key=lambda model: model['name'].casefold()))
