"""Which daimon tools an agent enables by name, and their record per agent."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from daimon.core.stores.agent_daimon_tools import (
    enabled_daimon_tools,
    get_enabled_daimon_tools,
    record_enabled_daimon_tools,
)
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def _daimon_toolset(
    *, default_enabled: bool | None, configs: list[dict[str, Any]]
) -> dict[str, Any]:
    default_config: dict[str, Any] = {"permission_policy": {"type": "always_allow"}}
    if default_enabled is not None:
        default_config["enabled"] = default_enabled
    return {
        "type": "mcp_toolset",
        "mcp_server_name": DAIMON_SERVER_NAME,
        "default_config": default_config,
        "configs": configs,
    }


_ALLOWLIST = _daimon_toolset(
    default_enabled=False,
    configs=[
        {"name": "list_watches", "enabled": True},
        {"name": "create_watch", "enabled": True, "permission_policy": {"type": "always_ask"}},
        {"name": "delete_watch", "enabled": False},
        {"name": "show_watch"},
    ],
)


def test_names_only_the_tools_an_allowlist_enables() -> None:
    assert enabled_daimon_tools([_ALLOWLIST]) == {"list_watches", "create_watch"}, (
        "a config that is off, or leaves `enabled` to the toolset's off default, is not enabled"
    )


def test_a_toolset_with_its_default_on_has_no_allowlist() -> None:
    for default_enabled in (True, None):
        toolset = _daimon_toolset(
            default_enabled=default_enabled, configs=[{"name": "delete_watch", "enabled": False}]
        )
        assert enabled_daimon_tools([toolset]) is None


def test_other_servers_allowlists_are_ignored() -> None:
    other = {**_ALLOWLIST, "mcp_server_name": "notion"}
    sandbox = {"type": "agent_toolset_20260401", "default_config": {"enabled": False}}
    assert enabled_daimon_tools([other, sandbox]) is None


@pytest_asyncio.fixture
async def factory(
    db_engine: AsyncEngine, db_clean: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(db_engine, expire_on_commit=False)


async def test_record_replaces_and_clears_an_agents_allowlist(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory.begin() as session:
        tenant = await make_tenant(session)
    agent_id = uuid.uuid4()

    async def stored() -> frozenset[str] | None:
        async with factory() as session:
            return await get_enabled_daimon_tools(session, tenant_id=tenant.id, agent_id=agent_id)

    await record_enabled_daimon_tools(
        factory, tenant_id=tenant.id, agent_id=agent_id, tools=[_ALLOWLIST]
    )
    assert await stored() == {"list_watches", "create_watch"}

    narrower = _daimon_toolset(
        default_enabled=False, configs=[{"name": "show_watch", "enabled": True}]
    )
    await record_enabled_daimon_tools(
        factory, tenant_id=tenant.id, agent_id=agent_id, tools=[narrower]
    )
    assert await stored() == {"show_watch"}, "a later session's tools replace the earlier list"

    open_toolset = _daimon_toolset(default_enabled=True, configs=[])
    await record_enabled_daimon_tools(
        factory, tenant_id=tenant.id, agent_id=agent_id, tools=[open_toolset]
    )
    assert await stored() is None, "dropping the allowlist must stop the server narrowing"
