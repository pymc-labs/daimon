"""Cold DM discovery and selection use fake Discord I/O and the real routing store."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import direct_messages as module
from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import AgentRef, Place, Surface, build_subject
from daimon.core.config import DirectMessagePolicy
from daimon.core.direct_messages import DirectMessageReply
from daimon.core.errors import DaimonError
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.scope import ResolvedConfig
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.direct_messages import get_conversation, set_dm_agent_name, set_dm_enabled
from daimon.core.stores.tenants import set_provision_status
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_queue import TurnQueue
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for


def assert_generic_guidance(msg, tenants):
    msg.channel.send.assert_awaited_once()
    notice = msg.channel.send.await_args.args[0]
    assert notice == module.cold_dm_text("discord", dms_on=None)
    for tenant in tenants:
        assert str(tenant.id) not in notice
        assert tenant.external_id not in notice
    assert "private-0" not in notice


def private_reply(row, text="private answer"):
    return DirectMessageReply(
        text=text,
        conversation=row,
        grant=AdmissionGrant(
            tenant_id=row.tenant_id,
            subject=build_subject(
                is_admin=False, platform_user_id=row.external_user_id, verified_membership=True
            ),
            surface=Surface.DM,
            turn_place=Place(),
            agent=AgentRef.of("private"),
            run_place=Place(),
            channel_id=row.channel_id,
            thread_id=row.scope_id,
            is_dm=True,
            cold_dm=row.is_cold,
            human_invocation=True,
        ),
    )


def message(message_id=101):
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.author = SimpleNamespace(id=999, bot=False)
    msg.channel = MagicMock(spec=discord.DMChannel)
    msg.channel.id = 555
    msg.channel.send = AsyncMock(return_value=SimpleNamespace(id=888, edit=AsyncMock()))
    msg.content = "original private question"
    return msg


async def setup(db_session_factory, monkeypatch, count=1):
    tenants = []
    admissions = {}
    async with db_session_factory.begin() as session:
        for index in range(count):
            tenant = await make_tenant(session, workspace_id=str(700000101 + index))
            account = await make_account(session, tenant=tenant)
            await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
            await set_dm_agent_name(session, tenant_id=tenant.id, agent_name=f"private-{index}")
            tenants.append(tenant)
            admissions[tenant.id] = Admission(
                account_id=account.id,
                agent=ma_agent(id=f"ag_{index}", name=f"private-{index}", tenant_id=tenant.id),
                environment=ma_environment(id="env_1", name="default", tenant_id=tenant.id),
                config=ResolvedConfig(agent_name=f"private-{index}", environment_name="default"),
            )
    fetch = AsyncMock(return_value=Membership(is_member=True, role_ids=("123",)))
    resolver = CachedMembershipResolver(fetch)
    bot = MagicMock()
    bot.draining = False
    bot.runtime.sessionmaker = db_session_factory
    bot.runtime.settings = SimpleNamespace(
        direct_message_policies={},
        ops=SimpleNamespace(alert_webhook_url=None),
        discord=None,
        table_rendering={},
    )
    bot.runtime.turn_deps = replace(
        deps_for(None, db_session_factory), membership_resolver=resolver
    )
    bot.get_guild.side_effect = lambda guild_id: SimpleNamespace(name=f"Server {guild_id}")
    bot.turn_queue = TurnQueue(
        platform="discord", global_cap=2, max_queued_per_tenant=2, max_queued=10, max_wait_s=1
    )

    async def admit(deps, **kwargs):
        return admissions[kwargs["tenant_id"]]

    monkeypatch.setattr(module, "admit_cold_dm", admit)

    async def answer(deps, **kwargs):
        row = await conversation(db_session_factory)
        return private_reply(row)

    reply = AsyncMock(side_effect=answer)
    monkeypatch.setattr(module, "reply_to_dm_result", reply)
    return DirectMessageCog(bot), tenants, fetch, reply


async def conversation(factory):
    async with factory() as session:
        return await get_conversation(
            session, platform="discord", route_key="555", external_user_id="999"
        )


def selector(msg):
    return next(
        call.kwargs["view"]
        for call in msg.channel.send.await_args_list
        if "view" in call.kwargs and isinstance(call.kwargs["view"], module.ServerSelector)
    )


def interaction(selector_id=888, user=999, route=555, channel=None):
    it = MagicMock(spec=discord.Interaction)
    it.id = 7777
    it.user = SimpleNamespace(id=user)
    it.channel = channel if channel is not None else MagicMock(spec=discord.DMChannel)
    it.channel.id = route
    it.channel_id = route
    it.message = SimpleNamespace(id=selector_id)
    it.response.defer = AsyncMock()
    it.response.send_message = AsyncMock()
    it.edit_original_response = AsyncMock()
    it.followup = MagicMock()
    it.followup.send = AsyncMock()
    return it


async def test_one_tenant_answers_original_without_public_history(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    msg = message()
    await cog.on_message(msg)
    row = await conversation(db_session_factory)
    assert row.tenant_id == tenants[0].id
    assert row.context == "" and row.history == []
    assert row.source_channel_id is None and row.source_thread_id is None
    assert reply.await_args.kwargs["text"] == msg.content
    assert reply.await_args.kwargs["verified_membership"] is True
    assert fetch.await_count >= 2
    assert "private-0" in msg.channel.send.await_args_list[0].args[0]
    assert "/dm-settings" in msg.channel.send.await_args_list[0].args[0]
    assert isinstance(msg.channel.send.await_args_list[0].kwargs["view"], module.ServerSwitch)
    assert "view" not in msg.channel.send.await_args_list[-1].kwargs, (
        "only the opening message carries the controls, never a reply"
    )


async def test_multiple_tenants_pending_messages_are_silent_and_selection_answers_once(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    msg = message()
    await cog.on_message(msg)
    view = selector(msg)
    assert await conversation(db_session_factory) is None
    later = message(102)
    await cog.on_message(later)
    later.channel.send.assert_not_awaited()
    reply.assert_not_awaited()
    assert not await view.interaction_check(interaction(user=111))
    assert not await view.interaction_check(interaction(route=666))
    await view.choose(interaction(selector_id=777), str(tenants[1].id))
    assert await conversation(db_session_factory) is None
    await view.choose(interaction(), str(tenants[1].id))
    row = await conversation(db_session_factory)
    assert row.tenant_id == tenants[1].id
    assert reply.await_count == 1
    assert reply.await_args.kwargs["message_id"] == "101"
    assert reply.await_args.kwargs["text"] == "original private question"
    await view.choose(interaction(), str(tenants[1].id))
    assert reply.await_count == 1
    view._expiry.cancel()
    view.stop()


@pytest.mark.parametrize("facts", [Membership(is_member=False), Membership(is_member=True)])
async def test_non_member_gets_generic_guidance_and_member_without_role_is_told(
    db_session_factory, monkeypatch, facts
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    fetch.return_value = facts
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session, tenant_id=tenants[0].id, policy=TenantAccessPolicy(invoker_role_ids=("123",))
        )
    msg = message()
    await cog.on_message(msg)
    if facts.is_member:
        msg.channel.send.assert_awaited_once()
        assert msg.channel.send.await_args.args[0] == module.dm_permission_text("discord"), (
            "a verified member without the role is told they lack permission"
        )
    else:
        assert_generic_guidance(msg, tenants)
    reply.assert_not_awaited()
    assert await conversation(db_session_factory) is None


async def test_generic_notice_does_not_skip_later_eligible_discovery(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenants[0].id, enabled=False)
    initial = message()
    await cog.on_message(initial)
    assert_generic_guidance(initial, tenants)
    reply.assert_not_awaited()
    cog.bot.get_guild.assert_not_called()
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenants[0].id, enabled=True)
    accepted = message(102)
    await cog.on_message(accepted)
    assert (await conversation(db_session_factory)).tenant_id == tenants[0].id
    reply.assert_awaited_once()
    assert fetch.await_count >= 2
    assert all(
        call.args[0] != module.cold_dm_text("discord", dms_on=None)
        for call in accepted.channel.send.await_args_list
        if call.args
    )


async def test_unknown_prevents_assuming_only_verified_candidate(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    fetch.side_effect = [Membership(is_member=True), Membership(is_member=None)]
    msg = message()
    await cog.on_message(msg)
    assert "retry" in msg.channel.send.await_args.args[0]
    reply.assert_not_awaited()
    assert await conversation(db_session_factory) is None


@pytest.mark.parametrize(
    "change", ["disabled", "archived", "not_ready", "unset", "recipient", "agent"]
)
async def test_ineligible_tenant_gets_only_generic_guidance(
    db_session_factory, monkeypatch, change
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    tenant = tenants[0]
    async with db_session_factory.begin() as session:
        if change == "disabled":
            await set_dm_enabled(session, tenant_id=tenant.id, enabled=False)
        elif change == "unset":
            await set_dm_agent_name(session, tenant_id=tenant.id, agent_name=None)
    if change in {"archived", "not_ready"}:
        await set_provision_status(
            db_session_factory,
            tenant_id=tenant.id,
            archive=change == "archived",
            status="failed" if change == "not_ready" else None,
        )
    if change == "recipient":
        cog.bot.runtime.settings = SimpleNamespace(
            direct_message_policies={tenant.id: DirectMessagePolicy(mode="disabled")}
        )
    if change == "agent":
        monkeypatch.setattr(
            module, "admit_cold_dm", AsyncMock(side_effect=AdmissionDenied(reason="runs_elsewhere"))
        )
    msg = message()
    await cog.on_message(msg)
    assert_generic_guidance(msg, tenants)
    reply.assert_not_awaited()


async def test_selection_rechecks_membership_and_expiry(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    msg = message()
    await cog.on_message(msg)
    view = selector(msg)
    fetch.return_value = Membership(is_member=False)
    await view.choose(interaction(), str(tenants[0].id))
    assert await conversation(db_session_factory) is None
    reply.assert_not_awaited()
    view._expiry.cancel()
    view.stop()
    fetch.return_value = Membership(is_member=True)
    await cog.on_message(message(103))
    pending = cog._pending[(999, 555)]
    pending.expires_at = 0
    await cog.on_message(message(104))
    assert cog._pending[(999, 555)] is not pending
    for call in msg.channel.send.await_args_list:
        if isinstance(call.kwargs.get("view"), module.ServerSelector):
            call.kwargs["view"]._expiry.cancel()
            call.kwargs["view"].stop()


async def test_revoked_active_conversation_never_discovers_other_tenant(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    # Start directly in the first tenant, then remove its membership.
    original = module.list_tenants_by_platform
    monkeypatch.setattr(module, "list_tenants_by_platform", AsyncMock(return_value=[tenants[0]]))
    await cog.on_message(message())
    first = await conversation(db_session_factory)
    monkeypatch.setattr(module, "list_tenants_by_platform", original)
    fetch.return_value = Membership(is_member=False)
    msg = message(102)
    await cog.on_message(msg)
    msg.channel.send.assert_not_awaited()
    assert reply.await_count == 1
    assert (await conversation(db_session_factory)).scope_id == first.scope_id


async def test_switch_creates_empty_scope_without_replaying_text(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    monkeypatch.setattr(module, "list_tenants_by_platform", AsyncMock(return_value=[tenants[0]]))
    msg = message()
    await cog.on_message(msg)
    old = await conversation(db_session_factory)
    switch = msg.channel.send.await_args_list[0].kwargs["view"]
    monkeypatch.setattr(module, "list_tenants_by_platform", AsyncMock(return_value=tenants))
    await switch.children[0].callback(interaction(channel=msg.channel))
    view = selector(msg)
    await view.choose(interaction(), str(tenants[1].id))
    new = await conversation(db_session_factory)
    assert new.scope_id != old.scope_id and new.tenant_id == tenants[1].id
    assert new.context == "" and new.history == []
    assert reply.await_count == 1
    view._expiry.cancel()
    view.stop()


def switch_view(msg):
    return msg.channel.send.await_args_list[0].kwargs["view"]


async def test_new_conversation_button_replaces_scope_in_same_server(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    monkeypatch.setattr(module, "list_tenants_by_platform", AsyncMock(return_value=[tenants[1]]))
    msg = message()
    await cog.on_message(msg)
    old = await conversation(db_session_factory)
    view = switch_view(msg)
    assert [child.label for child in view.children] == ["Switch server", "New conversation"]
    assert not await view.interaction_check(interaction(user=111))
    assert not await view.interaction_check(interaction(route=666))
    # No server picker: the restart never looks at the person's other servers.
    monkeypatch.setattr(module, "list_tenants_by_platform", AsyncMock(return_value=tenants))
    click = interaction(channel=msg.channel)
    await view.children[1].callback(click)
    new = await conversation(db_session_factory)
    assert new.scope_id != old.scope_id and new.tenant_id == tenants[1].id
    assert new.context == "" and new.history == []
    assert click.followup.send.await_args.args[0] == "Started a new conversation."
    assert click.followup.send.await_args.kwargs["ephemeral"] is True
    assert "private-1" in msg.channel.send.await_args.args[0]
    assert not any(
        isinstance(call.kwargs.get("view"), module.ServerSelector)
        for call in msg.channel.send.await_args_list
    )
    assert reply.await_count == 1
    await cog.cog_unload()


async def test_stale_new_conversation_button_refuses(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    msg = message()
    await cog.on_message(msg)
    stale = switch_view(msg)
    await stale.children[1].callback(interaction(channel=msg.channel))
    current = await conversation(db_session_factory)
    click = interaction(channel=msg.channel)
    await stale.children[1].callback(click)
    assert "conversation changed" in click.response.send_message.await_args.args[0]
    click.response.defer.assert_not_awaited()
    assert (await conversation(db_session_factory)).scope_id == current.scope_id
    await cog.cog_unload()


@pytest.mark.parametrize("change", ["membership", "disabled", "agent", "recipient"])
async def test_new_conversation_rechecks_access(db_session_factory, monkeypatch, change):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    msg = message()
    await cog.on_message(msg)
    old = await conversation(db_session_factory)
    if change == "membership":
        fetch.return_value = Membership(is_member=False)
    elif change == "recipient":
        cog.bot.runtime.settings = SimpleNamespace(
            direct_message_policies={tenants[0].id: DirectMessagePolicy(mode="disabled")}
        )
    else:
        async with db_session_factory.begin() as session:
            if change == "disabled":
                await set_dm_enabled(session, tenant_id=tenants[0].id, enabled=False)
            else:
                await set_dm_agent_name(session, tenant_id=tenants[0].id, agent_name=None)
    click = interaction(channel=msg.channel)
    await switch_view(msg).children[1].callback(click)
    assert click.followup.send.await_args.args[0] == "Couldn't start a new conversation."
    assert (await conversation(db_session_factory)).scope_id == old.scope_id
    await cog.cog_unload()


async def test_new_command_replaces_current_conversation(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    msg = message()
    await cog.on_message(msg)
    old = await conversation(db_session_factory)
    command = interaction(channel=msg.channel)
    await cog.new.callback(cog, command)
    new = await conversation(db_session_factory)
    assert new.scope_id != old.scope_id and new.tenant_id == old.tenant_id
    assert command.followup.send.await_args.args[0] == "Started a new conversation."
    await cog.cog_unload()


async def test_new_command_outside_dms_or_without_conversation_changes_nothing(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    in_guild = interaction(channel=MagicMock(spec=discord.TextChannel))
    await cog.new.callback(cog, in_guild)
    assert "DMs" in in_guild.response.send_message.await_args.args[0]
    first = interaction()
    await cog.new.callback(cog, first)
    assert "Send a message" in first.response.send_message.await_args.args[0]
    assert await conversation(db_session_factory) is None
    fetch.assert_not_awaited()


def test_rate_bucket_shared_and_refills(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    cog = DirectMessageCog(MagicMock())
    assert [cog._claim_discovery(999) for _ in range(4)] == [True, True, True, False]
    clock[0] += 20
    assert cog._claim_discovery(999)
    assert not cog._claim_discovery(999)
    assert cog._claim_discovery(111)


async def test_expired_selector_requests_new_message_and_cleans_pending(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    msg = message()
    await cog.on_message(msg)
    view = selector(msg)
    cog._pending[(999, 555)].expires_at = 0
    click = interaction()
    await view.choose(click, str(tenants[0].id))
    assert "expired" in click.response.send_message.await_args.args[0]
    reply.assert_not_awaited()
    await view.on_timeout()
    view.stop()
    assert (999, 555) not in cog._pending
    assert "Send a new message" in view.message.edit.await_args.kwargs["content"]


async def test_rate_limit_does_not_limit_active_replies(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    await cog.on_message(message())
    assert cog._claim_discovery(999)
    assert cog._claim_discovery(999)
    assert not cog._claim_discovery(999)
    await cog.on_message(message(102))
    assert reply.await_count == 2


async def test_discovery_rate_limit_is_silent(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    fetch.return_value = Membership(is_member=False)
    for index in range(3):
        await cog.on_message(message(101 + index))
    fetch.reset_mock()
    msg = message(104)
    await cog.on_message(msg)
    fetch.assert_not_awaited()
    msg.channel.send.assert_not_awaited()
    reply.assert_not_awaited()


async def test_selector_pagination_does_not_extend_absolute_expiry():
    view = module.ServerSelector(
        user_id=999,
        route_id=555,
        servers=[(str(index), f"Server {index}") for index in range(26)],
        choose=AsyncMock(),
        expire=MagicMock(),
    )
    expiry = view._expiry.when()
    assert len(view.children[0].options) == 25
    click = interaction()
    click.response.edit_message = AsyncMock()
    await view.children[2].callback(click)
    assert len(view.children[0].options) == 1
    assert view.children[0].options[0].value == "25"
    assert view._expiry.when() == expiry
    view._expiry.cancel()
    view.stop()


async def test_failed_selector_send_does_not_leave_an_unanswerable_pending_choice(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    msg = message()
    msg.channel.send.side_effect = discord.HTTPException(
        MagicMock(status=503, reason="Unavailable"), "retry"
    )
    await cog.on_message(msg)
    assert (999, 555) not in cog._pending
    reply.assert_not_awaited()


@pytest.mark.parametrize(
    "reason",
    [
        "backend_unsupported",
        "balance_depleted",
        "cap_exceeded",
        "channel_budget_exceeded",
        "invoker_not_allowed",
        "writers_none",
        "runs_elsewhere",
        "own_agents_only",
    ],
)
@pytest.mark.parametrize("active_route", [False, True])
async def test_admission_denials_preserve_notices_and_silence_access_failures(
    db_session_factory, monkeypatch, reason, active_route
):
    from structlog.testing import capture_logs

    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    try:
        if active_route:
            await cog.on_message(message())
            reply.side_effect = AdmissionDenied(reason=reason)
        else:
            monkeypatch.setattr(
                module, "admit_cold_dm", AsyncMock(side_effect=AdmissionDenied(reason=reason))
            )
        msg = message(102)
        with capture_logs() as logs:
            await cog.on_message(msg)
        if reason in {
            "invoker_not_allowed",
            "writers_none",
            "runs_elsewhere",
            "own_agents_only",
        }:
            if active_route:
                msg.channel.send.assert_not_awaited()
            elif reason == "invoker_not_allowed":
                msg.channel.send.assert_awaited_once()
                assert msg.channel.send.await_args.args[0] == module.dm_permission_text("discord")
            else:
                assert_generic_guidance(msg, tenants)
            assert any(entry.get("reason") == reason for entry in logs)
        else:
            msg.channel.send.assert_awaited_once()
            notice = msg.channel.send.await_args.args[0]
            if active_route:
                assert notice == module.admission_refusal_message(
                    reason, cog.bot.runtime.settings, in_dm=True
                )
            else:
                assert notice == module._DISCOVERY_FAILURE_NOTICE
                assert "private-0" not in notice and "Server" not in notice
        if not active_route:
            reply.assert_not_awaited()
            assert await conversation(db_session_factory) is None
    finally:
        await cog.cog_unload()


async def test_discovery_operational_failure_stops_before_exposing_other_candidates(
    db_session_factory, monkeypatch
):
    from daimon.core.errors import UserFacingError

    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    admit = AsyncMock(side_effect=UserFacingError("secret candidate or backend details"))
    monkeypatch.setattr(module, "admit_cold_dm", admit)
    try:
        msg = message()
        await cog.on_message(msg)
        msg.channel.send.assert_awaited_once()
        assert msg.channel.send.await_args.args == (module._DISCOVERY_FAILURE_NOTICE,)
        assert not any(msg.channel.send.await_args.kwargs["allowed_mentions"].to_dict().values())
        assert admit.await_count == 1
        reply.assert_not_awaited()
    finally:
        await cog.cog_unload()


@pytest.mark.parametrize("failure", ["resource", "policy"])
async def test_discovery_resource_and_unreadable_policy_get_only_generic_guidance(
    db_session_factory, monkeypatch, failure
):
    from daimon.core.ma_resolver import MAResolverMissError
    from sqlalchemy import text
    from structlog.testing import capture_logs

    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    if failure == "resource":
        monkeypatch.setattr(
            module,
            "admit_cold_dm",
            AsyncMock(
                side_effect=MAResolverMissError(
                    kind="agent", tenant_id=tenants[0].id, daimon_tag="private-0"
                )
            ),
        )
    else:
        async with db_session_factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:id, CAST(:policy AS jsonb))"
                ),
                {"id": tenants[0].id, "policy": '{"unrecognized": true}'},
            )
    try:
        msg = message()
        with capture_logs() as logs:
            await cog.on_message(msg)
        assert_generic_guidance(msg, tenants)
        reply.assert_not_awaited()
        assert any(entry.get("event") == "discord.dm.suppressed" for entry in logs)
    finally:
        await cog.cog_unload()


async def test_selector_recheck_operational_failure_is_generic_and_consumes_selection(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch, count=2)
    try:
        msg = message()
        await cog.on_message(msg)
        view = selector(msg)
        msg.channel.send.reset_mock()
        monkeypatch.setattr(
            module,
            "admit_cold_dm",
            AsyncMock(side_effect=AdmissionDenied(reason="backend_unsupported")),
        )
        await view.choose(interaction(), str(tenants[0].id))
        msg.channel.send.assert_awaited_once()
        assert msg.channel.send.await_args.args == (module._DISCOVERY_FAILURE_NOTICE,)
        assert (999, 555) not in cog._pending
        assert await conversation(db_session_factory) is None
        reply.assert_not_awaited()
    finally:
        await cog.cog_unload()


async def test_dm_turn_posts_approval_cards_in_the_dm(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    hooks = []

    def hook(channel, **kwargs):
        hooks.append((channel, kwargs))
        return AsyncMock()

    monkeypatch.setattr(module, "discord_confirmation_hook", hook)
    msg = message()
    await cog.on_message(msg)
    confirmation_for = reply.await_args.kwargs["confirmation_for"]
    admitted = SimpleNamespace(account_id="acct-1")
    confirmation_for(admitted)
    ((channel, kwargs),) = hooks
    assert channel is msg.channel, "the card is posted in the person's DM"
    assert kwargs["tenant_id"] == tenants[0].id
    assert kwargs["account_id"] == "acct-1"
    assert kwargs["admission"] is admitted


async def test_dm_settings_shows_the_controls_for_the_current_conversation(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    msg = message()
    await cog.on_message(msg)
    row = await conversation(db_session_factory)
    asked = interaction(channel=msg.channel)
    await cog.dm_settings.callback(cog, asked)
    text = asked.response.send_message.await_args.args[0]
    view = asked.response.send_message.await_args.kwargs["view"]
    assert "private-0" in text
    assert isinstance(view, module.ServerSwitch)
    assert [child.label for child in view.children] == ["Switch server", "New conversation"]
    click = interaction(channel=msg.channel)
    await view.children[1].callback(click)
    assert (await conversation(db_session_factory)).scope_id != row.scope_id, (
        "the controls shown by /dm-settings act on the current conversation"
    )
    await cog.cog_unload()


async def test_dm_settings_outside_dms_or_without_conversation_shows_no_controls(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    in_guild = interaction(channel=MagicMock(spec=discord.TextChannel))
    await cog.dm_settings.callback(cog, in_guild)
    assert "in your DMs" in in_guild.response.send_message.await_args.args[0]
    first = interaction()
    await cog.dm_settings.callback(cog, first)
    assert "Send a message to start one" in first.response.send_message.await_args.args[0]
    for asked in (in_guild, first):
        assert "view" not in asked.response.send_message.await_args.kwargs
    await cog.cog_unload()


async def _card_from_turn(cog, msg, reply, admission):
    await cog.on_message(msg)
    card_for = reply.await_args.kwargs["card_for"]
    return card_for(admission, asyncio.Event())


async def test_dm_turn_card_rechecks_delivery_access_before_every_write(
    db_session_factory, monkeypatch
):
    from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
    from daimon.core.direct_messages import DirectMessageAccessRevoked

    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    delivery = AsyncMock()
    monkeypatch.setattr(module, "require_dm_delivery", delivery)
    await cog.on_message(message())
    row_reply = private_reply(await conversation(db_session_factory))
    admission = SimpleNamespace(
        grant=row_reply.grant,
        agent=SimpleNamespace(name="private-0", model=SimpleNamespace(id="claude-test")),
        is_admin=False,
        budget_channel_id=None,
    )
    card = await _card_from_turn(cog, message(102), reply, admission)
    assert isinstance(card, DiscordTurnLifecycle), "a DM turn gets the thread's progress card"
    delivery.reset_mock()
    await card.check_output()
    delivery.assert_awaited_once()
    delivery.side_effect = DirectMessageAccessRevoked("delivery_access_revoked", message="gone")
    with pytest.raises(AdmissionDenied):
        await card.check_output()
    delivery.side_effect = None
    with pytest.raises(AdmissionDenied):
        await card.check_output()
    assert delivery.await_count == 2, "a proved refusal closes the card for the rest of the turn"


async def test_recipient_policy_closes_the_dm_turn_card(db_session_factory, monkeypatch):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    delivery = AsyncMock()
    monkeypatch.setattr(module, "require_dm_delivery", delivery)
    await cog.on_message(message())
    grant = private_reply(await conversation(db_session_factory)).grant
    admission = SimpleNamespace(
        grant=grant,
        agent=SimpleNamespace(name="private-0", model=SimpleNamespace(id="claude-test")),
        is_admin=False,
        budget_channel_id=None,
    )
    card = await _card_from_turn(cog, message(102), reply, admission)
    cog.bot.runtime.settings.direct_message_policies = {
        tenants[0].id: DirectMessagePolicy(mode="disabled")
    }
    delivery.reset_mock()
    with pytest.raises(AdmissionDenied):
        await card.check_output()
    delivery.assert_not_awaited()


class _AnsweredCard:
    def __init__(self, *, answered: bool, ended: bool) -> None:
        self.was_answered = answered
        self.ended = ended

    def set_output_authorization(self, authorization) -> None:
        self.authorization = authorization


@pytest.mark.parametrize("answered", [True, False], ids=["card-answered", "recovered"])
async def test_answer_is_sent_as_text_only_when_the_card_did_not_answer(
    db_session_factory, monkeypatch, answered
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    await cog.on_message(message())
    monkeypatch.setattr(
        module,
        "DiscordTurnLifecycle",
        lambda **kwargs: _AnsweredCard(answered=answered, ended=answered),
    )

    async def answer(deps, **kwargs):
        kwargs["card_for"](
            SimpleNamespace(
                grant=None,
                agent=SimpleNamespace(name="private-0", model=SimpleNamespace(id="m")),
                is_admin=False,
                budget_channel_id=None,
            ),
            asyncio.Event(),
        )
        return private_reply(await conversation(db_session_factory), text="the answer")

    reply.side_effect = answer
    msg = message(102)
    await cog.on_message(msg)
    sent = [call.args[0] for call in msg.channel.send.await_args_list if call.args]
    assert ("the answer" in sent) is not answered, (
        "the card delivers its own answer; text goes out only when it did not"
    )


async def test_failed_turn_adds_no_second_error_under_an_ended_card(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    await cog.on_message(message())
    monkeypatch.setattr(
        module, "DiscordTurnLifecycle", lambda **kwargs: _AnsweredCard(answered=False, ended=True)
    )

    async def fail(deps, **kwargs):
        kwargs["card_for"](
            SimpleNamespace(
                grant=None,
                agent=SimpleNamespace(name="private-0", model=SimpleNamespace(id="m")),
                is_admin=False,
                budget_channel_id=None,
            ),
            asyncio.Event(),
        )
        raise DaimonError("boom")

    reply.side_effect = fail
    msg = message(102)
    await cog.on_message(msg)
    msg.channel.send.assert_not_awaited()


async def test_dm_turn_files_are_swept_into_the_dm_and_gated_by_dm_access(
    db_session_factory, monkeypatch
):
    from daimon.core.direct_messages import DirectMessageAccessRevoked

    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    await cog.on_message(message())

    async def answer(deps, **kwargs):
        row = await conversation(db_session_factory)
        return replace(private_reply(row), session_id="sesn_files")

    reply.side_effect = answer
    delivery = AsyncMock()
    monkeypatch.setattr(module, "require_dm_delivery", delivery)
    msg = message(102)
    await cog.on_message(msg)

    schedule = cog.bot.schedule_dm_output_sweep
    schedule.assert_called_once()
    assert schedule.call_args.args == (msg.channel, "sesn_files")
    may_post = schedule.call_args.kwargs["may_post"]
    assert await may_post() is True
    delivery.side_effect = DirectMessageAccessRevoked("delivery_access_revoked", message="gone")
    assert await may_post() is False, "a revoked grant stops the next file"
    delivery.side_effect = None
    cog.bot.runtime.settings.direct_message_policies = {
        tenants[0].id: DirectMessagePolicy(mode="disabled")
    }
    assert await may_post() is False, "a recipient policy change stops the next file"


async def test_dm_turn_without_a_managed_agents_session_sends_no_files(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    await cog.on_message(message())
    await cog.on_message(message(102))
    cog.bot.schedule_dm_output_sweep.assert_not_called()


async def test_dm_card_edit_drops_the_transport_only_replacement_flag(
    db_session_factory, monkeypatch
):
    cog, tenants, fetch, reply = await setup(db_session_factory, monkeypatch)
    await cog.on_message(message())
    built: dict[str, object] = {}

    def lifecycle(**kwargs):
        built.update(kwargs)
        return _AnsweredCard(answered=True, ended=True)

    monkeypatch.setattr(module, "DiscordTurnLifecycle", lifecycle)

    async def answer(deps, **kwargs):
        kwargs["card_for"](
            SimpleNamespace(
                grant=None,
                agent=SimpleNamespace(name="private-0", model=SimpleNamespace(id="m")),
                is_admin=False,
                budget_channel_id=None,
            ),
            asyncio.Event(),
        )
        return private_reply(await conversation(db_session_factory))

    reply.side_effect = answer
    await cog.on_message(message(102))
    sent = MagicMock()
    sent.edit = AsyncMock()
    await built["edit"](sent, content="x", _allow_replacement=False)
    sent.edit.assert_awaited_once_with(content="x")
