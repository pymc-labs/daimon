"""TeamsApp: per-thread queueing, the tenant cap, Cancel, and turn bookkeeping."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.commands import fresh_start
from daimon.adapters.teams.context import HistoryBlock
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import TurnCardIntentRow
from daimon.core.stores.tenants import set_turn_cap
from daimon.core.stores.thread_agent_bindings import create_binding, update_lifecycle
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.stores.turn_outcomes import OutcomeRecord, list_for_tenant
from daimon.core.teams_threads import new_setup_thread_id
from daimon.core.turn.admission import AdmissionDenied
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.outcomes import drain_outcomes
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


async def _outcomes(db_factory: async_sessionmaker[AsyncSession]) -> list[OutcomeRecord]:
    await drain_outcomes()
    async with db_factory() as session:
        return await list_for_tenant(session, TENANT)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_the_tenant_cap_sheds_a_new_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender, cap=1)
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
    assert [a.text for a in sender.activities] == [app_module._BALANCE_DEPLETED]
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

    assert [a.text for a in sender.activities] == ["Thursday, per the release notes."]
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
