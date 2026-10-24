"""Gemini accepts only lowercase snake_case MCP server names; products keep theirs."""

from datetime import UTC, datetime

import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    MCPConnection,
    Session,
    SessionSpec,
)
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.mcp_auth import (
    compile_mcp,
    native_server_name,
    server_names,
    validate_tools,
)
from mux.drivers.gemini.normalize import saved_events
from mux.errors import ContinuityLost, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage

URL = "https://daimon.invalid/mcp"
SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
MESSAGE = UserMessage(content=(TextPart(text="hi"),))
THREAD = ThreadRef(
    channel=ChannelRef(tenant_id="t", platform="discord", channel_id="c"), thread_id="th"
)


def server(name: str) -> MCPConnection:
    return MCPConnection(name=name, url=URL)


@pytest.mark.parametrize(
    ("declared", "native"),
    [
        ("daimon-mcp", "daimon_mcp"),
        ("Daimon.MCP:v2", "daimon_mcp_v2"),
        ("_private", "mcp__private"),
        ("daimon_mcp", "daimon_mcp"),
        ("math", "math"),
    ],
)
def test_declared_names_compile_to_gemini_snake_case(declared: str, native: str) -> None:
    assert native_server_name(declared) == native
    assert compile_mcp((server(declared),), enabled=False) == [
        {"type": "mcp_server", "name": native, "url": URL}
    ]
    assert server_names((server(declared),)) == {native: declared}


@pytest.mark.parametrize("pair", [("daimon-mcp", "daimon_mcp"), ("Daimon-MCP", "daimon.mcp")])
def test_names_that_normalize_alike_are_refused(pair: tuple[str, str]) -> None:
    with pytest.raises(UnsupportedCapability) as refused:
        compile_mcp(tuple(server(name) for name in pair), enabled=False)
    assert refused.value.missing == ("mcp_server_name_collision",)
    with pytest.raises(UnsupportedCapability):
        compile_agent(
            AgentSpec(
                name="daimon",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                mcp_servers=tuple(server(name) for name in pair),
            )
        )


@pytest.mark.parametrize("name", ["daimon-mcp", "Daimon", "1mcp", "daimon.mcp"])
def test_native_requests_with_unsafe_server_names_are_refused(name: str) -> None:
    with pytest.raises(UnsupportedCapability):
        validate_tools([{"type": "mcp_server", "name": name, "url": URL}])


def test_saved_steps_map_native_server_names_back() -> None:
    raw = {
        "id": "i1",
        "status": "completed",
        "steps": [
            {
                "type": "mcp_server_tool_call",
                "id": "a",
                "name": "describe_agent",
                "server_name": "daimon_mcp",
                "arguments": {},
            },
            {
                "type": "mcp_server_tool_call",
                "id": "b",
                "name": "sum",
                "server_name": "unlisted",
                "arguments": {},
            },
        ],
    }
    session = ResourceRef(provider="gemini", kind="session", id="s", account_scope_id="project")
    events = saved_events(
        raw,
        session,
        root="i1",
        now=datetime.now(UTC),
        server_names={"daimon_mcp": "daimon-mcp"},
    )
    calls = [e.payload for e in events if e.type == "agent.tool_use"]
    assert [c["mcp_server"] for c in calls] == ["daimon-mcp", "unlisted"]


async def hyphenated_session() -> tuple[GeminiManagedAgents, FakeTransport, MemoryStorage, Session]:
    t, storage = FakeTransport(), MemoryStorage()
    ma = GeminiManagedAgents(
        t, storage=storage, state_store=MemoryStateStore(), account_scope_id="project"
    )
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(
            name="a",
            model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
            mcp_servers=(server("daimon-mcp"),),
        ),
        key="agent",
    )
    env = await ma.environments.create(SCOPE, EnvironmentSpec(name="e"), key="env")
    s = await ma.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"thread": THREAD.model_dump(mode="json"), "binding_id": "b"},
                )
            },
        ),
        key="session",
    )
    return ma, t, storage, s


@pytest.mark.asyncio
async def test_session_round_trip_sends_snake_case_and_journals_declared_name() -> None:
    ma, t, _, s = await hyphenated_session()
    stamp = datetime.now(UTC).isoformat()
    t.responses.append(
        {
            "id": "i1",
            "status": "completed",
            "created": stamp,
            "updated": stamp,
            "environment_id": "e1",
            "steps": [
                {
                    "type": "mcp_server_tool_call",
                    "id": "call",
                    "name": "describe_agent",
                    "server_name": "daimon_mcp",
                    "arguments": {},
                },
                {"type": "mcp_server_tool_result", "call_id": "call", "result": "ok"},
            ],
        }
    )
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="one")
    assert [tool for tool in t.requests[0]["tools"] if tool["type"] == "mcp_server"] == [
        {"type": "mcp_server", "name": "daimon_mcp", "url": URL}
    ]
    page = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    (call,) = [e.payload for e in page.data if e.type == "agent.tool_use"]
    assert call["executor"] == "mcp" and call["mcp_server"] == "daimon-mcp"


@pytest.mark.asyncio
async def test_saved_hyphenated_session_asks_for_a_new_thread_without_dispatch() -> None:
    ma, t, storage, s = await hyphenated_session()
    # A record persisted before names were mapped: hyphenated wire name, no map.
    async with storage.transaction() as records:
        record = records.sessions[s.ref.id]
        for tool in record.request["tools"]:
            if isinstance(tool, dict) and tool.get("type") == "mcp_server":
                tool["name"] = "daimon-mcp"
        record.mcp_server_names = {}
    with pytest.raises(ContinuityLost) as lost:
        await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="one")
    assert lost.value.binding_id == "b"
    assert "mcp_server_name" in str(lost.value) and "start a new thread" in str(lost.value)
    assert t.requests == [] and t.read_requests == []
    with pytest.raises(UnsupportedCapability) as refused:
        validate_tools([{"type": "mcp_server", "name": "daimon-mcp", "url": URL}])
    assert refused.value.missing == ("mcp_server_name",)
