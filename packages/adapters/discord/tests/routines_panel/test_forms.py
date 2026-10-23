"""Panel create/delete interactions exercise the real routine stores."""

# pyright: reportPrivateUsage=false

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.routines_panel import embeds, forms, panel
from daimon.adapters.discord.routines_panel.state import (
    RoutineEntry,
    RoutinesPanelState,
    derive_state,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.config import RecruitmentSettings, RecruitmentTenantSettings
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_budgets import set_channel_budget
from daimon.core.stores.routines import (
    create_routine,
    delete_routine,
    get_routine,
    list_routines_for_tenant,
)
from daimon.testing.factories import make_tenant


def interaction(*, admin=True, user_id=42):
    result = MagicMock(spec=discord.Interaction)
    result.user = MagicMock(spec=discord.Member)
    result.user.id = user_id
    result.user.guild_permissions.administrator = admin
    result.user.guild_permissions.manage_guild = False
    result.guild.owner_id = 999
    result.guild_id = 123456
    result.channel_id = 987654
    result.channel = MagicMock(spec=discord.TextChannel)
    result.channel.id = result.channel_id
    result.channel.guild.id = result.guild_id
    result.response.send_message = AsyncMock()
    result.response.send_modal = AsyncMock()
    result.response.edit_message = AsyncMock()
    result.response.defer = AsyncMock()
    result.followup.send = AsyncMock()
    return result


@pytest.fixture
async def setup_panel(db_session_factory, monkeypatch, stub_anthropic):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="123456")
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.settings = SimpleNamespace(recruitment=RecruitmentSettings())
    runtime.sessionmaker = db_session_factory
    runtime.anthropic = stub_anthropic
    state = RoutinesPanelState.initial(rows=[], over_cap_count=0, agent_name_map={})
    view = panel.RoutinesPanelView(state, runtime=runtime, allowed_user_id=42)
    refresh = AsyncMock()
    monkeypatch.setattr(panel, "_rerender", refresh)
    resolver = AsyncMock(return_value=SimpleNamespace(id="agent_a", name="Agent", metadata={}))
    monkeypatch.setattr(forms, "find_agent_by_daimon_tag", resolver)
    return view, tenant, refresh, resolver


async def open_modal(view, click):
    button = next(child for child in view.walk_children() if isinstance(child, forms.NewButton))
    await button.callback(click)
    modal = click.response.send_modal.call_args.args[0]
    modal.agent._value = "daimon"
    modal.cron._value = "0 9 * * 1-5"
    modal.timezone._value = "UTC"
    modal.message._value = "daily summary"
    return modal


@pytest.mark.parametrize("grant", ["administrator", "manage_guild", "owner"])
async def test_admin_creates_attributed_scheduled_routine(setup_panel, db_session_factory, grant):
    view, tenant, refresh, _ = setup_panel
    click = interaction(admin=False)
    submit = interaction(admin=False)
    for event in (click, submit):
        if grant == "owner":
            event.guild.owner_id = event.user.id
        else:
            setattr(event.user.guild_permissions, grant, True)
    modal = await open_modal(view, click)
    before = datetime.now(UTC)
    await modal.on_submit(submit)
    async with db_session_factory() as session:
        rows = await list_routines_for_tenant(session, tenant_id=tenant.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.created_by_user_id == "42"
    assert row.next_fire_at is not None and row.next_fire_at > before
    assert row.channel_id == "987654"
    assert row.source_channel_id == "987654"
    assert row.source_thread_id is None
    assert row.source_provenance_known
    assert (row.agent_id, row.agent_name, row.trigger_message) == (
        "agent_a",
        "daimon",
        "daily summary",
    )
    assert row.destination_kind is None
    refresh.assert_awaited_once_with(click, view)
    assert "Routine created" in submit.followup.send.call_args.args[0]


@pytest.mark.parametrize("field,value", [("cron", "bad cron"), ("timezone", "Invalid/Zone")])
async def test_invalid_schedule_writes_nothing(setup_panel, db_session_factory, field, value):
    view, tenant, refresh, resolver = setup_panel
    modal = await open_modal(view, interaction())
    getattr(modal, field)._value = value
    submit = interaction()
    await modal.on_submit(submit)
    submit.response.send_message.assert_awaited_once_with(embeds.INVALID_SCHEDULE, ephemeral=True)
    async with db_session_factory() as session:
        assert await list_routines_for_tenant(session, tenant_id=tenant.id) == []
    resolver.assert_not_awaited()
    refresh.assert_not_awaited()


async def test_non_admin_refused_at_click_and_submit(setup_panel, db_session_factory):
    view, tenant, _, resolver = setup_panel
    button = next(child for child in view.walk_children() if isinstance(child, forms.NewButton))
    click = interaction(admin=False)
    await button.callback(click)
    click.response.send_modal.assert_not_awaited()
    click.response.send_message.assert_awaited_once_with(embeds.ADMIN_ONLY, ephemeral=True)
    modal = await open_modal(view, interaction())
    submit = interaction(admin=False)
    await modal.on_submit(submit)
    submit.response.send_message.assert_awaited_once_with(embeds.ADMIN_ONLY, ephemeral=True)
    resolver.assert_not_awaited()
    async with db_session_factory() as session:
        assert await list_routines_for_tenant(session, tenant_id=tenant.id) == []


@pytest.mark.parametrize("restriction", ["missing_agent", "agent_rule", "channel_home"])
async def test_agent_validation_and_save_policy(setup_panel, db_session_factory, restriction):
    view, tenant, refresh, resolver = setup_panel
    if restriction == "missing_agent":
        resolver.return_value = None
    else:
        async with db_session_factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=tenant.id,
                policy=(
                    TenantAccessPolicy(agent_rules={"daimon": AgentRule(runs_in=("123",))})
                    if restriction == "agent_rule"
                    else TenantAccessPolicy(
                        channel_rules={"987654": ChannelRule(readers="own", writers="own")}
                    )
                ),
            )
    modal = await open_modal(view, interaction())
    submit = interaction()
    await modal.on_submit(submit)
    expected = embeds.AGENT_UNAVAILABLE if restriction == "missing_agent" else embeds.SAVE_REFUSED
    submit.followup.send.assert_awaited_once_with(expected, ephemeral=True)
    async with db_session_factory() as session:
        assert await list_routines_for_tenant(session, tenant_id=tenant.id) == []
    refresh.assert_not_awaited()


async def seed_selected(view, tenant, factory, *, creator="42"):
    async with factory.begin() as session:
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id=creator,
            agent_id="agent_a",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="summary",
        )
    glyph, color = derive_state(row)
    view = panel.RoutinesPanelView(
        RoutinesPanelState.initial(
            rows=[
                RoutineEntry(
                    routine=row, agent_name="daimon", glyph=glyph, color=color, label="summary"
                )
            ],
            over_cap_count=0,
            agent_name_map={},
        ),
        runtime=view.runtime,
        allowed_user_id=42,
    )
    button = next(child for child in view.walk_children() if isinstance(child, forms.DeleteButton))
    return row, view, button


@pytest.mark.parametrize("admin", [True, False])
async def test_delete_requires_confirmation(setup_panel, db_session_factory, admin):
    view, tenant, refresh, _ = setup_panel
    row, view, button = await seed_selected(view, tenant, db_session_factory)
    click = interaction(admin=admin)
    await button.callback(click)
    click.response.defer.assert_awaited_once_with()
    confirm = click.followup.send.call_args.kwargs["view"]
    assert click.followup.send.call_args.args[0] == embeds.DELETE_CONFIRM
    async with db_session_factory() as session:
        assert await get_routine(session, row.id, tenant_id=tenant.id) is not None
    submit = interaction(admin=admin)
    await confirm.confirm.callback(submit)
    async with db_session_factory() as session:
        assert await get_routine(session, row.id, tenant_id=tenant.id) is None
    submit.response.edit_message.assert_awaited_once_with(content=embeds.DELETED, view=None)
    refresh.assert_awaited_once_with(click, view)


async def test_delete_refuses_non_creator_and_rechecks_on_submit(setup_panel, db_session_factory):
    view, tenant, refresh, _ = setup_panel
    row, view, button = await seed_selected(view, tenant, db_session_factory, creator="777")
    outsider = interaction(admin=False)
    await button.callback(outsider)
    outsider.response.send_message.assert_awaited_once_with(embeds.DELETE_REFUSED, ephemeral=True)
    click = interaction()
    await button.callback(click)
    confirm = click.followup.send.call_args.kwargs["view"]
    submit = interaction(admin=False)
    await confirm.confirm.callback(submit)
    submit.response.send_message.assert_awaited_once_with(embeds.DELETE_REFUSED, ephemeral=True)
    async with db_session_factory() as session:
        assert await get_routine(session, row.id, tenant_id=tenant.id) is not None
    refresh.assert_not_awaited()


async def test_already_deleted_row_is_gone(setup_panel, db_session_factory):
    view, tenant, refresh, _ = setup_panel
    row, _, button = await seed_selected(view, tenant, db_session_factory)
    click = interaction()
    await button.callback(click)
    confirm = click.followup.send.call_args.kwargs["view"]
    async with db_session_factory.begin() as session:
        await delete_routine(session, row.id, tenant_id=tenant.id)
    submit = interaction()
    await confirm.confirm.callback(submit)
    submit.response.send_message.assert_awaited_once_with(embeds.GONE, ephemeral=True)
    refresh.assert_not_awaited()


@pytest.mark.parametrize("grant", ["administrator", "manage_guild", "owner"])
async def test_pause_recognizes_all_server_admins(setup_panel, db_session_factory, grant):
    view, tenant, refresh, _ = setup_panel
    row, view, _ = await seed_selected(view, tenant, db_session_factory, creator="777")
    click = interaction(admin=False)
    if grant == "owner":
        click.guild.owner_id = click.user.id
    else:
        setattr(click.user.guild_permissions, grant, True)
    button = next(child for child in view.walk_children() if isinstance(child, panel._PauseButton))
    await button.callback(click)
    async with db_session_factory() as session:
        saved = await get_routine(session, row.id, tenant_id=tenant.id)
    assert saved is not None and not saved.enabled
    refresh.assert_awaited_once_with(click, view)


async def test_delete_cancel_keeps_row(setup_panel, db_session_factory):
    view, tenant, refresh, _ = setup_panel
    row, _, button = await seed_selected(view, tenant, db_session_factory)
    click = interaction()
    await button.callback(click)
    confirm = click.followup.send.call_args.kwargs["view"]
    await confirm.cancel.callback(interaction())
    async with db_session_factory() as session:
        assert await get_routine(session, row.id, tenant_id=tenant.id) is not None
    refresh.assert_not_awaited()


async def test_cross_tenant_delete_is_gone(setup_panel, db_session_factory):
    view, tenant, refresh, _ = setup_panel
    row, _, button = await seed_selected(view, tenant, db_session_factory)
    click = interaction()
    click.guild_id = 555555
    await button.callback(click)
    confirm = click.followup.send.call_args.kwargs["view"]
    submit = interaction()
    submit.guild_id = 555555
    await confirm.confirm.callback(submit)
    submit.response.send_message.assert_awaited_once_with(embeds.GONE, ephemeral=True)
    async with db_session_factory() as session:
        assert await get_routine(session, row.id, tenant_id=tenant.id) is not None
    refresh.assert_not_awaited()


def thread_interaction(*, parent_id=987654):
    event = interaction()
    event.channel_id = 111111
    event.channel = MagicMock(spec=discord.Thread)
    event.channel.id = event.channel_id
    event.channel.guild.id = event.guild_id
    event.channel.parent_id = parent_id
    event.channel.parent = None  # A verified parent id does not require a warm channel cache.
    return event


def recruitment_settings():
    return RecruitmentSettings(
        tenants={
            "123456": RecruitmentTenantSettings(channel_ids=("987654",), recruiter_user_ids=("42",))
        }
    )


async def test_thread_creation_preserves_source_and_parent_budget(setup_panel, db_session_factory):
    view, tenant, refresh, _ = setup_panel
    async with db_session_factory.begin() as session:
        await set_channel_budget(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="987654",
            limit_usd=Decimal("0"),
            window="total",
            starts_at=None,
            ends_at=None,
            set_by_account_id=None,
        )
    click = thread_interaction()
    modal = await open_modal(view, click)
    await modal.on_submit(thread_interaction())
    async with db_session_factory() as session:
        rows = await list_routines_for_tenant(session, tenant_id=tenant.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.channel_id == "987654", "scheduler must use the parent's budget key"
        assert row.source_channel_id == "987654"
        assert row.source_thread_id == "111111"
        assert row.source_provenance_known
        assert row.created_by_user_id == "42" and row.next_fire_at is not None
        assert (
            await list_routines_for_tenant(
                session, tenant_id=tenant.id, recruitment=recruitment_settings()
            )
            == []
        ), "a later settings restart must withhold a routine from this parent"
    assert await is_over_channel_budget(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        channel_id=row.channel_id,
        now=datetime.now(UTC),
    ), "the scheduler's budget gate must reject this thread-created routine"
    assert not await is_over_channel_budget(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="111111",
        now=datetime.now(UTC),
    ), "using the thread id would bypass the parent's exhausted budget"
    refresh.assert_awaited_once_with(click, view)


@pytest.mark.parametrize("in_thread", [False, True])
async def test_creation_refuses_recruitment_origin_at_submit(
    setup_panel, db_session_factory, in_thread
):
    view, tenant, refresh, resolver = setup_panel
    click = thread_interaction() if in_thread else interaction()
    modal = await open_modal(view, click)
    view.runtime.settings.recruitment = recruitment_settings()
    submit = thread_interaction() if in_thread else interaction()
    await modal.on_submit(submit)
    submit.response.send_message.assert_awaited_once_with(embeds.SOURCE_UNAVAILABLE, ephemeral=True)
    async with db_session_factory() as session:
        assert await list_routines_for_tenant(session, tenant_id=tenant.id) == []
    resolver.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "invalid", ["missing_channel", "unresolved_parent", "wrong_guild", "wrong_id"]
)
async def test_creation_refuses_unverified_source(setup_panel, db_session_factory, invalid):
    view, tenant, refresh, resolver = setup_panel
    click = thread_interaction()
    if invalid == "missing_channel":
        click.channel = None
    elif invalid == "unresolved_parent":
        click.channel.parent_id = None
    elif invalid == "wrong_guild":
        click.channel.guild.id = 555555
    else:
        click.channel.id = 222222
    modal = await open_modal(view, click)
    submit = interaction()
    await modal.on_submit(submit)
    submit.response.send_message.assert_awaited_once_with(embeds.SOURCE_UNVERIFIED, ephemeral=True)
    async with db_session_factory() as session:
        assert await list_routines_for_tenant(session, tenant_id=tenant.id) == []
    resolver.assert_not_awaited()
    refresh.assert_not_awaited()
