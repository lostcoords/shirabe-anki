"""Native authoring entrypoints. Card messages may propose Lookup data only.

open_add_note opens Anki's Add window; preview_update and enrich_selected_notes
require native field approval before persisting. apply_previews is for the
native confirmation callback, never a JavaScript bridge handler.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from html.parser import HTMLParser
from typing import Callable, Mapping

from .bridge import MAX_MESSAGE_BYTES


ROLE_ORDER = ("expression", "reading", "meanings", "sentence", "translation", "pitch", "audio", "source", "entry")
ROLES = frozenset(ROLE_ORDER)
POLICIES = frozenset({"keep", "fill", "append", "replace", "refresh"})
PROVENANCE_KEY = "shirabe.authoring.generated."
MAX_BATCH = 100
MAX_DEFINITION_CHARS = 16_000


class AuthoringError(ValueError):
    pass


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"rt", "rp", "script", "style"}:
            self.hidden += 1
        if tag in {"br", "div", "p", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"rt", "rp", "script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _PlainText()
    parser.feed(value)
    return "".join(parser.parts).strip()


def identity_text(value: str, *, reading: bool = False, entry: bool = False) -> str:
    if entry:
        parser = _PlainText()
        parser.feed(value)
        return "".join(parser.parts).strip("\n")
    value = unicodedata.normalize("NFKC", plain_text(value)).strip()
    if reading:
        value = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in value)
    return value


def field_is_empty(value: str) -> bool:
    return not plain_text(value) and not re.search(r"<(?:img|audio|video|object|iframe)\b|\[sound:", value, re.I)


def escaped_text(value: str) -> str:
    return html.escape(value, quote=True).replace("[", "&#91;").replace("{", "&#123;").replace("\n", "<br>")


def _text(value, name: str, limit: int, *, required=False) -> str:
    if not isinstance(value, str):
        raise AuthoringError(f"Invalid {name}.")
    if len(value) > limit:
        action = "Choose fewer meanings." if name in {"meaning", "definition"} else "Choose less text."
        raise AuthoringError(f"The {name} exceeds {limit:,} characters. {action}")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise AuthoringError(f"Invalid {name}.")
    if required and not value.strip():
        raise AuthoringError(f"Choose a {name} first.")
    return value


@dataclass(frozen=True)
class Example:
    japanese: str
    translation: str = ""
    source: str = ""
    reference: str = ""
    translation_language: str = ""

    def __post_init__(self):
        for name in ("japanese", "translation", "source", "reference"):
            _text(getattr(self, name), name, 8000, required=name == "japanese")
        _text(self.translation_language, "translation language", 16)


@dataclass(frozen=True)
class Lookup:
    surface: str = ""
    lemma: str = ""
    reading: str = ""
    entry: str = ""
    word_id: str = ""
    meanings: tuple[str, ...] = ()
    sentence: str = ""
    translation: str = ""
    pitch: tuple[int, ...] = ()
    source: str = ""
    expression: str = ""
    examples: tuple[Example, ...] = ()
    sense_refs: tuple[str, ...] = ()
    audio_urls: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("surface", "lemma", "reading", "expression"):
            _text(getattr(self, name), name, 256)
        if not (self.lemma.strip() or self.surface.strip()):
            raise AuthoringError("Choose a word first.")
        _text(self.entry, "entry", 256)
        _text(self.word_id, "word id", 512)
        if self.entry:
            parts = self.entry.split(":", 1)
            if len(parts) != 2 or not all(part.strip() for part in parts) or any(unicodedata.category(c) == "Cc" for c in self.entry):
                raise AuthoringError("This dictionary entry reference is invalid. Look the word up again.")
        for name in ("sentence", "translation", "source"):
            _text(getattr(self, name), name, 8000)
        if not isinstance(self.meanings, (tuple, list)) or len(self.meanings) > 100:
            raise AuthoringError("Too many meanings.")
        for meaning in self.meanings:
            _text(meaning, "meaning", MAX_DEFINITION_CHARS)
        if not isinstance(self.pitch, (tuple, list)) or len(self.pitch) > 32:
            raise AuthoringError("Invalid pitch patterns.")
        if any(type(p) is not int or not 0 <= p <= 100 for p in self.pitch):
            raise AuthoringError("Invalid pitch downstep.")
        object.__setattr__(self, "meanings", tuple(self.meanings))
        object.__setattr__(self, "pitch", tuple(self.pitch))
        if not isinstance(self.examples, (list, tuple)) or len(self.examples) > 50 or any(not isinstance(e, Example) for e in self.examples):
            raise AuthoringError("Invalid example choices.")
        if not isinstance(self.sense_refs, (list, tuple)) or len(self.sense_refs) > 100:
            raise AuthoringError("Invalid meaning references.")
        for ref in self.sense_refs:
            _text(ref, "meaning reference", 256)
        object.__setattr__(self, "examples", tuple(self.examples))
        object.__setattr__(self, "sense_refs", tuple(self.sense_refs))
        if not isinstance(self.audio_urls, (tuple, list)) or len(self.audio_urls) > 32:
            raise AuthoringError("Invalid audio suggestions.")
        for url in self.audio_urls:
            _text(url, "audio URL", 2048)
        object.__setattr__(self, "audio_urls", tuple(self.audio_urls))

    @classmethod
    def from_payload(cls, payload: dict) -> Lookup:
        """Parse a small, plain-text proposal with no note IDs, media URLs or paths."""
        if not isinstance(payload, dict) or set(payload) - (set(cls.__dataclass_fields__) - {"examples", "audio_urls"}):
            raise AuthoringError("Unsupported authoring proposal.")
        try:
            if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise AuthoringError("This selection is too large. Choose fewer meanings or examples.")
            return cls(**payload)
        except UnicodeEncodeError as error:
            raise AuthoringError("This selection contains invalid text. Select the word again.") from error
        except (TypeError, RecursionError) as error:
            raise AuthoringError("Invalid authoring proposal.") from error

    @classmethod
    def from_selection(cls, selection: Mapping) -> Lookup:
        """Parse a dictionary selection without choosing an example or media.

        audioUrls are untrusted suggestions and confer no download permission;
        licensed audio is selected separately through authoring_media.
        """
        allowed = {"surface", "lemma", "entry", "expression", "reading", "contextSentence", "senseRefs",
                   "definition", "examples", "sourceUrl", "pitch", "audioUrls"}
        if not isinstance(selection, Mapping) or set(selection) - allowed:
            raise AuthoringError("Unsupported authoring selection.")
        try:
            if len(json.dumps(dict(selection), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise AuthoringError("This selection is too large. Choose fewer meanings or examples.")
        except UnicodeEncodeError as error:
            raise AuthoringError("This selection contains invalid text. Select the word again.") from error
        except (TypeError, RecursionError) as error:
            raise AuthoringError("Invalid authoring selection.") from error
        examples = selection.get("examples", [])
        if not isinstance(examples, list) or len(examples) > 50:
            raise AuthoringError("Invalid example choices.")
        choices = []
        for example in examples:
            if not isinstance(example, dict) or set(example) - {"japanese", "translation", "source", "reference", "translationLanguage"}:
                raise AuthoringError("Invalid example.")
            choices.append(Example(example.get("japanese", ""), example.get("translation") or "",
                                   example.get("source", ""), example.get("reference", ""), example.get("translationLanguage", "")))
        definition = _text(selection.get("definition", ""), "definition", MAX_DEFINITION_CHARS)
        audio = selection.get("audioUrls", [])
        if not isinstance(audio, list) or len(audio) > 32:
            raise AuthoringError("Invalid audio choices.")
        for url in audio:
            _text(url, "audio URL", 2048)
        return cls(surface=selection.get("surface", ""), lemma=selection.get("lemma", ""),
                   entry=selection.get("entry") or "", expression=selection.get("expression", ""),
                   reading=selection.get("reading") or "", sentence=selection.get("contextSentence") or "",
                   meanings=(definition,) if definition else (), examples=tuple(choices),
                   sense_refs=selection.get("senseRefs", ()), pitch=selection.get("pitch", ()),
                   source=selection.get("sourceUrl", ""), audio_urls=audio)


@dataclass(frozen=True)
class FieldProfile:
    name: str
    note_type_id: int
    fields: Mapping[str, str]
    deck_id: int | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self):
        _text(self.name, "profile name", 100, required=True)
        if type(self.note_type_id) is not int or self.note_type_id <= 0:
            raise AuthoringError("Choose a note type.")
        if self.deck_id is not None and (type(self.deck_id) is not int or self.deck_id <= 0):
            raise AuthoringError("Choose a deck.")
        if not isinstance(self.fields, Mapping) or set(self.fields) - ROLES:
            raise AuthoringError("Unsupported field mapping.")
        for name in self.fields.values():
            _text(name, "field name", 256, required=True)
        if len(set(self.fields.values())) != len(self.fields):
            raise AuthoringError("Map each note field only once.")
        if not isinstance(self.tags, (list, tuple)) or len(self.tags) > 50:
            raise AuthoringError("Too many tags.")
        for tag in self.tags:
            _text(tag, "tag", 100, required=True)
            if any(c.isspace() for c in tag):
                raise AuthoringError("Separate tags with spaces.")
        object.__setattr__(self, "fields", {role: self.fields[role] for role in ROLE_ORDER if role in self.fields})
        object.__setattr__(self, "tags", tuple(self.tags))

    def validate(self, model: dict) -> None:
        if not model or model["id"] != self.note_type_id:
            raise AuthoringError("The mapped note type no longer exists.")
        missing = set(self.fields.values()) - {f["name"] for f in model["flds"]}
        if missing:
            raise AuthoringError("Update the mapping for missing fields: " + ", ".join(sorted(missing)))
        if not self.fields:
            raise AuthoringError("Map at least one field.")


def profiles_from_config(config: dict) -> list[FieldProfile]:
    return [FieldProfile(**row) for row in config.get("authoringProfiles", [])]


def remember_profile(config: dict, profile: FieldProfile, *, previous: FieldProfile | None = None) -> dict:
    replaced = {(p.note_type_id, p.name) for p in (profile, previous) if p is not None}
    profiles = [p for p in profiles_from_config(config) if (p.note_type_id, p.name) not in replaced]
    return {**config, "authoringProfiles": [asdict(p) for p in [*profiles, profile]],
            "authoringLastProfile": [profile.note_type_id, profile.name]}


def suggest_mapping(model: dict, existing: Mapping[str, str] | None = None) -> dict[str, str]:
    """Offer familiar names for native review; never save a suggestion."""
    aliases = {
        "expression": ("Expression", "Word", "Vocabulary", "単語", "表現", "Front"),
        "reading": ("Reading", "Kana", "Yomikata", "読み", "読み方"),
        "meanings": ("Meaning", "Meanings", "Definition", "Definitions", "意味", "Back"),
        "sentence": ("Sentence", "Example", "ExampleSentence", "例文"),
        "translation": ("Translation", "SentenceTranslation", "SentenceMeaning", "例文訳", "訳"),
        "pitch": ("Pitch", "PitchAccent", "アクセント"), "audio": ("Audio", "Sound", "Pronunciation", "音声"),
        "source": ("Source", "SourceUrl", "URL", "出典"),
        "entry": ("Entry", "EntryId", "EntryReference", "ShirabeEntry"),
    }
    normalize = lambda name: re.sub(r"[\s_-]", "", name).casefold()
    names = {normalize(f["name"]): f["name"] for f in model["flds"]}
    result = dict(existing or {})
    for role in ROLE_ORDER:
        if role not in result:
            candidate = next((names[normalize(alias)] for alias in aliases[role]
                              if normalize(alias) in names and names[normalize(alias)] not in result.values()), None)
            if candidate:
                result[role] = candidate
    return result


@dataclass
class PresetResult:
    changes: object
    profile: FieldProfile


def create_preset(col) -> PresetResult:
    """Native button operation. Creates a separate type; never changes an existing one."""
    if col.models.by_name("Shirabe Japanese"):
        raise AuthoringError("Shirabe Japanese already exists. Choose it from the note type list.")
    model = col.models.new("Shirabe Japanese")
    fields = dict(zip(ROLE_ORDER, ("Expression", "Reading", "Meaning", "Sentence", "Translation", "Pitch", "Audio", "Source", "Entry")))
    for name in fields.values():
        col.models.add_field(model, col.models.new_field(name))
    template = col.models.new_template("Japanese")
    template["qfmt"] = "{{Expression}}"
    template["afmt"] = ('{{FrontSide}}<hr id="answer">{{Reading}}<br>{{Meaning}}<br>'
                        '{{Sentence}}<br>{{Translation}}<br>{{Pitch}}<br>{{Audio}}<br>{{Source}}')
    col.models.add_template(model, template)
    changes = col.models.add_dict(model)
    return PresetResult(changes.changes, FieldProfile("Japanese", changes.id, fields))


@dataclass(frozen=True)
class NoteChoices:
    kind: str = "vocabulary"
    headword: str = "lemma"
    sense_indexes: tuple[int, ...] | None = None
    cloze_span: tuple[int, int] | None = None
    cloze_number: int = 1
    example_index: int | None = None
    include_sentence: bool = True
    include_translation: bool = True


def proposed_fields(lookup: Lookup, profile: FieldProfile, model: dict, choices: NoteChoices, *, creating=True) -> dict[str, str]:
    profile.validate(model)
    if choices.example_index is not None:
        if type(choices.example_index) is not int or not 0 <= choices.example_index < len(lookup.examples):
            raise AuthoringError("The chosen example is no longer available.")
        example = lookup.examples[choices.example_index]
        source = "\n".join(s for s in (lookup.source, example.source, example.reference) if s)
        lookup = replace(lookup, sentence=example.japanese, translation=example.translation, source=source)
    if choices.kind not in {"vocabulary", "sentence", "cloze"} or choices.headword not in {"lemma", "surface", "expression"}:
        raise AuthoringError("Invalid note format.")
    indexes = choices.sense_indexes if choices.sense_indexes is not None else range(len(lookup.meanings))
    if any(type(i) is not int or not 0 <= i < len(lookup.meanings) for i in indexes):
        raise AuthoringError("A selected meaning is no longer available.")
    sentence = escaped_text(lookup.sentence)
    if choices.kind in {"sentence", "cloze"} and (not lookup.sentence.strip() or "sentence" not in profile.fields):
        raise AuthoringError("Choose a sentence and map its field.")
    if choices.kind in {"sentence", "cloze"} and not choices.include_sentence:
        raise AuthoringError("Include the sentence for this note format.")
    if choices.kind == "cloze":
        span = choices.cloze_span
        if model.get("type") != 1 or not span or len(span) != 2 or any(type(n) is not int for n in span):
            raise AuthoringError("Choose a cloze note type and select the text to hide.")
        start, end = span
        if not 0 <= start < end <= len(lookup.sentence) or lookup.sentence[start:end] != lookup.surface:
            raise AuthoringError("The selected cloze text has changed.")
        if type(choices.cloze_number) is not int or not 1 <= choices.cloze_number <= 499:
            raise AuthoringError("Use a cloze number from 1 to 499.")
        if "{{" in lookup.sentence or "}}" in lookup.sentence:
            raise AuthoringError("Select a sentence without existing cloze deletions.")
        target = profile.fields["sentence"]
        if not any(re.search(r"\{\{cloze:" + re.escape(target) + r"\}\}", t.get("qfmt", "")) for t in model.get("tmpls", [])):
            raise AuthoringError("The sentence field must appear with a cloze filter on the front template.")
        sentence = (escaped_text(lookup.sentence[:start]) + "{{c" + str(choices.cloze_number) + "::"
                    + escaped_text(lookup.sentence[start:end]) + "}}" + escaped_text(lookup.sentence[end:]))
    elif model.get("type") == 1:
        if creating:
            raise AuthoringError("Select Cloze and the text to hide for this note type.")
        sentence = ""
    expression = getattr(lookup, choices.headword) or lookup.lemma or lookup.surface
    if choices.kind == "sentence":
        expression = lookup.sentence
    return {
        "expression": escaped_text(expression), "reading": escaped_text(lookup.reading),
        "meanings": "<br>".join(escaped_text(lookup.meanings[i]) for i in dict.fromkeys(indexes)),
        "sentence": sentence if choices.include_sentence else "",
        "translation": escaped_text(lookup.translation) if choices.include_translation else "",
        "pitch": ", ".join(str(p) for p in dict.fromkeys(lookup.pitch)),
        "source": escaped_text(lookup.source), "entry": escaped_text(lookup.entry), "audio": "",
    }


@dataclass(frozen=True)
class NoteSnapshot:
    note_id: int
    note_type_id: int
    guid: str
    mod: int
    usn: int
    fields: tuple[tuple[str, str], ...]
    tags: tuple[str, ...]
    model_hash: str

    @classmethod
    def capture(cls, note) -> NoteSnapshot:
        return cls(note.id, note.mid, note.guid, note.mod, note.usn,
                   tuple(zip(note.keys(), note.fields)), tuple(note.tags),
                   content_hash(json.dumps(note.note_type(), sort_keys=True, ensure_ascii=False)))

    def matches(self, note) -> bool:
        return self == self.capture(note)


class NoteDraft:
    """A captured note that can cross threads without accessing its collection."""

    def __init__(self, snapshot: NoteSnapshot, model: dict):
        self.id, self.mid, self.guid = snapshot.note_id, snapshot.note_type_id, snapshot.guid
        self.mod, self.usn = snapshot.mod, snapshot.usn
        self.fields, self.tags = [v for _, v in snapshot.fields], list(snapshot.tags)
        self._names, self._model = [n for n, _ in snapshot.fields], deepcopy(model)

    @classmethod
    def capture(cls, note):
        return cls(NoteSnapshot.capture(note), note.note_type())

    def keys(self):
        return self._names[:]

    def note_type(self):
        return self._model

    def __getitem__(self, name):
        return self.fields[self._names.index(name)]

    def __setitem__(self, name, value):
        self.fields[self._names.index(name)] = value


@dataclass(frozen=True)
class FieldChange:
    role: str
    field: str
    before: str
    proposed: str
    after: str
    policy: str
    reason: str = ""


@dataclass(frozen=True)
class NotePreview:
    snapshot: NoteSnapshot
    changes: tuple[FieldChange, ...]
    tags: tuple[str, ...]
    entry: str
    source: str
    sense_refs: tuple[str, ...] = ()

    def field_values(self) -> dict[str, str]:
        return dict(self.snapshot.fields) | {c.field: c.after for c in self.changes}

    @property
    def changed(self) -> bool:
        return any(c.before != c.after for c in self.changes) or self.tags != self.snapshot.tags


def build_preview(note, profile: FieldProfile, lookup: Lookup, *, choices: NoteChoices | None = None,
                  policies: Mapping[str, str] | None = None, provenance: dict | None = None, media=None) -> NotePreview:
    """Build a field diff without changing the note; policies are keyed by role."""
    values = proposed_fields(lookup, profile, note.note_type(), choices or NoteChoices(), creating=not note.id)
    source = lookup.source
    if media is not None:
        from .authoring_media import ImportedMedia
        if not isinstance(media, ImportedMedia) or not re.fullmatch(r"shirabe_[a-f0-9]{64}\.(mp3|ogg|wav|m4a)", media.filename):
            raise AuthoringError("Choose imported Anki audio for this field.")
        values["audio"] = f"[sound:{media.filename}]"
        source = "\n".join(s for s in (source, media.attribution, media.license) if s)
        values["source"] = escaped_text(source)
    policies, provenance = policies or {}, provenance or {}
    if set(policies) - set(profile.fields) or set(policies.values()) - POLICIES:
        raise AuthoringError("Invalid field update choice.")
    changes = []
    for role, name in profile.fields.items():
        before, proposed = note[name], values[role]
        policy = policies.get(role, "fill")
        after, reason = before, ""
        if proposed and policy != "keep":
            if field_is_empty(before) or policy == "replace":
                after = proposed
            elif policy == "append" and proposed not in before:
                after = before + "<br>" + proposed
            elif policy == "refresh":
                generated = provenance.get(name, {})
                if generated.get("hash") == content_hash(before) and generated.get("refreshable", True):
                    after = proposed
                else:
                    reason = "user-edited" if name in provenance else "not-generated"
        changes.append(FieldChange(role, name, before, proposed, after, policy, reason))
    tags = list(note.tags)
    for tag in profile.tags:
        if tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(tag)
    refs = lookup.sense_refs
    if choices and choices.sense_indexes is not None and len(refs) == len(lookup.meanings):
        refs = tuple(refs[i] for i in choices.sense_indexes)
    return NotePreview(NoteSnapshot.capture(note), tuple(changes), tuple(tags), lookup.entry, source, refs)


def provenance_for(col, note_id: int) -> dict:
    return col.get_config(PROVENANCE_KEY + str(note_id), {})


def _field_provenance(preview, change):
    value = {"hash": content_hash(change.after), "entry": preview.entry, "source": preview.source, "role": change.role,
             "refreshable": change.policy != "append" or field_is_empty(change.before)}
    if change.role == "meanings":
        value["senses"] = list(preview.sense_refs)
    return value


def record_created_note(col, note_id: int, preview: NotePreview, *, undo_step: int):
    """Merge generated-field history into the native Add note undo step.

    Fields edited in Anki before Save stay user-owned. If another operation
    reached the undo stack first, leave it untouched.
    """
    from anki.collection import OpChanges
    if col.undo_status().last_step != undo_step:
        return OpChanges()
    note = col.get_note(note_id)
    if note.mid != preview.snapshot.note_type_id:
        return OpChanges()
    provenance = {c.field: _field_provenance(preview, c) for c in preview.changes
                  if c.after and c.after != c.before and c.field in note and note[c.field] == c.after}
    if not provenance:
        return OpChanges()
    col.set_config(PROVENANCE_KEY + str(note_id), provenance, undoable=True)
    return col.merge_undo_entries(undo_step)


@dataclass
class ApplyResult:
    changes: object
    applied: tuple[int, ...] = ()
    conflicts: tuple[int, ...] = ()
    skipped: tuple[int, ...] = ()


def apply_previews(col, previews: list[NotePreview], *, cancelled: Callable[[], bool] = lambda: False) -> ApplyResult:
    """Apply native-approved previews in one undo step, called inside CollectionOp.

    No network or Qt calls belong here. Conflicting rows are skipped; a retry
    of an already applied preview conflicts and cannot append its fields again.
    """
    from anki.collection import OpChanges
    from anki.errors import NotFoundError

    if len(previews) > MAX_BATCH or len({p.snapshot.note_id for p in previews}) != len(previews):
        raise AuthoringError(f"Select at most {MAX_BATCH} different notes per update.")
    notes, accepted, conflicts, skipped = [], [], [], []
    for preview in previews:
        nid = preview.snapshot.note_id
        if nid <= 0:
            raise AuthoringError("New notes must be saved in Anki's Add window.")
        try:
            note = col.get_note(nid)
        except NotFoundError:
            conflicts.append(nid)
            continue
        if not preview.snapshot.matches(note):
            conflicts.append(nid)
            continue
        if not preview.changed:
            skipped.append(nid)
            continue
        for name, value in preview.field_values().items():
            note[name] = value
        note.tags = list(preview.tags)
        notes.append(note)
        accepted.append(preview)
    if cancelled():
        return ApplyResult(OpChanges(), skipped=tuple(p.snapshot.note_id for p in previews))
    if not notes:
        return ApplyResult(OpChanges(), conflicts=tuple(conflicts), skipped=tuple(skipped))
    undo = col.add_custom_undo_entry("Shirabe: enrich notes")
    try:
        col.update_notes(notes)
        changes = col.merge_undo_entries(undo)
        for preview in accepted:
            provenance = dict(provenance_for(col, preview.snapshot.note_id))
            for change in preview.changes:
                if change.before != change.after:
                    provenance[change.field] = _field_provenance(preview, change)
            col.set_config(PROVENANCE_KEY + str(preview.snapshot.note_id), provenance, undoable=True)
            changes = col.merge_undo_entries(undo)
    except Exception:
        col.merge_undo_entries(undo)
        col.undo()
        raise
    return ApplyResult(changes, tuple(n.id for n in notes), tuple(conflicts), tuple(skipped))


def open_add_note(mw, lookup: Lookup, profile: FieldProfile | None = None, **kwargs):
    """Native UI entrypoint; kwargs include config, save_config, parent, choices.

    on_closed() runs once after cancellation or native draft handoff;
    on_error(exception) handles failure and is followed by on_closed().
    """
    from .authoring_ui import open_add_note as open_native
    return open_native(mw, lookup, profile, **kwargs)


def preview_update(mw, note_id: int, lookup: Lookup, profile: FieldProfile | None = None, **kwargs):
    """note_id comes from native context. on_closed() spans all async callbacks.

    kwargs: config, save_config, parent, editor, choices, on_done(ApplyResult),
    on_closed(), on_error(exception). on_closed follows cancellation, conflict,
    error or persisted completion, exactly once.
    """
    from .authoring_ui import preview_update as open_native
    return open_native(mw, note_id, lookup, profile, **kwargs)


def find_existing_notes(col, lookup: Lookup, profiles: list[FieldProfile], **kwargs):
    from .collection import find_existing_notes as find
    return [candidate.note_id for candidate in find(col, lookup, profiles, **kwargs)]


def enrich_selected_notes(browser, profiles: list[FieldProfile], resolve: Callable, **kwargs):
    """Resolve only Browser-selected identities off-thread, then show native previews.

    resolve(identity) returns a sequence of Lookup candidates. It receives no
    collection object or full note text. kwargs: on_done, policies, cancelled.
    """
    from .authoring_ui import enrich_selected_notes as enrich
    return enrich(browser, profiles, resolve, **kwargs)


def configure_profiles(mw, config: dict, save_config: Callable, *, note_type_id=None, parent=None):
    """Settings button: edit a reusable mapping and return it, or None on Cancel."""
    from .authoring_ui import configure_profiles as configure
    return configure(mw, config, save_config, note_type_id=note_type_id, parent=parent)


def choose_profile(mw, config: dict, save_config: Callable, *, note_type_id=None, parent=None):
    """Choose a saved profile, offering configuration when none matches."""
    from .authoring_ui import choose_profile as choose
    return choose(mw, config, save_config, note_type_id=note_type_id, parent=parent)


def configure(mw, config: dict, save_config: Callable):
    return configure_profiles(mw, config, save_config)


def open_editor_lookup(editor, config: dict, save_config: Callable):
    """Editor button callback; opens a native selection-only online lookup panel."""
    from .authoring_ui import open_editor_lookup as open_lookup
    return open_lookup(editor, config, save_config)


def enrich_browser_selection(browser, config: dict, save_config: Callable):
    """Browser menu callback using the native read-only DictionaryBackend."""
    from .authoring_ui import enrich_browser_selection as enrich
    return enrich(browser, config, save_config)


def propose(mw, config: dict, save_config: Callable, payload: dict, context_note=None, *, editor=None):
    """Open native approval after the bridge validates its current card context.

    payload is {action: add|update|find, selection: Lookup.from_selection-compatible dict}, or
    {action: ..., lookup: Lookup.from_payload-compatible dict}. context_note is
    a Note resolved natively; editor is its optional native Editor.
    """
    if not isinstance(payload, dict) or set(payload) not in ({"action", "selection"}, {"action", "lookup"}):
        raise AuthoringError("Unsupported authoring action.")
    action = payload["action"]
    if action not in {"add", "update", "find"}:
        raise AuthoringError("Unsupported authoring action.")
    lookup = Lookup.from_selection(payload["selection"]) if "selection" in payload else Lookup.from_payload(payload["lookup"])
    if action == "update" and context_note is None:
        raise AuthoringError("There is no current note to update.")
    from . import authoring_ui
    if action == "find":
        return authoring_ui.show_existing_notes(mw, lookup, profiles_from_config(config))
    kwargs = {"config": config, "save_config": save_config}
    if action == "add":
        return open_add_note(mw, lookup, **kwargs)
    return preview_update(mw, context_note.id, lookup, editor=editor, **kwargs)
