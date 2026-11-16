"""Imported channel timers: held with the channel as parent, run as unattended owner turns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.core import direct_messages
from daimon.core.access_policy import CHANNEL_SCOPE_PREFIX
from daimon.core.authz import AgentRef, Place, Surface, build_subject
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.wakes import enqueue_wake
from daimon.core.continuity.watch_imports import ImportedTimer
from daimon.core.direct_messages import DirectMessageAccessRevoked, run_channel_timer
from daimon.core.scope import ResolvedConfig
from daimon.core.stores.domain import ContinuationReason, Role
from daimon.core.stores.held_timers import (
    import_held_timer,
    settle_held_timers,
    supported,
)
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

OWNER = "555"
CHANNEL = "900"


def imported(place: str, *, channel: str = CHANNEL, source_id: str = "c1") -> ImportedTimer:
    return ImportedTimer(
        source_id=source_id,
        owner_discord_id=OWNER,
        channel_id=channel,
        thread_id=None,
        place_type=place,  # pyright: ignore[reportArgumentType]
        fire_at=datetime.now(UTC) + timedelta(hours=2),
        note="stretch",
    )


@pytest.mark.parametrize(
    ("place", "channel", "expected"),
    [
        ("channel", "900", True),
        ("channel", "general", False),
        ("direct-message", "900", True),
        ("voice", "900", False),
    ],
)
def test_a_channel_timer_needs_a_numeric_channel(place: str, channel: str, expected: bool) -> None:
    assert supported(imported(place, channel=channel)) is expected


async def test_a_confirmed_channel_timer_is_queued_in_its_own_scope_under_its_channel(
    db_session_factory: async_sessionmaker,
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="700000303")
        held = await import_held_timer(
            session, imported("channel"), tenant_id=tenant.id, now=datetime.now(UTC)
        )
    assert held.outcome == "held"

    counts = await settle_held_timers(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id="ag_target",
        agent_name="target-agent",
        now=datetime.now(UTC),
    )

    assert counts["scheduled"] == 1
    async with db_session_factory() as session:
        row = (
            await session.execute(
                text("select parent_channel_id, thread_id, reason from task_continuations")
            )
        ).one()
    assert row.parent_channel_id == CHANNEL and row.reason == "timer"
    assert row.thread_id.startswith(CHANNEL_SCOPE_PREFIX) and row.thread_id != CHANNEL


async def seed(factory, *, reason: ContinuationReason = "timer"):
    async with factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="700000304")
        account = await make_account(session, tenant=tenant)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id=CHANNEL,
        thread_id=f"{CHANNEL_SCOPE_PREFIX}{uuid.uuid4()}",
        requester_account_id=account.id,
        requester_external_user_id=OWNER,
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work="stretch",
        reason=reason,
        idempotency_key=uuid.uuid4(),
    )
    await enqueue_wake(factory, request, available_at=datetime.now(UTC) - timedelta(seconds=1))
    async with factory() as session:
        row = await get_continuation(session, idempotency_key=request.idempotency_key)
    assert row is not None
    return tenant, account, row


def admission_for(tenant, account_id: uuid.UUID, agent_id: str) -> Admission:
    return Admission(
        account_id=account_id,
        agent=ma_agent(id=agent_id, name="target-agent", tenant_id=tenant.id),
        environment=ma_environment(id="env_1", name="default", tenant_id=tenant.id),
        config=ResolvedConfig(agent_name="target-agent", environment_name="default"),
        grant=AdmissionGrant(
            tenant_id=tenant.id,
            subject=build_subject(is_admin=False, platform_user_id=OWNER, verified_membership=True),
            surface=Surface.CHANNEL,
            turn_place=Place(),
            agent=AgentRef.of("target-agent"),
            run_place=Place(),
            channel_id=CHANNEL,
            thread_id=None,
            is_dm=False,
        ),
    )


def patch_world(monkeypatch, admission: Admission):
    admit = AsyncMock(return_value=admission)
    bind = AsyncMock(return_value=SimpleNamespace(admission=admission, backend=None))
    run = AsyncMock(return_value=("done", "sesn_ch", []))
    monkeypatch.setattr(direct_messages, "admit", admit)
    monkeypatch.setattr(direct_messages, "bind_session", bind)
    monkeypatch.setattr(direct_messages, "_run_admitted_dm_turn", run)
    return admit, bind, run


async def test_a_channel_timer_runs_unattended_in_a_fresh_scope_of_its_channel(
    db_session_factory: async_sessionmaker, monkeypatch
) -> None:
    tenant, account, row = await seed(db_session_factory)
    admit, bind, run = patch_world(monkeypatch, admission_for(tenant, account.id, "ag_target"))

    reply = await run_channel_timer(
        deps_for(None, db_session_factory),
        row,
        prompt="seed text",
        role=Role.USER,
        role_ids=("7",),
        category_id="12",
    )

    assert (reply.text, reply.session_id, reply.producing_agent_id) == (
        "done",
        "sesn_ch",
        "ag_target",
    )
    asked = admit.await_args.kwargs
    assert asked["channel_id"] == CHANNEL and asked["external_user_id"] == OWNER
    assert asked["attended"] is False and asked["origin"] == "routine"
    assert asked["role_ids"] == ("7",) and asked["category_id"] == "12"
    assert "thread_id" not in asked and not asked.get("is_dm")
    scope = bind.await_args.kwargs["thread_id"]
    assert scope.startswith(CHANNEL_SCOPE_PREFIX) and scope != row.thread_id
    assert bind.await_args.kwargs["reuse_existing"] is False
    kwargs = run.await_args.kwargs
    assert kwargs["prompt"] == "seed text" and kwargs["parent_channel_id"] == CHANNEL
    assert kwargs["scope_id"] == scope and kwargs["origin"] == "routine"


async def test_a_channel_timer_whose_agent_changed_does_not_run(
    db_session_factory: async_sessionmaker, monkeypatch
) -> None:
    tenant, account, row = await seed(db_session_factory)
    _admit, bind, run = patch_world(monkeypatch, admission_for(tenant, account.id, "ag_other"))

    with pytest.raises(DirectMessageAccessRevoked, match="agent changed") as caught:
        await run_channel_timer(deps_for(None, db_session_factory), row, prompt="x", role=Role.USER)

    assert caught.value.reason == "invoker_not_allowed"
    bind.assert_not_awaited()
    run.assert_not_awaited()


async def test_a_channel_timer_refuses_a_row_that_is_not_a_timer(
    db_session_factory: async_sessionmaker, monkeypatch
) -> None:
    tenant, account, row = await seed(db_session_factory, reason="watch_report")
    admit, _bind, _run = patch_world(monkeypatch, admission_for(tenant, account.id, "ag_target"))

    with pytest.raises(DirectMessageAccessRevoked, match="Not a timer"):
        await run_channel_timer(deps_for(None, db_session_factory), row, prompt="x", role=Role.USER)

    admit.assert_not_awaited()
