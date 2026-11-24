"""One post a fire queued, as every platform's `Poster` takes it: a `DeliveryJob`.

A job is the fire's `Subject` (who it is for, as whom, where) plus what the outbox holds: the
text and file refs, the failure-notice note, the hold the fire ran under, whether the post
opens with its header, and the fence each send re-checks. A routine's outbox row becomes a
job in `routine_job`; any other outbox builds its own the same way, and the poster serves both.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from daimon.core.config import Settings
from daimon.core.delivery import DeliveryOutcome, RoutinePoster
from daimon.core.fire.subject import RoutineFacts, Subject, routine_subject
from daimon.core.routine_header import routine_header_shown
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import get_routine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "INTERRUPTED",
    "DeliveryFacts",
    "DeliveryJob",
    "Poster",
    "StillCurrent",
    "claim_still_current",
    "routine_job",
    "routine_poster",
]

INTERRUPTED: Final[str] = "Delivery was interrupted; it will not be retried."


class DeliveryFacts(RoutineFacts, Protocol):
    """What the delivery helpers read; a `RoutineRow` and a `DeliveryJob` both have them."""

    @property
    def delivery_payload(self) -> str | None: ...
    @property
    def delivery_note(self) -> str | None: ...
    @property
    def delivery_hold(self) -> dict[str, Any] | None: ...
    @property
    def last_fired_at(self) -> datetime | None: ...


#: Asked before each send: None while the job may still go out, else the skip note.
StillCurrent = Callable[[], Awaitable[str | None]]


@dataclass(frozen=True, kw_only=True)
class DeliveryJob(Subject):
    """A queued post. `delivery_payload` is the text and file refs (`routine_files`),
    `delivery_note` marks a failure notice or a files envelope, `delivery_hold` is the stored
    hold, `header` whether the post opens with its "Result from" line. `delivery_id` keys the
    recorded posts as one reply; None gives each post its own."""

    header: bool
    still_current: StillCurrent = field(compare=False, repr=False)
    delivery_payload: str | None = None
    delivery_note: str | None = None
    delivery_hold: dict[str, Any] | None = None
    last_fired_at: datetime | None = None
    delivery_id: uuid.UUID | None = None


#: A platform's poster: post `job` (or refuse) and say what happened.
Poster = Callable[[DeliveryJob], Awaitable[DeliveryOutcome]]


def claim_still_current(current: RoutineRow, row: RoutineRow, now: datetime) -> bool:
    """Whether the delivery lease `row` was claimed with is still held and unexpired.

    `current` is the routine as read now. A row that was not claimed has no
    lease to lose.
    """
    return row.delivery_status != "claimed" or (
        current.delivery_status == "claimed"
        and current.delivery_lease_owner == row.delivery_lease_owner
        and current.delivery_lease_expires_at is not None
        and current.delivery_lease_expires_at > now
    )


def _routine_fence(sm: async_sessionmaker[AsyncSession], row: RoutineRow) -> StillCurrent:
    """The routine as read now still has the destination, private revision and lease `row` had."""

    async def still_current() -> str | None:
        async with sm() as session:
            current = await get_routine(session, row.id, tenant_id=row.tenant_id)
        if current is None or (current.destination_kind, current.destination_id) != (
            row.destination_kind,
            row.destination_id,
        ):
            return "destination_changed"
        private = row.destination_kind == "dm"
        if private and (current.agent_id, current.agent_name, current.updated_at) != (
            row.agent_id,
            row.agent_name,
            row.updated_at,
        ):
            return "private_revision_changed"
        if not claim_still_current(current, row, datetime.now(UTC)):
            return INTERRUPTED
        if private and (current.destination_kind, current.destination_id) != (
            "dm",
            row.created_by_user_id,
        ):
            return "destination_changed"
        return None

    return still_current


async def routine_job(
    sm: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    *,
    platform: str,
    settings: Settings | None = None,
) -> DeliveryJob:
    """The job for a routine's claimed outbox row, fenced on the row as claimed."""
    return DeliveryJob(
        **vars(routine_subject(row)),
        header=await routine_header_shown(sm, row, platform, settings),
        still_current=_routine_fence(sm, row),
        delivery_payload=row.delivery_payload,
        delivery_note=row.delivery_note,
        delivery_hold=row.delivery_hold,
        last_fired_at=row.last_fired_at,
    )


def routine_poster(
    sm: async_sessionmaker[AsyncSession],
    post: Poster,
    *,
    platform: str,
    settings: Settings | None = None,
) -> RoutinePoster:
    """The routine outbox's poster: each claimed row posted through `post` as its job."""

    async def post_row(row: RoutineRow) -> DeliveryOutcome:
        return await post(await routine_job(sm, row, platform=platform, settings=settings))

    return post_row
