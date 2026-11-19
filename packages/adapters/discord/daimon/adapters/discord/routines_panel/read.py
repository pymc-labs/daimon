"""Load + sort + cap the routines for a single tenant for /routines."""

from __future__ import annotations

import dataclasses
import uuid

from anthropic import AsyncAnthropic
from daimon.adapters.discord.routines_panel.state import (
    RoutineEntry,
    derive_state,
    picker_label,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.routine_sandbox import opted_in_by_many
from daimon.core.routines import routine_for_reader
from daimon.core.stores.routines import list_routines_for_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PICKER_CAP = 25


async def load_guild_routines(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    viewer_user_id: str | None = None,
    only_own: bool = False,
    records: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[list[RoutineEntry], int, dict[str, str]]:
    """Fetch routines + tenant agents; sort, cap, and decorate per entry.

    Returns ``(entries, over_cap_count, agent_name_map)``. ``agent_name_map``
    covers every agent in the tenant (not just those referenced) so the caller
    can render fallbacks without a per-row LIST. With `only_own`, only routines
    created by `viewer_user_id` are loaded, so a non-admin never reads another
    member's routine; no viewer then loads none.
    """
    rows = await list_routines_for_tenant(session, tenant_id=tenant_id)
    if only_own:
        rows = [
            row
            for row in rows
            if viewer_user_id is not None and row.created_by_user_id == viewer_user_id
        ]
    agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id, records=records)
    agent_name_map: dict[str, str] = {}
    for agent in agents:
        name = agent.metadata.get("daimon_name")
        if name is None:
            name = agent.id
        agent_name_map[agent.id] = name

    entries: list[RoutineEntry] = []
    for row in rows:
        row = routine_for_reader(row, user_id=viewer_user_id)
        glyph, color = derive_state(row)
        agent_name = agent_name_map.get(row.agent_id, f"<agent {row.agent_id[:8]}>")
        label = picker_label(row)
        entries.append(
            RoutineEntry(
                routine=row,
                agent_name=agent_name,
                glyph=glyph,
                color=color,
                label=label,
            )
        )
    entries.sort(key=lambda e: e.label.lower())
    over_cap_count = max(0, len(entries) - _PICKER_CAP)
    shown = entries[:_PICKER_CAP]
    by = await opted_in_by_many(session, [entry.routine for entry in shown], "discord")
    shown = [dataclasses.replace(entry, sandbox_by=by[entry.routine.id]) for entry in shown]
    return shown, over_cap_count, agent_name_map
