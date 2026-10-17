"""Example corpus searches through a native, read-only JSON fetch callback."""

from __future__ import annotations

from .authoring import AuthoringError, Example, Lookup


def search_examples(fetch_json, lookup: Lookup, *, language="en", min_length=1, max_length=120,
                    take=20, cancelled=lambda: False) -> tuple[Example, ...]:
    """fetch_json(path, params) runs off the UI thread and enforces its timeout.

    One request is made. A selected word pins the corpus search; without one,
    the corpus uses the surface. The current note is not uploaded.
    """
    if language not in {"en", "es", "fr", "de", "pt", "ru", "nl", "hu", "sv", "ja"}:
        raise AuthoringError("Unsupported translation language.")
    if not 1 <= min_length <= max_length <= 8000 or not 1 <= take <= 50:
        raise AuthoringError("Invalid example filters.")
    languages = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu", "pt": "por", "ru": "rus",
                 "nl": "nld", "hu": "hun", "sv": "swe", "ja": "jpn"}
    requests = []
    surface = lookup.surface or lookup.lemma
    if 0 < len(surface) <= 64:
        params = {"query": surface, "limit": take}
        if lookup.word_id:
            params["filter[wordId]"] = lookup.word_id
        params["filter[translationLanguages][]"] = language
        requests.append(("/api/v1/sentences", params, "sentences"))
    result, seen = [], set()
    for path, params, key in requests:
        if cancelled() or len(result) >= take:
            break
        payload = fetch_json(path, params)
        rows = payload.get(key, []) if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 100:
            raise AuthoringError("Invalid example corpus response.")
        for row in rows:
            if not isinstance(row, dict):
                raise AuthoringError("Invalid example sentence.")
            japanese = row.get("japanese", "")
            if not isinstance(japanese, str) or not min_length <= len(japanese) <= max_length:
                continue
            translations = row.get("translations", [])
            if not isinstance(translations, list) or len(translations) > 100:
                raise AuthoringError("Invalid example translations.")
            translation = next((t.get("text", "") for t in translations if isinstance(t, dict)
                                and t.get("lang") in {language, languages[language]}), "")
            if not translation and language != "ja":
                continue
            reference = row.get("id", "")
            source_data = row.get("source")
            source = source_data.get("name", "") if isinstance(source_data, dict) else source_data
            source = source or ("Tatoeba" if reference.startswith("tatoeba:") else reference.partition(":")[0])
            example = Example(japanese, translation, source, reference, language if translation else "")
            identity = (japanese, translation)
            if identity not in seen:
                result.append(example)
                seen.add(identity)
            if len(result) >= take:
                break
    return tuple(result)


def search_examples_from_config(config, lookup, *, language=None, max_length=120, cancelled=lambda: False):
    """Native corpus request using the account's language choice by default."""
    from .offline import DictionaryBackend
    from urllib.parse import urlencode
    backend = DictionaryBackend(config.get("apiBase", "https://shirabe.org"), config.get("cardToken", ""))
    preferences = backend.me()["preferences"]
    language = language or next(iter(preferences.get("languages") or ["en"]))
    backend._locale = language
    def fetch(path, params):
        return backend._request(path.removeprefix("/api/v1") + "?" + urlencode(params))
    return search_examples(fetch, lookup, language=language, max_length=max_length, cancelled=cancelled)
