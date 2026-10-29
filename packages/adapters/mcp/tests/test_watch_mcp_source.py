"""Watches on a listed read tool of an attached MCP server (I3, I4, I8, I9).

The fake server is the real preview parity fixture, served in process. Its
`fixture_mislabeled_write` says it only reads and writes anyway: the registry,
never the hint, is what allows a poll.
"""

# ruff: noqa: F811

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp import watch_polling
from daimon.adapters.mcp.tools.watches import create_watch_impl
from daimon.adapters.mcp.watch_polling import (
    SourceBusy,
    UrlBudget,
    attached_server,
    operator_denied,
    run_poll,
)
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_tool_call import McpCallError, McpToolSession, mcp_tool_session
from daimon.core.stores import watches
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing import ma_agent
from daimon.testing.asgi import INIT_BODY, asgi_lifespan
from daimon.testing.preview_parity_mcp import build_parity_host
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from .test_watch_polling import attended_owner, runtime_watch  # noqa: F401

pytestmark = pytest.mark.fresh_schema

TOKEN = "watch-bearer-sentinel-" + "q" * 40
BRIDGE = "https://parity.example/mcp"
OTHER = "https://elsewhere.example/mcp"
PINNED = "93.184.216.34"
OS_URL = "https://os.example/mcp"


def entry(tool: str, **extra: object) -> dict[str, object]:
    return {
        "server": BRIDGE,
        "tool": tool,
        "credential": "agent",
        "returns_chat": False,
        "max_interval_floor_s": 60,
        **extra,
    }


NATIVE = [
    {"server": "daimon-mcp", "tool": tool, "credential": "none", "returns_chat": True}
    for tool in ("read_thread", "read_channel")
]


def toolset(name: str, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "type": "mcp_toolset",
        "mcp_server_name": name,
        "configs": [],
        "default_config": {"enabled": enabled, "permission_policy": {"type": "always_ask"}},
    }


def agent_with(*servers: tuple[str, str], enabled: bool = True):
    return ma_agent(
        id="agent-bound",
        name="analyst",
        mcp_servers=[
            {"type": "url", "name": "daimon-mcp", "url": OS_URL},
            *({"type": "url", "name": name, "url": url} for name, url in servers),
        ],
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "configs": [],
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
            },
            *(toolset(name, enabled=enabled) for name, _ in servers),
        ],
    )


class Recording(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


async def pinned(host: str, port: int) -> str:
    assert host == "parity.example"
    return PINNED


@pytest.fixture
async def bridge(attended_owner, monkeypatch, tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    runtime, native, auth, origin = attended_owner
    monkeypatch.setenv(
        "DAIMON_WATCHES__POLLABLE_TOOLS",
        json.dumps([*NATIVE, entry("fixture_counter_read"), entry("lead_fixture_submit")]),
    )
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    runtime.fernet = fernet
    runtime.settings.tool_safety = ToolSafetyPolicy()
    await save_agent_mcp_credential(
        sessionmaker=runtime.session_factory,
        fernet=fernet,
        tenant_id=native.tenant_id,
        agent_id=derive_agent_uuid(tenant_id=native.tenant_id, ma_agent_id=native.agent_id),
        mcp_server_url=BRIDGE,
        plaintext_token=TOKEN,
    )
    app = build_parity_host(token=TOKEN, journal_path=tmp_path / "calls.jsonl")
    transport = Recording(httpx.ASGITransport(app=app))
    monkeypatch.setattr(
        watch_polling, "open_source", partial(mcp_tool_session, resolve=pinned, transport=transport)
    )
    agent = AsyncMock(return_value=agent_with(("parity-bridge", BRIDGE)))
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.retrieve_agent", agent)
    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.retrieve_agent", agent)
    # A fresh per-URL budget per test.
    monkeypatch.setattr(watch_polling, "SOURCE_BUDGET", UrlBudget())
    async with (
        asgi_lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qa",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {TOKEN}",
            },
        ) as owner_side,
    ):
        assert (await owner_side.post("/mcp", json=INIT_BODY)).status_code == 200

        async def submit(marker: str) -> None:
            response = await owner_side.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "lead_fixture_submit", "arguments": {"marker": marker}},
                },
            )
            assert response.status_code == 200

        async def journal() -> dict[str, Any]:
            return (await owner_side.get("/calls")).json()

        yield SimpleNamespace(
            runtime=runtime,
            native=native,
            auth=auth,
            origin=origin,
            transport=transport,
            agent=agent,
            submit=submit,
            journal=journal,
        )


async def lease(runtime: Any, watch: Any) -> str:
    """Make `watch` due and return its own lease; the fixture's native watch may be due too."""
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET next_check_at=now()-interval '1 second' WHERE id=:id"),
            {"id": watch.id},
        )
        claimed = await watches.claim_poll_watches(db)
    return next(item["lease"] for item in claimed if item["watch_id"] == str(watch.id))


async def create(fx: SimpleNamespace, tool: str = "fixture_counter_read", **kwargs: Any):
    return await create_watch_impl(
        fx.runtime,
        fx.auth,
        origin_context_id=fx.origin,
        instructions="Report new markers.",
        source="poll",
        server=kwargs.pop("server", "parity-bridge"),
        tool=tool,
        arguments=kwargs.pop("arguments", {}),
        projection=kwargs.pop("projection", ".markers"),
        rule=kwargs.pop("rule", "added"),
        interval=60,
        **kwargs,
    )


async def get(fx: SimpleNamespace, watch_id: uuid.UUID):
    async with fx.runtime.session_factory() as db:
        return await watches.get_watch(db, watch_id)


def executed(calls: dict[str, Any], tool: str) -> int:
    return sum(c["phase"] == "executed" and c["tool"] == tool for c in calls["calls"])


async def test_a_listed_read_fires_once_per_new_marker_and_quotes_it_as_evidence(bridge):
    fx = bridge
    created = await create(fx)
    assert created.source == "mcp_poll" and created.source_host == "parity.example"
    watch = await get(fx, created.watch_id)
    # The canonical URL is stored and checked, never the name the owner typed.
    assert watch.source_state["server_url"] == BRIDGE
    assert watch.source_state["server_name_at_enrollment"] == "parity-bridge"
    assert watch.source_state["credential"] == "agent"
    assert len(watch.source_state["catalogue_digest"]) == 64
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    async with fx.runtime.session_factory() as db:
        assert await watches.list_evidence(db, watch.id) == []
    await fx.submit("marker-alpha")
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    async with fx.runtime.session_factory() as db:
        evidence = await watches.list_evidence(db, watch.id)
        assert len(evidence) == 1 and "marker-alpha" in evidence[0].payload
        wake = await get_continuation(db, idempotency_key=evidence[0].wake_key)
        seed = await watches.watch_seed(db, wake, now=datetime.now(UTC))
    assert seed is not None and 'source="os_poll_mcp"' in seed and "untrusted" in seed
    calls = await fx.journal()
    # The poller only ever ran the listed read; the one write is the owner's own.
    # Three polls; reading the listing at create runs nothing.
    assert executed(calls, "fixture_counter_read") == 3
    assert executed(calls, "lead_fixture_submit") == 1 and calls["write_count"] == 1


async def test_a_delisted_tools_queued_wake_never_runs(bridge, monkeypatch):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    await fx.submit("marker-beta")
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    monkeypatch.setenv("DAIMON_WATCHES__POLLABLE_TOOLS", json.dumps(NATIVE))
    async with fx.runtime.session_factory() as db:
        (event,) = await watches.list_evidence(db, watch.id)
        wake = await get_continuation(db, idempotency_key=event.wake_key)
        assert await watches.watch_seed(db, wake, now=datetime.now(UTC)) is None
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    current = await get(fx, watch.id)
    assert current.status == "paused" and current.source_state["reason"] == "tool_unlisted"


async def test_an_unlisted_mislabeled_write_is_refused_and_never_called(bridge):
    fx = bridge
    with pytest.raises(ToolError, match="not on the list"):
        await create(fx, "fixture_mislabeled_write", arguments={"marker": "m"})
    calls = await fx.journal()
    assert calls["write_count"] == 0 and executed(calls, "fixture_mislabeled_write") == 0
    # Refused from the registry alone: the server was never contacted.
    assert fx.transport.requests == []


async def test_a_listed_write_is_refused_by_its_own_annotation(bridge):
    fx = bridge
    with pytest.raises(ToolError, match="only reads"):
        await create(fx, "lead_fixture_submit", arguments={"marker": "m"}, projection=".")
    calls = await fx.journal()
    assert calls["write_count"] == 0
    # Only the listing was read, never a tools/call.
    assert all(
        json.loads(request.content).get("method") != "tools/call"
        for request in fx.transport.requests
    )


def relisting(change):
    """Wrap the real source so its listing of the counter is changed by `change`."""
    real = watch_polling.open_source

    @asynccontextmanager
    async def opened(url: str, token: str | None) -> AsyncIterator[McpToolSession]:
        async with real(url, token) as source:
            listed = source.list_tools

            async def list_tools():
                tools = [dict(tool) for tool in await listed()]
                for tool in tools:
                    if tool["name"] == "fixture_counter_read":
                        change(tool)
                return tuple(tools)

            source.list_tools = list_tools  # type: ignore[method-assign]
            yield source

    return opened


@pytest.mark.parametrize(
    "change",
    [
        lambda tool: tool.update(annotations={"readOnlyHint": False}),
        lambda tool: tool.update(annotations={"readOnlyHint": True, "destructiveHint": True}),
        lambda tool: tool.pop("annotations"),
        lambda tool: tool.update(name="renamed"),
        lambda tool: tool.update(inputSchema={"type": "object", "properties": {"x": {}}}),
    ],
)
async def test_a_changed_listing_pauses_the_watch_before_any_call(bridge, monkeypatch, change):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    monkeypatch.setattr(watch_polling, "open_source", relisting(change))
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    current = await get(fx, watch.id)
    assert current.status == "paused" and current.source_state["reason"] == "source_changed"
    assert executed(await fx.journal(), "fixture_counter_read") == 0


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda tool: tool.pop("annotations"), "only reads"),
        (lambda tool: tool.update(annotations={"readOnlyHint": False}), "only reads"),
        (
            lambda tool: tool.update(annotations={"readOnlyHint": True, "destructiveHint": True}),
            "only reads",
        ),
    ],
)
async def test_a_missing_or_write_annotation_refuses_create(bridge, monkeypatch, change, message):
    fx = bridge
    monkeypatch.setattr(watch_polling, "open_source", relisting(change))
    with pytest.raises(ToolError, match=message):
        await create(fx)
    async with fx.runtime.session_factory() as db:
        count = await db.scalar(text("SELECT count(*) FROM watches WHERE source='mcp_poll'"))
    assert count == 0


@pytest.mark.parametrize(
    "policy",
    [
        ToolSafetyPolicy(denied=("parity-bridge/fixture_counter_read",)),
        ToolSafetyPolicy(denied=("parity-bridge",)),
        ToolSafetyPolicy(effects={"parity-bridge/fixture_counter_read": "write"}),
    ],
)
async def test_a_denied_entry_or_write_pin_stops_the_poll_before_the_server(bridge, policy):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    fx.transport.requests.clear()
    fx.runtime.settings.tool_safety = policy
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    current = await get(fx, watch.id)
    assert current.status == "paused" and current.source_state["reason"] == "access_revoked"
    assert fx.transport.requests == []
    with pytest.raises(ToolError):
        await create(fx)


async def test_renamed_server_at_the_same_url_keeps_working(bridge):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    fx.agent.return_value = agent_with(("bridge-renamed", BRIDGE + "/"))
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert (await get(fx, watch.id)).status == "active"


@pytest.mark.parametrize(
    "servers",
    [
        # R2: the listed name now points somewhere else.
        (("parity-bridge", OTHER),),
        # Detached.
        (),
        # Two attachments of one URL are ambiguous.
        (("parity-bridge", BRIDGE), ("copy", BRIDGE)),
    ],
)
async def test_a_moved_or_detached_server_pauses_and_gets_no_bearer(bridge, servers):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    fx.transport.requests.clear()
    fx.agent.return_value = agent_with(*servers)
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    current = await get(fx, watch.id)
    assert current.status == "paused" and current.source_state["reason"] == "access_revoked"
    assert fx.transport.requests == []


async def test_a_disabled_tool_pauses(bridge):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    fx.agent.return_value = agent_with(("parity-bridge", BRIDGE), enabled=False)
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert (await get(fx, watch.id)).source_state["reason"] == "access_revoked"


async def test_a_personal_sign_in_server_hidden_for_the_owner_is_refused(bridge, monkeypatch):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    hidden = AsyncMock(return_value=frozenset({"parity-bridge"}))
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.resolve_hidden_mcp_server_names", hidden)
    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.resolve_hidden_mcp_server_names", hidden)
    with pytest.raises(ToolError, match="own sign-in"):
        await create(fx)
    fx.transport.requests.clear()
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert (await get(fx, watch.id)).source_state["reason"] == "access_revoked"
    assert fx.transport.requests == []


async def test_owner_credential_entries_are_refused_until_owner_keys_exist(bridge, monkeypatch):
    fx = bridge
    monkeypatch.setenv(
        "DAIMON_WATCHES__POLLABLE_TOOLS",
        json.dumps([*NATIVE, entry("fixture_counter_read", credential="owner")]),
    )
    with pytest.raises(ToolError, match="your own key"):
        await create(fx)
    assert fx.transport.requests == []


async def test_a_switched_registry_credential_pauses(bridge, monkeypatch):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    monkeypatch.setenv(
        "DAIMON_WATCHES__POLLABLE_TOOLS",
        json.dumps([*NATIVE, entry("fixture_counter_read", credential="none")]),
    )
    fx.transport.requests.clear()
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert (await get(fx, watch.id)).source_state["reason"] == "access_revoked"
    assert fx.transport.requests == []


@pytest.mark.parametrize("kind", ["tool_error", "redirect", "too_large", "timeout"])
async def test_remote_failures_are_transient_and_back_off(bridge, monkeypatch, kind):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)

    @asynccontextmanager
    async def failing(url: str, token: str | None) -> AsyncIterator[McpToolSession]:
        raise McpCallError(kind)
        yield  # pragma: no cover

    monkeypatch.setattr(watch_polling, "open_source", failing)
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    current = await get(fx, watch.id)
    assert current.status == "active" and current.consecutive_errors == 1
    assert current.backoff_until is not None


async def test_redirecting_server_gets_the_bearer_only_at_the_registered_url(bridge, monkeypatch):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    seen: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.example/steal"})

    monkeypatch.setattr(
        watch_polling,
        "open_source",
        partial(mcp_tool_session, resolve=pinned, transport=httpx.MockTransport(redirect)),
    )
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert len(seen) == 1
    assert seen[0].url.host == PINNED and seen[0].headers["host"] == "parity.example"
    assert (await get(fx, watch.id)).consecutive_errors == 1


async def test_the_token_never_reaches_logs_rows_or_evidence(bridge, caplog, capsys, monkeypatch):
    fx = bridge
    caplog.set_level(logging.DEBUG)
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    await fx.submit("marker-gamma")
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad token {request.headers.get('authorization')}")

    monkeypatch.setattr(
        watch_polling,
        "open_source",
        partial(mcp_tool_session, resolve=pinned, transport=httpx.MockTransport(refusing)),
    )
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    out, err = capsys.readouterr()
    assert TOKEN not in caplog.text and TOKEN not in out and TOKEN not in err
    async with fx.runtime.session_factory() as db:
        for table in ("watches", "watch_inbox", "watch_firings", "task_continuations"):
            rows = (await db.execute(text(f"SELECT * FROM {table}"))).all()
            assert rows and TOKEN not in repr(rows), table
        evidence = await watches.list_evidence(db, watch.id)
        assert evidence and all(TOKEN not in item.payload for item in evidence)
        stored = await db.scalar(text("SELECT encrypted_token FROM agent_mcp_credentials"))
        assert TOKEN.encode() not in bytes(stored)


async def test_a_spent_server_budget_releases_the_lease_without_an_error(bridge, monkeypatch):
    fx = bridge
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    budget = UrlBudget(concurrent=2, per_minute=0)
    monkeypatch.setattr(watch_polling, "SOURCE_BUDGET", budget)
    fx.transport.requests.clear()
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    current = await get(fx, watch.id)
    assert current.status == "active" and current.consecutive_errors == 0
    assert current.lease_owner is None and fx.transport.requests == []


async def test_native_watches_still_poll_with_mcp_entries_listed(bridge):
    fx = bridge
    from unittest.mock import patch

    source = AsyncMock(
        return_value=SimpleNamespace(
            model_dump=lambda **_: {"rows": [{"role": "user", "content": "1"}]}
        )
    )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(fx.runtime, fx.native.id, await lease(fx.runtime, fx.native))
    assert source.await_count == 1 and fx.transport.requests == []


def test_url_budget_caps_concurrency_and_rate_per_url():
    budget = UrlBudget(concurrent=2, per_minute=3)
    with budget.take(BRIDGE), budget.take(BRIDGE):
        with pytest.raises(SourceBusy), budget.take(BRIDGE):
            pass
        # Another server is not slowed by this one.
        with budget.take(OTHER):
            pass
    with budget.take(BRIDGE):
        pass
    with pytest.raises(SourceBusy), budget.take(BRIDGE):
        pass


def test_attached_server_matches_by_url_whatever_the_name():
    agent = agent_with(("anything", "https://PARITY.example/mcp/"))
    assert attached_server(agent, BRIDGE, "fixture_counter_read") == "anything"
    for servers in ((), (("parity-bridge", OTHER),), (("a", BRIDGE), ("b", BRIDGE))):
        with pytest.raises(watch_polling.PollAccessRevoked):
            attached_server(agent_with(*servers), BRIDGE, "fixture_counter_read")


def test_operator_denied_and_write_pins_refuse_but_hints_cannot_allow():
    clear = ToolSafetyPolicy()
    assert not operator_denied(clear, server_name="s", tool="anything_at_all")
    assert operator_denied(ToolSafetyPolicy(denied=("s",)), server_name="s", tool="t")
    assert operator_denied(ToolSafetyPolicy(denied=("s/t",)), server_name="s", tool="t")
    assert operator_denied(ToolSafetyPolicy(effects={"s/t": "write"}), server_name="s", tool="t")
