"""The hub lists a gated tool while its feature is On in one of the caller's workspaces."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

from daimon.adapters.mcp.hub.data_gate import HubDataGate
from daimon.adapters.mcp.hub.identity import HubIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.capability_tools import LIBRARY_SKILLS, SERVING_TOOLS
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.data_tools import DATA_TOOLS
from daimon.core.hub_identity import HubTenant
from daimon.testing.factories import make_tenant, turn_on
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SETTINGS = Settings(
    database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
    anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
)


async def test_library_skills_on_in_one_workspace_lists_them_for_the_hub_caller(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    on, off = await make_tenant(db_session), await make_tenant(db_session)
    await db_session.commit()
    runtime = SimpleNamespace(session_factory=db_session_factory, settings=_SETTINGS)
    gate = HubDataGate(cast(McpRuntime, runtime))

    def hub(*tenants: uuid.UUID) -> HubIdentity:
        return HubIdentity(
            platform="discord",
            platform_user_id="7",
            tenants=tuple(
                HubTenant(
                    tenant_id=t, account_id=uuid.uuid4(), workspace_id=str(t), workspace_name="W"
                )
                for t in tenants
            ),
        )

    both = hub(on.id, off.id)
    assert await gate._hidden(both) == DATA_TOOLS | SERVING_TOOLS  # pyright: ignore[reportPrivateUsage]
    async with db_session_factory.begin() as session:
        await turn_on(session, tenant_id=on.id, key=LIBRARY_SKILLS)
    assert await gate._hidden(both) == DATA_TOOLS  # pyright: ignore[reportPrivateUsage]
    assert await gate._hidden(hub(off.id)) == DATA_TOOLS | SERVING_TOOLS  # pyright: ignore[reportPrivateUsage]
