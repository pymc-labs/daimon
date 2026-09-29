"""Teams continuations: queued work and due wakes run as the requester, once."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.commands import fresh_start
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.continuity.continuation import (
    ContinuationRequest,
    ResponderChanged,
    record_continuation,
)
from daimon.core.continuity.wakes import WakeThread, enqueue_wake, poll_wakes_once
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.teams_threads import new_setup_thread_id
from daimon.core.turn.admission import AdmissionDenied
from daimon.core.turn.errors import AdmissionDenialReason, SessionAgentMismatch, SessionBusyError
from daimon.testing.factories import make_account
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    THREAD_ID,
    FakeSender,
    bot_token,
    build_teams_runtime,
    make_inbound,
    patched_admission,
    teams_settings,
)

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
_TARGET = "agt_stats"


async def _app(
    db: async_sessionmaker[AsyncSession], sender: FakeSender
) -> tuple[TeamsApp, uuid.UUID]:
    """The app over a live tenant whose MA knows the handoff target, and the requester."""
    await provision_tenant(db, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with db() as session:
        tenant = await get_tenant(session, TENANT)
        assert tenant is not None
        account = await make_account(session, tenant=tenant)
        await session.commit()
    router = MARouter()
    router.add_agent(
        ma_agent(
            id=_TARGET, name="stats-bot", model="m", tenant_id=TENANT, created_at=datetime.now(UTC)
        )
    )
    # What `patched_admission` resolves: the chat's current responder.
    router.add_agent(ma_agent(id="agent_test_id", name="daimon", model="m", tenant_id=TENANT))
    router.add_environment(ma_environment(id="env_test_id", tenant_id=TENANT))
    runtime = build_teams_runtime(db, anthropic=build_fake_anthropic(router.dispatch))
    teams = TeamsApp(
        runtime=runtime, sender=sender, commands={"new": fresh_start}, bot_token=bot_token
    )
    return teams, account.id


async def _hand_off(
    db: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    thread_id: str = CONVERSATION_ID,
    parent: str = CONVERSATION_ID,
    *,
    timer: bool = False,
) -> uuid.UUID:
    """What hand_off_task records mid-turn, or with `timer` what create_timer records, due."""
    request = ContinuationRequest(
        tenant_id=TENANT,
        platform="teams",
        parent_channel_id=parent,
        thread_id=thread_id,
        requester_account_id=account_id,
        requester_external_user_id=AAD_OBJECT_ID,
        target_ma_agent_id=_TARGET,
        target_name="stats-bot",
        requested_work="check the deploy" if timer else "finish the writeup",
        reason="timer" if timer else "task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    if timer:
        await enqueue_wake(db, request, available_at=datetime.now(UTC) - timedelta(seconds=1))
    else:
        await record_continuation(db, request)
    return request.idempotency_key


async def _poll(db: async_sessionmaker[AsyncSession], teams: TeamsApp) -> int:
    opened = await poll_wakes_once(
        db, platform="teams", open_thread=teams._open_wake_thread, now=datetime.now(UTC)
    )
    await asyncio.gather(*teams._tasks)
    return opened


async def _status(db: async_sessionmaker[AsyncSession], key: uuid.UUID) -> tuple[str, str | None]:
    async with db() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    return row.status, row.skip_reason


@pytest.mark.parametrize("in_setup", [False, True], ids=["chat", "setup"])
async def test_a_handoff_runs_after_the_turn_as_the_requester(
    db_session_factory: async_sessionmaker[AsyncSession], in_setup: bool
) -> None:
    teams, account_id = await _app(db_session_factory, FakeSender())
    setup = new_setup_thread_id(CONVERSATION_ID) if in_setup else None
    if setup is not None:
        async with db_session_factory.begin() as session:
            await create_binding(
                session,
                tenant_id=TENANT,
                platform="teams",
                parent_channel_id=CONVERSATION_ID,
                thread_id=setup,
                responder_ma_agent_id=_TARGET,
                responder_name="stats-bot",
            )
    calls: list[tuple[TeamsInbound, dict[str, Any]]] = []
    keys: list[uuid.UUID] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID, **kw: Any) -> None:
        calls.append((inbound, kw))
        if len(calls) == 1:
            keys.append(await _hand_off(db_session_factory, account_id, inbound.thread_id))

    with patch.object(TeamsApp, "_run_turn", _turn):
        await teams._orchestrate(make_inbound("hand this to stats-bot"), TENANT)

    (_, _), (follow, kw) = calls
    assert (follow.text, follow.user_id, follow.conversation_id, follow.kind) == (
        "finish the writeup",
        AAD_OBJECT_ID,
        CONVERSATION_ID,
        "dm",
    )
    assert follow.thread_id == (setup or CONVERSATION_ID), "a setup handoff stays in setup"
    assert kw["reraise"] is True and kw["handoff"] is not None
    assert await _status(db_session_factory, keys[0]) == ("delivered", None)


async def test_a_newer_message_supersedes_the_queued_work(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams, account_id = await _app(db_session_factory, FakeSender())
    ran: list[str] = []
    keys: list[uuid.UUID] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID, **kw: Any) -> None:
        ran.append(inbound.text)
        if len(ran) == 1:
            keys.append(await _hand_off(db_session_factory, account_id))
            await teams._orchestrate(make_inbound("actually, stop"), TENANT)

    with patch.object(TeamsApp, "_run_turn", _turn):
        await teams._orchestrate(make_inbound("hand this to stats-bot"), TENANT)

    assert ran == ["hand this to stats-bot", "actually, stop"], "the stale work never runs"
    assert await _status(db_session_factory, keys[0]) == ("skipped", "skip_superseded")


@pytest.mark.parametrize(
    ("reason", "copy"),
    [
        ("balance_depleted", app_module._BALANCE_DEPLETED),
        ("cap_exceeded", app_module._CAP_REACHED),
        ("invoker_not_allowed", app_module._NOT_INVITED),
    ],
)
async def test_a_refused_turn_tells_only_a_live_person_then_raises(
    db_session_factory: async_sessionmaker[AsyncSession], reason: AdmissionDenialReason, copy: str
) -> None:
    sender = FakeSender()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={"new": fresh_start},
        bot_token=bot_token,
    )
    denied = AsyncMock(side_effect=AdmissionDenied(reason=reason))
    with patch.object(app_module, "admit", denied), pytest.raises(AdmissionDenied):
        await teams._run_turn(make_inbound("w"), TENANT, reraise=True)
    queued = cast(TaskContinuationRow, object())
    with patch.object(app_module, "admit", denied), pytest.raises(AdmissionDenied):
        await teams._run_turn(make_inbound("w"), TENANT, reraise=True, continuation=queued)
    assert [a.text for a in sender.activities] == [copy], "queued work settles silently"


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize(
    "error",
    [
        SessionBusyError(pending_reasons=("agent",), retry_after=datetime.now(UTC)),
        SessionAgentMismatch(
            mapping_id=uuid.uuid4(), session_id="s", source_agent_id="a", destination_agent_id="b"
        ),
    ],
    ids=["busy", "mismatch"],
)
async def test_a_refused_bind_reaches_the_dispatcher_without_a_failure_report(
    db_session_factory: async_sessionmaker[AsyncSession], error: Exception
) -> None:
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
    )
    with (
        patched_admission(),
        patch.object(app_module, "bind_session", AsyncMock(side_effect=error)),
        patch.object(app_module, "capture_exception_with_scope") as sentry,
        pytest.raises(type(error)),
    ):
        await teams._run_turn(make_inbound("w"), TENANT, reraise=True)
    sentry.assert_not_called()


async def test_a_saved_input_runs_its_work_now_or_once_the_busy_turn_ends(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams, account_id = await _app(db_session_factory, FakeSender())
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID, **kw: Any) -> None:
        ran.append(inbound.text)

    with patch.object(TeamsApp, "_run_turn", _turn):
        idle = await _hand_off(db_session_factory, account_id)
        await teams.dispatch_after_input(TENANT, CONVERSATION_ID, SERVICE_URL)
        assert ran == ["finish the writeup"], "an idle conversation runs it at once"

        busy = await _hand_off(db_session_factory, account_id)
        teams._processing.add(CONVERSATION_ID)
        await teams.dispatch_after_input(TENANT, CONVERSATION_ID, SERVICE_URL)
        assert len(ran) == 1, "a busy conversation is not interrupted"
        teams._release(CONVERSATION_ID)
        await asyncio.gather(*teams._tasks)

    assert len(ran) == 2, "the release re-runs the deferred dispatch"
    assert await _status(db_session_factory, idle) == ("delivered", None)
    assert await _status(db_session_factory, busy) == ("delivered", None)


async def test_the_poller_opens_a_due_timer_and_runs_it_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams, account_id = await _app(db_session_factory, FakeSender())
    key = await _hand_off(db_session_factory, account_id, timer=True)
    calls: list[tuple[TeamsInbound, dict[str, Any]]] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID, **kw: Any) -> None:
        calls.append((inbound, kw))

    with patch.object(TeamsApp, "_run_turn", _turn):
        assert await _poll(db_session_factory, teams) == 1, "the chat with a due timer opens"
        assert await _poll(db_session_factory, teams) == 0, "a delivered timer is not polled again"

    [(follow, kw)] = calls
    assert "check the deploy" in follow.text and follow.user_id == AAD_OBJECT_ID
    assert follow.service_url is None, "a wake stores no service URL: the SDK default is used"
    assert kw["continuation"].idempotency_key == key and kw["reraise"] is True
    assert await _status(db_session_factory, key) == ("delivered", None)
    gone = WakeThread(
        tenant_id=uuid.uuid4(),
        platform="teams",
        parent_channel_id=CONVERSATION_ID,
        thread_id=CONVERSATION_ID,
        requester_account_id=account_id,
    )
    assert await teams._open_wake_thread(gone) is False, "a missing tenant is pushed back"
    other = await provision_tenant(
        db_session_factory, platform="teams", workspace_id=str(uuid.uuid4())
    )
    foreign = gone.model_copy(update={"tenant_id": other.tenant_id})
    assert await teams._open_wake_thread(foreign) is False, "another organisation's rows wait"


async def test_a_due_timer_waits_while_the_tenant_is_at_its_turn_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Wakes count against the per-tenant cap: at the cap the timer stays due for the next poll."""
    teams, account_id = await _app(db_session_factory, FakeSender())
    key = await _hand_off(db_session_factory, account_id, timer=True)
    ran: list[TeamsInbound] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID, **kw: Any) -> None:
        ran.append(inbound)

    with patch.object(TeamsApp, "_run_turn", _turn):
        teams._inflight[TENANT] = teams._teams.max_concurrent_turns_per_tenant
        await _poll(db_session_factory, teams)
        assert ran == [], "no wake runs over the cap"
        assert (await _status(db_session_factory, key))[0] == "pending", "the timer stays due"
        del teams._inflight[TENANT]
        await _poll(db_session_factory, teams)
    assert len(ran) == 1, "the next poll under the cap runs it"
    assert await _status(db_session_factory, key) == ("delivered", None)


async def test_a_timer_whose_chat_changed_responder_says_so_and_never_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams, account_id = await _app(db_session_factory, sender)
    key = await _hand_off(db_session_factory, account_id, timer=True)

    with patched_admission():
        await _poll(db_session_factory, teams)

    notice = ResponderChanged(target_name="stats-bot", current_name="daimon").message
    assert [a.text for a in sender.activities] == [notice], "the notice, and no status card"
    assert await _status(db_session_factory, key) == ("skipped", "skip_target_changed")


async def test_a_continuation_in_a_protected_channel_settles_without_a_word(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SYS-048: admission refuses it as channel_protected; nothing is posted and
    the row still settles."""
    sender = FakeSender()
    teams, account_id = await _app(db_session_factory, sender)
    policy = TenantAccessPolicy(protected_channel_ids=(CHANNEL_ID,))
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    key = await _hand_off(db_session_factory, account_id, THREAD_ID, parent=CHANNEL_ID)

    with patched_admission():
        await teams.dispatch_after_input(TENANT, THREAD_ID, SERVICE_URL)

    assert sender.sent == [], "a protected channel hears nothing, not even the refusal"
    assert await _status(db_session_factory, key) == (
        "skipped",
        "admission_denied:channel_protected",
    )


async def test_a_wake_deferred_behind_a_turn_keeps_the_saved_input_and_the_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams, _ = await _app(db_session_factory, FakeSender())
    teams._processing.update({CONVERSATION_ID, THREAD_ID})
    await teams.dispatch_after_input(TENANT, CONVERSATION_ID, SERVICE_URL)
    await teams.dispatch_after_input(TENANT, CONVERSATION_ID, None, capped=True)
    await teams.dispatch_after_input(TENANT, THREAD_ID, None, capped=True)
    assert teams._deferred_dispatch[CONVERSATION_ID] == (TENANT, SERVICE_URL, False), (
        "a saved input's URL survives the wake, and its resume ignores the cap"
    )
    assert teams._deferred_dispatch[THREAD_ID] == (TENANT, None, True), "a wake alone stays capped"

    ran: list[str] = []

    async def _dispatch(self: TeamsApp, thread_id: str, *_: Any) -> None:
        ran.append(thread_id)

    with patch.object(TeamsApp, "_dispatch_continuations", _dispatch):
        teams._inflight[TENANT] = teams._teams.max_concurrent_turns_per_tenant
        teams._release(CONVERSATION_ID)
        teams._release(THREAD_ID)
        await asyncio.gather(*teams._tasks)
    assert ran == [CONVERSATION_ID], "at the cap the saved input resumes and the wake waits"


async def test_a_disabled_deployment_runs_no_wake_poller(
    db_session_factory: async_sessionmaker[AsyncSession], no_wake_poller: AsyncMock
) -> None:
    runtime = build_teams_runtime(db_session_factory, teams=teams_settings(enabled=False))
    teams = TeamsApp(runtime=runtime, sender=FakeSender(), commands={}, bot_token=bot_token)
    await teams.start()
    await teams.drain(timeout=1.0)
    no_wake_poller.assert_not_called()


async def test_start_runs_the_teams_wake_poller_until_drain(
    db_session_factory: async_sessionmaker[AsyncSession],
    no_wake_poller: AsyncMock,
    no_boot_provisioning: AsyncMock,
) -> None:
    teams = TeamsApp(
        runtime=build_teams_runtime(
            db_session_factory, teams=teams_settings(admins=(AAD_OBJECT_ID,))
        ),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
    )
    await teams.start()
    kwargs = no_wake_poller.call_args.kwargs
    assert kwargs["platform"] == "teams" and kwargs["open_thread"] == teams._open_wake_thread
    assert kwargs["should_stop"]() is False, "the poller runs while the app admits turns"
    await teams.drain(timeout=1.0)
    assert no_boot_provisioning.call_args.kwargs["admin_user_ids"] == (AAD_OBJECT_ID,)
    assert kwargs["should_stop"]() is True, "drain stops the poller"
