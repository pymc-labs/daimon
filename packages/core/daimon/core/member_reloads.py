"""Member list changes that events make while a full reload of the same workspace runs.

A reload reads the platform's whole list, then writes it. A leave, deactivation or lost
admin role that commits in between would be undone by the older read, and the person
would keep web access. The chat adapters note each event's change here while a reload of
its workspace runs; the reload lays the notes over its read before writing, and writes
them once more after its own commit, for an event that landed during the write.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

#: A member's state after an event: listed with this admin flag, or None for gone.
MemberChange = bool | None


class ReloadChanges:
    """Per process: the changes noted for each workspace with a reload running."""

    def __init__(self) -> None:
        # Workspace to (reloads running, user id to latest change).
        self._running: dict[uuid.UUID, tuple[int, dict[str, MemberChange]]] = {}

    def start(self, tenant_id: uuid.UUID) -> None:
        """A reload of the workspace begins; call `finish` once it ends, however it ends."""
        count, changes = self._running.get(tenant_id, (0, {}))
        self._running[tenant_id] = (count + 1, changes)

    def note(self, tenant_id: uuid.UUID, user_id: str, change: MemberChange) -> None:
        """An event wrote `change`; kept only while a reload of the workspace runs."""
        running = self._running.get(tenant_id)
        if running is not None:
            running[1][user_id] = change

    def seen(self, tenant_id: uuid.UUID) -> dict[str, MemberChange]:
        """What events changed since the reload began, latest per user."""
        running = self._running.get(tenant_id)
        return {} if running is None else dict(running[1])

    def finish(self, tenant_id: uuid.UUID) -> dict[str, MemberChange]:
        """The reload ended: what events changed while it ran, to write once more."""
        count, changes = self._running.pop(tenant_id, (1, {}))
        if count > 1:
            self._running[tenant_id] = (count - 1, changes)
        return dict(changes)


def overlay(members: Mapping[str, bool], changes: Mapping[str, MemberChange]) -> dict[str, bool]:
    """The platform's list read with the changes events made since laid over it."""
    merged = dict(members)
    for user_id, change in changes.items():
        if change is None:
            merged.pop(user_id, None)
        else:
            merged[user_id] = change
    return merged
