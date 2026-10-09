"""The journal: normalized events in local commit order, and the session
projection derived from them.

Entries are unique on (session, source_key, revision), so a page of saved
items that overlaps buffered stream events adds nothing twice. Previews are
deduplicated in their own namespace: a preview never takes the place of the
authoritative entry with the same source key. An append,
the projection it produces and the stream cursor commit together. Preview
events are kept but never complete a turn or bill.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime

from pydantic import Field

from mux.contracts._base import Contract
from mux.contracts.events import (
    Event,
    RequiresActionPayload,
    StatusRunningPayload,
    TurnEndedPayload,
)
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import ProjectionSnapshot

_AUTHORITATIVE = frozenset({"record", "reconciled"})

type EntryIdentity = tuple[str, int, bool]
"""(source_key, revision, is_preview): what the journal deduplicates on."""


class JournalEntry(Contract):
    """An event to append. `source_key` names it upstream (usually the
    native event id); `revision` orders corrections to the same source.
    The event's `sequence` is ignored: the store assigns it."""

    source_key: str
    revision: int = Field(default=1, ge=1)
    event: Event

    @property
    def identity(self) -> EntryIdentity:
        return (self.source_key, self.revision, self.event.authority == "preview")


class JournalAppend(Contract):
    appended: tuple[Event, ...]
    duplicates: int = Field(ge=0)
    projection: ProjectionSnapshot


def completes_turn(event: Event) -> bool:
    return event.type == "session.turn_ended" and event.authority in _AUTHORITATIVE


def is_billable(event: Event) -> bool:
    return event.type == "usage.observed" and event.authority in _AUTHORITATIVE


def empty_projection(session: ResourceRef, now: datetime) -> ProjectionSnapshot:
    return ProjectionSnapshot(session=session, cursor="", state="idle", taken_at=now)


def plan_append(
    session_id: str,
    seen: AbstractSet[EntryIdentity],
    next_sequence: int,
    entries: Iterable[JournalEntry],
) -> tuple[list[tuple[JournalEntry, Event]], int]:
    """Number the new entries from `next_sequence`; drop the ones already
    journaled (or repeated within the batch). Returns the new entries with
    their sequenced events, and how many were dropped."""
    seen = set(seen)
    planned: list[tuple[JournalEntry, Event]] = []
    duplicates = 0
    for entry in entries:
        if entry.event.session_id != session_id:
            raise ValueError(f"event {entry.event.id} belongs to session {entry.event.session_id}")
        identity = entry.identity
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        sequence = next_sequence + len(planned)
        planned.append((entry, entry.event.model_copy(update={"sequence": sequence})))
    return planned, duplicates


def project(snapshot: ProjectionSnapshot, event: Event) -> ProjectionSnapshot:
    """Fold one event into the projection. Pure, and blind to previews."""
    if event.authority == "preview" or snapshot.state == "terminated":
        return snapshot
    if event.authority == "gap" or event.type == "session.history_gap":
        return snapshot.model_copy(update={"gaps": (*snapshot.gaps, event.id)})
    match event.type:
        case "session.status_running":
            running = StatusRunningPayload.model_validate(event.payload)
            return snapshot.model_copy(
                update={
                    "state": "running",
                    "active_root_turn": running.root_turn_id,
                    "required_actions": (),
                }
            )
        case "session.requires_action":
            payload = RequiresActionPayload.model_validate(event.payload)
            return snapshot.model_copy(
                update={"state": "requires_action", "required_actions": payload.actions}
            )
        case "session.turn_ended" if completes_turn(event):
            payload = TurnEndedPayload.model_validate(event.payload)
            if snapshot.active_root_turn not in (None, payload.root_turn_id):
                return snapshot
            return snapshot.model_copy(
                update={"state": "idle", "active_root_turn": None, "required_actions": ()}
            )
        case "session.status_terminated":
            return snapshot.model_copy(
                update={"state": "terminated", "active_root_turn": None, "required_actions": ()}
            )
        case _:
            return snapshot


def project_all(
    snapshot: ProjectionSnapshot, events: Sequence[Event], *, cursor: str, now: datetime
) -> ProjectionSnapshot:
    for event in events:
        snapshot = project(snapshot, event)
    return snapshot.model_copy(update={"cursor": cursor, "taken_at": now})
