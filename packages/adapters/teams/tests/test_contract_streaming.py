"""Turn delivery against Teams' documented streaming responses.

Every test here drives a real SDK ingress POST, the real ``HttpStream`` and
the real ``ActivityContext.send``; only the Bot Framework transport is faked,
by ``TeamsContractFake``, which answers the way Teams documents (201 then
202 ``{}``, 403 on Stop / 2-minute limit / size). ``run_turn`` is scripted so
each test can place a Stop or a timeout at an exact point in the turn.

Tests marked ``xfail(strict=True)`` pin a known open gap (REVIEW-13 N-ids).
They must start passing, and then lose the marker, when the gap is fixed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from daimon.adapters.teams.app import DirectCoreTurnDispatcher
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.adapters.teams.identity import VerifiedTeamsTurnResolver
from daimon.adapters.teams.turn_lifecycle import SDK_PLACEHOLDER_MESSAGE_ID
from daimon.core.config import TeamsSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.asgi import asgi_lifespan
from microsoft_teams.apps.http_stream import HttpStream  # pyright: ignore[reportMissingTypeStubs]
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    BOT_CLIENT_ID,
    ENTRA_TENANT_ID,
    build_teams_runtime,
    make_message_activity,
    patched_turn_pipeline,
)
from .contract_fake import TeamsContractFake, VisibleMessage, build_contract_client

ANSWER = "The answer from the agent."
ScriptedTurn = Callable[..., Awaitable[TurnState]]


@dataclasses.dataclass
class TurnResult:
    marked_ids: list[uuid.UUID]
    cancel_events: list[asyncio.Event]


def _settings() -> TeamsSettings:
    return TeamsSettings(
        client_id=BOT_CLIENT_ID,
        client_secret=SecretStr("test-secret"),
        tenant_id=ENTRA_TENANT_ID,
        port=3978,
    )


async def _run_through_ingress(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsContractFake,
    scripted_turn: ScriptedTurn,
    *,
    activities: list[dict[str, object]] | None = None,
    drain_timeout: float = 30,
) -> TurnResult:
    await provision_tenant(db_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    runtime = build_teams_runtime(db_factory)
    dispatcher = DirectCoreTurnDispatcher(
        settings=_settings(), turn_deps=runtime.turn_deps, sessionmaker=db_factory
    )
    runtime = dataclasses.replace(
        runtime,
        resolver=VerifiedTeamsTurnResolver(
            sessionmaker=db_factory, entra_tenant_id=ENTRA_TENANT_ID
        ),
        dispatcher=dispatcher,
    )
    service = create_teams_http_service(
        settings=_settings(), runtime=runtime, client=build_contract_client(fake)
    )
    result = TurnResult(marked_ids=[], cancel_events=[])

    async def _run_turn(*, cancel: asyncio.Event, **kwargs: Any) -> TurnState:
        result.cancel_events.append(cancel)
        return await scripted_turn(cancel=cancel, **kwargs)

    with (
        patched_turn_pipeline(result.marked_ids),
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_run_turn.side_effect = _run_turn
        async with asgi_lifespan(service.app):
            assert service.boot_sweep_task is not None
            await service.boot_sweep_task
            transport = httpx.ASGITransport(app=service.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for payload in activities or [make_message_activity()]:
                    response = await client.post("/api/messages", json=payload)
                    assert response.status_code in (200, 201, 202), response.text
            await dispatcher.drain(timeout=drain_timeout)
    return result


def _answer_state(text: str = ANSWER) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


async def _answer(*, lifecycle: Any, **_: Any) -> TurnState:
    state = _answer_state()
    await lifecycle.on_terminal_success(state)
    return state


async def _render_safely(lifecycle: Any, state: TurnState) -> None:
    """One driver render tick. The SDK raises StreamCancelledError (a
    CancelledError) from ``update`` on a stopped stream; the real driver's
    render loop would die on it, so a scripted turn swallows it and goes on."""
    with contextlib.suppress(asyncio.CancelledError):
        await lifecycle.on_render(state)


async def _watermark(
    db_factory: async_sessionmaker[AsyncSession], mapping_id: uuid.UUID
) -> str | None:
    async with db_factory() as session:
        row = await get_thread_session_by_id(session, id=mapping_id)
        assert row is not None
        return row.watermark_message_id


# ---- documented happy path ---------------------------------------------------


@pytest.mark.asyncio
async def test_answer_lands_once_on_the_streamed_message_under_202_empty_bodies(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """Guard. Teams answers every send after the first with ``202 {}``, so
    the SDK hands back its placeholder id. The adapter must still (a) treat
    the final as delivered (no duplicate fallback post), and (b) persist the
    REAL stream id as marker and watermark, never the placeholder."""
    fake = TeamsContractFake()
    result = await _run_through_ingress(db_session_factory, fake, _answer)

    bubbles = fake.bubbles_containing(ANSWER)
    assert len(bubbles) == 1, [b.id for b in bubbles]
    (bubble,) = bubbles
    assert bubble.kind == "stream" and bubble.state == "final"
    assert not [m for m in fake.messages.values() if m.kind == "plain"], "no fallback post"
    assert [e.status for e in fake.exchanges if e.method == "POST"][0] == 201
    assert all(e.status == 202 for e in fake.exchanges[1:] if e.method == "POST")

    assert result.marked_ids, "the turn must have written its orphan marker"
    watermark = await _watermark(db_session_factory, result.marked_ids[0])
    assert watermark == bubble.id
    assert watermark != SDK_PLACEHOLDER_MESSAGE_ID


# ---- F2: final send refused ----------------------------------------------------


@pytest.mark.asyncio
async def test_final_send_outage_still_delivers_the_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """F2. The final streaming send fails after the SDK's retries (5xx). The
    user must still get the answer, as a new message.
    Fails on 5bdc726 (the answer is swallowed); passes with F2."""
    fake = TeamsContractFake(fail_final_status=502)
    await _run_through_ingress(db_session_factory, fake, _answer)

    bubbles = fake.bubbles_containing(ANSWER)
    assert len(bubbles) == 1, "the answer must reach the user exactly once"
    assert bubbles[0].kind == "plain"


# ---- F4 / N1: Stop ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_forwarded_to_the_turn_cancel_event(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """F4. The user presses Stop; Teams refuses the next streaming send with
    403 "canceled by user"; the next render tick must set the turn's cancel.
    Fails on 5bdc726 (cancel never set); passes with F4."""
    fake = TeamsContractFake()
    observed: list[bool] = []

    async def _turn(*, lifecycle: Any, cancel: asyncio.Event, **_: Any) -> TurnState:
        state = TurnState(content=[])
        fake.press_stop()
        await _render_safely(lifecycle, state)  # this send gets the 403
        await asyncio.wait_for(fake.stop_refused.wait(), timeout=5)
        await asyncio.sleep(0.05)  # let the SDK's flush task record it
        await _render_safely(lifecycle, state)  # the driver's next tick
        try:
            await asyncio.wait_for(cancel.wait(), timeout=1)
            observed.append(True)
        except TimeoutError:
            observed.append(False)
        return state

    await _run_through_ingress(db_session_factory, fake, _turn)

    assert observed == [True], "Stop must reach the cancel event the driver races"
    assert not fake.bubbles_containing(ANSWER)
    assert not [m for m in fake.messages.values() if m.kind == "plain"], (
        "a stopped turn must not re-post anything as a new message"
    )


@pytest.mark.xfail(strict=True, reason="N1: Stop is only seen on a send; no send while quiet")
@pytest.mark.asyncio
async def test_stop_during_a_quiet_tool_call_is_noticed(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """N1. Stop lands while a long tool call emits nothing, so the driver
    skips on_render. Cancel must still be set within a few seconds. Fails on
    5bdc726 and on teams-parity/base (F4 only checks inside on_render)."""
    fake = TeamsContractFake()
    observed: list[bool] = []

    async def _turn(*, lifecycle: Any, cancel: asyncio.Event, **_: Any) -> TurnState:
        await _render_safely(lifecycle, TurnState(content=[]))
        fake.press_stop()
        try:
            await asyncio.wait_for(cancel.wait(), timeout=3)  # no renders meanwhile
            observed.append(True)
        except TimeoutError:
            observed.append(False)
        return TurnState(content=[])

    await _run_through_ingress(db_session_factory, fake, _turn)
    assert observed == [True]


# ---- F3 / N4: the 2-minute limit ---------------------------------------------------


def _expire_after_first_stream_request(fake: TeamsContractFake, message: VisibleMessage) -> None:
    # message.stream_requests counts requests already answered for this stream.
    if message.stream_requests >= 1:
        fake.expire_streams()


@pytest.mark.asyncio
async def test_answer_after_the_two_minute_limit_lands_once_via_put(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """The stream times out (403 "exceeded streaming time"); the SDK
    finalizes the same bubble with a non-streaming PUT that returns the Bot
    Framework ResourceResponse ``{"id"}``. The answer lands once, in place."""
    fake = TeamsContractFake(on_stream_request=_expire_after_first_stream_request)
    await _run_through_ingress(db_session_factory, fake, _answer)

    bubbles = fake.bubbles_containing(ANSWER)
    assert len(bubbles) == 1
    assert bubbles[0].kind == "stream"
    assert any(e.method == "PUT" for e in fake.exchanges)


@pytest.mark.xfail(
    strict=True,
    reason="F3 (GUESS: PUT returns 200 {}): SDK KeyError -> F2 fallback re-posts -> duplicate",
)
@pytest.mark.asyncio
async def test_answer_after_the_two_minute_limit_with_id_less_put_is_not_duplicated(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """F3, conditional on an unobserved shape. If the PUT succeeds but its
    body carries no id, the SDK's ``response.json()["id"]`` raises after the
    edit already landed. On 5bdc726 the user sees the answer once (by luck:
    the error is swallowed). With F2 the fallback re-posts, so the user sees
    it twice. Settle the PUT body live before choosing a fix."""
    fake = TeamsContractFake(put_body="empty", on_stream_request=_expire_after_first_stream_request)
    await _run_through_ingress(db_session_factory, fake, _answer)
    assert len(fake.bubbles_containing(ANSWER)) == 1


@pytest.mark.xfail(strict=True, reason="N4: progress is dropped after the 2-minute limit")
@pytest.mark.asyncio
async def test_progress_keeps_updating_after_the_two_minute_limit(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """N4. After the stream times out, later progress ("3 tool calls so far")
    must still reach the user somehow; the SDK silently drops it."""
    fake = TeamsContractFake()

    async def _turn(*, lifecycle: Any, **_: Any) -> TurnState:
        fake.expire_streams()
        await _render_safely(lifecycle, TurnState(content=[]))
        await asyncio.sleep(0.2)
        before = len(fake.exchanges)
        for _i in range(3):
            await _render_safely(lifecycle, TurnState(content=[]))
            await asyncio.sleep(0.6)
        progressed.append(len(fake.exchanges) > before)
        return TurnState(content=[])

    progressed: list[bool] = []
    await _run_through_ingress(db_session_factory, fake, _turn)
    assert progressed == [True]


# ---- size ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_refused_as_too_large_on_the_stream_falls_back_to_a_new_message(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """F2 via the documented 403 "Message size too large" on the final
    streaming send, with a plain message still accepted (GUESS: the plain
    limit is at least the streaming one). Fails on 5bdc726; passes with F2."""
    long_answer = "x" * 6000

    async def _long(*, lifecycle: Any, **_: Any) -> TurnState:
        state = _answer_state(ANSWER + long_answer)
        await lifecycle.on_terminal_success(state)
        return state

    fake = TeamsContractFake(stream_size_limit=5000)
    await _run_through_ingress(db_session_factory, fake, _long)

    bubbles = fake.bubbles_containing(ANSWER)
    assert len(bubbles) == 1
    assert bubbles[0].kind == "plain"


@pytest.mark.xfail(strict=True, reason="N7 (new): an answer too large to send leaves no notice")
@pytest.mark.asyncio
async def test_answer_too_large_for_any_send_still_tells_the_user(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """Both the streamed final (403 size) and the fallback plain post
    (``PLAIN_TOO_LARGE_STATUS``, GUESS) are refused. The user must get some
    short notice rather than a frozen progress bubble."""
    long_answer = "x" * 6000

    async def _long(*, lifecycle: Any, **_: Any) -> TurnState:
        state = _answer_state(ANSWER + long_answer)
        await lifecycle.on_terminal_success(state)
        return state

    fake = TeamsContractFake(stream_size_limit=5000, plain_size_limit=5000)
    await _run_through_ingress(db_session_factory, fake, _long)

    finals = [m for m in fake.messages.values() if m.state == "final"]
    assert finals, "the user must see a terminal message of some kind"


# ---- N3: one stream per chat -------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="N3: a follow-up during a turn opens a second stream")
@pytest.mark.asyncio
async def test_follow_up_during_a_running_turn_gets_a_reply(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """N3. Teams supports one concurrent stream per chat (DOC); what it
    returns to a second one is a GUESS (403 "not allowed"). A follow-up sent
    while the first turn runs must still get a visible reply.

    Two turns at once cannot share the test's single DB connection, so the
    core calls ``app`` makes (admit, bind_session, run_prepared_turn) are
    replaced with DB-free stand-ins here; the resolver, the dispatcher, the
    SDK stream and ``TeamsTurnLifecycle`` stay real.

    On teams-parity/base the follow-up is not lost: its stream never gets an
    id, ``post_initial`` gives up after FIRST_CHUNK_TIMEOUT_S (15 s), close()
    gives up after the SDK's 30 s wait, and F2's fallback posts the answer as
    a new message, about 45 s late. On 5bdc726 it is lost. Both waits are
    shortened here (to 1 s each) and the test asks for a reply well inside
    them, which is what a per-conversation guard would give."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    fake = TeamsContractFake(second_stream="reject")
    runtime = build_teams_runtime(db_session_factory)
    dispatcher = DirectCoreTurnDispatcher(
        settings=_settings(), turn_deps=runtime.turn_deps, sessionmaker=db_session_factory
    )
    runtime = dataclasses.replace(
        runtime,
        resolver=VerifiedTeamsTurnResolver(
            sessionmaker=db_session_factory, entra_tenant_id=ENTRA_TENANT_ID
        ),
        dispatcher=dispatcher,
    )
    service = create_teams_http_service(
        settings=_settings(), runtime=runtime, client=build_contract_client(fake)
    )
    first_running = asyncio.Event()
    second_done = asyncio.Event()
    real_stream_init = HttpStream.__init__

    def _short_wait_stream(self: HttpStream, *args: Any, **kwargs: Any) -> None:
        real_stream_init(self, *args, **kwargs)
        self._total_wait_timeout = 1.0  # pyright: ignore[reportPrivateUsage]

    async def _run_prepared(*_: Any, lifecycle: Any, user_message: str, **__: Any) -> Any:
        if user_message == "first":
            first_running.set()
            # The first turn outlives the follow-up, as a real minutes-long turn does.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(second_done.wait(), timeout=10)
            await lifecycle.on_terminal_success(_answer_state("first answer"))
        else:
            await lifecycle.on_terminal_success(_answer_state("second answer"))
            second_done.set()
        return SimpleNamespace(mapping_id=None)

    with (
        patch("daimon.adapters.teams.app.admit", new_callable=AsyncMock),
        patch(
            "daimon.adapters.teams.app.bind_session",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(mapping_id=None),
        ),
        patch("daimon.adapters.teams.app.run_prepared_turn", side_effect=_run_prepared),
        patch("daimon.adapters.teams.turn_lifecycle.FIRST_CHUNK_TIMEOUT_S", 1.0),
        patch.object(HttpStream, "__init__", _short_wait_stream),
    ):
        async with asgi_lifespan(service.app):
            assert service.boot_sweep_task is not None
            await service.boot_sweep_task
            transport = httpx.ASGITransport(app=service.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post(
                    "/api/messages", json=make_message_activity(activity_id="1", text="first")
                )
                await asyncio.wait_for(first_running.wait(), timeout=10)
                second_sent_at = time.monotonic()
                await client.post(
                    "/api/messages", json=make_message_activity(activity_id="2", text="second")
                )
            await dispatcher.drain(timeout=30)

    assert fake.bubbles_containing("first answer"), "the first turn must still answer"
    delivered = fake.first_delivery_of("second answer")
    assert delivered is not None, "the follow-up must not vanish on a refused second stream"
    assert delivered.at - second_sent_at < 0.5, (
        f"the follow-up waited {delivered.at - second_sent_at:.1f}s on stream timeouts"
    )
