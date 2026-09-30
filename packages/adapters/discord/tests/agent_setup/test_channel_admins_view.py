"""The channel admins screen: what it lists, who may edit, and what a save stores."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.agent_setup.channel_admins_view import (
    EDIT_LABEL,
    ChannelAdminsModal,
    ChannelAdminsView,
    build_channel_admins_container,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_admins import MAX_CHANNEL_ADMIN_IDS
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.domain import ChannelAdminsRow
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 2001
CHANNEL_ID = 900000000000000001


def _row(channel_id: str, *, roles: tuple[str, ...] = (), users: tuple[str, ...] = ()):
    return ChannelAdminsRow(
        tenant_id=uuid.uuid4(),
        platform="discord",
        channel_id=channel_id,
        role_ids=roles,
        user_ids=users,
        updated_by_account_id=None,
        updated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


def _state(*, account_id: uuid.UUID, channel_id: int = CHANNEL_ID) -> PanelState:
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        is_admin=True,
        guild_id=GUILD_ID,
        channel_id=channel_id,
        channel_name="growth",
    )


def _runtime(sessionmaker: object) -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_stub_anthropic(),
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
    return interaction


def _walk(item: Any) -> list[Any]:
    found = [item]
    for child in getattr(item, "children", []) or []:
        found.extend(_walk(child))
    return found


def _text(item: Any) -> str:
    return "\n".join(
        str(node.content) for node in _walk(item) if isinstance(node, discord.ui.TextDisplay)
    )


def _button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any] | None:
    return next(
        (n for n in _walk(view) if isinstance(n, discord.ui.Button) and n.label == label), None
    )


def test_container_lists_each_channels_roles_and_members() -> None:
    text = _text(build_channel_admins_container([_row("500", roles=("77",), users=("88",))]))
    assert "<#500> → <@&77>, <@88>" in text, "roles then members, by mention"
    assert "no channel has its own admins yet" in _text(build_channel_admins_container([])), (
        "an empty tenant says so"
    )


def test_container_stays_inside_discords_text_cap_with_full_grants() -> None:
    ids = tuple(str(10**20 + n) for n in range(MAX_CHANNEL_ADMIN_IDS))
    grants = [_row(str(10**20 + n), roles=ids, users=ids) for n in range(40)]
    text = _text(build_channel_admins_container(grants))
    assert len(text) <= 4000, f"{len(text)} characters exceed Discord's message text cap"
    assert f"+{2 * MAX_CHANNEL_ADMIN_IDS - 5} more" in text, "extra mentions fold per channel"
    assert "-# and 25 more" in text, "channels past the listed ones are counted"


def test_edit_is_offered_only_when_the_panel_has_a_channel(account_id: uuid.UUID) -> None:
    runtime = _runtime(MagicMock())
    with_channel = ChannelAdminsView(
        _state(account_id=account_id), runtime=runtime, allowed_user_id=42, grants=[]
    )
    without = ChannelAdminsView(
        _state(account_id=account_id, channel_id=0), runtime=runtime, allowed_user_id=42, grants=[]
    )
    assert _button(with_channel, EDIT_LABEL) is not None, "edit this panel's channel"
    assert _button(without, EDIT_LABEL) is None, "no channel, nothing to edit"


async def test_edit_rechecks_manage_server_before_opening_the_modal(
    account_id: uuid.UUID,
) -> None:
    view = ChannelAdminsView(
        _state(account_id=account_id), runtime=_runtime(MagicMock()), allowed_user_id=42, grants=[]
    )
    button = _button(view, EDIT_LABEL)
    assert button is not None, "the edit button is offered"
    member = _interaction(admin=False)
    await button.callback(member)
    member.response.send_modal.assert_not_awaited()
    admin = _interaction(admin=True)
    await button.callback(admin)
    admin.response.send_modal.assert_awaited_once()


async def test_modal_saves_then_clears_this_channels_admins(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    view = ChannelAdminsView(
        _state(account_id=account.id),
        runtime=_runtime(db_session_factory),
        allowed_user_id=42,
        grants=[],
    )
    role, everyone = MagicMock(spec=discord.Role), MagicMock(spec=discord.Role)
    role.id, everyone.id = 123456789012345678, GUILD_ID
    role.is_default.return_value, everyone.is_default.return_value = False, True
    user = MagicMock(spec=discord.Member)
    user.id, user.bot = 223456789012345678, False

    modal = ChannelAdminsModal(view)
    modal.roles._values = [role, everyone]  # pyright: ignore[reportPrivateUsage]
    modal.users._values = [user]  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_interaction(admin=True))
    async with db_session_factory() as session:
        rows = await list_channel_admins(session, tenant_id=tenant.id, platform="discord")
    assert [(r.channel_id, r.role_ids, r.user_ids, r.updated_by_account_id) for r in rows] == [
        (str(CHANNEL_ID), (str(role.id),), (str(user.id),), account.id)
    ], "@everyone is never stored as a grant"

    cleared = ChannelAdminsModal(view)
    cleared.roles._values = []  # pyright: ignore[reportPrivateUsage]
    cleared.users._values = []  # pyright: ignore[reportPrivateUsage]
    await cleared.on_submit(_interaction(admin=True))
    async with db_session_factory() as session:
        assert await list_channel_admins(session, tenant_id=tenant.id, platform="discord") == []


async def test_modal_refuses_a_member_and_stores_nothing(account_id: uuid.UUID) -> None:
    sessionmaker = MagicMock()
    view = ChannelAdminsView(
        _state(account_id=account_id), runtime=_runtime(sessionmaker), allowed_user_id=42, grants=[]
    )
    modal = ChannelAdminsModal(view)
    member = _interaction(admin=False)
    await modal.on_submit(member)
    member.response.send_message.assert_awaited_once()
    sessionmaker.begin.assert_not_called()
