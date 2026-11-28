"""Every routine surface says Legacy, and each list that acts on routines offers admins the move.

Routines still run; workflows replace them. A surface that drops the label, or an admin's
button, is a path the change missed (docs/routines.md, `daimon.core.routines.LEGACY`).
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import discord
import pytest
from daimon.adapters.cli.commands.routines import routines_app
from daimon.adapters.discord.routines_panel.embeds import build_panel_container
from daimon.adapters.discord.routines_panel.state import RoutinesPanelState as DiscordState
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.routine_run import register_routine_run_tool
from daimon.adapters.mcp.tools.routines import register_routines_tools
from daimon.adapters.slack.routines_panel.state import RoutinesPanelState as SlackState
from daimon.adapters.slack.routines_panel.views import build_content_view
from daimon.adapters.teams.routines_card import panel_card
from daimon.core.credit_report import channel_entry
from daimon.core.credit_views import ChannelSpend
from daimon.core.routines import LEGACY
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]


def _discord() -> str:
    state = DiscordState.initial(rows=[], over_cap_count=0, agent_name_map={})
    container = build_panel_container(state, now=datetime.now(UTC))
    return " ".join(
        cast("str", item.content)
        for item in container.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _slack() -> str:
    state = SlackState(rows=[], over_cap_count=0, agent_name_map={})
    return json.dumps(build_content_view(state))


def _teams() -> str:
    return json.dumps(panel_card([], 0, user_id="u", is_admin=False).model_dump(by_alias=True))


def _cli() -> str:
    return routines_app.info.help or ""


def _docs() -> str:
    return (ROOT / "docs" / "routines.md").read_text()


def _credit_row() -> str:
    spend = ChannelSpend("discord", None, "routine", Decimal(1), 1)
    return channel_entry(spend, {}).name


async def _mcp() -> str:
    mcp = FastMCP("routines-legacy")
    register_routines_tools(mcp, cast(McpRuntime, None))
    register_routine_run_tool(mcp, cast(McpRuntime, None))
    tools = await mcp.local_provider.list_tools()
    assert tools, "the routine tools are registered"
    missing = [
        t.name for t in tools if "Legacy: prefer the workflow tools" not in (t.description or "")
    ]
    return LEGACY if not missing else f"missing on {missing}"


SURFACES: dict[str, Callable[[], Any]] = {
    "discord /routines": _discord,
    "slack /routines": _slack,
    "teams routines": _teams,
    "cli routines help": _cli,
    "docs/routines.md": _docs,
    "credit report row": _credit_row,
    "mcp routine tools": _mcp,
}


@pytest.mark.parametrize("surface", SURFACES)
async def test_routine_surface_shows_legacy(surface: str) -> None:
    """Each routine surface carries the label (the web page is checked by Routines.test.tsx)."""
    shown = SURFACES[surface]()
    text = await shown if inspect.isawaitable(shown) else shown
    assert LEGACY in text, f"{surface} does not say {LEGACY}: {text[:200]}"
