"""The agent answering in a channel, for a read made before admission names it."""

import uuid

import structlog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.scope import ScopeContext
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from sqlalchemy.exc import SQLAlchemyError

_log = structlog.get_logger()


async def channel_agent_id(
    runtime: DiscordRuntime, tenant_id: uuid.UUID, channel_id: str
) -> str | None:
    """The provider id of the channel's configured agent, or None on a cold resolver cache
    or a failed read, so the caller reads at the channel level instead."""
    scope = ScopeContext(tenant_id=tenant_id, channel_id=channel_id, platform="discord")
    try:
        async with runtime.sessionmaker() as session:
            config = await resolve_config(
                session, context=scope, default=runtime.deployment_default
            )
    except SQLAlchemyError:
        _log.warning("channel_agent.read_failed", exc_info=True)
        return None
    if config.agent_name is None:
        return None
    return runtime.resolver_cache.get((tenant_id, "agent", config.agent_name))
