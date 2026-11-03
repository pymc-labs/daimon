"""The audit row for a change to an agent, written in the change's own transaction."""

from __future__ import annotations

from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_target import ChangeRecorder
from sqlalchemy.ext.asyncio import AsyncSession


def recorded(
    runtime: WebRuntime, caller: WebCaller, action: str, detail: str | None = None
) -> ChangeRecorder:
    """A recorder naming the agent the change was made on; ``detail`` never carries a value."""

    async def record(db: AsyncSession, agent_name: str) -> None:
        await audit(
            db,
            tenant_id=caller.person.tenant_id,
            actor_account_id=caller.person.account_id,
            action=action,
            target=agent_name,
            detail=detail,
            at=runtime.now(),
        )

    return record
