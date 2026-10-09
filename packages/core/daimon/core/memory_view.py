"""Read-only view of the memory store behind a chat's agent, for the `memory` commands."""

from __future__ import annotations

import uuid

from anthropic import AsyncAnthropic
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.rule_views import is_memory_hidden
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.domain import Platform
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_channel_memory_store(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: Platform,
    user_id: str,
    channel_id: str,
    default: DeploymentDefault,
    thread_id: str | None = None,
) -> tuple[str, str] | None:
    """`(agent_name, memory_store_id)` for the agent answering in `channel_id`.

    With `thread_id`, the agent answering in that thread under it, so a thread
    bound to another agent shows that agent's memory.
    None when no agent is configured there, it has no memory store yet, or its
    pin or channel isolation hides it from that place.
    Raises DaimonError when the configured agent is missing on the MA side.
    """
    # Never committed: a first-contact principal only scopes this read.
    async with sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=user_id
        )
        scope = ScopeContext(
            account_id=principal.account_id,
            tenant_id=tenant_id,
            channel_id=channel_id,
            platform=platform,
            thread_id=thread_id,
        )
        config = await resolve_config(session, context=scope, default=default)
        policy = await load_access_policy(session, tenant_id=tenant_id)
    if config.agent_name is None:
        return None
    agent = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=config.agent_name)
    if agent is None:
        raise DaimonError(f"Configured agent '{config.agent_name}' not found.")
    names = (config.agent_name, *agent_pin_names(agent.name, agent.metadata))
    if is_memory_hidden(
        policy,
        agent_names=names,
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id if thread_id is not None else None,
    ):
        return None
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
    async with sessionmaker() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_uuid)
    return None if store_id is None else (config.agent_name, store_id)


async def list_memory_paths(anthropic: AsyncAnthropic, store_id: str) -> list[str]:
    """Every memory path in the store, sorted."""
    page = await anthropic.beta.memory_stores.memories.list(store_id, path_prefix="/")
    return sorted([item.path async for item in page if item.type == "memory"])


async def get_memory_content(anthropic: AsyncAnthropic, store_id: str, path: str) -> str | None:
    """The memory at exactly `path`, or None when there is none."""
    page = await anthropic.beta.memory_stores.memories.list(store_id, path_prefix="/")
    async for item in page:
        if item.type == "memory" and item.path == path:
            memory = await anthropic.beta.memory_stores.memories.retrieve(
                item.id, memory_store_id=store_id, view="full"
            )
            return memory.content or ""
    return None
