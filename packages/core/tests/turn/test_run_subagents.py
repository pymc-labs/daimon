"""`run_prepared_turn` for an agent with helpers: a spending limit per request.

Before the message is sent, the session's native budget becomes what it has spent
plus one request's worth; a turn the limit stopped retires its session, so raising
the limit later can never resume the paused helpers.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.config import SubagentsSettings
from daimon.core.stores import session_settle_state
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.subagents import subagents_override
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    list_response,
    send_events_response,
    sse_response,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_run_prepared_turn import (
    _admission,
    _deps,
    _prepared_turn,
    _recovery_lifecycle,
    _reseed,
    _router,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ma_subagents"


def _coordinator(tenant_id: Any) -> BetaManagedAgentsAgent:
    plain = ma_agent(id="ag_coord", tenant_id=tenant_id)
    return BetaManagedAgentsAgent.model_validate(
        {
            **plain.model_dump(mode="json"),
            "multiagent": {
                "type": "coordinator",
                "agents": [{"type": "agent", "id": "ag_coord", "version": 1}],
            },
        }
    )


def _with_budgeted_session(
    router: MARouter,
    journal: list[str],
    updates: list[dict[str, Any]],
    *,
    stream: list[Any] | None,
    spent: str = "412",
    running_helper: bool = False,
) -> None:
    session = {
        "id": "sess_coord",
        "type": "session",
        "agent": {"id": "ag_coord", "model": {"id": "claude-sonnet-4-6"}, "type": "agent"},
        "budget": {"type": "limit", "max_list_cost": {"amount": "1000", "currency": "USD"}},
        "usage": {"list_cost": {"amount": spent, "currency": "USD"}},
        "status": "idle",
        "metadata": {},
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T00:00:00Z",
        "environment_id": "env_1",
        "resources": [],
        "stats": {},
        "vault_ids": [],
        "outcome_evaluations": [],
    }

    def retrieve(_request: httpx.Request, _match: object) -> httpx.Response:
        return httpx.Response(200, json=session)

    def update(request: httpx.Request, _match: object) -> httpx.Response:
        journal.append("budget")
        updates.append(json.loads(request.content))
        return httpx.Response(200, json=session)

    def send(request: httpx.Request, _match: object) -> httpx.Response:
        events = json.loads(request.content)["events"]
        journal.append("interrupt" if events[0]["type"] == "user.interrupt" else "send")
        return send_events_response()

    def threads(_request: httpx.Request, _match: object) -> httpx.Response:
        helper = {
            "id": "sthr_helper",
            "parent_thread_id": "sthr_primary",
            "archived_at": None,
            "status": "running",
            "agent": {"model": {"id": "claude-sonnet-4-6"}},
        }
        return list_response([helper] if running_helper else [])

    def thread_events(_request: httpx.Request, _match: object) -> httpx.Response:
        return list_response([])

    def events(_request: httpx.Request, _match: object) -> httpx.Response:
        message = {
            "id": "sevt_user_1",
            "type": "user.message",
            "content": [{"type": "text", "text": "do both in parallel"}],
            "processed_at": "2026-07-28T00:00:01Z",
        }
        return list_response([message])

    routes: list[tuple[str, str, Any]] = [
        ("GET", r"/v1/sessions/sess_coord", retrieve),
        ("POST", r"/v1/sessions/sess_coord", update),
        ("POST", r"/v1/sessions/sess_coord/events", send),
        ("GET", r"/v1/sessions/sess_coord/threads/[^/]+/events", thread_events),
        ("GET", r"/v1/sessions/sess_coord/threads", threads),
        ("GET", r"/v1/sessions/sess_coord/events", events),
    ]
    if stream is not None:
        routes.append(
            ("GET", r"/v1/sessions/sess_coord/events/stream", lambda _r, _m: sse_response(stream))
        )
    for method, path, handler in reversed(routes):
        router.routes.insert(0, (method, re.compile(path), cast(Any, handler)))


async def _run(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    stream: list[Any] | None,
    deadline: datetime | None = None,
    spent: str = "412",
    running_helper: bool = False,
) -> tuple[Any, list[str], list[dict[str, Any]], Any]:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-helpers",
        ma_session_id="sess_coord",
    )
    await db_session.commit()
    router = _router(session_bodies=[], dead_session_ids=set())
    journal: list[str] = []
    updates: list[dict[str, Any]] = []
    _with_budgeted_session(
        router, journal, updates, stream=stream, spent=spent, running_helper=running_helper
    )
    deps = _deps(sessionmaker=db_session_factory, router=router)
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(
            account_id=account.id,
            agent=_coordinator(tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_coord",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    with subagents_override(SubagentsSettings(enabled=True, turn_budget_usd=Decimal("5"))):
        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-helpers",
            external_user_id="user-1",
            user_message="do both in parallel",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
            deadline=deadline,
        )
    async with db_session_factory() as s:
        mapping = await get_thread_session_by_id(s, id=row.id)
    return outcome, journal, updates, mapping


async def test_the_limit_is_raised_to_one_more_request_before_the_message_is_sent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    outcome, journal, updates, mapping = await _run(db_session, db_session_factory, stream=None)

    assert outcome.state.termination is TerminationReason.COMPLETED
    assert journal[:2] == ["budget", "send"], "the limit is in place before any work starts"
    assert updates == [
        {"budget": {"type": "limit", "max_list_cost": {"amount": "912", "currency": "USD"}}}
    ]
    assert mapping is not None and mapping.status == "live"


async def test_a_request_stopped_at_its_limit_retires_its_session(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    stream = [
        json.loads(line) for line in (FIXTURES / "budget_reached.jsonl").read_text().splitlines()
    ]

    outcome, _journal, _updates, mapping = await _run(db_session, db_session_factory, stream=stream)

    assert outcome.state.termination is TerminationReason.BUDGET_REACHED
    assert mapping is not None and mapping.status == "dead", (
        "a raise on the next turn would resume the paused helpers"
    )


async def test_the_ceiling_stops_every_helper_before_the_session_is_retired(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    outcome, journal, _updates, mapping = await _run(
        db_session,
        db_session_factory,
        stream=None,
        deadline=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert outcome.state.termination is TerminationReason.CEILING
    assert "interrupt" in journal, "helpers would keep spending on an abandoned session"
    assert mapping is not None and mapping.status == "dead"


async def test_a_helper_still_working_after_the_turn_is_left_to_the_drain(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    outcome, _journal, _updates, _mapping = await _run(
        db_session, db_session_factory, stream=None, running_helper=True
    )

    assert outcome.state.termination is TerminationReason.COMPLETED
    async with db_session_factory() as s:
        request = await session_settle_state.settle_request(s, managed_session_id="sess_coord")
    assert request is not None and request.turn_event_id == "sevt_user_1", (
        "the scheduler's drain bills the helper once the session idles"
    )


async def test_a_session_already_at_its_limit_is_retired_not_resumed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    outcome, journal, updates, mapping = await _run(
        db_session, db_session_factory, stream=None, spent="1000"
    )

    assert outcome.state.termination is not TerminationReason.COMPLETED
    assert "send" not in journal and updates == [], "nothing resumed, nothing sent"
    assert mapping is not None and mapping.status == "dead"
