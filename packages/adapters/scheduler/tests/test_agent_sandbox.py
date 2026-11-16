"""The scheduler prefers an operator's agent-level sandbox opt-in over a routine pin."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

from daimon.adapters.scheduler.main import _routine_sandbox
from daimon.core.authz import Subject
from daimon.core.routine_sandbox import AgentSandboxPin
from daimon.core.stores.agent_sandbox import set_agent_sandbox_tools
from daimon.core.stores.domain import RoutineRow
from daimon.testing.factories import make_tenant


async def test_agent_opt_in_wins_and_never_clears_itself(db_session_factory) -> None:
    """With an agent opt-in the run uses it; a mismatch is only logged."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session)
        await set_agent_sandbox_tools(
            session,
            tenant_id=tenant.id,
            agent_id="ag",
            pin=AgentSandboxPin("ag", "default"),
            set_by="op",
            actor=Subject(is_admin=True),
        )
    row = cast(
        RoutineRow,
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant.id, agent_id="ag", sandbox_tools_enabled=False
        ),
    )

    sandbox = await _routine_sandbox(db_session_factory, row)

    assert sandbox is not None
    assert sandbox.pin == AgentSandboxPin("ag", "default"), "the agent pin is used"
    await sandbox.on_drift()  # must not touch the routine


async def test_no_opt_in_and_no_routine_pin_means_no_sandbox(db_session_factory) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session)
    row = cast(
        RoutineRow,
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant.id, agent_id="ag", sandbox_tools_enabled=False
        ),
    )

    assert await _routine_sandbox(db_session_factory, row) is None
