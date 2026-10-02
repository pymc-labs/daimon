"""Volume limits and audit rows for the channel tidy tools.

An agent may edit or delete only messages and threads it posted itself
(`daimon.core.stores.agent_posts`). Every edit, delete or archive writes one
`security_audit_events` row with the target's ids, a SHA-256 of the text it
replaced and the turn it ran in, never the text. The row is written and
committed before the platform call, so no change happens unaudited; a
platform failure afterwards adds an `error` row.

Limits count those `allowed` rows per agent: `PER_TURN_LIMIT` in one turn
and `PER_HOUR_LIMIT` in any rolling hour. A transaction-scoped advisory lock
on (tenant, agent) makes the count and the insert atomic, so parallel calls
cannot both slip under a limit.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from daimon.core.stores.security_audit import append_event, count_allowed
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PER_TURN_LIMIT = 10
PER_HOUR_LIMIT = 40
WINDOW = timedelta(hours=1)

TIDY_TOOL_NAMES: frozenset[str] = frozenset(
    {"edit_message", "delete_message", "archive_thread", "delete_thread"}
)

TidyOperation = Literal["message.edit", "message.delete", "thread.archive", "thread.delete"]

_LOCK_NAMESPACE = "daimon:channel_tidy:"


class TidyLimitReached(Exception):
    """The agent has used its tidy budget for this turn or this hour."""

    def __init__(self, scope: Literal["turn", "hour"]) -> None:
        super().__init__(scope)
        self.scope = scope


@dataclass(frozen=True)
class TidyActor:
    """Who is acting, as the audit row records it."""

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID | None
    platform: str
    platform_user_id: str | None
    turn_ref: str


@dataclass(frozen=True)
class TidyTarget:
    channel_id: str
    message_id: str
    content_sha256: str | None = None


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _lock_agent(session: AsyncSession, actor: TidyActor) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{_LOCK_NAMESPACE}{actor.tenant_id}:{actor.agent_id}"},
    )


async def record_tidy_actions(
    session: AsyncSession,
    *,
    actor: TidyActor,
    tool_name: str,
    operation: TidyOperation,
    targets: list[TidyTarget],
    now: datetime,
) -> None:
    """Check both limits and write one `allowed` audit row per target.

    Raises `TidyLimitReached` and writes nothing when the targets would take
    the agent over either limit. The caller commits before acting.
    """
    await _lock_agent(session, actor)
    in_turn = await count_allowed(
        session,
        tenant_id=actor.tenant_id,
        agent_id=actor.agent_id,
        tool_names=TIDY_TOOL_NAMES,
        turn_ref=actor.turn_ref,
    )
    if in_turn + len(targets) > PER_TURN_LIMIT:
        raise TidyLimitReached("turn")
    in_hour = await count_allowed(
        session,
        tenant_id=actor.tenant_id,
        agent_id=actor.agent_id,
        tool_names=TIDY_TOOL_NAMES,
        since=now - WINDOW,
    )
    if in_hour + len(targets) > PER_HOUR_LIMIT:
        raise TidyLimitReached("hour")
    for target in targets:
        await _append(
            session,
            actor=actor,
            tool_name=tool_name,
            operation=operation,
            outcome="allowed",
            reason="own_message",
            target=target,
            now=now,
        )


async def record_tidy_outcome(
    session: AsyncSession,
    *,
    actor: TidyActor,
    tool_name: str,
    operation: TidyOperation,
    outcome: Literal["denied", "error"],
    reason: str,
    target: TidyTarget,
    now: datetime,
) -> None:
    """A refused or failed tidy action, with the same ids and no text."""
    await _append(
        session,
        actor=actor,
        tool_name=tool_name,
        operation=operation,
        outcome=outcome,
        reason=reason,
        target=target,
        now=now,
    )


async def _append(
    session: AsyncSession,
    *,
    actor: TidyActor,
    tool_name: str,
    operation: TidyOperation,
    outcome: Literal["allowed", "denied", "error"],
    reason: str,
    target: TidyTarget,
    now: datetime,
) -> None:
    row = await append_event(
        session,
        tenant_id=actor.tenant_id,
        account_id=actor.account_id,
        agent_id=actor.agent_id,
        platform=actor.platform,
        platform_user_id=actor.platform_user_id,
        tool_name=tool_name,
        operation=operation,
        outcome=outcome,
        reason=reason,
        occurred_at=now,
        target_channel_id=target.channel_id,
        target_message_id=target.message_id,
        content_sha256=target.content_sha256,
        turn_ref=actor.turn_ref,
    )
    if row is None:
        # append_event drops rows for a tenant that no longer exists. A tidy
        # action must never run without its row, so stop here.
        raise RuntimeError("security audit row was not written: tenant not found")
