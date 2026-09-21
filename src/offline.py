"""Bounded, portable dictionary results for explicitly selected card text.

Call preparation off the UI thread. Persist through the collection's media
manager, then update the caller-owned note reference only after success.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import base64
import gzip
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Iterable
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit
import zlib

from . import net, offline_audio

FORMAT_VERSION = 1
MAX_SNAPSHOT_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_STORAGE_BYTES = 256 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SEGMENTS = 16
MAX_TEXT_UNITS = 4096
MAX_TOKENS = 128
MAX_CANDIDATES = 3
MAX_RAW_CANDIDATES = 64
MAX_RAW_ENTRIES = 128
MAX_ENTRY_BYTES = 24 * 1024
MAX_REQUESTS = 65
MAX_LOOKUP_CACHE_BYTES = 1024 * 1024
MAX_LOOKUP_CACHE_ENTRIES = 256
MAX_EXAMPLES_PER_ENTRY = 2
MAX_EXAMPLES_PER_NOTE = 8
MAX_EXAMPLE_TEXT_UNITS = 512
MAX_EXAMPLE_BYTES = 8 * 1024
CORPUS_LICENSE = "CC BY 2.0 FR"
CORPUS_LICENSE_URL = "https://creativecommons.org/licenses/by/2.0/fr/"
CORPUS_SENTENCE = re.compile(r"tatoeba:tatoeba_([1-9][0-9]{0,11})\Z")
MEDIA_NAME = re.compile(r"_shirabe_snapshot_v(?:1|2)_([a-f0-9]{64})\.json\Z")
MAX_REFERENCE_CHARS = 4096
_connection_generation = 0


def invalidate_preparations():
    global _connection_generation
    _connection_generation += 1


class SnapshotError(ValueError):
    def __init__(self, message, *, code="lookup_unavailable"):
        super().__init__(message)
        self.code = code


def _issue(error, fallback):
    return error.code if isinstance(error, SnapshotError) else fallback


_JSON_ENCODERS = tuple(json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=ordered)
                       for ordered in (False, True))


def _json(value, *, sort_keys=False) -> bytes:
    return _JSON_ENCODERS[bool(sort_keys)].encode(value).encode("utf-8")


def _bounded(value, remaining, depth=0):
    if remaining < 0 or depth > 16:
        raise SnapshotError("Snapshot data exceeds its limit", code="entry_limit")
    if isinstance(value, str):
        if len(value) > remaining:
            raise SnapshotError("Snapshot text exceeds its limit", code="entry_limit")
        remaining -= len(_json(value))
    elif isinstance(value, (list, dict)):
        if len(value) > remaining // 2:
            raise SnapshotError("Snapshot array exceeds its limit", code="entry_limit")
        remaining -= 2
        for index, key in enumerate(value):
            remaining = _bounded(key, remaining - int(index > 0), depth + 1)
            if isinstance(value, dict):
                remaining = _bounded(value[key], remaining - 1, depth + 1)
    elif value is None or type(value) in (bool, int, float):
        if type(value) is int and value.bit_length() > 54:
            raise SnapshotError("Invalid snapshot number")
        remaining -= len(_json(value))
    else:
        raise SnapshotError("Invalid snapshot value")
    if remaining < 0:
        raise SnapshotError("Snapshot data exceeds its limit", code="entry_limit")
    return remaining


def snapshot_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _string(value, maximum=512):
    if isinstance(value, str) and len(value) > maximum:
        raise SnapshotError("Snapshot text exceeds its limit", code="entry_limit")
    if not isinstance(value, str) or _units(value) > maximum:
        raise SnapshotError("Invalid snapshot text")
    return value


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise SnapshotError("Invalid snapshot number")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise SnapshotError("Invalid snapshot number")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise SnapshotError("Invalid snapshot boolean")
    return value


def _array(shape, maximum):
    def read(value):
        if not isinstance(value, list) or len(value) > maximum:
            raise SnapshotError("Snapshot array exceeds its limit", code="entry_limit")
        return [_clean(shape, item) for item in value]
    return read


def _nullable(shape):
    return lambda value: None if value is None else _clean(shape, value)


def _reveal(value):
    if not isinstance(value, dict) or len(value) > 128:
        raise SnapshotError("Invalid dictionary reveal preferences")
    result = {}
    for key, mode in value.items():
        if mode not in ("show", "hover"):
            raise SnapshotError("Invalid dictionary reveal mode")
        result[_string(key)] = mode
    return result


def _clean(shape, value):
    if callable(shape):
        return shape(value)
    if not isinstance(value, dict):
        raise SnapshotError("Invalid snapshot object")
    result = {}
    for field, reader in shape.items():
        key = field.rstrip("?")
        if field.endswith("?") and key not in value:
            continue
        if key not in value:
            raise SnapshotError("Incomplete snapshot object")
        result[key] = _clean(reader, value[key])
    return result


TAG = {"category": _string, "code": _string, "label": _string, "scope?": _string}
SOURCE_TAG = {"name": _string, "category": _string, "notes?": lambda v: _string(v, 2048),
              "order": _number, "score": _number}
GLOSS = {"lang": _string, "text": lambda v: _string(v, 4096), "title?": lambda v: _string(v, 2048)}
CROSS_REFERENCE = {"text": lambda v: _string(v, 2048), "reading?": _nullable(lambda v: _string(v, 2048)),
                   "label?": lambda v: _string(v, 256)}
SENSE = {
    "position": _integer, "depth?": _integer, "label?": _string,
    "definitions": _array(GLOSS, 32), "usages?": _array(GLOSS, 32),
    "tags": _array(TAG, 32), "sourceTags?": _array(SOURCE_TAG, 128),
    "notes?": _array(lambda v: _string(v, 2048), 16),
    "related?": _array(CROSS_REFERENCE, 32), "antonyms?": _array(CROSS_REFERENCE, 32),
    "seeAlso?": _array(CROSS_REFERENCE, 32),
}
ENTRY = {
    "dictionary": _string, "dictionaryName?": _string,
    "type": _string, "sourceId": _string,
    "sourceHeadwords?": _array({"text": _string, "reading": _string, "tags": _array(SOURCE_TAG, 32),
                                "rules": _array(lambda v: _string(v, 128), 16), "score": _number}, 128),
    "senses": _array(SENSE, 64),
}


def _pitch(value):
    result = _clean({"downstep": _integer, "pattern": _string}, value)
    recording = offline_audio.recording(value.get("recording"))
    if recording is not None:
        result["recording"] = recording
    return result


CANDIDATE = {
    "id": _string, "dictionary": _string, "sourceId": _string,
    "headword": _string, "reading?": _nullable(_string), "common": _boolean,
    "name?": _boolean, "jlpt?": _nullable(_string), "frequency?": _nullable(_integer),
    "furigana?": _array({"text": _string, "ruby?": _string}, 64),
    "pitch": _array(_pitch, 32),
    "forms": _array({"text": _string, "script": _string, "common": _boolean, "tags?": _array(TAG, 32)}, 64),
    "entries": _array(ENTRY, 16),
    "examples?": _array({"japanese": lambda v: _string(v, 2048), "translations?": _array({"lang": _string, "text": lambda v: _string(v, 2048)}, 8)}, 8),
}
CANDIDATE_HEADER = {key: value for key, value in CANDIDATE.items() if key != "entries"}
INFLECTION = {
    "base": _string,
    "forms": _array({"code": _string, "label": lambda value: _string(value, 128)}, 16),
}
TOKEN = {
    "surface": _string, "start": _integer, "length": _integer,
    "entry?": _string, "lemma?": _string, "reading?": _string,
    "pos?": _string, "content?": _boolean,
    "inflection?": _array(lambda value: _string(value, 128), 16), "inflectionBase?": INFLECTION["base"],
}
PREFERENCES = {"languages": _array(_string, 32), "dictionaryReveal": _reveal}
SOURCE = {
    "slug": _string, "name": _string, "attribution?": _nullable(lambda v: _string(v, 4096)),
    "source?": _nullable({"name?": _string, "url?": _string, "license?": _string, "licenseUrl?": _string}),
}


def _candidate(value, allowed_dictionaries, issues, *, include_examples=True):
    if not isinstance(value, dict):
        raise SnapshotError("Invalid dictionary candidate")
    if _string(value.get("dictionary")) not in allowed_dictionaries:
        raise SnapshotError("Dictionary not approved for export", code="dictionary_not_approved")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise SnapshotError("Invalid dictionary entries")
    if not entries:
        raise SnapshotError("Dictionary not approved for export", code="dictionary_not_approved")
    header = {key.rstrip("?"): value[key.rstrip("?")] for key in CANDIDATE_HEADER
              if key.rstrip("?") in value and (include_examples or key != "examples?")}
    header["pitch"] = CANDIDATE["pitch"](value.get("pitch"))
    _bounded(header, MAX_ENTRY_BYTES)
    clean = _clean(CANDIDATE_HEADER, header)
    clean["entries"] = []
    if len(entries) > MAX_RAW_ENTRIES:
        issues.append("entry_limit")
    for raw in entries[:MAX_RAW_ENTRIES]:
        try:
            if not isinstance(raw, dict):
                raise SnapshotError("Invalid dictionary entry")
            if _string(raw.get("dictionary")) not in allowed_dictionaries:
                issues.append("dictionary_not_approved")
                continue
            _bounded(raw, MAX_ENTRY_BYTES)
            entry = _clean(ENTRY, raw)
            if len(clean["entries"]) >= 16 or len(_json({**clean, "entries": [*clean["entries"], entry]})) > MAX_ENTRY_BYTES:
                issues.append("entry_limit")
                continue
            clean["entries"].append(entry)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            issues.append(_issue(error, "lookup_unavailable"))
    return clean if clean["entries"] else None


def _entry_id(candidate):
    return f"{candidate['dictionary']}:{candidate['sourceId']}"


def _candidates(values, allowed_dictionaries, issues, *, entry=None, prefer_words=False, include_examples=True):
    if not isinstance(values, list):
        raise SnapshotError("Invalid dictionary candidates")
    if len(values) > MAX_RAW_CANDIDATES:
        issues.append("candidate_limit")
    candidates = []
    for raw in values[:MAX_RAW_CANDIDATES]:
        try:
            candidate = _candidate(raw, allowed_dictionaries, issues, include_examples=include_examples)
            if candidate is not None:
                candidates.append(candidate)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            issues.append(_issue(error, "lookup_unavailable"))
    if values and not candidates:
        return None
    if prefer_words:
        candidates = [candidate for candidate in candidates if candidate.get("name") is not True or _entry_id(candidate) == entry] or candidates
    if len(candidates) > MAX_CANDIDATES:
        issues.append("candidate_limit")
    selected = candidates[:MAX_CANDIDATES]
    pinned = next((candidate for candidate in candidates if _entry_id(candidate) == entry), None)
    if pinned is not None and pinned not in selected:
        selected[-1] = pinned
    return selected


def _entry_available(token, candidates):
    return not token.get("entry") or any(_entry_id(candidate) == token["entry"] for candidate in candidates)


def _same_parse_tokens(current, saved):
    def without_identity(token):
        return {key: value for key, value in token.items() if key != "entry"}
    return len(current) == len(saved) and all(without_identity(left) == without_identity(right)
                                               for left, right in zip(current, saved))


def _allocate_results(snapshot, pending, maximum, issues):
    alternatives = []
    for segment, result in pending:
        candidates = result["candidates"]
        token = segment["tokens"][result["tokenIndex"]]
        primary = next((index for index, candidate in enumerate(candidates)
                        if _entry_id(candidate) == token.get("entry")), 0)
        selected = {primary} if candidates else set()
        result["candidates"] = [candidates[primary]] if candidates else []
        segment["results"].append(result)
        if len(_json(snapshot)) > maximum:
            segment["results"].pop()
            issues.append("byte_limit")
        else:
            alternatives.append((result, candidates, selected))
    for result, candidates, selected in alternatives:
        for index, candidate in enumerate(candidates):
            if index in selected:
                continue
            position = sum(previous < index for previous in selected)
            result["candidates"].insert(position, candidate)
            if len(_json(snapshot)) > maximum:
                result["candidates"].pop(position)
                issues.append("byte_limit")
            else:
                selected.add(index)


def _tokens(text, values):
    tokens = _array(TOKEN, MAX_TOKENS)(values)
    encoded = text.encode("utf-16-le")
    end = 0
    for token in tokens:
        start, length = token["start"], token["length"]
        if not length or length != _units(token["surface"]) or start < end or encoded[start * 2:(start + length) * 2] != token["surface"].encode("utf-16-le"):
            raise SnapshotError("Snapshot offsets do not match its text")
        end = start + length
    return tokens


def _parsed_tokens(text, answer):
    results = answer.get("results") if isinstance(answer, dict) else None
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise SnapshotError("Invalid parser response")
    raw_tokens = results[0].get("tokens")
    if not isinstance(raw_tokens, list) or len(raw_tokens) > MAX_TOKENS:
        raise SnapshotError("Invalid parser response")
    values = []
    for raw in raw_tokens:
        if not isinstance(raw, dict):
            raise SnapshotError("Invalid parser token")
        start = raw.get("startOffset")
        end = raw.get("endOffset")
        value = {"surface": raw.get("surface"), "start": start,
                 "length": end - start if isinstance(start, int) and isinstance(end, int) else None,
                 "content": raw.get("kind") not in ("function", "symbol")}
        for key in ("lemma", "reading"):
            if raw.get(key) is not None:
                value[key] = raw[key]
        if raw.get("partOfSpeech") is not None:
            value["pos"] = raw["partOfSpeech"]
        if raw.get("inflection") is not None:
            inflection = _clean(INFLECTION, raw["inflection"])
            value["inflection"] = [form["label"] for form in inflection["forms"]]
            value["inflectionBase"] = inflection["base"]
        values.append(value)
    return _tokens(text, values)


def _segment(value, allowed_dictionaries, issues):
    if not isinstance(value, dict):
        raise SnapshotError("Invalid snapshot segment")
    text = _string(value.get("text"), MAX_TEXT_UNITS)
    if snapshot_hash(text) != value.get("contentHash"):
        raise SnapshotError("Snapshot text hash does not match")
    tokens = _tokens(text, value.get("tokens"))
    results = []
    seen = set()
    raw_results = value.get("results")
    if not isinstance(raw_results, list) or len(raw_results) > MAX_TOKENS:
        raise SnapshotError("Invalid snapshot results")
    for raw in raw_results:
        try:
            index = _integer(raw["tokenIndex"])
            if index >= len(tokens) or index in seen:
                raise SnapshotError("Invalid snapshot result index")
            candidates = _candidates(raw["candidates"], allowed_dictionaries, issues, entry=tokens[index].get("entry"))
            if candidates is None:
                continue
            results.append({"tokenIndex": index, "candidates": candidates})
            seen.add(index)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            issues.append(_issue(error, "lookup_unavailable"))
            continue
    return {"text": text, "contentHash": snapshot_hash(text), "tokens": tokens, "results": results}


def validate_snapshot(value, *, allowed_dictionaries=None):
    """Read bounded JSON; malformed segments are misses, not display failures."""
    try:
        if isinstance(value, (str, bytes)):
            if len(value) > MAX_SNAPSHOT_BYTES or (isinstance(value, str) and len(value.encode("utf-8")) > MAX_SNAPSHOT_BYTES):
                return None
            value = json.loads(value)
        _bounded(value, MAX_SNAPSHOT_BYTES)
        if not isinstance(value, dict) or value.get("formatVersion") != FORMAT_VERSION:
            return None
        version = _string(value.get("contentVersion"), 128)
        if not version:
            return None
        created = _integer(value.get("createdAt"))
        values = value.get("segments")
        if not isinstance(values, list) or len(values) > MAX_SEGMENTS:
            return None
        segments = []
        issues = []
        units = tokens = 0
        for raw in values:
            try:
                allowed = allowed_dictionaries
                if allowed is None:
                    allowed = _AllDictionaries()
                segment = _segment(raw, allowed, issues)
                units += _units(segment["text"])
                tokens += len(segment["tokens"])
                if units > MAX_TEXT_UNITS or tokens > MAX_TOKENS:
                    return None
                if any(s["text"] == segment["text"] for s in segments):
                    return None
                segments.append(segment)
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        clean = {"formatVersion": FORMAT_VERSION, "contentVersion": version, "createdAt": created, "segments": segments}
        offline_audio.limit_recordings(clean)
        clean["complete"] = value.get("complete") is True and not issues and len(segments) == len(values) and coverage(clean).complete
        if "preferences" in value:
            clean["preferences"] = _clean(PREFERENCES, value["preferences"])
        if "sources" in value:
            clean["sources"] = _array(SOURCE, 128)(value["sources"])
        return clean if len(_json(clean)) <= MAX_SNAPSHOT_BYTES else None
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


class _AllDictionaries:
    def __contains__(self, _):
        return True


@dataclass(frozen=True)
class Budget:
    max_bytes: int = MAX_SNAPSHOT_BYTES
    max_requests: int = MAX_REQUESTS

    def __post_init__(self):
        if type(self.max_bytes) is not int or not 512 <= self.max_bytes <= MAX_SNAPSHOT_BYTES:
            raise ValueError("Snapshot budget must be between 512 bytes and 128 KiB")
        if type(self.max_requests) is not int or not 0 <= self.max_requests <= MAX_REQUESTS:
            raise ValueError("Request budget must be between 0 and 65")


@dataclass(frozen=True)
class Coverage:
    segments: int
    tokens: int
    prepared: int
    complete: bool


def coverage(snapshot) -> Coverage:
    segments = snapshot["segments"]
    tokens = sum(sum(t.get("content") is not False for t in s["tokens"]) for s in segments)
    prepared = sum(sum(s["tokens"][r["tokenIndex"]].get("content") is not False
                       and _entry_available(s["tokens"][r["tokenIndex"]], r["candidates"]) for r in s["results"]) for s in segments)
    return Coverage(len(segments), tokens, prepared, tokens == prepared and snapshot.get("complete", True))


@dataclass(frozen=True)
class Preparation:
    snapshot: dict
    coverage: Coverage
    bytes_used: int
    requests: int
    issues: tuple[str, ...]
    media: dict = field(default_factory=dict)

    @property
    def complete(self):
        return self.coverage.complete and not self.issues


def public_example_corpus(sources):
    if not isinstance(sources, (tuple, list)) or len(sources) > 128:
        return False
    for row in sources:
        if not isinstance(row, dict) or (row.get("slug"), row.get("type"), row.get("visibility")) != ("tatoeba", "sentence", "official"):
            continue
        source = row.get("source")
        if isinstance(source, dict) and (source.get("url"), source.get("license"), source.get("licenseUrl")) == (
                "https://tatoeba.org", CORPUS_LICENSE, CORPUS_LICENSE_URL):
            return True
    return False


def _snapshot_candidates(snapshot):
    for segment in snapshot["segments"]:
        for result in segment["results"]:
            yield from result["candidates"]


def _has_examples(snapshot):
    return bool(snapshot and any(candidate.get("examples") for candidate in _snapshot_candidates(snapshot)))


def _corpus_examples(answer, languages, issues):
    rows = answer.get("sentences") if isinstance(answer, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_EXAMPLES_PER_ENTRY:
        raise SnapshotError("Sentence response exceeds the example limit", code="examples_limit")
    pagination = answer.get("pagination")
    if not isinstance(pagination, dict) or type(pagination.get("hasMore")) is not bool:
        raise SnapshotError("Invalid sentence pagination", code="examples_unavailable")
    iso = {"eng": "en", "spa": "es", "fra": "fr", "deu": "de", "por": "pt", "rus": "ru",
           "nld": "nl", "hun": "hu", "swe": "sv", "jpn": "ja"}
    wanted = [iso.get(language, language) for language in languages]
    examples = []
    seen = set()
    for row in rows:
        try:
            identity = _string(row.get("id"), 64) if isinstance(row, dict) else ""
            match = CORPUS_SENTENCE.fullmatch(identity)
            if not match:
                raise SnapshotError("Unverified example source")
            japanese = _string(row.get("japanese"), MAX_EXAMPLE_TEXT_UNITS)
            if not japanese.strip():
                raise SnapshotError("Empty example")
            raw = row.get("translations")
            if not isinstance(raw, list) or len(raw) > 32:
                raise SnapshotError("Invalid example translations")
            translations = {}
            for translation in raw:
                try:
                    language = _string(translation.get("lang"), 16) if isinstance(translation, dict) else ""
                    language = iso.get(language, language)
                    if language not in wanted or language == "ja" or language in translations:
                        continue
                    text = _string(translation.get("text"), MAX_EXAMPLE_TEXT_UNITS)
                    if text.strip():
                        translations[language] = text
                except (ValueError, TypeError, KeyError):
                    issues.append("examples_unavailable")
            chosen = next(({"lang": lang, "text": translations[lang]} for lang in wanted if lang in translations), None)
            example = {"japanese": japanese, "translations": [chosen] if chosen else []}
            key = _json(example)
            if key not in seen:
                examples.append((example, "https://tatoeba.org/en/sentences/show/" + match[1]))
                seen.add(key)
        except (ValueError, TypeError, KeyError, OverflowError):
            issues.append("examples_unavailable")
    return examples


def _prepare_examples(snapshot, fetch, request, issues, maximum, sources, cancelled):
    if not public_example_corpus(sources):
        issues.append("examples_unavailable")
        return
    base_sources = [source for source in snapshot.get("sources", []) if source["slug"] != "tatoeba"]
    before = len(_json(snapshot))
    cache, links = {}, []
    count = 0
    for candidate in _snapshot_candidates(snapshot):
        if cancelled():
            issues.append("cancelled")
            break
        if count >= MAX_EXAMPLES_PER_NOTE:
            issues.append("examples_limit")
            break
        identity = _entry_id(candidate)
        if identity not in cache:
            try:
                answer = request(fetch, candidate)
                if cancelled():
                    issues.append("cancelled")
                    break
                cache[identity] = _corpus_examples(answer, snapshot.get("preferences", {}).get("languages", ["en"]), issues)
            except Exception as error:
                cache[identity] = []
                issues.append(_issue(error, "examples_unavailable"))
        for example, link in cache[identity]:
            if count >= MAX_EXAMPLES_PER_NOTE:
                issues.append("examples_limit")
                break
            previous_sources = snapshot.get("sources")
            proposed_links = list(dict.fromkeys([*links, link]))
            notice = {"slug": "tatoeba", "name": "Tatoeba",
                      "attribution": "Sentences and translations from Tatoeba contributors (CC BY 2.0 FR).\n" + "\n".join(proposed_links),
                      "source": {"name": "Tatoeba", "url": "https://tatoeba.org", "license": CORPUS_LICENSE, "licenseUrl": CORPUS_LICENSE_URL}}
            snapshot["sources"] = [*base_sources, notice]
            candidate.setdefault("examples", []).append(example)
            size = len(_json(snapshot))
            if (size > maximum or size - before > MAX_EXAMPLE_BYTES or len(_json(candidate)) > MAX_ENTRY_BYTES
                    or len(snapshot["sources"]) > 128):
                candidate["examples"].pop()
                if not candidate["examples"]:
                    del candidate["examples"]
                if previous_sources is None:
                    del snapshot["sources"]
                else:
                    snapshot["sources"] = previous_sources
                issues.append("examples_limit")
                continue
            links = proposed_links
            count += 1


class LookupCache:
    """Batch-local, bounded copies of validated candidates before optional enrichment."""

    def __init__(self, backend, *, max_bytes=MAX_LOOKUP_CACHE_BYTES, max_entries=MAX_LOOKUP_CACHE_ENTRIES):
        if (type(max_bytes) is not int or not 0 < max_bytes <= MAX_LOOKUP_CACHE_BYTES
                or type(max_entries) is not int or not 0 < max_entries <= MAX_LOOKUP_CACHE_ENTRIES):
            raise ValueError("Lookup cache exceeds its byte or entry limit")
        self._identify = backend.identify
        self._check_access = backend.check_access
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._scope = None
        self._entries = OrderedDict()
        self.bytes_used = 0

    def bind(self, identify, version, allowed, preferences):
        if self._identify != identify:
            raise ValueError("A lookup cache belongs to one dictionary backend")
        scope = (version, allowed, _json(preferences))
        if self._scope != scope:
            self._entries.clear()
            self.bytes_used = 0
            self._scope = scope

    def get(self, query, entry):
        self._check_access()
        key = _json([query, entry])
        data = self._entries.get(key)
        if data is None:
            return None
        self._entries.move_to_end(key)
        return json.loads(data)

    def put(self, query, entry, candidates):
        key, data = _json([query, entry]), _json(candidates)
        size = len(key) + len(data)
        if size > self._max_bytes:
            return
        previous = self._entries.pop(key, None)
        if previous is not None:
            self.bytes_used -= len(key) + len(previous)
        while self._entries and (len(self._entries) >= self._max_entries or self.bytes_used + size > self._max_bytes):
            old_key, old_data = self._entries.popitem(last=False)
            self.bytes_used -= len(old_key) + len(old_data)
        self._entries[key] = data
        self.bytes_used += size


def prepare_snapshot(texts: Iterable[str], *, parse: Callable, identify: Callable,
                     content_version: str, allowed_dictionaries: Iterable[str],
                     previous=None, budget: Budget = Budget(),
                     cancelled: Callable[[], bool] = lambda: False, now: Callable = time.time,
                     preferences: dict | None = None, sources=(), static_text_only=False, audio=None, examples=None,
                     lookup_cache: LookupCache | None = None) -> Preparation:
    """Prepare exact visible segments using API-shaped parse/identify callbacks.

    `content_version` identifies dictionary versions, ordering and locale.
    `allowed_dictionaries` contains only dictionaries approved for redistribution.
    Prior results fill failed refreshes only for identical text and content version.
    """
    version = _string(content_version, 128)
    if not version:
        raise SnapshotError("A content version is required")
    allowed = frozenset(allowed_dictionaries)
    prior = validate_snapshot(previous, allowed_dictionaries=allowed)
    old = {s["text"]: s for s in prior["segments"]} if prior and prior["contentVersion"] == version else {}
    snapshot = {"formatVersion": FORMAT_VERSION, "contentVersion": version, "createdAt": _integer(int(now() * 1000)), "segments": [], "complete": False}
    if preferences is not None:
        snapshot["preferences"] = _clean(PREFERENCES, preferences)
    if lookup_cache is not None:
        lookup_cache.bind(identify, version, allowed, snapshot.get("preferences"))
    if sources:
        snapshot["sources"] = [source for source in _array(SOURCE, 128)(sources) if source["slug"] in allowed]
    if len(_json(snapshot)) > budget.max_bytes:
        raise SnapshotError("Dictionary metadata exceeds the snapshot budget", code="byte_limit")
    issues = ["static_text_only"] if static_text_only else []
    requests = total_units = total_tokens = 0
    seen = set()
    pending = []

    def request(call, value):
        nonlocal requests
        if cancelled():
            raise SnapshotError("Preparation cancelled", code="cancelled")
        if requests >= budget.max_requests:
            raise SnapshotError("Preparation request limit reached", code="request_limit")
        requests += 1
        return call(value)

    for index, text in enumerate(texts):
        if index >= MAX_SEGMENTS:
            issues.append("segment_limit")
            break
        if cancelled():
            issues.append("cancelled")
            break
        try:
            text = _string(text, MAX_TEXT_UNITS)
            if not text or text in seen:
                continue
            seen.add(text)
            total_units += _units(text)
            if total_units > MAX_TEXT_UNITS:
                issues.append("text_limit")
                break
            saved = old.get(text)
            try:
                tokens = _parsed_tokens(text, request(parse, text))
            except Exception as error:
                if not saved:
                    issues.append(_issue(error, "parse_unavailable"))
                    continue
                tokens = saved["tokens"]
                issues.append("refresh_incomplete")
            total_tokens += len(tokens)
            if total_tokens > MAX_TOKENS:
                issues.append("token_limit")
                break
            segment = {"text": text, "contentHash": snapshot_hash(text), "tokens": tokens, "results": []}
            snapshot["segments"].append(segment)
            if len(_json(snapshot)) > budget.max_bytes:
                snapshot["segments"].pop()
                issues.append("byte_limit")
                break
            if saved and _same_parse_tokens(tokens, saved["tokens"]):
                for token, old_token in zip(tokens, saved["tokens"]):
                    if old_token.get("entry"):
                        token["entry"] = old_token["entry"]
                old_results = {result["tokenIndex"]: result for result in saved["results"]}
            else:
                old_results = {}
            for token_index, token in enumerate(tokens):
                if token.get("content") is False:
                    continue
                result = None
                try:
                    if cancelled():
                        raise SnapshotError("Preparation cancelled", code="cancelled")
                    if not allowed:
                        raise SnapshotError("No dictionaries approved for export", code="dictionary_not_approved")
                    query = {"lemma": token.get("lemma") or token["surface"], "surface": token["surface"]}
                    query.update({key: token[key] for key in ("reading", "pos") if token.get(key)})
                    entry = token.get("entry")
                    candidates = lookup_cache.get(query, entry) if lookup_cache is not None else None
                    if candidates is None:
                        answer = request(identify, query)
                        words = answer["words"]
                        if not isinstance(words, list) or len(words) != 1:
                            raise SnapshotError("Invalid identification response")
                        raw = [] if words[0] is None else words[0]["candidates"]
                        issue_count = len(issues)
                        candidates = _candidates(raw, allowed, issues, entry=entry, prefer_words=True, include_examples=False)
                        if (lookup_cache is not None and candidates is not None and len(issues) == issue_count
                                and _entry_available(token, candidates) and not cancelled()):
                            lookup_cache.put(query, entry, candidates)
                    if candidates and not token.get("entry"):
                        token["entry"] = _entry_id(candidates[0])
                    result = {"tokenIndex": token_index, "candidates": candidates} if candidates is not None else old_results.get(token_index)
                except Exception as error:
                    result = old_results.get(token_index)
                    issues.append(_issue(error, "lookup_unavailable"))
                if result is not None:
                    result = {**result, "candidates": [{k: v for k, v in candidate.items() if k != "examples"} for candidate in result["candidates"]]}
                    if not _entry_available(token, result["candidates"]):
                        issues.append("entry_unavailable")
                    pending.append((segment, result))
                if cancelled():
                    issues.append("cancelled")
                    break
        except (ValueError, TypeError, KeyError, OverflowError):
            issues.append("invalid_text")
    _allocate_results(snapshot, pending, budget.max_bytes, issues)
    old_audio = {(_entry_id(candidate), candidate.get("reading"), pitch["downstep"], pitch["pattern"])
                 for candidate, pitch in offline_audio.pitches(prior) if "recording" in pitch} if audio is not None and prior else set()
    for _, pitch in offline_audio.pitches(snapshot):
        pitch.pop("recording", None)
    media = audio.prepare(snapshot, request, issues, cancelled) if audio is not None else {}
    if old_audio:
        if any((_entry_id(candidate), candidate.get("reading"), pitch["downstep"], pitch["pattern"]) in old_audio
               and "recording" not in pitch for candidate, pitch in offline_audio.pitches(snapshot)):
            issues.append("audio_unavailable")
    for candidate, pitch in reversed(list(offline_audio.pitches(snapshot))):
        if "recording" in pitch and (len(_json(candidate)) > MAX_ENTRY_BYTES or len(_json(snapshot)) > budget.max_bytes):
            del pitch["recording"]
            issues.append("byte_limit")
    media = {name: download for name, download in media.items() if name in offline_audio.recordings(snapshot)}
    if examples is not None:
        maximum = min(budget.max_bytes, audio.remaining_bytes) if audio is not None else budget.max_bytes
        _prepare_examples(snapshot, examples, request, issues, maximum, sources, cancelled)
        if _has_examples(prior) and not _has_examples(snapshot):
            issues.append("examples_unavailable")
    snapshot["complete"] = not issues
    snapshot["complete"] = coverage(snapshot).complete
    if issues and prior and old:
        snapshot["createdAt"] = min(snapshot["createdAt"], prior["createdAt"])
    return Preparation(snapshot, coverage(snapshot), len(_json(snapshot)), requests, tuple(dict.fromkeys(issues)), media)


def snapshot_field(filename: str, recordings=()) -> str:
    """Keep a standard media reference for APKG export; render with Anki's text filter."""
    if not isinstance(filename, str) or not MEDIA_NAME.fullmatch(filename):
        raise SnapshotError("Invalid snapshot filename")
    if not isinstance(recordings, (tuple, list, dict)) or len(recordings) > offline_audio.MAX_NOTE_FILES:
        raise SnapshotError("Invalid recording references")
    names = dict.fromkeys(recordings)
    if any(not isinstance(name, str) or not offline_audio.MEDIA_NAME.fullmatch(name) for name in names):
        raise SnapshotError("Invalid recording reference")
    return (f'{filename}<img src="{filename}" style="display:none">' +
            "".join(f'<audio src="{name}" preload="none" style="display:none"></audio>' for name in names))


class _SnapshotReference(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.image = None
        self.audio = set()
        self.in_audio = False
        self.valid = True

    def handle_data(self, data):
        self.text += data

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        source = attributes.get("src")
        style = attributes.get("style") or ""
        if tag == "audio":
            if (self.image is None or self.in_audio or len(attrs) != len(attributes)
                    or set(attributes) != {"src", "preload", "style"} or attributes.get("preload") != "none"
                    or not isinstance(source, str) or not offline_audio.MEDIA_NAME.fullmatch(source)
                    or source in self.audio or len(self.audio) >= offline_audio.MAX_NOTE_FILES
                    or re.sub(r"\s+", "", style).rstrip(";") != "display:none"):
                self.valid = False
            self.audio.add(source)
            self.in_audio = True
            return
        if (tag != "img" or self.image is not None or len(attributes) != len(attrs)
                or set(attributes) != {"src", "style"} or not isinstance(source, str) or not MEDIA_NAME.fullmatch(source)
                or re.sub(r"\s+", "", style).rstrip(";") != "display:none"):
            self.valid = False
        self.image = source

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "audio" and self.in_audio:
            self.in_audio = False
        else:
            self.valid = False


def snapshot_filename(value) -> str | None:
    """Decode bounded note field markup or a bare filename; never return HTML or paths."""
    if not isinstance(value, str) or len(value) > MAX_REFERENCE_CHARS:
        return None
    value = value.strip()
    if MEDIA_NAME.fullmatch(value):
        return value
    parser = _SnapshotReference()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError):
        return None
    return parser.image if parser.valid and not parser.in_audio and parser.image is not None and parser.text.strip() == parser.image else None


def snapshot_file(snapshot) -> tuple[str, bytes]:
    """Encode a validated snapshot without increasing its stored or decoded size."""
    data = _json(snapshot, sort_keys=True)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("Snapshot data exceeds its limit", code="byte_limit")
    packed = _json({"formatVersion": 2, "encoding": "gzip-base64", "decodedBytes": len(data),
                    "data": base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii")})
    version = 1
    if len(packed) < len(data):
        version, data = 2, packed
    return f"_shirabe_snapshot_v{version}_{hashlib.sha256(data).hexdigest()}.json", data


def _decompress_snapshot(data):
    envelope = json.loads(data)
    if (not isinstance(envelope, dict) or envelope.get("formatVersion") != 2
            or envelope.get("encoding") != "gzip-base64"):
        raise ValueError("Invalid compressed snapshot")
    size, encoded = envelope.get("decodedBytes"), envelope.get("data")
    if type(size) is not int or not 0 < size <= MAX_SNAPSHOT_BYTES or not isinstance(encoded, str):
        raise ValueError("Invalid decoded size")
    compressed = base64.b64decode(encoded, validate=True)
    if base64.b64encode(compressed).decode("ascii") != encoded:
        raise ValueError("Invalid snapshot encoding")
    decoder = zlib.decompressobj(wbits=31)
    raw = decoder.decompress(compressed, size + 1)
    if len(raw) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Invalid compressed stream")
    return raw


def read_snapshot(media, filename: str):
    """Only read this feature's immutable, hash-verified media files."""
    if not isinstance(filename, str) or not MEDIA_NAME.fullmatch(filename):
        return None
    path = Path(media.dir()) / filename
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SNAPSHOT_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SNAPSHOT_BYTES:
                return None
            data = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES or hashlib.sha256(data).hexdigest() != MEDIA_NAME.fullmatch(filename)[1]:
            return None
        if filename.startswith("_shirabe_snapshot_v2_"):
            data = _decompress_snapshot(data)
        return validate_snapshot(data)
    except (OSError, ValueError, TypeError, RecursionError, zlib.error):
        return None


@dataclass(frozen=True)
class StoredSnapshot:
    filename: str
    bytes_used: int
    coverage: Coverage
    kept_previous: bool = False
    recordings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotStorage:
    bytes_used: int
    files: int


def snapshot_storage(media, *, audio=False) -> SnapshotStorage:
    """Count all owned generations, including incomplete or corrupt payloads."""
    size = count = 0
    with os.scandir(media.dir()) as entries:
        for entry in entries:
            if (MEDIA_NAME.fullmatch(entry.name) or (audio and offline_audio.MEDIA_NAME.fullmatch(entry.name))) and entry.is_file(follow_symlinks=False):
                size += entry.stat(follow_symlinks=False).st_size
                count += 1
    return SnapshotStorage(size, count)


def _loses_saved_definitions(previous, proposed):
    if any(previous.get(key) != proposed.get(key) for key in ("contentVersion", "preferences", "sources")):
        return False
    old_segments, new_segments = previous["segments"], proposed["segments"]
    if len(old_segments) != len(new_segments) or any(
            any(old[key] != new[key] for key in ("text", "contentHash", "tokens"))
            for old, new in zip(old_segments, new_segments)):
        return False

    def available(segments):
        return [{(_entry_id(candidate), surface)
                 for result in segment["results"] for candidate in result["candidates"]
                 for surface in (segment["tokens"][result["tokenIndex"]]["surface"],
                                 segment["tokens"][result["tokenIndex"]].get("lemma"), candidate["headword"])}
                for segment in segments]

    old_available, new_available = available(old_segments), available(new_segments)
    for index, segment in enumerate(old_segments):
        for token in segment["tokens"]:
            if token.get("content") is False or not token.get("entry"):
                continue
            key = (token["entry"], token["surface"])
            if key not in new_available[index] and (
                    key in old_available[index] or any(key in old and key not in new
                                                      for old, new in zip(old_available, new_available))):
                return True
    return False


def persist_snapshot(media, preparation: Preparation, *, previous_name: str | None = None,
                     remaining_bytes: int = MAX_TOTAL_BYTES, total_budget_bytes: int = MAX_TOTAL_BYTES) -> StoredSnapshot:
    """Write through Anki media operations; preserve saved definitions on partial refresh.

    The caller decrements its batch's remaining budget and updates ShirabeLookup
    after this returns. No notes or scheduling records are touched here.
    """
    clean = validate_snapshot(preparation.snapshot)
    if clean != preparation.snapshot:
        raise SnapshotError("Invalid preparation")
    if previous_name and not preparation.complete:
        previous = read_snapshot(media, previous_name)
        if previous and (coverage(previous).complete or _loses_saved_definitions(previous, clean) or (_has_examples(previous)
                and set(preparation.issues) & {"examples_limit", "examples_unavailable", "request_limit", "cancelled", "byte_limit"}) or (offline_audio.recordings(previous)
                and set(preparation.issues) & {"audio_limit", "audio_unavailable", "request_limit", "cancelled", "byte_limit"})):
            return StoredSnapshot(previous_name, 0, coverage(previous), True, tuple(offline_audio.recordings(previous)))
    filename, data = snapshot_file(clean)
    existing = read_snapshot(media, filename) == clean
    recordings = offline_audio.recordings(clean)
    pending = {}
    for name, info in recordings.items():
        if offline_audio.verified_size(media, name, expected=info["bytes"]) is not None:
            continue
        download = preparation.media.get(name)
        if download is None or offline_audio.metadata(download) != info or os.path.lexists(Path(media.dir()) / name):
            raise SnapshotError("Prepared recording is missing or damaged", code="audio_unavailable")
        pending[name] = download
    required = (0 if existing else len(data)) + sum(len(download.data) for download in pending.values())
    if not required:
        return StoredSnapshot(filename, 0, coverage(clean), recordings=tuple(recordings))
    if type(remaining_bytes) is not int or not 0 <= remaining_bytes <= MAX_TOTAL_BYTES or required > remaining_bytes:
        raise SnapshotError("Offline preparation storage budget exceeded")
    if type(total_budget_bytes) is not int or not 0 < total_budget_bytes <= MAX_STORAGE_BYTES:
        raise SnapshotError("Collection snapshot budget must be at most 256 MiB")
    if snapshot_storage(media, audio=True).bytes_used + required > total_budget_bytes:
        raise SnapshotError("Collection offline storage budget exceeded; review unused files or raise the limit", code="storage_limit")
    if not existing and os.path.lexists(Path(media.dir()) / filename):
        raise SnapshotError("A conflicting snapshot media file needs review before writing")
    for download in pending.values():
        offline_audio.store(media, download)
    actual = filename if existing else media.write_data(filename, data)
    if actual != filename or read_snapshot(media, actual) != clean:
        raise SnapshotError("Snapshot media write could not be verified")
    return StoredSnapshot(actual, required, coverage(clean), recordings=tuple(recordings))


def cleanup_snapshots(media, candidates: Iterable[str], referenced: Iterable[str]) -> list[str]:
    """Trash only hash-verified owned assets the caller verified as unreferenced."""
    retained = set(referenced)
    unused = [name for name in dict.fromkeys(candidates) if name not in retained and read_snapshot(media, name) is not None]
    if unused:
        media.trash_files(unused)
    return unused


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise SnapshotError("Dictionary request redirected")


class DictionaryBackend:
    """Read-only parse/identify callbacks using the existing cardToken JSON.

    Expired/revoked tokens require the existing sign-in flow; this backend never
    refreshes credentials or exposes a generic card-controlled network proxy.
    """

    def __init__(self, api_base: str, credentials: str, *, locale="en", dictionaries=None, now=time.time, stack_fingerprint=None):
        parsed = urlsplit(api_base)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")))):
            raise SnapshotError("Invalid dictionary server")
        self._base = api_base.rstrip("/")
        self._credentials = credentials
        self._locale = _string(locale, 16)
        self._selection = {}
        if dictionaries is not None:
            selected = _array(lambda slug: _string(slug, 128), 128)(dictionaries)
            if not selected or any(not slug.strip() for slug in selected):
                raise SnapshotError("Choose between 1 and 128 nonempty dictionary slugs")
            self._selection["dictionaries"] = selected
        self._now = now
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._generation = _connection_generation
        self._failure = None
        self._fingerprint = _string(stack_fingerprint, 128) if stack_fingerprint is not None else None

    def check_access(self):
        self._access()

    def _access(self):
        if self._generation != _connection_generation:
            raise SnapshotError("Shirabe connection changed. Start preparation again.", code="reconnect_required")
        if self._failure is not None:
            if self._failure == "server_update_required":
                raise SnapshotError("The connected Shirabe server needs an update before preparing cards. Existing saved definitions still work.", code=self._failure)
            raise SnapshotError("Shirabe access or dictionary settings changed. Start preparation again.", code=self._failure)
        try:
            if not isinstance(self._credentials, str) or len(self._credentials) > 16384:
                raise ValueError()
            stored = json.loads(self._credentials)
            if isinstance(stored, dict) and "shirabe.link" in stored:
                stored = json.loads(stored["shirabe.link"])
            access = stored["access"]
            expiry = stored["expiresAt"]
            if (not isinstance(access, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,4096}", access)
                    or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= self._now() * 1000):
                raise ValueError()
            return access
        except (ValueError, TypeError, KeyError, RecursionError):
            raise SnapshotError("Connect to Shirabe again before preparing offline data", code="reconnect_required") from None

    def _request(self, path, fields=None):
        url = self._base + "/api/v1" + path
        headers = {"Authorization": f"Bearer {self._access()}", "Accept-Language": self._locale}
        request = net.request(url, headers=headers) if fields is None else net.json_request(url, fields, headers=headers)
        try:
            with self._opener.open(request, timeout=15) as response:
                if int(response.headers.get("Content-Length", "0")) > MAX_RESPONSE_BYTES:
                    raise SnapshotError("Dictionary response exceeds 256 KiB", code="response_limit")
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise SnapshotError("Dictionary response exceeds 256 KiB", code="response_limit")
                answer = json.loads(data)
                self.check_access()
                fingerprint = answer.get("stackFingerprint") if isinstance(answer, dict) else None
                required = self._fingerprint is not None and path in ("/parse", "/words/identify")
                if required and fingerprint is None:
                    self._failure = "server_update_required"
                    self.check_access()
                if fingerprint is not None or required:
                    if self._fingerprint is not None and fingerprint != self._fingerprint:
                        self._failure = "dictionary_changed"
                        self.check_access()
                    self._fingerprint = _string(fingerprint, 128)
                return answer
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403):
                self._failure = "reconnect_required"
                raise SnapshotError("Reconnect to Shirabe to permit dictionary preferences", code="reconnect_required") from None
            raise SnapshotError("Dictionary request unavailable") from None
        except SnapshotError:
            raise
        except (OSError, ValueError, urllib.error.URLError, RecursionError):
            raise SnapshotError("Dictionary request unavailable") from None

    def me(self):
        """Read account preferences and the dictionary stack used by offline preparation."""
        account = _clean({
            "credential": {"scopes": _array(_string, 64)},
        }, self._request("/me"))
        account["preferences"] = _clean({"preferences": {"uiLocale": _string}}, self._request("/preferences"))["preferences"]
        stack = _clean({"dictionaryStack": {
            "fingerprint": _string,
            "sources": _array({
                "dictionary": _string, "languages": _array(_string, 32), "reveal": _string,
            }, 128),
        }}, self._request("/dictionary-stack"))["dictionaryStack"]
        sources = stack["sources"]
        references = [f"{source['dictionary']}:{language}" for source in sources for language in source["languages"]]
        languages = list(dict.fromkeys(language for source in sources for language in source["languages"]))
        fingerprint = stack["fingerprint"]
        if self._fingerprint is not None and fingerprint != self._fingerprint:
            self._failure = "dictionary_changed"
            self.check_access()
        self._fingerprint = fingerprint
        account["preferences"].update({
            "dictionaries": references,
            "languages": languages,
            "stackFingerprint": fingerprint,
            "dictionaryReveal": {source["dictionary"]: source["reveal"] for source in sources},
        })
        return account

    def dictionaries(self):
        """Read bounded dictionary metadata without requesting any pack assets."""
        answer = _clean({"dictionaries": _array({
            "slug": _string, "name": _string, "kind": _string,
            "languages": _array(_string, 32), "visibility?": _string,
            "entryCount?": _nullable(_integer), "capabilities": _array(_string, 32),
            "attribution?": _nullable({
                "text?": _string,
                "source": _nullable({"name": _string, "url": _string}),
                "license": _nullable({"name": _string, "url?": _string}),
            }),
        }, 128)}, self._request("/dictionaries"))
        dictionaries = []
        for dictionary in answer["dictionaries"]:
            attribution = dictionary.get("attribution") or {}
            origin = attribution.get("source") or {}
            license = attribution.get("license") or {}
            source = {"name": origin.get("name"), "url": origin.get("url"),
                      "license": license.get("name"), "licenseUrl": license.get("url")}
            dictionaries.append({
                "slug": dictionary["slug"], "name": dictionary["name"], "type": dictionary["kind"],
                "languages": dictionary["languages"], "visibility": dictionary.get("visibility"),
                "entryCount": dictionary.get("entryCount"), "attribution": attribution.get("text"),
                "source": {key: value for key, value in source.items() if value is not None} or None,
            })
        return {"dictionaries": dictionaries}

    def parse(self, text: str):
        return self._request("/parse", {"texts": [_string(text, MAX_TEXT_UNITS)]})

    def identify(self, token: dict):
        query = _clean({"lemma": _string, "surface": _string, "reading?": _string, "pos?": _string}, token)
        return self._request("/words/identify", {
            "tokens": [query], "include": ["pitch", "frequency", "furigana", "jlpt", "forms", "notes", "relations"],
            **self._selection,
        })

    def examples(self, candidate):
        query = urlencode({"entry": _string(_entry_id(candidate)), "query": _string(candidate.get("headword"), 64),
                           "limit": MAX_EXAMPLES_PER_ENTRY})
        return self._request("/sentences?" + query)

    def recordings(self, candidate):
        """Refresh a pinned JMdict entry; only native-approved word M4As are eligible."""
        from .authoring import identity_text
        from .authoring_media import Recording, RECORDING_URL
        if candidate.get("dictionary") != "jmdict":
            return ()
        token = {"lemma": _string(candidate.get("headword")),
                 "surface": _string(candidate.get("headword")),
                 "reading": _string(candidate.get("reading"))}
        identified = self._request("/words/identify", {
            "tokens": [token], "include": ["pitch"], "dictionaries": ["jmdict"],
        })
        candidates = identified.get("words", [{}])[0].get("candidates", []) if isinstance(identified, dict) else []
        detail = next((row for row in candidates if row.get("dictionary") == "jmdict"
                       and row.get("sourceId") == candidate["sourceId"]), None)
        if (not isinstance(detail, dict)
                or identity_text(_string(detail.get("reading"), 256), reading=True)
                != identity_text(_string(candidate.get("reading"), 256), reading=True)):
            raise SnapshotError("Recording response does not match the prepared entry", code="audio_unavailable")
        pitch = detail.get("pitch")
        if not isinstance(pitch, list) or len(pitch) > 32:
            raise SnapshotError("Invalid recording metadata", code="audio_unavailable")
        result = []
        for row in pitch:
            if not isinstance(row, dict):
                raise SnapshotError("Invalid recording metadata", code="audio_unavailable")
            url = row.get("audioUrl")
            if isinstance(url, str) and len(url) < 256 and RECORDING_URL.fullmatch(url):
                download_url = row.get("audioDownloadUrl")
                if download_url != url[:-4] + ".mp3":
                    download_url = None
                result.append(Recording(url, identity_text(detail["reading"], reading=True), row.get("downstep"),
                                        row.get("pattern", ""), download_url))
        return tuple(result)
