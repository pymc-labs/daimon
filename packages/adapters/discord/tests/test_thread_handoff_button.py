"""The Hand over button on a thread whose channel now answers with another agent.

Driven through the real `HandOverButton.callback` against real Postgres and an
MA fake: a member's click binds the thread to the channel's agent, so the next
message routes to it; a refused click writes nothing and answers privately.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.feedback_button import FeedbackButton
from daimon.adapters.discord.support_escalation import SupportEscalateButton
from daimon.adapters.discord.thread_handoff import HandOverButton, build_custom_id, hand_over_view
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = "700000009"
_PARENT = 111111111111111111
_THREAD = 222222222222222222
_DEFAULT = DeploymentDefault(agent_name="daimon")


async def _seed(db_session: AsyncSession, *, policy: TenantAccessPolicy | None = None) -> Any:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=_GUILD)
    # The channel now answers with research-bot.
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=str(_PARENT)),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    return tenant


def _interaction(sessionmaker: async_sessionmaker[AsyncSession], tenant_id: Any) -> MagicMock:
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=tenant_id))
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        turn_deps=SimpleNamespace(deployment_default=_DEFAULT),
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD
    thread.parent_id = _PARENT
    thread.send = AsyncMock()
    interaction = MagicMock()
    interaction.client = SimpleNamespace(runtime=runtime)
    interaction.guild_id = int(_GUILD)
    interaction.channel = thread
    interaction.user = SimpleNamespace(id=42, mention="<@42>")
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=True)
    interaction.followup.send = AsyncMock()
    interaction.message.edit = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    return interaction


async def test_a_click_hands_the_thread_to_the_channels_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)

    await HandOverButton(agent_id="ag_research").callback(interaction)

    interaction.channel.send.assert_awaited_once()
    posted = interaction.channel.send.call_args.args[0]
    assert "research-bot answers in this conversation from the next message" in posted
    interaction.message.edit.assert_awaited_once_with(view=None)
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(_PARENT),
            thread_id=str(_THREAD),
        )
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id,
                channel_id=str(_PARENT),
                thread_id=str(_THREAD),
                platform="discord",
            ),
            default=_DEFAULT,
        )
    assert binding is not None and binding.kind == "handoff"
    assert routed.responder_ma_agent_id == "ag_research", "later messages go to research-bot"
    assert routed.thread_binding_kind == "handoff"


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(
        db_session, policy=TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)})
    )
    interaction = _interaction(db_session_factory, tenant.id)

    await HandOverButton(agent_id="ag_research").callback(interaction)

    interaction.channel.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert "only runs in other channels" in interaction.followup.send.call_args.args[0]
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(_PARENT),
            thread_id=str(_THREAD),
        )
    assert binding is None


async def test_the_button_carries_the_agent_and_its_template_overlaps_no_other() -> None:
    view = hand_over_view(agent_id="agent_011CSabc", agent_name="research-bot")
    (button,) = view.children
    custom_id = build_custom_id("agent_011CSabc")
    assert getattr(button, "custom_id", None) == custom_id
    assert HandOverButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    for other in (FeedbackButton, SupportEscalateButton):
        assert other.__discord_ui_compiled_template__.fullmatch(custom_id) is None
