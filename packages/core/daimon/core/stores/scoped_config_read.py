"""Centralized config reads: three-tier resolution and single-scope raw reads."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Literal, cast

from daimon.core._models import (
    Account,
    ChannelConfig,
    Routine,
    TenantConfig,
    ThreadAgentBinding,
    ThreadSession,
    UserConfig,
)
from daimon.core.errors import DaimonError
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    ResolvedConfig,
    ScopeContext,
    ScopeRef,
    TenantConfigRow,
    UserConfigRow,
    UserScopeRef,
    is_agent_reachable,
    merge,
)
from daimon.core.stores.thread_agent_bindings import get_binding
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve(
    session: AsyncSession,
    *,
    context: ScopeContext,
    default: DeploymentDefault,
) -> ResolvedConfig:
    channel_row: ChannelConfigRow | None = None
    if context.channel_id is not None:
        channel_row = await _fetch_channel(
            session,
            tenant_id=context.tenant_id,
            channel_id=context.channel_id,
        )

    tenant_row = await _fetch_tenant(session, tenant_id=context.tenant_id)
    config = merge(channel=channel_row, tenant=tenant_row, default=default)
    if (
        context.platform is not None
        and context.channel_id is not None
        and context.thread_id is not None
    ):
        binding = await get_binding(
            session,
            tenant_id=context.tenant_id,
            platform=context.platform,
            parent_channel_id=context.channel_id,
            thread_id=context.thread_id,
        )
        if binding is not None:
            if binding.deleted:
                raise DaimonError(
                    "This setup conversation was deleted. Open a new setup conversation."
                    if binding.kind == "setup"
                    else "This conversation was deleted. Start the task again in a new thread."
                )
            return config.model_copy(
                update={
                    "agent_name": binding.responder_name,
                    "agent_name_tier": "thread",
                    "responder_ma_agent_id": binding.responder_ma_agent_id,
                    "configuration_target_ma_agent_id": binding.configuration_target_ma_agent_id,
                    "configuration_target_name": binding.configuration_target_name,
                    "thread_binding_id": binding.id,
                    "thread_binding_kind": binding.kind,
                }
            )
    return config


async def get_scope(
    session: AsyncSession, *, scope: ScopeRef
) -> UserConfigRow | ChannelConfigRow | TenantConfigRow | None:
    if isinstance(scope, UserScopeRef):
        return await _fetch_user(session, account_id=scope.account_id)
    if isinstance(scope, ChannelScopeRef):
        return await _fetch_channel(
            session,
            tenant_id=scope.tenant_id,
            channel_id=scope.channel_id,
        )
    # TenantScopeRef is the only remaining variant.
    return await _fetch_tenant(session, tenant_id=scope.tenant_id)


async def list_propagations_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> tuple[TenantConfigRow | None, list[ChannelConfigRow]]:
    """Return the tenant-scope row (if any) and all channel-scope rows for one tenant.

    Maps ORM -> Pydantic at the boundary. Used by the Discord /propagate +
    /unpropagate panels to render the cross-channel cascade. Lives inside the
    store because the ORM models (`ChannelConfig`, `TenantConfig`) are private to
    `daimon.core.stores.**` per the import-linter ORM-privacy contract — adapters
    consume only Pydantic rows.
    """
    tenant_row = await _fetch_tenant(session, tenant_id=tenant_id)

    ch_stmt = (
        select(ChannelConfig)
        .where(ChannelConfig.tenant_id == tenant_id)
        .order_by(ChannelConfig.channel_id.asc())
    )
    ch_orms = (await session.execute(ch_stmt)).scalars().all()
    ch_rows = [
        ChannelConfigRow(
            tenant_id=ch.tenant_id,
            channel_id=ch.channel_id,
            agent_name=ch.agent_name,
            environment_name=ch.environment_name,
            agent_name_set_by_account_id=ch.agent_name_set_by_account_id,
            agent_name_set_at=ch.agent_name_set_at,
            mode=cast(Literal["agent", "user_active"], ch.mode),
        )
        for ch in ch_orms
    ]
    return tenant_row, ch_rows


async def is_agent_reachable_in_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    default: DeploymentDefault,
) -> bool:
    """Shell half of the reachability rule: answer for one tenant from the DB.

    Reads the tenant's config rows via `list_propagations_for_tenant` and feeds
    them straight into the pure `is_agent_reachable` predicate. Callers that
    already hold the config rows in memory (the Discord panel's cascade view)
    should call the pure predicate directly instead of paying for this read.
    """
    tenant_row, channel_rows = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    return is_agent_reachable(agent_name, tenant=tenant_row, channels=channel_rows, default=default)


async def is_agent_shared_for_attachments(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    ma_agent_id: str,
    default: DeploymentDefault,
) -> bool:
    """Reachability for the attachment rules (replacing or removing an MCP server).

    Wider than `is_agent_reachable_in_tenant`: an agent also answers other
    people when a live handoff or setup thread is bound to it, or when it is
    someone's personal default. Repointing its servers reaches them too.
    """
    if await is_agent_reachable_in_tenant(
        session, tenant_id=tenant_id, agent_name=agent_name, default=default
    ):
        return True
    bound = await session.scalar(
        select(ThreadAgentBinding.id)
        .where(
            ThreadAgentBinding.tenant_id == tenant_id,
            ThreadAgentBinding.responder_ma_agent_id == ma_agent_id,
            ThreadAgentBinding.deleted.is_(False),
        )
        .limit(1)
    )
    if bound is not None:
        return True
    personal = await session.scalar(
        select(UserConfig.account_id)
        .join(Account, Account.id == UserConfig.account_id)
        .where(Account.tenant_id == tenant_id, UserConfig.agent_name == agent_name)
        .limit(1)
    )
    return personal is not None


async def _fetch_user(session: AsyncSession, *, account_id: uuid.UUID) -> UserConfigRow | None:
    stmt = select(UserConfig).where(UserConfig.account_id == account_id)
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return UserConfigRow(agent_name=orm.agent_name, environment_name=orm.environment_name)


async def _fetch_channel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
) -> ChannelConfigRow | None:
    stmt = select(ChannelConfig).where(
        ChannelConfig.tenant_id == tenant_id,
        ChannelConfig.channel_id == channel_id,
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return ChannelConfigRow(
        tenant_id=orm.tenant_id,
        channel_id=orm.channel_id,
        agent_name=orm.agent_name,
        environment_name=orm.environment_name,
        agent_name_set_by_account_id=orm.agent_name_set_by_account_id,
        agent_name_set_at=orm.agent_name_set_at,
        mode=cast(Literal["agent", "user_active"], orm.mode),
    )


async def _fetch_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantConfigRow | None:
    stmt = select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return TenantConfigRow(
        tenant_id=orm.tenant_id,
        agent_name=orm.agent_name,
        environment_name=orm.environment_name,
        mode=cast(Literal["agent", "user_active"], orm.mode),
        agent_name_set_by_account_id=orm.agent_name_set_by_account_id,
        agent_name_set_at=orm.agent_name_set_at,
    )


async def has_personal_default(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_names: Collection[str]
) -> bool:
    """Whether someone in the tenant has one of `agent_names` as their personal default."""
    personal = await session.scalar(
        select(UserConfig.account_id)
        .join(Account, Account.id == UserConfig.account_id)
        .where(Account.tenant_id == tenant_id, UserConfig.agent_name.in_(agent_names))
        .limit(1)
    )
    return personal is not None


async def is_agent_shared_for_key_changes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_names: Collection[str],
    ma_agent_id: str,
    default: DeploymentDefault,
    caller_account_id: uuid.UUID | None = None,
    caller_platform_user_id: str | None = None,
) -> bool:
    """Whether replacing or removing this agent's keys reaches other people.

    Wider than `is_agent_reachable_in_tenant`, and checked against EVERY name
    the agent answers to (its display name and its ``daimon_name`` routing
    name) so a mismatch between them cannot make a shared agent look private.
    The agent is shared when any of these holds:

    - a channel, tenant or deployment default names it, under any name;
    - a live handoff or setup thread is bound to it (by stable MA id);
    - it is someone's personal default, under any name;
    - an enabled routine runs it (by MA id, or under any name) that the caller
      did not create — the scheduler mounts the agent's keys on every fire;
    - another account has a live thread session with it (by MA id).

    The caller's own routines and sessions do not count: changing a key only
    they use reaches nobody else. A caller the gate cannot identify
    (`caller_account_id` / `caller_platform_user_id` left None) owns nothing,
    so every routine and session counts. No name at all fails closed.
    """
    names = {name for name in agent_names if name}
    if not names:
        return True
    tenant_row, channel_rows = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    if any(
        is_agent_reachable(name, tenant=tenant_row, channels=channel_rows, default=default)
        for name in names
    ):
        return True
    bound = await session.scalar(
        select(ThreadAgentBinding.id)
        .where(
            ThreadAgentBinding.tenant_id == tenant_id,
            ThreadAgentBinding.responder_ma_agent_id == ma_agent_id,
            ThreadAgentBinding.deleted.is_(False),
        )
        .limit(1)
    )
    if bound is not None:
        return True
    if await has_personal_default(session, tenant_id=tenant_id, agent_names=names):
        return True
    routine_stmt = select(Routine.id).where(
        Routine.tenant_id == tenant_id,
        Routine.enabled.is_(True),
        or_(Routine.agent_id == ma_agent_id, Routine.agent_name.in_(names)),
    )
    if caller_platform_user_id is not None:
        routine_stmt = routine_stmt.where(
            Routine.created_by_user_id.is_distinct_from(caller_platform_user_id)
        )
    if await session.scalar(routine_stmt.limit(1)) is not None:
        return True
    session_stmt = select(ThreadSession.id).where(
        ThreadSession.tenant_id == tenant_id,
        ThreadSession.ma_agent_id == ma_agent_id,
        ThreadSession.status == "live",
    )
    if caller_account_id is not None:
        session_stmt = session_stmt.where(
            ThreadSession.account_id.is_distinct_from(caller_account_id)
        )
    return await session.scalar(session_stmt.limit(1)) is not None
