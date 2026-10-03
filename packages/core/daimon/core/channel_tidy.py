"""Volume limits and audit rows for the channel tidy tools.

An agent may edit or delete only messages and threads it posted itself
(`daimon.core.stores.agent_posts`). Every edit, delete or archive writes one
`security_audit_events` row with the target's ids, a keyed HMAC of the text
it replaced and the turn it ran in, never the text. The row is written and
committed before the platform call, so no change happens unaudited; a
platform failure afterwards adds an `error` row.

Limits count those `allowed` rows per agent: `PER_TURN_LIMIT` in one turn
(counted over the last `TURN_WINDOW`) and `PER_HOUR_LIMIT` in any rolling
hour. Refusals are capped too: after `DENIED_PER_HOUR_LIMIT` `denied` rows in
an hour the agent's tidy calls are refused outright. A transaction-scoped advisory lock
on (tenant, agent) makes the count and the insert atomic, so parallel calls
cannot both slip under a limit.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from daimon.core.stores.security_audit import append_event, count_tidy_events
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PER_TURN_LIMIT = 10
PER_HOUR_LIMIT = 40
DENIED_PER_HOUR_LIMIT = 20
WINDOW = timedelta(hours=1)
# A turn runs at most a few hours; older rows never count toward it.
TURN_WINDOW = timedelta(hours=24)

TidyOperation = Literal["message.edit", "message.delete", "thread.archive", "thread.delete"]

_LOCK_NAMESPACE = "daimon:channel_tidy:"


class TidyLimitReached(Exception):
    """The agent has used its tidy budget for this turn or this hour."""

    def __init__(self, scope: Literal["turn", "hour", "denied"]) -> None:
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
    content_hmac: str | None = None


_HMAC_LABEL = b"daimon:channel-tidy:content-hmac:v1"


def derive_content_key(secret: str) -> bytes:
    """The content-HMAC key, derived from a server secret under its own label."""
    return hmac.new(secret.encode("utf-8"), _HMAC_LABEL, hashlib.sha256).digest()


def content_hash(content: str, key: bytes) -> str:
    """Keyed HMAC-SHA256 of a message's text: comparable, not guessable without the key."""
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).hexdigest()


async def lock_tidy_agent(session: AsyncSession, actor: TidyActor) -> None:
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
    locked: bool = False,
) -> None:
    """Check both limits and write one `allowed` audit row per target.

    Raises `TidyLimitReached` and writes nothing when the targets would take
    the agent over either limit. The caller commits before acting. With
    ``locked=True``, the caller must already hold ``lock_tidy_agent`` in a
    transaction kept open until this audit transaction and the effect finish.
    """
    if not locked:
        await lock_tidy_agent(session, actor)
    in_turn = await count_tidy_events(
        session,
        tenant_id=actor.tenant_id,
        agent_id=actor.agent_id,
        outcome="allowed",
        since=now - TURN_WINDOW,
        turn_ref=actor.turn_ref,
    )
    if in_turn + len(targets) > PER_TURN_LIMIT:
        raise TidyLimitReached("turn")
    in_hour = await count_tidy_events(
        session,
        tenant_id=actor.tenant_id,
        agent_id=actor.agent_id,
        outcome="allowed",
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


async def require_refusals_under_cap(
    session: AsyncSession, *, actor: TidyActor, now: datetime
) -> None:
    """Raise `TidyLimitReached("denied")` once an agent has been refused too often.

    Bounds probing: a caller cannot try message after message that is not its own.
    """
    denied = await count_tidy_events(
        session,
        tenant_id=actor.tenant_id,
        agent_id=actor.agent_id,
        outcome="denied",
        since=now - WINDOW,
    )
    if denied >= DENIED_PER_HOUR_LIMIT:
        raise TidyLimitReached("denied")


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
        content_hmac=target.content_hmac,
        turn_ref=actor.turn_ref,
    )
    if row is None:
        # append_event drops rows for a tenant that no longer exists. A tidy
        # action must never run without its row, so stop here.
        raise RuntimeError("security audit row was not written: tenant not found")
