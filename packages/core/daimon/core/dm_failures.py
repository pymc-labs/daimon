"""Record a direct message that went unanswered as an ops issue, with its person and agent.

One helper for every chat adapter's DM failure paths. The caller logs its own line; this
only adds the row. The person and the bound agent are read where the conversation is
known, and a failed read leaves them out: reporting never breaks a failure path.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

import structlog
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.stores.direct_messages import get_conversation
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.thread_agent_bindings import get_binding
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: The design's stages of an unanswered direct message.
DmStage = Literal["discovery", "reply", "dropped", "not_sent"]
_LOOKUP_TIMEOUT_S = 2.0


async def _subject(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    platform: str,
    route_key: str | None,
    user_id: str | None,
) -> tuple[uuid.UUID | None, str | None]:
    """The person's account and the conversation's bound agent, where known."""
    if user_id is None:
        return None, None
    async with sm() as session:
        conversation = (
            await get_conversation(
                session, platform=platform, route_key=route_key, external_user_id=user_id
            )
            if route_key is not None
            else None
        )
        if conversation is None or conversation.tenant_id != tenant_id:
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform=platform, external_id=user_id
            )
            return (principal.account_id if principal is not None else None), None
        binding = await get_binding(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=conversation.channel_id,
            thread_id=conversation.scope_id,
        )
        agent_id = binding.responder_ma_agent_id if binding is not None else None
        return conversation.account_id, agent_id


async def report_dm_issue(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID | None,
    reason: DmStage | Literal["queue_full"],
    *,
    platform: str,
    route_key: str | None = None,
    user_id: str | None = None,
) -> None:
    """Record one `dm.failed` at its stage, or a `turn.shed` for a full queue. Never raises."""
    account_id: uuid.UUID | None = None
    agent_id: str | None = None
    if tenant_id is not None:
        try:
            async with asyncio.timeout(_LOOKUP_TIMEOUT_S):
                account_id, agent_id = await _subject(
                    sm, tenant_id, platform=platform, route_key=route_key, user_id=user_id
                )
        except Exception as exc:
            # Boundary: the issue is still recorded, only without its person and agent.
            log.info("dm.failure_subject_unread", error_class=type(exc).__name__)
    report_ops_event(
        sm,
        OpsEvent(
            "turn.shed" if reason == "queue_full" else "dm.failed",
            tenant_id,
            reason=reason,
            platform=platform,
            account_id=account_id,
            agent_id=agent_id,
        ),
        logged=False,
    )
