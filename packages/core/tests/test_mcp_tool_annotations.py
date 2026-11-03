"""Third-party MCP annotations reach the approval deciders, and only ever as hints."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import MultiFernet
from daimon.core import mcp_tool_annotations
from daimon.core.agent_mcp_credentials import ResolvedMcpCredential
from daimon.core.mcp_oauth.discovery import read_mcp_catalogue
from daimon.core.mcp_tool_annotations import (
    agent_mcp_token_for,
    annotations_from_mcp,
    mcp_annotation_lookup,
)
from daimon.core.tool_safety import ToolAnnotations, ToolCall, ToolSafetyPolicy, classify_tool
from daimon.core.turn.approvals import AnnotationLookup, interactive_decider, unattended_decider
from pydantic import JsonValue
from structlog.testing import capture_logs

_ON = ToolSafetyPolicy(enabled=True, trusted_annotations=("bridge",))
_UNTRUSTED = ToolSafetyPolicy(enabled=True)
_BRIDGE_URL = "https://bridge.example.com/mcp"
_SERVERS = {"bridge": _BRIDGE_URL}


def _call(tool: str, server: str | None = "bridge") -> ToolCall:
    return ToolCall(tool_use_id="tu_1", server_name=server, tool_name=tool)


def _catalogue(tools: Sequence[Mapping[str, JsonValue]], calls: list[str | None] | None = None):
    async def _read(url: str, token: str | None) -> Sequence[Mapping[str, JsonValue]]:
        assert url == _BRIDGE_URL
        if calls is not None:
            calls.append(token)
        return tools

    return _read


async def _no_token(url: str) -> str | None:
    return None


_BRIDGE_TOOLS: list[Mapping[str, JsonValue]] = [
    {"name": "memory_flag_list", "annotations": {"readOnlyHint": True}},
    {"name": "memory_flag_set", "annotations": {"readOnlyHint": False}},
    {"name": "memory_flag_purge", "annotations": {"readOnlyHint": True, "destructiveHint": True}},
    {"name": "memory_flag_count", "annotations": {"title": "Count flags"}},
    {"name": "get_flag_and_clear", "annotations": {"destructiveHint": True}},
    {"name": "memory_flag_show"},
]


def _deciders(policy: ToolSafetyPolicy, lookup: AnnotationLookup, card: AsyncMock):
    return {
        True: interactive_decider(
            policy, requester_platform_user_id="u1", confirm=card, annotations_for=lookup
        ),
        False: unattended_decider(policy, annotations_for=lookup),
    }


@pytest.mark.parametrize("attended", [True, False])
async def test_a_read_only_hint_runs_a_noun_first_read_without_a_card(attended: bool) -> None:
    card = AsyncMock(return_value="denied")
    lookup = mcp_annotation_lookup(
        server_urls=_SERVERS, token_for=_no_token, read_catalogue=_catalogue(_BRIDGE_TOOLS)
    )
    result = await _deciders(_ON, lookup, card)[attended](_call("memory_flag_list"))
    assert result.allow, "readOnlyHint=true makes memory_flag_list a read"
    card.assert_not_awaited()


@pytest.mark.parametrize("attended", [True, False])
@pytest.mark.parametrize(
    "tool",
    [
        "memory_flag_set",  # readOnlyHint false
        "memory_flag_purge",  # readOnlyHint true, destructiveHint true
        "memory_flag_count",  # annotations without either hint
        "get_flag_and_clear",  # destructiveHint only
        "memory_flag_show",  # no annotations at all
        "memory_flag_unknown",  # not in the catalogue
    ],
)
async def test_no_read_only_hint_or_a_destructive_one_is_never_auto_allowed(
    tool: str, attended: bool
) -> None:
    card = AsyncMock(return_value="denied")
    lookup = mcp_annotation_lookup(
        server_urls=_SERVERS, token_for=_no_token, read_catalogue=_catalogue(_BRIDGE_TOOLS)
    )
    result = await _deciders(_ON, lookup, card)[attended](_call(tool))
    assert not result.allow
    if attended:
        card.assert_awaited_once()


def test_a_destructive_hint_beats_a_read_only_hint_and_a_read_name() -> None:
    both = ToolAnnotations(read_only_hint=True, destructive_hint=True)
    assert classify_tool(_ON, server_name="s", tool_name="list_items", annotations=both) == "write"
    assert classify_tool(_ON, server_name="s", tool_name="memory_flag_list") == "write", (
        "without annotations the name decides, and a noun-first name is a write"
    )


@pytest.mark.parametrize("attended", [True, False])
async def test_an_operator_override_wins_over_annotations_both_ways(attended: bool) -> None:
    policy = ToolSafetyPolicy(
        enabled=True,
        trusted_annotations=("bridge",),
        effects={"bridge/memory_flag_list": "write", "bridge/memory_flag_purge": "read"},
    )
    reads: list[str | None] = []
    lookup = mcp_annotation_lookup(
        server_urls=_SERVERS, token_for=_no_token, read_catalogue=_catalogue(_BRIDGE_TOOLS, reads)
    )
    card = AsyncMock(return_value="denied")
    decide = _deciders(policy, lookup, card)[attended]
    assert not (await decide(_call("memory_flag_list"))).allow, "override write beats readOnlyHint"
    assert (await decide(_call("memory_flag_purge"))).allow, "override read beats destructiveHint"
    assert reads == [], "an overridden tool never reads the server's catalogue"


@pytest.mark.parametrize("attended", [True, False])
async def test_a_denied_entry_beats_a_trusted_read_only_hint(attended: bool) -> None:
    policy = ToolSafetyPolicy(
        enabled=True, trusted_annotations=("bridge",), denied=("bridge/memory_flag_list",)
    )
    reads: list[str | None] = []
    lookup = mcp_annotation_lookup(
        server_urls=_SERVERS, token_for=_no_token, read_catalogue=_catalogue(_BRIDGE_TOOLS, reads)
    )
    card = AsyncMock(return_value="approved")
    decide = _deciders(policy, lookup, card)[attended]
    assert not (await decide(_call("memory_flag_list"))).allow
    card.assert_not_awaited()
    assert reads == [], "a denied tool never reads the server's catalogue"


@pytest.mark.parametrize("attended", [True, False])
@pytest.mark.parametrize(
    "policy",
    [
        _UNTRUSTED,
        ToolSafetyPolicy(enabled=True, trusted_annotations=("some-other-server",)),
    ],
    ids=["default-empty", "other-server-trusted"],
)
async def test_an_untrusted_servers_annotations_are_never_read(
    policy: ToolSafetyPolicy, attended: bool
) -> None:
    reads: list[str | None] = []
    lookup = mcp_annotation_lookup(
        server_urls=_SERVERS, token_for=_no_token, read_catalogue=_catalogue(_BRIDGE_TOOLS, reads)
    )
    card = AsyncMock(return_value="denied")
    decide = _deciders(policy, lookup, card)[attended]
    with capture_logs() as logs:
        assert not (await decide(_call("memory_flag_list"))).allow, (
            "the name decides: a noun-first read is a write"
        )
        assert (await decide(_call("get_flag"))).allow, "a plain read name still runs"
    assert reads == [], "no tools/list for a server outside trusted_annotations"
    assert not [e for e in logs if e["event"] == "tool_safety.annotation_read_allowed"]


@pytest.mark.parametrize("attended", [True, False])
async def test_an_unreadable_catalogue_falls_back_to_the_name(attended: bool) -> None:
    async def _refused(url: str, token: str | None) -> Sequence[Mapping[str, JsonValue]]:
        raise ValueError("MCP catalogue read refused")

    lookup = mcp_annotation_lookup(
        server_urls=_SERVERS, token_for=_no_token, read_catalogue=_refused
    )
    card = AsyncMock(return_value="denied")
    decide = _deciders(_ON, lookup, card)[attended]
    assert not (await decide(_call("memory_flag_list"))).allow
    assert (await decide(_call("get_flag"))).allow


async def test_a_lookup_that_raises_falls_back_to_the_name() -> None:
    async def _broken(call: ToolCall) -> ToolAnnotations | None:
        raise RuntimeError("boom")

    decide = unattended_decider(_ON, annotations_for=_broken)
    assert not (await decide(_call("memory_flag_list"))).allow
    assert (await decide(_call("list_flags"))).allow


async def test_the_catalogue_is_read_once_per_server_with_the_agent_credential() -> None:
    reads: list[str | None] = []

    async def _token(url: str) -> str | None:
        return "agent-wide-token" if url == _BRIDGE_URL else None

    lookup = mcp_annotation_lookup(
        server_urls={**_SERVERS, "other": "https://other.example.com/mcp"},
        token_for=_token,
        read_catalogue=_catalogue(_BRIDGE_TOOLS, reads),
    )
    assert await lookup(_call("memory_flag_list")) == ToolAnnotations(read_only_hint=True)
    assert await lookup(_call("memory_flag_set")) == ToolAnnotations(read_only_hint=False)
    assert await lookup(_call("whatever", server="unattached")) is None
    assert reads == ["agent-wide-token"]


async def test_trusted_and_disabled_calls_do_not_read_annotations() -> None:
    lookup = AsyncMock(return_value=ToolAnnotations(read_only_hint=True))
    await unattended_decider(
        _ON, trusted_servers=frozenset({"daimon-mcp"}), annotations_for=lookup
    )(_call("memory_flag_list", server="daimon-mcp"))
    await unattended_decider(ToolSafetyPolicy(enabled=False), annotations_for=lookup)(
        _call("memory_flag_list")
    )
    await unattended_decider(_ON, annotations_for=lookup)(_call("memory_flag_list", server=None))
    lookup.assert_not_awaited()


@pytest.mark.parametrize("attended", [True, False])
async def test_each_annotation_based_allow_is_logged(attended: bool) -> None:
    lookup = mcp_annotation_lookup(
        server_urls=_SERVERS, token_for=_no_token, read_catalogue=_catalogue(_BRIDGE_TOOLS)
    )
    card = AsyncMock(return_value="denied")
    decide = _deciders(_ON, lookup, card)[attended]
    with capture_logs() as logs:
        await decide(_call("memory_flag_list"))
        await decide(_call("memory_flag_purge"))
        await decide(_call("get_flag"))
    allowed = [e for e in logs if e["event"] == "tool_safety.annotation_read_allowed"]
    assert allowed == [
        {
            "event": "tool_safety.annotation_read_allowed",
            "log_level": "info",
            "server": "bridge",
            "tool": "memory_flag_list",
            "attended": attended,
            "hint": "readOnlyHint",
            "by_name": "write",
        }
    ], "only the hint-decided read is logged, not the destructive one or a plain name read"


async def test_a_refused_read_warns_once_per_server_then_logs_at_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_tool_annotations, "_warned_urls", set[str]())

    async def _refused(url: str, token: str | None) -> Sequence[Mapping[str, JsonValue]]:
        raise ValueError("MCP catalogue read refused")

    with capture_logs() as logs:
        for _ in range(2):
            lookup = mcp_annotation_lookup(
                server_urls=_SERVERS, token_for=_no_token, read_catalogue=_refused
            )
            assert await lookup(_call("memory_flag_list")) is None
    levels = [e["log_level"] for e in logs if e["event"] == "tool_safety.annotations_unavailable"]
    assert levels == ["warning", "info"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"name": "t"}, None),
        ({"name": "t", "annotations": None}, None),
        ({"name": "t", "annotations": {"readOnlyHint": "yes"}}, None),
        (
            {"name": "t", "annotations": {"readOnlyHint": True, "destructiveHint": 1}},
            ToolAnnotations(read_only_hint=True),
        ),
        (
            {"name": "t", "annotations": {"destructiveHint": True}},
            ToolAnnotations(destructive_hint=True),
        ),
    ],
)
def test_only_boolean_hints_are_read(
    raw: Mapping[str, JsonValue], expected: ToolAnnotations | None
) -> None:
    assert annotations_from_mcp(raw) == expected


async def test_the_token_is_the_agent_wide_credential_for_that_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _credentials(**_: object) -> tuple[ResolvedMcpCredential, ...]:
        return (
            ResolvedMcpCredential(
                mcp_server_url="https://BRIDGE.example.com/mcp/", token="t1", version="v"
            ),
        )

    monkeypatch.setattr(mcp_tool_annotations, "resolve_agent_mcp_credentials", _credentials)
    token_for = agent_mcp_token_for(
        AsyncMock(),
        fernet=cast(MultiFernet, object()),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
    )
    assert await token_for(_BRIDGE_URL) == "t1"
    assert await token_for("https://other.example.com/mcp") is None
    no_keys = agent_mcp_token_for(
        AsyncMock(), fernet=None, tenant_id=uuid.uuid4(), agent_id=uuid.uuid4()
    )
    assert await no_keys(_BRIDGE_URL) is None


async def test_the_catalogue_read_keeps_annotations_and_sends_no_empty_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_auth: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result: dict[str, object] = {"protocolVersion": "2025-06-18", "capabilities": {}}
        if body["method"] == "tools/list":
            result = {"tools": _BRIDGE_TOOLS[:2]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original = httpx.AsyncClient

    def _client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    tools = await read_mcp_catalogue(_BRIDGE_URL, None)
    assert [annotations_from_mcp(tool) for tool in tools] == [
        ToolAnnotations(read_only_hint=True),
        ToolAnnotations(read_only_hint=False),
    ]
    assert seen_auth == [None, None, None]
