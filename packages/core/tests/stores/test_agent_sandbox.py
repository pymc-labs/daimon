"""An operator's agent-level sandbox opt-in: who may set it, and what is recorded."""

from __future__ import annotations

import pytest
from daimon.core.authz import Subject
from daimon.core.stores.agent_sandbox import (
    AGENT_SETTER_ERASED,
    agent_sandbox_history,
    agent_sandbox_pin,
    erase_agent_sandbox_setter,
    set_agent_sandbox_tools,
)
from daimon.core.stores.domain import AgentSandboxPin
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

OPERATOR = Subject(is_admin=True)
PIN = AgentSandboxPin("ag", "default")


async def test_an_opt_in_is_recorded_once_and_logged(db_session: AsyncSession) -> None:
    """Setting the same pin twice changes nothing and logs once."""
    tenant = await make_tenant(db_session)
    kwargs = {"tenant_id": tenant.id, "agent_id": "ag", "set_by": "op", "actor": OPERATOR}

    assert await set_agent_sandbox_tools(db_session, pin=PIN, **kwargs)
    assert not await set_agent_sandbox_tools(db_session, pin=PIN, **kwargs), "a repeat is a no-op"

    assert await agent_sandbox_pin(db_session, tenant_id=tenant.id, agent_id="ag") == PIN
    history = await agent_sandbox_history(db_session, tenant_id=tenant.id, agent_id="ag")
    assert [(h.after_enabled, h.environment_name, h.set_by) for h in history] == [
        (True, "default", "op")
    ]


async def test_turning_it_off_forgets_the_pin_and_logs(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    kwargs = {"tenant_id": tenant.id, "agent_id": "ag", "set_by": "op", "actor": OPERATOR}
    await set_agent_sandbox_tools(db_session, pin=PIN, **kwargs)

    assert await set_agent_sandbox_tools(db_session, pin=None, **kwargs)
    assert not await set_agent_sandbox_tools(db_session, pin=None, **kwargs)

    assert await agent_sandbox_pin(db_session, tenant_id=tenant.id, agent_id="ag") is None
    history = await agent_sandbox_history(db_session, tenant_id=tenant.id, agent_id="ag")
    assert [h.after_enabled for h in history] == [True, False]


async def test_a_new_environment_replaces_the_pin(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    kwargs = {"tenant_id": tenant.id, "agent_id": "ag", "set_by": "op", "actor": OPERATOR}
    await set_agent_sandbox_tools(db_session, pin=PIN, **kwargs)

    assert await set_agent_sandbox_tools(db_session, pin=AgentSandboxPin("ag", "build"), **kwargs)

    pin = await agent_sandbox_pin(db_session, tenant_id=tenant.id, agent_id="ag")
    assert pin == AgentSandboxPin("ag", "build")


async def test_only_an_operator_may_set_it(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)

    with pytest.raises(PermissionError):
        await set_agent_sandbox_tools(
            db_session,
            tenant_id=tenant.id,
            agent_id="ag",
            pin=PIN,
            set_by="member",
            actor=Subject(is_admin=False),
        )
    assert await agent_sandbox_pin(db_session, tenant_id=tenant.id, agent_id="ag") is None


async def test_erasing_a_setter_keeps_the_change(db_session: AsyncSession) -> None:
    """Privacy erasure drops the person from the log, never the change."""
    tenant = await make_tenant(db_session)
    await set_agent_sandbox_tools(
        db_session, tenant_id=tenant.id, agent_id="ag", pin=PIN, set_by="gone", actor=OPERATOR
    )

    assert await erase_agent_sandbox_setter(db_session, ids=["gone"]) == 1

    history = await agent_sandbox_history(db_session, tenant_id=tenant.id, agent_id="ag")
    assert [h.set_by for h in history] == [AGENT_SETTER_ERASED]
