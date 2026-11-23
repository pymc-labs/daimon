"""The reaction-action route calls only what a binding allows, as the reactor, with the server's key."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from daimon.adapters.mcp.reaction_actions import (
    METHODS,
    ROUTE,
    build_reaction_action_route,
    stripped_arguments,
)
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_tool_call import McpCallError
from daimon.core.reaction_actions import CALL_PATH, ReactionAction
from daimon.core.reaction_credential import ReactionCall, mint
from pydantic import SecretStr
from starlette.applications import Starlette

SECRET = b"s" * 32
KEY = "server-key-sentinel"
SERVER = SignedIdentityServer(
    name="tools",
    url="https://tools.example/mcp",
    auth="bearer",
    requester_meta=True,
    workspaces=frozenset({"discord:1"}),
    agents=("Helper",),
)
ACTION = ReactionAction(
    workspace="discord:1",
    server="tools",
    agent="Helper",
    mark_tool="propose",
    mark_emoji="mark:111",
    confirm_tool="confirm",
    confirm_emoji="done:222",
)
CALL = ReactionCall(
    workspace="discord:1",
    server="tools",
    tool="confirm",
    agent="Helper",
    user="9",
    guild="1",
    channel="2",
    message="3",
)


class Upstream:
    """Stands in for the server: records each call and answers with `result`."""

    def __init__(self, result: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.result = result or {"content": [{"type": "text", "text": "saved"}]}
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def session(self, url: str, token: str) -> AbstractAsyncContextManager[Any]:
        @asynccontextmanager
        async def open_session() -> AsyncIterator[Any]:
            if self.fail:
                raise McpCallError("transport")
            yield self
            return

        self.calls.append({"url": url, "token": token})
        return open_session()

    async def call_tool_result(
        self, name: str, arguments: dict[str, Any], *, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls[-1].update(name=name, arguments=arguments, meta=meta)
        return self.result


def _client(upstream: Upstream, now: datetime | None = None) -> httpx.AsyncClient:
    app = Starlette()
    app.add_route(
        ROUTE,
        build_reaction_action_route(
            actions=[ACTION],
            servers={"tools": SERVER},
            bearers={"tools": SecretStr(KEY)},
            secret=SECRET,
            clock=lambda: now or datetime.now(UTC),
            session=upstream.session,
        ),
        methods=METHODS,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://d.test")


def _token(call: ReactionCall = CALL, *, age: float = 0) -> str:
    return mint(call, secret=SECRET, now=datetime.now(UTC) - timedelta(seconds=age))


def _post(client: httpx.AsyncClient, token: str | None, **body: object) -> Any:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return client.post(CALL_PATH, headers=headers, json=body)


async def test_a_call_carries_the_reactor_the_link_and_the_servers_key() -> None:
    upstream = Upstream()
    async with _client(upstream) as client:
        reply = await _post(client, _token())
    assert reply.status_code == 200
    assert reply.json() == {"status": "ok", "message": ""}
    (call,) = upstream.calls
    assert call["url"] == "https://tools.example/mcp"
    assert call["token"] == KEY
    assert call["name"] == "confirm"
    assert call["arguments"] == {"message_url": "https://discord.com/channels/1/2/3"}
    assert call["meta"] == {
        "requester_platform_user_id": "9",
        "requester_kind": "person",
        "requester_conversation": {
            "place_type": "channel",
            "channel_id": "2",
            "parent_channel_id": None,
        },
    }


async def test_a_thread_names_its_parent_channel() -> None:
    upstream = Upstream()
    async with _client(upstream) as client:
        await _post(client, _token(replace(CALL, parent="7")))
    assert upstream.calls[0]["meta"]["requester_conversation"] == {
        "place_type": "thread",
        "channel_id": "2",
        "parent_channel_id": "7",
    }


async def test_caller_supplied_requester_keys_and_link_are_dropped() -> None:
    upstream = Upstream()
    hostile = {
        "requester_platform_user_id": "666",
        "Requester_Kind": "routine",
        "_meta": {"requester_platform_user_id": "666"},
        "message_url": "https://discord.com/channels/1/9/9",
        "extra": "kept",
    }
    async with _client(upstream) as client:
        await _post(client, _token(), arguments=hostile)
    (call,) = upstream.calls
    assert call["arguments"] == {
        "extra": "kept",
        "message_url": "https://discord.com/channels/1/2/3",
    }
    assert call["meta"]["requester_platform_user_id"] == "9"
    assert call["meta"]["requester_kind"] == "person"
    assert stripped_arguments("not an object") == {}


async def test_a_pair_no_binding_allows_is_refused_and_nothing_is_called() -> None:
    upstream = Upstream()
    async with _client(upstream) as client:
        edits: tuple[dict[str, str], ...] = (
            {"tool": "propose"},
            {"tool": "delete_everything"},
            {"server": "other"},
            {"agent": "Intruder"},
            {"workspace": "discord:2"},
        )
        for edit in edits:
            reply = await _post(client, _token(replace(CALL, **edit)))
            assert reply.status_code == 403, edit
    assert upstream.calls == []


async def test_a_bad_or_expired_credential_is_refused_and_nothing_is_called() -> None:
    upstream = Upstream()
    other = mint(CALL, secret=b"t" * 32, now=datetime.now(UTC))
    async with _client(upstream) as client:
        assert (await _post(client, None)).status_code == 401
        assert (await _post(client, "garbage")).status_code == 401
        assert (await _post(client, other)).status_code == 401
        assert (await _post(client, _token(age=120))).status_code == 401
    assert upstream.calls == []


async def test_the_results_map_to_a_status_and_a_short_text() -> None:
    cases = [
        ({"isError": True, "content": [{"type": "text", "text": "Not allowed"}]}, "error"),
        ({"structuredContent": {"status": "attention", "message": "Differs"}}, "attention"),
    ]
    for result, status in cases:
        async with _client(Upstream(result)) as client:
            reply = await _post(client, _token())
        assert reply.json()["status"] == status
    async with _client(Upstream(cases[1][0])) as client:
        assert (await _post(client, _token())).json()["message"] == "Differs"


async def test_an_unreachable_server_is_an_error_without_its_detail() -> None:
    async with _client(Upstream(fail=True)) as client:
        reply = await _post(client, _token())
    assert reply.status_code == 200
    assert reply.json()["status"] == "error"
    assert KEY not in reply.text


async def test_an_oversized_body_is_refused() -> None:
    upstream = Upstream()
    async with _client(upstream) as client:
        reply = await _post(client, _token(), arguments={"x": "y" * 20000})
    assert reply.status_code == 413
    assert upstream.calls == []
