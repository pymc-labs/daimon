"""Executable record: a watch on a page's events can be made from Discord only.

A page watch reports in the thread that made it and enrolls through a Discord thread's
approval card. Slack, Teams and the web app have no port of that enrollment, so the store
refuses their bindings and `create_watch` is a Discord tool. Their agents read a page's
events with `artifact_db_query` instead. If another platform gains it, update this record
rather than delete it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.watches import register_watch_tools
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores import watches
from fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_the_store_refuses_a_page_watch_from_another_platform(platform: str) -> None:
    binding = ContinuationRequest(
        tenant_id=uuid.uuid4(),
        platform=platform,  # type: ignore[arg-type]  # the parametrized chat platforms
        parent_channel_id="C1",
        thread_id="T1",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="U1",
        target_ma_agent_id="agent",
        target_name="analyst",
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    with pytest.raises(ValueError, match="Discord"):
        await watches.register_page_watch(
            cast(AsyncSession, None),  # refused before any query
            binding,
            instructions="Tally the votes.",
            page_id=uuid.uuid4(),
            expires_at=None,
            now=datetime.now(UTC),
        )


async def test_create_watch_is_a_discord_tool() -> None:
    mcp = FastMCP("record")
    register_watch_tools(mcp, cast(McpRuntime, object()))
    tools = {tool.name: tool for tool in await mcp.local_provider.list_tools()}
    assert tools["create_watch"].tags == {"discord"}
