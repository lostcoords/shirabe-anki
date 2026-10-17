"""Mapped note identities and collection study states, without an account."""

from __future__ import annotations

from dataclasses import dataclass

from .authoring import AuthoringError, FieldProfile, Lookup, escaped_text, identity_text

CARD_TYPE_NEW = 0


@dataclass(frozen=True)
class ExistingNote:
    note_id: int
    note_type_id: int
    expression: str
    reading: str
    entry: str
    match: str


def _matching_ids(col, profile: FieldProfile, value: str):
    from anki.collection import SearchNode

    # A field search anchors the text and misses ruby readings. Search literally,
    # then compare only the mapped identity fields on each candidate below.
    # Anki searches stored field HTML, including entity-escaped punctuation.
    terms = [SearchNode(literal_text=text) for text in sorted({value, escaped_text(value)})]
    query = col.build_search_string(*terms, joiner="OR")
    return col.find_notes(col.build_search_string(f"mid:{profile.note_type_id}", query))


def find_existing_notes(col, lookup: Lookup, profiles: list[FieldProfile], *, limit: int = 100) -> list[ExistingNote]:
    """Return candidates, never merge. A spelling-only match remains ambiguous."""
    if not 1 <= limit <= 500:
        raise ValueError("Duplicate result limit must be between 1 and 500.")
    found = {}
    for profile in profiles:
        try:
            profile.validate(col.models.get(profile.note_type_id))
        except AuthoringError:
            continue
        candidates = set()
        if lookup.entry and "entry" in profile.fields:
            candidates.update(_matching_ids(col, profile, lookup.entry))
        if "expression" in profile.fields:
            for expression in {lookup.lemma, lookup.surface} - {""}:
                candidates.update(_matching_ids(col, profile, expression))
        for nid in sorted(candidates):
            note = col.get_note(nid)
            values = {role: identity_text(note[name], reading=role == "reading", entry=role == "entry") for role, name in profile.fields.items()}
            expression, reading, entry = (values.get(role, "") for role in ("expression", "reading", "entry"))
            same_reading = bool(reading and lookup.reading and reading == identity_text(lookup.reading, reading=True))
            if entry and lookup.entry:
                if entry != lookup.entry:
                    continue
                match = "entry-reading" if same_reading else "entry"
            elif expression and expression in {identity_text(lookup.lemma), identity_text(lookup.surface)}:
                match = "expression-reading" if same_reading else "expression"
            else:
                continue
            result = ExistingNote(nid, note.mid, expression, reading, entry, match)
            if nid not in found or _rank(result) < _rank(found[nid]):
                found[nid] = result
    return sorted(found.values(), key=lambda n: (_rank(n), n.note_id))[:limit]


def _rank(note: ExistingNote) -> int:
    return ("entry-reading", "entry", "expression-reading", "expression").index(note.match)


def states_for(col, words: list[str], profiles: list[FieldProfile] | None = None, *, allow_heuristic: bool = False) -> dict[str, str]:
    """Mapped expression states. The optional whole-text heuristic is opt-in."""
    out = {}
    for word in dict.fromkeys(words):
        if not word.strip():
            out[word] = "none"
            continue
        matches = find_existing_notes(col, Lookup(surface=word, lemma=word), profiles or [])
        ids = {match.note_id for match in matches if match.expression == identity_text(word)}
        if allow_heuristic:
            mapped_types = {profile.note_type_id for profile in profiles or []}
            for nid in col.find_notes(col.build_search_string(word)):
                if not mapped_types or col.get_note(nid).mid not in mapped_types:
                    ids.add(nid)
        out[word] = _state_for_notes(col, ids)
    return out


def _state_for_notes(col, note_ids) -> str:
    cards = [card for nid in note_ids for card in col.get_note(nid).cards()]
    if not cards:
        return "none"

    intervals = [card.ivl for card in cards if card.type != CARD_TYPE_NEW]
    if not intervals:
        return "new"
    return "retired" if max(intervals) >= 21 else "learning"
