"""Bounded dictionary candidates for editor and Browser authoring."""

from __future__ import annotations

from urllib.parse import quote, urlencode

from .authoring import AuthoringError, Example, Lookup, _text, identity_text


LANGUAGES = {"en": "eng", "es": "spa", "ja": "jpn", "fr": "fre", "de": "ger", "nl": "dut",
             "ru": "rus", "sv": "swe", "hu": "hun", "sl": "slv"}


def _language(code):
    return LANGUAGES.get(code, code)


def candidates_from_response(response, *, surface: str, language="en", languages=None, api_base="https://shirabe.org") -> tuple[Lookup, ...]:
    """Adapt the read-only identify response, retaining entry identity and senses."""
    if not isinstance(response, dict) or not isinstance(response.get("words"), list) or len(response["words"]) != 1:
        raise AuthoringError("Invalid dictionary response.")
    word = response["words"][0]
    if word is None:
        return ()
    if not isinstance(word, dict):
        raise AuthoringError("Invalid dictionary word.")
    rows = word.get("candidates", [])
    if not isinstance(rows, list) or len(rows) > 20:
        raise AuthoringError("Too many dictionary candidates.")
    languages = tuple(dict.fromkeys(_language(code) for code in (languages or (language,))))
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise AuthoringError("Invalid dictionary candidate.")
        entry = f"{row.get('dictionary', '')}:{row.get('sourceId', '')}"
        entries = row.get("entries", [])
        if not isinstance(entries, list) or len(entries) > 16 or any(not isinstance(e, dict) or type(e.get("order")) is not int for e in entries):
            raise AuthoringError("Invalid dictionary entries.")
        meanings, refs = [], []
        for source in sorted(entries, key=lambda e: e["order"]):
            senses = source.get("senses", [])
            if not isinstance(senses, list) or len(senses) > 64:
                raise AuthoringError("Invalid dictionary senses.")
            for sense in senses:
                if not isinstance(sense, dict):
                    raise AuthoringError("Invalid dictionary sense.")
                definitions = sense.get("definitions", [])
                if not isinstance(definitions, list) or len(definitions) > 32:
                    raise AuthoringError("Invalid definitions.")
                if any(not isinstance(d, dict) for d in definitions):
                    raise AuthoringError("Invalid definition.")
                selected = [d for lang in languages for d in definitions if _language(d.get("lang")) == lang]
                texts = [_text(d.get("text", ""), "definition", 4000) for d in (selected or definitions)]
                if texts:
                    meanings.append("; ".join(texts))
                    refs.append(f"{source.get('dictionary', '')}#{sense.get('position', 0)}")
        examples = []
        raw_examples = row.get("examples", [])
        if not isinstance(raw_examples, list) or len(raw_examples) > 50:
            raise AuthoringError("Invalid examples.")
        for example in raw_examples:
            if not isinstance(example, dict) or not isinstance(example.get("translations", []), list):
                raise AuthoringError("Invalid example sentence.")
            translation = next((t.get("text", "") for lang in languages for t in example.get("translations", [])
                                if isinstance(t, dict) and _language(t.get("lang")) == lang), "")
            examples.append(Example(example["japanese"], translation, example.get("source", ""), example.get("id", "")))
        result.append(Lookup(surface=surface, lemma=row.get("headword", ""), expression=row.get("headword", ""),
                             reading=identity_text(row.get("reading") or "", reading=True), entry=entry,
                             meanings=tuple(meanings), sense_refs=tuple(refs), examples=tuple(examples),
                             pitch=tuple(p["downstep"] for p in row.get("pitch", [])),
                             source=api_base.rstrip("/") + "/" + language + "/words/" + quote(row.get("id", surface), safe="") + "?entry=" + quote(entry, safe="")))
    return tuple(result)


def resolver_from_config(config):
    """Native-only callback. Requests the selected identity, never whole notes."""
    from .offline import DictionaryBackend
    base, language = config.get("apiBase", "https://shirabe.org"), config.get("locale", "en")
    backend = DictionaryBackend(base, config.get("cardToken", ""), locale=language)
    backend._access()
    preferences = None

    def resolve(identity):
        nonlocal preferences
        if preferences is None:
            preferences = backend.me()["preferences"]
        languages = preferences.get("languages") or [language]
        if identity.entry:
            Lookup(lemma=identity.expression, entry=identity.entry)
            path = "/words/" + quote(identity.expression, safe="") + "?" + urlencode({
                "entry": identity.entry, "include": "examples", "locale": preferences.get("uiLocale", language),
            })
            response = {"words": [{"candidates": [backend._request(path)]}]}
        else:
            token = {"lemma": identity.expression, "surface": identity.expression}
            if identity.reading:
                token["reading"] = identity.reading
            response = backend.identify(token)
        return candidates_from_response(response, surface=identity.expression, language=language, languages=languages, api_base=base)
    return resolve
