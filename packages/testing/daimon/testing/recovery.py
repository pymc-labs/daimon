"""Restart-and-replay harness: a provider that outlives a process, and processes that can die.

`ProviderWorld` is a stateful Managed Agents fake whose model answers every
accepted `user.message`, whether or not anyone is still reading the stream. It
is the one thing that survives a simulated restart, so a test can ask what the
provider received (duplicate inputs), how many sessions it holds (orphans) and
how many model calls it ran (what should have been billed).

`Process` is one service process: its own `TurnDeps`, SDK client, session
factory and in-memory queues. `kill()` models SIGKILL: the process's database
handle starts refusing work, so cleanup in `finally` blocks cannot write, the
way a dead process cannot. `cancel()` models a graceful drain: the task is
cancelled and its cleanup runs against a live database.

Faults are queued per route and consumed in order, so a test states exactly
which request meets which failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.deps import TurnDeps
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio.session import (
    _AsyncSessionContextManager,  # pyright: ignore[reportPrivateUsage]
)

type Path_ = Literal["legacy", "mux"]

#: What a queued fault does to the request that meets it.
#:
#: ``ok``          the provider handles the request normally.
#: ``accept_hang`` the provider handles it (an input is accepted and the model
#:                 answers) but the response never reaches the caller.
#: ``hang``        the provider never handles it and the caller never hears back.
#: ``accept_drop`` the provider handles it, then the connection drops.
#: ``refuse_429``  a retryable refusal; nothing ran.
Fault = Literal["ok", "accept_hang", "hang", "accept_drop", "refuse_429"]

#: ``hang_before_span`` / ``hang_before_idle``: the model finished (its events are
#: in the provider's log) but the stream stalls before that event, so a reader
#: never sees it.
StreamFault = Literal["ok", "hang_before_span", "hang_before_idle"]

_SSE = {"content-type": "text/event-stream"}


class _SseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


@dataclass
class Reached:
    """Set when a request met a fault, so a test can kill at that instant."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    count: int = 0

    def hit(self) -> None:
        self.count += 1
        self.event.set()


@dataclass
class ProviderWorld:
    """The provider side of every scenario; shared by all processes of one test."""

    agent: BetaManagedAgentsAgent
    environment: BetaEnvironment
    tenant_id: uuid.UUID
    create_faults: deque[Fault] = field(default_factory=deque[Fault])
    send_faults: deque[Fault] = field(default_factory=deque[Fault])
    stream_faults: deque[StreamFault] = field(default_factory=deque[StreamFault])
    #: Every request the provider saw, in order: (method, path).
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: Text of every user.message the provider accepted: (session id, text).
    accepted_inputs: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: Ids of every model call the provider ran (its span.model_request_end).
    model_calls: list[str] = field(default_factory=list[str])
    #: Sessions the provider created.
    created_sessions: list[str] = field(default_factory=list[str])
    interrupts: list[str] = field(default_factory=list[str])
    reached_send: Reached = field(default_factory=Reached)
    reached_create: Reached = field(default_factory=Reached)
    reached_refusal: Reached = field(default_factory=Reached)
    reached_stream_hang: Reached = field(default_factory=Reached)
    refusal_retry_after_s: int = 2
    #: A stream with nothing to say closes after this long, as the real one does at ~600s.
    quiet_close_s: float = 0.5

    def __post_init__(self) -> None:
        self.ma = FakeMAState()
        agent_body = self.agent.model_dump(mode="json")
        self.ma.agents[self.agent.id] = agent_body
        self.ma.agent_versions[self.agent.id] = {self.agent.version: agent_body}
        self.sessions = FakeSessionsState(ma=self.ma)
        self._sessions_handler = make_fake_sessions_handler(self.sessions)
        self._memory = make_fake_memory_store_handler()
        self._resolver = resolved_agent_env_router(self.agent, self.environment).dispatch
        self._wake = asyncio.Condition()
        self._counter = 0
        self._never = asyncio.Event()

    # -- model ------------------------------------------------------------

    def _answer(self, session_id: str, text: str) -> None:
        """The model's whole turn for one accepted input, appended to the log."""
        self._counter += 1
        n = self._counter
        now = datetime.now(UTC)
        span_id = f"evt_span_{n}"
        events = [
            BetaManagedAgentsAgentMessageEvent(
                id=f"evt_msg_{n}",
                type="agent.message",
                processed_at=now,
                content=[BetaManagedAgentsTextBlock(type="text", text=f"reply to: {text}")],
            ),
            BetaManagedAgentsSpanModelRequestEndEvent(
                id=span_id,
                type="span.model_request_end",
                is_error=False,
                model_request_start_id=f"start_{n}",
                model_usage=ma_model_usage(input_tokens=100, output_tokens=50),
                processed_at=now,
            ),
            BetaManagedAgentsSessionStatusIdleEvent(
                id=f"evt_idle_{n}",
                type="session.status_idle",
                processed_at=now,
                stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
            ),
        ]
        self.sessions.events.setdefault(session_id, []).extend(
            event.model_dump(mode="json") for event in events
        )
        self.model_calls.append(span_id)

    async def _notify(self) -> None:
        async with self._wake:
            self._wake.notify_all()

    # -- HTTP -------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method == "POST" and path == "/v1/sessions":
            return await self._create(request)
        match = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)/events", path)
        if match and request.method == "POST":
            return await self._send(request, match["sid"])
        match = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)/events/stream", path)
        if match and request.method == "GET":
            return await self._stream(match["sid"])
        for handler in (self._sessions_handler, self._memory, self._resolver):
            try:
                return handler(request)
            except NotHandled:
                continue
        raise AssertionError(f"unscripted request {request.method} {path}")

    async def _create(self, request: httpx.Request) -> httpx.Response:
        fault = self.create_faults.popleft() if self.create_faults else "ok"
        if fault == "hang":
            self.reached_create.hit()
            await self._never.wait()
        response = self._sessions_handler(request)
        if response.status_code == 200:
            self.created_sessions.append(response.json()["id"])
        if fault == "accept_hang":
            self.reached_create.hit()
            await self._never.wait()
        if fault == "accept_drop":
            raise httpx.ReadError("session created, response lost")
        return response

    async def _send(self, request: httpx.Request, session_id: str) -> httpx.Response:
        fault = self.send_faults.popleft() if self.send_faults else "ok"
        body = json.loads(request.content)
        events = body.get("events", [])
        if fault == "refuse_429":
            self.reached_refusal.hit()
            return httpx.Response(
                429,
                headers={"retry-after": str(self.refusal_retry_after_s)},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            )
        if fault == "hang":
            self.reached_send.hit()
            await self._never.wait()
        response = self._sessions_handler(request)
        if response.status_code == 200:
            for event in events:
                if event.get("type") == "user.interrupt":
                    self.interrupts.append(session_id)
                if event.get("type") == "user.message":
                    text = "".join(block.get("text", "") for block in event.get("content", []))
                    # Daimon frames a routine's input; the matrix names inputs by their payload.
                    text = text.split("</turn_context>\n\n", 1)[-1]
                    self.accepted_inputs.append((session_id, text))
                    self._answer(session_id, text)
            await self._notify()
        if fault == "accept_hang":
            self.reached_send.hit()
            await self._never.wait()
        if fault == "accept_drop":
            self.reached_send.hit()
            raise httpx.ReadError("input accepted, response lost")
        return response

    async def _stream(self, session_id: str) -> httpx.Response:
        mode = self.stream_faults.popleft() if self.stream_faults else "ok"
        log = self.sessions.events.setdefault(session_id, [])
        cursor = len(log)

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal cursor
            while True:
                while cursor < len(log):
                    event = log[cursor]
                    cursor += 1
                    if event["type"].startswith("user."):
                        continue
                    stalls = {
                        "hang_before_span": "span.model_request_end",
                        "hang_before_idle": "session.status_idle",
                    }
                    if stalls.get(mode) == event["type"]:
                        self.reached_stream_hang.hit()
                        await self._never.wait()
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    if event["type"] == "session.status_idle":
                        return
                async with self._wake:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self.quiet_close_s)
                    except TimeoutError:
                        return  # the server's clean close of a quiet stream

        return httpx.Response(200, headers=_SSE, stream=_SseStream(chunks()))

    def client(self) -> AsyncAnthropic:
        """A real SDK client over this provider; one per process.

        The test builder zeroes the SDK's retry backoff. Production waits, and a
        process can die inside that wait, so the backoff is the refusal's own
        Retry-After here.
        """
        client = build_fake_anthropic(cast(Callable[[httpx.Request], httpx.Response], self.handle))
        backoff = float(self.refusal_retry_after_s)

        def calculate_retry_timeout(*_args: object, **_kwargs: object) -> float:
            return backoff

        object.__setattr__(client, "_calculate_retry_timeout", calculate_retry_timeout)
        return client

    # -- reads for assertions --------------------------------------------

    def inputs_for(self, session_id: str) -> list[str]:
        return [text for sid, text in self.accepted_inputs if sid == session_id]

    def posts(self, suffix: str) -> int:
        return sum(
            1 for method, path in self.requests if method == "POST" and path.endswith(suffix)
        )


class KillableSessionmaker(async_sessionmaker[AsyncSession]):
    """A session factory that stops working when its process is killed."""

    dead: bool = False

    def __call__(self, **local_kw: object) -> AsyncSession:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.dead:
            raise ProcessKilled("process is dead; the database handle is gone")
        return super().__call__(**cast("dict[str, Any]", local_kw))

    def begin(self) -> _AsyncSessionContextManager[AsyncSession]:
        if self.dead:
            raise ProcessKilled("process is dead; the database handle is gone")
        return super().begin()


class ProcessKilled(RuntimeError):
    """Raised for any database use after the process was killed."""


@dataclass
class Process:
    """One service process: its own deps, client, session factory and tasks."""

    world: ProviderWorld
    sessionmaker: KillableSessionmaker
    deps: TurnDeps
    tasks: list[asyncio.Task[Any]] = field(default_factory=list[asyncio.Task[Any]])

    @classmethod
    def start(
        cls, world: ProviderWorld, engine: AsyncEngine, path: Path_, *, tmp_path: Path
    ) -> Process:
        sessionmaker = KillableSessionmaker(engine, expire_on_commit=False)
        deps = TurnDeps(
            anthropic=world.client(),
            sessionmaker=sessionmaker,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            defaults_root=tmp_path,
            tool_safety=OPEN_TOOL_SAFETY,
            mcp=McpSettings(),
            billing_config=None,
            markup=Decimal(1),
            fernet=None,
            github_fallback_pat=None,
            github_app_id=None,
            github_app_private_key=None,
            public_url=None,
            turn_path=path,
        )
        return cls(world=world, sessionmaker=sessionmaker, deps=deps)

    @property
    def path(self) -> Path_:
        return cast(Path_, self.deps.turn_path)

    def spawn[T](self, coro: Awaitable[T]) -> asyncio.Task[T]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def kill(self) -> None:
        """SIGKILL: no cleanup can write, nothing in memory survives."""
        self.sessionmaker.dead = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def cancel(self) -> None:
        """SIGTERM drain that gave up: tasks are cancelled, cleanup runs against a live database."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def restarted(process: Process, path: Path_, *, tmp_path: Path) -> Process:
    """A fresh process against the same provider and database."""
    engine = process.sessionmaker.kw["bind"]
    return Process.start(process.world, engine, path, tmp_path=tmp_path)


async def count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(text(sql), params)).scalar_one())


async def total(engine: AsyncEngine, sql: str, **params: object) -> str:
    async with engine.connect() as connection:
        return str((await connection.execute(text(sql), params)).scalar_one())


async def rows(engine: AsyncEngine, sql: str, **params: object) -> list[tuple[Any, ...]]:
    async with engine.connect() as connection:
        return [tuple(row) for row in (await connection.execute(text(sql), params)).all()]


@dataclass(frozen=True)
class Ledger:
    """What a scenario left in the money and coordination tables."""

    live_leases: int
    operations_by_status: dict[str, int]
    usage_events: dict[str, int]  # provider event id -> rows
    debits: int
    debit_total: str
    journal_events: int
    open_turn_outcomes: int
    finished_turn_outcomes: int
    active_turn_markers: int
    live_mappings: int


async def ledger(engine: AsyncEngine, tenant_id: uuid.UUID) -> Ledger:
    params = {"t": tenant_id}
    operations = await rows(
        engine,
        "select status, count(*) from operation where tenant_id = :t group by status",
        **params,
    )
    usage = await rows(
        engine,
        "select event_id, count(*) from usage_events where tenant_id = :t group by event_id",
        **params,
    )
    return Ledger(
        live_leases=await count(
            engine,
            "select count(*) from thread_lease "
            "where tenant_id = :t and jsonb_typeof(active) = 'object'",
            **params,
        ),
        operations_by_status={status: count for status, count in operations},
        usage_events={event: count for event, count in usage},
        debits=await count(
            engine,
            "select count(*) from tenant_ledger where tenant_id = :t and delta_usd < 0",
            **params,
        ),
        debit_total=str(
            await total(
                engine,
                "select coalesce(sum(-delta_usd), 0) from tenant_ledger "
                "where tenant_id = :t and delta_usd < 0",
                **params,
            )
        ),
        journal_events=await count(engine, "select count(*) from journal"),
        open_turn_outcomes=await count(
            engine,
            "select count(*) from turn_outcomes where tenant_id = :t and ended_at is null",
            **params,
        ),
        finished_turn_outcomes=await count(
            engine,
            "select count(*) from turn_outcomes where tenant_id = :t and ended_at is not null",
            **params,
        ),
        active_turn_markers=await count(
            engine,
            "select count(*) from thread_sessions where tenant_id = :t "
            "and active_turn_message_id is not null",
            **params,
        ),
        live_mappings=await count(
            engine,
            "select count(*) from thread_sessions where tenant_id = :t and status = 'live'",
            **params,
        ),
    )


def record(surface: str, point: str, mode: str, path: str, **observed: object) -> None:
    target = os.environ.get("RECOVERY_MATRIX_JSONL")
    if not target:
        return
    line = {"surface": surface, "point": point, "mode": mode, "path": path, **observed}
    with open(target, "a", encoding="utf-8") as sink:
        sink.write(json.dumps(line, default=str, sort_keys=True) + "\n")
