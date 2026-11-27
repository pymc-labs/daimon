"""MCP tools of an item's fork, run by the worker (`host_tools`) so the provider dials nothing.

The fork lists them as custom tools. Daimon's own go to the deployment's MCP service with a
short credential bound to the item and its turn: the service's gates and cards apply and its
world middleware answers. A recorded server's go to its eval route with the world token.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final, cast
from urllib.parse import urljoin

import httpx
from daimon.core.evals.format import Case
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import bind_execution_session, mint_chat_mcp_token
from daimon.core.mcp_tool_call import (
    McpCallError,
    Resolver,
    mcp_tool_session,
    resolve_public_address,
)
from daimon.core.mux_backend import resource_scope
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.host_tools import HostTool
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CALL_TTL: Final = timedelta(minutes=10)
_FAILED: Final = "The tool couldn't be reached. Nothing was done."
JsonObject = dict[str, Any]


@dataclass(frozen=True)
class McpService:
    """Where the deployment's MCP service is, and the secret its credentials are signed with."""

    url: str
    secret: bytes
    transport: httpx.AsyncBaseTransport | None = None
    resolve: Resolver = resolve_public_address


@dataclass(frozen=True)
class Caller:
    """Who an item's calls run as: the requester's account and the fork."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    ma_agent_id: str


def _text(result: Mapping[str, JsonValue]) -> tuple[str, bool]:
    content = cast(list[JsonValue], result.get("content") or [])
    texts = [str(c.get("text", "")) for c in content
             if isinstance(c, dict) and c.get("type") == "text"]  # fmt: skip
    if not texts and result.get("structuredContent") is not None:
        texts = [json.dumps(result["structuredContent"], ensure_ascii=False)]
    return "\n".join(texts), result.get("isError") is True


async def _call(service: McpService, url: str, token: str, name: str,
                args: Mapping[str, object]) -> tuple[str, bool]:  # fmt: skip
    try:
        async with mcp_tool_session(url, token, resolve=service.resolve,
                                    transport=service.transport) as mcp:  # fmt: skip
            return _text(await mcp.call_tool_result(name, cast(Mapping[str, JsonValue], args)))
    except McpCallError:
        return _FAILED, True


async def _mint(sessions: async_sessionmaker[AsyncSession], service: McpService, caller: Caller,
                *, bind: bool) -> tuple[str, uuid.UUID]:  # fmt: skip
    """A chat credential of the fork; it carries the running item (`current_eval_item`) and,
    with `bind`, the executing turn's grant."""
    now = datetime.now(UTC)
    agent_id = derive_agent_uuid(tenant_id=caller.tenant_id, ma_agent_id=caller.ma_agent_id)
    async with sessions.begin() as session:
        token, jti, _ = await mint_chat_mcp_token(
            session, account_id=caller.account_id, agent_id=agent_id, secret=service.secret,
            now=now, ttl=_CALL_TTL,
        )  # fmt: skip
    if bind:
        scope = resource_scope(tenant_id=str(caller.tenant_id), account_id=str(caller.account_id))
        token = await bind_execution_session(sessions, token=token, secret=service.secret,
                                             scope=scope, agent_id=agent_id, now=now)  # fmt: skip
    return token, jti


async def _revoke(sessions: async_sessionmaker[AsyncSession], jti: uuid.UUID) -> None:
    async with sessions.begin() as session:
        await revoke_mcp_token(session, jti=jti, now=datetime.now(UTC))


async def daimon_catalogue(
    sessions: async_sessionmaker[AsyncSession], service: McpService, caller: Caller
) -> tuple[JsonObject, ...]:
    """The tools Daimon's MCP service lists to an eval caller."""
    token, jti = await _mint(sessions, service, caller, bind=False)
    try:
        async with mcp_tool_session(service.url, token, resolve=service.resolve,
                                    transport=service.transport) as mcp:  # fmt: skip
            return tuple(cast(JsonObject, t) for t in await mcp.list_tools())
    finally:
        await _revoke(sessions, jti)


def _config(toolset: Mapping[str, Any], name: str) -> tuple[bool, bool]:
    """Whether the toolset enables `name`, and whether it asks before it runs."""
    default: JsonObject = toolset.get("default_config") or {}
    configs: list[JsonObject] = toolset.get("configs") or []
    config = next((c for c in configs if c.get("name") == name), default)
    policy: JsonObject = config.get("permission_policy") or default.get("permission_policy") or {}
    enabled = config.get("enabled", default.get("enabled", True))
    return bool(enabled), policy.get("type") == "always_ask"


def custom_tools(
    source_tools: Sequence[Mapping[str, Any]], daimon: Sequence[JsonObject], case: Case
) -> list[JsonObject]:
    """The fork's custom tools: each MCP toolset of the source agent whose server is
    Daimon's or a recorded one, with the tools that toolset enables. `source_tools` carry
    the deployment's tool safety, as a session's would (`session_tools_for_policy`)."""
    recorded = {s.name: s for s in case.world.mcp_servers or () if s.kind != "daimon"}
    listed: dict[str, JsonObject] = {}
    for toolset in source_tools:
        server = toolset.get("mcp_server_name")
        if toolset.get("type") != "mcp_toolset":
            continue
        if server == DAIMON_SERVER_NAME:
            tools = [(t["name"], t.get("description"), t.get("inputSchema")) for t in daimon]
        elif server in recorded:
            tools = [(t.name, t.description, t.input_schema) for t in recorded[server].tools or ()]
        else:
            continue
        for name, description, schema in tools:
            enabled, ask = _config(toolset, str(name))
            if enabled and name not in listed:
                listed[str(name)] = {
                    "type": "custom", "name": name, "description": description or "",
                    "input_schema": schema or {"type": "object"}, "_server": server, "_ask": ask,
                }  # fmt: skip
    return list(listed.values())


def item_tools(
    sessions: async_sessionmaker[AsyncSession], service: McpService, caller: Caller,
    *, item_id: uuid.UUID, world_token: str, tools: Sequence[JsonObject],
) -> dict[str, HostTool]:  # fmt: skip
    """How the worker answers each of the fork's custom tools (`custom_tools`)."""

    async def daimon(name: str, args: Mapping[str, object]) -> tuple[str, bool]:
        token, jti = await _mint(sessions, service, caller, bind=True)
        try:
            return await _call(service, service.url, token, name, args)
        finally:
            await _revoke(sessions, jti)

    def recorded(name: str, args: Mapping[str, object], server: str) -> Awaitable[tuple[str, bool]]:
        url = urljoin(service.url, f"/eval-mcp/{item_id}/{server}")
        return _call(service, url, world_token, name, args)

    def host(tool: JsonObject) -> HostTool:
        server = str(tool["_server"])
        call = daimon if server == DAIMON_SERVER_NAME else partial(recorded, server=server)
        return HostTool(server, call, ask=bool(tool["_ask"]))

    return {str(t["name"]): host(t) for t in tools}
