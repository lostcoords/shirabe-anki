from __future__ import annotations

from dataclasses import dataclass
import json
import time

MAX_MESSAGE_BYTES = 64 * 1024


class BridgeError(ValueError):
    pass


@dataclass(frozen=True)
class Proposal:
    action: str
    card_id: int
    selection: dict

    @classmethod
    def parse(cls, raw: str) -> Proposal:
        try:
            if len(raw) > MAX_MESSAGE_BYTES or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise BridgeError("This selection is too large. Choose fewer meanings or examples.")
            data = json.loads(raw)
        except BridgeError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise BridgeError("Invalid note request.") from None
        if not isinstance(data, dict) or set(data) != {"action", "cardId", "selection"}:
            raise BridgeError("Invalid note request.")
        if data["action"] not in ("add", "update", "find"):
            raise BridgeError("Unsupported note action.")
        if type(data["cardId"]) is not int or data["cardId"] <= 0:
            raise BridgeError("This card is no longer available.")
        if not isinstance(data["selection"], dict):
            raise BridgeError("Choose a word first.")
        return cls(data["action"], data["cardId"], data["selection"])


class ProposalGate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last = float("-inf")
        self.open = False

    def enter(self):
        now = self.clock()
        if self.open or now - self.last < 1:
            raise BridgeError("Finish the open Anki action before starting another.")
        self.last = now
        self.open = True

    def leave(self):
        self.open = False


def session_slots(raw: str) -> str:
    if len(raw) > 16 * 1024:
        raise BridgeError("Sign-in data exceeds its limit.")
    try:
        slots = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise BridgeError("Invalid sign-in data.") from None
    if not isinstance(slots, dict) or set(slots) - {"shirabe.link", "shirabe.pending"}:
        raise BridgeError("Invalid sign-in data.")
    if any(not isinstance(value, str) for value in slots.values()):
        raise BridgeError("Invalid sign-in data.")
    return json.dumps(slots)


def report_details(raw: str) -> dict:
    if len(raw) > 1024:
        return {}
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    safe = {}
    if data.get("kind") in ("word", "kanji", "text"):
        safe["kind"] = data["kind"]
    if type(data.get("senses")) is int and 0 <= data["senses"] <= 10_000:
        safe["senses"] = data["senses"]
    if data.get("reason") in ("threw", "pro_required", "refused"):
        safe["reason"] = data["reason"]
    return safe
