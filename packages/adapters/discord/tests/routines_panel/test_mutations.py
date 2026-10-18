"""Routine lifecycle interactions against owned Postgres and a fake MA transport."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.discord.routines_panel.mutations import (
    CreateRoutineModal,
    DeleteRoutineButton,
    DeleteRoutineConfirmView,
    NewRoutineButton,
)
from daimon.adapters.discord.routines_panel.panel import RoutinesPanelView
from daimon.adapters.discord.routines_panel.state import (
    RoutineEntry,
    RoutinesPanelState,
    derive_state,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    create_routine,
    delete_routine,
    get_routine,
    list_routines_for_tenant,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = derive_tenant_uuid(platform="discord", workspace_id="100")
FOREIGN = derive_tenant_uuid(platform="discord", workspace_id="200")
AGENT = "agent_00000000000000000000000001"


def interaction(*, admin: bool = True, user_id: int = 42, guild_id: int = 100) -> Any:
    result = MagicMock(spec=discord.Interaction)
    result.user = MagicMock(spec=discord.Member)
    result.user.id = user_id
    result.user.guild_permissions = discord.Permissions(manage_guild=admin)
    result.guild_id = guild_id
    result.channel_id = 101
    result.response = MagicMock(spec=discord.InteractionResponse)
    done = False

    async def acknowledge(*_args: object, **_kwargs: object) -> None:
        nonlocal done
        assert not done, "each Discord interaction can only be acknowledged once"
        done = True

    result.response.defer = AsyncMock(side_effect=acknowledge)
    result.response.send_message = AsyncMock(side_effect=acknowledge)
    result.response.send_modal = AsyncMock(side_effect=acknowledge)
    result.response.edit_message = AsyncMock(side_effect=acknowledge)
    result.response.is_done = lambda: done
    result.edit_original_response = AsyncMock()
    result.followup.send = AsyncMock()
    return result


@dataclass
class Harness:
    factory: async_sessionmaker[AsyncSession]
    client: AsyncAnthropic
    view: RoutinesPanelView
    agents: list[dict[str, Any]]
    requests: list[httpx.Request] = field(default_factory=list)
    fail_on_request: int | None = None

    async def rows(self, tenant_id: uuid.UUID = TENANT) -> list[RoutineRow]:
        async with self.factory() as session:
            return await list_routines_for_tenant(session, tenant_id=tenant_id)

    async def seed(
        self, *, tenant_id: uuid.UUID = TENANT, creator: str | None = "42"
    ) -> RoutineEntry:
        async with self.factory() as session, session.begin():
            row = await create_routine(
                session,
                tenant_id=tenant_id,
                created_by_user_id=creator,
                agent_id=AGENT,
                agent_name="helper",
                cron_expr="0 9 * * 1-5",
                timezone_="UTC",
                trigger_message="Summarize @everyone **carefully**",
            )
        glyph, color = derive_state(row)
        entry = RoutineEntry(row, "helper", glyph, color, row.trigger_message)
        self.view.state.rows.append(entry)
        self.view.state.selected = entry
        return entry


@pytest.fixture
async def harness(db_session_factory: async_sessionmaker[AsyncSession]) -> Any:
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id="100", id=TENANT)
        await make_tenant(session, platform="discord", workspace_id="200", id=FOREIGN)
    agents = [ma_agent(id=AGENT, name="helper", tenant_id=TENANT).model_dump(mode="json")]

    def handler(request: httpx.Request) -> httpx.Response:
        h.requests.append(request)
        assert request.method == "GET" and request.url.path == "/v1/agents"
        if h.fail_on_request == len(h.requests):
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "fake failure"},
                },
            )
        return httpx.Response(200, json={"data": agents, "next_page": None})

    client = build_stub_anthropic(handler)
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    runtime.anthropic = client
    state = RoutinesPanelState.initial(rows=[], over_cap_count=0, agent_name_map={AGENT: "helper"})
    view = RoutinesPanelView(state, runtime=runtime, allowed_user_id=42)
    h = Harness(db_session_factory, client, view, agents)
    yield h
    await client.close()


def form(h: Harness) -> CreateRoutineModal:
    modal = CreateRoutineModal(h.view, guild_id=100, channel_id=101)
    modal.agent._values = [AGENT]
    modal.cron._value = " 0 9 * * 1-5 "
    modal.timezone._value = " UTC "
    modal.message._value = " Summarize yesterday "
    return modal


def button(view: RoutinesPanelView, kind: type[Any]) -> Any:
    return next(item for item in view.walk_children() if isinstance(item, kind))


async def test_new_opens_labeled_form_without_writes(harness: Harness) -> None:
    click = interaction()
    await button(harness.view, NewRoutineButton).callback(click)
    modal = click.response.send_modal.call_args.args[0]
    assert isinstance(modal, CreateRoutineModal)
    assert [item.text for item in modal.children] == [
        "Agent",
        "Schedule (cron)",
        "Time zone",
        "What to ask the agent",
    ]
    assert len(modal.to_components()) == 4
    assert await harness.rows() == [], "opening or dismissing the modal must not create a routine"
    assert harness.requests == [], "opening the form must not wait on provider I/O"
    assert button(harness.view, DeleteRoutineButton).disabled
    for item in harness.view.walk_children():
        if isinstance(item, discord.ui.ActionRow):
            assert len(item.children) <= 5


@pytest.mark.parametrize("user_id,admin", [(42, False), (43, True)])
async def test_new_refuses_nonadmin_or_other_opener(
    harness: Harness, user_id: int, admin: bool
) -> None:
    click = interaction(user_id=user_id, admin=admin)
    await button(harness.view, NewRoutineButton).callback(click)
    click.response.send_modal.assert_not_awaited()
    click.response.send_message.assert_awaited_once()
    assert await harness.rows() == []


async def test_empty_agent_list_explains_next_step(harness: Harness) -> None:
    harness.view.state.agent_name_map.clear()
    click = interaction()
    await button(harness.view, NewRoutineButton).callback(click)
    assert "No agents" in click.response.send_message.call_args.args[0]
    click.response.send_modal.assert_not_awaited()


async def test_agent_select_caps_and_explains_overflow(harness: Harness) -> None:
    harness.view.state.agent_name_map = {f"agent_{n}": "x" * 110 + str(n) for n in range(30)}
    modal = CreateRoutineModal(harness.view, guild_id=100, channel_id=101)
    assert len(modal.agent.options) == 25
    assert all(len(option.label) <= 100 for option in modal.agent.options)
    assert "First 25" in modal.children[0].description


async def test_create_persists_identity_schedule_channel_and_refreshes(harness: Harness) -> None:
    submit = interaction()
    before = datetime.now(UTC)
    await form(harness).on_submit(submit)
    rows = await harness.rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row.tenant_id, row.created_by_user_id, row.channel_id) == (TENANT, "42", "101")
    assert (row.agent_id, row.agent_name, row.trigger_message) == (
        AGENT,
        "helper",
        "Summarize yesterday",
    )
    assert (row.cron_expr, row.timezone, row.enabled) == ("0 9 * * 1-5", "UTC", True)
    assert row.next_fire_at is not None and row.next_fire_at >= before
    assert row.destination_kind is None and row.destination_id is None
    submit.response.defer.assert_awaited_once()
    fresh = submit.edit_original_response.call_args.kwargs["view"]
    assert fresh.state.selected.routine.id == row.id
    assert (
        submit.edit_original_response.call_args.kwargs["allowed_mentions"].to_dict()
        == discord.AllowedMentions.none().to_dict()
    )
    assert submit.followup.send.call_args.args[0] == "Routine created."


@pytest.mark.parametrize(
    "field,value",
    [
        ("cron", " "),
        ("message", " "),
        ("timezone", ""),
        ("cron", "invalid"),
        ("cron", "0 9 * * * extra"),
        ("timezone", "Not/AZone"),
    ],
)
async def test_bad_form_never_reaches_provider_or_store(
    harness: Harness, field: str, value: str
) -> None:
    modal = form(harness)
    getattr(modal, field)._value = value
    submit = interaction()
    await modal.on_submit(submit)
    submit.response.send_message.assert_awaited_once()
    assert harness.requests == []
    assert await harness.rows() == []


@pytest.mark.parametrize(
    "user_id,admin,guild_id", [(42, False, 100), (43, True, 100), (42, True, 200)]
)
async def test_create_rechecks_permissions_and_context(
    harness: Harness, user_id: int, admin: bool, guild_id: int
) -> None:
    await form(harness).on_submit(interaction(user_id=user_id, admin=admin, guild_id=guild_id))
    assert await harness.rows() == []
    assert harness.requests == []


@pytest.mark.parametrize("mode", ["missing", "foreign", "forged", "no_selection"])
async def test_create_requires_current_tenant_agent(harness: Harness, mode: str) -> None:
    modal = form(harness)
    if mode == "missing":
        harness.agents.clear()
    elif mode == "foreign":
        harness.agents[0]["metadata"]["daimon_tenant"] = str(FOREIGN)
    else:
        modal.agent._values = ["unlisted"] if mode == "forged" else []
    await modal.on_submit(interaction())
    assert await harness.rows() == []


async def test_repeated_create_after_refresh_failure_is_not_retried(harness: Harness) -> None:
    harness.fail_on_request = 2
    modal = form(harness)
    first, duplicate = interaction(), interaction()
    await asyncio.gather(modal.on_submit(first), modal.on_submit(duplicate))
    assert len(await harness.rows()) == 1
    messages = [first.followup.send.call_args.args[0], duplicate.followup.send.call_args.args[0]]
    assert any("Routine created. Open /routines" in message for message in messages)
    assert any("already created" in message for message in messages)


async def test_create_provider_failure_leaves_no_row_and_reports_error(harness: Harness) -> None:
    harness.fail_on_request = 1
    submit = interaction()
    await form(harness).on_submit(submit)
    assert await harness.rows() == []
    assert "Something went wrong" in submit.followup.send.call_args.args[0]


async def test_delete_first_click_acknowledges_and_only_opens_confirmation(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await harness.seed()
    view = RoutinesPanelView(harness.view.state, runtime=harness.view.runtime, allowed_user_id=42)
    click = interaction()

    async def checked_read(*args: Any, **kwargs: Any) -> RoutineRow | None:
        assert click.response.is_done(), "DB I/O must follow the interaction acknowledgement"
        return await get_routine(*args, **kwargs)

    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.mutations.get_routine", checked_read
    )
    await button(view, DeleteRoutineButton).callback(click)
    confirmation = click.edit_original_response.call_args.kwargs["view"]
    assert isinstance(confirmation, DeleteRoutineConfirmView)
    assert confirmation.routine_id == entry.routine.id
    assert len(await harness.rows()) == 1
    assert (
        click.edit_original_response.call_args.kwargs["allowed_mentions"].to_dict()
        == discord.AllowedMentions.none().to_dict()
    )
    labels = [
        item.label for item in confirmation.walk_children() if isinstance(item, discord.ui.Button)
    ]
    assert labels == ["Delete routine", "Cancel"]


@pytest.mark.parametrize("creator,admin", [("42", False), ("someone_else", True), (None, True)])
async def test_creator_or_admin_can_confirm_deletion(
    harness: Harness, creator: str | None, admin: bool
) -> None:
    entry = await harness.seed(creator=creator)
    view = DeleteRoutineConfirmView(harness.view, entry, guild_id=100)
    submit = interaction(admin=admin)
    await view.confirm(submit)
    assert await harness.rows() == []
    assert submit.followup.send.call_args.args[0] == "Routine deleted."
    assert submit.edit_original_response.call_args.kwargs["view"].state.selected is None


@pytest.mark.parametrize(
    "creator,admin,user_id,guild_id,tenant_id",
    [
        ("other", False, 42, 100, TENANT),
        (None, False, 42, 100, TENANT),
        ("42", True, 43, 100, TENANT),
        ("42", True, 42, 200, TENANT),
        ("42", True, 42, 100, FOREIGN),
    ],
)
async def test_delete_refuses_changed_authority_or_wrong_context(
    harness: Harness,
    creator: str | None,
    admin: bool,
    user_id: int,
    guild_id: int,
    tenant_id: uuid.UUID,
) -> None:
    entry = await harness.seed(tenant_id=tenant_id, creator=creator)
    view = DeleteRoutineConfirmView(harness.view, entry, guild_id=100)
    await view.confirm(interaction(admin=admin, user_id=user_id, guild_id=guild_id))
    assert len(await harness.rows(tenant_id)) == 1


async def test_delete_target_does_not_follow_changed_panel_selection(harness: Harness) -> None:
    first = await harness.seed()
    view = DeleteRoutineConfirmView(harness.view, first, guild_id=100)
    second = await harness.seed()
    await view.confirm(interaction())
    assert [row.id for row in await harness.rows()] == [second.routine.id]


async def test_cancel_closes_confirmation_and_delayed_confirm_does_not_delete(
    harness: Harness,
) -> None:
    entry = await harness.seed()
    view = DeleteRoutineConfirmView(harness.view, entry, guild_id=100)
    await view.cancel(interaction())
    delayed = interaction()
    await view.confirm(delayed)
    assert len(await harness.rows()) == 1
    assert "already closed" in delayed.followup.send.call_args.args[0]


async def test_deleted_between_open_and_confirm_is_explained(harness: Harness) -> None:
    entry = await harness.seed()
    view = DeleteRoutineConfirmView(harness.view, entry, guild_id=100)
    async with harness.factory() as session, session.begin():
        await delete_routine(session, entry.routine.id, tenant_id=TENANT)
    submit = interaction()
    await view.confirm(submit)
    assert submit.followup.send.call_args.args[0] == "This routine no longer exists."
    assert await harness.rows() == []


async def test_repeated_delete_and_refresh_failure_report_committed_result(
    harness: Harness,
) -> None:
    entry = await harness.seed()
    harness.fail_on_request = 1
    view = DeleteRoutineConfirmView(harness.view, entry, guild_id=100)
    first, duplicate = interaction(), interaction()
    await asyncio.gather(view.confirm(first), view.confirm(duplicate))
    assert await harness.rows() == []
    messages = [first.followup.send.call_args.args[0], duplicate.followup.send.call_args.args[0]]
    assert any("Routine deleted. Open /routines" in message for message in messages)
    assert any("already closed" in message for message in messages)


@pytest.mark.parametrize("foreign", [False, True])
async def test_delete_open_refuses_nonowner_or_foreign_routine_after_deferral(
    harness: Harness, foreign: bool
) -> None:
    await harness.seed(tenant_id=FOREIGN if foreign else TENANT, creator="other")
    view = RoutinesPanelView(harness.view.state, runtime=harness.view.runtime, allowed_user_id=42)
    click = interaction(admin=False)
    await button(view, DeleteRoutineButton).callback(click)
    click.response.defer.assert_awaited_once()
    click.followup.send.assert_awaited_once()
    click.edit_original_response.assert_not_awaited()
    assert len(await harness.rows(FOREIGN if foreign else TENANT)) == 1


@pytest.mark.parametrize("operation", ["create", "delete"])
async def test_store_failure_rolls_back_and_surfaces_ephemeral_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from daimon.adapters.discord.routines_panel import mutations
    from sqlalchemy.exc import SQLAlchemyError

    submit = interaction()
    if operation == "create":
        original = mutations.create_routine_via_panel
        callback = form(harness).on_submit
        target = "create_routine_via_panel"
    else:
        entry = await harness.seed()
        original = mutations.delete_routine_via_panel
        callback = DeleteRoutineConfirmView(harness.view, entry, guild_id=100).confirm
        target = "delete_routine_via_panel"

    async def fail_after_write(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        raise SQLAlchemyError("fake database failure")

    monkeypatch.setattr(mutations, target, fail_after_write)
    await callback(submit)
    assert len(await harness.rows()) == (0 if operation == "create" else 1), (
        "failed transaction must roll back"
    )
    assert "Something went wrong" in submit.followup.send.call_args.args[0]
    assert submit.followup.send.call_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("operation", ["create", "delete"])
async def test_lost_success_notice_does_not_repeat_committed_write(
    harness: Harness, operation: str
) -> None:
    submit = interaction()
    submit.followup.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=500, reason="fake Discord failure"), "fake Discord failure"
    )
    if operation == "create":
        callback = form(harness).on_submit
    else:
        entry = await harness.seed()
        callback = DeleteRoutineConfirmView(harness.view, entry, guild_id=100).confirm
    await callback(submit)
    await callback(interaction())
    assert len(await harness.rows()) == (1 if operation == "create" else 0)
