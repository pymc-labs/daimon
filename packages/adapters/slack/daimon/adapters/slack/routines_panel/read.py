"""Load + sort + cap the routines for a single tenant for /routines.

Shell module: performs real I/O (DB reads + MA agent list). Ported from
discord/routines_panel/read.py with color dropped from RoutineEntry.
"""

from __future__ import annotations

import uuid

from anthropic import AsyncAnthropic
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.routines_panel.state import RoutineEntry
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.routines import derive_glyph, panel_rows, routine_label
from daimon.core.stores.routines import list_routines_for_tenant
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["load_routines"]


async def load_routines(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    viewer_user_id: str | None,
) -> tuple[list[RoutineEntry], int, dict[str, str]]:
    """Fetch routines + tenant agents; sort, cap at 25, and decorate per entry.

    Returns ``(entries, over_cap_count, agent_name_map)``.
    ``agent_name_map`` covers all tenant agents so the caller can render
    name fallbacks without an extra LIST call per row.

    Args:
        session:   Async DB session (schema-scoped in tests).
        anthropic: Injected ``AsyncAnthropic`` client.
        tenant_id: Slack workspace tenant UUID.
        viewer_user_id: The Slack user the panel is for, who sees only the
            routines they created; ``None`` for an admin, who sees them all.
            A routine's label and trigger are its creator's (often a
            client's) work.

    Returns:
        Tuple of ``(entries[:25], over_cap_count, agent_name_map)``.
    """
    rows = await list_routines_for_tenant(session, tenant_id=tenant_id)
    if viewer_user_id is not None:
        rows = [row for row in rows if row.created_by_user_id == viewer_user_id]
    agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)

    agent_name_map: dict[str, str] = {}
    for agent in agents:
        name: str | None = agent.metadata.get("daimon_name")  # type: ignore[assignment]
        if name is None:
            name = agent.id
        agent_name_map[agent.id] = name

    shown, over_cap_count = panel_rows(rows)
    entries = [
        RoutineEntry(
            routine=row,
            agent_name=agent_name_map.get(row.agent_id, f"<agent {row.agent_id[:8]}>"),
            glyph=derive_glyph(row),
            label=routine_label(row),
        )
        for row in shown
    ]
    return entries, over_cap_count, agent_name_map


async def routines_viewer(client: AsyncWebClient, *, user_id: str) -> str | None:
    """``viewer_user_id`` for ``load_routines``: None for an admin, else the user.

    A failed admin lookup counts as not admin, so it narrows rather than widens.
    """
    return None if await resolve_is_admin(client, user_id=user_id) else user_id
