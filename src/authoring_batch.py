"""Bounded selected-note lookup, retry and cancellation before native approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Callable

from .authoring import (
    AuthoringError, FieldProfile, Lookup, MAX_BATCH, NoteDraft, NoteSnapshot,
    identity_text, provenance_for,
)


@dataclass(frozen=True)
class LookupIdentity:
    expression: str
    reading: str = ""
    entry: str = ""


@dataclass
class BatchRow:
    snapshot: NoteSnapshot
    model: dict
    profile: FieldProfile | None = None
    identity: LookupIdentity | None = None
    provenance: dict = field(default_factory=dict)
    status: str = "pending"
    candidates: tuple[Lookup, ...] = ()
    selected: int | None = None

    def note_copy(self):
        return NoteDraft(self.snapshot, self.model)

    def choose(self, index: int):
        if type(index) is not int or not 0 <= index < len(self.candidates):
            raise AuthoringError("Choose one of this note's dictionary matches.")
        self.selected, self.status = index, "ready"

    def set_profile(self, profile):
        profile.validate(self.model)
        self.profile = profile
        self.candidates, self.selected = (), None
        note = self.note_copy()
        values = {role: identity_text(note[name], reading=role == "reading", entry=role == "entry")
                  for role, name in profile.fields.items() if role in {"expression", "reading", "entry"}}
        expression = values.get("expression", "")
        if not expression or len(expression) > 256 or "{{" in expression or "[sound:" in expression:
            self.status, self.identity = "missing-identity", None
        else:
            self.identity = LookupIdentity(expression, values.get("reading", ""), values.get("entry", ""))
            self.status = "pending"


class Batch:
    """capture uses collection access; prepare receives only a bounded resolver.

    resolve(LookupIdentity) returns at most 20 Lookup candidates, with its own
    request timeout. Call cancel from the UI thread; progress(done, total) runs
    in the worker and must marshal UI changes through taskman.run_on_main.
    """

    def __init__(self):
        self.rows = []
        self._cancel = Event()
        self.external_cancelled = lambda: False
        self._cache = {}
        self.requests = 0

    def cancel(self):
        self._cancel.set()

    def cancelled(self):
        return self._cancel.is_set() or self.external_cancelled()

    def resume(self):
        self._cancel.clear()

    def capture(self, col, note_ids, profiles):
        ids = tuple(dict.fromkeys(note_ids))
        if len(ids) > MAX_BATCH or any(type(n) is not int or n <= 0 for n in ids):
            raise AuthoringError(f"Select at most {MAX_BATCH} saved notes.")
        rows = []
        for nid in ids:
            note = col.get_note(nid)
            row = BatchRow(NoteSnapshot.capture(note), note.note_type(), provenance=provenance_for(col, nid))
            matches = [p for p in profiles if p.note_type_id == note.mid]
            if len(matches) != 1:
                row.status = "choose-profile" if matches else "unmapped"
            else:
                try:
                    row.set_profile(matches[0])
                except AuthoringError:
                    row.status = "choose-profile"
            rows.append(row)
        return rows

    def prepare(self, resolve: Callable, *, progress=lambda done, total: None):
        retryable = {"pending", "failed", "cancelled"}
        for done, row in enumerate(self.rows, 1):
            if row.status not in retryable:
                continue
            if self.cancelled():
                row.status = "cancelled"
                continue
            try:
                if row.identity not in self._cache:
                    self.requests += 1
                    result = resolve(row.identity)
                    if not isinstance(result, (tuple, list)) or len(result) > 20 or any(not isinstance(c, Lookup) for c in result):
                        raise AuthoringError("Invalid dictionary candidates.")
                    self._cache[row.identity] = tuple(result)
                candidates = self._cache[row.identity]
                if row.identity.entry:
                    candidates = tuple(c for c in candidates if c.entry == row.identity.entry)
                if row.identity.reading:
                    candidates = tuple(c for c in candidates if identity_text(c.reading, reading=True) == row.identity.reading)
                row.candidates = tuple(dict.fromkeys(candidates))
                if self.cancelled():
                    row.status = "cancelled"
                elif len(row.candidates) == 1:
                    row.choose(0)
                else:
                    row.status = "ambiguous" if row.candidates else "unmatched"
            except Exception:
                row.status = "failed"
            progress(done, len(self.rows))
        return self.rows
