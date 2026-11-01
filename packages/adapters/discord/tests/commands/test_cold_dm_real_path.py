"""Private transport regressions through real admission, claims, sessions and provider I/O."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.discord.commands import direct_messages as module
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.direct_messages import get_conversation, set_dm_agent_name, set_dm_enabled
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_queue import TurnQueue
from daimon.testing.factories import make_ledger_entry, make_tenant, make_tenant_config
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.turn_fakes import deps_for
from daimon.testing.turn_router import turn_events
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_memory_store_handler,
    not_found_response,
    sse_response,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from .test_cold_direct_messages import interaction, message, selector


class _Provider:
    def __init__(self, agents, environments):
        self.agents = {agent.id: agent for agent in agents}
        self.unavailable = None
        self.submissions = []
        self.streams = []
        self.on_stream = None
        self.submission_ready = asyncio.Condition()
        self.state = FakeSessionsState(FakeMAState())
        self.state.ma.agents.update({agent.id: agent.model_dump(mode="json") for agent in agents})
        roster = MARouter()
        roster.add_agent_list(*agents)
        roster.add_environment_list(*environments)
        for env in environments:
            roster.add_environment(env)
        sessions = make_fake_sessions_handler(self.state)
        memory = make_fake_memory_store_handler()

        async def dispatch(request):
            path = request.url.path
            if request.method == "GET" and path.startswith("/v1/agents/"):
                agent_id = path.rsplit("/", 1)[-1]
                if self.unavailable == "deleted":
                    return not_found_response("deleted")
                agent = self.agents[agent_id]
                if self.unavailable == "archived":
                    agent = agent.model_copy(update={"archived_at": datetime.now(UTC)})
                elif self.unavailable == "foreign":
                    agent = agent.model_copy(
                        update={
                            "metadata": {
                                **agent.metadata,
                                "daimon_tenant": agents[-1].metadata["daimon_tenant"],
                            }
                        }
                    )
                return httpx.Response(200, json=agent.model_dump(mode="json"))
            if request.method == "POST" and path.endswith("/events"):
                async with self.submission_ready:
                    self.submissions.append(json.loads(request.content))
                    self.submission_ready.notify_all()
            if request.method == "GET" and path.endswith("/events/stream"):
                sid = path.split("/")[-3]
                self.streams.append(sid)
                turn_number = len(self.streams)
                provider = self

                class AnswerStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        # The driver opens SSE before posting the user event.
                        # Revoke only after a real model submission was accepted.
                        async with provider.submission_ready:
                            await asyncio.wait_for(
                                provider.submission_ready.wait_for(
                                    lambda: len(provider.submissions) >= turn_number
                                ),
                                timeout=10,
                            )
                        if provider.on_stream is not None:
                            await provider.on_stream()
                        events = turn_events(
                            agent_text="private answer", event_id_suffix=f"_{turn_number}"
                        )
                        provider.state.events.setdefault(sid, []).extend(events)
                        yield sse_response(events).content

                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=AnswerStream()
                )
            return combine_handlers(sessions, memory, roster.dispatch)(request)

        self.client = build_fake_anthropic(dispatch)


@pytest.fixture
async def real_dm(db_engine, db_clean):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    tenants, agents, environments = [], [], []
    async with factory.begin() as session:
        for index in range(2):
            tenant = await make_tenant(session, workspace_id=str(700000101 + index))
            await make_tenant_config(
                session, tenant=tenant, agent_name="daimon", environment_name="default"
            )
            await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("100"))
            await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
            await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="daimon")
            tenants.append(tenant)
            agents.append(ma_agent(id=f"ag_private_{index}", name="daimon", tenant_id=tenant.id))
            environments.append(
                ma_environment(id=f"env_private_{index}", name="default", tenant_id=tenant.id)
            )
    provider = _Provider(agents, environments)
    fetch = AsyncMock(return_value=Membership(is_member=True, role_ids=("123",)))
    bot = MagicMock()
    bot.draining = False
    bot.runtime.sessionmaker = factory
    bot.runtime.settings = SimpleNamespace(
        direct_message_policies={},
        ops=SimpleNamespace(alert_webhook_url=None),
        table_rendering={},
    )
    bot.runtime.turn_deps = replace(
        deps_for(provider.client, factory),
        membership_resolver=CachedMembershipResolver(fetch),
        turn_path="legacy",
    )
    bot.get_guild.side_effect = lambda guild_id: SimpleNamespace(name=f"Server {guild_id}")
    bot.turn_queue = TurnQueue(
        platform="discord", global_cap=2, max_queued_per_tenant=2, max_queued=10, max_wait_s=1
    )
    cog = module.DirectMessageCog(bot)
    # Enable the second server only in tests that need a chooser.
    async with factory.begin() as session:
        await set_dm_agent_name(session, tenant_id=tenants[1].id, agent_name=None)
    harness = SimpleNamespace(
        cog=cog, provider=provider, factory=factory, tenants=tenants, fetch=fetch
    )
    try:
        yield harness
    finally:
        await cog.cog_unload()
        await provider.client.close()


async def active(harness):
    async with harness.factory() as session:
        return await get_conversation(
            session, platform="discord", route_key="555", external_user_id="999"
        )


def answers(msg):
    """Answers the person saw: a text send, or the progress card edited into the answer."""
    sent = [
        call.args[0]
        for call in msg.channel.send.await_args_list
        if call.args and call.args[0] == "private answer"
    ]
    card = msg.channel.send.return_value
    edited = [
        call.kwargs["content"]
        for call in getattr(getattr(card, "edit", None), "await_args_list", [])
        if call.kwargs.get("content") == "private answer"
    ]
    return sent + edited


def model_prompts(provider):
    return [
        block["text"]
        for batch in provider.submissions
        for event in batch["events"]
        if event["type"] == "user.message"
        for block in event["content"]
        if block["type"] == "text"
    ]


async def test_different_simultaneous_first_messages_each_answer_once_in_one_scope(real_dm):
    first, second = message(101), message(102)
    first.content, second.content = "first message", "second message"
    await asyncio.gather(real_dm.cog.on_message(first), real_dm.cog.on_message(second))
    row = await active(real_dm)
    assert len(real_dm.provider.submissions) == 2
    prompts = model_prompts(real_dm.provider)
    assert sum(prompt.endswith("\n\nfirst message") for prompt in prompts) == 1
    assert sum(prompt.endswith("\n\nsecond message") for prompt in prompts) == 1
    assert len(set(real_dm.provider.streams)) == 1, "both turns use the one new private session"
    assert answers(first) == ["private answer"] and answers(second) == ["private answer"]
    assert [entry["text"] for entry in row.history if entry["role"] == "user"] == [
        "first message",
        "second message",
    ]
    assert set(row.recent_message_ids) == {"101", "102"}
    async with real_dm.factory() as session:
        scopes = (
            (await session.execute(text("SELECT scope_id FROM direct_message_conversations")))
            .scalars()
            .all()
        )
    assert scopes == [row.scope_id]


async def test_duplicate_gateway_delivery_submits_and_answers_once(real_dm):
    first, duplicate = message(101), message(101)
    await asyncio.gather(real_dm.cog.on_message(first), real_dm.cog.on_message(duplicate))
    row = await active(real_dm)
    assert len(real_dm.provider.submissions) == 1
    assert len(answers(first)) + len(answers(duplicate)) == 1
    assert len(row.history) == 2 and row.recent_message_ids == ["101"]


async def test_concurrent_selector_clicks_submit_original_once(real_dm):
    async with real_dm.factory.begin() as session:
        await set_dm_agent_name(session, tenant_id=real_dm.tenants[1].id, agent_name="daimon")
    msg = message()
    await real_dm.cog.on_message(msg)
    view = selector(msg)
    await asyncio.gather(
        view.choose(interaction(), str(real_dm.tenants[0].id)),
        view.choose(interaction(), str(real_dm.tenants[1].id)),
    )
    row = await active(real_dm)
    assert len(real_dm.provider.submissions) == 1
    assert answers(msg) == ["private answer"]
    assert row.tenant_id == real_dm.tenants[0].id and len(row.history) == 2
    assert row.history[0]["text"] == msg.content


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("restriction", ["pin", "isolation"])
async def test_policy_changed_during_real_turn_suppresses_transport(real_dm, admin, restriction):
    real_dm.fetch.return_value = Membership(is_member=True, is_admin=admin)

    async def revoke():
        policy = TenantAccessPolicy(
            agent_rules={"daimon": AgentRule(runs_in=("123",))},
            channel_rules={"123": ChannelRule(readers="own", writers="own")}
            if restriction == "isolation"
            else {},
        )
        async with real_dm.factory.begin() as session:
            await set_access_policy(session, tenant_id=real_dm.tenants[0].id, policy=policy)

    real_dm.provider.on_stream = revoke
    msg = message()
    with capture_logs() as logs:
        await real_dm.cog.on_message(msg)
    reason = "delivery_own_agents_only" if restriction == "isolation" else "delivery_runs_elsewhere"
    assert any(
        entry.get("event") == "discord.dm.suppressed" and entry.get("reason") == reason
        for entry in logs
    )
    assert len(real_dm.provider.submissions) == 1
    assert not answers(msg)
    assert not any(call.args[0] == "private answer" for call in msg.channel.send.await_args_list)


@pytest.mark.parametrize("unavailable", ["archived", "deleted", "foreign", "policy_unreadable"])
async def test_active_route_unavailable_authority_is_silent_no_model_no_rediscovery(
    real_dm, unavailable
):
    await real_dm.cog.on_message(message())
    before = await active(real_dm)
    assert len(real_dm.provider.submissions) == 1
    # Another eligible tenant must never be discovered as a fallback.
    async with real_dm.factory.begin() as session:
        await set_dm_agent_name(session, tenant_id=real_dm.tenants[1].id, agent_name="daimon")
        if unavailable == "policy_unreadable":
            await session.execute(
                text(
                    "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:id, CAST(:policy AS jsonb))"
                ),
                {"id": before.tenant_id, "policy": '{"unrecognized": true}'},
            )
    real_dm.provider.unavailable = unavailable
    msg = message(102)
    with capture_logs() as logs:
        await real_dm.cog.on_message(msg)
    reason = "policy_unreadable" if unavailable == "policy_unreadable" else "agent_unavailable"
    assert any(
        entry.get("event") == "discord.dm.suppressed" and entry.get("reason") == reason
        for entry in logs
    )
    msg.channel.send.assert_not_awaited()
    assert len(real_dm.provider.submissions) == 1
    assert (await active(real_dm)).scope_id == before.scope_id
    assert len(real_dm.provider.state.sessions) == 1


async def test_moved_source_sealed_during_turn_is_quarantined_without_private_delivery(real_dm):
    from daimon.core.direct_messages import start_dm
    from daimon.core.handoff_context import TranscriptTurn
    from daimon.core.stores.domain import Role
    from daimon.core.turn.admission import admit

    tenant = real_dm.tenants[0]
    deps = real_dm.cog.bot.runtime.turn_deps
    admission = await admit(
        deps,
        origin="chat",
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="999",
        channel_id="123",
        now=datetime.now(UTC),
        role=Role.USER,
        verified_membership=True,
        is_dm=True,
        dm_source_channel_id="123",
        requester_org=RequesterOrg.HOST,
    )
    await start_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        workspace_id=tenant.external_id,
        route_key="555",
        channel_id="555",
        external_user_id="999",
        source_url="https://discord.com/source",
        source_channel_id="123",
        source_thread_id=None,
        context=[TranscriptTurn(role="user", text="source detail")],
    )

    async def seal_source():
        async with real_dm.factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=tenant.id,
                policy=TenantAccessPolicy(channel_rules={"123": ChannelRule(readers="inside")}),
            )

    real_dm.provider.on_stream = seal_source
    msg = message()
    await real_dm.cog.on_message(msg)
    msg.channel.send.assert_not_awaited()
    assert len(real_dm.provider.submissions) == 1
    assert await active(real_dm) is None
    assert all(
        session.archived_at is not None for session in real_dm.provider.state.sessions.values()
    )
