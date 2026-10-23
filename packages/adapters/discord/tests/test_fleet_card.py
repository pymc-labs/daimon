"""Actual Discord controls over the shared resolver/operations and offline ports."""

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, create_autospec
from uuid import UUID

import discord
import pytest
import structlog
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.posted_controls.fleet import (
    DiscordFleetCard,
    DiscordFleetCardFactory,
    FleetControl,
    build_fleet_view,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import AgentRef, FleetFacts, Place, Subject
from daimon.core.fleet import FleetRun, FleetWorker, ResultPointer, result_digest
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetPorts, FleetRefused
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.neutral_resources import WorkerLimits, WorkerPermissions
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from discord.webhook.async_ import AsyncWebhookAdapter, async_context
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, NetworkPolicy, ToolSpec
from mux.profiles import backend_selection, get_profile
from sqlalchemy.ext.asyncio import async_sessionmaker

NOW = datetime(2026, 10, 12, 19, tzinfo=UTC)
TENANT = derive_tenant_uuid(platform="discord", workspace_id="60")
ACCOUNT, RUN = UUID(int=41), UUID(int=42)
POLICY = TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini"))


def parent(provider="anthropic"):
    backend = backend_selection(provider)
    model = ModelRef(provider=provider, id=backend.model or "claude-haiku-5-5")
    profile = get_profile(backend.profile)
    tools = (ToolSpec(name="read", kind="custom", permission="auto"),)
    if profile.implicit_execution:
        tools += (ToolSpec(name=profile.execution_tool, kind="builtin"),)
    return WorkerPermissions(
        fleet=FleetFacts(
            scope=Scope(
                tenant_id=str(TENANT),
                account_id=str(ACCOUNT),
                principal_id="daimon",
                authorization_id="fixture-root-grant",
            ),
            root=ThreadRef(
                channel=ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="50"),
                thread_id="70",
            ),
            account_id=ACCOUNT,
            origin_id=UUID(int=43),
            run_id=RUN,
            place=Place(channel_id="70", parent_channel_id="50"),
            enabled=True,
            active=True,
        ),
        subject=Subject(platform_user_id="123"),
        agent=AgentRef.of("root"),
        spec=AgentSpec(name="root", model=model, system="Root safety", tools=tools),
        base_system="Root safety",
        backend_grants=(backend,),
        models=(model,),
        limits=WorkerLimits(
            budget_usd=Decimal("1"), deadline=NOW + timedelta(minutes=10), max_turns=4
        ),
        environment=EnvironmentSpec(name="root", network=NetworkPolicy(mode="none")),
        agent_id=UUID(int=44),
    )


def run():
    return FleetRun(
        RUN,
        TENANT,
        ACCOUNT,
        "root-session",
        "root-binding",
        "discord",
        "50",
        "70",
        "running",
        Decimal("1"),
        Decimal("0.2"),
        Decimal("0"),
        NOW + timedelta(minutes=10),
        8,
        None,
        2,
    )


def worker(provider="anthropic", ordinal=1, **updates):
    owner = parent(provider)
    row = FleetWorker(
        UUID(int=50 + ordinal),
        RUN,
        TENANT,
        ordinal,
        "research @everyone <#50>",
        owner.spec.model_dump(mode="json"),
        "fixture-digest",
        provider,
        owner.backend_grants[0].profile,
        owner.spec.model.id,
        "worker-binding",
        "pending",
        Decimal("0.2"),
        Decimal("0.2"),
        NOW + timedelta(minutes=5),
        2,
        0,
        1,
        None,
        None,
        None,
        None,
        3,
    )
    return replace(row, **updates)


def interaction(user_id=123, guild=60, thread=70, card=None):
    value = MagicMock(spec=discord.Interaction)
    value.user.id = user_id
    value.guild_id, value.channel_id = guild, thread
    value.channel = MagicMock(spec=discord.Thread)
    value.channel.send = AsyncMock()
    value.client.fleet_card_for = MagicMock(return_value=card)
    value.client.recover_fleet_card = AsyncMock(return_value=card)
    value.response.send_message = AsyncMock()
    value.response.defer = AsyncMock()
    value.message = MagicMock(spec=discord.Message)
    value.message.id = 900
    value.response.edit_message = AsyncMock()
    value.response.is_done.return_value = False

    async def responded(*args, **kwargs):
        value.response.is_done.return_value = True

    value.response.send_message.side_effect = responded
    value.response.edit_message.side_effect = responded
    return value


def buttons(view):
    return [
        item
        for item in view.walk_children()
        if isinstance(item, FleetControl) and item.worker_id is not None
    ]


def setup(provider="anthropic"):
    owner = create_autospec(FleetPorts, instance=True)
    owner.controller_account.side_effect = lambda current: (
        ACCOUNT if current.subject.platform_user_id == "123" else None
    )
    owner.read_run.return_value = (run(), (worker(provider),))
    owner.retained_control.return_value = None

    async def control(_parent, fleet, target, **kwargs):
        return {
            "run_id": str(fleet.id),
            "worker_id": str(target.id) if target is not None else None,
            "accepted": True,
            "version": kwargs["expected_version"] + 1,
            "action": kwargs["action"],
            "operation_key": kwargs["operation_key"],
            "expected_version": kwargs["expected_version"],
        }

    owner.control.side_effect = control

    async def settle_run(_parent, fleet, status, **_kwargs):
        return replace(fleet, status=status, version=fleet.version + 1)

    owner.settle_run.side_effect = settle_run
    owner.pending_wakes.return_value = 0
    loader = AsyncMock(return_value=(parent(provider), POLICY))
    send, edit = AsyncMock(), AsyncMock()
    card = DiscordFleetCard(
        run_id=RUN,
        service=FleetOrchestrator(owner),
        load_parent=loader,
        send=send,
        edit=edit,
        clock=lambda: NOW,
    )
    return card, owner, loader, send, edit


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("action", ["kill", "resume"])
async def test_sdk_button_uses_real_shared_control_and_current_host_loader(provider, action):
    card, owner, loader, send, _ = setup(provider)
    await card.refresh()
    view = send.call_args.kwargs["view"]
    label = "Stop 1" if action == "kill" else "Resume 1"
    click = interaction(card=card)
    await next(item for item in buttons(view) if item.item.label == label).callback(click)
    owner.control.assert_awaited_once()
    assert owner.control.call_args.kwargs["action"] == action
    assert owner.control.call_args.kwargs["expected_version"] == 3
    assert owner.control.call_args.args[0] is loader.return_value[0]
    assert loader.await_args_list[-1].args == (click,)
    click.response.defer.assert_awaited_once()
    assert str(RUN.hex) in buttons(view)[0].item.custom_id


@pytest.mark.parametrize(
    "mutation", ["wrong_user", "wrong_guild", "wrong_thread", "worker_origin", "inactive", "stale"]
)
async def test_real_core_refusal_before_any_control_write(mutation):
    card, owner, loader, send, _ = setup()
    await card.refresh()
    view = send.call_args.kwargs["view"]
    click = interaction(
        456 if mutation == "wrong_user" else 123,
        guild=61 if mutation == "wrong_guild" else 60,
        thread=71 if mutation == "wrong_thread" else 70,
        card=card,
    )
    if mutation in ("worker_origin", "inactive"):
        current, policy = loader.return_value
        loader.return_value = (
            replace(
                current,
                fleet=replace(current.fleet, depth=1)
                if mutation == "worker_origin"
                else replace(current.fleet, active=False),
            ),
            policy,
        )
    if mutation == "stale":
        owner.read_run.return_value = (run(), (worker(version=4),))
    await buttons(view)[0].callback(click)
    owner.control.assert_not_awaited()
    click.response.edit_message.assert_not_awaited()
    click.response.send_message.assert_awaited_once()


async def test_waiting_owner_ack_keeps_controls_active():
    card, owner, _, send, _ = setup()
    await card.refresh()
    view = send.call_args.kwargs["view"]
    original = owner.control.side_effect

    async def waiting(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["accepted"] = False
        return result

    owner.control.side_effect = waiting
    click = interaction(card=card)
    await buttons(view)[0].callback(click)
    assert not any(button.item.disabled for button in buttons(view))
    click.response.edit_message.assert_not_awaited()


async def test_fleet_edits_follow_the_transport_replacement_message():
    card, _, _, send, edit = setup()
    first = MagicMock(spec=discord.Message)
    first.id = 900
    replacement = MagicMock(spec=discord.Message)
    replacement.id = 901
    send.return_value = first
    edit.return_value = replacement
    await card.refresh()
    await card.refresh()
    await card.refresh()
    assert edit.await_args_list[0].args[0] is first
    assert edit.await_args_list[1].args[0] is replacement


@pytest.mark.parametrize("admin", [False, True])
async def test_current_nonrequester_subject_is_authorized_by_the_shared_owner(admin):
    card, owner, loader, send, _ = setup()
    await card.refresh()
    current, policy = loader.return_value
    loader.return_value = (
        replace(current, subject=Subject(platform_user_id="456", is_admin=admin)),
        policy,
    )
    click = interaction(456, card=card)
    with structlog.testing.capture_logs() as logs:
        await buttons(send.call_args.kwargs["view"])[0].callback(click)
    assert owner.control.await_count == (1 if admin else 0)
    pressed = [entry for entry in logs if entry["event"] == "fleet.control_pressed"]
    assert pressed == (
        [
            {
                "event": "fleet.control_pressed",
                "log_level": "info",
                "run_id": str(RUN),
                "worker_id": str(worker().id),
                "action": "kill",
                "pressed_by": "456",
                "presser_is_admin": True,
            }
        ]
        if admin
        else []
    ), "only an accepted press is recorded, with who pressed it"
    if not admin:
        click.response.send_message.assert_awaited_once_with(
            "Only the person who started this fleet, or someone with Manage Server, can stop it.",
            ephemeral=True,
        )


@pytest.mark.parametrize(
    ("refusal", "text"),
    [
        ("terminal_worker", "This worker has already finished."),
        (
            "stale",
            "This fleet changed since the card was drawn. Wait for the card to update, then try again.",
        ),
        ("inactive", "You can't control this fleet from here."),
        ("outage", "Couldn't reach the fleet just now. Try again in a moment."),
        (
            "limit",
            "This fleet has run out of the time or steps it was given, so it can't be resumed.",
        ),
        (
            "operation_conflict",
            "Someone's press on this fleet is still going through. Try again in a moment.",
        ),
        (
            "control_evidence_unavailable",
            "Someone's press on this fleet is still going through. Try again in a moment.",
        ),
        (
            "control_evidence_invalid",
            "This fleet changed since the card was drawn. Wait for the card to update, then try again.",
        ),
    ],
)
async def test_a_refused_fleet_click_names_its_real_cause(refusal, text):
    card, owner, loader, send, _ = setup()
    await card.refresh()
    if refusal == "terminal_worker":
        # The run goes on; only the clicked worker has finished.
        owner.read_run.return_value = (run(), (worker(status="succeeded"),))
    elif refusal == "stale":
        owner.read_run.return_value = (run(), (worker(version=4),))
    elif refusal == "inactive":
        current, policy = loader.return_value
        loader.return_value = (replace(current, fleet=replace(current.fleet, active=False)), policy)
    elif refusal == "outage":
        loader.side_effect = FleetRefused("prerequisites_unavailable")
    elif refusal == "limit":
        owner.read_run.return_value = (run(), (worker(deadline_at=NOW),))
    else:
        # Refused by the shared owner while writing the press.
        owner.control.side_effect = FleetRefused(refusal)
    label = "Resume 1" if refusal == "limit" else "Stop 1"
    click = interaction(card=card)
    view = send.call_args.kwargs["view"]
    await next(item for item in buttons(view) if item.item.label == label).callback(click)
    written = refusal in (
        "operation_conflict",
        "control_evidence_unavailable",
        "control_evidence_invalid",
    )
    assert owner.control.await_count == (1 if written else 0)
    click.response.send_message.assert_awaited_once_with(text, ephemeral=True)


async def test_a_click_on_a_fleet_that_already_ended_says_so():
    card, owner, _, send, _ = setup()
    await card.refresh()
    owner.read_run.return_value = (replace(run(), status="cancelled"), (worker(),))
    click = interaction(card=card)
    await buttons(send.call_args.kwargs["view"])[0].callback(click)
    owner.control.assert_not_awaited()
    click.response.send_message.assert_awaited_once_with(
        "This fleet has already stopped.", ephemeral=True
    )


async def test_restart_dispatch_rebuilds_the_card_through_the_registered_sdk_item(
    db_session_factory, monkeypatch
):
    from .harness import make_bot
    from .test_bot import _make_runtime

    original, owner, loader, send, edit = setup()
    await original.refresh()
    view = send.call_args.kwargs["view"]
    custom_id = buttons(view)[0].item.custom_id
    components = view.to_components()
    del original, view

    async def resolve_root(thread, actor_id, intent_id, run_id, current):
        assert thread.id == 70 and actor_id == 123
        assert intent_id == intent.id and run_id == RUN
        return await loader(current)

    factory = DiscordFleetCardFactory(FleetOrchestrator(owner), resolve_root)

    from daimon.core.stores.agent_posts import record_post
    from daimon.core.stores.turn_card_intents import create_turn_card_intent
    from daimon.testing.factories import make_tenant

    async with db_session_factory.begin() as session:
        await make_tenant(session, id=TENANT, platform="discord", workspace_id="60")
        intent = await create_turn_card_intent(
            session,
            tenant_id=TENANT,
            platform="discord",
            thread_id="70",
            channel_id="50",
            turn_token=UUID(int=74),
        )
        await record_post(
            session,
            tenant_id=TENANT,
            platform="discord",
            channel_id="70",
            message_id="900",
            agent_id=UUID(int=44),
            parent_channel_id="50",
            source="turn",
            requester_platform_user_id="123",
            turn_card_intent_id=intent.id,
        )
    runtime = _make_runtime(db_session_factory)
    runtime.settings.fleet.enabled = True
    bot = make_bot(replace(runtime, fleet_card_factory=factory))
    bot.start_orphan_recovery = MagicMock()
    await bot.setup_hook()
    click = interaction()
    click.client = bot
    click.channel.id = 70
    click.channel.parent_id = 50
    click.channel.guild.id = 60
    click.message = discord.Message(
        state=bot._connection,
        channel=click.channel,
        data={
            "id": "900",
            "type": 0,
            "content": "",
            "author": {"id": "999", "username": "fixture", "discriminator": "0", "avatar": None},
            "flags": 32768,
            "timestamp": "2026-10-12T19:00:00+00:00",
            "edited_timestamp": None,
            "pinned": False,
            "mention_everyone": False,
            "tts": False,
            "attachments": [],
            "embeds": [],
            "mentions": [],
            "mention_roles": [],
            "components": components,
        },
    )

    async def edit_gateway(channel_id, message_id, *, params):
        assert channel_id == 70 and message_id == 900
        assert params.payload["components"]
        return {
            "id": "900",
            "channel_id": "70",
            "type": 0,
            "content": "",
            "author": {"id": "999", "username": "fixture", "discriminator": "0", "avatar": None},
            "flags": 32768,
            "timestamp": NOW.isoformat(),
            "edited_timestamp": NOW.isoformat(),
            "pinned": False,
            "mention_everyone": False,
            "tts": False,
            "attachments": [],
            "embeds": [],
            "mentions": [],
            "mention_roles": [],
            "components": params.payload["components"],
        }

    bot.http.edit_message = AsyncMock(side_effect=edit_gateway)
    click.data = {"custom_id": custom_id, "component_type": 2}
    store = bot._connection._view_store
    tasks = []
    original_add = store.add_task

    def retain(task):
        tasks.append(task)
        original_add(task)

    monkeypatch.setattr(store, "add_task", retain)
    store.dispatch_dynamic_items(2, custom_id, click)
    assert len(tasks) == 1, "setup_hook must register the durable handler"
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
    owner.control.assert_awaited_once()
    assert owner.control.call_args.args[1].id == RUN
    assert owner.control.call_args.kwargs["expected_version"] == 3
    assert (
        owner.control.call_args.kwargs["operation_key"]
        == f"discord:fleet:{RUN}:{worker().id}:3:kill"
    )
    click.response.defer.assert_awaited_once()
    bot.http.edit_message.assert_awaited_once()
    assert loader.await_args_list[-1].args == (click,)


async def test_the_default_maximum_fleet_keeps_every_worker_and_control():
    rows = tuple(worker(ordinal=index) for index in range(1, 9))
    view = build_fleet_view(run(), rows)
    assert len(buttons(view)) == 16
    assert view.total_children_count <= 40
    assert view.content_length() <= 4000


@pytest.mark.parametrize("terminal", [False, True])
async def test_sixteen_workers_fit_with_all_statuses_and_run_level_controls(terminal):
    rows = tuple(
        worker(ordinal=index, status="succeeded" if terminal and index > 12 else "pending")
        for index in range(1, 17)
    )
    view = build_fleet_view(replace(run(), max_workers=16), rows)
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert all(f"{index}. **" in text for index in range(1, 17))
    assert view.total_children_count <= 40
    assert view.content_length() <= 4000
    individual = buttons(view)
    assert len(individual) == 24
    expected = range(1, 13) if terminal else range(5, 17)
    assert {item.worker_id for item in individual} == {
        worker(ordinal=index).id for index in expected
    }
    all_controls = [
        item
        for item in view.walk_children()
        if isinstance(item, FleetControl) and item.worker_id is None
    ]
    assert {item.item.label for item in all_controls} == {"Stop all", "Resume all"}
    assert all(
        item.run_id == RUN and item.expected_version == run().version for item in all_controls
    )
    assert text.count("status only") == (0 if terminal else 4)


@pytest.mark.parametrize("size", [16, 50, 200])
@pytest.mark.parametrize(
    "collected,status", [(False, "running"), (False, "failed"), (True, "completed")]
)
@pytest.mark.parametrize("long_labels", [False, True])
async def test_large_fleet_text_keeps_the_maximum_complete_rows(
    size, collected, status, long_labels
):
    rows = tuple(
        worker(
            ordinal=index,
            role="**@everyone🙂" * 20 if long_labels else f"worker {index}",
            model="model_*🙂" * 20 if long_labels else "fixture-model",
        )
        for index in range(1, size + 1)
    )
    view = build_fleet_view(
        replace(run(), max_workers=size, status=status), rows, collected=collected
    )
    displays = [
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    ]
    body = displays[1]
    facts = [line.removeprefix("-# ") for line in body.split("\n\n")]
    summaries = [fact for fact in facts if re.fullmatch(r"\+\d+ more", fact)]
    status_rows = [fact for fact in facts if re.match(r"\d+\. \*\*", fact)]
    retained = len(status_rows)
    assert [int(fact.split(".", 1)[0]) for fact in status_rows] == list(range(1, retained + 1))
    assert all(" — pending" in fact for fact in status_rows)
    assert all("model" in fact for fact in status_rows)
    assert view.content_length() == sum(map(len, displays)) <= 4000
    assert view.total_children_count <= 40
    assert view.to_components()
    assert "@everyone" not in body
    if size == 16 or (size == 50 and not long_labels):
        assert retained == size
        assert summaries == []
    else:
        assert 0 < retained < size
        assert summaries == [f"+{size - retained} more"]
        assert facts[-1] == summaries[0]
        # One more complete row must exceed the limit, even after reducing
        # the omitted count. Rows that fit must not be silently discarded.
        next_row = status_rows[-1].replace(f"{retained}.", f"{retained + 1}.", 1)
        if not long_labels:
            next_row = next_row.replace(f"worker {retained}", f"worker {retained + 1}")
        next_body = "\n\n".join(
            f"-# {fact}" for fact in status_rows + [next_row, f"+{size - retained - 1} more"]
        )
        assert len(displays[0]) + len(next_body) + len(displays[-1]) > 4000
    all_controls = [
        item
        for item in view.walk_children()
        if isinstance(item, FleetControl) and item.worker_id is None
    ]
    assert {item.item.label for item in all_controls} == (
        {"Stop all", "Resume all"} if status == "running" else set()
    )


async def test_unrenderable_fleet_metadata_refuses_instead_of_exceeding_the_text_limit():
    from daimon.core.fleet_orchestration import FleetRefused

    with pytest.raises(FleetRefused, match="invalid_card"):
        build_fleet_view(replace(run(), total_budget_usd=Decimal("1" + "0" * 4000)), ())


@pytest.mark.parametrize("action", ["kill", "resume"])
async def test_run_level_control_uses_the_same_owner_and_run_compare_and_set(action):
    card, owner, _, send, _ = setup()
    await card.refresh()
    view = send.call_args.kwargs["view"]
    control = next(
        item
        for item in view.walk_children()
        if isinstance(item, FleetControl) and item.worker_id is None and item.action == action
    )
    await control.callback(interaction(card=card))
    owner.control.assert_awaited_once()
    assert owner.control.call_args.args[2] is None
    assert owner.control.call_args.kwargs["expected_version"] == run().version
    assert owner.control.call_args.kwargs["operation_key"] == f"discord:fleet:{RUN}:all:2:{action}"


@pytest.mark.parametrize("enabled", [False, True])
async def test_setup_registers_fleet_control_only_when_enabled(db_session_factory, enabled):
    from .harness import make_bot
    from .test_bot import _make_runtime

    runtime = _make_runtime(db_session_factory)
    runtime.settings.fleet.enabled = enabled
    bot = make_bot(runtime)
    bot.start_orphan_recovery = MagicMock()
    await bot.setup_hook()
    registry = bot._connection._view_store._dynamic_items
    assert (registry.get(FleetControl.__discord_ui_compiled_template__) is FleetControl) is enabled


async def test_all_three_backends_render_without_spec_or_result_material():
    rows = tuple(
        worker(provider, i + 1, spec={"credential": "DO_NOT_RENDER_SECRET"})
        for i, provider in enumerate(("anthropic", "openai", "gemini"))
    )
    view = build_fleet_view(run(), rows)
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert all(provider in text for provider in ("anthropic", "openai", "gemini"))
    assert "DO_NOT_RENDER_SECRET" not in text and "root-session" not in text
    assert "@everyone" not in text and "<#50>" not in text
    assert len(buttons(view)) == 6


def _texts(*mocks):
    return [str(c.kwargs.get("content", "")) for mock in mocks for c in mock.await_args_list]


def _headlines(*mocks):
    return [
        item.content
        for mock in mocks
        for c in mock.await_args_list
        if c.kwargs.get("view") is not None
        for item in c.kwargs["view"].walk_children()
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith("**Fleet")
    ]


def _collected(status, *, via_search=False, **reply):
    """The root's completed daimon fleet_collect call and the reply it read."""
    text = json.dumps({"run_id": str(RUN), "status": status, **reply})
    return ToolUseBlock(
        kind="tool_use",
        id=f"collect-{status}",
        type="agent.mcp_tool_use",
        name="call_tool" if via_search else "fleet_collect",
        input={"name": "fleet_collect", "arguments": {"run_id": str(RUN)}}
        if via_search
        else {"run_id": str(RUN)},
        mcp_server_name=DAIMON_SERVER_NAME,
        status="complete",
        result_content=[{"type": "text", "text": text}],
    )


def _message(identifier):
    value = MagicMock(spec=discord.Message)
    value.id = identifier
    return value


def _result_event(identifier, text):
    return Event(
        id=identifier,
        session_id=identifier,
        sequence=4,
        type="agent.message",
        turn_id="fixture-turn",
        observed_at=NOW,
        authority="record",
        native=NativeProvenance(provider="anthropic", api_revision="1"),
        payload={
            "item_id": identifier,
            "content": [{"type": "text", "text": text}],
            "complete": True,
        },
    )


async def test_waiting_collect_ends_the_turn_card_without_the_premature_answer():
    # r5 turn 1: workers still running when the root ended its turn. The
    # "Working on it…" card stayed at 0s; it now ends with a waiting notice.
    card, owner, _, send, edit = setup()
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    state = TurnState(content=[TextBlock(kind="text", text="A premature claimed summary")])
    await lifecycle.on_render(state)
    await lifecycle.on_terminal_success(state)
    owner.wait.assert_awaited_once()
    owner.settle_run.assert_not_awaited()
    messages = _texts(send, edit)
    assert not any("premature" in text for text in messages)
    (notice,) = [text for text in messages if "Fleet is still working" in text]
    # The answer message is never edited again: it names no worker state that
    # would read as stuck after the run ends (r14); the card carries progress.
    assert "pending" not in notice and "fleet card" in notice
    assert lifecycle.was_answered and lifecycle._terminal


@pytest.mark.parametrize("render_first", [False, True])
async def test_zero_worker_run_delivers_the_roots_answer_and_ends_the_fleet_card(render_first):
    # r2/r3/r4: the root spawned nothing, so "Working on it…" and
    # "Fleet — running" stayed stuck and its answer never reached the user.
    card, owner, _, send, edit = setup()
    owner.read_run.return_value = (run(), ())
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    state = TurnState(
        content=[TextBlock(kind="text", text="The fleet run didn't start: root_unavailable.")]
    )
    if render_first:
        await lifecycle.on_render(state)
    await lifecycle.on_terminal_success(state)
    assert sum("The fleet run didn't start" in text for text in _texts(send, edit)) == 1
    assert lifecycle.was_answered and lifecycle._terminal
    owner.settle_run.assert_awaited_once()
    assert owner.settle_run.await_args.args[2] == "cancelled"
    headlines = _headlines(send, edit)
    if render_first:
        assert headlines[-1] == "**Fleet — no workers started**"
    else:
        assert not headlines
    owner.wait.assert_not_awaited()
    owner.read_result.assert_not_awaited()


async def test_partial_result_heads_the_roots_answer_with_the_failed_worker():
    # r5 wake: W1 and W2 succeeded, W3 failed; the reply said only
    # "No reviewed result to share".
    card, owner, _, send, edit = setup()
    output = _result_event("worker-result", "Verified W1 result")
    pointer = ResultPointer(output.session_id, 4, result_digest(dict(output.payload)), "x")
    succeeded = worker(role="W1", status="succeeded", result=pointer)
    failed = worker(
        ordinal=2, role="W3", status="failed", error_code="dispatch_refused", backend="gemini"
    )
    owner.read_run.return_value = (run(), (succeeded, failed))
    owner.read_result.return_value = output
    owner.validate_contract.return_value = "Verified W1 result"
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    await lifecycle.on_render(TurnState())
    state = TurnState(
        content=[
            _collected("blocked", partial=True),
            TextBlock(kind="text", text="W1 says: verified. W3 failed."),
        ]
    )
    await lifecycle.on_terminal_success(state)
    final = [text for text in _texts(send, edit) if "W1 says: verified." in text]
    assert len(final) == 1
    assert final[0].startswith("Partial result: 1 of 2 workers succeeded.")
    assert "W3: failed: it couldn't start on that model here" in final[0] and "\\_" not in final[0]
    assert "No reviewed result to share." not in final[0]
    assert _headlines(send, edit)[-1] == "**Fleet — partial result**"
    assert owner.settle_run.await_args.args[2] == "failed"
    assert lifecycle.was_answered


WAKE = UUID(int=99)


def _card(owner, loader, send, edit, **kwargs):
    return DiscordFleetCard(
        run_id=None,
        service=FleetOrchestrator(owner),
        load_parent=loader,
        send=send,
        edit=edit,
        clock=lambda: NOW,
        **kwargs,
    )


@pytest.mark.parametrize("restarted", [False, True])
async def test_wake_turn_edits_the_fleet_card_its_root_turn_posted(restarted):
    # r5: the wake posted a second "Fleet — blocked" card and left the first
    # "Fleet — running" card stuck above it. After a restart the process map is
    # empty, so the run's card is found again by its run-scoped controls.
    _, owner, loader, _, _ = setup()
    posted: dict = {}
    first_message = _message(1)
    first_send = AsyncMock(return_value=first_message)
    first_edit = AsyncMock(side_effect=lambda message, **kwargs: message)
    first = _card(owner, loader, first_send, first_edit, posted=posted)
    root_turn = DiscordTurnLifecycle(
        send=first_send, edit=first_edit, agent_name="root", model_id="m", fleet_card=first
    )
    await root_turn.on_render(TurnState())
    await root_turn.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="ok")]))
    assert posted[RUN] is first_message
    owner.settle_run.assert_not_awaited()

    owner.read_run.return_value = (run(), (worker(status="failed", error_code="deadline_expired"),))
    find = AsyncMock(return_value=first_message)
    wake_send = AsyncMock(return_value=_message(2))
    wake_edit = AsyncMock(side_effect=lambda message, **kwargs: message)
    wake = _card(
        owner,
        loader,
        wake_send,
        wake_edit,
        posted={} if restarted else posted,
        find_posted=find,
        wake_id=WAKE,
    )
    wake_turn = DiscordTurnLifecycle(
        send=wake_send, edit=wake_edit, agent_name="root", model_id="m", fleet_card=wake
    )
    await wake_turn.on_render(TurnState())
    await wake_turn.on_terminal_success(
        TurnState(content=[_collected("blocked"), TextBlock(kind="text", text="x")])
    )
    assert find.await_count == (1 if restarted else 0)
    card_sends = [c for c in wake_send.await_args_list if c.kwargs.get("view") is not None]
    assert not card_sends
    adopted = [c for c in wake_edit.await_args_list if c.args and c.args[0] is first_message]
    assert adopted
    assert _headlines(wake_edit)[-1] == "**Fleet — blocked**"
    assert any("ran out of time" in text for text in _texts(wake_send, wake_edit))
    # The wake's own claim is kept for the wake owner to settle as delivered.
    assert owner.settle_run.await_args.kwargs == {"wake_id": WAKE, "unless_waking": False}


@pytest.mark.parametrize("owed", [0, 1])
async def test_result_finishing_during_the_roots_last_reply_is_left_to_its_wake(owed):
    # #1288 review: the root collected "waiting" and wrote "I'll report back";
    # the workers finished before the turn ended. That stale answer must not
    # become the final one, the run must stay open, and the wake must deliver.
    card, owner, _, send, edit = setup()
    output = _result_event("worker-result", "DO_NOT_POST_RESULT")
    pointer = ResultPointer(output.session_id, 4, result_digest(dict(output.payload)), "x")
    owner.read_run.return_value = (run(), (worker(status="succeeded", result=pointer),))
    owner.read_result.return_value = output
    owner.validate_contract.return_value = "DO_NOT_POST_RESULT"
    owner.pending_wakes.return_value = owed
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    await lifecycle.on_render(TurnState())
    state = TurnState(
        content=[
            _collected("waiting"),
            TextBlock(kind="text", text="STALE: I'll report back when they finish."),
        ]
    )
    await lifecycle.on_terminal_success(state)
    messages = _texts(send, edit)
    assert not any("STALE" in text or "DO_NOT_POST" in text for text in messages)
    assert any("I'll post the result here next." in text for text in messages)
    owner.settle_run.assert_not_awaited()
    # An owed wake is reused; otherwise one is armed for the finished workers.
    assert owner.wait.await_count == (0 if owed else 1)
    assert "**Fleet — collected**" not in _headlines(send, edit)
    assert lifecycle.was_answered and lifecycle._terminal


async def test_a_root_answer_after_its_terminal_collect_ends_the_run_and_retires_wakes():
    card, owner, _, send, edit = setup()
    output = _result_event("worker-result", "Verified result")
    pointer = ResultPointer(output.session_id, 4, result_digest(dict(output.payload)), "x")
    owner.read_run.return_value = (run(), (worker(status="succeeded", result=pointer),))
    owner.read_result.return_value = output
    owner.validate_contract.return_value = "Verified result"
    owner.pending_wakes.return_value = 1
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    await lifecycle.on_render(TurnState())
    state = TurnState(
        content=[
            _collected("waiting"),
            _collected("ready"),
            TextBlock(kind="text", text="The verified summary."),
        ]
    )
    await lifecycle.on_terminal_success(state)
    assert sum("The verified summary." in text for text in _texts(send, edit)) == 1
    owner.wait.assert_not_awaited()
    assert owner.settle_run.await_args.args[2] == "completed"
    assert owner.settle_run.await_args.kwargs == {"wake_id": None, "unless_waking": False}


@pytest.mark.parametrize("workers", ["none", "running", "finished"])
async def test_root_turn_error_ends_the_fleet_card_and_still_owes_the_result(workers):
    card, owner, _, send, edit = setup()
    rows = {
        "none": (),
        "running": (worker(status="running"),),
        "finished": (worker(status="failed", error_code="dispatch_refused"),),
    }[workers]
    owner.read_run.return_value = (run(), rows)
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    await lifecycle.on_render(TurnState())
    await lifecycle.on_terminal_failure(TurnState(), RuntimeError("fixture root failure"))
    headlines = _headlines(send, edit)
    if workers == "none":
        assert owner.settle_run.await_args.args[2] == "cancelled"
        assert headlines[-1] == "**Fleet — no workers started**"
        owner.wait.assert_not_awaited()
    else:
        owner.settle_run.assert_not_awaited()
        owner.wait.assert_awaited_once()
        assert headlines[-1] == "**Fleet — running**"
    owner.read_result.assert_not_awaited()


async def test_wake_turn_posts_its_own_card_when_the_earlier_one_is_gone():
    _, owner, loader, _, _ = setup()
    gone = _message(1)
    response = MagicMock(status=404, reason="Not Found")
    edit = AsyncMock(side_effect=discord.NotFound(response, "Unknown Message"))
    send = AsyncMock(return_value=_message(2))
    card = DiscordFleetCard(
        run_id=None,
        service=FleetOrchestrator(owner),
        load_parent=loader,
        send=send,
        edit=edit,
        clock=lambda: NOW,
        posted={RUN: gone},
    )
    assert await card.refresh()
    send.assert_awaited_once()


@pytest.mark.parametrize(
    "run_status,worker_status",
    [
        ("running", "failed"),
        ("failed", "starting"),
        ("cancelled", "cancelled"),
        ("collecting", "succeeded"),
    ],
)
@pytest.mark.parametrize("render_first", [False, True])
async def test_blocked_fleet_posts_only_status_notice_and_finishes_turn(
    run_status, worker_status, render_first
):
    card, owner, _, send, edit = setup()
    owner.read_run.return_value = (
        replace(run(), status=run_status),
        (worker(status=worker_status, result_contract={"review_required": True}),),
    )
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    state = TurnState(
        content=[
            TextBlock(kind="text", text="DO_NOT_POST_SEALED " * 80),
            ToolUseBlock(kind="tool_use", id="tool", type="agent.tool_use", name="read", input={}),
            _collected("blocked"),
            TextBlock(kind="text", text="DO_NOT_POST_FINAL"),
        ]
    )
    if render_first:
        await lifecycle.on_render(state)
    await lifecycle.on_terminal_success(state)
    messages = [
        str(c.kwargs.get("content", "")) for c in send.await_args_list + edit.await_args_list
    ]
    assert sum("No reviewed result to share." in text for text in messages) == 1
    assert not any(
        "DO_NOT_POST" in text or "@everyone" in text or "<#50>" in text for text in messages
    )
    assert any(worker_status in text for text in messages)
    assert lifecycle.was_answered and lifecycle._terminal
    views = [
        c.kwargs["view"]
        for c in send.await_args_list + edit.await_args_list
        if c.kwargs.get("view")
    ]
    blocked = [
        v
        for v in views
        if any(
            "Fleet — blocked" in item.content
            for item in v.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
    ]
    assert blocked
    if run_status in ("failed", "cancelled"):
        assert not any(isinstance(item, FleetControl) for item in blocked[-1].walk_children())
    owner.read_result.assert_not_awaited()


@pytest.mark.parametrize("worker_status", [None, "pending", "failed"])
async def test_terminal_failure_sealed_answers_obey_fleet_disclosure_policy(worker_status):
    card, owner, _, send, edit = setup()
    owner.read_run.return_value = (run(), (worker(status=worker_status or "pending"),))
    lifecycle = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        agent_name="root",
        model_id="fixture-model",
        fleet_card=card if worker_status is not None else None,
    )
    sealed = "An unreviewed pre-tool answer. " * 30
    state = TurnState(
        content=[
            TextBlock(kind="text", text=sealed),
            ToolUseBlock(
                kind="tool_use", id="trailing-tool", type="agent.tool_use", name="read", input={}
            ),
        ]
    )
    await lifecycle.on_terminal_failure(state, RuntimeError("fixture worker failure"))
    messages = [
        str(call.kwargs.get("content", "")) for call in send.await_args_list + edit.await_args_list
    ]
    assert any(sealed in message for message in messages) is (worker_status is None)
    assert any("embeds" in call.kwargs for call in send.await_args_list + edit.await_args_list)
    owner.read_result.assert_not_awaited()
    # The failed root is still owed its workers' result through a wake.
    assert owner.wait.await_count == (worker_status is not None)
    assert not lifecycle.was_answered


@pytest.mark.parametrize("verdict", ["approved", "refused"])
async def test_actual_review_barrier_controls_the_existing_summary_delivery(verdict):
    card, owner, _, send, edit = setup()

    def result(identifier, text):
        return Event(
            id=identifier,
            session_id=identifier,
            sequence=4,
            type="agent.message",
            turn_id="fixture-turn",
            observed_at=NOW,
            authority="record",
            native=NativeProvenance(provider="anthropic", api_revision="1"),
            payload={
                "item_id": identifier,
                "content": [{"type": "text", "text": text}],
                "complete": True,
            },
        )

    output = result("worker-result", "Verified primary result")
    digest = result_digest(dict(output.payload))
    review = result(
        "review-result", json.dumps({"verdict": verdict, "reviewed_digest": digest})
    ).model_copy(update={"native": NativeProvenance(provider="openai", api_revision="1")})
    primary = worker(
        status="succeeded",
        result_contract={"review_required": True},
        result=ResultPointer(output.session_id, 4, digest, "DO_NOT_POST_EXCERPT"),
    )
    reviewer = worker(
        ordinal=2,
        status="succeeded",
        backend="openai",
        profile=backend_selection("openai").profile,
        model=backend_selection("openai").model,
        reviewer_of=primary.id,
        result_contract={"format": "review", "reviewed_digest": digest},
        result=ResultPointer(
            review.session_id, 4, result_digest(dict(review.payload)), "DO_NOT_POST_REVIEW"
        ),
    )
    owner.read_run.return_value = (run(), (primary, reviewer))
    owner.read_result.side_effect = lambda _parent, _policy, _run, row: (
        output if row.id == primary.id else review
    )
    owner.validate_contract.return_value = "Verified primary result"
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    await lifecycle.on_render(TurnState())
    send.reset_mock()
    state = TurnState(
        content=[
            _collected("ready" if verdict == "approved" else "blocked", via_search=True),
            TextBlock(kind="text", text="The orchestrator's one final summary."),
        ]
    )
    await lifecycle.on_terminal_success(state)
    messages = [
        str(call.kwargs.get("content", "")) for call in send.await_args_list + edit.await_args_list
    ]
    final = [text for text in messages if "The orchestrator's one final summary." in text]
    assert len(final) == (1 if verdict == "approved" else 0)
    assert not any("DO_NOT_POST" in text or "reviewed_digest" in text for text in messages)
    assert lifecycle.was_answered
    assert any("No reviewed result to share." in text for text in messages) is (
        verdict == "refused"
    )


def test_real_bot_flag_off_does_not_construct_a_sender_or_fleet_service():
    from daimon.adapters.discord.bot import DaimonBot

    bot = MagicMock()
    bot.runtime.fleet_card_factory = MagicMock(side_effect=AssertionError("must stay cold"))
    bot.runtime.settings.fleet.enabled = False
    sender = MagicMock(side_effect=AssertionError("must stay cold"))
    assert (
        DaimonBot.fleet_card_for(
            bot, thread=MagicMock(), requester_id=123, send=sender, edit=AsyncMock()
        )
        is None
    )
    sender.assert_not_called()
    bot.runtime.fleet_card_factory.assert_not_called()


@pytest.mark.parametrize("recovered_attribution", [False, True])
async def test_real_webhook_replacement_records_both_accepted_card_messages(
    db_committing_engine, db_clean, monkeypatch, recovered_attribution
):
    from daimon.adapters.discord import post_transport
    from daimon.adapters.discord.post_transport import DiscordPostTransport
    from daimon.adapters.discord.turn_posts import TurnPostRecorder
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.agent_posts import get_post
    from daimon.core.stores.fleet import add_worker, create_run, run_status
    from daimon.core.stores.turn_card_intents import create_turn_card_intent
    from daimon.testing.factories import make_account, make_tenant

    from .harness import make_bot
    from .test_bot import _make_runtime

    sessionmaker = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=TENANT, platform="discord", workspace_id="60")
        await make_account(session, id=ACCOUNT, tenant=tenant)
        intent = await create_turn_card_intent(
            session,
            tenant_id=TENANT,
            platform="discord",
            thread_id="70",
            channel_id="50",
            turn_token=UUID(int=71),
        )
    deadline = datetime.now(UTC) + timedelta(minutes=10)
    fleet = await create_run(
        sessionmaker,
        tenant_id=TENANT,
        orchestrator_account_id=ACCOUNT,
        orchestrator_session_id="root-session",
        orchestrator_binding_id="root-binding",
        platform="discord",
        channel_id="50",
        thread_id="70",
        operation_key="card-transport",
        total_budget_usd=Decimal("1"),
        deadline_at=deadline,
        max_workers=3,
        created_by="123",
    )
    for ordinal, provider in enumerate(("anthropic", "openai", "gemini")):
        row = worker(provider, ordinal)
        await add_worker(
            sessionmaker,
            tenant_id=TENANT,
            run_id=fleet.id,
            ordinal=ordinal,
            role=row.role,
            spec=row.spec,
            spec_digest=row.spec_digest,
            backend=row.backend,
            profile=row.profile,
            model=row.model,
            budget_usd=row.budget_usd,
            deadline_at=deadline,
            max_turns=row.max_turns,
        )

    bot = make_bot(_make_runtime(sessionmaker))
    bot._connection.application_id = 999
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id, thread.locked = 70, 50, False
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 50
    thread.parent = channel
    thread.guild.id = channel.guild.id = 60
    channel.guild.me = MagicMock(spec=discord.Member)
    channel.permissions_for.return_value.manage_webhooks = True
    bot.http.channel_webhooks = AsyncMock(return_value=[])
    hooks = [
        discord.Webhook.from_state(
            {
                "id": str(webhook_id),
                "type": 1,
                "token": "offline-webhook-token",
                "channel_id": "50",
                "guild_id": "60",
                "name": "Root",
            },
            bot._connection,
        )
        for webhook_id in (500, 600)
    ]
    channel.create_webhook = AsyncMock(side_effect=hooks)
    monkeypatch.setattr(post_transport, "_webhooks", {})
    monkeypatch.setattr(post_transport, "_creation_tasks", {})
    monkeypatch.setattr(post_transport, "_locks", {})
    monkeypatch.setattr(post_transport, "_unavailable_until", {})
    monkeypatch.setattr(post_transport, "_send_unavailable_until", {})

    class Gateway(AsyncWebhookAdapter):
        def __init__(self):
            super().__init__()
            self.calls = []
            self.accepted = {}

        def reply(self, webhook_id, message_id, payload):
            return {
                "id": str(message_id),
                "type": 0,
                "channel_id": "70",
                "webhook_id": str(webhook_id),
                "content": "",
                "author": {
                    "id": str(webhook_id),
                    "username": "Root",
                    "discriminator": "0",
                    "avatar": None,
                },
                "flags": payload.get("flags", 32768),
                "timestamp": NOW.isoformat(),
                "edited_timestamp": None,
                "pinned": False,
                "mention_everyone": False,
                "tts": False,
                "attachments": [],
                "embeds": [],
                "mentions": [],
                "mention_roles": [],
                "components": payload["components"],
            }

        async def execute_webhook(self, webhook_id, token, **kwargs):
            assert kwargs["wait"] is True and kwargs["thread_id"] == 70
            payload = json.loads(json.dumps(kwargs["payload"]))
            message_id = 900 + len(self.accepted)
            self.calls.append(("send", webhook_id, message_id))
            self.accepted[message_id] = self.reply(webhook_id, message_id, payload)
            return self.accepted[message_id]

        async def edit_webhook_message(self, webhook_id, token, message_id, **kwargs):
            self.calls.append(("edit", webhook_id, message_id))
            if webhook_id == 500:
                raise discord.NotFound(
                    MagicMock(status=404, reason="Not Found"),
                    {"code": 10015, "message": "Unknown Webhook"},
                )
            assert message_id == 901 and kwargs["thread_id"] == 70
            return self.reply(webhook_id, message_id, kwargs["payload"])

    owner = create_autospec(FleetPorts, instance=True)

    async def read_run(scope, run_id):
        return await run_status(sessionmaker, tenant_id=UUID(scope.tenant_id), run_id=run_id)

    owner.read_run.side_effect = read_run
    authority = parent()
    authority = replace(authority, fleet=replace(authority.fleet, run_id=fleet.id))
    recorder = TurnPostRecorder(sessionmaker, TENANT, "root-agent", 123)
    transport = DiscordPostTransport(
        bot, thread, name="Root", avatar_url=None, builtin=False, identity_enabled=True
    )

    async def record_replacement(message):
        if not recovered_attribution:
            await recorder.message(thread, message, turn_card_intent_id=intent.id)
            return
        from daimon.adapters.discord.turn_posts import record_replacement_post
        from daimon.core import channel_tidy

        async with sessionmaker() as session:
            captured = await get_post(
                session, tenant_id=TENANT, platform="discord", channel_id="70", message_id="900"
            )
        assert captured is not None
        with monkeypatch.context() as patch:
            patch.setattr(
                channel_tidy,
                "derive_agent_uuid",
                MagicMock(
                    side_effect=AssertionError("stored attribution must not derive a native agent")
                ),
            )
            await record_replacement_post(sessionmaker, captured, message)

    async def resolve_root(destination, actor_id, intent_id, run_id, interaction):
        assert destination is thread and actor_id == 123
        assert intent_id == intent.id and run_id in (None, fleet.id) and interaction is None
        return authority, POLICY

    factory = DiscordFleetCardFactory(FleetOrchestrator(owner), resolve_root)
    card = factory(
        thread,
        123,
        recorder.sender(thread, turn_card_intent_id=intent.id, transport=transport),
        transport.edit,
        turn_card_intent_id=intent.id,
        on_replacement=record_replacement,
    )
    assert card is not None
    gateway = Gateway()
    token = async_context.set(gateway)
    try:
        await card.refresh()
        await card.refresh()
        await card.refresh()
    finally:
        async_context.reset(token)
    assert gateway.calls == [
        ("send", 500, 900),
        ("edit", 500, 900),
        ("send", 600, 901),
        ("edit", 600, 901),
    ]
    async with sessionmaker() as session:
        posts = [
            await get_post(
                session,
                tenant_id=TENANT,
                platform="discord",
                channel_id="70",
                message_id=str(message_id),
            )
            for message_id in (900, 901)
        ]
    assert all(post is not None for post in posts)
    assert all(
        post.turn_card_intent_id == intent.id
        and post.requester_platform_user_id == "123"
        and post.agent_id == derive_agent_uuid(tenant_id=TENANT, ma_agent_id="root-agent")
        for post in posts
    )
    for payload in gateway.accepted.values():
        text = json.dumps(payload["components"], ensure_ascii=False)
        assert all(provider in text for provider in ("anthropic", "openai", "gemini"))
        assert "pending" in text
        assert payload["flags"] & 32768


async def test_factory_defers_then_binds_the_exact_authoritative_run():
    _, owner, _, send, edit = setup()
    resolver = AsyncMock(side_effect=[None, (parent(), POLICY), (parent(), POLICY)])
    factory = DiscordFleetCardFactory(FleetOrchestrator(owner), resolver)
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 70, 50
    thread.guild.id = 60
    intent = UUID(int=72)
    card = factory(thread, 123, send, edit, turn_card_intent_id=intent)
    assert card is not None and card.run_id is None
    resolver.assert_not_awaited()
    assert await card.refresh() is False
    send.assert_not_awaited()
    owner.read_run.assert_not_awaited()
    assert await card.refresh() is True
    assert card.run_id == RUN
    assert await card.refresh() is True
    assert resolver.await_args_list[-1].args == (thread, 123, intent, RUN, None)
    send.assert_awaited_once()
    edit.assert_awaited_once()


@pytest.mark.parametrize("mutation", ["user", "tenant", "thread", "channel", "missing"])
async def test_recovered_factory_refuses_foreign_or_missing_projection_before_posts(mutation):
    from daimon.core.fleet_orchestration import FleetRefused

    _, owner, _, send, edit = setup()
    authority = parent()
    if mutation == "user":
        authority = replace(authority, subject=Subject(platform_user_id="456"))
    thread = MagicMock(spec=discord.Thread)
    thread.id = 71 if mutation == "thread" else 70
    thread.parent_id = 51 if mutation == "channel" else 50
    thread.guild.id = 61 if mutation == "tenant" else 60
    resolver = AsyncMock(return_value=None if mutation == "missing" else (authority, POLICY))
    card = DiscordFleetCardFactory(FleetOrchestrator(owner), resolver)(
        thread, 123, send, edit, run_id=RUN
    )
    assert card is not None
    with pytest.raises(FleetRefused, match="foreign_run"):
        await card.refresh()
    owner.read_run.assert_not_awaited()
    send.assert_not_awaited()
    edit.assert_not_awaited()


async def test_unbound_factory_absence_allows_only_an_ordinary_final_answer():
    _, owner, _, send, edit = setup()
    resolver = AsyncMock(return_value=None)
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 70, 50
    thread.guild.id = 60
    card = DiscordFleetCardFactory(FleetOrchestrator(owner), resolver)(thread, 123, send, edit)
    assert card is not None and await card.allow_summary()
    owner.read_run.assert_not_awaited()
    owner.read_result.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.parametrize("terminal", ["success", "failure"])
@pytest.mark.parametrize("render_first", [False, True])
async def test_installed_factory_proven_absence_preserves_complete_lifecycle_delivery(
    terminal, render_first
):
    """Compare the actual lifecycle's full delivery trace, including sealed text."""
    state = TurnState(
        content=[
            TextBlock(kind="text", text="An ordinary sealed answer. " * 30),
            ToolUseBlock(
                kind="tool_use", id="trailing-tool", type="agent.tool_use", name="read", input={}
            ),
            TextBlock(kind="text", text="The ordinary final recap."),
        ]
    )

    async def deliver(installed):
        _, owner, _, send, edit = setup()
        resolver = AsyncMock(return_value=None)
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.parent_id = 70, 50
        thread.guild.id = 60
        card = (
            DiscordFleetCardFactory(FleetOrchestrator(owner), resolver)(
                thread, 123, send, edit, turn_card_intent_id=UUID(int=72)
            )
            if installed
            else None
        )
        trace = []

        async def deliver_message(operation, args, kwargs):
            payload = dict(kwargs)
            if "embeds" in payload:
                payload["embeds"] = [embed.to_dict() for embed in payload["embeds"]]
            if payload.get("allowed_mentions") is not None:
                payload["allowed_mentions"] = payload["allowed_mentions"].to_dict()
            if payload.get("view") is not None:
                payload["view"] = payload["view"].to_components()
            trace.append((operation, tuple(message.id for message in args), payload))
            message = MagicMock(spec=discord.Message)
            message.id = 900 + len(trace)
            return message

        async def send_message(*args, **kwargs):
            return await deliver_message("send", args, kwargs)

        async def edit_message(*args, **kwargs):
            return await deliver_message("edit", args, kwargs)

        send.side_effect, edit.side_effect = send_message, edit_message
        lifecycle = DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="root",
            model_id="fixture-model",
            fleet_card=card,
            clock=lambda: 1000.0,
            request_id=lambda: "fixture-request",
        )
        if render_first:
            await lifecycle.on_render(state)
        if terminal == "success":
            await lifecycle.on_terminal_success(state)
        else:
            await lifecycle.on_terminal_failure(state, RuntimeError("fixture failure"))
        owner.read_run.assert_not_awaited()
        owner.read_result.assert_not_awaited()
        owner.wait.assert_not_awaited()
        if installed:
            assert resolver.await_count > 0
        return trace, lifecycle.was_answered

    ordinary = await deliver(False)
    installed = await deliver(True)
    assert installed == ordinary
    assert (
        sum("An ordinary sealed answer." in row[2].get("content", "") for row in installed[0]) == 1
    )


@pytest.mark.parametrize("authority", ["unresolved", "foreign", "active", "failed"])
async def test_installed_factory_never_discloses_sealed_text_without_proven_absence(authority):
    from daimon.core.fleet_orchestration import FleetRefused

    _, owner, _, send, edit = setup()
    root = parent()
    if authority == "foreign":
        root = replace(root, subject=Subject(platform_user_id="456"))
    resolver = AsyncMock(return_value=(root, POLICY))
    if authority == "unresolved":
        resolver.side_effect = FleetRefused("prerequisites_unavailable")
    if authority == "failed":
        owner.read_run.return_value = (run(), (worker(status="failed"),))
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 70, 50
    thread.guild.id = 60
    card = DiscordFleetCardFactory(FleetOrchestrator(owner), resolver)(thread, 123, send, edit)
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    sealed = "An unreviewed sealed answer. " * 30
    state = TurnState(
        content=[
            TextBlock(kind="text", text=sealed),
            ToolUseBlock(kind="tool_use", id="tool", type="agent.tool_use", name="read", input={}),
        ]
    )
    await lifecycle.on_terminal_failure(state, RuntimeError("fixture failure"))
    assert not any(
        sealed in call.kwargs.get("content", "")
        for call in send.await_args_list + edit.await_args_list
    )
    owner.read_result.assert_not_awaited()
    assert owner.wait.await_count == (authority in ("active", "failed"))


@pytest.mark.parametrize("enabled", [False, True])
async def test_runtime_installs_factory_only_with_opted_in_owner_bindings(
    db_committing_engine, monkeypatch, enabled
):
    from pathlib import Path

    from daimon.adapters.discord import runtime as runtime_module
    from daimon.core.config import Settings
    from mux.testing.drivers.anthropic.ma import build_stub_anthropic

    settings = Settings.model_validate(
        {
            "database": {
                "url": db_committing_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline-test-key"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
            "fleet": {"enabled": enabled},
        }
    )
    # Exercise the real runtime bootstrap and PG owner; only external SDK
    # construction uses the shared isolated client, with no ambient credentials.
    monkeypatch.setattr(runtime_module, "AsyncAnthropic", lambda **kwargs: build_stub_anthropic())
    owner = create_autospec(FleetPorts, instance=True)
    resolver = AsyncMock()
    grants = AsyncMock(side_effect=AssertionError("bootstrap must not discover root grants"))
    async with runtime_module.build_runtime(
        settings,
        fleet_service=FleetOrchestrator(owner),
        fleet_parent_loader=resolver,
        fleet_root_grants=grants,
    ) as runtime:
        assert isinstance(runtime.fleet_card_factory, DiscordFleetCardFactory) is enabled
        resolver.assert_not_awaited()
        grants.assert_not_awaited()
        assert (runtime.turn_deps.state_store is None) is (not enabled)
        assert runtime.turn_deps.fleet_root_grants is (grants if enabled else None)


@pytest.mark.parametrize("failure", ["write", "timeout"])
async def test_replacement_capture_uses_the_owners_failure_policy(
    db_session_factory, monkeypatch, failure
):
    from daimon.adapters.discord.turn_posts import TurnPostRecorder, record_replacement_post
    from daimon.core import channel_tidy
    from daimon.core.stores.agent_posts import get_post
    from daimon.testing.factories import make_tenant
    from structlog.testing import capture_logs

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 70, 50
    original = MagicMock(spec=discord.Message)
    original.id = 900
    recorder = TurnPostRecorder(db_session_factory, tenant.id, "root-agent", 123)
    await recorder.message(thread, original, turn_card_intent_id=UUID(int=72))
    async with db_session_factory() as session:
        captured = await get_post(
            session, tenant_id=tenant.id, platform="discord", channel_id="70", message_id="900"
        )
    assert captured is not None
    replacement = MagicMock(spec=discord.Message)
    replacement.id = 901

    entered = asyncio.Event()

    async def blocked_write(*args, **kwargs):
        entered.set()
        if failure == "write":
            raise RuntimeError("injected store failure")
        await asyncio.Event().wait()

    monkeypatch.setattr(channel_tidy, "record_post", blocked_write)
    monkeypatch.setattr(channel_tidy, "RECORD_TIMEOUT_S", 1.0)
    with capture_logs() as logs:
        await record_replacement_post(db_session_factory, captured, replacement)
    assert entered.is_set()
    assert logs == [
        {
            "event": "channel_tidy.turn_record_failed",
            "log_level": "warning",
            "tenant_id": str(tenant.id),
            "platform": "discord",
            "source": "turn",
            "error_type": "RuntimeError" if failure == "write" else "TimeoutError",
        }
    ]
    async with db_session_factory() as session:
        assert (
            await get_post(
                session, tenant_id=tenant.id, platform="discord", channel_id="70", message_id="901"
            )
            is None
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": UUID(int=999)},
        {"platform": "slack"},
        {"channel_id": "71"},
        {"requester_platform_user_id": "456"},
        {"source": "auto_thread"},
        {"turn_card_intent_id": UUID(int=73)},
        {"parent_channel_id": "51"},
        {"thread_ts": "foreign-thread"},
        {"ma_agent_id": "do-not-infer-native-id"},
        {"recorded_intent": None},
    ],
)
async def test_stored_replacement_attribution_cannot_be_overridden_before_writing(overrides):
    from daimon.core.channel_tidy import record_turn_post
    from daimon.core.stores.agent_posts import AgentPostRow

    overrides = dict(overrides)
    intent = overrides.pop("recorded_intent", UUID(int=72))
    stored = AgentPostRow(
        id=UUID(int=80),
        tenant_id=TENANT,
        platform="discord",
        channel_id="70",
        message_id="900",
        parent_channel_id="50",
        thread_ts=None,
        kind="message",
        agent_id=UUID(int=44),
        content_hmac=None,
        source="turn",
        requester_platform_user_id="123",
        turn_card_intent_id=intent,
        posted_at=NOW,
        deleted_at=None,
    )
    sessionmaker = MagicMock(spec=async_sessionmaker)
    with pytest.raises(ValueError, match="recorded attribution|must name its turn"):
        await record_turn_post(sessionmaker, recorded_post=stored, message_id="901", **overrides)
    sessionmaker.begin.assert_not_called()


@pytest.fixture
async def discord_frozen_root(db_preflight, db_committing_engine, db_clean, monkeypatch, request):
    """Reuse the root owner's SDK/PG fixture with Discord destination metadata."""
    from packages.core.tests.turn import test_frozen_root as root_owner

    make_tenant = root_owner.make_tenant
    make_principal = root_owner.make_platform_principal
    root_permissions = root_owner.parent
    subject = root_owner.Subject

    async def tenant(db, **kwargs):
        return await make_tenant(db, id=TENANT, platform="discord", workspace_id="60", **kwargs)

    async def principal(db, **kwargs):
        kwargs["external_id"] = "123"
        return await make_principal(db, **kwargs)

    def original_subject(**kwargs):
        if kwargs.get("platform_user_id") == "requester":
            kwargs["platform_user_id"] = "123"
        return subject(**kwargs)

    def permissions(provider):
        value = root_permissions(provider)
        destination = value.fleet.root.model_copy(
            update={
                "channel": value.fleet.root.channel.model_copy(update={"channel_id": "50"}),
                "thread_id": "70",
            }
        )
        return replace(
            value,
            fleet=replace(
                value.fleet, root=destination, place=Place(channel_id="70", parent_channel_id="50")
            ),
        )

    monkeypatch.setattr(root_owner, "make_tenant", tenant)
    monkeypatch.setattr(root_owner, "make_platform_principal", principal)
    monkeypatch.setattr(root_owner, "Subject", original_subject)
    monkeypatch.setattr(root_owner, "parent", permissions)
    async for value in root_owner.root_turn.__wrapped__(db_preflight, db_committing_engine):
        if getattr(request, "param", True):
            await root_owner.freeze(value)
        yield value


@pytest.mark.parametrize("actor", ["owner", "admin", "foreign_account", "unbound"])
async def test_runtime_binds_real_frozen_root_without_a_caller_permission_projection(
    discord_frozen_root, db_committing_engine, db_schema, monkeypatch, actor
):
    from pathlib import Path

    from daimon.adapters.discord import runtime as runtime_module
    from daimon.adapters.discord.turn_posts import TurnPostRecorder
    from daimon.core.config import Settings
    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.stores import fleet, tenants
    from daimon.core.stores.agent_posts import get_post
    from daimon.core.turn.runtime_manifest import read_frozen_root
    from daimon.testing.db import build_test_engine
    from daimon.testing.factories import make_platform_principal
    from mux.testing.drivers.anthropic.ma import build_stub_anthropic

    deps, prepared, _, intent, _, scope, _, _ = discord_frozen_root
    original, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    ports = HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal("1"), deps=deps)
    shared = FleetOrchestrator(ports)
    if actor == "foreign_account":
        async with deps.sessionmaker.begin() as db:
            tenant = await tenants.get_tenant(db, TENANT)
            assert tenant is not None
            await make_platform_principal(db, tenant=tenant, platform="discord", external_id="456")
    settings = Settings.model_validate(
        {
            "database": {
                "url": db_committing_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline-test-key"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
            "fleet": {"enabled": True},
            "turn": {"path": "mux"},
        }
    )
    monkeypatch.setattr(runtime_module, "AsyncAnthropic", lambda **kwargs: build_stub_anthropic())
    # Real PG connections use this process-owned schema, just as the shared fixture.
    monkeypatch.setattr(
        runtime_module,
        "build_engine",
        lambda url, **kwargs: build_test_engine(url, db_schema, **kwargs),
    )
    async with runtime_module.build_runtime(
        settings,
        fleet_service=shared,
        fleet_parent_loader=DiscordFleetCardFactory.from_runtime(shared, deps)._resolve,
    ) as runtime:
        assert runtime.turn_deps.fleet_root_grants is deps.fleet_root_grants
        assert isinstance(runtime.fleet_card_factory, DiscordFleetCardFactory)
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.parent_id = 70, 50
        thread.guild.id = 60
        accepted = MagicMock(spec=discord.Message)
        accepted.id = 900
        thread.send = AsyncMock(return_value=accepted)
        recorder = TurnPostRecorder(runtime.sessionmaker, TENANT, prepared.admission.agent.id, 123)
        card = runtime.fleet_card_factory(
            thread,
            123,
            recorder.sender(thread, turn_card_intent_id=intent.id),
            AsyncMock(),
            turn_card_intent_id=intent.id,
        )
        assert card is not None and await card.refresh()
        assert card.run_id == original.fleet.run_id
        thread.send.assert_awaited_once()
        async with runtime.sessionmaker() as db:
            captured = await get_post(
                db, tenant_id=TENANT, platform="discord", channel_id="70", message_id="900"
            )
        assert captured is not None
        assert captured.agent_id == original.agent_id
        assert (
            captured.turn_card_intent_id == intent.id
            and captured.requester_platform_user_id == "123"
        )
        preserved, _ = await read_frozen_root(
            runtime.turn_deps, scope, intent_id=intent.id, now=datetime.now(UTC)
        )
        assert preserved.fleet.scope == original.fleet.scope

        # Restart recovery uses the actual bot + stored post, with current actor
        # authorization owned by the shared ports and original scope preserved.
        from .harness import make_bot

        bot = make_bot(runtime)
        actor_id = 123 if actor == "owner" else 456
        click = interaction(actor_id)
        click.client, click.channel, click.message = bot, thread, accepted
        accepted.webhook_id = None
        accepted.author = MagicMock(spec=discord.User)
        accepted.author.name = "root"
        click.user = MagicMock(spec=discord.Member)
        click.user.id = actor_id
        click.user.roles = []
        click.user.guild.owner_id = 999
        click.user.guild_permissions.administrator = actor == "admin"
        click.user.guild_permissions.manage_guild = False
        recovered = await bot.recover_fleet_card(click, run_id=original.fleet.run_id)
        assert recovered is not None
        control = AsyncMock(wraps=shared.control)
        monkeypatch.setattr(shared, "control", control)
        before, _ = await fleet.run_status(
            deps.sessionmaker, tenant_id=TENANT, run_id=original.fleet.run_id
        )
        authorized = actor in ("owner", "admin")
        assert (
            await recovered.control(
                click, worker_id=None, action="kill", expected_version=before.version
            )
            is authorized
        )
        control.assert_awaited_once()
        current = control.await_args.args[0]
        assert current.subject.platform_user_id == str(actor_id)
        assert current.subject.is_admin is (actor == "admin")
        assert current.fleet.scope == original.fleet.scope
        assert current.fleet.account_id == original.fleet.account_id
        after, _ = await fleet.run_status(
            deps.sessionmaker, tenant_id=TENANT, run_id=original.fleet.run_id
        )
        assert after.status == ("cancelled" if authorized else before.status)
        assert after.version == before.version + int(authorized)
        if authorized:
            click.response.defer.assert_awaited_once()
            click.response.send_message.assert_not_awaited()
        else:
            click.response.send_message.assert_awaited_once()
            click.response.defer.assert_not_awaited()


@pytest.mark.parametrize("fault", ["intent", "run", "requester", "origin", "thread", "guild"])
async def test_real_runtime_root_refuses_retargeting_before_a_card_post(discord_frozen_root, fault):
    from uuid import uuid4

    from daimon.core.fleet_orchestration import FleetRefused, HostFleetPorts
    from daimon.core.stores.turn_origins import delete_origin

    deps, _, _, intent, origin, _, _, _ = discord_frozen_root
    if fault == "origin":
        async with deps.sessionmaker.begin() as db:
            await delete_origin(db, origin_id=origin.id)
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = (71 if fault == "thread" else 70), 50
    thread.guild.id = 61 if fault == "guild" else 60
    send = AsyncMock()
    shared = FleetOrchestrator(HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal("1")))
    card = DiscordFleetCardFactory.from_runtime(shared, deps)(
        thread,
        456 if fault == "requester" else 123,
        send,
        AsyncMock(),
        turn_card_intent_id=uuid4() if fault == "intent" else intent.id,
        run_id=uuid4() if fault == "run" else None,
    )
    assert card is not None
    with pytest.raises(FleetRefused):
        await card.refresh()
    send.assert_not_awaited()


@pytest.mark.parametrize("admin", [False, True])
async def test_real_runtime_refreshes_click_subject_without_replacing_root_scope(
    discord_frozen_root, admin
):
    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.turn.runtime_manifest import read_frozen_root

    deps, _, _, intent, _, scope, _, _ = discord_frozen_root
    original, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    shared = FleetOrchestrator(HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal("1")))
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 70, 50
    thread.guild.id = 60
    factory = DiscordFleetCardFactory.from_runtime(shared, deps)
    click = interaction(456)
    click.user = MagicMock(spec=discord.Member)
    click.user.id = 456
    click.user.roles = []
    click.user.guild.owner_id = 999
    click.user.guild_permissions.administrator = admin
    click.user.guild_permissions.manage_guild = False
    current, _ = await factory._resolve(thread, 123, intent.id, original.fleet.run_id, click)
    assert current.subject.platform_user_id == "456" and current.subject.is_admin is admin
    assert current.fleet.scope == original.fleet.scope
    assert current.fleet.account_id == original.fleet.account_id
    assert current.fleet.scope.principal_id == "daimon"
    assert (
        current.limits == original.limits and current.source_revisions == original.source_revisions
    )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("supplied_prices", [False, True])
async def test_runtime_constructs_one_scoped_fleet_owner_and_drains_before_clients(
    db_committing_engine, monkeypatch, enabled, supplied_prices
):
    from pathlib import Path

    from daimon.adapters.discord import runtime as runtime_module
    from daimon.core.config import Settings
    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.pricing import MODEL_PRICING
    from mux.testing.drivers.anthropic.ma import build_stub_anthropic

    settings = Settings.model_validate(
        {
            "database": {
                "url": db_committing_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline-test-key"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
            "turn": {"path": "mux", "channel_backends": True, "agent_backends": True},
            "fleet": {"enabled": enabled},
        }
    )
    client = build_stub_anthropic()
    monkeypatch.setattr(runtime_module, "AsyncAnthropic", lambda **kwargs: client)
    prices = {("anthropic", model): rate for model, rate in MODEL_PRICING.items()}
    from datetime import date

    from daimon.core.pricing import ProviderPrice
    from daimon.core.turn import runtimes as runtime_registry

    native = ProviderPrice(
        "openai", "gpt-6-luna", date(2026, 10, 12), Decimal("1"), Decimal("2"), Decimal("0.5")
    )
    tariff_reads = []

    def no_native_client(request):
        raise AssertionError("bootstrap must not construct a foreign provider client")

    def tariffs():
        tariff_reads.append("read")
        return {("openai", "gpt-6-luna"): native}

    monkeypatch.setattr(runtime_registry, "_RUNTIME_FACTORIES", {})
    # This fixture supplies its own tariffs; installed providers are tested separately.
    monkeypatch.setattr(runtime_registry, "entry_points", lambda **kwargs: ())
    runtime_registry.register_channel_runtime(
        "openai.persistent_workspace", no_native_client, tariffs=tariffs
    )
    expected = prices | {("openai", "gpt-6-luna"): native}
    events = []
    engines = []
    build = runtime_module.build_engine
    close = HostFleetPorts.close
    from sqlalchemy.ext.asyncio import AsyncEngine

    dispose = AsyncEngine.dispose

    def observe_engine(*args, **kwargs):
        engine = build(*args, **kwargs)
        engines.append(engine)
        return engine

    async def observe_dispose(engine, *args, **kwargs):
        if engine in engines:
            events.append(f"engine-{engines.index(engine)}")
        await dispose(engine, *args, **kwargs)

    async def observe_close(owner):
        assert owner._deps.running_turns.shutting_down
        assert not client.is_closed()
        assert events == []
        events.append("fleet")
        await close(owner)

    drain_outcomes, drain_notices = (
        runtime_module.drain_outcomes,
        runtime_module.drain_budget_notices,
    )

    async def outcomes():
        events.append("outcomes")
        await drain_outcomes()

    async def notices():
        events.append("notices")
        await drain_notices()

    monkeypatch.setattr(AsyncEngine, "dispose", observe_dispose)
    monkeypatch.setattr(runtime_module, "build_engine", observe_engine)
    monkeypatch.setattr(runtime_module, "drain_outcomes", outcomes)
    monkeypatch.setattr(runtime_module, "drain_budget_notices", notices)
    monkeypatch.setattr(HostFleetPorts, "close", observe_close)
    async with runtime_module.build_runtime(
        settings, fleet_prices=prices if supplied_prices else None
    ) as runtime:
        assert len(engines) == 2
        assert runtime.gateway_lease is not None
        assert runtime.turn_deps.fleet_root_grants is None
        assert runtime.turn_deps.running_turns.count() == 0
        if enabled:
            assert isinstance(runtime.fleet_card_factory, DiscordFleetCardFactory)
            owner = runtime.fleet_card_factory._service._owner()
            assert isinstance(owner, HostFleetPorts)
            assert owner.sessions is runtime.sessionmaker
            assert owner.prices is prices if supplied_prices else dict(owner.prices) == expected
            assert owner.markup == settings.billing.markup
            assert owner._deps is runtime.turn_deps
            assert owner._pool._tenant_cap == settings.fleet.max_concurrent_workers_per_tenant
            assert owner._pool._slots._value == settings.fleet.max_concurrent_workers
            assert not owner._tasks and not owner._watches
        else:
            assert runtime.fleet_card_factory is None
            assert runtime.turn_deps.state_store is None
        assert not events
        assert tariff_reads == (["read"] if enabled and not supplied_prices else [])
    assert events == (["fleet"] if enabled else []) + [
        "outcomes",
        "notices",
        "engine-1",
        "engine-0",
    ]
    assert client.is_closed()


async def test_runtime_preserves_falsey_injected_fleet_service_ownership(
    db_committing_engine, monkeypatch
):
    from pathlib import Path

    from daimon.adapters.discord import runtime as runtime_module
    from daimon.core.config import Settings
    from daimon.core.fleet_orchestration import HostFleetPorts
    from mux.testing.drivers.anthropic.ma import build_stub_anthropic

    class ExplicitOwner(FleetOrchestrator):
        def __bool__(self):
            return False

    settings = Settings.model_validate(
        {
            "database": {
                "url": db_committing_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline-test-key"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
            "fleet": {"enabled": True},
        }
    )
    monkeypatch.setattr(runtime_module, "AsyncAnthropic", lambda **kwargs: build_stub_anthropic())
    constructor = MagicMock(side_effect=AssertionError("explicit fleet service must stay external"))
    monkeypatch.setattr(HostFleetPorts, "__init__", constructor)
    service = ExplicitOwner()
    async with runtime_module.build_runtime(settings, fleet_service=service) as runtime:
        assert runtime.fleet_card_factory._service is service
    constructor.assert_not_called()


@pytest.mark.parametrize("discord_frozen_root", [False], indirect=True)
@pytest.mark.parametrize("fleet_enabled", [False, True])
async def test_normal_runtime_no_grant_preserves_sealed_and_final_delivery(
    discord_frozen_root, db_committing_engine, db_schema, monkeypatch, fleet_enabled
):
    from pathlib import Path

    import httpx
    from daimon.adapters.discord import runtime as runtime_module
    from daimon.core.config import Settings
    from daimon.core.stores import mux_state
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.db import build_test_engine
    from mux.testing.drivers.anthropic.ma import sse_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    from packages.core.tests.turn.conftest import (
        make_agent_message,
        make_status_idle,
        make_tool_use,
    )
    from packages.core.tests.turn.test_frozen_preparation import revoke

    deps, prepared, _, intent, origin, scope, _, transport = discord_frozen_root
    await revoke(discord_frozen_root)
    ordinary = replace(prepared, _source=None, _preparation=None)
    settings = Settings.model_validate(
        {
            "database": {
                "url": db_committing_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline-test-key"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
            "fleet": {"enabled": fleet_enabled},
            "turn": {"path": "mux", "channel_backends": True},
        }
    )
    monkeypatch.setattr(runtime_module, "AsyncAnthropic", lambda **kwargs: deps.anthropic)
    monkeypatch.setattr(
        runtime_module,
        "build_engine",
        lambda url, **kwargs: build_test_engine(url, db_schema, **kwargs),
    )
    sent = asyncio.Event()
    sealed = "A complete ordinary answer. " * 30
    final = "Ordinary final answer."
    payload = sse_response(
        [
            make_agent_message(event_id="ordinary-sealed", text=sealed).model_dump(mode="json"),
            make_tool_use(event_id="ordinary-tool", name="read").model_dump(mode="json"),
            make_agent_message(event_id="ordinary-final", text=final).model_dump(mode="json"),
            make_status_idle(event_id="ordinary-idle").model_dump(mode="json"),
        ]
    ).content

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await sent.wait()
            yield payload

    assert transport.router is not None
    original_routes = transport.router.routes

    def accept_input(request, match):
        from mux.testing.drivers.anthropic.ma import send_events_response

        sent.set()
        return send_events_response()

    transport.router.routes = [
        ("POST", re.compile(rf"/v1/sessions/{prepared.ma_session_id}/events"), accept_input),
        *original_routes,
    ]
    transport.queue(
        ScriptedReply(
            "GET",
            f"/v1/sessions/{prepared.ma_session_id}/events/stream",
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream()),
        )
    )
    async with runtime_module.build_runtime(settings) as runtime:
        assert runtime.turn_deps.fleet_root_grants is None
        assert (runtime.fleet_card_factory is not None) is fleet_enabled
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.parent_id, thread.guild.id = 70, 50, 60
        accepted = MagicMock(spec=discord.Message)
        accepted.id, accepted.channel.id = 900, 70
        send = AsyncMock(return_value=accepted)
        edit = AsyncMock(return_value=accepted)
        card = (
            runtime.fleet_card_factory(thread, 123, send, edit, turn_card_intent_id=intent.id)
            if fleet_enabled
            else None
        )
        lifecycle = DiscordTurnLifecycle(
            send=send, edit=edit, agent_name="root", model_id="claude-haiku-5-5", fleet_card=card
        )

        async def reseed():
            return "Ordinary input."

        outcome = await run_prepared_turn(
            runtime.turn_deps,
            ordinary,
            tenant_id=ordinary.admission.grant.tenant_id,
            platform="discord",
            thread_id=origin.thread_id,
            external_user_id="123",
            user_message="Ordinary input.",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: lifecycle,
            turn_card_intent_id=intent.id,
        )
        assert outcome.state.error is None
        contents = [
            call.kwargs.get("content", "") for call in send.await_args_list + edit.await_args_list
        ]
        assert any(sealed.strip() in content for content in contents)
        assert any(final in content for content in contents)
        assert not any("Fleet" in content for content in contents)
        async with runtime.sessionmaker() as db:
            assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
        assert (
            len(
                [r for r in transport.requests if r.method == "POST" and r.path.endswith("/events")]
            )
            == 1
        )
        assert not transport.violations


async def test_real_runtime_ordinary_turn_without_fleet_root_has_no_card(discord_frozen_root):
    """With fleet enabled, a normal turn of this thread renders no fleet card and
    never refuses: its own turn intent exists but no fleet root was frozen."""
    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.stores.turn_card_intents import create_turn_card_intent

    deps, _, _, intent, _, _, _, _ = discord_frozen_root
    async with deps.sessionmaker.begin() as db:
        ordinary = await create_turn_card_intent(
            db,
            tenant_id=intent.tenant_id,
            platform="discord",
            thread_id=intent.thread_id,
            channel_id=intent.channel_id,
            turn_token=UUID(int=991),
        )
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = int(intent.thread_id), 50
    thread.guild.id = 60
    send = AsyncMock()
    shared = FleetOrchestrator(HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal("1")))
    card = DiscordFleetCardFactory.from_runtime(shared, deps)(
        thread, 123, send, AsyncMock(), turn_card_intent_id=ordinary.id
    )
    assert card is not None
    assert await card.refresh() is False
    assert await card.allow_sealed_responses() is True
    assert await card.allow_summary() is True
    send.assert_not_awaited()


async def test_history_finder_matches_only_a_bot_card_with_this_runs_controls():
    from types import SimpleNamespace

    from daimon.adapters.discord.posted_controls.fleet import history_finder

    def posted(author_bot, custom_id, webhook_id=None):
        button = SimpleNamespace(custom_id=custom_id)
        row = SimpleNamespace(children=[button])
        container = SimpleNamespace(children=[SimpleNamespace(content="text"), row])
        message = MagicMock(spec=discord.Message)
        message.webhook_id = webhook_id
        message.author.bot = author_bot
        message.components = [container]
        return message

    other_run = posted(True, f"fleet:{UUID(int=7).hex}:all:1:kill")
    forged = posted(False, f"fleet:{RUN.hex}:all:1:kill")
    card = posted(False, f"fleet:{RUN.hex}:all:4:resume", webhook_id=5)

    async def history(limit):
        assert limit == 50
        for message in (other_run, forged, card):
            yield message

    thread = MagicMock(spec=discord.Thread)
    thread.history = history
    assert await history_finder(thread)(RUN) is card
    assert await history_finder(thread)(UUID(int=8)) is None


def test_a_worker_running_as_another_agent_shows_that_agents_name():
    as_researcher = worker(ordinal=1, role="research")
    as_researcher = replace(
        as_researcher,
        spec={
            **as_researcher.spec,
            "name": "researcher",
            "metadata": {"daimon_name": "researcher"},
        },
    )
    plain = replace(worker(ordinal=2, role="W2"), spec={**worker().spec, "name": "W2"})
    # An inline spec named like an agent inherits the root's metadata: it ran as no agent.
    inline = replace(
        worker(ordinal=3, role="claude"),
        spec={**worker().spec, "name": "fleet-claude", "metadata": {"daimon_name": "daimon-fleet"}},
    )
    view = build_fleet_view(run(), (as_researcher, plain, inline))
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert "**research** (researcher) ·" in text
    # A worker named after its role (a plain task worker) shows no agent name.
    assert "**W2** ·" in text and "(W2)" not in text
    assert "**claude** ·" in text and "(fleet-claude)" not in text


def test_an_agent_workers_card_row_says_what_it_runs_without():
    worker_row = worker(ordinal=1, role="openai")
    as_openai = replace(
        worker_row,
        spec={
            **worker_row.spec,
            "name": "fleet-openai",
            "metadata": {"daimon_name": "fleet-openai"},
            "declared_drops": ["memory", "tools", "model_controls"],
        },
    )
    view = build_fleet_view(run(), (as_openai,))
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert "**openai** (fleet-openai, without its memory, its tools, its model settings) ·" in text


def test_a_task_copy_of_the_root_says_which_of_its_setup_it_runs_without():
    """#1357: workers of a root with a logged-in connector or stored secrets run without them."""
    copy_row = worker(ordinal=0, role="W1")
    copy = replace(
        copy_row,
        spec={
            **copy_row.spec,
            "name": "W1",
            "declared_drops": ["files", "mcp", "skills", "tools"],
        },
    )
    plain_row = worker(ordinal=1, role="W2")
    plain = replace(
        plain_row, spec={**plain_row.spec, "name": "W2", "declared_drops": ["skills", "tools"]}
    )
    view = build_fleet_view(run(), (copy, plain))
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert "**W1** (without its files, its connected tools) ·" in text
    # Every task worker runs without tools and skills; that isn't news.
    assert "**W2** ·" in text
