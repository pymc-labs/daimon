"""The real Discord caller's identity lifetime at the preparation boundary."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import bot as bot_module
from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendService, BackendActor, load_backend_agent
from daimon.core.neutral_resources import AgentFleetGrant, WorkerLimits
from daimon.core.stores.turn_card_intents import get_turn_card_intent
from daimon.core.stores.turn_origins import list_active_origins
from daimon.core.turn import admission as admission_module
from daimon.core.turn import prepare as prepare_module
from daimon.core.turn import runtime_manifest
from daimon.core.turn_origin import current_origin_id
from daimon.testing import ma_agent, ma_session
from daimon.testing.factories import make_account
from mux.contracts.ids import ModelRef
from mux.profiles import backend_selection

from .conftest import make_tenant
from .harness import make_bot
from .test_orchestration import (
    _make_channel_message,
    _make_runtime,
    _setup_workspace_and_config,
    _stub_resolved_config,
)


class BoundaryFailure(Exception):
    pass


@pytest.mark.parametrize(
    ("enabled", "granted", "failure"),
    [
        (False, True, "bind"),
        (True, False, "bind"),
        (True, True, "bind"),
        (True, True, "cancel"),
        (True, True, "run"),
    ],
)
async def test_real_caller_binds_acknowledged_identity_and_unwinds_once(
    db_session, db_session_factory, monkeypatch, enabled, granted, failure
):
    tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
    await _setup_workspace_and_config(db_session, tenant.id)
    runtime = _make_runtime(tenant.id, db_session_factory)
    runtime = replace(runtime, turn_deps=replace(runtime.turn_deps, fleet_enabled=enabled))
    native = ma_agent(id="ag_test", name="test-agent", tenant_id=str(tenant.id))
    runtime.anthropic.beta.agents.retrieve.return_value = native
    monkeypatch.setattr(
        admission_module, "resolve_config", AsyncMock(return_value=_stub_resolved_config())
    )
    monkeypatch.setattr(admission_module, "resolve_agent", AsyncMock(return_value=native.id))
    monkeypatch.setattr(admission_module, "resolve_environment", AsyncMock(return_value="env_test"))
    monkeypatch.setattr(
        prepare_module, "create_session", AsyncMock(return_value=ma_session(id="sess-origin"))
    )
    if granted:
        account = await make_account(db_session, tenant=tenant)
        source = await load_backend_agent(
            runtime.anthropic,
            db_session_factory,
            tenant_id=tenant.id,
            agent_name=native.name,
            actual_agent=native,
        )
        value = AgentFleetGrant(
            max_workers=2,
            choices=(
                (
                    backend_selection("anthropic"),
                    ModelRef(provider="anthropic", id=native.model.id),
                ),
            ),
            limits=WorkerLimits(
                budget_usd=Decimal("1"),
                deadline=datetime.now(UTC) + timedelta(minutes=10),
                max_turns=3,
            ),
            max_run_seconds=120,
            max_worker_seconds=120,
            max_root_turns=3,
        )
        service = AgentBackendService(db_session_factory, enabled=True)
        preview = await service.preview_fleet_grant(source, value, now=datetime.now(UTC))
        await service.set_fleet_grant(
            source,
            value,
            0,
            BackendActor(tenant.id, account.id, Subject(is_admin=True)),
            expected_preview_digest=preview.digest,
            now=datetime.now(UTC),
        )
    read = AsyncMock(wraps=runtime_manifest.read_agent_fleet_grant)
    monkeypatch.setattr(runtime_manifest, "read_agent_fleet_grant", read)
    bot = make_bot(runtime)
    message = _make_channel_message()
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 9999, 789
    thread.send = AsyncMock(return_value=type("Post", (), {"id": 1000, "edit": AsyncMock()})())
    message.create_thread.return_value = thread
    original_bind = bot_module.bind_session
    captured = []

    async def bind(deps, admission, **kwargs):
        origin_id = current_origin_id.get()
        early = enabled and granted
        assert (origin_id is not None) is early
        assert ("turn_card_intent_id" in kwargs) is early
        async with db_session_factory() as db:
            rows = await list_active_origins(
                db,
                tenant_id=tenant.id,
                account_id=admission.account_id,
                platform="discord",
                now=datetime.now(UTC),
            )
            assert len(rows) == int(early)
            if early:
                intent = await get_turn_card_intent(db, id=kwargs["turn_card_intent_id"])
                assert intent is not None
                assert (intent.tenant_id, intent.platform, intent.thread_id) == (
                    tenant.id,
                    "discord",
                    "9999",
                )
                assert rows[0].id == origin_id
                assert (rows[0].account_id, rows[0].responder_ma_agent_id) == (
                    admission.account_id,
                    native.id,
                )
                captured.append((origin_id, intent.id))
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "bind":
            raise BoundaryFailure
        # Use ordinary preparation to reach the real run caller. Funding and
        # counted root dispatch remain disabled in this slice.
        kwargs.pop("turn_card_intent_id")
        return await original_bind(replace(deps, fleet_enabled=False), admission, **kwargs)

    async def run(*args, **kwargs):
        assert current_origin_id.get() == captured[0][0]
        assert kwargs["turn_card_intent_id"] == captured[0][1]
        async with db_session_factory() as db:
            rows = await list_active_origins(
                db,
                tenant_id=tenant.id,
                account_id=bind_call.call_args.args[1].account_id,
                platform="discord",
                now=datetime.now(UTC),
            )
            assert [row.id for row in rows] == [captured[0][0]]
        raise BoundaryFailure

    bind_call = AsyncMock(side_effect=bind)
    run_call = AsyncMock(side_effect=run)
    monkeypatch.setattr(bot_module, "bind_session", bind_call)
    monkeypatch.setattr(bot_module, "run_prepared_turn", run_call)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else BoundaryFailure):
        await bot._orchestrate(message, "123456", tenant.id)
    bind_call.assert_awaited_once()
    assert run_call.await_count == int(failure == "run")
    assert read.await_count == int(enabled)
    assert current_origin_id.get() is None
    async with db_session_factory() as db:
        assert (
            await list_active_origins(
                db,
                tenant_id=tenant.id,
                account_id=bind_call.call_args.args[1].account_id,
                platform="discord",
                now=datetime.now(UTC),
            )
            == []
        )
