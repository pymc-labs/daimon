"""`/model` shows or switches the model a Discord conversation thread runs on."""

from __future__ import annotations

import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import model as model_command
from daimon.adapters.discord.commands.model import ModelCog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core import channel_backend
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.model_switch import SwitchRefusal
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_models import get_thread_effort, get_thread_model
from daimon.core.thread_models import ThreadModelChange
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PARENT_ID = 555
_THREAD_ID = 777


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=MagicMock(),  # pyright: ignore[reportArgumentType]  # /model makes no MA call
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None, membership_resolver=None),
    )


def _interaction(runtime: DiscordRuntime, *, guild_id: int, in_thread: bool = True) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = 1
    user = MagicMock(spec=discord.Member)
    user.id = 42
    user.guild_permissions.administrator = False
    user.guild_permissions.manage_guild = False
    if in_thread:
        channel = MagicMock(spec=discord.Thread)
        channel.id = _THREAD_ID
        channel.parent_id = _PARENT_ID
        channel.parent = MagicMock(spec=discord.TextChannel)
        channel.parent.category_id = None
    else:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = _PARENT_ID
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = guild_id
    interaction.guild = guild
    interaction.user = user
    interaction.channel = channel
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


async def _registered_guild(
    db_session_factory: async_sessionmaker[AsyncSession],
    guild_id: int,
    policy: TenantAccessPolicy | None = None,
) -> None:
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
        if policy is not None:
            await set_access_policy(session, tenant_id=tenant.id, policy=policy)
        await session.commit()


async def _run(interaction: MagicMock, choice: str | None, effort: str | None = None) -> str:
    cog = ModelCog(MagicMock())
    await cog.model.callback(cog, interaction, choice, effort)  # pyright: ignore[reportArgumentType, reportCallIssue]
    interaction.edit_original_response.assert_awaited_once()
    return interaction.edit_original_response.call_args.kwargs["content"]


async def _stored(
    db_session_factory: async_sessionmaker[AsyncSession], guild_id: int
) -> str | None:
    async with db_session_factory() as session:
        return await get_thread_model(
            session,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
            platform="discord",
            thread_id=str(_THREAD_ID),
        )


async def test_a_member_switches_the_thread_and_sees_it_on_the_next_look(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000001
    await _registered_guild(db_session_factory, guild_id)
    runtime = _runtime(db_session_factory)

    assert await _run(_interaction(runtime, guild_id=guild_id), None) == (
        "This conversation runs on the agent's own model."
    )
    assert await _run(_interaction(runtime, guild_id=guild_id), "haiku") == (
        "This conversation runs on Haiku 5.5 from the next reply."
    )
    assert await _stored(db_session_factory, guild_id) == "claude-haiku-5-5"
    assert await _run(_interaction(runtime, guild_id=guild_id), None) == (
        "This conversation runs on Haiku 5.5."
    )


async def test_someone_who_may_not_start_a_turn_there_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000002
    await _registered_guild(
        db_session_factory, guild_id, TenantAccessPolicy(protected_channel_ids=(str(_PARENT_ID),))
    )
    runtime = _runtime(db_session_factory)

    content = await _run(_interaction(runtime, guild_id=guild_id), "opus")

    assert "Only people who can start a conversation here can change its model." in content
    assert await _stored(db_session_factory, guild_id) is None


async def test_outside_a_thread_it_points_at_one(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000003
    await _registered_guild(db_session_factory, guild_id)
    runtime = _runtime(db_session_factory)

    content = await _run(_interaction(runtime, guild_id=guild_id, in_thread=False), "opus")

    assert "Use /model inside a conversation thread or a direct message." in content
    assert await _stored(db_session_factory, guild_id) is None


def _with_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    def enabled(_turn: object) -> bool:
        return True

    monkeypatch.setattr(channel_backend, "backend_selection_enabled", enabled)
    monkeypatch.setattr(model_command, "thread_pick_of", AsyncMock(return_value=MagicMock()))


async def test_a_cross_provider_pick_applies_at_once_and_says_what_is_off(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pick to another provider is kept at once, like a model change: no card, no Switch."""
    guild_id = 720000004
    await _registered_guild(db_session_factory, guild_id)
    runtime = _runtime(db_session_factory)
    _with_selection(monkeypatch)
    note = "Now on GPT-5.\nOff on OpenAI: memory. It comes back when you switch back."
    picked = AsyncMock(return_value=ThreadModelChange(model_id="gpt-5", note=note))
    monkeypatch.setattr(model_command, "pick_thread", picked)
    interaction = _interaction(runtime, guild_id=guild_id)

    cog = ModelCog(MagicMock())
    await cog.model.callback(cog, interaction, "openai:gpt-5")  # pyright: ignore[reportArgumentType, reportCallIssue]

    sent = interaction.edit_original_response.call_args.kwargs
    assert sent["content"] == note, "the change, then what is off, as information"
    assert "view" not in sent, "no Switch button to press"
    picked.assert_awaited_once()


async def test_a_refused_pick_says_why_and_offers_no_confirm(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    guild_id = 720000005
    await _registered_guild(db_session_factory, guild_id)
    runtime = _runtime(db_session_factory)
    _with_selection(monkeypatch)
    refusal = SwitchRefusal("Ask the agent's owner or an admin to move it.", None, "owner")
    monkeypatch.setattr(model_command, "pick_thread", AsyncMock(return_value=refusal))
    interaction = _interaction(runtime, guild_id=guild_id)

    cog = ModelCog(MagicMock())
    await cog.model.callback(cog, interaction, "openai:gpt-5")  # pyright: ignore[reportArgumentType, reportCallIssue]

    sent = interaction.edit_original_response.call_args.kwargs
    assert sent["content"] == "Ask the agent's owner or an admin to move it. Nothing changed.", (
        "the refusal and that nothing changed"
    )
    assert "view" not in sent, "a refusal offers no button"
    assert await _stored(db_session_factory, guild_id) is None


_DM_CHANNEL_ID = 888
_DM_SCOPE = "dm:test-model"


def _dm_interaction(runtime: DiscordRuntime) -> MagicMock:
    user = MagicMock(spec=discord.User)
    user.id = 42
    channel = MagicMock(spec=discord.DMChannel)
    channel.id = _DM_CHANNEL_ID
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = None
    interaction.guild = None
    interaction.user = user
    interaction.channel = channel
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


async def _dm_conversation(
    db_session_factory: async_sessionmaker[AsyncSession], guild_id: int
) -> uuid.UUID:
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=str(_DM_CHANNEL_ID),
            external_user_id="42",
            workspace_id=str(guild_id),
            scope_id=_DM_SCOPE,
        )
        await session.commit()
        return tenant.id


async def test_a_direct_message_switches_its_own_conversation_and_keeps_the_effort(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _dm_conversation(db_session_factory, 720000010)
    runtime = _runtime(db_session_factory)
    key = {"tenant_id": tenant_id, "platform": "discord", "thread_id": _DM_SCOPE}

    assert await _run(_dm_interaction(runtime), None) == (
        "This conversation runs on the agent's own model."
    )
    assert await _run(_dm_interaction(runtime), "fable", "high") == (
        "This conversation runs on Fable 5.2 at high effort from the next reply."
    )
    assert await _run(_dm_interaction(runtime), "opus") == (
        "This conversation runs on Opus 5.5 at high effort from the next reply."
    )
    assert await _run(_dm_interaction(runtime), None, "low") == (
        "This conversation runs on Opus 5.5 at low effort from the next reply."
    )
    async with db_session_factory() as session:
        assert await get_thread_model(session, **key) == "claude-opus-5-5"
        assert await get_thread_effort(session, **key) == "low"


async def test_a_direct_message_with_no_conversation_points_at_one(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(db_session_factory)

    content = await _run(_dm_interaction(runtime), "opus")

    assert "Use /model inside a conversation thread or a direct message." in content


async def test_an_unknown_effort_is_refused_and_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000011
    await _registered_guild(db_session_factory, guild_id)
    runtime = _runtime(db_session_factory)

    content = await _run(_interaction(runtime, guild_id=guild_id), "opus", "turbo")

    assert "isn't an effort this conversation can use" in content
    assert await _stored(db_session_factory, guild_id) is None


async def test_an_effort_the_model_refuses_keeps_the_model_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    guild_id = 720000012
    await _registered_guild(db_session_factory, guild_id)
    _with_selection(monkeypatch)
    monkeypatch.setattr(model_command, "_off_claude", AsyncMock(return_value=False))
    monkeypatch.setattr(model_command, "model_efforts", lambda _model: ("low",))
    picked = AsyncMock()
    monkeypatch.setattr(model_command, "pick_thread", picked)
    interaction = _interaction(_runtime(db_session_factory), guild_id=guild_id)

    content = await _run(interaction, "haiku", "max")

    assert "doesn't take an effort. Nothing changed." in content
    picked.assert_not_awaited()
    assert await _stored(db_session_factory, guild_id) is None


async def test_an_effort_with_a_pick_on_another_provider_is_noted_not_refused(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    guild_id = 720000013
    await _registered_guild(db_session_factory, guild_id)
    _with_selection(monkeypatch)
    monkeypatch.setattr(model_command, "_off_claude", AsyncMock(return_value=False))
    done = ThreadModelChange(model_id="openai:gpt-5", note="Now on GPT-5.")
    monkeypatch.setattr(model_command, "pick_thread", AsyncMock(return_value=done))
    interaction = _interaction(_runtime(db_session_factory), guild_id=guild_id)

    content = await _run(interaction, "openai:gpt-5", "high")

    assert content == "Now on GPT-5. The effort was not set: only Claude models take one."


async def test_an_effort_alone_is_refused_on_an_agent_off_claude(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    guild_id = 720000014
    await _registered_guild(db_session_factory, guild_id)
    _with_selection(monkeypatch)
    monkeypatch.setattr(model_command, "_off_claude", AsyncMock(return_value=True))
    interaction = _interaction(_runtime(db_session_factory), guild_id=guild_id)

    content = await _run(interaction, None, "high")

    assert "this agent doesn't run on Anthropic. Nothing changed." in content
    assert await _stored(db_session_factory, guild_id) is None


async def test_default_with_an_effort_on_an_agent_off_claude_clears_and_notes(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    guild_id = 720000015
    await _registered_guild(db_session_factory, guild_id)
    _with_selection(monkeypatch)
    monkeypatch.setattr(model_command, "_off_claude", AsyncMock(return_value=True))
    interaction = _interaction(_runtime(db_session_factory), guild_id=guild_id)

    content = await _run(interaction, "default", "high")

    assert content.endswith("The effort was not set: this agent doesn't run on Anthropic.")
    async with db_session_factory() as session:
        assert (
            await get_thread_effort(
                session,
                tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
                platform="discord",
                thread_id=str(_THREAD_ID),
            )
            is None
        )


async def test_a_guild_off_the_lane_allowlist_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 720000016
    await _registered_guild(db_session_factory, guild_id)
    interaction = _interaction(_runtime(db_session_factory), guild_id=guild_id)
    bot = MagicMock()
    bot._allows_guild.return_value = False
    cog = ModelCog(bot)

    await cog.model.callback(cog, interaction, "opus", None)  # pyright: ignore[reportArgumentType, reportCallIssue]

    assert (
        "isn't set up to answer in this server"
        in (interaction.edit_original_response.call_args.kwargs["content"])
    )
    assert await _stored(db_session_factory, guild_id) is None
