"""Per-item world state: the case's seed resources plus everything the item's writes changed.

A resource is `(service, "<type>:<key>")`. Reads see the item's writes first, then the case's
seed. Writes and new blobs are staged here and persisted by the dispatcher in the same
transaction as the call that made them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from daimon.core.evals.format import BlobRef, JsonObject, Resource, canonical_json
from daimon.core.stores.eval_world import OverlayRow
from pydantic import JsonValue


def resource_key(type_: str, key: str) -> str:
    return f"{type_}:{key}"


class Overlay:
    def __init__(self, seed: Iterable[Resource], rows: Iterable[OverlayRow]) -> None:
        self._seed: dict[tuple[str, str], JsonObject] = {
            (r.service, resource_key(r.type, r.key)): dict(r.data) for r in seed
        }
        self._rows: dict[tuple[str, str], JsonObject | None] = {
            (r.service, r.resource_key): r.data for r in rows
        }
        self.writes: dict[tuple[str, str], JsonObject | None] = {}
        self.blobs: dict[str, tuple[BlobRef, bytes]] = {}

    def _current(self) -> dict[tuple[str, str], JsonObject | None]:
        return self._rows | self.writes

    def get(self, service: str, type_: str, key: str) -> JsonObject | None:
        name = (service, resource_key(type_, key))
        current = self._current()
        return current[name] if name in current else self._seed.get(name)

    def written(self, service: str, type_: str, key: str) -> bool:
        """Whether the item's own writes changed this resource (the seed does not count)."""
        return (service, resource_key(type_, key)) in self._current()

    def put(self, service: str, type_: str, key: str, data: JsonObject | None) -> None:
        """Stage a resource's new state; None deletes it."""
        self.writes[(service, resource_key(type_, key))] = data

    def list(self, service: str, type_: str) -> list[tuple[str, JsonObject]]:
        """Every live resource of one type, by key, seed and writes merged."""
        prefix = resource_key(type_, "")
        merged: dict[tuple[str, str], JsonObject | None] = {**self._seed, **self._current()}
        return sorted(
            (name[len(prefix) :], data)
            for (svc, name), data in merged.items()
            if svc == service and name.startswith(prefix) and data is not None
        )

    def put_blob(self, content: bytes, media_type: str, name: str | None = None) -> BlobRef:
        sha = hashlib.sha256(content).hexdigest()
        ref = BlobRef(sha256=sha, size=len(content), media_type=media_type, name=name)
        self.blobs[sha] = (ref, content)
        return ref

    def changes(self) -> JsonObject:
        """What this item changed so far, for the simulator's prompt."""
        return {
            f"{svc}/{name}": data
            for (svc, name), data in sorted(self._current().items(), key=lambda kv: kv[0])
        }

    def digest(self) -> str:
        """sha256 of the item's changes: equal state, equal digest, so cached answers fit."""
        state: JsonValue = self.changes()
        return hashlib.sha256(canonical_json(state)).hexdigest()
