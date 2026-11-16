"""`/routines` shows each member their own routines and an admin all of them."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import daimon.adapters.discord.commands.routines as routines_cmd
import discord
import httpx
import pytest
from daimon.adapters.discord.commands.routines import RoutinesCog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import deps_for
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD = 710000100
ME, OTHER = 100000000000000001, 100000000000000002


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "next_page": None})

    settings = MagicMock()
    settings.web = None
    return DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(handle),  # pyright: ignore[reportArgumentType]
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None),
    )


def _interaction(runtime: DiscordRuntime, *, admin: bool) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = OTHER + 1
    user = MagicMock(spec=discord.Member)
    user.id = ME
    user.guild_permissions.administrator = False
    user.guild_permissions.manage_guild = admin
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = GUILD
    interaction.guild = guild
    interaction.user = user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def _open_panel(db_session_factory, monkeypatch, *, admin: bool):
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD))
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="discord", workspace_id=str(GUILD))
        for creator, text in ((str(ME), "mine"), (str(OTHER), "theirs")):
            await create_routine(
                session,
                tenant_id=tenant_id,
                created_by_user_id=creator,
                agent_id="agent_a",
                agent_name="daimon",
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message=text,
            )
    admin_flags: list[bool] = []

    def links(_web, _tenant, *, admin: bool):
        admin_flags.append(admin)

    monkeypatch.setattr(routines_cmd, "panel_links", links)
    runtime = _runtime(db_session_factory)
    interaction = _interaction(runtime, admin=admin)
    cog = RoutinesCog(MagicMock())

    await cog.routines.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    view = interaction.followup.send.call_args.kwargs["view"]
    return [entry.label for entry in view.state.rows], admin_flags


async def test_member_sees_only_their_own_routines(db_session_factory, monkeypatch):
    """A member without admin rights opens the panel and gets only the routines they made."""
    labels, admin_flags = await _open_panel(db_session_factory, monkeypatch, admin=False)

    assert labels == ["mine"], "another member's routine must not load for a member"
    assert admin_flags == [False], "member links must not offer admin pages"


async def test_admin_sees_every_routine(db_session_factory, monkeypatch):
    """An admin opens the same panel and gets every routine in the server."""
    labels, admin_flags = await _open_panel(db_session_factory, monkeypatch, admin=True)

    assert sorted(labels) == ["mine", "theirs"], "an admin sees the server's routines"
    assert admin_flags == [True], "admin links match the role"


@pytest.mark.parametrize("viewer", [None, "999"])
async def test_only_own_loads_nothing_without_a_matching_creator(db_session_factory, viewer):
    """With `only_own`, no viewer or a stranger loads no routine at all."""
    from daimon.adapters.discord.routines_panel.read import load_guild_routines

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD))
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="discord", workspace_id=str(GUILD))
        await create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=str(ME),
            agent_id="agent_a",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="mine",
        )
    async with db_session_factory() as session:
        entries, over_cap, _ = await load_guild_routines(
            session,
            _runtime(db_session_factory).anthropic,
            tenant_id=tenant_id,
            viewer_user_id=viewer,
            only_own=True,
        )

    assert (entries, over_cap) == ([], 0), "no routine loads for a non-creator"


async def _panel_view(db_session_factory, monkeypatch, *, admin: bool):
    """Open the panel for ME, returning the view and the interaction that opened it."""
    monkeypatch.setattr(routines_cmd, "panel_links", lambda *_a, **_k: None)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD))
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="discord", workspace_id=str(GUILD))
        for creator, text in ((str(ME), "mine"), (str(OTHER), "theirs")):
            await create_routine(
                session,
                tenant_id=tenant_id,
                created_by_user_id=creator,
                agent_id="agent_a",
                agent_name="daimon",
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message=text,
            )
    interaction = _interaction(_runtime(db_session_factory), admin=admin)
    cog = RoutinesCog(MagicMock())
    await cog.routines.callback(cog, interaction)  # pyright: ignore[reportArgumentType]
    view = interaction.followup.send.call_args.kwargs["view"]
    click = _interaction(view.runtime, admin=admin)
    click.response.is_done = MagicMock(return_value=False)
    click.response.edit_message = AsyncMock()
    return view, click


def _rerendered_labels(click: MagicMock) -> list[str]:
    return [
        entry.label for entry in click.response.edit_message.call_args.kwargs["view"].state.rows
    ]


@pytest.mark.parametrize(("admin", "expected"), [(False, ["mine"]), (True, ["mine", "theirs"])])
async def test_refresh_keeps_the_panel_scoped_to_the_clicker(
    db_session_factory, monkeypatch, admin, expected
):
    """Refresh re-reads the clicker's role: a member never gains other members' routines."""
    from daimon.adapters.discord.routines_panel.panel import _rerender

    view, click = await _panel_view(db_session_factory, monkeypatch, admin=admin)

    await _rerender(click, view)

    assert sorted(_rerendered_labels(click)) == expected, "the refreshed list matches the role"


async def test_pause_rerenders_only_the_members_own_routines(db_session_factory, monkeypatch):
    """After a member pauses their routine, the redrawn panel still hides everyone else's."""
    from daimon.adapters.discord.routines_panel import panel as panel_mod
    from daimon.core.authz import build_subject

    monkeypatch.setattr(
        panel_mod,
        "resolve_subject",
        AsyncMock(
            return_value=build_subject(
                is_admin=False, platform_user_id=str(ME), verified_membership=True
            )
        ),
    )
    view, click = await _panel_view(db_session_factory, monkeypatch, admin=False)
    pause = next(c for c in view.walk_children() if isinstance(c, panel_mod._PauseButton))

    await pause.callback(click)

    assert _rerendered_labels(click) == ["mine"], "Pause must not widen the list"
    selected = click.response.edit_message.call_args.kwargs["view"].state.selected
    assert selected is not None
    assert not selected.routine.enabled, "the routine was paused"


@pytest.mark.parametrize("membership", [None, False])
async def test_pause_refuses_a_creator_who_is_no_longer_a_member(
    db_session_factory, monkeypatch, membership
):
    """Creating the routine is not enough: the clicker's current membership is read fresh."""
    from daimon.adapters.discord.routines_panel import panel as panel_mod
    from daimon.core.authz import build_subject

    monkeypatch.setattr(
        panel_mod,
        "resolve_subject",
        AsyncMock(
            return_value=build_subject(
                is_admin=False, platform_user_id=str(ME), verified_membership=membership
            )
        ),
    )
    view, click = await _panel_view(db_session_factory, monkeypatch, admin=False)
    pause = next(c for c in view.walk_children() if isinstance(c, panel_mod._PauseButton))
    click.response.send_message = AsyncMock()

    await pause.callback(click)

    click.response.send_message.assert_awaited_once()
    click.response.edit_message.assert_not_awaited()
    assert view.state.selected is not None and view.state.selected.routine.enabled, (
        "the routine stays running"
    )
