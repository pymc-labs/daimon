"""Self-hosted Discord and Teams participation never consults an Anthropic client."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.discord.thread_participation import ThreadParticipant
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.participation import TeamsParticipation
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import ThreadParticipationSettings
from daimon.core.participation_gates import ParticipationGates
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import mux_state
from daimon.core.thread_participation import ParticipationMode, ResolvedParticipation
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_engine,
    db_schema,
)
from daimon.testing.db import (
    db_session as db_session,
)
from daimon.testing.db import (
    db_session_factory as db_session_factory,
)
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ThreadRef
from mux.contracts.resources import ProviderBinding
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class NoAnthropic:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"selfhosted flow accessed Anthropic.{name}")


@pytest.mark.parametrize("switched", [False, True], ids=["current", "retained_after_switch"])
async def test_selfhosted_discord_and_teams_followed_threads_stay_mention_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], switched: bool
) -> None:
    client = cast(AsyncAnthropic, NoAnthropic())
    settings = ThreadParticipationSettings(mode=ParticipationMode.ON, quiet_seconds=0.01)
    tenants = {}
    for platform in ("discord", "teams"):
        tenant = await make_tenant(db_session, platform=platform, workspace_id=platform)
        tenants[platform] = tenant
        channel = channel_ref(tenant.id, platform, "4200")
        original = await set_channel_backend(
            db_session,
            channel,
            BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna"),
        )
        if switched:
            account = await make_account(db_session, tenant=tenant)
            await make_platform_principal(
                db_session,
                platform=platform,
                external_id="2" if platform == "discord" else "user",
                tenant=tenant,
                account=account,
            )
            await mux_state.put_binding(
                db_session,
                ProviderBinding(
                    id=f"{platform}-selfhosted",
                    thread=ThreadRef(
                        channel=channel, thread_id="4242" if platform == "discord" else "thread"
                    ),
                    provider=original.backend,
                    profile=original.profile,
                    native_refs={},
                    generation=1,
                    config_revision=original.local,
                    legacy_account_id=str(account.id),
                ),
                expected_generation=0,
            )
            await set_channel_backend(db_session, channel, BackendConfig())
    await db_session.commit()

    participant = ThreadParticipant(
        settings=settings,
        sessionmaker=db_session_factory,
        anthropic=client,
        bot_user_id=999,
        bot_display_name="Daimon",
        billing_config=None,
        markup=Decimal("1"),
        deployment_default=DeploymentDefault(),
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 4242, 4200
    thread.history = MagicMock(side_effect=AssertionError("history must not be read"))
    message = cast(
        discord.Message,
        SimpleNamespace(id=1, author=SimpleNamespace(id=2, display_name="user"), content="private"),
    )
    assert not await participant.should_respond(
        thread,
        [message],
        trigger=message,
        tenant_id=tenants["discord"].id,
        resolved=ResolvedParticipation(ParticipationMode.ON, "deployment"),
    )

    gates = ParticipationGates(
        platform="teams",
        settings=settings,
        sessionmaker=db_session_factory,
        anthropic=client,
        bot_display_name="Daimon",
        billing_config=None,
        markup=Decimal("1"),
        deployment_default=DeploymentDefault(),
    )
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    fire = AsyncMock()
    reader = MagicMock(spec=ThreadReader)
    reader.read_window = AsyncMock(side_effect=AssertionError("history must not be read"))
    teams = TeamsParticipation(
        gates=gates,
        settings=settings,
        reader=reader,
        spawn=spawn,
        fire=fire,
        is_busy=lambda _: False,
    )
    inbound = TeamsInbound(
        kind="channel",
        entra_tenant_id="tenant",
        user_id="user",
        conversation_id="thread",
        channel_id="4200",
        activity_id="1",
        text="private",
        service_url=None,
        unprompted=True,
    )
    await teams.observe(inbound, tenants["teams"].id, admitted=AsyncMock(return_value=inbound))
    await asyncio.gather(*tasks)
    reader.read_window.assert_not_awaited()
    fire.assert_not_awaited()


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize("ready", [False, True], ids=["runtime_unavailable", "runtime_ready"])
async def test_selfhosted_timer_dispatch_never_resolves_at_anthropic(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    ready: bool,
) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from daimon.adapters.discord.continuation_dispatch import (
        dispatch_pending_continuations as discord_dispatch,
    )
    from daimon.adapters.slack.continuation_dispatch import (
        dispatch_pending_continuations as slack_dispatch,
    )
    from daimon.core.anthropic_guard import AnthropicDisabled, require_anthropic_allowed
    from daimon.core.continuity.continuation import ContinuationRequest
    from daimon.core.continuity.dispatch import dispatch_pending_continuations as core_dispatch
    from daimon.core.continuity.wakes import enqueue_wake
    from daimon.core.stores.selfhosted_resources import SelfHostedAgents
    from daimon.core.stores.task_continuations import get_continuation
    from daimon.testing.factories import make_account
    from mux.contracts.ids import ModelRef, Scope
    from mux.contracts.resources import AgentSpec

    tenant = await make_tenant(db_session, platform=platform)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(account.id),
        principal_id="test",
        authorization_id="test",
    )
    target = await SelfHostedAgents(db_session_factory).create(
        scope,
        AgentSpec(name="timer-agent", model=ModelRef(provider="selfhosted", id="gpt-6-luna")),
        key="timer-target",
    )
    monkeypatch.setattr("daimon.core.turn.runtimes.ensure_channel_runtime", lambda _: ready)
    now = datetime.now(UTC)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="4200",
        thread_id="4242",
        requester_account_id=account.id,
        requester_external_user_id="user",
        target_ma_agent_id=target.ref.id,
        target_name="timer-agent",
        requested_work="Check the build now",
        reason="timer",
        idempotency_key=uuid4(),
    )
    await enqueue_wake(db_session_factory, request, available_at=now - timedelta(seconds=1))
    notices: list[str] = []
    seeds: list[str] = []

    async def run(_row: object, decision: object) -> None:
        with pytest.raises(AnthropicDisabled):
            require_anthropic_allowed()
        seeds.append(str(decision))

    async def post(text: str | None = None, *, embed: discord.Embed | None = None) -> None:
        notices.append(str(embed.description if embed is not None else text))

    client = cast(AsyncAnthropic, NoAnthropic())
    if platform == "discord":
        thread = MagicMock(spec=discord.Thread)
        thread.id = 4242
        thread.send = AsyncMock(side_effect=post)
        await discord_dispatch(
            db_session_factory,
            client,
            tenant_id=tenant.id,
            thread=thread,
            run_follow_up=run,
            may_post=AsyncMock(return_value=True),
        )
    elif platform == "slack":
        web = MagicMock()
        web.conversations_replies = AsyncMock(return_value={"messages": []})
        web.chat_postMessage = AsyncMock()
        await slack_dispatch(
            db_session_factory,
            client,
            web,
            tenant_id=tenant.id,
            channel="4200",
            thread_id="4242",
            active_turn=False,
            run_follow_up=run,
        )
        notices.extend(str(call) for call in web.chat_postMessage.call_args_list)
    else:
        await core_dispatch(
            db_session_factory,
            client,
            tenant_id=tenant.id,
            platform="teams",
            thread_id="4242",
            run_follow_up=run,
            post_notice=post,
            latest_user_message_at=AsyncMock(return_value=None),
        )
    async with db_session_factory() as session:
        after = await get_continuation(session, idempotency_key=request.idempotency_key)
    assert after is not None
    assert after.status == ("delivered" if ready else "skipped")
    assert bool(seeds) == ready
    if not ready:
        assert after.skip_reason == "blocked_preparation_failed"
        assert "not available yet" in str(notices)
    require_anthropic_allowed()


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_selfhosted_adapter_turn_enters_guard_before_dispatch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: str,
) -> None:
    from daimon.adapters.discord.bot import DaimonBot
    from daimon.adapters.slack.app import SlackApp
    from daimon.adapters.teams.app import TeamsApp
    from daimon.core.anthropic_guard import AnthropicDisabled, require_anthropic_allowed
    from daimon.core.mux_backend import managed_agents

    tenant = await make_tenant(db_session, platform=platform)
    await set_channel_backend(
        db_session,
        channel_ref(tenant.id, platform, "4200"),
        BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna"),
    )
    await db_session.commit()
    client = cast(AsyncAnthropic, NoAnthropic())

    async def probe(*_args: object, **_kwargs: object) -> None:
        with pytest.raises(AnthropicDisabled):
            managed_agents(client)

    app = MagicMock()
    app.runtime = SimpleNamespace(sessionmaker=db_session_factory, anthropic=client)
    if platform == "discord":
        app._orchestrate_observed_guarded = AsyncMock(side_effect=probe)
        message = MagicMock(spec=discord.Message)
        message.channel = SimpleNamespace(id=4200)
        message.author = SimpleNamespace(id=111)
        await DaimonBot._orchestrate_observed(app, message, "guild", tenant.id)
        app._orchestrate_observed_guarded.assert_awaited_once()
    elif platform == "slack":
        app._run_thread_turn_observed_guarded = AsyncMock(side_effect=probe)
        await SlackApp._run_thread_turn_observed(
            app,
            {},
            channel="4200",
            web_client=MagicMock(),
            tenant_id=tenant.id,
            thread_id="4242",
            team_id="team",
        )
        app._run_thread_turn_observed_guarded.assert_awaited_once()
    else:
        app._run_turn_observed_guarded = AsyncMock(side_effect=probe)
        inbound = MagicMock(spec=TeamsInbound)
        inbound.channel_id = "4200"
        inbound.kind = "channel"
        inbound.thread_id = "4242"
        inbound.user_id = "111"
        await TeamsApp._run_turn_observed(
            app,
            inbound,
            tenant.id,
            handoff=None,
            reraise=True,
            continuation=None,
        )
        app._run_turn_observed_guarded.assert_awaited_once()
    require_anthropic_allowed()
