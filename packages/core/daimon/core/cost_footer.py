"""The finished-turn cost footer: whether a workspace shows it, and how it is described.

The note is shared by the `/cost-footer` command and the cost footer MCP tools,
so the slash command and the agent describe the setting alike.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.stores.tenants import get_cost_footer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def cost_footer_shown(
    session_factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID
) -> bool:
    """Whether the tenant's finished turns end with the footer.

    A failed read shows it: the footer is the default, and a turn never loses
    its answer or its close over a display setting.
    """
    try:
        async with session_factory() as session:
            return await get_cost_footer(session, tenant_id=tenant_id)
    except Exception:
        log.warning("turn.cost_footer_read_failed", exc_info=True)
        return True


def cost_footer_note(shown: bool) -> str:
    if shown:
        return (
            "Finished answers here end with a footer naming the agent, the time "
            "taken, what the answer cost and the money left."
        )
    return (
        "Finished answers here end without the footer: no agent name, time, cost "
        "or money left under them. The billing panel still shows usage and credit."
    )
