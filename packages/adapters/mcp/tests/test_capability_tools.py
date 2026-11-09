"""A capability feature switches exactly the tools the server registers for it."""

from __future__ import annotations

from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.capability_tools import CAPABILITY_TOOLS
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_tool_schema_snapshot import (
    _fully_configured_settings,  # pyright: ignore[reportPrivateUsage]
)

_FAMILIES = {
    "watches": lambda name: "watch" in name,
    "routines": lambda name: "routine" in name,
    "long_jobs": lambda name: "long_job" in name,
    "compute": lambda name: name.startswith("fly_"),
    "microsoft_365": lambda name: "microsoft" in name,
    "lead_intake": lambda name: name.startswith("lead_intake_"),
}


async def test_capability_tools_are_the_registered_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    registered = {tool.name for tool in await app.state.mcp.local_provider.list_tools()}

    for key, names in CAPABILITY_TOOLS.items():
        assert names <= registered, f"{key}: names no registered tool: {sorted(names - registered)}"
    for key, belongs in _FAMILIES.items():
        family = {name for name in registered if belongs(name)}
        assert family <= CAPABILITY_TOOLS[key], (
            f"{key}: a tool of this family is not switched by it"
        )
