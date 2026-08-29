"""Answering "do I know this word" from the reader's own collection.

This is the part no other surface can do. The browser extension and Nadeshiko
ask Shirabe what you know, because a Shirabe account is the only thing they can
see. An add-on is inside Anki, so it can answer from the deck you actually
study, which for an Anki user is the better answer and needs no account at all.

The mapping is deliberately crude, and it should stay crude until someone shows
it is not enough: a word is "known" if any note contains it in a field, with a
card that is not new. Anki has no notion of which word a note is ABOUT, so
anything cleverer would be us guessing at their note types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from anki.collection import Collection

# Anki's own card queue/type numbers (see anki/consts.py). A card that has never
# been studied is `new`; anything else has been seen at least once.
CARD_TYPE_NEW = 0


def states_for(col: "Collection", words: list[str]) -> dict[str, str]:
    """`{word: state}` for the card, using only what the collection knows.

    States are the card's own vocabulary: `none` (no note), `new` (a note exists
    but has never been studied), `learning` (studied, not yet mature) and
    `retired` (mature). `due` is deliberately not returned: whether a card is due
    is a scheduling fact that Anki already shows you, and colouring a word by it
    would compete with Anki's own queue rather than tell you something new.
    """
    out: dict[str, str] = {}
    for word in words:
        out[word] = _state_for(col, word)
    return out


def _state_for(col: "Collection", word: str) -> str:
    # `search_notes` is quoted through Anki's own escaping: a word can contain
    # characters its search syntax reads (`*`, `_`, `:`), and a hand-built query
    # would either fail or match the wrong notes.
    query = col.build_search_string(word)
    note_ids = col.find_notes(query)
    if not note_ids:
        return "none"

    intervals = []
    for note_id in note_ids:
        for card in col.get_note(note_id).cards():
            if card.type == CARD_TYPE_NEW:
                continue
            intervals.append(card.ivl)
    if not intervals:
        return "new"
    # Anki calls a card mature at 21 days, and so does its own statistics.
    return "retired" if max(intervals) >= 21 else "learning"
