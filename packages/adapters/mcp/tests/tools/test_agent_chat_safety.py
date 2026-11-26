"""Agent-chat turns under the unattended tool gate, over a fake MA transport.

Every send is preceded by a re-gate of the session's own tool config; every
pause is answered by the sending process, or by its owner's resume of a turn
agent chat started, from the current policy; a read answers nothing, before
or after a restart. The deciders, reducer and gate are the real ones.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _agent_chat_safety as safety
from daimon.adapters.mcp.tools import agent_chat
from daimon.adapters.mcp.tools import sessions as general_sessions
from daimon.adapters.mcp.tools._session_access import hub_session_access
from daimon.adapters.mcp.tools.agent_chat import (
    _ask_impl,  # pyright: ignore[reportPrivateUsage]
    _await_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _continue_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _deliver_turn_charts_impl,  # pyright: ignore[reportPrivateUsage]
    _get_session_impl,  # pyright: ignore[reportPrivateUsage]
    _list_events_impl,  # pyright: ignore[reportPrivateUsage]
    _start_turn_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_CHAT_SESSION,
    MA_METADATA_KEY_PRIVATE_DM,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_tool_rules, session_settle_state
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.tool_safety import (
    CONFIRMED_DAIMON_TOOLS,
    ToolAnnotations,
    ToolCall,
    ToolSafetyPolicy,
)
from daimon.testing import ma_agent, ma_environment, with_coordinator, with_hidden_capability
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import (
    build_fake_anthropic,
    json_body,
    ma_session,
    send_events_response,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import auth_identity

_TENANT_ID = uuid.uuid4()
_MA_AGENT_ID = "ag_safety"
_AGENT_UUID = derive_agent_uuid(tenant_id=_TENANT_ID, ma_agent_id=_MA_AGENT_ID)
_OWNER = uuid.uuid4()
_OTHER = uuid.uuid4()
_SESSION = "ses_safety"
_PUBLIC = "https://daimon.example/mcp"
_LINEAR_URL = "https://linear.example/mcp"
_ENABLED = ToolSafetyPolicy(enabled=True)
_DISABLED = ToolSafetyPolicy(enabled=False)

Call = tuple[str, str, dict[str, Any]]


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "_REOPEN_S", 0.01)
    monkeypatch.setattr(safety, "_CATCH_UP_GAP_S", 0.01)


def _toolset(server: str, policy: str, *names: str) -> dict[str, Any]:
    """A toolset frozen as `policy`, with that policy on per-tool configs too."""
    return {
        "type": "mcp_toolset",
        "mcp_server_name": server,
        "configs": [
            {"name": name, "enabled": True, "permission_policy": {"type": policy}} for name in names
        ],
        "default_config": {"enabled": True, "permission_policy": {"type": policy}},
    }


class _LiveStream(httpx.AsyncByteStream):
    """One open SSE connection: frames arrive as the session's log grows."""

    def __init__(self) -> None:
        self.frames: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (frame := await self.frames.get()) is not None:
            yield frame


class _Log(list[dict[str, Any]]):
    """The event log. Each event gets the next `processed_at` and goes to the open
    streams, as Managed Agents does; a test may queue an event without one first."""

    def __init__(self) -> None:
        super().__init__()
        self.streams: list[_LiveStream] = []
        self._at = dt.datetime(2026, 11, 26, tzinfo=dt.UTC)

    def append(self, event: dict[str, Any]) -> None:
        self._at += dt.timedelta(seconds=1)
        event.setdefault("processed_at", self._at.isoformat())
        super().append(event)
        frame = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for stream in self.streams:
            stream.frames.put_nowait(frame)

    def __iadd__(self, events: Iterable[dict[str, Any]]) -> _Log:  # type: ignore[override]
        for event in events:
            self.append(event)
        return self

    def drop(self) -> None:
        """Close every open stream, as a proxy or the provider may."""
        for stream in self.streams:
            stream.frames.put_nowait(None)
        self.streams.clear()

    def unheard(self) -> AbstractContextManager[None]:
        """Events appended inside reach the list but no stream: a missed event."""

        @contextmanager
        def muted() -> Iterator[None]:
            streams, self.streams = self.streams, []
            try:
                yield
            finally:
                self.streams = streams

        return muted()


class FakeSession:
    """One MA session: tool updates, the event log, and what was sent, in order.

    A `user.message` starts a turn that pauses on each batch of `script` in
    turn; answering every pending call moves to the next batch, and after the
    last one the turn ends. `calls` is the wire order of every mutating
    request. `page_size` pages event lists; `fail_update` / `fail_confirm`
    refuse those requests; `throttle` answers the next N sends of a kind
    (`message`, `confirm`) with a retryable 429, and `attempts` counts every
    send. With `honor_permissions`, a call the session's current config does
    not ask on runs at once (`executed`) instead of pausing, as MA does.
    """

    def __init__(
        self,
        *,
        account: uuid.UUID = _OWNER,
        frozen: str = "always_allow",
        tool: str = "create_issue",
        script: list[list[Call]] | None = None,
    ) -> None:
        self.metadata = {"daimon_agent_chat": "true", MA_METADATA_KEY_ACCOUNT: str(account)}
        self.tools = [_toolset("linear", frozen, "create_issue")]
        self.servers = [{"name": "linear", "type": "url", "url": _LINEAR_URL}]
        self.script = script or [[("linear", tool, {})]]
        self.events = _Log()
        self.calls: list[tuple[str, Any]] = []
        # Every read of the session's status or event list, to show nothing polls.
        self.reads = 0
        # How many times the events stream was opened.
        self.opened = 0
        # With `live`, the events stream stays open and delivers the log as it grows.
        self.live = False
        self.status = "idle"
        # The next N status reads still say "running": the status trails the events.
        self.stale_status_reads = 0
        # The next N event lists show only the first `_listed` events: the list
        # trails the stream.
        self.stale_lists = 0
        self._listed = 0
        self.page_size = 1000
        self.fail_update = False
        self.unconstrainable: Callable[[Any], Any] | None = None
        self.fail_confirm = False
        self.on_confirm: Callable[[], None] | None = None
        self.honor_permissions = False
        self.throttle: dict[str, int] = {}
        self.attempts: dict[str, int] = {}
        self._remaining: list[list[Call]] = []
        self._ids = 0

    def with_daimon(self, url: str = _PUBLIC) -> FakeSession:
        self.tools.append(_toolset("daimon-mcp", "always_allow"))
        self.servers.append({"name": "daimon-mcp", "type": "url", "url": url})
        return self

    def _id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}_{self._ids:03d}"

    def _asks(self, server: str, name: str) -> bool:
        toolset = next((t for t in self.tools if t.get("mcp_server_name") == server), None)
        if toolset is None:
            return True
        config = next((c for c in toolset.get("configs") or [] if c["name"] == name), None)
        chosen = config or toolset["default_config"]
        return chosen["permission_policy"]["type"] == "always_ask"

    def _run(self, batch: list[Call]) -> None:
        """A turn reaching `batch`: unasked calls run, asked ones pause."""
        if self.honor_permissions:
            for server, name, _input in batch:
                if not self._asks(server, name):
                    self.calls.append(("executed", f"{server}/{name}"))
            batch = [c for c in batch if self._asks(c[0], c[1])]
        if batch:
            self._pause_on(batch)
        elif self._remaining:
            self._run(self._remaining.pop(0))
        else:
            self._end()

    def _end(self) -> None:
        self.events += [
            {
                "id": self._id("evt_msg"),
                "type": "agent.message",
                "content": [{"type": "text", "text": "done"}],
            },
            {
                "id": self._id("evt_idle"),
                "type": "session.status_idle",
                "stop_reason": {"type": "end_turn"},
            },
        ]

    def _pause_on(self, batch: list[Call]) -> None:
        ids: list[str] = []
        for server, name, tool_input in batch:
            tool_use = self._id("evt_tool")
            ids.append(tool_use)
            self.events.append(
                {
                    "id": tool_use,
                    "type": "agent.mcp_tool_use",
                    "mcp_server_name": server,
                    "name": name,
                    "input": tool_input,
                    "evaluated_permission": "ask",
                }
            )
        self.events.append(
            {
                "id": self._id("evt_pause"),
                "type": "session.status_idle",
                "stop_reason": {"type": "requires_action", "event_ids": ids},
            }
        )

    def pause(self) -> str:
        """Append a turn paused on the script's first batch; returns its boundary."""
        boundary = self._start_turn()
        self._pause_on(self._remaining.pop(0))
        return boundary

    def _start_turn(self) -> str:
        boundary = self._id("evt_user")
        self.events.append(
            {"id": boundary, "type": "user.message", "content": [{"type": "text", "text": "hi"}]}
        )
        self._remaining = list(self.script)
        return boundary

    @property
    def confirmations(self) -> list[dict[str, Any]]:
        return [body for kind, body in self.calls if kind == "confirm"]

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    @property
    def model(self) -> Any:
        snapshot = ma_session(
            id=_SESSION,
            agent=ma_agent(id=_MA_AGENT_ID, tools=self.tools, mcp_servers=self.servers),
            status=self.status,
            metadata=self.metadata,
        )
        return self.unconstrainable(snapshot) if self.unconstrainable else snapshot

    def router(self, router: MARouter | None = None) -> MARouter:
        router = router or MARouter()
        router.add("GET", rf"/v1/sessions/{_SESSION}$", self._retrieve)
        # `continue_turn` reads the agent to see if it changed since the session froze it.
        router.add("GET", rf"/v1/agents/{_MA_AGENT_ID}$", self._agent)
        router.add("POST", rf"/v1/sessions/{_SESSION}$", self._update)
        router.add("GET", rf"/v1/sessions/{_SESSION}/events$", self._list)
        router.add("GET", rf"/v1/sessions/{_SESSION}/events/stream$", self._stream)
        router.add("POST", rf"/v1/sessions/{_SESSION}/events$", self._send)
        return router

    def _agent(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        agent = ma_agent(id=_MA_AGENT_ID, tools=self.tools, mcp_servers=self.servers)
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    def _retrieve(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        self.reads += 1
        body = self.model.model_dump(mode="json")
        if self.stale_status_reads > 0:
            self.stale_status_reads -= 1
            body["status"] = "running"
        return httpx.Response(200, json=body)

    def list_trails(self, lists: int) -> None:
        """The next `lists` event lists show nothing appended from now on."""
        self.stale_lists, self._listed = lists, len(self.events)

    def _stream(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        """A live connection, or (the default) one that closes at once: a stream that
        cannot be used, after which the caller reads the state again."""
        self.opened += 1
        if not self.live:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")
        stream = _LiveStream()
        self.events.streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    def _update(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if self.fail_update:
            return httpx.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error"}}
            )
        agent = json_body(request)["agent"]
        self.calls.append(("update", agent))
        self.tools = agent["tools"]
        self.servers = agent["mcp_servers"]
        return httpx.Response(200, json=self.model.model_dump(mode="json"))

    def _list(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        self.reads += 1
        params = request.url.params
        events = list(self.events)
        if self.stale_lists > 0:
            self.stale_lists -= 1
            events = events[: self._listed]
        types = params.get_list("types[]") or params.get_list("types")
        if types:
            events = [e for e in events if e["type"] in types]
        if params.get("order") == "desc":
            events.reverse()
        start = int(params.get("page") or 0)
        size = min(int(params.get("limit") or 100), self.page_size)
        page = events[start : start + size]
        more = start + size < len(events)
        return httpx.Response(
            200, json={"data": page, "next_page": str(start + size) if more else None}
        )

    def _send(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(request)["events"]
        kind = "confirm" if body[0]["type"] == "user.tool_confirmation" else "message"
        self.attempts[kind] = self.attempts.get(kind, 0) + 1
        if self.throttle.get(kind):
            self.throttle[kind] -= 1
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "rate_limit_error"}},
            )
        if self.fail_confirm and any(e["type"] == "user.tool_confirmation" for e in body):
            return httpx.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error"}}
            )
        echoed: list[dict[str, Any]] = []
        for event in body:
            if event["type"] == "user.message":
                self.calls.append(("message", event))
                boundary = self._start_turn()
                echoed.append({**event, "id": boundary})
                self._run(self._remaining.pop(0))
            elif event["type"] == "user.tool_confirmation":
                self.calls.append(("confirm", event))
                self.events.append({**event, "id": self._id("evt_confirm")})
        if any(e["type"] == "user.tool_confirmation" for e in body) and not self._pending():
            if self.on_confirm is not None:
                self.on_confirm()
            if self._remaining:
                self._run(self._remaining.pop(0))
            else:
                self._end()
        return send_events_response(data=echoed)

    def _pending(self) -> set[str]:
        asked: set[str] = set()
        for event in self.events:
            if event["type"] == "session.status_idle" and event["stop_reason"]["type"] == (
                "requires_action"
            ):
                asked |= set(event["stop_reason"]["event_ids"])
            if event["type"] == "user.tool_confirmation":
                asked.discard(event["tool_use_id"])
        return asked


def _runtime(
    fake: FakeSession,
    factory: async_sessionmaker[AsyncSession],
    policy: ToolSafetyPolicy,
    *,
    public_url: str | None = None,
) -> McpRuntime:
    settings = MagicMock()
    settings.tool_safety = policy
    settings.mcp = McpSettings(public_url=public_url, jwt_secret=None)
    settings.features = {}
    settings.github.fallback_pat = None
    return McpRuntime(
        session_factory=factory,
        client=build_fake_anthropic(fake.router().dispatch),  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(environment_name="production"),
    )


def _auth(
    account: uuid.UUID = _OWNER, *, bound: str | None = None, platform: str | None = None
) -> AuthIdentity:
    return auth_identity(
        account,
        _TENANT_ID,
        role=Role.USER,
        agent_id=_AGENT_UUID,
        bound_channel_id=bound,
        platform=platform,
    )


async def _settle(runtime: McpRuntime) -> None:
    """Wait for this runtime's confirmation worker, if one is running."""
    worker = safety._workers.get((id(runtime), _SESSION))  # pyright: ignore[reportPrivateUsage]
    if worker is not None:
        await asyncio.wait_for(worker.task, timeout=5)


def _restart() -> None:
    """A new process: no worker survives."""
    for worker in safety._workers.values():  # pyright: ignore[reportPrivateUsage]
        worker.task.cancel()
    safety._workers.clear()  # pyright: ignore[reportPrivateUsage]


def _no_workers() -> bool:
    return not safety._workers  # pyright: ignore[reportPrivateUsage]


async def _record(
    factory: async_sessionmaker[AsyncSession], boundary: str, channel: str | None = None
) -> None:
    """What an agent-chat send left behind before the restart."""
    async with factory.begin() as db:
        await session_settle_state.record_unattended_turn(
            db,
            managed_session_id=_SESSION,
            turn_event_id=boundary,
            rules_channel_id=channel,
            send=None,
        )


def _asks(tools: list[dict[str, Any]], server: str = "linear") -> bool:
    toolset = next(t for t in tools if t.get("mcp_server_name") == server)
    return toolset["default_config"]["permission_policy"]["type"] == "always_ask" and all(
        c["permission_policy"]["type"] == "always_ask" for c in toolset["configs"]
    )


@pytest.fixture
def factory(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """A pooled factory: the worker and the request each take a connection."""
    return committing_sessionmaker


def _creation_patches(fake: FakeSession, creator: AsyncMock | None = None) -> Any:
    agent = ma_agent(id=_MA_AGENT_ID, tools=fake.tools, mcp_servers=fake.servers)
    return (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._resolve_ma_agent", AsyncMock(return_value=agent)
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.find_environment_by_daimon_tag",
            AsyncMock(return_value=ma_environment(name="production")),
        ),
        # Creation's own gating is covered in core (test_sessions*); this
        # hands back a session whose frozen config allows, as an old one would.
        patch(
            "daimon.adapters.mcp.tools.agent_chat.create_session",
            creator or AsyncMock(return_value=fake.model),
        ),
    )


async def _start(runtime: McpRuntime, fake: FakeSession, auth: AuthIdentity) -> dict[str, str]:
    a, b, c = _creation_patches(fake)
    with a, b, c:
        return await _start_turn_impl(runtime, auth, "hi")


Entry = Callable[[McpRuntime, FakeSession], Any]


async def _entry_start(runtime: McpRuntime, fake: FakeSession) -> None:
    await _start(runtime, fake, _auth())


async def _entry_continue(runtime: McpRuntime, fake: FakeSession) -> None:
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")


async def _entry_ask_existing(runtime: McpRuntime, fake: FakeSession) -> None:
    result = await _ask_impl(runtime, _auth(), "hi", handle=_SESSION)
    assert result.message == "done"


async def _entry_hub_continue(runtime: McpRuntime, fake: FakeSession) -> None:
    with hub_session_access(Subject(platform_user_id="U1"), "continue"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")


@pytest.mark.parametrize(
    "entry",
    [_entry_start, _entry_continue, _entry_ask_existing, _entry_hub_continue],
    ids=["start_turn", "continue_turn", "ask_existing", "hub_continue_turn"],
)
@pytest.mark.parametrize(
    ("tool", "policy", "expected"),
    [
        ("create_issue", _ENABLED, "deny"),
        ("get_issue", _ENABLED, "allow"),
        ("opaque_query", _ENABLED, "deny"),
        (
            "create_issue",
            ToolSafetyPolicy(enabled=True, unattended_writes=("linear/create_issue",)),
            "allow",
        ),
    ],
)
async def test_every_send_is_regated_and_its_pause_decided_by_current_policy(
    entry: Entry,
    tool: str,
    policy: ToolSafetyPolicy,
    expected: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A frozen `always_allow` (default and per-tool) is rewritten before the
    message, so the call pauses and the unattended decider answers it."""
    fake = FakeSession(tool=tool)
    runtime = _runtime(fake, factory, policy)
    await entry(runtime, fake)
    await _settle(runtime)

    kinds = fake.kinds()
    assert kinds.index("update") < kinds.index("message")
    assert _asks(fake.calls[kinds.index("update")][1]["tools"])
    assert [c["result"] for c in fake.confirmations] == [expected]


async def test_ask_new_session_is_gated_before_its_message(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    creator = AsyncMock(return_value=fake.model)
    a, b, c = _creation_patches(fake, creator)
    with a, b, c:
        result = await _ask_impl(runtime, _auth(), "hi")
    assert result.message == "done"
    assert creator.call_args.kwargs["tool_safety"] == _ENABLED
    kinds = fake.kinds()
    assert kinds.index("update") < kinds.index("message")
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_disabled_policy_is_the_explicit_off_switch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Off leaves the session as it is and allows the third-party call it pauses on."""
    fake = FakeSession(frozen="always_ask")
    runtime = _runtime(fake, factory, _DISABLED)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)
    assert fake.kinds() == ["message", "confirm"]
    assert [c["result"] for c in fake.confirmations] == ["allow"]


async def test_a_failed_gate_sends_nothing_and_starts_no_worker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    fake.fail_update = True
    runtime = _runtime(fake, factory, _ENABLED)
    with pytest.raises(Exception):  # noqa: B017 - whatever the provider's refusal maps to
        await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    with pytest.raises(Exception):  # noqa: B017
        await _start(runtime, fake, _auth())
    assert fake.calls == []
    assert _no_workers()


@pytest.mark.parametrize(
    ("unconstrainable", "named"),
    [
        (with_coordinator, "delegation to other agents"),
        (
            with_hidden_capability,
            "unreviewed provider field session.agent.future_capability: "
            "capability behavior has not been reviewed",
        ),
    ],
    ids=["coordinator", "hidden_field"],
)
@pytest.mark.parametrize(
    "entry",
    [_entry_start, _entry_continue, _entry_ask_existing, _entry_hub_continue],
    ids=["start_turn", "continue_turn", "ask_existing", "hub_continue_turn"],
)
async def test_a_coordinator_session_is_refused_before_anything_is_sent(
    entry: Entry,
    unconstrainable: Callable[[Any], Any],
    named: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Its roster's children could write unasked, and MA cannot drop it mid-session.

    The refusal names what it refused; only a coordinator is called delegation.
    """
    fake = FakeSession()
    fake.unconstrainable = unconstrainable
    runtime = _runtime(fake, factory, _ENABLED)
    with pytest.raises(ToolError, match="cannot run here") as refused:
        await entry(runtime, fake)
    assert str(refused.value) == (
        f"This agent cannot run here: an agent chat nobody watches cannot limit {named}."
    )
    assert fake.calls == []
    assert _no_workers()


def _with_preview_fields(session: Any) -> Any:
    """`session` as Preview returns it (2026-10-20): fields the pinned SDK does not parse."""
    dumped = session.model_dump(mode="json")
    dumped["budget"] = None
    dumped["agent"]["execution_identity"] = {"type": "service_account"}
    return type(session).model_validate(dumped)


@pytest.mark.parametrize(
    "entry",
    [_entry_start, _entry_continue, _entry_ask_existing, _entry_hub_continue],
    ids=["start_turn", "continue_turn", "ask_existing", "hub_continue_turn"],
)
async def test_a_session_with_preview_provider_fields_runs(
    entry: Entry, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Every agent chat turn was refused on Preview over `budget` and `execution_identity`."""
    fake = FakeSession(tool="get_issue")
    fake.unconstrainable = _with_preview_fields
    runtime = _runtime(fake, factory, _ENABLED)
    await entry(runtime, fake)
    await _settle(runtime)

    kinds = fake.kinds()
    assert kinds.index("update") < kinds.index("message")
    assert [c["result"] for c in fake.confirmations] == ["allow"]


@pytest.mark.parametrize(
    "entry",
    [_entry_start, _entry_continue, _entry_ask_existing, _entry_hub_continue],
    ids=["start_turn", "continue_turn", "ask_existing", "hub_continue_turn"],
)
@pytest.mark.parametrize(
    ("server", "policy"),
    [("linear", _DISABLED), ("daimon-mcp", _ENABLED), ("daimon-mcp", _DISABLED)],
    ids=["foreign-safety-off", "trusted-safety-on", "trusted-safety-off"],
)
async def test_every_mcp_call_of_an_agent_chat_turn_pauses_for_the_decider(
    entry: Entry, server: str, policy: ToolSafetyPolicy, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Trusted Daimon tools, and foreign ones with safety off, still pause; the
    decider allows them as before."""
    fake = FakeSession(script=[[(server, "list_agents", {})]]).with_daimon()
    runtime = _runtime(fake, factory, policy, public_url=_PUBLIC)
    await entry(runtime, fake)
    await _settle(runtime)

    kinds = fake.kinds()
    assert kinds.index("update") < kinds.index("message")
    tools = fake.calls[kinds.index("update")][1]["tools"]
    for toolset in tools:
        assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
        assert all(c["permission_policy"] == {"type": "always_ask"} for c in toolset["configs"])
    assert [c["result"] for c in fake.confirmations] == ["allow"]


async def test_every_pause_of_a_turn_is_decided_and_each_call_on_its_own(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A read beside a write in one pause, then a second pause: per-call verdicts."""
    fake = FakeSession(
        script=[
            [("linear", "get_issue", {}), ("linear", "create_issue", {})],
            [("linear", "list_teams", {})],
        ]
    )
    runtime = _runtime(fake, factory, _ENABLED)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["allow", "deny", "allow"]


async def test_a_paged_history_is_read_back_to_its_boundary(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    fake.page_size = 2
    runtime = _runtime(fake, factory, _ENABLED)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_history_past_the_page_ceiling_answers_nothing(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety, "_MAX_TURN_PAGES", 1)
    fake = FakeSession()
    fake.page_size = 1
    runtime = _runtime(fake, factory, _DISABLED)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)  # the worker gave up, failing closed
    assert fake.confirmations == []


async def test_a_refused_confirmation_leaves_the_turn_paused_for_its_owner(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    fake.fail_confirm = True
    runtime = _runtime(fake, factory, _ENABLED)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)  # the worker gave up, failing closed
    info = await _get_session_impl(runtime, _auth(), _SESSION)
    assert (info.status, info.hint) == ("paused", safety.PAUSED_HINT)
    fake.fail_confirm = False
    await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_restart_after_a_partial_batch_answers_only_the_rest(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(
        frozen="always_ask",
        script=[[("linear", "get_issue", {}), ("linear", "create_issue", {})]],
    )
    boundary = fake.pause()
    first = next(e for e in fake.events if e["type"] == "agent.mcp_tool_use")
    fake.events.append(
        {
            "id": "evt_early",
            "type": "user.tool_confirmation",
            "tool_use_id": first["id"],
            "result": "allow",
        }
    )
    await _record(factory, boundary)
    runtime = _runtime(fake, factory, _ENABLED)
    _restart()
    await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert [(c["tool_use_id"], c["result"]) for c in fake.confirmations] == [
        (next(e["id"] for e in fake.events if e.get("name") == "create_issue"), "deny")
    ]


async def test_restart_then_read_stays_paused_and_answers_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, _DISABLED)
    _restart()
    for _ in range(3):
        info = await _get_session_impl(runtime, _auth(), _SESSION)
        assert (info.status, info.hint) == ("paused", safety.PAUSED_HINT)
    await asyncio.sleep(0.05)
    assert fake.calls == []
    assert _no_workers()


async def test_every_read_surface_answers_nothing(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The general get_session reports the pause too; no read sends or starts a worker."""
    fake = FakeSession(frozen="always_ask")
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, _ENABLED)
    _restart()
    settled: list[str] = []

    async def on_read(session: Any) -> None:
        settled.append(session.id)

    monkeypatch.setattr(
        general_sessions,
        "list_agents_by_tenant",
        AsyncMock(return_value=[ma_agent(id=_MA_AGENT_ID)]),
    )
    general = await general_sessions._get_session_impl(runtime, _auth(), _SESSION)  # pyright: ignore[reportPrivateUsage]
    assert (general.status, general.hint) == ("paused", safety.PAUSED_HINT)
    await _get_session_impl(runtime, _auth(), _SESSION, on_read=on_read)
    await _list_events_impl(runtime, _auth(), _SESSION, None, None, "asc", on_read=on_read)
    with pytest.raises(ToolError, match="paused"):
        await _deliver_turn_charts_impl(runtime, _auth(), _SESSION)
    await asyncio.sleep(0.05)
    assert fake.calls == []
    assert _no_workers()
    assert settled == [_SESSION], "only the transcript read's settle hook ran; it bills only"


async def test_hub_admin_read_of_another_accounts_paused_channel_session_answers_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review 2, finding 2: a read grant never drives another account's turn."""
    fake = FakeSession(account=_OTHER, frozen="always_ask")
    fake.metadata[MA_METADATA_KEY_CHANNEL] = "C1"
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, _ENABLED)
    admin = Subject(is_admin=True, platform_user_id="UA")
    with hub_session_access(admin, "read"):
        info = await _get_session_impl(runtime, _auth(), _SESSION)
    assert info.status == "paused"
    with hub_session_access(admin, "continue"), pytest.raises(ToolError, match="not found"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await asyncio.sleep(0.05)
    assert fake.calls == []
    assert _no_workers()


async def test_owner_cannot_resume_a_pause_agent_chat_did_not_start(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An attended card stays with the person it asks (no agent-chat record)."""
    fake = FakeSession(frozen="always_ask")
    fake.metadata[MA_METADATA_KEY_CHANNEL] = "C1"
    fake.pause()
    runtime = _runtime(fake, factory, _ENABLED)
    info = await _get_session_impl(runtime, _auth(), _SESSION)
    assert (info.status, info.hint) == ("paused", safety.AWAITING_CARD_HINT)
    for message in (None, "go on"):
        with pytest.raises(ToolError, match="approval in the chat"):
            await _continue_turn_impl(runtime, _auth(), _SESSION, message)
    assert fake.confirmations == []
    assert "message" not in fake.kinds()


async def test_a_record_of_an_older_turn_does_not_cover_a_newer_card(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    await _record(factory, "evt_older_agent_chat_turn")
    fake.pause()
    runtime = _runtime(fake, factory, _DISABLED)
    with pytest.raises(ToolError, match="approval in the chat"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert fake.confirmations == []


async def test_another_account_cannot_resume_a_paused_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(account=_OTHER, frozen="always_ask")
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, _DISABLED)
    with pytest.raises(ToolError, match="not found"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert fake.calls == []


@pytest.mark.parametrize(("policy", "expected"), [(_ENABLED, "deny"), (_DISABLED, "allow")])
async def test_owner_continue_after_restart_resumes_under_current_policy(
    policy: ToolSafetyPolicy, expected: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Review 1, finding 3: the policy now in force decides, enabled or not."""
    fake = FakeSession()
    boundary = fake.pause()
    await _record(factory, boundary)
    runtime = _runtime(fake, factory, policy)
    _restart()
    started = await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert started["turn_event_id"] == boundary
    assert [c["result"] for c in fake.confirmations] == [expected]
    assert "message" not in fake.kinds()
    # Policy on or off, the resume first makes every call pause for the decider.
    assert fake.calls[0][0] == "update" and _asks(fake.calls[0][1]["tools"])
    assert (await _get_session_impl(runtime, _auth(), _SESSION)).status == "idle"


async def test_continue_with_a_message_resumes_first_then_sends(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    old = fake.pause()
    await _record(factory, old)
    runtime = _runtime(fake, factory, _ENABLED)
    started = await _continue_turn_impl(runtime, _auth(), _SESSION, "next")
    await _settle(runtime)
    assert started["turn_event_id"] != old
    assert fake.kinds() == ["confirm", "message", "confirm"]


async def test_the_message_after_a_resume_is_gated_again_under_the_policy_then(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review A, finding 3: a policy change during the wait reaches the new message."""
    fake = FakeSession()
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, _DISABLED)

    def turn_safety_on() -> None:
        runtime.settings.tool_safety = _ENABLED

    fake.on_confirm = turn_safety_on
    await _continue_turn_impl(runtime, _auth(), _SESSION, "next")
    await _settle(runtime)
    # The session already asks after the resume's gate, so the policy that
    # changed meanwhile reaches the new message through the decider.
    assert fake.kinds() == ["update", "confirm", "message", "confirm"]
    assert [c["result"] for c in fake.confirmations] == ["allow", "deny"]


async def test_a_failed_gate_after_the_resume_leaves_the_message_unsent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, _DISABLED)

    def drift_and_break_updates() -> None:
        # Something reset the session to always_allow while it was resumed.
        runtime.settings.tool_safety = _ENABLED
        fake.tools = [_toolset("linear", "always_allow", "create_issue")]
        fake.fail_update = True

    fake.on_confirm = drift_and_break_updates
    with pytest.raises(Exception):  # noqa: B017 - the provider's refusal
        await _continue_turn_impl(runtime, _auth(), _SESSION, "next")
    assert fake.kinds() == ["update", "confirm"]


async def test_continue_with_a_message_refuses_when_the_resumed_turn_runs_on(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSession(frozen="always_ask")
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, _ENABLED)
    # The resumed turn never ends here.
    monkeypatch.setattr(FakeSession, "_pending", lambda self: {"evt_still"})
    monkeypatch.setattr(agent_chat, "_RESUME_WAIT_S", 0.1)
    with pytest.raises(ToolError, match="not sent"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, "next")
    assert "message" not in fake.kinds()


async def test_continue_without_a_message_needs_a_paused_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    with pytest.raises(ToolError, match="message is required"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert "message" not in fake.kinds()


async def test_worker_answers_only_its_own_turn(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pause under another boundary (an attended card, a newer turn) is not its own."""
    fake = FakeSession(frozen="always_ask")
    fake.pause()
    runtime = _runtime(fake, factory, _DISABLED)
    worker = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id="evt_someone_else", rules_channel_id=None
    )
    await asyncio.sleep(0.1)
    assert not worker.done()
    worker.cancel()
    assert fake.calls == []


async def test_another_runtime_reads_a_pause_it_does_not_answer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the runtime whose worker owns the turn reads it as running; no lease."""
    fake = FakeSession(frozen="always_ask")
    boundary = fake.pause()
    await _record(factory, boundary)
    owner = _runtime(fake, factory, _ENABLED)
    other = _runtime(fake, factory, _ENABLED)
    # The owner's worker is stuck before answering: its decider never returns.
    with patch.object(
        safety, "_answer", AsyncMock(side_effect=lambda *_a, **_k: asyncio.Event().wait())
    ):
        worker = safety.watch_turn(
            owner, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None
        )
        try:
            assert (await _get_session_impl(owner, _auth(), _SESSION)).status == "running"
            assert (await _get_session_impl(other, _auth(), _SESSION)).status == "paused"
            await asyncio.sleep(0.05)
            assert fake.calls == []
        finally:
            worker.cancel()


@pytest.mark.parametrize("policy", [_ENABLED, _DISABLED], ids=["enabled", "disabled"])
async def test_a_protected_first_party_action_is_refused_unattended(
    policy: ToolSafetyPolicy, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Review A, finding 1: nobody is here to press its card, policy on or off.

    A provider switch is protected too: it asks before another provider receives the chat.
    """
    call: Call = ("daimon-mcp", "microsoft_send", {"action": "send_mail"})
    fake = FakeSession(script=[[call]]).with_daimon()
    await _record(factory, fake.pause())
    runtime = _runtime(fake, factory, policy, public_url=_PUBLIC)
    _restart()
    await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["deny"], "nobody is here to confirm"
    [(_, agent)] = [c for c in fake.calls if c[0] == "update"]
    daimon = next(t for t in agent["tools"] if t["mcp_server_name"] == "daimon-mcp")
    asked = {c["name"] for c in daimon["configs"] if c["permission_policy"]["type"] == "always_ask"}
    assert "microsoft_send" in asked, asked
    assert "request_agent_backend_switch" in asked, "a move to another provider asks"


async def test_a_foreign_reserved_server_is_healed_with_safety_off(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review A, finding 5."""
    fake = FakeSession().with_daimon(url="https://evil.example/mcp")
    runtime = _runtime(fake, factory, _DISABLED, public_url=_PUBLIC)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    kinds = fake.kinds()
    assert kinds.index("update") < kinds.index("message")
    servers = {s["name"]: s["url"] for s in fake.calls[0][1]["mcp_servers"]}
    assert servers == {"linear": _LINEAR_URL, "daimon-mcp": _PUBLIC}


async def _deny_in_channel(
    factory: async_sessionmaker[AsyncSession], channel: str, tool: str = "get_issue"
) -> None:
    async with factory.begin() as db:
        await make_tenant(db, id=_TENANT_ID)
    async with factory.begin() as db:
        await agent_tool_rules.set_rule(
            db,
            tenant_id=_TENANT_ID,
            agent_id=_MA_AGENT_ID,
            agent_name="safety",
            tool_key=f"linear/{tool}",
            channel_id=channel,
            server_url=_LINEAR_URL,
            rule="deny",
            set_by="admin",
            actor=Subject(is_admin=True, platform_user_id="UA"),
        )


_RULES = ToolSafetyPolicy(enabled=True, agent_rules=True)


@pytest.mark.parametrize(
    ("auth", "stamp", "expected"),
    [
        (_auth(bound="C1", platform="discord"), None, "deny"),
        (_auth(bound="C1:1700000000.0001", platform="slack"), None, "deny"),
        (_auth(), "C1", "deny"),
        (_auth(bound="C2", platform="discord"), None, "allow"),
        (_auth(), None, "allow"),
    ],
    ids=["bound-key", "slack-thread-parent", "channel-session", "other-channel", "no-channel"],
)
async def test_channel_tool_rules_reach_agent_chat_turns(
    auth: AuthIdentity,
    stamp: str | None,
    expected: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review A, finding 2: the turn's channel's rules apply, a read included."""
    await _deny_in_channel(factory, "C1")
    fake = FakeSession(tool="get_issue")
    if stamp is not None:
        fake.metadata[MA_METADATA_KEY_CHANNEL] = stamp
    runtime = _runtime(fake, factory, _RULES)
    await _continue_turn_impl(runtime, auth, _SESSION, "hi")
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == [expected]


@pytest.mark.parametrize("channel", ["C1", "C1:1234.5678"])
async def test_a_routine_privacy_stamp_keeps_its_channel_rules(factory, channel):
    from dataclasses import replace

    await _deny_in_channel(factory, "C1")
    fake = FakeSession(tool="get_issue")
    fake.metadata[MA_METADATA_KEY_CHANNEL] = channel
    fake.metadata[MA_METADATA_KEY_PRIVATE_DM] = "routine:job"
    auth = replace(_auth(), platform="slack")
    assert safety.rules_channel(auth, fake.model) == "C1"
    runtime = _runtime(fake, factory, _RULES)
    await _continue_turn_impl(runtime, auth, _SESSION, "continue the routine")
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_a_dm_never_inherits_its_source_channels_rules(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _deny_in_channel(factory, "C1")
    fake = FakeSession(tool="get_issue")
    fake.metadata[MA_METADATA_KEY_CHANNEL] = "C1"
    fake.metadata[MA_METADATA_KEY_PRIVATE_DM] = "dm-1"
    assert safety.rules_channel(_auth(), fake.model) is None


async def test_a_resumed_turn_keeps_the_channel_it_was_sent_under(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The resuming key's own (absent) channel never replaces the turn's."""
    await _deny_in_channel(factory, "C1")
    fake = FakeSession(tool="get_issue")
    await _record(factory, fake.pause(), channel="C1")
    runtime = _runtime(fake, factory, _RULES)
    _restart()
    await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_each_send_records_its_turn_and_channel(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    started = await _continue_turn_impl(
        runtime, _auth(bound="C9:1.2", platform="slack"), _SESSION, "hi"
    )
    async with factory() as db:
        recorded = await session_settle_state.unattended_turn(db, managed_session_id=_SESSION)
    assert recorded == session_settle_state.UnattendedTurn(
        turn_event_id=started["turn_event_id"], rules_channel_id="C9"
    )


@pytest.mark.parametrize(
    ("tool", "hints", "expected"),
    [
        ("memory_flag_list", None, "deny"),
        ("memory_flag_list", ToolAnnotations(read_only_hint=True), "allow"),
        ("memory_flag_list", ToolAnnotations(read_only_hint=True, destructive_hint=True), "deny"),
    ],
)
async def test_trusted_annotation_seam_feeds_the_unattended_verdict(
    tool: str,
    hints: ToolAnnotations | None,
    expected: str,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A noun-first read needs a trusted hint; a destructive hint wins over read-only."""
    looked_up: list[str] = []

    async def lookup(call: ToolCall) -> ToolAnnotations | None:
        looked_up.append(call.key)
        return hints

    monkeypatch.setattr(safety, "_annotations_for", lambda *_: lookup)
    fake = FakeSession(tool=tool)
    runtime = _runtime(
        fake, factory, ToolSafetyPolicy(enabled=True, trusted_annotations=("linear",))
    )
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)
    assert looked_up == [f"linear/{tool}"]
    assert [c["result"] for c in fake.confirmations] == [expected]


async def test_no_annotations_are_trusted_without_the_lookup(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing on this build answers for a server: a claimed read is still a write."""
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    assert safety._annotations_for(runtime, _auth(), fake.model) is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("entry", ["start", "continue", "resume"])
@pytest.mark.parametrize(
    ("trusted", "tool", "destructive", "expected"),
    [
        (True, "memory_flag_list", False, "allow"),
        (False, "create_issue", False, "deny"),
        (True, "memory_flag_list", True, "deny"),
    ],
)
async def test_session_annotations_over_fake_http(
    entry: str,
    trusted: bool,
    tool: str,
    destructive: bool,
    expected: str,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core import mcp_tool_annotations
    from daimon.core.mcp_oauth import discovery

    fake = FakeSession(script=[[("linear", tool, {})], [("linear", tool, {})]])
    runtime = _runtime(
        fake,
        factory,
        ToolSafetyPolicy(enabled=True, trusted_annotations=("linear",) if trusted else ()),
    )
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _LINEAR_URL
        assert request.headers.get("authorization") is None
        body = json.loads(request.content)
        seen.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(204)
        result = (
            {
                "tools": [
                    {
                        "name": tool,
                        "annotations": {"readOnlyHint": True, "destructiveHint": destructive},
                    }
                ]
            }
            if (body["method"] == "tools/list")
            else {}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original = httpx.AsyncClient

    class Client(original):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    async def catalogue(url: str, token: str | None) -> list[dict[str, Any]]:
        with monkeypatch.context() as scoped:
            scoped.setattr(httpx, "AsyncClient", Client)
            scoped.setattr(discovery, "assert_public_https_url", lambda *a, **k: None)
            return list(await discovery.read_mcp_catalogue(url, token))

    monkeypatch.setattr(mcp_tool_annotations, "read_mcp_catalogue", catalogue)
    if entry == "resume":
        boundary = fake.pause()
        await _record(factory, boundary)
        _restart()
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    elif entry == "start":
        await _start(runtime, fake, _auth())
    else:
        await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == [expected, expected]
    assert seen.count("tools/list") == int(trusted)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("trusted", [False, True])
def test_annotation_credentials_use_the_sessions_agent(
    enabled: bool, trusted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.turn import unattended

    fake = FakeSession()
    factory = async_sessionmaker[AsyncSession]()
    runtime = _runtime(
        fake,
        factory,
        ToolSafetyPolicy(enabled=enabled, trusted_annotations=("linear",) if trusted else ()),
    )
    token_builder = MagicMock()
    monkeypatch.setattr(unattended, "agent_mcp_token_for", token_builder)
    lookup_builder = MagicMock()
    monkeypatch.setattr(unattended, "mcp_annotation_lookup", lookup_builder)
    # An agent key can belong to another agent than the session being continued.
    auth = replace(_auth(), agent_id=uuid.uuid4())
    result = safety._annotations_for(runtime, auth, fake.model)  # pyright: ignore[reportPrivateUsage]
    if not enabled or not trusted:
        assert result is None
        token_builder.assert_not_called()
        lookup_builder.assert_not_called()
        return
    assert result is lookup_builder.return_value
    token_builder.assert_called_once_with(
        factory, fernet=runtime.fernet, tenant_id=_TENANT_ID, agent_id=_AGENT_UUID
    )
    lookup_builder.assert_called_once_with(
        server_urls={"linear": _LINEAR_URL}, token_for=token_builder.return_value, relays={}
    )


async def test_a_confirmation_is_gated_before_it_resumes_the_turn(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A config loosened (or a policy tightened) while the turn is paused reaches
    its next call: the sender gates before it confirms (finding 1)."""
    fake = FakeSession(script=[[("linear", "get_issue", {})], [("linear", "create_issue", {})]])
    fake.honor_permissions = True
    boundary = fake.pause()
    # While paused, the frozen config allows everything again.
    fake.tools = [_toolset("linear", "always_allow", "create_issue")]
    runtime = _runtime(fake, factory, _ENABLED)
    safety.watch_turn(runtime, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None)
    await _settle(runtime)
    # Without the gate before the first confirmation, create_issue would have run.
    assert "executed" not in fake.kinds()
    assert fake.kinds() == ["update", "confirm", "confirm"]
    assert [c["result"] for c in fake.confirmations] == ["allow", "deny"]


async def test_a_failed_gate_before_a_confirmation_sends_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_allow")
    await _record(factory, fake.pause())
    fake.fail_update = True
    runtime = _runtime(fake, factory, _ENABLED)
    safety.watch_turn(
        runtime,
        _auth(),
        _SESSION,
        turn_event_id=fake.events[0]["id"],
        rules_channel_id=None,
    )
    await _settle(runtime)
    assert fake.calls == []


async def test_a_throttled_message_is_not_resent_by_the_sdk(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2: one attempt; a retry is a new, fully checked call."""
    fake = FakeSession()
    fake.throttle = {"message": 1}
    runtime = _runtime(fake, factory, _ENABLED)
    with pytest.raises(Exception):  # noqa: B017 - the provider's 429
        await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    assert fake.attempts == {"message": 1}
    assert "message" not in fake.kinds()
    assert _no_workers()


async def test_a_throttled_confirmation_is_decided_again_on_resume(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The allow built before a rule change is never replayed by the SDK."""
    fake = FakeSession(frozen="always_ask", tool="get_issue")
    boundary = fake.pause()
    await _record(factory, boundary)
    fake.throttle = {"confirm": 1}
    runtime = _runtime(fake, factory, _RULES)
    await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert fake.attempts == {"confirm": 1} and fake.confirmations == []
    await _deny_in_channel(factory, "C1")
    async with factory.begin() as db:
        await session_settle_state.record_unattended_turn(
            db,
            managed_session_id=_SESSION,
            turn_event_id=boundary,
            rules_channel_id="C1",
            send=None,
        )
    await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_a_worker_on_an_older_turn_neither_reads_as_running_nor_blocks_resume(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 3: the worker's own boundary decides running and reuse."""
    fake = FakeSession(frozen="always_ask")
    runtime = _runtime(fake, factory, _ENABLED)
    stale = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id="evt_older_turn", rules_channel_id=None
    )
    newer = fake.pause()
    await _record(factory, newer)
    info = await _get_session_impl(runtime, _auth(), _SESSION)
    assert (info.status, info.hint) == ("paused", safety.PAUSED_HINT)
    started = await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert started["turn_event_id"] == newer
    assert stale.cancelled()
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_a_worker_on_an_older_turn_does_not_hide_a_newer_card(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    runtime = _runtime(fake, factory, _ENABLED)
    stale = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id="evt_older_turn", rules_channel_id=None
    )
    try:
        fake.pause()
        info = await _get_session_impl(runtime, _auth(), _SESSION)
        assert (info.status, info.hint) == ("paused", safety.AWAITING_CARD_HINT)
    finally:
        stale.cancel()
    assert fake.calls == []


async def test_a_lost_turn_record_leaves_the_pause_unresumable(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write fails after an accepted send: the sender still answers; after a
    restart nobody does (fail closed)."""

    async def refuse(*_a: object, **_k: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(session_settle_state, "record_unattended_turn", refuse)
    fake = FakeSession(frozen="always_ask")
    runtime = _runtime(fake, factory, _ENABLED)
    # The sender's worker is stopped before it answers: the process died.
    with patch.object(safety, "_answer", AsyncMock(side_effect=RuntimeError("crash"))):
        await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
        await _settle(runtime)
    _restart()
    with pytest.raises(ToolError, match="approval in the chat"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert fake.confirmations == []


async def test_another_sessions_record_does_not_authorize_this_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    boundary = fake.pause()
    async with factory.begin() as db:
        await session_settle_state.record_unattended_turn(
            db,
            managed_session_id="ses_elsewhere",
            turn_event_id=boundary,
            rules_channel_id=None,
            send=None,
        )
    runtime = _runtime(fake, factory, _DISABLED)
    with pytest.raises(ToolError, match="approval in the chat"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert fake.confirmations == []


async def test_a_resume_by_a_key_bound_elsewhere_keeps_the_turns_channel(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _deny_in_channel(factory, "C1")
    fake = FakeSession(tool="get_issue")
    await _record(factory, fake.pause(), channel="C1")
    runtime = _runtime(fake, factory, _RULES)
    _restart()
    await _continue_turn_impl(runtime, _auth(bound="C2", platform="discord"), _SESSION, None)
    await _settle(runtime)
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_a_channel_ask_beats_an_agent_wide_allow_for_routines(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory.begin() as db:
        await make_tenant(db, id=_TENANT_ID)
    admin = Subject(is_admin=True, platform_user_id="UA")
    for channel, rule, scope in ((None, "allow", "chat_and_routines"), ("C1", "ask", "chat")):
        async with factory.begin() as db:
            await agent_tool_rules.set_rule(
                db,
                tenant_id=_TENANT_ID,
                agent_id=_MA_AGENT_ID,
                agent_name="safety",
                tool_key="linear/create_issue",
                channel_id=channel,
                server_url=_LINEAR_URL,
                rule=rule,
                scope=scope,
                set_by="admin",
                actor=admin,
            )
    for auth, expected in ((_auth(), "allow"), (_auth(bound="C1", platform="discord"), "deny")):
        fake = FakeSession()
        runtime = _runtime(fake, factory, _RULES)
        await _continue_turn_impl(runtime, auth, _SESSION, "hi")
        await _settle(runtime)
        assert [c["result"] for c in fake.confirmations] == [expected]


@pytest.mark.parametrize("revocation", ["membership", "allowlist", "agent_role"])
async def test_revocation_between_confirmations_leaves_the_second_unanswered(
    factory: async_sessionmaker[AsyncSession], revocation: str
) -> None:
    async with factory.begin() as db:
        await make_tenant(db, id=_TENANT_ID, platform="discord", workspace_id="g1")
        await set_access_policy(
            db, tenant_id=_TENANT_ID, policy=TenantAccessPolicy(invoker_user_ids=("u1",))
        )
    from daimon.core.access_policy import AgentRule

    if revocation == "agent_role":
        async with factory.begin() as db:
            await set_access_policy(
                db,
                tenant_id=_TENANT_ID,
                policy=TenantAccessPolicy(
                    agent_rules={ma_agent().name: AgentRule(invoker_role_ids=("123",))}
                ),
            )
    fake = FakeSession(script=[[("linear", "get_issue", {})], [("linear", "get_issue", {})]])
    member = True
    roles = ("123",)

    async def fetch(tenant_id: uuid.UUID, guild_id: str, user_id: str) -> Membership:
        return Membership(is_member=member, role_ids=roles)

    runtime = replace(
        _runtime(fake, factory, _ENABLED), membership_resolver=CachedMembershipResolver(fetch)
    )
    auth = replace(_auth(platform="discord"), platform_user_id="u1")
    boundary = fake.pause()

    # Change admission after the first confirmation, before the next answer.
    original = safety._answer  # pyright: ignore[reportPrivateUsage]

    async def answer(*args: Any, **kwargs: Any) -> None:
        nonlocal member, roles
        await original(*args, **kwargs)
        if revocation == "membership":
            member = False
        elif revocation == "agent_role":
            roles = ()
        else:
            async with factory.begin() as db:
                await set_access_policy(
                    db,
                    tenant_id=_TENANT_ID,
                    policy=TenantAccessPolicy(invoker_user_ids=("someone-else",)),
                )

    with (
        patch.object(safety, "_answer", answer),
        patch.object(
            agent_chat, "_resolve_ma_agent", AsyncMock(return_value=ma_agent(id=_MA_AGENT_ID))
        ),
    ):
        worker = safety.watch_turn(
            runtime, auth, _SESSION, turn_event_id=boundary, rules_channel_id=None
        )
        assert await asyncio.wait_for(worker, timeout=5) is False
    assert [c["result"] for c in fake.confirmations] == ["allow"]


async def test_the_worker_runs_to_the_unattended_run_limit_at_its_place(
    monkeypatch: pytest.MonkeyPatch, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Nobody watches an agent-chat turn: its worker follows the unattended run limit."""
    from daimon.core.features.service import set_workspace_feature

    async with factory.begin() as db:
        await make_tenant(db, id=_TENANT_ID)
        await set_workspace_feature(
            db,
            None,
            tenant_id=_TENANT_ID,
            platform="discord",
            key="unattended_time_limit",
            value="30",
            account_id=None,
            surface="cli",
        )
    real = safety.unattended_ceiling_s
    read: list[float] = []

    async def bounded(*args: Any, **kwargs: Any) -> float:
        read.append(await real(*args, **kwargs))
        return 0.05

    monkeypatch.setattr(safety, "unattended_ceiling_s", bounded)
    fake = FakeSession(frozen="always_ask")
    fake.pause()
    runtime = _runtime(fake, factory, _DISABLED)
    worker = safety.watch_turn(
        runtime,
        _auth(platform="discord"),
        _SESSION,
        turn_event_id="evt_never_listed",
        rules_channel_id="C1",
    )
    assert await asyncio.wait_for(worker, timeout=5) is False, "the worker stops at its limit"
    assert read == [30 * 60.0], "the worker reads the tenant's unattended run limit"
    assert safety.why_stopped(runtime, _SESSION) == safety.RUN_LIMIT_MSG


async def _cloud_computers(factory: async_sessionmaker[AsyncSession], agent_chat: str) -> None:
    """Cloud computers on for the workspace, and cloud computers from agent chat `agent_chat`."""
    async with factory.begin() as db:
        await make_tenant(db, id=_TENANT_ID)
        for key, value in (("compute", "on"), ("compute_agent_chat", agent_chat)):
            await set_feature_value(
                db, tenant_id=_TENANT_ID, target=FeatureTarget("workspace"), key=key,
                value=value, set_by=None, surface="cli",
            )  # fmt: skip


@pytest.mark.parametrize("policy", [_ENABLED, _DISABLED], ids=["enabled", "disabled"])
@pytest.mark.parametrize(
    ("caller", "switch", "expected"),
    [
        ("owner", "on", "allow"),
        ("owner", "off", "deny"),
        ("no_grant", "on", "deny"),
        ("no_platform_user", "on", "deny"),
    ],
)
async def test_an_agent_chat_owner_starts_a_cloud_computer_with_nobody_watching(
    policy: ToolSafetyPolicy,
    factory: async_sessionmaker[AsyncSession],
    caller: str,
    switch: str,
    expected: str,
) -> None:
    """Only for a person's account, on a session stamped with its credential's grant, with
    the switch on. Off, the worker answers exactly as before: nobody is here to press its
    card. A session of another account is not driven at all (`require_session_driver`)."""
    calls: list[Call] = [
        ("daimon-mcp", "compute_launch_session", {"template": "mmm"}),
        ("daimon-mcp", "compute_extend_session", {"session_id": "s", "hours": 1}),
        ("daimon-mcp", "create_watch", {CONFIRMED_DAIMON_TOOLS["create_watch"] or "": "x"}),
    ]
    fake = FakeSession(script=[calls]).with_daimon()
    fake.metadata[MA_METADATA_KEY_CHAT_SESSION] = "grant-safety"
    await _record(factory, fake.pause())
    await _cloud_computers(factory, switch)
    runtime = _runtime(fake, factory, policy, public_url=_PUBLIC)
    auth = dataclasses.replace(_auth(platform="discord"), platform_user_id="u1")
    if caller == "no_grant":
        del fake.metadata[MA_METADATA_KEY_CHAT_SESSION]
    if caller == "no_platform_user":
        auth = _auth(platform="discord")
    _restart()
    await _continue_turn_impl(runtime, auth, _SESSION, None)
    await _settle(runtime)
    answers = {c["tool_use_id"]: c["result"] for c in fake.confirmations}
    assert list(answers.values()) == [expected, expected, "deny"], "a watch still needs its card"


async def test_a_signed_in_resume_records_where_an_older_session_came_from(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session made before provenance was kept has no record of it. A person's resume,
    with no message, copies it from the session's stamp and changes nothing else the
    driver record holds."""
    fake = FakeSession().with_daimon()
    fake.metadata[MA_METADATA_KEY_CHAT_SESSION] = "grant-older"
    await _record(factory, fake.pause())

    async def stored() -> tuple[object, bool, object]:
        async with factory() as db:
            return (
                await session_settle_state.count_agent_chat_sends(db, managed_session_id=_SESSION),
                await session_settle_state.is_agent_chat_signed_in(db, managed_session_id=_SESSION),
                await session_settle_state.find_agent_chat_session(
                    db, grant="grant-older", account_id=_OWNER
                ),
            )

    sends, signed_in, found = await stored()
    assert found is None, "no send of this release recorded it"
    runtime = _runtime(fake, factory, _ENABLED, public_url=_PUBLIC)
    _restart()
    person = dataclasses.replace(_auth(), signed_in_person=True)
    await _continue_turn_impl(runtime, person, _SESSION, None)
    await _settle(runtime)
    assert await stored() == (
        sends,
        signed_in,
        session_settle_state.AgentChatSession(managed_session_id=_SESSION, channel_id=None),
    )


# --- Waiting on the event stream: the worker, `ask` and `await_turn` ---


async def _eventually(check: Callable[[], bool]) -> None:
    """Yield to the loop until `check` holds; fails the test after about two seconds."""
    for _ in range(400):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the awaited state never came")


async def _reads_while_quiet(fake: FakeSession) -> int:
    """How many session reads happen over a stretch many times the old poll gap, once
    the read that follows the subscription is over."""
    await _eventually(lambda: fake.opened > 0 and fake.reads > 0)
    settled = -1
    while settled != fake.reads:
        settled = fake.reads
        await asyncio.sleep(0.03)
    await asyncio.sleep(0.1)
    return fake.reads - settled


def _running_turn(fake: FakeSession) -> str:
    """A turn the agent is working on, with a live stream; returns its boundary."""
    fake.live = True
    fake.status = "running"
    return fake._start_turn()  # pyright: ignore[reportPrivateUsage]


def _say(fake: FakeSession, text: str) -> str:
    event_id = fake._id("evt_msg")  # pyright: ignore[reportPrivateUsage]
    fake.events.append(
        {"id": event_id, "type": "agent.message", "content": [{"type": "text", "text": text}]}
    )
    return event_id


def _finish(fake: FakeSession) -> None:
    fake.status = "idle"
    fake._end()  # pyright: ignore[reportPrivateUsage]


async def test_the_worker_sleeps_on_the_event_stream_and_reads_only_on_news(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Issue 1894: no poll. A running turn costs no reads until the stream says it stopped."""
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    boundary = _running_turn(fake)

    worker = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None
    )
    assert await _reads_while_quiet(fake) == 0

    fake.status = "idle"
    _pause_now(fake)
    assert await asyncio.wait_for(worker, timeout=5) is True
    assert [c["result"] for c in fake.confirmations] == ["allow"]
    assert fake.opened == 1, "one subscription served the whole turn"


def _pause_now(fake: FakeSession) -> None:
    fake._pause_on(fake._remaining.pop(0))  # pyright: ignore[reportPrivateUsage]


async def test_the_worker_answers_a_pause_the_status_read_does_not_show(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Staging, 2026-11-26: right after the idle event the session's status still said
    running, the worker read it once and went back to waiting for an event that never
    came. Where a turn stands is read from its events."""
    fake = FakeSession(script=[[("linear", "get_issue", {})], [("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    boundary = _running_turn(fake)

    worker = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None
    )
    await _reads_while_quiet(fake)
    _pause_now(fake)  # the status stays "running" through both pauses and the end
    assert await asyncio.wait_for(worker, timeout=5) is True
    assert [c["result"] for c in fake.confirmations] == ["allow", "allow"]


async def test_the_worker_waits_for_the_event_list_to_show_the_pause(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The list can trail the stream: the state is read once it shows the event."""
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    boundary = _running_turn(fake)

    worker = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None
    )
    await _reads_while_quiet(fake)
    fake.list_trails(3)
    fake.status = "idle"
    _pause_now(fake)
    assert await asyncio.wait_for(worker, timeout=5) is True
    assert [c["result"] for c in fake.confirmations] == ["allow"]


async def test_the_worker_finds_a_pause_no_stream_event_brought(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream only saves reads. A missed event costs one recheck, never the turn."""
    monkeypatch.setattr(safety, "RECHECK_S", 0.05)
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    boundary = _running_turn(fake)

    worker = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None
    )
    await _eventually(lambda: fake.opened == 1)
    fake.status = "idle"
    with fake.events.unheard():
        _pause_now(fake)
    await _eventually(lambda: len(fake.confirmations) == 1)
    assert await asyncio.wait_for(worker, timeout=5) is True
    assert fake.opened == 1, "a recheck reads the state; it does not reopen the stream"


async def test_await_turn_finds_an_end_no_stream_event_brought(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_chat, "RECHECK_S", 0.05)
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, _running_turn(fake))

    waiting = asyncio.create_task(_await_turn_impl(runtime, _auth(), _SESSION))
    await _eventually(lambda: fake.opened == 1)
    with fake.events.unheard():
        _finish(fake)
    news = await asyncio.wait_for(waiting, timeout=5)

    assert (news.status, news.messages) == ("idle", ("done",))


async def test_ask_returns_the_answer_when_the_status_read_trails_the_end(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stream says the turn ended while the status still says running: `ask`
    reads again at once instead of sleeping to its next recheck."""
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    fake.live = True
    fake.on_confirm = lambda: setattr(fake, "stale_status_reads", 2)
    runtime = _runtime(fake, factory, _ENABLED)

    result = await asyncio.wait_for(_ask_impl(runtime, _auth(), "hi", handle=_SESSION), timeout=5)

    assert result.message == "done"
    assert fake.stale_status_reads == 0, "the trailing status was read through"


@pytest.mark.parametrize("status", ["running", "idle"])
async def test_a_pause_whose_calls_are_all_confirmed_reads_as_running(
    factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    """The confirmation is in and the running event is not listed yet. Nothing waits
    for anybody, so no read says paused and no caller is told to resume. The turn is
    not over either, whatever the status still says: a read that said idle would let
    `ask` take a progress line for the final answer."""
    fake = FakeSession(frozen="always_ask")
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, fake.pause())
    for tool_use_id in fake._pending():  # pyright: ignore[reportPrivateUsage]
        fake.events.append(
            {"id": "evt_confirmed", "type": "user.tool_confirmation", "tool_use_id": tool_use_id}
        )
    fake.status = status

    info = await _get_session_impl(runtime, _auth(), _SESSION)

    assert (info.status, info.hint) == ("running", None)
    # Nothing is left to resume, and nothing is answered twice.
    with pytest.raises(ToolError, match="message is required"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert fake.confirmations == [] and _no_workers()


class _BusyStream:
    """A stream on which an event outside every caller's `kinds` is always ready."""

    def __init__(self) -> None:
        self.read = 0

    async def __anext__(self) -> Any:
        self.read += 1
        await asyncio.sleep(0)
        return MagicMock(native=MagicMock(type="span.model_request_end", id=f"evt_{self.read}"))

    async def close(self) -> None:
        return None


async def test_a_busy_stream_does_not_carry_a_wait_past_its_time_or_its_worker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The recheck must come on time while events it does not wait for keep arriving."""
    feed = safety.TurnFeed(_runtime(FakeSession(), factory, _ENABLED), _auth(), _SESSION)
    stream = _BusyStream()
    feed._stream = stream  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    woke = await asyncio.wait_for(feed.next(safety.TURN_STOPPED, within=0.05), timeout=5)
    assert woke is None and stream.read > 1

    finished: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    finished.set_result(False)
    before = stream.read
    woke = await asyncio.wait_for(
        feed.next(safety.TURN_STOPPED, within=60, or_done=finished), timeout=5
    )
    assert woke is None and stream.read - before <= 2, "a finished worker ends the wait at once"
    await feed.close()


async def test_a_dropped_stream_is_reopened_and_the_pause_still_answered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    boundary = _running_turn(fake)

    worker = safety.watch_turn(
        runtime, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None
    )
    await _eventually(lambda: fake.opened == 1)
    fake.events.drop()
    await _eventually(lambda: fake.opened == 2)
    # Paused while no stream delivered it: the read after the reopen finds it.
    fake.status = "idle"
    _pause_now(fake)
    assert await asyncio.wait_for(worker, timeout=5) is True
    assert [c["result"] for c in fake.confirmations] == ["allow"]


async def _role_gated(factory: async_sessionmaker[AsyncSession]) -> None:
    from daimon.core.access_policy import AgentRule

    async with factory.begin() as db:
        await make_tenant(db, id=_TENANT_ID, platform="discord", workspace_id="g1")
        await set_access_policy(
            db,
            tenant_id=_TENANT_ID,
            policy=TenantAccessPolicy(
                agent_rules={ma_agent().name: AgentRule(invoker_role_ids=("123",))}
            ),
        )


@pytest.mark.parametrize(
    ("unanswered", "ends", "confirmed"),
    [(3, True, ["allow"]), (10_000, False, [])],
    ids=["answers-on-a-later-try", "gives-up-and-says-why"],
)
async def test_a_membership_check_discord_does_not_answer_is_tried_again(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    unanswered: int,
    ends: bool,
    confirmed: list[str],
) -> None:
    """Production, 2026-11-26: one unanswered check ended the worker and left a code
    review paused three times. Unknown is not a refusal: the same pause is tried again."""
    monkeypatch.setattr(safety, "_UNANSWERED_WAITS_S", (0.0, 0.0, 0.0))
    await _role_gated(factory)
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    calls = 0

    async def fetch(tenant_id: uuid.UUID, guild_id: str, user_id: str) -> Membership:
        nonlocal calls
        calls += 1
        if calls <= unanswered:
            raise httpx.ReadTimeout("discord did not answer")
        return Membership(is_member=True, role_ids=("123",))

    runtime = replace(
        _runtime(fake, factory, _ENABLED), membership_resolver=CachedMembershipResolver(fetch)
    )
    auth = replace(_auth(platform="discord"), platform_user_id="u1")
    boundary = fake.pause()

    with patch.object(
        agent_chat, "_resolve_ma_agent", AsyncMock(return_value=ma_agent(id=_MA_AGENT_ID))
    ):
        worker = safety.watch_turn(
            runtime, auth, _SESSION, turn_event_id=boundary, rules_channel_id=None
        )
        assert await asyncio.wait_for(worker, timeout=5) is ends
    assert [c["result"] for c in fake.confirmations] == confirmed
    why = safety.why_stopped(runtime, _SESSION)
    assert (why is None) is ends
    if why is not None:
        assert "Discord didn't answer" in why and "list of people" not in why, why


async def test_a_pause_another_worker_answered_first_is_not_a_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two instances may hold a worker for one turn after a resume. The slower one's
    confirmation is refused; its calls read as confirmed, so it carries on."""
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    boundary = fake.pause()
    original = safety._answer  # pyright: ignore[reportPrivateUsage]

    async def answer(*args: Any, **kwargs: Any) -> None:
        # The other worker's confirmation lands, and the turn runs on to its end.
        pending = fake._pending()  # pyright: ignore[reportPrivateUsage]
        for tool_use_id in pending:
            fake.events.append(
                {
                    "id": fake._id("evt_confirm"),  # pyright: ignore[reportPrivateUsage]
                    "type": "user.tool_confirmation",
                    "tool_use_id": tool_use_id,
                    "result": "allow",
                }
            )
        fake._end()  # pyright: ignore[reportPrivateUsage]
        fake.fail_confirm = True
        await original(*args, **kwargs)

    with patch.object(safety, "_answer", answer):
        worker = safety.watch_turn(
            runtime, _auth(), _SESSION, turn_event_id=boundary, rules_channel_id=None
        )
        assert await asyncio.wait_for(worker, timeout=5) is True
    assert fake.confirmations == [], "this worker's own confirmation was refused"


async def test_await_turn_returns_the_final_answer_of_a_finished_turn(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    await _start(runtime, fake, _auth())
    await _settle(runtime)

    news = await _await_turn_impl(runtime, _auth(), _SESSION)

    assert (news.status, news.messages, news.hint) == ("idle", ("done",), None)
    assert news.cursor == next(e["id"] for e in fake.events if e["type"] == "agent.message")
    # Asked again with that cursor, the final answer is still the last message.
    again = await _await_turn_impl(runtime, _auth(), _SESSION, after=news.cursor)
    assert (again.status, again.messages) == ("idle", ("done",))


async def test_await_turn_returns_on_what_the_agent_says_then_on_the_end(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A long poll: each call returns on news, and the cursor skips what was returned."""
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, _running_turn(fake))

    waiting = asyncio.create_task(_await_turn_impl(runtime, _auth(), _SESSION))
    assert await _reads_while_quiet(fake) == 0, "a wait reads nothing until there is news"
    assert not waiting.done()
    first = _say(fake, "Step 1 of 3 done")
    news = await asyncio.wait_for(waiting, timeout=5)
    assert (news.status, news.messages, news.cursor) == ("running", ("Step 1 of 3 done",), first)
    assert news.hint is not None and "await_turn" in news.hint

    waiting = asyncio.create_task(_await_turn_impl(runtime, _auth(), _SESSION, after=first))
    await _eventually(lambda: fake.opened == 2)
    assert not waiting.done(), "what the cursor already covers is not news"
    _finish(fake)
    news = await asyncio.wait_for(waiting, timeout=5)
    assert (news.status, news.messages) == ("idle", ("done",))


async def test_await_turn_returns_still_running_when_its_bound_passes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, _running_turn(fake))

    news = await _await_turn_impl(runtime, _auth(), _SESSION, wait_seconds=0.05)

    assert (news.status, news.messages, news.cursor) == ("running", (), None)
    assert news.hint is not None and "await_turn" in news.hint
    # No wait at all reads where the turn stands and returns.
    now = await asyncio.wait_for(
        _await_turn_impl(runtime, _auth(), _SESSION, wait_seconds=0), timeout=5
    )
    assert now.status == "running"


async def test_await_turn_names_the_tool_call_running_now(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, _running_turn(fake))
    fake.events.append(
        {
            "id": "evt_tool_now",
            "type": "agent.mcp_tool_use",
            "mcp_server_name": "github",
            "name": "get_pull_request",
            "input": {},
        }
    )

    news = await _await_turn_impl(runtime, _auth(), _SESSION, wait_seconds=0)

    assert news.activity is not None and news.activity.startswith("github get_pull_request")


async def test_await_turn_does_not_take_the_turn_before_for_the_one_just_sent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Right after a send the list still ends at the earlier turn: its idle and its
    reply are not this turn's (`_newest_turn`)."""
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    await _start(runtime, fake, _auth())
    await _settle(runtime)
    await _record(factory, "evt_sent_but_not_listed_yet")

    news = await _await_turn_impl(runtime, _auth(), _SESSION, wait_seconds=0)

    assert (news.status, news.messages) == ("running", ())


async def test_await_turn_gives_a_queued_message_none_of_the_turn_before(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A message sent while a turn runs is listed before that turn's last words. They
    were said before it was taken up, so they are not its progress and not its end."""
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    _running_turn(fake)
    fake.events.append(
        {
            "id": "evt_queued",
            "type": "user.message",
            "content": [{"type": "text", "text": "and then?"}],
            "processed_at": None,
        }
    )
    _say(fake, "the answer of the turn before")
    _finish(fake)
    await _record(factory, "evt_queued")

    news = await _await_turn_impl(runtime, _auth(), _SESSION, wait_seconds=0)

    assert (news.status, news.messages, news.cursor) == ("running", (), None)


async def test_await_turn_with_no_wait_only_reads(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`wait_seconds=0` is a status read: no stream, no resume, no tool call answered."""
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, fake.pause())
    _restart()
    resume = AsyncMock()

    news = await _await_turn_impl(runtime, _auth(), _SESSION, wait_seconds=0, resume=resume)

    assert news.status == "paused" and news.hint is not None and "await_turn" in news.hint
    resume.assert_not_awaited()
    assert (fake.opened, fake.calls) == (0, [])
    assert _no_workers()


async def test_await_turn_returns_inside_its_bound_when_the_stream_will_not_open(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound covers the whole call, the subscription and the reads included."""
    monkeypatch.setattr(agent_chat, "_AWAIT_GRACE_S", 0.05)
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, _running_turn(fake))
    closed = False

    class StuckFeed:
        async def open(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            nonlocal closed
            closed = True

    news = await asyncio.wait_for(
        _await_turn_impl(runtime, _auth(), _SESSION, wait_seconds=0.05, feed=StuckFeed()),  # type: ignore[arg-type]
        timeout=5,
    )

    assert (news.status, news.messages) == ("running", ())
    assert news.hint is not None and "await_turn" in news.hint
    assert closed


async def test_await_turn_resumes_a_turn_paused_by_a_restart_and_returns_its_answer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The caller that waits keeps the turn going: no `get_session` loop, no `continue_turn`."""
    fake = FakeSession(script=[[("linear", "get_issue", {})], [("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, fake.pause())
    _restart()
    resumes = 0

    async def resume() -> None:
        nonlocal resumes
        resumes += 1
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)

    news = await asyncio.wait_for(
        _await_turn_impl(runtime, _auth(), _SESSION, resume=resume), timeout=5
    )

    assert (news.status, news.messages) == ("idle", ("done",))
    assert [c["result"] for c in fake.confirmations] == ["allow", "allow"]
    assert resumes == 1


@pytest.mark.parametrize("failure", ["resume-refused", "stops-again"])
async def test_await_turn_says_why_a_paused_turn_cannot_go_on(
    factory: async_sessionmaker[AsyncSession], failure: str
) -> None:
    fake = FakeSession(script=[[("linear", "get_issue", {})]])
    runtime = _runtime(fake, factory, _ENABLED)
    await _record(factory, fake.pause())
    _restart()

    async def resume() -> None:
        if failure == "resume-refused":
            raise ToolError("this conversation's channel has used its spending budget.")
        fake.fail_confirm = True
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)

    news = await asyncio.wait_for(
        _await_turn_impl(runtime, _auth(), _SESSION, resume=resume), timeout=5
    )

    assert news.status == "paused" and news.hint is not None
    expected = "spending budget" if failure == "resume-refused" else "could not decide a tool call"
    assert expected in news.hint, news.hint
    assert fake.confirmations == []


async def test_await_turn_leaves_a_chat_approval_card_to_the_person_it_asks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    runtime = _runtime(fake, factory, _ENABLED)
    fake.pause()
    resume = AsyncMock()

    news = await _await_turn_impl(runtime, _auth(), _SESSION, resume=resume)

    assert (news.status, news.hint) == ("paused", safety.AWAITING_CARD_HINT)
    resume.assert_not_awaited()
    assert fake.calls == []


async def test_await_turn_reports_a_terminated_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    fake.pause()
    fake.status = "terminated"

    news = await _await_turn_impl(runtime, _auth(), _SESSION)

    assert news.status == "terminated" and news.hint is not None and "start_turn" in news.hint


def _await_turn_tools() -> dict[str, ast.AsyncFunctionDef]:
    """Every registered `await_turn`, by the module that registers it, found from the
    code: any tools module that registers `start_turn` is a surface that drives turns."""
    found: dict[str, ast.AsyncFunctionDef] = {}
    tools = Path(agent_chat.__file__).parent
    for path in sorted(tools.glob("*.py")):
        defs = {
            node.name: node
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.AsyncFunctionDef)
        }
        if "start_turn" in defs:
            assert "await_turn" in defs, f"{path.name} starts turns but cannot wait for one"
            found[path.name] = defs["await_turn"]
    return found


#: What the `await_turn` description promises, and the test that proves each promise.
AWAIT_TURN_CLAIMS: dict[str, str] = {
    "said something": "test_await_turn_returns_on_what_the_agent_says_then_on_the_end",
    "the turn ended": "test_await_turn_returns_the_final_answer_of_a_finished_turn",
    "stopped and cannot go on": "test_await_turn_says_why_a_paused_turn_cannot_go_on",
    "the conversation is over": "test_await_turn_reports_a_terminated_session",
    "``wait_seconds`` passed": "test_await_turn_returns_still_running_when_its_bound_passes",
    "is resumed by": "test_await_turn_resumes_a_turn_paused_by_a_restart_and_returns_its_answer",
    "``activity``": "test_await_turn_names_the_tool_call_running_now",
    "without polling": "test_await_turn_returns_on_what_the_agent_says_then_on_the_end",
    "resumes nothing": "test_await_turn_with_no_wait_only_reads",
}


def test_every_surface_that_starts_turns_waits_for_them_the_same_way() -> None:
    """One rule, every path: agent keys and the hub register the same wait, each with a
    resume, and each description makes only the promises a test here proves."""
    tools = _await_turn_tools()
    assert set(tools) == {"agent_chat.py", "hub.py"}, sorted(tools)
    for module, tool in tools.items():
        calls = [
            node
            for node in ast.walk(tool)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_await_turn_impl"
        ]
        assert len(calls) == 1, f"{module}: await_turn must use the one implementation"
        assert "resume" in {kw.arg for kw in calls[0].keywords}, f"{module}: no resume"
        doc = " ".join((ast.get_docstring(tool) or "").split())
        for claim, test in AWAIT_TURN_CLAIMS.items():
            assert claim in doc, f"{module}: await_turn no longer says {claim!r}"
            assert callable(globals().get(test)), f"{claim!r} has no test named {test}"
