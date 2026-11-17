"""Routing for ask-a-human requests.

The adapter posts support requests to its configured escalation channel.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from daimon.core.native_admins import GroupMembers, ServerAdminIds
from daimon.core.stores.support_rows import SupportEscalationRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_RECIPIENTS: Final = 10
_log = structlog.get_logger(__name__)


async def support_recipient_tiers(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    requester_id: str,
    members: GroupMembers | None = None,
    server_admins: ServerAdminIds | None = None,
) -> tuple[tuple[str, ...], ...]:
    """No DM tier: the normal escalation channel receives the request."""
    return ()


def log_undelivered(row: SupportEscalationRow) -> None:
    """Every platform's error for a recorded request that reached no support channel.

    The row keeps its hold with `delivered_at` NULL; the requests list's undelivered
    filter (CLI, MCP, web) is where an admin finds it.
    """
    _log.error(
        "support.request_undelivered",
        escalation_id=str(row.id),
        tenant_id=str(row.tenant_id),
        platform=row.platform,
        status=row.status,
    )


__all__ = ["MAX_RECIPIENTS", "log_undelivered", "support_recipient_tiers"]
