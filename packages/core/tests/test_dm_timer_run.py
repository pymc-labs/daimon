"""`run_dm_timer`: a due DM timer runs as a fresh private turn of its owner, or is refused."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.core import direct_messages
from daimon.core.authz import AgentRef, Place, Surface, build_subject
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.wakes import enqueue_wake
from daimon.core.direct_messages import (
    DirectMessageAccessRevoked,
    owner_dm_turn,
    run_dm_routine,
    run_dm_timer,
    run_dm_watch,
)
from daimon.core.scope import ResolvedConfig
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import ContinuationReason, TenantRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn.admission import Admission, AdmissionGrant, DmSource
from daimon.core.turn.errors import SessionBusyError
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import async_sessionmaker

OWNER = "555"


async def seed(factory, *, reason: ContinuationReason = "timer", dm: bool = True):
    async with factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="700000202")
        account = await make_account(session, tenant=tenant)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=dm)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="dm",
        thread_id=f"dm:{uuid.uuid4()}",
        requester_account_id=account.id,
        requester_external_user_id=OWNER,
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work="drink water",
        reason=reason,
        idempotency_key=uuid.uuid4(),
    )
    await enqueue_wake(factory, request, available_at=datetime.now(UTC) - timedelta(seconds=1))
    async with factory() as session:
        row = await get_continuation(session, idempotency_key=request.idempotency_key)
    assert row is not None
    return tenant, account, row


def admission_for(tenant: TenantRow, account_id: uuid.UUID, agent_id: str) -> Admission:
    return Admission(
        account_id=account_id,
        agent=ma_agent(id=agent_id, name="target-agent", tenant_id=tenant.id),
        environment=ma_environment(id="env_1", name="default", tenant_id=tenant.id),
        config=ResolvedConfig(agent_name="target-agent", environment_name="default"),
        grant=AdmissionGrant(
            tenant_id=tenant.id,
            subject=build_subject(is_admin=False, platform_user_id=OWNER, verified_membership=True),
            surface=Surface.DM,
            turn_place=Place(),
            agent=AgentRef.of("target-agent"),
            run_place=Place(),
            channel_id="dm",
            thread_id="dm",
            is_dm=True,
            cold_dm=True,
        ),
    )


def patch_world(monkeypatch, admission: Admission):
    admit = AsyncMock(return_value=admission)
    bind = AsyncMock(return_value=SimpleNamespace(admission=admission, backend=None))
    run = AsyncMock(return_value=SimpleNamespace(text="done", session_id="sesn_dm"))
    monkeypatch.setattr(direct_messages, "admit", admit)
    monkeypatch.setattr(direct_messages, "bind_session", bind)
    monkeypatch.setattr(direct_messages, "_run_admitted_dm_turn", run)
    return admit, bind, run


async def test_a_timer_runs_in_a_fresh_private_scope_with_its_prompt(
    db_session_factory: async_sessionmaker, monkeypatch
):
    tenant, account, row = await seed(db_session_factory)
    admit, _bind, run = patch_world(monkeypatch, admission_for(tenant, account.id, "ag_target"))

    reply = await run_dm_timer(deps_for(None, db_session_factory), row, prompt="seed text")

    assert (reply.text, reply.session_id, reply.producing_agent_id) == (
        "done",
        "sesn_dm",
        "ag_target",
    )
    scope = admit.await_args.kwargs["channel_id"]
    assert scope.startswith("dm:") and scope != row.thread_id, "a new scope, no history"
    assert admit.await_args.kwargs["dm_agent_name"] == "target-agent"
    kwargs = run.await_args.kwargs
    assert kwargs["prompt"] == "seed text" and kwargs["watch_row"] is None


async def test_a_timer_whose_agent_changed_does_not_run(
    db_session_factory: async_sessionmaker, monkeypatch
):
    tenant, account, row = await seed(db_session_factory)
    _admit, _bind, run = patch_world(monkeypatch, admission_for(tenant, account.id, "ag_other"))

    with pytest.raises(DirectMessageAccessRevoked, match="agent changed") as caught:
        await run_dm_timer(deps_for(None, db_session_factory), row, prompt="seed text")

    assert caught.value.reason == "invoker_not_allowed"
    run.assert_not_awaited()


async def test_a_timer_refuses_a_row_that_is_not_a_timer(
    db_session_factory: async_sessionmaker, monkeypatch
):
    tenant, account, row = await seed(db_session_factory, reason="watch_report")
    admit, _bind, _run = patch_world(monkeypatch, admission_for(tenant, account.id, "ag_target"))

    with pytest.raises(DirectMessageAccessRevoked, match="Not a timer"):
        await run_dm_timer(deps_for(None, db_session_factory), row, prompt="x")

    admit.assert_not_awaited()


async def test_a_timer_does_not_run_when_dms_are_off(
    db_session_factory: async_sessionmaker, monkeypatch
):
    tenant, account, row = await seed(db_session_factory, dm=False)
    admit, _bind, _run = patch_world(monkeypatch, admission_for(tenant, account.id, "ag_target"))

    with pytest.raises(DirectMessageAccessRevoked) as caught:
        await run_dm_timer(deps_for(None, db_session_factory), row, prompt="x")

    assert caught.value.reason == "dm_disabled"
    admit.assert_not_awaited()


async def test_a_timer_waits_while_its_owner_has_a_dm_turn_running(
    db_session_factory: async_sessionmaker, monkeypatch
):
    tenant, account, row = await seed(db_session_factory)
    patch_world(monkeypatch, admission_for(tenant, account.id, "ag_target"))
    deps = deps_for(None, db_session_factory)

    async with owner_dm_turn(deps, platform="discord", owner=OWNER, wait=True):
        with pytest.raises(SessionBusyError):
            await run_dm_timer(deps, row, prompt="x")


@pytest.mark.parametrize(
    ("entry", "reach"), [("routine", True), ("timer", False), ("watch", False)]
)
async def test_only_the_owner_dm_routine_fire_is_given_own_dm_reach(
    db_session_factory: async_sessionmaker, monkeypatch, entry: str, reach: bool
):
    """Watches and timers share the routine's DM path but never reach a limited room, and
    only a routine's run may end with no text (its outbox then posts nothing)."""
    tenant, account, row = await seed(
        db_session_factory, reason="watch_report" if entry == "watch" else "timer"
    )
    admission = admission_for(tenant, account.id, "ag_target")
    patch_world(monkeypatch, admission)
    monkeypatch.setattr(
        direct_messages,
        "bind_session",
        AsyncMock(
            return_value=SimpleNamespace(admission=admission, backend=None, ma_session_id="sesn")
        ),
    )
    unattended = AsyncMock(return_value=SimpleNamespace(text="done"))
    monkeypatch.setattr(direct_messages, "_run_unattended_turn", unattended)
    monkeypatch.setattr(
        "daimon.core.stores.watches.record_delivery_session", AsyncMock(return_value=True)
    )
    deps = deps_for(None, db_session_factory)
    source = DmSource(channel_id="4241", thread_id="4242")

    if entry == "routine":
        await run_dm_routine(
            deps,
            tenant_id=tenant.id,
            owner_user_id=OWNER,
            agent_name="target-agent",
            prompt="p",
            source=None,
            budget_channel_id=None,
            requester_org=RequesterOrg.HOST,
        )
    elif entry == "timer":
        await run_dm_timer(deps, row, prompt="p")
    else:
        await run_dm_watch(
            deps, row, owner_user_id=OWNER, prompt="p", source=source, budget_channel_id="4241"
        )

    assert unattended.await_args.kwargs["own_dm_reach"] is reach
    assert unattended.await_args.kwargs["may_end_empty"] is reach
