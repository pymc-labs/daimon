from __future__ import annotations

import asyncio
import datetime as dt
import re
import uuid
from typing import Any

import aiohttp
import pytest
from aioresponses import CallbackResult
from cryptography.fernet import Fernet
from daimon.adapters.slack import boot_sweep
from daimon.adapters.slack.boot_sweep import recover_slack_card_intents
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import TurnCardIntentRow
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    list_recoverable_turn_card_intents,
    record_turn_card_message,
)
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import build_slack_runtime

_REPLIES = re.compile(r"https://slack\.com/api/conversations\.replies.*")
_UPDATE_URL = "https://slack.com/api/chat.update"
_TEAM_ID = "T_CARD_RECOVERY"
_THREAD_ID = "1000000000.000001"
_CHANNEL_ID = "C_TEST"


def _page(messages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "ok": True,
        "messages": messages,
        "has_more": False,
        "response_metadata": {"next_cursor": ""},
    }


def _message(ts: str, key: str) -> dict[str, object]:
    return {
        "ts": ts,
        "blocks": [
            {
                "type": "actions",
                "elements": [{"type": "button", "action_id": "cancel_turn", "value": key}],
            }
        ],
    }


class _Clock:
    def __init__(self, started_at: dt.datetime) -> None:
        self.started_at = started_at
        self.seconds = 0.0

    def now(self) -> dt.datetime:
        return self.started_at + dt.timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    async def sleep(self, seconds: float) -> None:
        self.seconds += seconds


async def _create_intent(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: str = _THREAD_ID,
    channel_id: str | None = _CHANNEL_ID,
    message_id: str | None = None,
) -> TurnCardIntentRow:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    await provision_tenant(db_session_factory, platform="slack", workspace_id=_TEAM_ID)
    async with db_session_factory() as session:
        intent = await create_turn_card_intent(
            session,
            tenant_id=tenant_id,
            platform="slack",
            thread_id=thread_id,
            turn_token=uuid.uuid4(),
            channel_id=channel_id,
        )
        if message_id is not None:
            assert await record_turn_card_message(
                session,
                id=intent.id,
                message_id=message_id,
            )
        await session.commit()
    remaining = await _remaining_intents(db_session_factory)
    return next(row for row in remaining if row.id == intent.id)


async def _remaining_intents(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> list[TurnCardIntentRow]:
    async with db_session_factory() as session:
        return await list_recoverable_turn_card_intents(session, platform="slack")


async def _run_recovery(
    runtime: SlackRuntime,
    intents: list[TurnCardIntentRow],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    async def resolve(_runtime: SlackRuntime, *, team_id: str) -> Any:
        assert team_id == _TEAM_ID
        return fake_slack_web_client.client

    monkeypatch.setattr(boot_sweep, "resolve_web_client", resolve)
    await recover_slack_card_intents(
        runtime,
        intents,
        sleep=clock.sleep,
        now=clock.now,
        monotonic=clock.monotonic,
    )


async def test_two_complete_no_match_reads_retire_prepared_intent(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(db_session_factory)
    clock = _Clock(intent.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(_REPLIES, payload=_page([]), repeat=True)

    await _run_recovery(
        runtime,
        [intent],
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    assert await _remaining_intents(db_session_factory) == []
    assert clock.seconds >= 60.0
    assert (
        sum(
            len(requests)
            for (method, url), requests in fake_slack_web_client.mock.requests.items()
            if method == "GET" and url.path == "/api/conversations.replies"
        )
        == 2
    )


async def test_duplicate_matches_are_all_edited_before_intent_retirement(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(db_session_factory)
    clock = _Clock(intent.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(
        _REPLIES,
        payload=_page(
            [
                _message(f"{intent.created_at.timestamp() + 0.1:.6f}", intent.id.hex),
                _message(f"{intent.created_at.timestamp() + 0.2:.6f}", intent.id.hex),
            ]
        ),
    )
    fake_slack_web_client.mock.post(
        _UPDATE_URL,
        payload={
            "ok": True,
            "ts": f"{intent.created_at.timestamp() + 0.1:.6f}",
            "channel": _CHANNEL_ID,
        },
        repeat=True,
    )

    await _run_recovery(
        runtime,
        [intent],
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    update_calls = [
        request.kwargs["json"]
        for (method, url), requests in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.update"
        for request in requests
    ]
    assert [call["ts"] for call in update_calls] == [
        f"{intent.created_at.timestamp() + 0.1:.6f}",
        f"{intent.created_at.timestamp() + 0.2:.6f}",
    ]
    assert await _remaining_intents(db_session_factory) == []


async def test_failed_live_card_edit_leaves_recorded_intent_recoverable(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(db_session_factory)
    clock = _Clock(intent.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    message_ts = f"{intent.created_at.timestamp() + 0.1:.6f}"
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(
        _REPLIES,
        payload=_page([_message(message_ts, intent.id.hex)]),
    )
    fake_slack_web_client.mock.post(
        _UPDATE_URL,
        payload={"ok": False, "error": "message_not_found"},
    )

    await _run_recovery(
        runtime,
        [intent],
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    remaining = await _remaining_intents(db_session_factory)
    assert len(remaining) == 1
    assert remaining[0].status == "posted"
    assert remaining[0].message_id == message_ts


async def test_separate_same_thread_intents_reconcile_their_own_cards(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _create_intent(db_session_factory)
    second = await _create_intent(db_session_factory)
    clock = _Clock(first.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(
        _REPLIES,
        payload=_page(
            [
                _message(f"{first.created_at.timestamp() + 0.1:.6f}", first.id.hex),
                _message(f"{second.created_at.timestamp() + 0.2:.6f}", second.id.hex),
            ]
        ),
        repeat=True,
    )
    fake_slack_web_client.mock.post(
        _UPDATE_URL,
        payload={"ok": True, "ts": "1000000000.000011", "channel": _CHANNEL_ID},
        repeat=True,
    )

    await _run_recovery(
        runtime,
        [first, second],
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    get_calls = [
        request
        for (method, url), requests in fake_slack_web_client.mock.requests.items()
        if method == "GET" and url.path == "/api/conversations.replies"
        for request in requests
    ]
    update_calls = [
        request.kwargs["json"]
        for (method, url), requests in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.update"
        for request in requests
    ]
    assert len(get_calls) == 2
    assert {call["ts"] for call in update_calls} == {
        f"{first.created_at.timestamp() + 0.1:.6f}",
        f"{second.created_at.timestamp() + 0.2:.6f}",
    }
    assert await _remaining_intents(db_session_factory) == []


async def test_terminal_known_card_is_retired_without_editing_its_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(
        db_session_factory,
        message_id=f"{dt.datetime.now(dt.UTC).timestamp():.6f}",
    )
    clock = _Clock(intent.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(_REPLIES, payload=_page([]))

    await _run_recovery(
        runtime,
        [intent],
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    assert await _remaining_intents(db_session_factory) == []
    assert not any(
        method == "POST" and str(url) == _UPDATE_URL
        for method, url in fake_slack_web_client.mock.requests
    )


async def test_known_message_id_uses_its_bounded_timestamp_window_after_expiry(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(
        db_session_factory,
        message_id=f"{dt.datetime.now(dt.UTC).timestamp():.6f}",
    )
    clock = _Clock(intent.created_at + dt.timedelta(minutes=6))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(_REPLIES, payload=_page([]))

    await _run_recovery(
        runtime,
        [intent],
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    remaining = await _remaining_intents(db_session_factory)
    assert remaining == []


async def test_cancellation_closes_recovery_client_session(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(db_session_factory)
    clock = _Clock(intent.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(
        _REPLIES,
        status=429,
        headers={"Retry-After": "90"},
        payload={"ok": False, "error": "ratelimited"},
    )
    sleep_started = asyncio.Event()
    never = asyncio.Event()
    session = aiohttp.ClientSession()
    recovery_client = AsyncWebClient(token="xoxb-test", session=session)

    async def hold_retry(_seconds: float) -> None:
        sleep_started.set()
        await never.wait()

    async def resolve(_runtime: SlackRuntime, *, team_id: str) -> Any:
        assert team_id == _TEAM_ID
        return recovery_client

    monkeypatch.setattr(boot_sweep, "resolve_web_client", resolve)
    task = asyncio.create_task(
        recover_slack_card_intents(
            runtime,
            [intent],
            sleep=hold_retry,
            now=clock.now,
            monotonic=clock.monotonic,
        )
    )
    await asyncio.wait_for(sleep_started.wait(), timeout=2)
    assert not session.closed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed, "cancellation must close the per-tenant Slack HTTP session"


async def test_indeterminate_search_keeps_intent_recoverable(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(db_session_factory)
    clock = _Clock(intent.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(
        _REPLIES,
        status=429,
        headers={"Retry-After": "90"},
        payload={"ok": False, "error": "ratelimited"},
        repeat=True,
    )

    await _run_recovery(
        runtime,
        [intent],
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    remaining = await _remaining_intents(db_session_factory)
    assert [row.id for row in remaining] == [intent.id]
    assert clock.seconds >= 180.0


async def test_accepted_post_with_lost_response_is_reconciled_after_restart(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = await _create_intent(db_session_factory)
    clock = _Clock(intent.created_at + dt.timedelta(seconds=1))
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    accepted: list[dict[str, object]] = []

    async def accept_then_drop_response(_url: Any, **kwargs: Any) -> Any:
        accepted.append(kwargs["json"])
        raise TimeoutError("Slack accepted the post, but its response was lost")

    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.post(
        "https://slack.com/api/chat.postMessage",
        callback=accept_then_drop_response,
    )
    lifecycle = SlackTurnLifecycle(
        client=fake_slack_web_client.client,
        channel=_CHANNEL_ID,
        thread_ts=_THREAD_ID,
        cancel=asyncio.Event(),
        author_id="U_TEST",
        agent_name="test-agent",
        model_id="claude-sonnet-4-6",
        register=lambda *_args: None,
        deregister=lambda _status_ts: None,
        intent_id=intent.id,
    )
    with pytest.raises(TimeoutError, match="response was lost"):
        await lifecycle.post_initial()

    assert len(accepted) == 1
    accepted_key = next(
        element["value"]
        for block in accepted[0]["blocks"]
        if block.get("type") == "actions"
        for element in block.get("elements", [])
    )
    assert accepted_key == intent.id.hex
    unresolved = await _remaining_intents(db_session_factory)
    assert len(unresolved) == 1 and unresolved[0].status == "prepared"

    message_ts = f"{intent.created_at.timestamp() + 0.1:.6f}"
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.get(
        _REPLIES,
        payload=_page([_message(message_ts, intent.id.hex)]),
    )

    async def check_marker_before_edit(_url: Any, **_kwargs: Any) -> CallbackResult:
        async with db_session_factory() as session:
            recovered = await list_recoverable_turn_card_intents(session, platform="slack")
        assert len(recovered) == 1
        assert recovered[0].status == "posted"
        assert recovered[0].message_id == message_ts
        return CallbackResult(payload={"ok": True, "ts": message_ts, "channel": _CHANNEL_ID})

    fake_slack_web_client.mock.post(_UPDATE_URL, callback=check_marker_before_edit)
    await _run_recovery(
        runtime,
        unresolved,
        fake_slack_web_client,
        monkeypatch,
        clock,
    )

    assert await _remaining_intents(db_session_factory) == []
    updated_messages = [
        request.kwargs["json"]["ts"]
        for (method, url), requests in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.update"
        for request in requests
    ]
    assert updated_messages == [message_ts]


async def test_recovery_is_backgrounded_after_intents_are_snapshotted(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.slack.app import SlackApp

    intent = await _create_intent(db_session_factory)
    runtime = build_slack_runtime(Fernet.generate_key().decode(), db_session_factory)
    card_work_started = asyncio.Event()
    hold_card_work = asyncio.Event()

    async def retire_orphans(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def recover_cards(*_args: Any, **_kwargs: Any) -> None:
        card_work_started.set()
        await hold_card_work.wait()

    async def snapshot(*_args: Any) -> list[TurnCardIntentRow]:
        return [intent]

    monkeypatch.setattr("daimon.adapters.slack.app.retire_orphaned_turns", retire_orphans)
    monkeypatch.setattr(
        "daimon.adapters.slack.app.snapshot_slack_card_intents",
        snapshot,
    )
    monkeypatch.setattr("daimon.adapters.slack.app.recover_slack_card_intents", recover_cards)
    app = SlackApp(runtime=runtime)
    await app.start_orphan_recovery()
    await app._wait_for_orphan_recovery()  # pyright: ignore[reportPrivateUsage]
    await card_work_started.wait()

    assert app._card_recovery_task is not None  # pyright: ignore[reportPrivateUsage]
    assert not app._card_recovery_task.done()  # pyright: ignore[reportPrivateUsage]
    app._card_recovery_task.cancel()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(asyncio.CancelledError):
        await app._card_recovery_task
