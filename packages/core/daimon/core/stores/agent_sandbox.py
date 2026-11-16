"""An operator's agent-level opt-in to built-in sandbox tools for routine runs.

It names the agent and the environment, never an agent version, so editing the
agent keeps it. A row exists while the opt-in is on. Each change is logged in
`agent_sandbox_history` and as `agent.sandbox_tools_set`.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from daimon.core._models import AgentSandboxHistoryRecord, AgentSandboxTools
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.stores.domain import AgentSandboxPin
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AGENT_SETTER_ERASED",
    "agent_sandbox_history",
    "agent_sandbox_pin",
    "erase_agent_sandbox_setter",
    "set_agent_sandbox_tools",
]

log = structlog.get_logger(__name__)

AGENT_SETTER_ERASED: Final = "erased"


async def agent_sandbox_pin(
    session: AsyncSession, *, tenant_id: _uuid.UUID, agent_id: str
) -> AgentSandboxPin | None:
    """The agent's recorded opt-in, or None when it has none."""
    row = await session.get(AgentSandboxTools, (tenant_id, agent_id))
    return None if row is None else AgentSandboxPin(row.agent_id, row.environment_name)


async def set_agent_sandbox_tools(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    agent_id: str,
    pin: AgentSandboxPin | None,
    set_by: str,
    actor: Subject,
) -> bool:
    """Opt the agent in with `pin`, or out with None. Returns whether anything changed.

    The caller has read that the agent and environment are live. Setting the value
    it already has writes nothing. Only the CLI operator calls this.
    """
    if not authorize(TenantAccessPolicy(), subject=actor, action=Action.SET_ROUTINE_SANDBOX_TOOLS):
        raise PermissionError("only an operator may change an agent's sandbox tools")
    if pin is not None and pin.agent_id != agent_id:
        raise ValueError("the pin names another agent")
    row = await session.scalar(
        select(AgentSandboxTools)
        .where(AgentSandboxTools.tenant_id == tenant_id, AgentSandboxTools.agent_id == agent_id)
        .with_for_update()
    )
    if (row is None) == (pin is None) and (
        row is None or pin is None or row.environment_name == pin.environment_name
    ):
        return False
    now = datetime.now(UTC)
    if pin is None:
        assert row is not None
        await session.delete(row)
    elif row is None:
        session.add(
            AgentSandboxTools(
                tenant_id=tenant_id,
                agent_id=agent_id,
                environment_name=pin.environment_name,
                set_by=set_by,
                set_at=now,
            )
        )
    else:
        row.environment_name = pin.environment_name
        row.set_by = set_by
        row.set_at = now
    session.add(
        AgentSandboxHistoryRecord(
            tenant_id=tenant_id,
            agent_id=agent_id,
            after_enabled=pin is not None,
            environment_name=None if pin is None else pin.environment_name,
            set_by=set_by,
            set_at=now,
        )
    )
    await session.flush()
    log.info(
        "agent.sandbox_tools_set",
        tenant_id=str(tenant_id),
        agent_id=agent_id,
        enabled=pin is not None,
        environment=None if pin is None else pin.environment_name,
        set_by=set_by,
    )
    return True


async def erase_agent_sandbox_setter(session: AsyncSession, *, ids: Collection[str]) -> int:
    """Privacy erasure: replace a purged operator principal's id as a setter."""
    erased = 0
    for setter in ids:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(AgentSandboxHistoryRecord)
                .where(AgentSandboxHistoryRecord.set_by == setter)
                .values(set_by=AGENT_SETTER_ERASED)
            ),
        )
        erased += result.rowcount or 0
        await session.execute(
            update(AgentSandboxTools)
            .where(AgentSandboxTools.set_by == setter)
            .values(set_by=AGENT_SETTER_ERASED)
            .execution_options(synchronize_session=False)
        )
    return erased


async def agent_sandbox_history(
    session: AsyncSession, *, tenant_id: _uuid.UUID, agent_id: str
) -> list[AgentSandboxHistoryRecord]:
    """Every recorded change for one agent, oldest first."""
    return list(
        (
            await session.scalars(
                select(AgentSandboxHistoryRecord)
                .where(
                    AgentSandboxHistoryRecord.tenant_id == tenant_id,
                    AgentSandboxHistoryRecord.agent_id == agent_id,
                )
                .order_by(AgentSandboxHistoryRecord.set_at, AgentSandboxHistoryRecord.id)
            )
        ).all()
    )
