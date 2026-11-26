"""Durable, owner-bound DM routing and bounded conversation history."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from daimon.core._models import (
    DirectMessageConversation,
    DirectMessagePolicy,
    Tenant,
    ThreadAgentBinding,
)
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.session_fence_retry import FenceUnavailable, try_fence
from daimon.core.stores.thread_sessions import retire_dm_scope
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select, text, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

COLD_DM_ORIGIN = "Private conversation"
#: A Private answer from a contained room (`audience_hold`): a cold conversation whose
#: "no source" is a recorded fact, so the agent's budget channel never stands in for one.
PRIVATE_ANSWER_ORIGIN = "Private answer"

DM_SCOPE_PREFIX = "dm:"
"""Thread id prefix of a private conversation's scope."""


def is_dm_thread(platform: str, thread_id: str) -> bool:
    """A private conversation's scope, or a Teams personal chat (`a:…`), which is its own."""
    return thread_id.startswith(DM_SCOPE_PREFIX) or (
        platform == "teams" and thread_id.startswith("a:")
    )


class DirectMessageBusy(DaimonError):
    """A previous message still owns this private conversation."""


def owner_dm_fence(platform: str, owner: str) -> str:
    """The advisory fence each running DM turn of `owner` holds; it frees when the process dies."""
    return f"owner_dm:{platform}:{owner}"


@dataclass(frozen=True, slots=True)
class OwnerDmFence:
    """The owner's fence as `owner_dm_turn` holds it, on connection `pid`.

    `fence_held` checks the hold; one built without a `pid` proves none. `lost` is set
    once the holder sees the fence gone. `db` is the holding session, which the holder
    pings so an idle drop cannot end the hold unseen.
    """

    platform: str
    owner: str
    pid: int = 0
    db: AsyncSession | None = field(default=None, compare=False, repr=False)
    lost: asyncio.Event = field(default_factory=asyncio.Event, compare=False, repr=False)


async def fence_held(session: AsyncSession, fence: OwnerDmFence) -> bool:
    """Whether `fence`'s connection still holds the owner's fence; a dropped one frees it."""
    # The bigint key `try_fence(schema_scoped=True)` locks, split into two 32-bit halves.
    held = await session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND pid = :pid AND objsubid = 1"
            " AND ((classid::bigint << 32) | objid::bigint)"
            " = hashtextextended(current_schema() || ':' || :key, 0))"
        ),
        {"pid": fence.pid, "key": owner_dm_fence(fence.platform, fence.owner)},
    )
    return bool(held)


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
    claimed_at: datetime | None

    @property
    def is_cold(self) -> bool:
        """Source-free provenance, distinct from legacy moves with missing source fields."""
        return (
            self.source_url in (COLD_DM_ORIGIN, PRIVATE_ANSWER_ORIGIN)
            and self.source_channel_id is None
        )

    @property
    def private_answer(self) -> bool:
        """A Private answer's conversation: it has no source by design, on every turn."""
        return self.source_url == PRIVATE_ANSWER_ORIGIN and self.source_channel_id is None


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


async def get_conversation_in_scope(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope_id: str
) -> DirectMessageRow | None:
    """The conversation holding `scope_id` in this tenant, whoever owns it."""
    row = await session.scalar(
        select(DirectMessageConversation).where(
            DirectMessageConversation.tenant_id == tenant_id,
            DirectMessageConversation.scope_id == scope_id,
        )
    )
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


async def start_conversation(session: AsyncSession, *, conversation: DirectMessageRow) -> None:
    """Open `conversation` in place of the route's current one.

    A set claim is live while its owner's fence is held (see `claim_message`), so a held
    fence is busy; one that is free is taken here, until commit, so nothing claims meanwhile.
    """
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
    if row is not None and row.claimed_at is not None:
        try:
            await try_fence(
                session, owner_dm_fence(row.platform, row.external_user_id), schema_scoped=True
            )
        except FenceUnavailable:
            raise DirectMessageBusy(
                "Wait for the current DM reply before starting a new conversation."
            ) from None
    values = {**conversation.model_dump(), "updated_at": func.now()}
    if row is None:
        session.add(DirectMessageConversation(**values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    await session.flush()


async def claim_message(
    session: AsyncSession,
    *,
    fence: OwnerDmFence,
    route_key: str,
    message_ids: Sequence[str],
    expected_scope_id: str,
    claimed_at: datetime,
) -> DirectMessageRow | None:
    """Claim the conversation of `fence`'s owner for the turn that answers `message_ids`.

    A merged turn answers several messages. None means every one was seen before (a
    redelivery); otherwise the unseen ids are recorded and the turn runs.

    `claimed_at` identifies the claim: finish and delivery check it is still the set one.
    Each claim's turn holds the owner's fence until it finishes, and the fence frees when
    its connection drops. So a claim still set here is taken over only while `fence` is
    held, which proves the claim's own turn lost it; that turn then fails its claim checks.
    Without that proof the claim is still live and this one is busy.
    """
    row = await session.get(
        DirectMessageConversation,
        (fence.platform, route_key, fence.owner),
        with_for_update=True,
    )
    if row is None:
        raise UserFacingError(
            "No private conversation is open here.\n\n"
            "Run /dm-me in the channel you want to continue from."
        )
    if row.scope_id != expected_scope_id:
        raise UserFacingError("That private conversation changed. Please send your message again.")
    unseen = [i for i in dict.fromkeys(message_ids) if i not in row.recent_message_ids]
    if not unseen:
        return None
    if row.claimed_at is not None:
        if not await fence_held(session, fence):
            raise DirectMessageBusy(
                "Your previous message is still being answered.\n\n"
                "Send this one again when the reply arrives."
            )
        structlog.get_logger(__name__).warning(
            "dm.claim_taken_over", platform=fence.platform, claimed_at=row.claimed_at.isoformat()
        )
    row.claimed_at = claimed_at
    row.updated_at = func.now()
    row.recent_message_ids = [*row.recent_message_ids, *unseen][-50:]
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
        and row.claimed_at is not None
        and row.claimed_at == conversation.claimed_at
    )


async def finish_message(
    session: AsyncSession,
    *,
    conversation: DirectMessageRow,
    history: list[dict[str, str]] | None,
) -> bool:
    """Release `conversation`'s claim and keep `history`; False if the claim is no longer set."""
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
    if row is None or row.claimed_at is None or row.claimed_at != conversation.claimed_at:
        return False
    row.claimed_at = None
    row.updated_at = func.now()
    if history is not None:
        row.history = history
    await session.flush()
    return True


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


async def dm_turned_off(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether someone set `direct_messages` off for the workspace; the default off is not."""
    from daimon.core.features.snapshot import FeaturePlace, feature_in

    platform = await session.scalar(select(Tenant.platform).where(Tenant.id == tenant_id))
    if platform is None:
        return False
    found = await feature_in(session, None, FeaturePlace(tenant_id, platform), "direct_messages")
    return not found.on and found.tier != "default"


async def get_dm_agent_name(session: AsyncSession, *, tenant_id: uuid.UUID) -> str | None:
    return await session.scalar(
        select(DirectMessagePolicy.dm_agent_name).where(DirectMessagePolicy.tenant_id == tenant_id)
    )


async def set_dm_agent_name(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_name: str | None
) -> None:
    """Store the DM default agent: the one value cold DMs and `daimon dm default` read. Only
    `dm_settings.set_default` calls this outside tests, so a change is checked and audited."""
    if agent_name is not None and not agent_name.strip():
        raise UserFacingError("The DM agent name must not be empty.")
    await session.execute(
        insert(DirectMessagePolicy)
        .values(tenant_id=tenant_id, dm_agent_name=agent_name)
        .on_conflict_do_update(index_elements=["tenant_id"], set_={"dm_agent_name": agent_name})
    )
    await session.flush()


async def dm_enabled(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """The `direct_messages` feature for a workspace, at its own platform's default
    (Teams starts on); off for a workspace that does not exist."""
    # Imported here: the feature modules import the stores.
    from daimon.core.features.snapshot import FeaturePlace, feature_in

    platform = await session.scalar(select(Tenant.platform).where(Tenant.id == tenant_id))
    if platform is None:
        return False
    place = FeaturePlace(tenant_id, platform)
    return (await feature_in(session, None, place, "direct_messages")).on


async def set_dm_enabled(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    enabled: bool,
    account_id: uuid.UUID | None = None,
) -> None:
    """Turn `direct_messages` on or off for a workspace through the features writer, at the
    workspace's own platform and with its change record. For the policy import and fixtures;
    admins go through `workspace_settings.set_dms` or `daimon features set`."""
    # Imported here: the feature service reads these stores.
    from daimon.core.features.service import set_workspace_feature

    platform = await session.scalar(select(Tenant.platform).where(Tenant.id == tenant_id))
    if platform is None:
        raise LookupError(f"no workspace {tenant_id}")
    await set_workspace_feature(
        session,
        None,
        tenant_id=tenant_id,
        platform=platform,
        key="direct_messages",
        value="on" if enabled else "off",
        account_id=account_id,
        surface="cli",
    )


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
