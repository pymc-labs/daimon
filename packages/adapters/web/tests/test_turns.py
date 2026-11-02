"""Real web turns over a scripted Managed Agents stream, down to the saved reply."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsAgentToolResultEvent,
    BetaManagedAgentsAgentToolUseEvent,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.web.stream import Event
from daimon.adapters.web.turns import WebTurns
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.errors import TurnError
from daimon.core.stores import accounts, spend_allowances, tenants, turn_outcomes, usage_events
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import WebThreadRow
from daimon.core.stores.shared_channels import is_channel_shared
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.core.web_identity import place_of, principal_of
from daimon.testing.factories import make_platform_principal
from daimon.testing.turn_router import AGENT_ID, AGENT_TEXT, build_turn_router
from mux.testing.drivers.anthropic.ma import MARouter, sse_response
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME, ma_model_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import (
    STREAM,
    Workspace,
    build_turns,
    funded_workspace,
    new_thread,
    one_session,
    visitor_of,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _run(
    turns: WebTurns, factory: async_sessionmaker[AsyncSession], ws: Workspace, text: str = "hi"
) -> tuple[WebThreadRow, list[Event]]:
    thread, message = await new_thread(factory, ws, text)
    async with factory() as session:
        principal = await principal_of(
            session, ws.person, "visitor" if ws.person.kind == "visitor" else "member"
        )
    stream = turns.start(person=ws.person, principal=principal, thread=thread, message=message)
    events = [event async for _, event in stream.segment(0)]
    await turns.drain(10)
    return thread, events


def _tool_script() -> list[dict[str, Any]]:
    def model(cls: Any, **fields: Any) -> dict[str, Any]:
        return cls(processed_at=NOW, **fields).model_dump(mode="json")

    return [
        model(
            BetaManagedAgentsAgentToolUseEvent,
            id="evt_tool",
            type="agent.tool_use",
            name="web_search",
            input={"query": "q3 spend"},
        ),
        model(
            BetaManagedAgentsAgentToolResultEvent,
            id="evt_result",
            type="agent.tool_result",
            tool_use_id="evt_tool",
            content=[BetaManagedAgentsTextBlock(type="text", text="found")],
        ),
        model(
            BetaManagedAgentsAgentMessageEvent,
            id="evt_msg",
            type="agent.message",
            content=[BetaManagedAgentsTextBlock(type="text", text="Spend rose.")],
        ),
        model(
            BetaManagedAgentsSessionStatusIdleEvent,
            id="evt_idle",
            type="session.status_idle",
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]


async def test_a_turn_streams_its_answer_and_saves_it(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with one_session():
        thread, events = await _run(turns, db_session_factory, ws)

    done = events[-1]
    assert done["type"] == "done" and done["message"]["content"] == AGENT_TEXT, events
    assert "".join(e["text"] for e in events if e["type"] == "delta") == AGENT_TEXT
    async with db_session_factory() as session:
        [_, reply] = await chat_store.list_messages(session, thread_id=thread.id)
    assert (reply.role, reply.status, reply.content) == ("assistant", "done", AGENT_TEXT)
    assert str(reply.id) == done["message"]["id"], "the saved reply is the one streamed"
    assert turns.streams.running(thread.id) is None, "the stream closes with the turn"


async def test_a_tool_call_streams_as_a_step(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    router = MARouter()
    router.add("GET", STREAM, lambda _r, _m: sse_response(_tool_script()))
    build_turn_router(str(ws.tenant_id), router=router)
    turns = build_turns(db_session_factory, router, tmp_path)

    with one_session():
        _, events = await _run(turns, db_session_factory, ws)

    steps = [e["step"] for e in events if e["type"] == "step"]
    assert steps and steps[-1]["id"] == "evt_tool" and steps[-1]["status"] == "done", events
    message = events[-1]["message"]
    assert message["content"] == "Spend rose." and message["steps"][0]["status"] == "done"


async def test_a_channel_bound_agent_is_refused_with_a_card(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=ws.tenant_id,
            policy=TenantAccessPolicy(agent_channel_pins={DEFAULT_AGENT_NAME: ("C1",)}),
        )
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    _, events = await _run(turns, db_session_factory, ws)

    card = events[-1]["message"]["card"]
    assert card["kind"] == "refusal" and "own channels" in card["title"], events


async def test_stop_saves_the_reply_as_stopped(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    started = asyncio.Event()

    async def run_until_stopped(*_args: object, **kw: Any) -> SimpleNamespace:
        await kw["lifecycle"].on_render(TurnState(content=[TextBlock(kind="text", text="Half")]))
        started.set()
        await kw["cancel"].wait()
        return SimpleNamespace(state=TurnState())

    with one_session(), patch("daimon.adapters.web.turns.run_prepared_turn", run_until_stopped):
        thread, message = await new_thread(db_session_factory, ws)
        async with db_session_factory() as session:
            principal = await principal_of(session, ws.person, "member")
        stream = turns.start(person=ws.person, principal=principal, thread=thread, message=message)
        await started.wait()
        assert await turns.stop(thread.id, "x"), "a running turn can be stopped"
        events = [event async for _, event in stream.segment(0)]
        await turns.drain(10)

    message_json = events[-1]["message"]
    assert message_json["stopped"] is True and message_json["content"] == "Half", events
    assert await turns.stop(thread.id, "x") is False, "nothing left to stop"


async def _recorded(factory: async_sessionmaker[AsyncSession], ws: Workspace) -> list[Any]:
    async with factory() as session:
        return await usage_events.list_for_tenant(session, tenant_id=ws.tenant_id)


async def _exhaust(
    factory: async_sessionmaker[AsyncSession], ws: Workspace, subject_id: str, *, spent: bool = True
) -> None:
    """A $1 allowance for `subject_id`, spent when asked (a $5 call at the 2x markup)."""
    async with factory.begin() as session:
        await spend_allowances.create_allowance(
            session,
            tenant_id=ws.tenant_id,
            subject_kind="person",
            subject_id=subject_id,
            agent_id=None,
            amount_usd=Decimal(1),
            window="month",
            starts_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        if spent:
            await usage_events.record(
                session,
                tenant_id=ws.tenant_id,
                platform_user_id=subject_id,
                managed_session_id="earlier",
                model="claude-opus-4-7",
                model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
                event_id="evt-earlier",
                agent_id=AGENT_ID,
            )


async def test_an_exhausted_allowance_refuses_the_turn_before_a_session_starts(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    await _exhaust(db_session_factory, ws, str(ws.person.id))
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with one_session():
        _, events = await _run(turns, db_session_factory, ws)

    card = events[-1]["message"]["card"]
    assert card["kind"] == "refusal", events
    assert card["title"] == "Your credit has run out.", card
    assert card["help"] == "Ask an admin to add credit.", card
    assert [e.event_id for e in await _recorded(db_session_factory, ws)] == ["evt-earlier"], (
        "no model call was made or billed"
    )


async def test_an_allowance_on_another_person_does_not_refuse(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    await _exhaust(db_session_factory, ws, str(uuid.uuid4()))
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with one_session():
        _, events = await _run(turns, db_session_factory, ws)

    assert events[-1]["message"]["content"] == AGENT_TEXT, events


async def test_a_web_turn_is_recorded_under_the_person_and_agent(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with one_session():
        await _run(turns, db_session_factory, ws)

    [row] = await _recorded(db_session_factory, ws)
    assert row.platform_user_id == str(ws.person.id), "a web-only person is their own id"
    assert row.agent_id == AGENT_ID


async def test_a_linked_member_is_recorded_and_gated_as_their_chat_account(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    async with db_session_factory.begin() as session:
        await make_platform_principal(
            session,
            platform="discord",
            external_id="d-ada",
            tenant=await tenants.get_tenant(session, ws.tenant_id),
            account=await accounts.get_account(session, ws.person.account_id),
        )
    await _exhaust(db_session_factory, ws, "d-ada", spent=False)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with one_session():
        _, first = await _run(turns, db_session_factory, ws)
    [row] = await _recorded(db_session_factory, ws)
    assert row.platform_user_id == "d-ada", "usage follows the linked chat id"
    assert first[-1]["message"]["content"] == AGENT_TEXT, first

    async with db_session_factory.begin() as session:
        await usage_events.record(
            session,
            tenant_id=ws.tenant_id,
            platform_user_id="d-ada",
            managed_session_id="more",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
            event_id="evt-more",
            agent_id=AGENT_ID,
        )
    with one_session():
        _, second = await _run(turns, db_session_factory, ws, "again")
    assert second[-1]["message"]["card"]["title"] == "Your credit has run out.", second


async def test_a_visitor_chats_in_a_place_no_one_else_shares(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    first = await visitor_of(db_session_factory, ws)
    second = await visitor_of(db_session_factory, ws)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with one_session():
        await _run(turns, db_session_factory, Workspace(ws.tenant_id, first))
        _, member = await _run(turns, db_session_factory, ws)

    async def shared(place: str) -> bool:
        async with db_session_factory() as db:
            return await is_channel_shared(
                db, tenant_id=ws.tenant_id, platform="web", channel_id=place
            )

    assert await shared(place_of(first.id)), "a visitor's turn marks their own place shared"
    assert not await shared(place_of(second.id)), "another visitor's place stays unshared"
    assert not await shared("web"), "there is no one place for every web chat"
    assert not await shared(place_of(ws.person.id)), "a member's place is not shared"
    assert member[-1]["message"]["content"] == AGENT_TEXT, "and a member's turn is not held"


async def test_a_visitor_with_a_linked_chat_account_still_runs_as_their_web_id(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    visitor = await visitor_of(db_session_factory, ws)
    async with db_session_factory.begin() as session:
        await make_platform_principal(
            session,
            platform="discord",
            external_id="d-guest",
            tenant=await tenants.get_tenant(session, ws.tenant_id),
            account=await accounts.get_account(session, visitor.account_id),
        )
    async with db_session_factory() as session:
        principal = await principal_of(session, visitor, "visitor")

    assert (principal.platform, principal.external_id) == ("web", str(visitor.id)), (
        "a guest never takes a chat identity"
    )


async def _reasons(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[TerminationReason]:
    """The reasons recorded for the workspace's turns, once the background write lands."""
    for _ in range(50):
        async with factory() as session:
            rows = await turn_outcomes.list_for_tenant(session, tenant_id)
        if rows:
            return [r.reason for r in rows]
        await asyncio.sleep(0.1)
    return []


async def test_a_turn_that_ends_in_an_error_is_recorded_as_failed(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    async def errors(*_args: object, **_kw: Any) -> SimpleNamespace:
        return SimpleNamespace(
            state=TurnState(error=TurnError(kind="upstream", message="boom")), ma_session_id="s"
        )

    with one_session(), patch("daimon.adapters.web.turns.run_prepared_turn", errors):
        _, events = await _run(turns, db_session_factory, ws)

    assert events[-1]["message"]["card"]["title"], "the person sees a card saying it failed"
    assert await _reasons(db_session_factory, ws.tenant_id) == [TerminationReason.UPSTREAM], (
        "the turn is recorded as an upstream failure, not a quiet success"
    )


async def test_a_turn_shed_by_a_full_queue_is_recorded(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with patch.object(turns.queue, "admit", return_value=None):
        _, events = await _run(turns, db_session_factory, ws)

    assert events[-1]["message"]["card"]["kind"] == "refusal", "the person is told it is busy"
    assert await _reasons(db_session_factory, ws.tenant_id) == [
        TerminationReason.ADMISSION_CONCURRENCY_SHED
    ], "the shed turn leaves an outcome row, as on the other platforms"
