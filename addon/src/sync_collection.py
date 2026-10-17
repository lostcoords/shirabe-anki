from __future__ import annotations

import copy
import json
from pathlib import Path
import uuid

from .sync_reviews import verify_platform
from .sync_snapshot import SyncSnapshot, SnapshotConflict, source_digest
from .sync_state import SyncState


class SyncCollection:
    def __init__(self, path, client):
        self.path, self.client = path, client

    def _binding(self, state, collection=None):
        snapshot = state.get("initial_snapshot")
        if not snapshot or snapshot["phase"] != "uploaded" or snapshot["api"] != self.client.api:
            raise SnapshotConflict("Finish the initial copy before refreshing collection settings")
        if collection and snapshot["collection_path"] != str(Path(collection.path).resolve()):
            raise SnapshotConflict("The native collection changed")
        return {"profile_id": snapshot["profile"]["id"], "key": snapshot["pair_request"]["key"],
                "api": self.client.api, "collection_path": snapshot["collection_path"]}

    def prepare_publications(self, *, cancelled=lambda: False):
        while True:
            if cancelled():
                raise SnapshotConflict("Sync paused. Resume the saved publication page when ready.")
            with SyncState(self.path) as state:
                binding = self._binding(state)
                pending = state.get("publications")
                if pending is None:
                    pending = {"binding": binding, "request": {"key": binding["key"],
                        "request_key": str(uuid.uuid4()), "after": 0, "through": None}}
                    state.put("publications", pending)
                if pending["binding"] != binding:
                    raise SnapshotConflict("The saved publication page belongs to another sync epoch")
            request = pending["request"]
            receipt = self.client.prepare_publications(binding["profile_id"], request)
            if not isinstance(receipt, dict) or receipt.get("request_key") != request["request_key"] or \
                    any(type(receipt.get(field)) is not int for field in ("after", "through")) or \
                    type(receipt.get("complete")) is not bool or \
                    not request["after"] <= receipt["after"] <= receipt["through"] or \
                    request["through"] is not None and receipt["through"] != request["through"] or \
                    not receipt["complete"] and not request["after"] < receipt["after"] < receipt["through"]:
                raise SnapshotConflict("Shirabe returned a different publication page receipt")
            with SyncState(self.path) as state:
                if self._binding(state) != binding or state.get("publications") != pending:
                    raise SnapshotConflict("The saved publication page changed during preparation")
                following = None if receipt["complete"] else {"binding": binding, "request": {"key": binding["key"],
                    "request_key": str(uuid.uuid4()), "after": receipt["after"], "through": receipt["through"]}}
                state.put("publications", following)
            if receipt["complete"]:
                return

    def capture(self, collection):
        verify_platform(collection)
        with SyncState(self.path) as state:
            binding = self._binding(state, collection)
            delivery = state.get("delivery")
            if delivery and delivery.get("phase") != "received":
                raise SnapshotConflict("Finish the pending delivery before capturing native counters")
            pending = state.get("collection_settings")
            if pending:
                if pending["binding"] != binding:
                    raise SnapshotConflict("The saved settings belong to another sync epoch")
                return pending
            metadata = SyncSnapshot.metadata(collection, acknowledged_cursor=state.get("delivery_cursor", 0))
            stable = copy.deepcopy(metadata)
            stable["capture"].pop("captured_at")
            digest = source_digest(stable)
            if digest == state.get("collection_settings_digest"):
                return None
            request = {"key": binding["key"], "request_key": str(uuid.uuid4()),
                       "base_revision": state.get("collection_revision", 0), "source_metadata": metadata}
            if len(json.dumps(request).encode()) > SyncSnapshot.MAX_BYTES:
                raise SnapshotConflict("Collection settings exceed the sync byte limit")
            pending = {"binding": binding, "digest": digest, "request": request}
            state.put("collection_settings", pending)
            return pending

    def upload(self):
        with SyncState(self.path) as state:
            pending = state.get("collection_settings")
            if not pending:
                return
            if pending["binding"] != self._binding(state):
                raise SnapshotConflict("The saved settings belong to another sync epoch")
            request = pending["request"]
            receipt = self.client.collection_settings(pending["binding"]["profile_id"], request)
            if not isinstance(receipt, dict) or receipt.get("request_key") != request["request_key"] or type(receipt.get("revision")) is not int or receipt["revision"] != request["base_revision"] + 1:
                raise SnapshotConflict("Shirabe returned a different collection settings receipt")
            with state.db:
                state._put("collection_revision", receipt["revision"])
                state._put("collection_settings_digest", pending["digest"])
                state._put("collection_settings", None)
