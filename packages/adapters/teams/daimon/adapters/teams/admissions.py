"""Each Teams message admitted once, and one a restart caught replayed after it.

Every inbound message claims a `teams_message_admissions` row before any work
(the in-memory seen-set is only a pre-filter), so a duplicate delivery, a
redelivery after a restart, or a replay racing either runs at most once. A
message that could start a turn keeps its parsed content on the row until it
is handled: before its turn starts or it is otherwise answered. When the
process exits first (a drain, a crash), the next boot reclaims the row and
feeds it through the ordinary intake again; one older than `REPLAY_MAX_AGE`
gets a "send it again" notice instead. Notices are recorded before they are
sent, so none goes out twice; a failed send is lost, never repeated.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.stores.domain import TeamsMessageAdmissionRow
from daimon.core.stores.teams_message_admissions import (
    claim_teams_message,
    finish_teams_messages,
    mark_restart_notice,
    prune_teams_messages,
    reclaim_teams_messages,
)
from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# Past this a replayed answer would surprise more than help: the restart went wrong.
REPLAY_MAX_AGE = timedelta(minutes=10)
# Receipts outlive any Bot Framework redelivery by far; older ones are pruned.
ADMISSION_HORIZON = timedelta(hours=24)
# How often a running process looks for rows a predecessor left once it exited.
RECLAIM_INTERVAL_S = 30.0
HOLDING = "I'm restarting for an update. I'll pick this up as soon as I'm back."
NOT_HELD = "I restarted for an update and couldn't get to this in time. Please send it again."

_INBOUND = TypeAdapter(TeamsInbound)


def admission_channel(conversation_id: str) -> str:
    """The channel or chat a message is keyed under: a thread id without `;messageid=`.

    Graph names a channel message by this channel id and the same message id
    as the Bot Framework activity, so a Graph-delivered copy claims the same row.
    """
    return conversation_id.split(";", 1)[0]


def _sent_at(inbound: TeamsInbound, now: datetime) -> datetime:
    if inbound.timestamp is None:
        return now
    sent = datetime.fromisoformat(inbound.timestamp)
    return sent if sent.tzinfo is not None else sent.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Reclaimed:
    """What a dead process left: to replay (oldest sent first), and to tell to resend."""

    replay: list[TeamsInbound] = field(default_factory=list[TeamsInbound])
    stale: list[TeamsInbound] = field(default_factory=list[TeamsInbound])


@dataclass(frozen=True)
class TeamsAdmissions:
    """The admission gate for one process: its tenant and its owner key."""

    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    owner_key: int

    async def claim(self, inbound: TeamsInbound, *, now: datetime) -> bool:
        """Admit `inbound` once; one that may start a turn keeps its content for a replay."""
        message: dict[str, JsonValue] | None = (
            None if inbound.unprompted else _INBOUND.dump_python(inbound, mode="json")
        )
        async with self.sessionmaker.begin() as session:
            return await claim_teams_message(
                session,
                tenant_id=self.tenant_id,
                channel_id=admission_channel(inbound.conversation_id),
                message_id=inbound.activity_id,
                conversation_id=inbound.conversation_id,
                owner_key=self.owner_key,
                sent_at=_sent_at(inbound, now),
                user_id=inbound.user_id,
                message=message,
            )

    async def claim_receipt(self, conversation_id: str, message_id: str, *, now: datetime) -> bool:
        """Admit a message answered at once (a refusal): handled from the start."""
        async with self.sessionmaker.begin() as session:
            return await claim_teams_message(
                session,
                tenant_id=self.tenant_id,
                channel_id=admission_channel(conversation_id),
                message_id=message_id,
                conversation_id=conversation_id,
                owner_key=self.owner_key,
                sent_at=now,
            )

    async def finish(self, inbound: TeamsInbound) -> bool:
        """The fence before a turn or an answer: False means another process has it."""
        async with self.sessionmaker.begin() as session:
            return await finish_teams_messages(
                session,
                tenant_id=self.tenant_id,
                channel_id=admission_channel(inbound.conversation_id),
                message_ids=inbound.message_ids,
                owner_key=self.owner_key,
            )

    async def mark_notice(self, items: Sequence[TeamsInbound]) -> bool:
        """Record the restart notice for one chat's messages; True if it is still owed."""
        async with self.sessionmaker.begin() as session:
            return await mark_restart_notice(
                session,
                tenant_id=self.tenant_id,
                channel_id=admission_channel(items[0].conversation_id),
                message_ids=[i for item in items for i in item.message_ids],
                owner_key=self.owner_key,
            )

    async def reclaim(self, *, now: datetime) -> Reclaimed:
        """Take what dead processes left; stale rows are already handled when returned."""
        async with self.sessionmaker.begin() as session:
            rows = await reclaim_teams_messages(
                session,
                tenant_id=self.tenant_id,
                owner_key=self.owner_key,
                replay_after=now - REPLAY_MAX_AGE,
                horizon=now - ADMISSION_HORIZON,
            )
        replay: list[TeamsInbound] = []
        for row in rows.replay:
            inbound = self._decode(row)
            if inbound is None:
                await self._drop(row)  # unreadable: never retried
            else:
                replay.append(inbound)
        stale = [inbound for row in rows.stale if (inbound := self._decode(row)) is not None]
        return Reclaimed(replay=replay, stale=stale)

    async def prune(self, *, now: datetime) -> int:
        async with self.sessionmaker.begin() as session:
            return await prune_teams_messages(
                session,
                tenant_id=self.tenant_id,
                before=now - ADMISSION_HORIZON,
                owner_key=self.owner_key,
            )

    def _decode(self, row: TeamsMessageAdmissionRow) -> TeamsInbound | None:
        try:
            return _INBOUND.validate_python(row.message)
        except ValidationError:
            # Kept by a version whose message shape this one cannot read.
            log.warning("teams.admission.unreadable", message_id=row.message_id)
            return None

    async def _drop(self, row: TeamsMessageAdmissionRow) -> None:
        async with self.sessionmaker.begin() as session:
            await finish_teams_messages(
                session,
                tenant_id=self.tenant_id,
                channel_id=row.channel_id,
                message_ids=[row.message_id],
                owner_key=self.owner_key,
            )
