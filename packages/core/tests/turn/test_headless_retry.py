"""Headless mux sends use the mutation boundary with per-run memory state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.core import headless_runner
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.errors import TurnError
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.privacy import TurnPrivacy
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.state.lease import Slot
from mux.testing.drivers.anthropic.ma import MARouter, list_response, send_events_response
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import make_agent_message, make_status_idle

TENANT = uuid.UUID(int=701)
ACCOUNT = uuid.UUID(int=702)
SESSION_ID = "session-headless-retry"
EVENTS_PATH = f"/v1/sessions/{SESSION_ID}/events"


def transport() -> ScriptedTransport:
    agent = ma_agent(id="agent-headless-retry", tenant_id=TENANT)
    environment = ma_environment(id="environment-headless-retry", tenant_id=TENANT)
    session = ma_session(id=SESSION_ID, agent_id=agent.id, environment_id=environment.id)
    router = MARouter()
    router.add_agent(agent)
    router.add_environment(environment)
    router.add_session(session)
    router.add("GET", EVENTS_PATH, lambda request, match: list_response([]))
    router.add(
        "GET",
        f"{EVENTS_PATH}/stream",
        lambda request, match: httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "gone"}}
        ),
    )
    router.add(
        "POST",
        r"/v1/sessions$",
        lambda request, match: httpx.Response(200, json=session.model_dump(mode="json")),
    )
    return ScriptedTransport(router=router)


async def run_headless(
    transport: ScriptedTransport,
    *,
    sessions: async_sessionmaker | None,
    max_retries: int,
    deadline: datetime | None = None,
) -> str:
    async with transport.client(max_retries=max_retries) as client:
        return await headless_runner.run_turn(
            privacy=TurnPrivacy("test"),
            tool_safety=OPEN_TOOL_SAFETY,
            anthropic=client,
            agent_id="agent-headless-retry",
            environment_id="environment-headless-retry",
            trigger_message="question",
            tenant_id=TENANT,
            account_id=ACCOUNT,
            session_factory=sessions,
            turn_path="mux",
            deadline=deadline,
        )


async def seeded_sessions(db_engine: AsyncEngine) -> async_sessionmaker:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as db, db.begin():
        await make_tenant(db, id=TENANT, workspace_id="headless-retry")
    return sessions


def headless_slot() -> Slot:
    return Slot(
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(TENANT), platform="headless", channel_id=SESSION_ID),
            thread_id=SESSION_ID,
        ),
        account_id=str(ACCOUNT),
    )


@pytest.mark.parametrize("lost_response", [False, True])
async def test_headless_send_retries_only_refusals_without_a_durable_transcript(
    db_engine: AsyncEngine, db_clean: None, lost_response: bool
) -> None:
    sessions = await seeded_sessions(db_engine)
    scripted = transport()
    scripted.queue(
        ScriptedReply.stream(
            f"/v1/sessions/{SESSION_ID}/events/stream",
            [
                make_agent_message(event_id="answer", text="done").model_dump(mode="json"),
                make_status_idle(event_id="idle").model_dump(mode="json"),
            ],
        ),
        ScriptedReply(
            "POST",
            EVENTS_PATH,
            httpx.ReadError("accepted, response lost")
            if lost_response
            else httpx.Response(
                429,
                headers={"retry-after-ms": "1"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            ),
        ),
    )
    if not lost_response:
        scripted.queue(
            ScriptedReply(
                "POST",
                EVENTS_PATH,
                httpx.Response(
                    429,
                    headers={"retry-after-ms": "1"},
                    json={
                        "type": "error",
                        "error": {"type": "rate_limit_error", "message": "slow"},
                    },
                ),
            )
        )
        scripted.queue(ScriptedReply("POST", EVENTS_PATH, send_events_response()))
        assert await run_headless(scripted, sessions=sessions, max_retries=2) == "done"
    else:
        with pytest.raises(TurnError):
            await run_headless(scripted, sessions=sessions, max_retries=MA_MAX_RETRIES)
    scripted.assert_consumed()
    posts = [
        request
        for request in scripted.requests
        if request.method == "POST" and request.path == EVENTS_PATH
    ]
    assert len(posts) == (1 if lost_response else 3), "one ambiguous send or two safe retries"
    store = PostgresStateStore(sessions)
    binding = await store.get_binding(headless_slot())
    assert binding is None, "headless transcript should not gain a durable copy"
    async with sessions() as db:
        counts = [
            await db.scalar(text(f"SELECT count(*) FROM {table}"))
            for table in ("provider_binding_slot", "journal_session", "operation")
        ]
    assert counts == [0, 0, 0], "headless claims and events should stay in memory"


async def test_headless_mux_without_database_still_runs() -> None:
    scripted = transport()
    scripted.queue(
        ScriptedReply.stream(
            f"/v1/sessions/{SESSION_ID}/events/stream",
            [
                make_agent_message(event_id="answer", text="done").model_dump(mode="json"),
                make_status_idle(event_id="idle").model_dump(mode="json"),
            ],
        ),
        ScriptedReply("POST", EVENTS_PATH, send_events_response()),
    )
    assert await run_headless(scripted, sessions=None, max_retries=MA_MAX_RETRIES) == "done"
    scripted.assert_consumed()


async def test_headless_retry_stops_when_retry_after_exceeds_turn_deadline() -> None:
    scripted = transport()
    scripted.queue(
        ScriptedReply.stream(
            f"/v1/sessions/{SESSION_ID}/events/stream",
            [
                make_agent_message(event_id="answer", text="done").model_dump(mode="json"),
                make_status_idle(event_id="idle").model_dump(mode="json"),
            ],
        ),
        ScriptedReply(
            "POST",
            EVENTS_PATH,
            httpx.Response(
                429,
                headers={"retry-after": "60"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            ),
        ),
    )
    with pytest.raises(TurnError):
        await run_headless(
            scripted,
            sessions=None,
            max_retries=MA_MAX_RETRIES,
            deadline=datetime.now(UTC) + timedelta(seconds=3),
        )
    scripted.assert_consumed()
    posts = [
        request
        for request in scripted.requests
        if request.method == "POST" and request.path == EVENTS_PATH
    ]
    assert len(posts) == 1, "retry must not outlive the caller's turn deadline"
