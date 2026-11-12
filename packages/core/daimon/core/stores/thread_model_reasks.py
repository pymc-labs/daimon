"""Which consent a conversation was already asked to confirm again.

A conversation's consent goes stale when its agent is edited. The first message after
the edit posts the preview card; this record keeps the next ones quiet until the agent
changes again. It is a `thread_model` audit event (`reask`), so it needs no table of
its own. Callers own the transaction.
"""

from __future__ import annotations

import uuid

from daimon.core._models import SecurityAuditEvent
from daimon.core.stores import security_audit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_TOOL = "thread_model"
_OPERATION = "reask"


def reask_scope(tenant_id: uuid.UUID, platform: str, conversation_id: str) -> str:
    """The conversation, as `thread_model_switch` scopes its own audit events."""
    return f"thread_model:{tenant_id}:{platform}:{conversation_id}"


def reask_key(digest: str, agent_version: int | str) -> str:
    """The state a card was posted for: this consent, this agent revision."""
    return f"re-ask; key={digest}:{agent_version}"


async def reask_posted(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: str, key: str
) -> bool:
    """Whether the card for this consent and agent revision already went out."""
    found = await session.scalar(
        select(SecurityAuditEvent.id)
        .where(
            SecurityAuditEvent.tenant_id == tenant_id,
            SecurityAuditEvent.tool_name == _TOOL,
            SecurityAuditEvent.operation == _OPERATION,
            SecurityAuditEvent.scope == scope,
            SecurityAuditEvent.reason == key,
        )
        .limit(1)
    )
    return found is not None


async def record_reask(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    platform_user_id: str | None,
    scope: str,
    key: str,
) -> None:
    """Remember the card went out. The audit table is append-only: this stays."""
    await security_audit.append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        platform=platform,
        platform_user_id=platform_user_id,
        tool_name=_TOOL,
        operation=_OPERATION,
        outcome="allowed",
        reason=key,
        scope=scope,
    )
