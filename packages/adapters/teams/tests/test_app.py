"""TeamsApp: per-thread queueing, the tenant cap, Cancel, and turn bookkeeping."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from types import SimpleNamespace
from typing import Any, cast, get_args
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.commands import ANSWERED_IN_CHAT, CHANNEL_POINTER, fresh_start
from daimon.adapters.teams.context import HistoryBlock
from daimon.adapters.teams.externals import Membership
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_external
from daimon.core.stores.domain import Role, TurnCardIntentRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.tenants import set_turn_cap
from daimon.core.stores.thread_agent_bindings import create_binding, update_lifecycle
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.stores.turn_outcomes import OutcomeRecord, list_for_tenant
from daimon.core.teams_threads import new_setup_thread_id
from daimon.core.turn.admission import AdmissionDenied, ExternalFinding
from daimon.core.turn.errors import AdmissionDenialReason, MissingTurnConfigError
from daimon.core.turn.notices import admission_refusal_text
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.slots import holding, wait_for_slot
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from microsoft_teams.api import MessageActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
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

    async def post(self, conversation_id: str, text: str) -> None:
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


async def _outcomes(db_factory: async_sessionmaker[AsyncSession]) -> list[OutcomeRecord]:
    await drain_outcomes()
    async with db_factory() as session:
        return await list_for_tenant(session, TENANT)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_over_the_tenant_cap_a_new_thread_waits_for_a_slot(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
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


def _click(key: str, clicker: str) -> Any:
    action = SimpleNamespace(data={"action": "cancel_turn", "turn": key})
    activity = SimpleNamespace(
        value=SimpleNamespace(action=action), from_=SimpleNamespace(aad_object_id=clicker)
    )
    return SimpleNamespace(activity=activity)


def _message(payload: dict[str, object], reply: AsyncMock) -> Any:
    activity = MessageActivity.model_validate(payload)
    return SimpleNamespace(
        activity=activity, conversation_ref=SimpleNamespace(service_url=SERVICE_URL), reply=reply
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
        await teams.handle_message(
            _message(make_message_activity(conversation_type="groupChat"), reply)
        )
    reply.assert_awaited_once()
    assert {
        "event": "teams.refusal.send_failed",
        "log_level": "warning",
        "conversation_type": "groupChat",
        "reason": "ConnectError",
    } in logs, "a refusal nobody saw is still on record"


async def test_only_the_author_can_cancel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    cancel = asyncio.Event()
    teams._cancel_registry["key-1"] = (cancel, AAD_OBJECT_ID)

    refused = await teams.handle_cancel(_click("key-1", OTHER_AAD_OBJECT_ID))
    assert refused.value == app_module._CANCEL_NOT_AUTHOR and not cancel.is_set()
    ended = await teams.handle_cancel(_click("key-2", AAD_OBJECT_ID))
    assert ended.value == app_module._CANCEL_TURN_ENDED
    accepted = await teams.handle_cancel(_click("key-1", AAD_OBJECT_ID.upper()))
    assert accepted.value == app_module._CANCELLING and cancel.is_set()


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
        "This organisation's credit is depleted. "
        "An admin can top up with `billing` in a 1:1 chat with me."
    ], "the refusal says why, in the organisation's nouns"
    assert await _open_intents(db_session_factory) == [], "no card, so no intent"


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


def _external(text: str = "hi") -> TeamsInbound:
    inbound = make_inbound(text, conversation=THREAD_ID, kind="channel")
    return dataclasses.replace(inbound, channel_id=CHANNEL_ID, is_external=True)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_an_external_participants_command_is_not_intercepted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender, direct, memory = FakeSender(), _Direct(), AsyncMock()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={"memory": memory, "new": fresh_start},
        bot_token=bot_token,
        direct=direct,
    )
    orchestrate = AsyncMock()
    for text in ("memory", "new"):
        with patch.object(teams, "_orchestrate", orchestrate):
            await teams._handle(_external(text))
    assert [call.args[0].text for call in orchestrate.await_args_list] == ["memory", "new"]
    assert (memory.await_count, direct.looked_up, sender.sent) == (0, [], []), (
        "no command, no 1:1 chat attempt, no pointer"
    )


def test_an_external_participant_is_never_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    teams._teams = teams._teams.model_copy(update={"admin_user_ids": [AAD_OBJECT_ID]})
    assert teams._role(make_inbound()) is Role.ADMIN, "listed and ours"
    assert teams._role(_external()) is Role.USER, "listed, but from another organisation"


@dataclasses.dataclass
class _Externals:
    membership: Membership
    asked: list[dict[str, Any]] = dataclasses.field(default_factory=list[dict[str, Any]])

    async def classify(self, **kwargs: Any) -> Membership:
        self.asked.append(kwargs)
        return self.membership


async def test_every_sender_is_classified_with_what_the_activity_said(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    externals = _Externals(Membership(is_external=True, is_known=True, home_tenant_id="other"))
    teams.runtime = dataclasses.replace(teams.runtime, externals=cast(Any, externals))
    channel = dataclasses.replace(_external(), is_external=False, channel_type="shared")
    marked = await teams._classified(channel)
    assert (marked.is_external, marked.is_external_known, marked.home_tenant_id) == (
        True,
        True,
        "other",
    )
    dm = await teams._classified(make_inbound())
    foreign = dataclasses.replace(_external(), is_external_known=True, home_tenant_id="other")
    await teams._classified(foreign)
    asked = [(a["kind"], a["conversation_id"], a["foreign_tenant"]) for a in externals.asked]
    assert asked == [
        ("channel", CHANNEL_ID, None),
        ("dm", CONVERSATION_ID, None),
        ("channel", CHANNEL_ID, "other"),
    ], "a 1:1 chat too (guests); a foreign tenant the activity named is passed on"
    assert dm.is_external, "a guest in a 1:1 chat gets the external rules"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_stored_external_flag_holds_when_nothing_placed_the_sender(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    async with db_session_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=AAD_OBJECT_ID
        )
        await set_external(session, principal.account_id, True)
    unknown = make_inbound()
    assert (await teams._stored_external(unknown, TENANT)).is_external
    known = dataclasses.replace(unknown, is_external_known=True)
    assert await teams._stored_external(known, TENANT) is known, "evidence wins"
    other = make_inbound(user="22222222-2222-4222-8222-222222222222")
    assert not (await teams._stored_external(other, TENANT)).is_external


async def test_an_external_participant_outside_an_isolated_channel_is_told_where_to_ask(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    denied = AsyncMock(side_effect=AdmissionDenied(reason="external_participant"))
    with patch.object(app_module, "admit", denied):
        await teams._run_turn(_external(), TENANT)
        await teams._run_turn(make_inbound(), TENANT)
    flags = [call.kwargs["external"] for call in denied.await_args_list]
    assert flags == [ExternalFinding(True, False), ExternalFinding(False, False)], (
        "admission is told how each is treated, and that nothing placed them"
    )
    refusal = admission_refusal_text("external_participant", app_module.TEAMS_REFUSAL_NOUNS)
    assert [a.text for a in sender.activities] == [refusal] * 2, "the shared refusal copy"


@dataclasses.dataclass
class _Following:
    """A participation that follows every thread and records what it would batch."""

    batched: list[TeamsInbound | None] = dataclasses.field(
        default_factory=list[TeamsInbound | None]
    )

    async def observe(self, inbound: TeamsInbound, tenant_id: uuid.UUID, *, admitted: Any) -> None:
        self.batched.append(await admitted())


@pytest.mark.usefixtures("provisioned_tenant")
async def test_an_unmentioned_external_reply_is_judged_only_inside_an_isolated_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    externals = _Externals(Membership(is_external=True, is_known=True, home_tenant_id="other"))
    teams.runtime = dataclasses.replace(teams.runtime, externals=cast(Any, externals))
    following = _Following()
    teams._participation = cast(Any, following)
    reply = dataclasses.replace(_external(), is_external=False)
    await teams._observe(reply)
    async with db_session_factory.begin() as session:
        policy = TenantAccessPolicy(
            isolated_channel_ids=(CHANNEL_ID,), sealed_channel_ids=(CHANNEL_ID,)
        )
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    await teams._observe(reply)
    outside, inside = following.batched
    assert outside is None, "admission would refuse them: nothing they wrote is judged"
    assert inside is not None and inside.is_external, "batched as classified"
    assert len(externals.asked) == 2, "classified inside admitted, after the cheap checks"


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
            ((cancel, _author),) = teams._cancel_registry.values()
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
                assert "credit is depleted" in sender.activities[-1].model_dump_json()
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
        "Daimon isn't set up in this channel yet.\n\nAsk an admin to send setup to Daimon."
    )


def test_a_stale_setup_says_it_is_out_of_date() -> None:
    err = MAResolverMissError(kind="agent", tenant_id=uuid.uuid4(), daimon_tag="gone")
    assert app_module._admission_refusal(err, uuid.uuid4()) == (  # pyright: ignore[reportPrivateUsage]
        "This channel's setup is out of date.\n\nAsk an admin to send setup to Daimon."
    )
