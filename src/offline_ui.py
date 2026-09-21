"""Explicit selected-note preparation, with native collection undo and conflicts."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import threading
import time

from . import assets, offline, offline_audio, template, template_ui

SNAPSHOT_FIELD = "ShirabeLookup"
MAX_SELECTED_NOTES = 200
MAX_RENDER_BYTES = 256 * 1024
MAX_CARD_SIDES = 16
MAX_CLEANUP_FILES = 1000
CAPTURE_CSP = "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data:; media-src 'none'; connect-src 'none'; frame-src 'none'; form-action 'none'"
ISSUES = {
    "entry_limit": "Some dictionary entries exceeded 24 KiB or the entry limits.",
    "entry_unavailable": "Some parsed words point to entries that could not be prepared. Their entry identities were not changed.",
    "response_limit": "Some server responses exceeded 256 KiB.",
    "byte_limit": "Some notes reached their 128 KiB snapshot limit.",
    "request_limit": "Some notes reached their 65-request limit.",
    "candidate_limit": "Some words had more candidates than could be prepared.",
    "dictionary_not_approved": "Some results belonged to dictionaries not approved for export.",
    "lookup_unavailable": "Some dictionary lookups could not be completed.",
    "parse_unavailable": "Some card text could not be segmented.",
    "reconnect_required": "Reconnect to Shirabe to finish the missing lookups.",
    "static_text_only": "Note scripts were not run; only static visible text was prepared.",
    "cancelled": "Preparation was stopped before all lookups finished.",
    "refresh_incomplete": "Some refreshes reused previously prepared results.",
    "text_limit": "Some notes exceeded 4,096 text units.",
    "segment_limit": "Some notes exceeded 16 visible text segments.",
    "token_limit": "Some notes exceeded 128 word spans.",
    "invalid_text": "Some card text was malformed.",
    "storage_limit": "The collection's offline storage limit was reached.",
    "audio_limit": "Some pronunciation recordings exceeded the audio download or storage limits.",
    "audio_unavailable": "Some pronunciation recordings could not be verified or downloaded. Definitions remain available.",
    "examples_limit": "Some corpus examples exceeded the preparation limits; no additional pages were downloaded.",
    "examples_unavailable": "Some corpus examples or their reuse permissions could not be verified. Definitions remain available.",
    "dictionary_changed": "Dictionary settings changed during preparation. Start again to use the current selection.",
    "server_update_required": "The connected Shirabe server needs an update before preparing cards. Existing saved definitions still work.",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def note_revision(note, model):
    return _digest({"id": note.id, "mid": note.mid, "fields": note.fields, "tags": sorted(note.tags),
                    "model": {key: model[key] for key in ("flds", "tmpls", "css")}})


@dataclass(frozen=True)
class CapturedNote:
    note_id: int
    revision: str
    sides: tuple[tuple[int, str], ...]
    previous_name: str | None
    previous: dict | None


def capture_notes(col, note_ids):
    """Read rendered cards and revisions under a collection-backed QueryOp."""
    if not 0 < len(note_ids) <= MAX_SELECTED_NOTES:
        raise offline.SnapshotError("Select between 1 and 200 notes for one preparation batch")
    result = []
    total = 0
    for note_id in dict.fromkeys(note_ids):
        note = col.get_note(note_id)
        model = note.note_type()
        sides = []
        note_bytes = 0
        for card in note.cards():
            for html in (card.question(reload=True), card.answer()):
                note_bytes += len(html.encode("utf-8"))
                if note_bytes > MAX_RENDER_BYTES or len(sides) >= MAX_CARD_SIDES:
                    raise offline.SnapshotError("A selected note exceeds the rendered-card limit (256 KiB or 16 sides)")
                sides.append((card.ord + 1, html))
        total += note_bytes
        if total > offline.MAX_TOTAL_BYTES:
            raise offline.SnapshotError("Selected card HTML exceeds 25 MiB; select fewer notes")
        previous_name = offline.snapshot_filename(note[SNAPSHOT_FIELD]) if SNAPSHOT_FIELD in note else None
        result.append(CapturedNote(note.id, note_revision(note, model), tuple(sides), previous_name,
                                   offline.read_snapshot(col.media, previous_name) if previous_name else None))
    return result


def capture_script(html: str, ordinal: int) -> str:
    """Read the same DOM runs as attachment, without executing note scripts."""
    if len(html.encode("utf-8")) > MAX_RENDER_BYTES:
        raise offline.SnapshotError("Rendered card exceeds 256 KiB")
    html = template.QUESTION_BLOCK.sub("", template.BLOCK.sub("", html))
    return """(() => {
      if (!window.ShirabeCard?.collectReadableRuns) return {error: "missing_bundle"};
      const parsed = new DOMParser().parseFromString(%s, "text/html");
      const scripted = !!parsed.querySelector("script");
      parsed.querySelectorAll("script, iframe, object, embed, base, link, meta").forEach(node => node.remove());
      for (const node of parsed.querySelectorAll("*")) {
        for (const attribute of [...node.attributes]) {
          if (/^on/i.test(attribute.name) || ["srcdoc", "autofocus"].includes(attribute.name)) node.removeAttribute(attribute.name);
        }
      }
      const root = document.getElementById("qa");
      root.replaceChildren(...parsed.head.childNodes, ...parsed.body.childNodes);
      for (const name of [...document.body.classList]) if (/^card\\d+$/.test(name)) document.body.classList.remove(name);
      document.body.classList.add("card", %s);
      const texts = window.ShirabeCard.collectReadableRuns(root);
      if (texts.length > %d || texts.reduce((n, text) => n + text.length, 0) > %d) return {error: "text_limit"};
      return {texts, scripted};
    })()""" % (json.dumps(html), json.dumps(f"card{int(ordinal)}"), offline.MAX_SEGMENTS, offline.MAX_TEXT_UNITS)


def content_version(preferences, approved):
    return _digest({"stack": preferences["stackFingerprint"], "dictionaries": preferences["dictionaries"],
                    "languages": preferences["languages"], "locale": preferences["uiLocale"], "approved": sorted(approved)})


def approved_dictionary_slugs(preferences, approved, dictionaries):
    ordered = dict.fromkeys([*(source.split(":", 1)[0] for source in preferences["dictionaries"]),
                             *(dictionary["slug"] for dictionary in dictionaries)])
    return [slug for slug in ordered if slug in approved]


@dataclass
class PreparedBatch:
    rows: list[tuple[CapturedNote, offline.Preparation]] = field(default_factory=list)
    skipped: int = 0
    stopped: bool = False
    issue_notes: dict[str, int] = field(default_factory=dict)


def prepare_batch(captured, texts, backend, *, preferences, approved, cancelled=lambda: False, progress=lambda *_: None,
                  sources=(), static_only=(), include_audio=False, include_examples=False):
    """Network work over detached records; never holds the collection."""
    result = PreparedBatch()
    remaining = offline.MAX_TOTAL_BYTES
    audio = offline_audio.AudioBatch(backend, remaining) if include_audio else None
    lookup_cache = offline.LookupCache(backend)
    version = content_version(preferences, approved)
    started = time.time()
    for index, note in enumerate(captured):
        if cancelled():
            result.stopped = True
            break
        backend.check_access()
        runs = texts.get(note.note_id)
        if not runs:
            result.skipped += 1
            continue
        preparation = offline.prepare_snapshot(runs, parse=backend.parse, identify=backend.identify,
                                               content_version=version, allowed_dictionaries=approved,
                                               previous=note.previous, cancelled=cancelled, now=lambda: started,
                                               preferences={"languages": preferences["languages"], "dictionaryReveal": preferences.get("dictionaryReveal", {})},
                                               sources=sources, static_text_only=note.note_id in static_only, audio=audio,
                                               examples=backend.examples if include_examples else None, lookup_cache=lookup_cache)
        backend.check_access()
        for issue in preparation.issues:
            result.issue_notes[issue] = result.issue_notes.get(issue, 0) + 1
        available = audio.remaining_bytes if audio is not None else remaining
        if preparation.bytes_used > available:
            result.stopped = True
            break
        if preparation.coverage.prepared:
            result.rows.append((note, preparation))
            remaining -= preparation.bytes_used
            if audio is not None:
                audio.remaining_bytes -= preparation.bytes_used
        else:
            result.skipped += 1
        progress(index + 1, len(captured))
    result.stopped = result.stopped or cancelled()
    backend.check_access()
    return result


@dataclass
class AppliedBatch:
    changes: object
    saved: int = 0
    conflicts: int = 0
    kept_previous: int = 0
    prepared: int = 0
    tokens: int = 0
    bytes_used: int = 0
    partial: int = 0
    issues: set[str] = field(default_factory=set)
    audio_files: set[str] = field(default_factory=set)


def apply_prepared(col, batch: PreparedBatch, config: dict, *, check_access=lambda: None) -> AppliedBatch:
    """Recheck revisions, then save references and template changes in one undo step."""
    check_access()
    from anki.collection import OpChanges
    from anki.errors import NotFoundError

    result = AppliedBatch(OpChanges())
    fresh = []
    for captured, preparation in batch.rows:
        try:
            note = col.get_note(captured.note_id)
            if note_revision(note, note.note_type()) != captured.revision:
                result.conflicts += 1
                continue
        except (KeyError, NotFoundError):
            result.conflicts += 1
            continue
        fresh.append((captured, preparation, note.mid))
    if not fresh:
        return result
    check_access()
    files = assets.install(col.media)
    pending = []
    remaining = offline.MAX_TOTAL_BYTES
    for captured, preparation, mid in fresh:
        check_access()
        try:
            stored = offline.persist_snapshot(col.media, preparation, previous_name=captured.previous_name, remaining_bytes=remaining,
                                              total_budget_bytes=storage_budget(config))
        except offline.SnapshotError as error:
            if error.code != "storage_limit":
                raise
            result.issues.add("storage_limit")
            break
        remaining -= stored.bytes_used
        pending.append((captured.note_id, mid, offline.snapshot_field(stored.filename, stored.recordings)))
        result.kept_previous += int(stored.kept_previous)
        result.bytes_used += stored.bytes_used
        result.prepared += stored.coverage.prepared
        result.tokens += stored.coverage.tokens
        result.partial += int(not preparation.complete)
        result.issues.update(preparation.issues)
        result.audio_files.update(stored.recordings)
    if not pending:
        return result
    check_access()
    undo = col.add_custom_undo_entry("Prepare Shirabe offline lookup")
    try:
        for mid in dict.fromkeys(mid for _, mid, _ in pending):
            model = deepcopy(col.models.get(mid))
            if not any(f["name"] == SNAPSHOT_FIELD for f in model["flds"]):
                col.models.add_field(model, col.models.new_field(SNAPSHOT_FIELD))
            template_ui.refresh_installation(col, model, config, files)
            result.changes = col.merge_undo_entries(undo)
        notes = []
        for note_id, _, reference in pending:
            note = col.get_note(note_id)
            note[SNAPSHOT_FIELD] = reference
            notes.append(note)
        col.update_notes(notes)
        check_access()
        result.saved = len(notes)
        result.changes = col.merge_undo_entries(undo)
    except Exception:
        col.merge_undo_entries(undo)
        col.undo()
        raise
    return result


def storage_budget(config):
    mib = config.get("offlineStorageMiB", 25)
    if type(mib) is not int or not 1 <= mib <= 256:
        raise offline.SnapshotError("Choose a collection offline limit between 1 and 256 MiB")
    return mib * 1024 * 1024


def format_bytes(size):
    if size < 1024:
        return f"{size:,} B"
    if size < 1024 * 1024:
        return f"{size / 1024:,.1f}".rstrip("0").rstrip(".") + " KiB"
    return f"{size / 1024 / 1024:,.2f}".rstrip("0").rstrip(".") + " MiB"


def storage_summary(usage):
    return f"{format_bytes(usage.bytes_used)} in {usage.files:,} {'file' if usage.files == 1 else 'files'}"


def _owned_files(media):
    with os.scandir(media.dir()) as entries:
        for entry in entries:
            if (offline.MEDIA_NAME.fullmatch(entry.name) or assets.BUNDLE_NAME.fullmatch(entry.name)
                    or offline_audio.MEDIA_NAME.fullmatch(entry.name)) and entry.is_file(follow_symlinks=False):
                yield entry.name


def unreferenced_files(col, candidates=None):
    references = json.dumps((col.models.all(), col.all_config()))
    snapshot_audio = set()
    for name in _owned_files(col.media):
        if offline.MEDIA_NAME.fullmatch(name):
            snapshot = offline.read_snapshot(col.media, name)
            if snapshot is not None:
                snapshot_audio.update(offline_audio.recordings(snapshot))
    names = _owned_files(col.media) if candidates is None else islice(candidates, MAX_CLEANUP_FILES)
    unused = []
    seen = set()
    for name in names:
        if not isinstance(name, str) or name in seen:
            continue
        if not (offline.MEDIA_NAME.fullmatch(name) or assets.BUNDLE_NAME.fullmatch(name) or offline_audio.MEDIA_NAME.fullmatch(name)):
            continue
        if name in references or name in snapshot_audio or col.find_notes(f'"{name}"'):
            continue
        if offline.MEDIA_NAME.fullmatch(name):
            valid = offline.read_snapshot(col.media, name) is not None
        elif offline_audio.MEDIA_NAME.fullmatch(name):
            valid = offline_audio.verified_size(col.media, name) is not None
        else:
            valid = assets.verified_bundle_size(col.media, name) is not None
        if valid:
            unused.append(name)
            seen.add(name)
            if len(unused) >= MAX_CLEANUP_FILES:
                break
    return unused


def unreferenced_snapshots(col, candidates=None):
    return [name for name in unreferenced_files(col, candidates) if offline.MEDIA_NAME.fullmatch(name)]


@dataclass(frozen=True)
class CleanupReview:
    snapshots: offline.SnapshotStorage
    code: assets.BundleStorage
    names: tuple[str, ...]
    audio: offline.SnapshotStorage = offline.SnapshotStorage(0, 0)


def inspect_cleanup(col):
    snapshots = offline.snapshot_storage(col.media)
    shared = offline.snapshot_storage(col.media, audio=True)
    audio = offline.SnapshotStorage(shared.bytes_used - snapshots.bytes_used, shared.files - snapshots.files)
    return CleanupReview(snapshots, assets.bundle_storage(col.media), tuple(unreferenced_files(col)), audio)


def trash_reviewed(col, names):
    """Call under collection access; a review is not permission to skip revalidation."""
    checked = unreferenced_files(col, names)
    if checked:
        col.media.trash_files(checked)
    return checked


def review_cleanup(parent, on_done=lambda: None):
    """Review exact owned filenames, then recheck references before recoverable trash."""
    from aqt.operations import QueryOp
    from aqt.qt import QDialog, QDialogButtonBox, QFrame, QLabel, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget
    from aqt.utils import showInfo, showWarning

    def reviewed(review):
        dialog = QDialog(parent)
        dialog.setWindowTitle("Review Shirabe storage")
        outer = QVBoxLayout(dialog)
        content = QScrollArea()
        content.setWidgetResizable(True)
        content.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        content.setWidget(body)
        outer.addWidget(content)
        summary = QLabel(
            f"Snapshots: {storage_summary(review.snapshots)}\n"
            f"Pronunciation: {storage_summary(review.audio)}\n"
            f"Code bundles: {storage_summary(review.code)}\n"
            f"Total: {format_bytes(review.snapshots.bytes_used + review.audio.bytes_used + review.code.bytes_used)}, including old or damaged versions."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        explanation = QLabel(
            "Only hash-verified, unreferenced Shirabe files are listed. Notes, templates and saved removal baselines are checked again before removal. "
            "Recordings referenced by retained snapshots are kept; review again after removing old snapshots to clean up their audio. "
            "Damaged files, links, recordings above 1 MiB and code bundles above 4 MiB are kept. "
            "Undo can restore older references that are not visible now; restore these files from Anki's media trash if needed. "
            "Trash is temporary, so keep a backup if you need older versions. "
            f"Each review lists at most {MAX_CLEANUP_FILES:,} files; run it again to review more."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        question = QLabel(f"Move these {len(review.names)} unused files to Anki's media trash?" if review.names
                          else "No verified unused Shirabe files were found. Nothing will be removed.")
        question.setWordWrap(True)
        layout.addWidget(question)
        listing = QPlainTextEdit("\n".join(review.names))
        listing.setReadOnly(True)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Move to media trash")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(review.names))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel" if review.names else "Close")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)
        available = dialog.screen().availableGeometry()
        dialog.resize(min(760, available.width() - 40), min(600, available.height() - 40))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return on_done()
        QueryOp(parent=parent, op=lambda col: trash_reviewed(col, review.names),
                success=lambda removed: (showInfo(f"Moved {len(removed)} of {len(review.names)} reviewed files to Anki's media trash. "
                                                 "Files with new references or failed verification were kept. Removed files can be restored from media trash.", parent=parent), on_done()))\
            .failure(lambda _: showWarning("Cleanup could not finish. Check Anki's media trash before trying again.", parent=parent)).run_in_background()
    QueryOp(parent=parent, op=inspect_cleanup, success=reviewed).with_progress("Checking Shirabe media references…")\
        .failure(lambda _: showWarning("Shirabe media references could not be checked. Nothing was removed.", parent=parent)).run_in_background()


def _consent(parent, count, dictionaries, usage, config):
    from aqt.operations import QueryOp
    from aqt.qt import QCheckBox, QDialog, QDialogButtonBox, QFrame, QLabel, QPushButton, QScrollArea, QSpinBox, QToolButton, QVBoxLayout, QWidget, Qt

    dialog = QDialog(parent)
    dialog.setWindowTitle("Prepare for offline lookup")
    outer = QVBoxLayout(dialog)
    content = QScrollArea()
    content.setWidgetResizable(True)
    content.setFrameShape(QFrame.Shape.NoFrame)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    content.setWidget(body)
    outer.addWidget(content)
    title = QLabel(f"Prepare {count:,} {'note' if count == 1 else 'notes'} for offline lookup")
    font = title.font()
    font.setBold(True)
    title.setFont(font)
    layout.addWidget(title)
    for text in (
        "Only selected-card excerpts, not full dictionaries.",
        "Shirabe receives visible text from both sides of these cards, including revealed cloze answers.",
        "Saved text, dictionary excerpts, optional examples and recordings sync with Anki and travel with deck exports. Anyone receiving the deck can access them.",
        "This adds a hidden ShirabeLookup field and popup code to the note types. Existing study fields and schedules stay unchanged.",
    ):
        message = QLabel(text)
        message.setWordWrap(True)
        layout.addWidget(message)
    storage = [usage]
    used = QLabel(f"Snapshots and Shirabe pronunciation: {storage_summary(usage)} (including older versions)")
    used.setWordWrap(True)
    layout.addWidget(used)
    cleanup = QPushButton("Review unused Shirabe files…")
    def refreshed(new_usage):
        storage[0] = new_usage
        used.setText(f"Snapshots and Shirabe pronunciation: {storage_summary(new_usage)} (including older versions)")
        enable()
    cleanup.clicked.connect(lambda: review_cleanup(dialog, lambda: QueryOp(parent=dialog, op=lambda col: offline.snapshot_storage(col.media, audio=True), success=refreshed).run_in_background()))
    layout.addWidget(cleanup)
    storage_controls = QVBoxLayout()
    storage_label = QLabel("Offline storage limit for this collection:")
    storage_label.setWordWrap(True)
    storage_controls.addWidget(storage_label)
    limit = QSpinBox()
    limit.setRange(1, 256)
    limit.setValue(storage_budget(config) // 1024 // 1024)
    limit.setSuffix(" MiB")
    limit.setToolTip("Includes older snapshots and all hash-named Shirabe pronunciation files, including authoring recordings. Raising the limit permits more disk use, up to 256 MiB. Code bundles are counted separately.")
    storage_controls.addWidget(limit, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addLayout(storage_controls)
    details = QToolButton()
    details.setText("Preparation limits")
    details.setCheckable(True)
    details.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    details.setArrowType(Qt.ArrowType.RightArrow)
    layout.addWidget(details)
    bounds = QLabel("Up to 128 KiB of JSON per note, 24 KiB per entry and 25 MiB per batch including audio. Pronunciation is limited to 1 MiB per recording, 16 files and 4 MiB per note. "
                    "Examples are limited to 2 per entry, 8 per note and 8 KiB including attribution. "
                    "Each note has a shared 65-request limit for dictionary lookup, examples, recording metadata and downloads. Oversized or missing data leaves partial coverage. "
                    "Only static visible text is prepared. Script-generated text may need online lookup.")
    bounds.setWordWrap(True)
    bounds.hide()
    layout.addWidget(bounds)
    def show_details(expanded):
        bounds.setVisible(expanded)
        details.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
    details.toggled.connect(show_details)
    permission = QCheckBox("Send visible card text to Shirabe")
    layout.addWidget(permission)
    include_audio = QCheckBox("Include Shirabe pronunciation")
    include_audio.setChecked(False)
    include_audio.setToolTip("Optional verified Shirabe M4A word recordings. Permitted for Anki deck synchronization and export. Recordings count toward the offline storage limit; unavailable audio does not prevent definition lookup.")
    layout.addWidget(include_audio)
    audio_permission = QLabel("Shirabe permits these recordings in synced and exported Anki decks. Other audio sources are not included.")
    audio_permission.setWordWrap(True)
    layout.addWidget(audio_permission)
    include_examples = QCheckBox("Include Tatoeba examples")
    include_examples.setChecked(False)
    include_examples.setEnabled(offline.public_example_corpus(dictionaries))
    include_examples.setToolTip("Optional public corpus text only, with one translation in your preferred available language. No sentence audio or private corpora. Definitions and pronunciation take priority within the request budget.")
    layout.addWidget(include_examples)
    examples_permission = QLabel("Tatoeba contributors permit reuse under CC BY 2.0 FR. Source and license links stay with the examples in exported snapshots."
                                if include_examples.isEnabled() else "Public corpus examples are unavailable: Tatoeba's reuse license could not be verified.")
    examples_permission.setWordWrap(True)
    layout.addWidget(examples_permission)
    layout.addWidget(QLabel("Dictionaries to include"))
    scroll = QScrollArea()
    scroll.setObjectName("shirabe-dictionaries")
    scroll.setWidgetResizable(True)
    inner = QWidget()
    choices = QVBoxLayout(inner)
    boxes = []
    for dictionary in dictionaries:
        if dictionary.get("type") == "sentence":
            continue
        source = dictionary.get("source") or {}
        entry_count = dictionary.get("entryCount")
        count_label = f"{entry_count:,} {'entry' if entry_count == 1 else 'entries'}" if entry_count is not None else "entries unknown"
        label = f"{dictionary['name']} ({count_label})"
        box = QCheckBox(label)
        official = dictionary.get("visibility") == "official" and bool(source.get("license"))
        box.setChecked(official)
        boxes.append((dictionary["slug"], box))
        choices.addWidget(box)
        notice = QLabel((f"License: {source.get('license', 'not provided')}. {source.get('licenseUrl', '')}" if official else
                         "Select only if you may copy its excerpts into synced or exported decks."))
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        notice.setMinimumHeight(notice.fontMetrics().lineSpacing() * 3)
        choices.addWidget(notice)
    scroll.setWidget(inner)
    choices.setAlignment(Qt.AlignmentFlag.AlignTop)
    inner.ensurePolished()
    scroll.setFixedHeight(min(210, inner.sizeHint().height() + 12))
    layout.addWidget(scroll)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    start = buttons.button(QDialogButtonBox.StandardButton.Ok)
    start.setText("Prepare selected notes")
    start.setEnabled(False)
    def enable(*_):
        start.setEnabled(permission.isChecked() and any(box.isChecked() for _, box in boxes) and storage[0].bytes_used < limit.value() * 1024 * 1024)
    limit.valueChanged.connect(enable)
    permission.toggled.connect(enable)
    for _, box in boxes:
        box.toggled.connect(enable)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    outer.addWidget(buttons)
    available = dialog.screen().availableGeometry()
    width = min(available.width() - 40, max(620, body.minimumSizeHint().width() + 60))
    dialog.resize(width, min(available.height() - 80, layout.totalHeightForWidth(width - 40) + buttons.sizeHint().height() + 60))
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return {slug for slug, box in boxes if box.isChecked()}, limit.value(), include_audio.isChecked(), include_examples.isChecked()


def prepare_selected(browser, config, save_config):
    """Entry point for the Browser menu. Consent is scoped to this selection."""
    from aqt.operations import CollectionOp, QueryOp
    from aqt.qt import QDialog, QLabel, QProgressBar, QPushButton, QTimer, QVBoxLayout, Qt
    from aqt.utils import showInfo, showWarning
    from aqt.webview import AnkiWebView

    if getattr(browser, "_shirabe_offline", None):
        browser._shirabe_offline.raise_()
        return
    note_ids = list(browser.selected_notes())
    if not 0 < len(note_ids) <= MAX_SELECTED_NOTES:
        showWarning("Select between 1 and 200 notes for offline preparation.", parent=browser)
        return
    try:
        backend = offline.DictionaryBackend(config.get("apiBase", "https://shirabe.org"), config.get("cardToken", ""))
    except offline.SnapshotError as error:
        showWarning(str(error), parent=browser)
        return

    class Workflow(QDialog):
        def __init__(self):
            super().__init__(browser)
            self.setWindowTitle("Preparing Shirabe offline lookup")
            available = self.screen().availableGeometry()
            self.resize(min(680, available.width() - 40), min(500, available.height() - 40))
            self.setWindowModality(Qt.WindowModality.WindowModal)
            self.cancelled = threading.Event()
            self.finished_work = False
            self.captured = []
            self.texts = {}
            self.static_only = set()
            self.render_index = 0
            self.layout = QVBoxLayout(self)
            self.label = QLabel("Reading dictionary preferences…")
            self.label.setWordWrap(True)
            self.layout.addWidget(self.label)
            self.progress = QProgressBar()
            self.progress.setRange(0, len(note_ids))
            self.layout.addWidget(self.progress)
            self.web = AnkiWebView(self)
            self.layout.addWidget(self.web)
            self.stop = QPushButton("Stop preparation")
            self.stop.clicked.connect(self.reject)
            self.layout.addWidget(self.stop)
            self.backend = backend
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(lambda: self.fail(offline.SnapshotError("Card text collection timed out")))

        def reject(self):
            if self.finished_work:
                return super().reject()
            self.cancelled.set()
            self.stop.setEnabled(False)
            self.label.setText("Stopping; finished notes will be saved.")

        def start(self):
            self.show()
            QueryOp(parent=self, op=lambda _: (self.backend.me(), self.backend.dictionaries()), success=self.metadata).without_collection().failure(self.fail).run_in_background()

        def metadata(self, answer):
            if self.cancelled.is_set():
                return self.close_work()
            try:
                self.backend.check_access()
            except offline.SnapshotError as error:
                return self.fail(error)
            me, dictionaries = answer
            if "user:preferences:read" not in me["credential"]["scopes"]:
                return self.fail(offline.SnapshotError("Reconnect to Shirabe to permit dictionary preferences"))
            self.preferences = me["preferences"]
            self.dictionaries = dictionaries["dictionaries"]
            QueryOp(parent=self, op=lambda col: (capture_notes(col, note_ids), offline.snapshot_storage(col.media, audio=True)), success=self.review).failure(self.fail).run_in_background()

        def review(self, data):
            captured, usage = data
            if self.cancelled.is_set():
                return self.close_work()
            choice = _consent(self, len(note_ids), self.dictionaries, usage, config)
            if not choice:
                return self.close_work()
            self.approved, limit, self.include_audio, self.include_examples = choice
            try:
                self.backend.check_access()
                self.backend = offline.DictionaryBackend(config.get("apiBase", "https://shirabe.org"), config.get("cardToken", ""),
                                                         locale=self.preferences["uiLocale"],
                                                         stack_fingerprint=self.preferences["stackFingerprint"],
                                                         dictionaries=approved_dictionary_slugs(self.preferences, self.approved, self.dictionaries))
            except offline.SnapshotError as error:
                return self.fail(error)
            self.config = {**config, "offlineStorageMiB": limit}
            save_config(self.config)
            self.render(captured)

        def render(self, captured):
            self.captured = captured
            self.sides = [(note.note_id, ordinal, html) for note in captured for ordinal, html in note.sides]
            self.label.setText("Collecting visible card text…")
            addon = browser.mw.addonManager.addonFromModule(__name__)
            self.web.stdHtml('<main id="qa"></main>', js=[f"/_addons/{addon}/web/shirabe-card.js"], context=self,
                             head=f'<meta http-equiv="Content-Security-Policy" content="{CAPTURE_CSP}">')
            self.next_side()

        def next_side(self):
            if self.cancelled.is_set() or self.render_index == len(self.sides):
                self.timer.stop()
                self.web.hide()
                self.label.setText("Downloading bounded dictionary excerpts…")
                return QueryOp(parent=self, op=lambda _: prepare_batch(self.captured, self.texts, self.backend,
                               preferences=self.preferences, approved=self.approved, cancelled=self.cancelled.is_set,
                               sources=self.dictionaries, static_only=self.static_only, include_audio=self.include_audio, include_examples=self.include_examples,
                               progress=lambda done, total: browser.mw.taskman.run_on_main(lambda: self.progress.setValue(done))),
                               success=self.save).without_collection().failure(self.fail).run_in_background()
            note_id, ordinal, html = self.sides[self.render_index]
            self.timer.start(10000)
            self.web.evalWithCallback(capture_script(html, ordinal), lambda answer: self.collected(note_id, answer))

        def collected(self, note_id, answer):
            if self.finished_work:
                return
            self.timer.stop()
            if not isinstance(answer, dict) or answer.get("error"):
                if isinstance(answer, dict) and answer.get("error") == "missing_bundle":
                    return self.fail(offline.SnapshotError("Reinstall the Shirabe add-on: its offline text collector is missing"))
                self.texts[note_id] = None
            elif self.texts.get(note_id, []) is not None:
                if answer.get("scripted"):
                    self.static_only.add(note_id)
                runs = self.texts.setdefault(note_id, [])
                for text in answer.get("texts", []):
                    if text not in runs:
                        runs.append(text)
                if len(runs) > offline.MAX_SEGMENTS or sum(len(t.encode("utf-16-le")) // 2 for t in runs) > offline.MAX_TEXT_UNITS:
                    self.texts[note_id] = None
            self.render_index += 1
            self.next_side()

        def save(self, batch):
            if self.finished_work:
                return
            try:
                self.backend.check_access()
            except offline.SnapshotError as error:
                return self.fail(error)
            self.stop.setEnabled(False)
            self.label.setText("Saving offline references…")
            CollectionOp(parent=self, op=lambda col: apply_prepared(col, batch, self.config, check_access=self.backend.check_access)).success(lambda result: self.done_batch(batch, result)).failure(self.fail).run_in_background()

        def done_batch(self, batch, result):
            self.close_work()
            detail = (f"Saved {result.saved} notes; {result.prepared}/{result.tokens} dictionary lookups prepared. "
                      f"Added {format_bytes(result.bytes_used)} of snapshot and pronunciation media. "
                      f"The saved notes reference {len(result.audio_files)} pronunciation files.\n"
                      f"Skipped {batch.skipped} notes without prepared data and {result.conflicts} edited notes. "
                      f"Kept {result.kept_previous} previous snapshots; {result.partial} refreshes were partial.\n"
                      "Sync media before reviewing offline. Undo reverses note and template changes; media remains available for cleanup.")
            if batch.stopped:
                detail += "\nPreparation stopped before finishing the selection."
            issues = result.issues | batch.issue_notes.keys()
            if issues:
                detail += "\n" + "\n".join((f"{batch.issue_notes[issue]} note(s): " if issue in batch.issue_notes else "") + ISSUES.get(issue, "Some requested data was unavailable.") for issue in sorted(issues))
            showInfo(detail, parent=browser)

        def fail(self, error):
            self.cancelled.set()
            self.close_work()
            message = str(error) if isinstance(error, offline.SnapshotError) else "Offline preparation could not finish. Existing snapshots were retained."
            showWarning(message, parent=browser)

        def close_work(self):
            if self.finished_work:
                return
            self.finished_work = True
            self.timer.stop()
            self.web.cleanup()
            browser._shirabe_offline = None
            self.accept()
            self.deleteLater()

    try:
        workflow = Workflow()
        browser._shirabe_offline = workflow
        workflow.start()
    except offline.SnapshotError as error:
        showWarning(str(error), parent=browser)
