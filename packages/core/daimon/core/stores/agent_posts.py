"""Async store for agent_posted_messages: what each agent posted.

Rows come from the channel tools (`source='tool'`) and from the chat
adapters for what a turn posted (`source='turn'`, `'auto_thread'`).

The channel tidy tools read this to decide whether a message is the calling
agent's own. Rows hold ids and a keyed HMAC of the text, never the text.
Rows older than the audit retention are removed with `prune_posts`.

No try/except anywhere in this module: exceptions propagate to the adapter
boundary. Callers own the transaction; every write ends with
`await session.flush()`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, cast

from daimon.core._models import AgentPostedMessage
from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

PostKind = Literal["message", "thread"]
PostSource = Literal["tool", "turn", "auto_thread"]


class AgentPostRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform: str
    channel_id: str
    message_id: str
    parent_channel_id: str | None
    thread_ts: str | None
    kind: PostKind
    agent_id: uuid.UUID
    content_hmac: str | None
    source: PostSource
    requester_platform_user_id: str | None
    turn_card_intent_id: uuid.UUID | None
    posted_at: datetime
    deleted_at: datetime | None


async def record_post(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    message_id: str,
    agent_id: uuid.UUID,
    kind: PostKind = "message",
    parent_channel_id: str | None = None,
    thread_ts: str | None = None,
    content_hmac: str | None = None,
    source: PostSource = "tool",
    requester_platform_user_id: str | None = None,
    turn_card_intent_id: uuid.UUID | None = None,
) -> None:
    """Record one post. A second record of the same target keeps the first owner."""
    await session.execute(
        pg_insert(AgentPostedMessage)
        .values(
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            message_id=message_id,
            parent_channel_id=parent_channel_id,
            thread_ts=thread_ts,
            kind=kind,
            agent_id=agent_id,
            content_hmac=content_hmac,
            source=source,
            requester_platform_user_id=requester_platform_user_id,
            turn_card_intent_id=turn_card_intent_id,
        )
        .on_conflict_do_nothing(constraint="uq_agent_posted_messages_target")
    )
    await session.flush()


async def get_post(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    message_id: str,
    for_update: bool = False,
) -> AgentPostRow | None:
    """The live (not deleted) post at this target, whoever posted it."""
    stmt = select(AgentPostedMessage).where(
        AgentPostedMessage.tenant_id == tenant_id,
        AgentPostedMessage.platform == platform,
        AgentPostedMessage.channel_id == channel_id,
        AgentPostedMessage.message_id == message_id,
        AgentPostedMessage.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = await session.scalar(stmt)
    return AgentPostRow.model_validate(row) if row is not None else None


async def list_posts_in(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    message_ids: list[str],
) -> list[AgentPostRow]:
    """The live posts among `message_ids` in one channel."""
    if not message_ids:
        return []
    rows = await session.scalars(
        select(AgentPostedMessage).where(
            AgentPostedMessage.tenant_id == tenant_id,
            AgentPostedMessage.platform == platform,
            AgentPostedMessage.channel_id == channel_id,
            AgentPostedMessage.message_id.in_(message_ids),
            AgentPostedMessage.deleted_at.is_(None),
        )
    )
    return [AgentPostRow.model_validate(row) for row in rows.all()]


async def set_post_hash(
    session: AsyncSession, *, post_id: uuid.UUID, content_hmac: str | None
) -> None:
    """After an edit: the hash of the text the message now carries."""
    await session.execute(
        update(AgentPostedMessage)
        .where(AgentPostedMessage.id == post_id)
        .values(content_hmac=content_hmac)
    )
    await session.flush()


async def mark_deleted(session: AsyncSession, *, post_ids: list[uuid.UUID], now: datetime) -> None:
    """Keep the row for the record; a deleted post is no longer anyone's to act on."""
    if not post_ids:
        return
    await session.execute(
        update(AgentPostedMessage).where(AgentPostedMessage.id.in_(post_ids)).values(deleted_at=now)
    )
    await session.flush()


def _requester_rows(tenant_id: uuid.UUID, platform: str, platform_user_id: str) -> Any:  # noqa: ANN401
    return (
        AgentPostedMessage.tenant_id == tenant_id,
        AgentPostedMessage.platform == platform,
        AgentPostedMessage.requester_platform_user_id == platform_user_id,
    )


async def count_requester(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    """How many recorded posts name this person as who asked (erasure preview)."""
    count = await session.scalar(
        select(func.count())
        .select_from(AgentPostedMessage)
        .where(*_requester_rows(tenant_id, platform, platform_user_id))
    )
    return count or 0


async def clear_requester(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    """Erasure: drop a person's id from the posts recorded for their turns and threads.

    The posts stay owned by their agent; with no requester, only a server admin
    (or the opener of the thread they are in) can have them tidied.
    """
    result = await session.execute(
        update(AgentPostedMessage)
        .where(*_requester_rows(tenant_id, platform, platform_user_id))
        .values(requester_platform_user_id=None)
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def prune_posts(session: AsyncSession, *, tenant_id: uuid.UUID, older_than: datetime) -> int:
    """Remove a tenant's post records older than the cutoff (`daimon audit prune`).

    A pruned post can no longer be tidied with the tools.
    """
    if older_than.utcoffset() is None:
        raise ValueError("older_than must include a timezone")
    result = await session.execute(
        delete(AgentPostedMessage).where(
            AgentPostedMessage.tenant_id == tenant_id,
            AgentPostedMessage.posted_at < older_than,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount
