"""The channel skills screen: server admins add and remove; anyone else is refused, audited."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
from daimon.adapters.discord.agent_setup.channel_skills_view import (
    AddChannelSkillModal,
    ChannelSkillsView,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 2001
CHANNEL_ID = 900000000000000001
_NOW = dt.datetime(2026, 9, 13, tzinfo=dt.UTC).isoformat()


def _runtime(sessionmaker: object) -> DiscordRuntime:
    title = tenant_scoped_display_title(
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID)),
        name="pdf-tools",
    )
    skill = {
        "id": "skill_lib",
        "created_at": _NOW,
        "display_title": title,
        "latest_version": "v3",
        "source": "custom",
        "type": "skill",
        "updated_at": _NOW,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        rows = [skill] if request.url.path == "/v1/skills" else []
        return httpx.Response(200, json={"data": rows, "next_page": None})

    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_fake_anthropic(handler),
        sessionmaker=sessionmaker,  # type: ignore[arg-type]
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]
    )


def _interaction(*, admin: bool) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.user.guild_permissions.administrator = admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


async def _view(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, Any]:
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    state = PanelState(
        roster=[],
        selected=None,
        account_id=account.id,
        is_admin=True,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        channel_name="growth",
    )
    view = ChannelSkillsView(state, runtime=_runtime(sessionmaker), allowed_user_id=42, rows=[])
    return tenant.id, view


async def test_a_server_admin_adds_then_removes_a_skill(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, view = await _view(db_session_factory)
    modal = AddChannelSkillModal(view)
    modal.skill._value = "pdf-tools"  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_interaction(admin=True))
    async with db_session_factory() as session:
        rows = await list_channel_skills(session, tenant_id=tenant_id, platform="discord")
    assert [(r.channel_id, r.skill_id, r.version) for r in rows] == [
        (str(CHANNEL_ID), "skill_lib", "v3")
    ]

    listed = ChannelSkillsView(view.state, runtime=view.runtime, allowed_user_id=42, rows=rows)
    listed.remove_select._values = ["skill_lib"]  # pyright: ignore[reportPrivateUsage]
    await listed._on_remove(_interaction(admin=True))  # pyright: ignore[reportPrivateUsage]
    async with db_session_factory() as session:
        assert await list_channel_skills(session, tenant_id=tenant_id, platform="discord") == []
        events = await list_events(session, tenant_id=tenant_id)
    assert sorted((e.tool_name, e.outcome) for e in events) == [
        ("panel:channel_skills", "allowed"),
        ("panel:channel_skills", "allowed"),
    ]


async def test_anyone_without_manage_server_is_refused_and_audited(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A channel's own admins hold no Manage Server, so they are refused too."""
    tenant_id, view = await _view(db_session_factory)
    modal = AddChannelSkillModal(view)
    modal.skill._value = "pdf-tools"  # pyright: ignore[reportPrivateUsage]
    member = _interaction(admin=False)
    await modal.on_submit(member)
    member.response.send_message.assert_awaited_once()
    async with db_session_factory() as session:
        assert await list_channel_skills(session, tenant_id=tenant_id, platform="discord") == []
        (event,) = await list_events(session, tenant_id=tenant_id)
    assert (event.tool_name, event.outcome) == ("panel:channel_skills", "denied")


async def test_an_unknown_skill_is_refused_with_its_reason(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, view = await _view(db_session_factory)
    modal = AddChannelSkillModal(view)
    modal.skill._value = "missing"  # pyright: ignore[reportPrivateUsage]
    admin = _interaction(admin=True)
    await modal.on_submit(admin)
    (message,), _ = admin.response.send_message.await_args
    assert "No skill of this workspace" in message
    async with db_session_factory() as session:
        assert await list_channel_skills(session, tenant_id=tenant_id, platform="discord") == []


def test_the_card_names_the_channel_lists_versions_and_says_who_can_change_them() -> None:
    from daimon.adapters.discord.agent_setup.channel_skills_view import (
        build_channel_skills_container,
    )
    from daimon.core.stores.domain import ChannelSkillRow

    row = ChannelSkillRow(
        tenant_id=uuid.uuid4(),
        platform="discord",
        channel_id="555",
        skill_id="skill_1",
        version="v1",
        name="python-helper",
        owner_agent_name=None,
        added_by_account_id=None,
        added_at=dt.datetime(2026, 10, 10, tzinfo=dt.UTC),
    )
    container = build_channel_skills_container([row], channel_name="team-020")
    texts = [i.content for i in container.children if isinstance(i, discord.ui.TextDisplay)]
    assert texts == [
        "## Extra skills in #team-020",
        "`python-helper` (v1)",
        "-# These skills apply only here, from the next message.\n\n"
        "-# Only server admins can change them.",
    ], "no ' · ' separators; the explainer's two lines sit a blank line apart"


def test_the_add_form_says_which_skills_can_be_named() -> None:
    modal = AddChannelSkillModal(MagicMock(state=MagicMock(channel_name="team-020")))
    (label,) = modal.children
    assert isinstance(label, discord.ui.Label)
    assert label.description == "A library skill, or one uploaded to this channel's agent."
