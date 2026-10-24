"""A turn may drain another caller's wake without transferring its provider guard."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import httpx
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.slack.app import SlackApp
from daimon.core.anthropic_guard import (
    AnthropicDisabled,
    anthropic_disabled,
    guard_selfhosted_channel,
    require_anthropic_allowed,
)
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import DatabaseSettings, Settings
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.wakes import enqueue_wake
from daimon.core.stores import mux_state
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from pydantic import PostgresDsn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize(
    "deny_first_run", [False, True], ids=["delivered", "guard_denial_deferred"]
)
async def test_turn_tail_drains_other_callers_timer_with_its_own_provider_context(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: Literal["discord", "slack"],
    deny_first_run: bool,
) -> None:
    tenant = await make_tenant(db_session, platform=platform)
    caller_a = await make_account(db_session, tenant=tenant)
    caller_b = await make_account(db_session, tenant=tenant)
    for external_id, account in (("caller-a", caller_a), ("caller-b", caller_b)):
        await make_platform_principal(
            db_session, tenant=tenant, account=account, platform=platform, external_id=external_id
        )
    channel = channel_ref(tenant.id, platform, "4200")
    thread = ThreadRef(channel=channel, thread_id="4242")
    original = await set_channel_backend(db_session, channel, BackendConfig())
    await mux_state.put_binding(
        db_session,
        ProviderBinding(
            id="caller-a-anthropic",
            thread=thread,
            provider=original.backend,
            profile=original.profile,
            native_refs={},
            generation=1,
            config_revision=original.local,
            legacy_account_id=str(caller_a.id),
        ),
        expected_generation=0,
    )
    local = await set_channel_backend(
        db_session,
        channel,
        BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna"),
    )
    await mux_state.put_binding(
        db_session,
        ProviderBinding(
            id="caller-b-selfhosted",
            thread=thread,
            provider=local.backend,
            profile=local.profile,
            native_refs={},
            generation=1,
            config_revision=local.local,
            legacy_account_id=str(caller_b.id),
        ),
        expected_generation=0,
    )
    await db_session.commit()
    now = datetime.now(UTC)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="4200",
        thread_id="4242",
        requester_account_id=caller_a.id,
        requester_external_user_id="caller-a",
        target_ma_agent_id="agt_stats",
        target_name="stats",
        requested_work="Check the build",
        reason="timer",
        idempotency_key=uuid4(),
    )
    await enqueue_wake(db_session_factory, request, available_at=now - timedelta(seconds=1))
    router = MARouter()
    router.add_agent(ma_agent(id="agt_stats", name="stats", tenant_id=tenant.id))
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        require_anthropic_allowed()
        requests.append(request)
        return router.dispatch(request)

    client = build_fake_anthropic(handle)
    thread_lock = asyncio.Lock()
    deliveries: list[str] = []
    tries = 0

    async def run(row: TaskContinuationRow, seed: object, **_kwargs: object) -> None:
        nonlocal tries
        tries += 1
        assert row.requester_account_id == caller_a.id
        assert thread_lock.locked(), "provider isolation must preserve the thread concurrency lock"
        require_anthropic_allowed()
        # Tasks spawned by A's wake must inherit A's context too.
        assert not await asyncio.create_task(asyncio.to_thread(anthropic_disabled))
        if deny_first_run and tries == 1:
            raise AnthropicDisabled("A provider helper refused before the turn started")
        deliveries.append(str(seed))

    runtime = SimpleNamespace(
        sessionmaker=db_session_factory,
        anthropic=client,
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            _env_file=None,  # type: ignore[call-arg]
        ),
        turn_deps=SimpleNamespace(fleet_enabled=False),
    )
    app = MagicMock()
    app.runtime = runtime
    app.draining = False
    app._run_continuation_turn = AsyncMock(side_effect=run)
    app._may_post_in = AsyncMock(return_value=True)
    app._history_page_limit.return_value = 100
    discord_thread = MagicMock(spec=discord.Thread)
    discord_thread.id = 4242
    web_client = MagicMock()
    web_client.conversations_replies = AsyncMock(return_value={"messages": []})

    async def drain() -> None:
        # This is the turn-tail entry point; the originating caller still owns its
        # thread slot and selfhosted provider guard when the queue is drained.
        async with (
            thread_lock,
            guard_selfhosted_channel(
                db_session_factory,
                tenant_id=tenant.id,
                platform=platform,
                channel_id="4200",
                thread_id="4242",
                caller_id="caller-b",
            ) as disabled,
        ):
            assert disabled and anthropic_disabled()
            if platform == "discord":
                await DaimonBot._dispatch_continuations(  # pyright: ignore[reportPrivateUsage]
                    app, tenant_id=tenant.id, thread=discord_thread, guild_id="guild"
                )
            else:
                await SlackApp._dispatch_continuations(  # pyright: ignore[reportPrivateUsage]
                    app,
                    web_client=web_client,
                    tenant_id=tenant.id,
                    channel="4200",
                    thread_id="4242",
                    account_id=caller_b.id,
                    team_id="team",
                )
            assert thread_lock.locked()
            assert anthropic_disabled(), "B's prohibition must be restored after draining A's work"
            with pytest.raises(AnthropicDisabled):
                require_anthropic_allowed()
        assert not anthropic_disabled()

    try:
        await drain()
        if deny_first_run:
            async with db_session_factory() as session:
                pending = await get_continuation(session, idempotency_key=request.idempotency_key)
            assert pending is not None and pending.status == "pending"
            assert pending.skip_reason is None and pending.delivered_at is None
            assert pending.attempts == 0 and pending.started_at is None
            assert pending.lease_owner is None and not deliveries
        async with db_session_factory() as session:
            after = await get_continuation(session, idempotency_key=request.idempotency_key)
        assert after is not None
        if not deny_first_run:
            assert after.status == "delivered" and after.skip_reason is None
            assert len(deliveries) == 1 and "Check the build" in deliveries[0]
            assert len(requests) == 1
        assert not thread_lock.locked() and not anthropic_disabled()
    finally:
        await client.close()
