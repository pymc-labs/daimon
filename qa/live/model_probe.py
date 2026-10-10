"""Read deployment config and MA agent metadata; never create a turn or change config."""

from __future__ import annotations

import asyncio
import sys

from qa.live.schema import Contract


class ProbeRequest(Contract):
    guild_id: str
    channel_id: str
    category_id: str


class ModelEvidence(Contract):
    guild_id: str
    channel_id: str
    agent_name: str
    agent_id: str
    model: str
    channel_pinned: bool


async def probe(request: ProbeRequest) -> ModelEvidence:
    # Run with the deployment's settings/credentials, locally there or through
    # an operator-configured IAP argv command. These APIs only read metadata.
    from daimon.adapters.cli.runtime import build_runtime
    from daimon.core.config import load_settings
    from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.scope import ScopeContext
    from daimon.core.stores.scoped_config_read import resolve
    from daimon.core.stores.tenants import get_tenant
    from sqlalchemy import text

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=request.guild_id)
    async with build_runtime(load_settings()) as runtime:
        async with runtime.sessionmaker() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout = '15000ms'"))
            if await get_tenant(session, tenant_id) is None:
                raise ValueError("QA tenant was not found")
            config = await resolve(
                session,
                context=ScopeContext(
                    tenant_id=tenant_id, channel_id=request.channel_id, platform="discord"
                ),
                default=runtime.deployment_default,
            )
        if not config.agent_name:
            raise ValueError("QA channel has no resolved agent")
        agent = await find_agent_by_daimon_tag(
            runtime.anthropic,
            tenant_id=tenant_id,
            name=config.agent_name,
        )
        if agent is None:
            raise ValueError("resolved QA agent was not found")
        return ModelEvidence(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            agent_name=config.agent_name,
            agent_id=agent.id,
            model=agent.model.id,
            channel_pinned=config.agent_name_tier == "channel",
        )


if __name__ == "__main__":
    try:
        print(
            asyncio.run(probe(ProbeRequest.model_validate_json(sys.stdin.read()))).model_dump_json()
        )
    except Exception as exc:
        print(f"model probe unavailable: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
