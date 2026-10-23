"""TeamsApp: per-thread queueing, the tenant cap, Cancel, and turn bookkeeping."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams import card as card_module
from daimon.adapters.teams import reactions
from daimon.adapters.teams.admissions import HOLDING
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.commands import (
    ANSWERED_IN_CHAT,
    CHANNEL_POINTER,
    NO_CHAT_ACROSS_ORGS,
    fresh_start,
)
from daimon.adapters.teams.context import HistoryBlock
from daimon.adapters.teams.identity import INPUT_TOO_LONG, MAX_INBOUND_MESSAGE_BYTES, TeamsInbound
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.native_admins import GroupLookupFailed
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role, TurnCardIntentRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.core.stores.tenants import set_plain_text_mentions, set_turn_cap
from daimon.core.stores.thread_agent_bindings import create_binding, update_lifecycle
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.stores.turn_outcomes import OutcomeRecord, list_for_tenant
from daimon.core.teams_threads import new_setup_thread_id
from daimon.core.turn.admission import AdmissionDenied
from daimon.core.turn.errors import AdmissionDenialReason, MissingTurnConfigError
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.slots import holding, wait_for_slot
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from microsoft_teams.api import AdaptiveCardActionCardResponse, MessageActivity
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    ADMIN_TEAM,
    BOT_ACCOUNT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    THREAD_ID,
    FakeSender,
    bot_token,
    build_teams_runtime,
    make_channel_activity,
    make_inbound,
    make_message_activity,
    patched_admission,
    patched_turns,
    settle,
)

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


def test_agent_name_prefix_follows_deployment_switch() -> None:
    assert (
        app_module._agent_name_prefix(  # pyright: ignore[reportPrivateUsage]
            enabled=False, name="Ada", metadata=None, default_name="daimon"
        )
        is None
    )
    assert (
        app_module._agent_name_prefix(  # pyright: ignore[reportPrivateUsage]
            enabled=True, name="Ada", metadata=None, default_name="daimon"
        )
        == "Ada"
    )


def _app(
    db_factory: async_sessionmaker[AsyncSession], sender: FakeSender, cap: int = 3
) -> TeamsApp:
    runtime = build_teams_runtime(db_factory)
    runtime.settings.teams = runtime.settings.teams.model_copy(
        update={"max_concurrent_turns_per_tenant": cap}
    )
    return TeamsApp(
        runtime=runtime, sender=sender, commands={"new": fresh_start}, bot_token=bot_token
    )


async def test_messages_during_a_turn_queue_and_run_once_per_author(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[TeamsInbound] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound)
        if len(ran) == 1:
            started.set()
            await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        first = asyncio.create_task(teams._orchestrate(make_inbound("one"), TENANT))
        await started.wait()
        two, three = make_inbound("two"), make_inbound("three")
        await teams._orchestrate(two, TENANT)
        await teams._orchestrate(three, TENANT)
        await teams._orchestrate(make_inbound("other", user=OTHER_AAD_OBJECT_ID), TENANT)
        assert len(ran) == 1, "queued messages wait for the running turn"
        release.set()
        await first

    assert [(i.user_id, i.text) for i in ran] == [
        (AAD_OBJECT_ID, "one"),
        (AAD_OBJECT_ID, "two\n\nthree"),
        (OTHER_AAD_OBJECT_ID, "other"),
    ]
    assert ran[1].message_ids == (two.activity_id, three.activity_id), (
        "the composed turn knows every message it answers, for media and history"
    )


async def test_a_queued_message_shows_an_hourglass_until_its_turn_starts(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reactions, "MIN_INTERVAL_S", 0.0)
    made: list[tuple[str, str, bool]] = []

    async def react(target: reactions.ReactionTarget, reaction: str, *, add: bool) -> None:
        made.append((target.activity_id, reaction, add))

    runtime = build_teams_runtime(db_session_factory)
    teams = TeamsApp(runtime=runtime, sender=FakeSender(), commands={}, bot_token=bot_token)
    teams._reactions = reactions.TeamsReactions(
        react, spawn=lambda coro: teams.spawn(coro, name="r")
    )
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[TeamsInbound] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound)
        if len(ran) == 1:
            started.set()
            await release.wait()

    first, queued = make_inbound("one"), make_inbound("two")
    with patch.object(TeamsApp, "_run_turn", _turn):
        running = asyncio.create_task(teams._orchestrate(first, TENANT))
        await started.wait()
        await teams._orchestrate(queued, TENANT)
        await settle(teams)
        assert made == [(queued.activity_id, reactions.HOURGLASS, True)], (
            "only the message waiting behind the running turn gets ⌛"
        )
        release.set()
        await running
    await settle(teams)
    assert made[1:] == [(queued.activity_id, reactions.HOURGLASS, False)], (
        "the ⌛ comes off once its turn starts"
    )


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize(
    ("policy", "marked"),
    [
        (TenantAccessPolicy(), True),
        (TenantAccessPolicy(invoker_user_ids=(OTHER_AAD_OBJECT_ID,)), False),
        (TenantAccessPolicy(protected_channel_ids=(CONVERSATION_ID,)), False),
    ],
    ids=["allowed", "off-the-invoker-allowlist", "protected"],
)
async def test_an_hourglass_only_for_a_message_admission_would_let_start(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    policy: TenantAccessPolicy,
    marked: bool,
) -> None:
    """⌛ promises an answer, so it waits on the gates the turn itself would meet."""
    monkeypatch.setattr(reactions, "MIN_INTERVAL_S", 0.0)
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    made: list[tuple[str, str, bool]] = []

    async def react(target: reactions.ReactionTarget, reaction: str, *, add: bool) -> None:
        made.append((target.activity_id, reaction, add))

    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
        react=react,
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        if not started.is_set():
            started.set()
            await release.wait()

    queued = make_inbound("two", kind="channel")
    with patch.object(TeamsApp, "_run_turn", _turn):
        running = asyncio.create_task(
            teams._orchestrate(make_inbound("one", kind="channel"), TENANT)
        )
        await started.wait()
        await teams._orchestrate(queued, TENANT)
        async with asyncio.timeout(5):
            while teams._reactions._running:
                await asyncio.sleep(0.01)
        added = [(queued.activity_id, reactions.HOURGLASS, True)] if marked else []
        assert made == added, "⌛ only where admission would let the message start a turn"
        release.set()
        await running
    await teams.drain(timeout=5)
    removed = [(queued.activity_id, reactions.HOURGLASS, False)] if marked else []
    assert made[len(added) :] == removed, "a removal only for a ⌛ that was added"


def test_a_message_composed_with_a_later_form_keeps_its_reactions() -> None:
    """A form's id names no message: 👀 and ✅ go on the latest one the person sent."""
    sent = make_inbound("the question")
    form = dataclasses.replace(make_inbound("the form"), from_form=True)
    composed = app_module._compose([sent, form])
    assert (composed.activity_id, composed.from_form) == (sent.activity_id, False), (
        "the turn reacts on the person's message"
    )
    assert set(composed.message_ids) == {sent.activity_id, form.activity_id}, "it answers both"
    assert composed.text == "the question\n\nthe form", "in the order they came"
    forms = app_module._compose([form, dataclasses.replace(form, activity_id="2")])
    assert forms.from_form, "only forms: nothing to react on"


async def test_a_replayed_message_loses_a_predecessors_hourglass(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process that queued it may have exited before its ⌛ came off."""
    monkeypatch.setattr(reactions, "MIN_INTERVAL_S", 0.0)
    made: list[tuple[str, str, bool]] = []

    async def react(target: reactions.ReactionTarget, reaction: str, *, add: bool) -> None:
        made.append((target.activity_id, reaction, add))

    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
        react=react,
    )
    held = make_inbound("held")

    async def _intake(self: TeamsApp, inbound: TeamsInbound) -> None:
        return None

    with patch.object(TeamsApp, "_intake", _intake):
        teams._replay([held])
        await settle(teams)
    await teams.drain(timeout=5)
    assert made == [(held.activity_id, reactions.HOURGLASS, False)], "⌛ comes off on replay"


async def test_a_reaction_in_flight_does_not_hold_up_a_drain(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stuck = asyncio.Event()

    async def react(target: reactions.ReactionTarget, reaction: str, *, add: bool) -> None:
        await stuck.wait()

    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
        react=react,
    )
    teams._reactions.mark_queued(make_inbound())
    await asyncio.sleep(0)
    assert teams.in_flight == 0, "a reaction is not app work"
    async with asyncio.timeout(2):
        await teams.drain(timeout=30)
    assert teams._reactions._running == set(), "the unanswered reaction was cancelled"


async def test_a_drain_waits_for_an_output_sweep_spawned_while_it_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A turn ending mid-drain spawns its sweep then: it gets the drain's deadline too.

    Regression: a restart soon after a turn ended lost the file its sweep was delivering.
    """
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
    )
    delivered = asyncio.Event()

    async def sweep() -> None:
        await asyncio.sleep(0.05)
        delivered.set()

    async def turn() -> None:
        await asyncio.sleep(0.05)
        teams.spawn(sweep(), name="teams.output-sweep")

    teams.spawn(turn(), name="turn")
    async with asyncio.timeout(5):
        await teams.drain(timeout=app_module.DRAIN_GRACE_S)
    assert delivered.is_set(), "the sweep spawned during the drain ran to the end"


async def test_the_controls_follow_the_sweep_or_its_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finished, failed or overdue, the sweep is followed by the controls; never cut short."""
    monkeypatch.setattr(app_module, "CONTROLS_WAIT_S", 0.05)
    events: list[str] = []
    lifecycle: Any = SimpleNamespace(
        release_controls=AsyncMock(side_effect=lambda: events.append("controls"))
    )

    async def sweep(outcome: str, delay: float) -> None:
        await asyncio.sleep(delay)
        events.append(outcome)
        if outcome == "failed":
            raise RuntimeError("Graph is down")

    for outcome, delay in (("swept", 0.0), ("failed", 0.0)):
        task = asyncio.create_task(sweep(outcome, delay))
        await app_module._controls_after(task, lifecycle)
    slow = asyncio.create_task(sweep("late", 0.1))
    await app_module._controls_after(slow, lifecycle)
    await slow
    assert events == ["swept", "controls", "failed", "controls", "controls", "late"]

    monkeypatch.setattr(app_module, "CONTROLS_WAIT_S", 30.0)
    stuck = asyncio.create_task(sweep("never", 3600))
    waiting = asyncio.create_task(app_module._controls_after(stuck, lifecycle))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    stuck.cancel()
    assert events[-1] == "controls", "a drain's cancel still posts them"


async def test_the_next_turn_in_a_chat_posts_the_held_controls_first(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A follow-up's card never lands above the last turn's controls."""
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
    )
    events: list[str] = []
    held: Any = SimpleNamespace(
        release_controls=AsyncMock(side_effect=lambda: events.append("controls"))
    )
    teams._held_controls[CONVERSATION_ID] = held
    with patch.object(
        teams, "_run_turn_observed", AsyncMock(side_effect=lambda *a, **k: events.append("turn"))
    ):
        await teams._run_turn(make_inbound(), TENANT)
        await teams._run_turn(make_inbound(), TENANT)
    assert events == ["controls", "turn", "turn"], "posted once, before the next turn"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_mention_drops_the_threads_waiting_batch_only_once_its_turn_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A refused mention (here a command in a channel) must not drop another person's batch."""
    teams = _app(db_session_factory, FakeSender())
    mention = dataclasses.replace(
        make_inbound("is it shipped?", conversation=THREAD_ID, kind="channel"),
        channel_id=CHANNEL_ID,
    )
    command = dataclasses.replace(mention, text="new")
    participation = teams._participation_for(mention)

    with (
        patch.object(participation, "cancel", wraps=participation.cancel) as cancel,
        patch.object(TeamsApp, "_run_turns", AsyncMock()),
    ):
        await teams._handle(command)
        assert cancel.call_count == 0, "the pointer reply runs no turn, so the batch stays"
        await teams._handle(mention)

    cancel.assert_called_once_with(THREAD_ID)


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("draining", [True, False])
async def test_messages_still_queued_when_the_turn_ends_early_are_told_why(
    db_session_factory: async_sessionmaker[AsyncSession], draining: bool
) -> None:
    """A drain for a restart is not a failure: the queued message is held for the next boot."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    started = asyncio.Event()

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        first = teams.spawn(teams._orchestrate(make_inbound("one"), TENANT), name="turn")
        await started.wait()
        two = make_inbound("two")
        assert await teams._admissions.claim(two, now=datetime.now(UTC)), "admitted on arrival"
        await teams._orchestrate(two, TENANT)
        if draining:
            await teams.drain(timeout=0.0)
        else:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    expected = HOLDING if draining else app_module._FAILED
    assert [a.text for a in sender.activities] == [expected], (
        "the queued message is answered once, with the notice that matches the cause"
    )


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_queued_message_follows_a_setup_conversation_that_ended_meanwhile(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    setup = new_setup_thread_id(CONVERSATION_ID)
    key = {"tenant_id": TENANT, "platform": "teams", "parent_channel_id": CONVERSATION_ID}
    async with db_session_factory.begin() as session:
        await create_binding(
            session, **key, thread_id=setup, responder_ma_agent_id="agt", responder_name="s"
        )
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.thread_id)
        started.set()
        await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        first = asyncio.create_task(teams._orchestrate(make_inbound("one"), TENANT))
        await started.wait()
        await teams._orchestrate(make_inbound("two"), TENANT)
        async with db_session_factory.begin() as session:
            await update_lifecycle(session, **key, thread_id=setup, deleted=True)
        release.set()
        await first
    assert ran == [setup, CONVERSATION_ID], "routed when it runs, not when it arrived"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_failing_command_is_answered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    failing = AsyncMock(side_effect=DaimonError("database unavailable"))
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={"new": failing},
        bot_token=bot_token,
    )
    await teams._handle(make_inbound("new"))
    assert [a.text for a in sender.activities] == [app_module._FAILED]


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_failing_channel_command_is_answered_to_its_sender_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A command is the sender's own, so its failure is too, even typed as a mention."""
    sender = FakeSender()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={"help": AsyncMock(side_effect=DaimonError("database unavailable"))},
        bot_token=bot_token,
    )
    inbound = dataclasses.replace(
        make_inbound("help", conversation=THREAD_ID, kind="channel"),
        channel_id=CHANNEL_ID,
        sender_id="29:sender",
    )
    await teams._handle(inbound)
    [failed] = sender.activities
    assert failed.text == app_module._FAILED
    assert failed.recipient is not None and failed.recipient.is_targeted, "for the sender only"


@dataclasses.dataclass
class _Direct:
    """A `DirectChats` whose member lookup finds the person only when `present`."""

    present: bool = True
    looked_up: list[tuple[str, str]] = dataclasses.field(default_factory=list[tuple[str, str]])

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        self.looked_up.append((conversation_id, aad_object_id))
        return "29:member" if self.present else None

    async def open_chat(self, member_id: str) -> str:
        return "a:direct"

    async def post(self, conversation_id: str, text: str) -> str:
        raise AssertionError("commands answer through the sender")


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("present", [True, False])
async def test_a_channel_command_is_answered_in_the_senders_1_1_chat(
    db_session_factory: async_sessionmaker[AsyncSession], present: bool
) -> None:
    sender, direct = FakeSender(), _Direct(present)
    seen: list[TeamsInbound] = []

    async def _memory(context: Any) -> None:
        seen.append(context.inbound)

    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={"memory": _memory},
        bot_token=bot_token,
        direct=direct,
    )
    inbound = dataclasses.replace(
        make_inbound("memory", conversation=THREAD_ID, kind="channel"), channel_id=CHANNEL_ID
    )
    await teams._handle(inbound)
    [(where, reply, _)] = sender.sent
    assert where == THREAD_ID and direct.looked_up == [(CHANNEL_ID, AAD_OBJECT_ID)]
    if present:
        assert reply.text == ANSWERED_IN_CHAT.format(name="memory")
        [moved] = seen
        assert (moved.kind, moved.conversation_id, moved.team_id) == ("dm", "a:direct", None)
    else:
        assert reply.text == CHANNEL_POINTER.format(name="memory") and seen == []


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_channel_command_from_another_organisation_is_not_sent_to_a_1_1_chat(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender, direct = FakeSender(), _Direct()
    run = AsyncMock()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={"support": run},
        bot_token=bot_token,
        direct=direct,
    )
    inbound = dataclasses.replace(
        make_inbound("support", conversation=THREAD_ID, kind="channel"),
        channel_id=CHANNEL_ID,
        is_foreign=True,
    )
    await teams._handle(inbound)
    [(_, reply, _)] = sender.sent
    assert reply.text == NO_CHAT_ACROSS_ORGS.format(name="support"), "no pointer they can't use"
    assert direct.looked_up == [] and run.await_count == 0, "no 1:1 chat is attempted"


async def _outcomes(db_factory: async_sessionmaker[AsyncSession]) -> list[OutcomeRecord]:
    await drain_outcomes()
    async with db_factory() as session:
        return await list_for_tenant(session, TENANT)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_over_the_tenant_cap_a_new_thread_waits_for_a_slot(
    db_engine: AsyncEngine,
) -> None:
    # Concurrent conversations need independent connections; the rollback fixture
    # pins every session to one connection and cannot support overlapping queries.
    db_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    sender = FakeSender()
    teams = _app(db_session_factory, sender, cap=1)
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        # Stands in for _run_turn: its card is up, now it waits for a slot.
        assert (
            await wait_for_slot(
                asyncio.Event(), sessionmaker=db_session_factory, tenant_id=tenant_id
            )
            == "started"
        )
        ran.append(inbound.conversation_id)
        started.set()
        await release.wait()

    with (
        patch.object(TeamsApp, "_run_turn", _turn),
        patch("daimon.core.turn.slots.is_over_balance", AsyncMock(return_value=False)),
    ):
        first = asyncio.create_task(teams._orchestrate(make_inbound(), TENANT))
        async with asyncio.timeout(5):
            await started.wait()
        second = asyncio.create_task(
            teams._orchestrate(make_inbound(conversation="a:conversation-2"), TENANT)
        )
        async with asyncio.timeout(5):
            while not teams.turn_queue.depth(TENANT):
                await asyncio.sleep(0.01)
        assert len(ran) == 1, "the second waits"
        release.set()
        async with asyncio.timeout(5):
            await asyncio.gather(first, second)

    assert ran == [CONVERSATION_ID, "a:conversation-2"]
    assert sender.sent == [], "no capacity notice: the queue is backstage"
    assert teams.turn_queue.in_flight() == 0


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_full_queue_at_the_tenant_cap_sheds_a_new_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender, cap=1)
    teams.turn_queue.max_queued_per_tenant = 0
    started, release = asyncio.Event(), asyncio.Event()

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        started.set()
        await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        first = asyncio.create_task(teams._orchestrate(make_inbound(), TENANT))
        async with asyncio.timeout(5):
            await started.wait()
        await teams._orchestrate(make_inbound(conversation="a:conversation-2"), TENANT)
        release.set()
        await first

    assert [(c, a.text) for c, a, _ in sender.sent] == [("a:conversation-2", app_module._SHED)]
    [shed] = await _outcomes(db_session_factory)
    assert (shed.reason, shed.thread_id) == (
        TerminationReason.ADMISSION_CONCURRENCY_SHED,
        "a:conversation-2",
    ), "a shed turn leaves an outcome row"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_tenant_turn_cap_override_beats_the_deployment_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        await set_turn_cap(session, tenant_id=TENANT, cap=1)
    sender = FakeSender()
    teams = _app(db_session_factory, sender, cap=3)
    teams.turn_queue.max_queued_per_tenant = 0  # refuse at the cap, so the cap shows
    started, release = asyncio.Event(), asyncio.Event()

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        started.set()
        await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        first = asyncio.create_task(teams._orchestrate(make_inbound(), TENANT))
        async with asyncio.timeout(5):
            await started.wait()
        await teams._orchestrate(make_inbound(conversation="a:conversation-2"), TENANT)
        release.set()
        await first

    assert [(c, a.text) for c, a, _ in sender.sent] == [("a:conversation-2", app_module._SHED)]


def test_every_admission_denial_has_a_reply() -> None:
    assert set(app_module._DENIALS) == set(get_args(AdmissionDenialReason))


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_card_post_failure_after_admission_still_finishes_the_outcome(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender(fail_on={0})
    teams = _app(db_session_factory, sender)
    with patched_admission():
        await teams._run_turn_guarded(make_inbound(), TENANT)
    [outcome] = await _outcomes(db_session_factory)
    assert (outcome.platform, outcome.error_class) == ("teams", "ConnectError"), (
        "the failure before bind is recorded, not left unfinished"
    )


def _click(key: str, clicker: str | None) -> Any:
    action = SimpleNamespace(data={"action": "cancel_turn", "turn": key})
    activity = SimpleNamespace(
        value=SimpleNamespace(action=action), from_=SimpleNamespace(aad_object_id=clicker)
    )
    return SimpleNamespace(activity=activity)


def _message(payload: dict[str, object], reply: AsyncMock, send: AsyncMock | None = None) -> Any:
    activity = MessageActivity.model_validate(payload)
    return SimpleNamespace(
        activity=activity,
        conversation_ref=SimpleNamespace(service_url=SERVICE_URL),
        reply=reply,
        send=send or AsyncMock(),
    )


async def test_a_root_post_without_a_mention_is_dropped_with_a_log(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Organic participation screens thread replies only: a new post stays mention-only."""
    teams, reply = _app(db_session_factory, FakeSender()), AsyncMock()
    root = make_channel_activity(mention_bot=False, conversation_id=CHANNEL_ID)
    with structlog.testing.capture_logs() as logs:
        await teams.handle_message(_message(root, reply))
    reply.assert_not_awaited()
    assert {
        "event": "teams.message.ignored",
        "log_level": "debug",
        "conversation_type": "channel",
        "reason": "not_mentioned",
    } in logs, "a silent drop leaves the conversation type and why, never the text"


async def test_a_refusal_that_cannot_be_sent_is_logged(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    reply = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
    with structlog.testing.capture_logs() as logs:
        await teams.handle_message(_message(make_message_activity(aad_object_id=None), reply))
    reply.assert_awaited_once()
    assert {
        "event": "teams.refusal.send_failed",
        "log_level": "warning",
        "conversation_type": "personal",
        "reason": "ConnectError",
    } in logs, "a refusal nobody saw is still on record"


async def test_a_refusal_whose_receipt_cannot_be_recorded_is_not_sent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unrecorded, a second process could refuse it too: silence beats a double reply."""
    teams = _app(db_session_factory, FakeSender())
    reply = AsyncMock()
    down = AsyncMock(side_effect=SQLAlchemyError("down"))
    with patch("daimon.adapters.teams.app.TeamsAdmissions.claim_receipt", down):
        await teams.handle_message(
            _message(make_message_activity(conversation_type="groupChat"), reply)
        )
    reply.assert_not_awaited()


def _cancel_entry(
    teams: TeamsApp, key: str, *, sender: FakeSender, author: str = AAD_OBJECT_ID
) -> tuple[app_module._CancelEntry, asyncio.Event]:  # pyright: ignore[reportPrivateUsage]
    """A registered turn whose status card is posted, as the lifecycle leaves it."""
    cancel = asyncio.Event()
    lifecycle = TeamsTurnLifecycle(
        sender=sender, conversation_id=CONVERSATION_ID, service_url=None, cancel_key=key
    )
    entry = app_module._CancelEntry(cancel, author, lifecycle)  # pyright: ignore[reportPrivateUsage]
    teams._cancel_registry[key] = entry  # pyright: ignore[reportPrivateUsage]
    return entry, cancel


def _stopping_card(response: Any) -> str:
    assert isinstance(response, AdaptiveCardActionCardResponse), response
    return response.value.model_dump_json(by_alias=True)


async def test_only_the_author_can_cancel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    entry, cancel = _cancel_entry(teams, "key-1", sender=sender)
    await entry.lifecycle.post_initial()

    refused = await teams.handle_cancel(_click("key-1", OTHER_AAD_OBJECT_ID))
    assert refused.value == app_module._CANCEL_NOT_AUTHOR and not cancel.is_set()
    assert not entry.lifecycle.stopping, "another person's click leaves the card alone"
    ended = await teams.handle_cancel(_click("key-2", AAD_OBJECT_ID))
    assert ended.value == app_module._CANCEL_TURN_ENDED
    accepted = await teams.handle_cancel(_click("key-1", AAD_OBJECT_ID.upper()))
    assert cancel.is_set() and entry.lifecycle.stopping
    stopping = _stopping_card(accepted)
    assert card_module.STOPPING_HEADLINE in stopping, "the clicker sees it at once"
    assert card_module.CANCEL_VERB not in stopping, "no Stop button on the stopping card"
    assert len(sender.sent) == 1, "the click handler sends nothing itself"
    await entry.lifecycle._card_edits.stopping  # pyright: ignore[reportPrivateUsage]
    assert len(sender.sent) == 2 and sender.activities[-1].id == "m-1", (
        "the lifecycle edits the card"
    )
    assert card_module.STOPPING_HEADLINE in sender.activities[-1].model_dump_json()


async def test_a_current_deployment_admin_can_stop_anyones_reply(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    teams._teams = teams._teams.model_copy(update={"admin_team_id": ADMIN_TEAM})
    owners = AsyncMock(return_value=frozenset({OTHER_AAD_OBJECT_ID}))
    teams.runtime = dataclasses.replace(teams.runtime, team_owners=owners)
    entry, cancel = _cancel_entry(teams, "key-1", sender=sender)
    await entry.lifecycle.post_initial()

    accepted = await teams.handle_cancel(_click("key-1", OTHER_AAD_OBJECT_ID))
    assert cancel.is_set() and entry.lifecycle.stopping, "an owner of the admin team stops it"
    assert card_module.STOPPING_HEADLINE in _stopping_card(accepted)
    owners.assert_awaited_with(ADMIN_TEAM)


@pytest.mark.parametrize("case", ["non_admin", "owners_unreadable", "no_entra_id"])
async def test_a_non_admin_or_an_unreadable_role_cannot_stop_anothers_reply(
    db_session_factory: async_sessionmaker[AsyncSession], case: str
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    teams._teams = teams._teams.model_copy(update={"admin_team_id": ADMIN_TEAM})
    owners = (
        AsyncMock(side_effect=GroupLookupFailed("http error (status 503)"))
        if case == "owners_unreadable"
        else AsyncMock(return_value=frozenset({AAD_OBJECT_ID}))
    )
    teams.runtime = dataclasses.replace(teams.runtime, team_owners=owners)
    entry, cancel = _cancel_entry(teams, "key-1", sender=sender)
    await entry.lifecycle.post_initial()

    clicker = None if case == "no_entra_id" else OTHER_AAD_OBJECT_ID
    refused = await teams.handle_cancel(_click("key-1", clicker))
    expected = (
        app_module._CANCEL_ROLE_UNKNOWN  # pyright: ignore[reportPrivateUsage]
        if case == "owners_unreadable"
        else app_module._CANCEL_NOT_AUTHOR  # pyright: ignore[reportPrivateUsage]
    )
    assert refused.value == expected, "an outage is not read as 'not an admin'"
    assert not cancel.is_set() and not entry.lifecycle.stopping
    if case == "no_entra_id":
        owners.assert_not_awaited()  # a click with no Entra id is never checked as an admin


async def test_a_stop_click_after_the_closing_notice_started_is_told_the_reply_finished(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    entry, cancel = _cancel_entry(teams, "key-1", sender=sender)
    await entry.lifecycle.post_initial()
    await entry.lifecycle.on_terminal_success(TurnState())  # Registered until the turn's cleanup.

    late = await teams.handle_cancel(_click("key-1", AAD_OBJECT_ID))
    assert late.value == app_module._CANCEL_TURN_ENDED and not cancel.is_set()
    assert not entry.lifecycle.stopping and "Stopped." in sender.activities[-1].model_dump_json()


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_stop_survives_dead_session_recovery(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The retry's lifecycle takes the registry entry over and still reads "Stopping…"."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    seen: dict[str, Any] = {}

    async def run(*_args: Any, recovery_lifecycle: Any, **_kwargs: Any) -> RunOutcome:
        [key] = teams._cancel_registry  # pyright: ignore[reportPrivateUsage]
        seen["click"] = await teams.handle_cancel(_click(key, AAD_OBJECT_ID))
        adopted = recovery_lifecycle(asyncio.Event())
        seen["entry"] = teams._cancel_registry[key]  # pyright: ignore[reportPrivateUsage]
        seen["adopted"] = adopted
        await adopted._card_edits.stopping  # pyright: ignore[reportPrivateUsage]
        seen["edited"] = sender.activities[-1].model_copy(deep=True)
        later = await teams.handle_cancel(_click(key, AAD_OBJECT_ID))
        seen["again"] = later
        await adopted.on_terminal_success(TurnState())
        return RunOutcome(
            state=TurnState(), ma_session_id="sess-teams-1", mapping_id=None, recovered=True
        )

    with (
        patched_admission(),
        patch.object(app_module, "run_prepared_turn", AsyncMock(side_effect=run)),
    ):
        await teams._run_turn(make_inbound(), TENANT)  # pyright: ignore[reportPrivateUsage]

    assert seen["entry"].lifecycle is seen["adopted"], "a later click reaches the new attempt"
    assert seen["adopted"].stopping
    assert card_module.STOPPING_HEADLINE in seen["edited"].model_dump_json(), "into the same card"
    assert seen["edited"].id == "m-1" and 'Stop"' not in seen["edited"].model_dump_json()
    assert card_module.STOPPING_HEADLINE in _stopping_card(seen["again"])
    assert "Stopped." in sender.activities[-1].model_dump_json(), (
        "the closing notice has the last word"
    )
    assert teams._cancel_registry == {}  # pyright: ignore[reportPrivateUsage]


async def _open_intents(db_factory: async_sessionmaker[AsyncSession]) -> list[TurnCardIntentRow]:
    async with db_factory() as session:
        return await list_recoverable_turn_card_intents(session, platform="teams")


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_finished_turn_retires_its_card_intent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)

    with patched_turns("done"):
        await teams._run_turn(make_inbound(), TENANT)

    assert "m-1" in {a.id for a in sender.activities[1:]}, "the answer edits the posted card"
    assert await _open_intents(db_session_factory) == [], "a closed card retires its intent"
    assert teams._cancel_registry == {}, "the Cancel key is dropped with the turn"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_turn_cut_off_by_shutdown_keeps_its_intent_for_the_boot_sweep(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    started = asyncio.Event()

    async def _run_turn(**kwargs: Any) -> TurnState:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patched_admission(), patch("daimon.core.turn.run.run_turn", side_effect=_run_turn):
        task = asyncio.create_task(teams._run_turn(make_inbound(), TENANT))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    [intent] = await _open_intents(db_session_factory)
    assert (intent.status, intent.message_id) == ("posted", "m-1"), "left for the boot sweep"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_shutdown_after_the_answer_still_retires_the_intent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cut off while overflow chunks send, the delivered answer must not become a restart notice."""
    teams = _app(db_session_factory, FakeSender())
    answered = asyncio.Event()

    async def _run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        await lifecycle.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="ok")]))
        answered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patched_admission(), patch("daimon.core.turn.run.run_turn", side_effect=_run_turn):
        task = asyncio.create_task(teams._run_turn(make_inbound(), TENANT))
        await answered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await _open_intents(db_session_factory) == [], "a closed card is not left for the sweep"


async def test_a_denied_turn_says_why_without_a_card(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    denied = AsyncMock(side_effect=AdmissionDenied(reason="balance_depleted"))
    with patch.object(app_module, "admit", denied):
        await teams._run_turn(make_inbound(), TENANT)
    assert [a.text for a in sender.activities] == [
        "Your team's Daimon credit has run out.\n\nAn admin can top up with `billing` in a 1:1 chat with me."
    ], "the refusal says why, in the organisation's nouns"
    assert await _open_intents(db_session_factory) == [], "no card, so no intent"


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("named", [True, False])
async def test_a_refusal_that_sends_the_person_to_an_admin_says_who_the_admins_are(
    db_session_factory: async_sessionmaker[AsyncSession], named: bool
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    teams.runtime.settings.teams = teams.runtime.settings.teams.model_copy(
        update={"admin_team_id": ADMIN_TEAM}
    )
    if named:
        async with db_session_factory.begin() as session:
            await record_teams_installation(
                session,
                tenant_id=TENANT,
                team_id="19:admins@thread.tacv2",
                group_id=ADMIN_TEAM,
                name="Leads",
            )
    err = MissingTurnConfigError(
        missing=("agent",), agent_name_tier=None, environment_name_tier=None
    )  # pyright: ignore[reportArgumentType]
    with patch.object(app_module, "admit", AsyncMock(side_effect=err)):
        await teams._run_turn(make_inbound(), TENANT)
    [notice] = [a.text for a in sender.activities]
    assert notice.startswith("Daimon isn't set up in this channel yet.\n\nAsk an admin to ")
    assert notice.endswith("\n\nAdmins are the owners of the Leads team.") is named


def _unprompted_reply() -> TeamsInbound:
    inbound = make_inbound("is the release on?", conversation=THREAD_ID, kind="channel")
    return dataclasses.replace(inbound, channel_id=CHANNEL_ID, unprompted=True)


_THREAD = HistoryBlock(tag="thread", lines=('<message author="Grace">ship Thursday?</message>',))


@pytest.mark.usefixtures("provisioned_tenant")
async def test_an_unprompted_turn_posts_only_its_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)

    with (
        patch.object(TeamsApp, "_history", AsyncMock(return_value=_THREAD)),
        patched_turns("Thursday, per the release notes.") as turns,
    ):
        await teams._participate(_unprompted_reply(), TENANT)

    answer, controls = sender.activities
    assert answer.text == "Thursday, per the release notes." and not controls.text, (
        "and its controls"
    )
    assert 'unprompted="true"' in turns[0]["user_message"], "the agent knows nobody asked"
    assert await _open_intents(db_session_factory) == [], "no card, so no intent"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_an_unprompted_turn_stays_silent_when_refused_or_without_its_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    denied = AsyncMock(side_effect=AdmissionDenied(reason="balance_depleted"))
    with patch.object(app_module, "admit", denied):
        await teams._participate(_unprompted_reply(), TENANT)
    with patch.object(TeamsApp, "_history", AsyncMock(return_value=None)), patched_turns() as turns:
        await teams._participate(_unprompted_reply(), TENANT)
    assert sender.activities == [] and turns == []


async def test_an_owner_of_the_admin_team_gets_the_admin_role(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    teams._teams = teams._teams.model_copy(update={"admin_team_id": ADMIN_TEAM})
    owners = AsyncMock(return_value=frozenset({AAD_OBJECT_ID}))
    teams.runtime = dataclasses.replace(teams.runtime, team_owners=owners)
    assert await teams._role(make_inbound()) is Role.ADMIN, "an owner of the admin team"
    owners.assert_awaited_with(ADMIN_TEAM)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_turn_reads_its_role_once_for_admission_and_its_origin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two reads could disagree (the cache expiring between them): admit as one, prompt as other."""
    teams = _app(db_session_factory, FakeSender())
    role = AsyncMock(side_effect=[Role.ADMIN, Role.USER])
    origins: list[Role] = []
    real_origin = app_module.turn_origin

    def recording_origin(*args: Any, **kwargs: Any) -> Any:
        origins.append(kwargs["role"])
        return real_origin(*args, **kwargs)

    with (
        patched_turns("done"),
        patch.object(app_module, "inbound_role", role),
        patch.object(app_module, "turn_origin", recording_origin),
    ):
        await teams._run_turn(make_inbound(), TENANT)
    assert role.await_count == 1, "one role read per turn"
    assert origins == [Role.ADMIN], "the origin carries the role admission recorded"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_turn_while_the_owners_cannot_be_read_keeps_a_stored_admin_role(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An outage runs the turn as a user's but must not write USER over the stored ADMIN."""
    teams = _app(db_session_factory, FakeSender())
    teams._teams = teams._teams.model_copy(update={"admin_team_id": ADMIN_TEAM})
    failing = AsyncMock(side_effect=GroupLookupFailed("http error (status 503)"))
    teams.runtime = dataclasses.replace(teams.runtime, team_owners=failing)
    async with db_session_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=AAD_OBJECT_ID
        )
        await set_role(session, principal.account_id, Role.ADMIN)
    roles: list[Role | None] = []
    real_admit = app_module.admit

    async def recording_admit(*args: Any, **kwargs: Any) -> Any:
        roles.append(kwargs["role"])
        return await real_admit(*args, **kwargs)

    with patched_turns("done"), patch.object(app_module, "admit", recording_admit):
        await teams._run_turn(make_inbound(), TENANT)
    assert roles == [None], "admission records no role, so the turn is a user's"
    async with db_session_factory() as session:
        account = await get_account(session, principal.account_id)
    assert account is not None and account.role is Role.ADMIN, "the stored role stands"


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize(
    "outcome", ["starts-on-free-slot", "stopped-while-queued", "balance-ran-out-while-queued"]
)
async def test_a_queued_turn_posts_its_card_then_waits_behind_it(
    outcome: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stop = outcome == "stopped-while-queued"
    depleted = outcome == "balance-ran-out-while-queued"
    sender = FakeSender()
    teams = _app(db_session_factory, sender, cap=1)
    held = teams.turn_queue.claim(TENANT)  # the tenant's one slot is taken
    ticket = teams.turn_queue.admit(TENANT, cap=1)
    assert ticket is not None and ticket.queued

    async def _run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="ok")])
        await lifecycle.on_terminal_success(state)
        return state

    async def turn() -> None:
        with holding(ticket):
            await teams._run_turn(make_inbound(), TENANT)

    run = AsyncMock(side_effect=_run_turn)
    with (
        patched_admission(),
        patch("daimon.core.turn.run.run_turn", run),
        # Admission passed before the wait; the re-check after it decides.
        patch("daimon.core.turn.slots.is_over_balance", AsyncMock(return_value=depleted)),
    ):
        task = asyncio.create_task(turn())
        async with asyncio.timeout(5):
            while not teams._cancel_registry:
                await asyncio.sleep(0.01)
        card = sender.activities[0].model_dump_json()
        assert "Working on it" in card
        assert "slot" not in card.lower() and "queue" not in card.lower()
        await asyncio.sleep(0.05)
        run.assert_not_called()
        if stop:
            [(cancel, _author, _lifecycle)] = teams._cancel_registry.values()
            cancel.set()
            async with asyncio.timeout(5):
                await task
            held.release()
            await asyncio.sleep(0.05)
            run.assert_not_called()
            assert "Stopped." in sender.activities[-1].model_dump_json()
        else:
            held.release()
            async with asyncio.timeout(5):
                await task
            if depleted:
                run.assert_not_called()
                assert "credit has run out" in sender.activities[-1].model_dump_json()
            else:
                run.assert_awaited_once()
    assert await _open_intents(db_session_factory) == [], "the card's intent is retired"
    assert teams.turn_queue.in_flight() == 0 and teams.turn_queue.depth() == 0


@pytest.mark.parametrize("missing", [("agent",), ("environment",), ("agent", "environment")])
def test_a_channel_without_setup_gets_one_plain_notice(missing: tuple[str, ...]) -> None:
    err = MissingTurnConfigError(
        missing=missing,  # pyright: ignore[reportArgumentType]
        agent_name_tier=None,
        environment_name_tier=None,
    )
    assert app_module._admission_refusal(err, uuid.uuid4()) == (  # pyright: ignore[reportPrivateUsage]
        "Daimon isn't set up in this channel yet.\n\nAsk an admin to send `setup` in a 1:1 chat with me."
    )


def test_a_stale_setup_says_it_is_out_of_date() -> None:
    err = MAResolverMissError(kind="environment", tenant_id=uuid.uuid4(), daimon_tag="gone")
    assert app_module._admission_refusal(err, uuid.uuid4()) == (  # pyright: ignore[reportPrivateUsage]
        "This channel's setup is out of date.\n\nAsk an admin to check `setup` in a 1:1 chat with me."
    )


def test_a_deleted_agent_is_named_with_where_to_pick_another() -> None:
    err = MAResolverMissError(kind="agent", tenant_id=uuid.uuid4(), daimon_tag="gone")
    assert app_module._admission_refusal(err, uuid.uuid4()) == (  # pyright: ignore[reportPrivateUsage]
        "This channel's agent `gone` can't be found.\n\n"
        "Ask an admin to pick another in `setup` in a 1:1 chat with me."
    )


def _named_post(text: str, **kwargs: Any) -> dict[str, object]:
    """A person's unmentioned channel message; Teams names the bot on `recipient`."""
    payload = make_channel_activity(mention_bot=False, text=text, **kwargs)
    return payload | {"recipient": {"id": BOT_ACCOUNT_ID, "name": "Daimon"}}


async def _route(teams: TeamsApp, payload: dict[str, object]) -> list[tuple[str, TeamsInbound]]:
    """Run `handle_message` with the turn and participation paths recorded, not run."""
    routed: list[tuple[str, TeamsInbound]] = []

    async def _handle(self: TeamsApp, inbound: TeamsInbound) -> None:
        routed.append(("turn", inbound))

    async def _observe(self: TeamsApp, inbound: TeamsInbound) -> None:
        routed.append(("participation", inbound))

    with patch.object(TeamsApp, "_handle", _handle), patch.object(TeamsApp, "_observe", _observe):
        await teams.handle_message(_message(payload, AsyncMock()))
        await asyncio.gather(*teams._tasks)
    return routed


async def _switch(sessionmaker: async_sessionmaker[AsyncSession], *, on: bool) -> None:
    async with sessionmaker.begin() as session:
        await set_plain_text_mentions(session, tenant_id=TENANT, enabled=on)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_typed_bot_name_is_a_mention_when_the_switch_is_on(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Root post and thread reply both take the mention path, with the name stripped."""
    await _switch(db_session_factory, on=True)
    teams = _app(db_session_factory, FakeSender())
    root = _named_post("@Daimon summarise the week", conversation_id=CHANNEL_ID)

    with structlog.testing.capture_logs() as logs:
        [(path, inbound)] = await _route(teams, root)
    assert path == "turn" and inbound.text == "summarise the week", (
        "the root post must run a turn without the typed name in its text"
    )
    assert "plain_text_mention.accepted" in [entry["event"] for entry in logs], (
        "a typed name that starts a turn is logged as accepted"
    )
    assert not inbound.unprompted and not inbound.by_name, "it is an ordinary mention now"
    [(path, inbound)] = await _route(teams, _named_post("@daimon", activity_id="activity-2"))
    assert (path, inbound.text, inbound.unprompted) == ("turn", "", False), (
        "a bare typed name in a thread is a bare mention over the thread, never participation"
    )


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_typed_bot_name_behaves_as_before_when_the_switch_is_off(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Off: a root post is ignored and a thread reply is screened by participation."""
    teams = _app(db_session_factory, FakeSender())
    root = _named_post("@Daimon summarise the week", conversation_id=CHANNEL_ID)

    assert await _route(teams, root) == [], "with the switch off a root post stays unanswered"
    reply = _named_post("@daimon thoughts?", activity_id="activity-2")
    [(path, inbound)] = await _route(teams, reply)
    assert (path, inbound.unprompted, inbound.text) == (
        "participation",
        True,
        "@daimon thoughts?",
    ), "with the switch off a thread reply is screened exactly as before, text untouched"


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("on", [True, False])
async def test_an_oversize_typed_bot_name_gets_the_notice_only_when_the_switch_is_on(
    db_session_factory: async_sessionmaker[AsyncSession], on: bool
) -> None:
    """On, it is a mention and owed the too-long notice, for the sender only; off, nobody asked."""
    await _switch(db_session_factory, on=on)
    teams, reply, send = _app(db_session_factory, FakeSender()), AsyncMock(), AsyncMock()
    text = "@Daimon " + "x" * MAX_INBOUND_MESSAGE_BYTES
    with structlog.testing.capture_logs() as logs:
        post = _named_post(text, conversation_id=CHANNEL_ID)
        await teams.handle_message(_message(post, reply, send))
    reply.assert_not_awaited()
    if on:
        (notice,) = send.await_args.args
        assert notice.text == INPUT_TOO_LONG
        assert notice.recipient.is_targeted, "only the sender sees a refusal in a channel"
    else:
        send.assert_not_awaited()
    events = [entry["event"] for entry in logs]
    assert "plain_text_mention.accepted" not in events, "a refusal starts no turn"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_failed_switch_read_leaves_a_typed_bot_name_unaddressed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The read failing is logged and the reply is screened as if the switch were off."""
    teams = _app(db_session_factory, FakeSender())
    failing = AsyncMock(side_effect=SQLAlchemyError("down"))
    with (
        patch.object(app_module, "get_plain_text_mentions", failing),
        structlog.testing.capture_logs() as logs,
    ):
        routed = await _route(teams, _named_post("@daimon thoughts?"))
    assert [path for path, _ in routed] == ["participation"], "it must fall back to screening"
    events = [entry["event"] for entry in logs]
    assert "plain_text_mention.lookup_failed" in events, "the failure must be logged"
    assert "teams.task_failed" not in events, "the failure must not escape the task"
