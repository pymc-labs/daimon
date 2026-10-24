"""Durable, owner-bound DM routing and bounded conversation history."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import (
    DirectMessageConversation,
    DirectMessagePolicy,
    ThreadAgentBinding,
)
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.stores.thread_sessions import retire_dm_scope
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select, text, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

COLD_DM_ORIGIN = "Private conversation"

DM_SCOPE_PREFIX = "dm:"
"""Thread id prefix of a private conversation's scope."""


class DirectMessageBusy(DaimonError):
    """A previous message still owns this private conversation."""


class DirectMessageRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    platform: str
    route_key: str
    external_user_id: str
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    workspace_id: str
    channel_id: str
    scope_id: str
    source_url: str
    context: str
    memory_read_only: bool
    source_channel_id: str | None = None
    source_thread_id: str | None = None
    source_thread_keys: list[str] | None = None
    history: list[dict[str, str]]
    recent_message_ids: list[str]
    active_until: datetime | None

    @property
    def is_cold(self) -> bool:
        """Source-free provenance, distinct from legacy moves with missing source fields."""
        return self.source_url == COLD_DM_ORIGIN and self.source_channel_id is None


class DmOrigin(BaseModel):
    """A tenant's live private conversation and the channel it was started from."""

    model_config = ConfigDict(frozen=True)

    channel_id: str
    scope_id: str
    source_channel_id: str | None

    @property
    def origin(self) -> str:
        """The source channel, or the DM channel itself when none was recorded."""
        return self.source_channel_id or self.channel_id


async def list_dm_origins(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[DmOrigin]:
    rows = await session.execute(
        select(
            DirectMessageConversation.channel_id,
            DirectMessageConversation.scope_id,
            DirectMessageConversation.source_channel_id,
        ).where(DirectMessageConversation.tenant_id == tenant_id)
    )
    return [
        DmOrigin(channel_id=channel, scope_id=scope, source_channel_id=source)
        for channel, scope, source in rows.tuples()
    ]


async def get_conversation(
    session: AsyncSession, *, platform: str, route_key: str, external_user_id: str
) -> DirectMessageRow | None:
    row = await session.get(DirectMessageConversation, (platform, route_key, external_user_id))
    return None if row is None else DirectMessageRow.model_validate(row)


async def get_conversation_by_scope(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID, scope_id: str
) -> DirectMessageRow | None:
    """Resolve only the caller's current private scope in this tenant."""
    row = await session.scalar(
        select(DirectMessageConversation).where(
            DirectMessageConversation.tenant_id == tenant_id,
            DirectMessageConversation.account_id == account_id,
            DirectMessageConversation.scope_id == scope_id,
        )
    )
    return None if row is None else DirectMessageRow.model_validate(row)


async def start_conversation(
    session: AsyncSession, *, conversation: DirectMessageRow, now: datetime
) -> None:
    # Lock even an absent row, so concurrent first starts cannot both insert.
    await lock_route(
        session,
        platform=conversation.platform,
        route_key=conversation.route_key,
        external_user_id=conversation.external_user_id,
    )
    row = await session.get(
        DirectMessageConversation,
        (conversation.platform, conversation.route_key, conversation.external_user_id),
        with_for_update=True,
    )
    if row is not None and row.active_until is not None and row.active_until > now:
        raise DirectMessageBusy("Wait for the current DM reply before starting a new conversation.")
    values = conversation.model_dump()
    if row is None:
        session.add(DirectMessageConversation(**values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    await session.flush()


async def claim_message(
    session: AsyncSession,
    *,
    platform: str,
    route_key: str,
    external_user_id: str,
    message_id: str,
    expected_scope_id: str,
    now: datetime,
    active_until: datetime,
) -> DirectMessageRow | None:
    row = await session.get(
        DirectMessageConversation, (platform, route_key, external_user_id), with_for_update=True
    )
    if row is None:
        raise UserFacingError(
            "No private conversation is open here.\n\n"
            "Run /dm-me in the channel you want to continue from."
        )
    if row.scope_id != expected_scope_id:
        raise UserFacingError("That private conversation changed. Please send your message again.")
    if message_id in row.recent_message_ids:
        return None
    if row.active_until is not None and row.active_until > now:
        raise DirectMessageBusy(
            "A reply is still running. Please send your message again after it finishes."
        )
    row.active_until = active_until
    row.recent_message_ids = [*row.recent_message_ids[-49:], message_id]
    await session.flush()
    return DirectMessageRow.model_validate(row)


async def holds_claim(session: AsyncSession, *, conversation: DirectMessageRow) -> bool:
    """Lock the conversation and report whether `conversation`'s claim still holds it.

    The same comparison `finish_message` makes: a later delivery's claim, or
    a cleared one, means this delivery no longer owns the scope.
    """
    row = await session.get(
        DirectMessageConversation,
        (conversation.platform, conversation.route_key, conversation.external_user_id),
        with_for_update=True,
    )
    return (
        row is not None
        and row.scope_id == conversation.scope_id
        and row.active_until is not None
        and row.active_until == conversation.active_until
    )


async def finish_message(
    session: AsyncSession,
    *,
    conversation: DirectMessageRow,
    history: list[dict[str, str]] | None,
) -> None:
    row = (
        await session.execute(
            select(DirectMessageConversation)
            .where(
                DirectMessageConversation.platform == conversation.platform,
                DirectMessageConversation.route_key == conversation.route_key,
                DirectMessageConversation.external_user_id == conversation.external_user_id,
                DirectMessageConversation.scope_id == conversation.scope_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.active_until != conversation.active_until:
        return
    row.active_until = None
    if history is not None:
        row.history = history
    await session.flush()


async def quarantine_conversation(
    session: AsyncSession, *, conversation: DirectMessageRow
) -> list[str]:
    """Remove this exact conversation and retire the provider sessions of its scope.

    Returns the retired MA session ids, for a best-effort upstream archive. A
    later message finds no conversation; a fresh /dm-me starts a new scope.
    """
    await session.execute(
        delete(DirectMessageConversation).where(
            DirectMessageConversation.platform == conversation.platform,
            DirectMessageConversation.route_key == conversation.route_key,
            DirectMessageConversation.external_user_id == conversation.external_user_id,
            DirectMessageConversation.scope_id == conversation.scope_id,
        )
    )
    return await retire_dm_scope(
        session,
        tenant_id=conversation.tenant_id,
        platform=conversation.platform,
        scope_id=conversation.scope_id,
    )


async def lock_route(
    session: AsyncSession, *, platform: str, route_key: str, external_user_id: str
) -> None:
    """Serialize routing writes, including a route that has no conversation yet."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"dm:{platform}:{route_key}:{external_user_id}"},
    )


async def get_dm_agent_name(session: AsyncSession, *, tenant_id: uuid.UUID) -> str | None:
    return await session.scalar(
        select(DirectMessagePolicy.dm_agent_name).where(DirectMessagePolicy.tenant_id == tenant_id)
    )


async def set_dm_agent_name(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str | None
) -> None:
    if agent_name is not None and not agent_name.strip():
        raise UserFacingError("The DM agent name must not be empty.")
    await session.execute(
        insert(DirectMessagePolicy)
        .values(tenant_id=tenant_id, dm_agent_name=agent_name)
        .on_conflict_do_update(index_elements=["tenant_id"], set_={"dm_agent_name": agent_name})
    )
    await session.flush()


async def dm_enabled(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    policy = await session.get(DirectMessagePolicy, tenant_id)
    return policy is not None and policy.enabled


async def set_dm_enabled(session: AsyncSession, *, tenant_id: uuid.UUID, enabled: bool) -> None:
    await session.execute(
        insert(DirectMessagePolicy)
        .values(tenant_id=tenant_id, enabled=enabled)
        .on_conflict_do_update(index_elements=["tenant_id"], set_={"enabled": enabled})
    )
    await session.flush()


async def count_conversations_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(DirectMessageConversation)
            .where(DirectMessageConversation.account_id == account_id)
        )
    ).scalar_one()


async def delete_conversations_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    removed = (
        await session.execute(
            delete(DirectMessageConversation)
            .where(DirectMessageConversation.account_id == account_id)
            .returning(DirectMessageConversation.scope_id)
        )
    ).all()
    return len(removed)


async def get_source_channel(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope_id: str
) -> str | None:
    """The channel a DM scope was started from; None for an unknown scope or an older DM."""
    return (
        await session.execute(
            select(DirectMessageConversation.source_channel_id).where(
                DirectMessageConversation.tenant_id == tenant_id,
                DirectMessageConversation.scope_id == scope_id,
            )
        )
    ).scalar()


async def list_dm_channel_ids(session: AsyncSession, *, tenant_id: uuid.UUID) -> set[str]:
    """Channels this tenant's DM conversations were given, including ones since moved away.

    Starting a DM writes its channel a config row and a ``dm:`` handoff binding.
    The binding outlives the conversation row, so both are read.
    """
    conversations = select(DirectMessageConversation.channel_id).where(
        DirectMessageConversation.tenant_id == tenant_id
    )
    bindings = select(ThreadAgentBinding.parent_channel_id).where(
        ThreadAgentBinding.tenant_id == tenant_id,
        ThreadAgentBinding.thread_id.startswith(DM_SCOPE_PREFIX),
    )
    return set((await session.execute(union(conversations, bindings))).scalars())
