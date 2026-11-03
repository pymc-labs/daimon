"""The daimon tools an agent enables by name.

An agent whose daimon toolset turns its default off and enables tools one by
one gets only the tools daimon lists under those names. Daimon's server lists
most tools only behind `search_tools` and `call_tool`, so the session that
creates or updates an agent's tools records the names here, and the server
lists exactly them for that agent's chat credential.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from daimon.core._models import AgentDaimonToolAllowlist
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def enabled_daimon_tools(tools: Sequence[Mapping[str, Any]]) -> frozenset[str] | None:
    """The tool names daimon's toolset enables one by one, or None when its default is on.

    `tools` are the session's tools as SDK params dicts. None also covers an
    agent with no daimon toolset: nothing would reach the server to narrow.
    """
    for tool in tools:
        if tool.get("type") != "mcp_toolset" or tool.get("mcp_server_name") != DAIMON_SERVER_NAME:
            continue
        default_config: Mapping[str, Any] = tool.get("default_config") or {}
        if default_config.get("enabled", True):
            return None
        configs: Sequence[Mapping[str, Any]] = tool.get("configs") or ()
        # A config that leaves `enabled` out takes the toolset's default: off.
        return frozenset(config["name"] for config in configs if config.get("enabled", False))
    return None


async def get_enabled_daimon_tools(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> frozenset[str] | None:
    row = await session.get(AgentDaimonToolAllowlist, (tenant_id, agent_id))
    return None if row is None else frozenset(row.tool_names)


async def record_enabled_daimon_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tools: Sequence[Mapping[str, Any]],
) -> None:
    """Store the agent's enabled daimon tools from a session's `tools`, or clear them."""
    names = enabled_daimon_tools(tools)
    async with sessionmaker.begin() as session:
        if names is None:
            await session.execute(
                delete(AgentDaimonToolAllowlist).where(
                    AgentDaimonToolAllowlist.tenant_id == tenant_id,
                    AgentDaimonToolAllowlist.agent_id == agent_id,
                )
            )
            return
        stmt = pg_insert(AgentDaimonToolAllowlist).values(
            tenant_id=tenant_id, agent_id=agent_id, tool_names=sorted(names)
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "agent_id"],
                set_={"tool_names": stmt.excluded.tool_names},
            )
        )
