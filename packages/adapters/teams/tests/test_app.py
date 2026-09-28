"""TeamsApp: per-thread queueing, the tenant cap, Cancel, and turn bookkeeping."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import TurnCardIntentRow
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.turn.admission import AdmissionDenied
from daimon.core.turn.state import TextBlock, TurnState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    FakeSender,
    build_teams_runtime,
    patched_admission,
)

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


def _inbound(
    text: str = "hi", *, user: str = AAD_OBJECT_ID, conversation: str = CONVERSATION_ID
) -> TeamsInbound:
    return TeamsInbound(
        kind="dm",
        entra_tenant_id=ENTRA_TENANT_ID,
        user_id=user,
        conversation_id=conversation,
        channel_id=conversation,
        activity_id=str(uuid.uuid4()),
        text=text,
        service_url=SERVICE_URL,
    )


def _app(
    db_factory: async_sessionmaker[AsyncSession], sender: FakeSender, cap: int = 3
) -> TeamsApp:
    runtime = build_teams_runtime(db_factory)
    runtime.settings.teams = runtime.settings.teams.model_copy(
        update={"max_concurrent_turns_per_tenant": cap}
    )
    return TeamsApp(runtime=runtime, sender=sender)


@pytest.mark.asyncio
async def test_messages_during_a_turn_queue_and_run_once_per_author(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    release = asyncio.Event()
    ran: list[TeamsInbound] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound)
        if len(ran) == 1:
            await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        first = asyncio.create_task(teams._orchestrate(_inbound("one"), TENANT))
        await asyncio.sleep(0)
        await teams._orchestrate(_inbound("two"), TENANT)
        await teams._orchestrate(_inbound("three"), TENANT)
        await teams._orchestrate(_inbound("other", user=OTHER_AAD_OBJECT_ID), TENANT)
        assert len(ran) == 1, "queued messages wait for the running turn"
        release.set()
        await first

    assert [(i.user_id, i.text) for i in ran] == [
        (AAD_OBJECT_ID, "one"),
        (AAD_OBJECT_ID, "two\n\nthree"),
        (OTHER_AAD_OBJECT_ID, "other"),
    ]


@pytest.mark.asyncio
async def test_the_tenant_cap_sheds_a_new_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender, cap=1)
    release = asyncio.Event()

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        first = asyncio.create_task(teams._orchestrate(_inbound(), TENANT))
        await asyncio.sleep(0)
        await teams._orchestrate(_inbound(conversation="a:conversation-2"), TENANT)
        release.set()
        await first

    assert [(c, a.text) for c, a, _ in sender.sent] == [("a:conversation-2", app_module._SHED)]


def _click(verb: str, key: str, clicker: str) -> Any:
    action = SimpleNamespace(verb=verb, data={"turn": key})
    activity = SimpleNamespace(
        value=SimpleNamespace(action=action), from_=SimpleNamespace(aad_object_id=clicker)
    )
    return SimpleNamespace(activity=activity)


@pytest.mark.asyncio
async def test_only_the_author_can_cancel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _app(db_session_factory, FakeSender())
    cancel = asyncio.Event()
    teams._cancel_registry["key-1"] = (cancel, AAD_OBJECT_ID)

    refused = await teams.handle_card_action(_click("cancel_turn", "key-1", OTHER_AAD_OBJECT_ID))
    assert refused.value == app_module._CANCEL_NOT_AUTHOR and not cancel.is_set()
    ended = await teams.handle_card_action(_click("cancel_turn", "key-2", AAD_OBJECT_ID))
    assert ended.value == app_module._CANCEL_TURN_ENDED
    accepted = await teams.handle_card_action(_click("cancel_turn", "key-1", AAD_OBJECT_ID.upper()))
    assert accepted.value == app_module._CANCELLING and cancel.is_set()


async def _open_intents(db_factory: async_sessionmaker[AsyncSession]) -> list[TurnCardIntentRow]:
    async with db_factory() as session:
        return await list_recoverable_turn_card_intents(session, platform="teams")


@pytest.mark.asyncio
async def test_a_finished_turn_retires_its_card_intent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    sender = FakeSender()
    teams = _app(db_session_factory, sender)

    async def _run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="done")])
        await lifecycle.on_terminal_success(state)
        return state

    with patched_admission(), patch("daimon.core.turn.run.run_turn", side_effect=_run_turn):
        await teams._run_turn(_inbound(), TENANT)

    assert "m-1" in {a.id for a in sender.activities[1:]}, "the answer edits the posted card"
    assert await _open_intents(db_session_factory) == [], "a closed card retires its intent"
    assert teams._cancel_registry == {}, "the Cancel key is dropped with the turn"


@pytest.mark.asyncio
async def test_a_turn_cut_off_by_shutdown_keeps_its_intent_for_the_boot_sweep(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    teams = _app(db_session_factory, FakeSender())
    started = asyncio.Event()

    async def _run_turn(**kwargs: Any) -> TurnState:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patched_admission(), patch("daimon.core.turn.run.run_turn", side_effect=_run_turn):
        task = asyncio.create_task(teams._run_turn(_inbound(), TENANT))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    [intent] = await _open_intents(db_session_factory)
    assert (intent.status, intent.message_id) == ("posted", "m-1"), "left for the boot sweep"


@pytest.mark.asyncio
async def test_shutdown_after_the_answer_still_retires_the_intent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cut off while overflow chunks send, the delivered answer must not become a restart notice."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    teams = _app(db_session_factory, FakeSender())
    answered = asyncio.Event()

    async def _run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        await lifecycle.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="ok")]))
        answered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patched_admission(), patch("daimon.core.turn.run.run_turn", side_effect=_run_turn):
        task = asyncio.create_task(teams._run_turn(_inbound(), TENANT))
        await answered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await _open_intents(db_session_factory) == [], "a closed card is not left for the sweep"


@pytest.mark.asyncio
async def test_a_denied_turn_says_why_without_a_card(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    denied = AsyncMock(side_effect=AdmissionDenied(reason="balance_depleted"))
    with patch.object(app_module, "admit", denied):
        await teams._run_turn(_inbound(), TENANT)
    assert [a.text for a in sender.activities] == [app_module._BALANCE_DEPLETED]
    assert await _open_intents(db_session_factory) == [], "no card, so no intent"
