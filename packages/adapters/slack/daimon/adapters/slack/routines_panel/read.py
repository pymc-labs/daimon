"""Load + sort + cap the routines for a single tenant for /routines (SUX-02).

Shell module: performs real I/O (DB reads + MA agent list). Ported from
discord/routines_panel/read.py with color dropped from RoutineEntry.
"""

from __future__ import annotations

import uuid

from anthropic import AsyncAnthropic
from daimon.adapters.slack.routines_panel.state import (
    RoutineEntry,
    derive_glyph,
    picker_label,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.stores.routines import list_routines_for_tenant
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["load_routines"]

_PICKER_CAP = 25


async def load_routines(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
) -> tuple[list[RoutineEntry], int, dict[str, str]]:
    """Fetch routines + tenant agents; sort, cap at 25, and decorate per entry.

    Returns ``(entries, over_cap_count, agent_name_map)``.
    ``agent_name_map`` covers all tenant agents so the caller can render
    name fallbacks without an extra LIST call per row.

    Args:
        session:   Async DB session (schema-scoped in tests).
        anthropic: Injected ``AsyncAnthropic`` client.
        tenant_id: Slack workspace tenant UUID.

    Returns:
        Tuple of ``(entries[:25], over_cap_count, agent_name_map)``.
    """
    rows = await list_routines_for_tenant(session, tenant_id=tenant_id)
    agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)

    agent_name_map: dict[str, str] = {}
    for agent in agents:
        name: str | None = agent.metadata.get("daimon_name")  # type: ignore[assignment]
        if name is None:
            name = agent.id
        agent_name_map[agent.id] = name

    entries: list[RoutineEntry] = []
    for row in rows:
        glyph = derive_glyph(row)
        agent_name = agent_name_map.get(row.agent_id, f"<agent {row.agent_id[:8]}>")
        label = picker_label(row)
        entries.append(
            RoutineEntry(
                routine=row,
                agent_name=agent_name,
                glyph=glyph,
                label=label,
            )
        )

    entries.sort(key=lambda e: e.label.lower())
    over_cap_count = max(0, len(entries) - _PICKER_CAP)
    return entries[:_PICKER_CAP], over_cap_count, agent_name_map
