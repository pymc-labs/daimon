"""A workspace's shared MCP connections: add, read, list for a caller, call.

An admin adds one upstream MCP server with a bearer token. Its tools are read into the store
disabled; enabling one approves the description and schema the page showed, and those pinned
words are what callers get, so a server cannot rewrite a tool after review. Callers see an
approved tool of an active connection as `<connection>_<tool>` when the `shared_connections`
feature is on and their scope allows it. The credential is the connection's, not the person's.

Scopes live in the data schema and belong to the data package: `register_scope_check` is the
port it fills. Until it does, a scoped connection or tool is refused to everyone (fail
closed). Calls go through `mcp_tool_session`, the bounded public-address-only client.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

from cryptography.fernet import MultiFernet
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, Sessions, place_feature_or_none
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_https_url
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.mcp_tool_call import (
    INTERACTIVE_LIMITS,
    McpCallError,
    McpToolSession,
    mcp_tool_session,
)
from daimon.core.stores import mcp_connections as store
from daimon.core.stores.domain import (
    DiscoveredTool,
    McpConnectionRow,
    McpConnectionToolRow,
)
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    ResolvedAgentToolRule,
    ToolAnnotations,
    ToolCall,
    ToolSafetyPolicy,
    ToolVerdict,
    classify_tool,
    decide_tool_call,
)
from pydantic import JsonValue
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "FEATURE",
    "NAME_PATTERN",
    "SCOPE_PATTERN",
    "ScopeCheck",
    "SharedCaller",
    "SharedConnectionError",
    "SharedResult",
    "SharedTool",
    "add_connection",
    "approve_tool",
    "encrypt_auth",
    "call_upstream",
    "decide_shared_call",
    "is_served_name",
    "refresh_connection",
    "register_scope_check",
    "seen_digest",
    "served_tool",
    "served_tools",
    "shared_connections_on",
]

FEATURE: Final = "shared_connections"
NAME_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
"""A connection name carries no underscore: the first one in `<connection>_<tool>` ends it."""
SCOPE_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_SERVED = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_TOKEN_MAX = 4096
_TOOLS_MAX = 200
_DESCRIPTION_MAX = 2000
_RESULT_MAX = 100_000

OpenUpstream = Callable[[str, str | None], AbstractAsyncContextManager[McpToolSession]]


def open_upstream(url: str, token: str | None) -> AbstractAsyncContextManager[McpToolSession]:
    return mcp_tool_session(url, token, limits=INTERACTIVE_LIMITS)


class SharedConnectionError(Exception):
    """A connection could not be added, read or called; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class SharedCaller:
    """Who asks: what the data package's scope check needs, and nothing else."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    platform: str | None = None
    chat_session_id: str | None = None


class ScopeCheck(Protocol):
    """Whether `caller` holds data scope `scope`, asked of the audience the call runs for."""

    async def __call__(self, caller: SharedCaller, scope: str) -> bool: ...


async def _no_scopes(caller: SharedCaller, scope: str) -> bool:
    return False


_scope_check: ScopeCheck = _no_scopes


def register_scope_check(check: ScopeCheck | None) -> None:
    """Set the scope check, once at start (the data package); None restores the refusal."""
    global _scope_check
    _scope_check = check or _no_scopes


@dataclass(frozen=True, slots=True)
class SharedTool:
    """An approved tool a caller may use, under the name it is served as."""

    name: str
    connection: McpConnectionRow
    tool: McpConnectionToolRow


def is_served_name(name: str) -> bool:
    return _SERVED.fullmatch(name) is not None


def _served_name(connection: str, tool: str) -> str | None:
    name = f"{connection}_{tool}"
    return name if _SERVED.fullmatch(name) else None


async def shared_connections_on(
    sessions: Sessions, settings: Settings | None, places: Sequence[FeaturePlace]
) -> bool:
    """Whether the feature is on at every place the call may run; a failed read is off."""
    if not places:
        return False
    for place in places:
        found = await place_feature_or_none(sessions, settings, place, "shared_connections")
        if found is None or not found.on:
            return False
    return True


async def served_tools(session: AsyncSession, caller: SharedCaller) -> list[SharedTool]:
    """The tools `caller` may see: approved, on an active connection, within scope."""
    allowed: dict[str, bool] = {}

    async def within(scope: str | None) -> bool:
        if scope is None:
            return True
        if scope not in allowed:
            allowed[scope] = await _scope_check(caller, scope)
        return allowed[scope]

    out: list[SharedTool] = []
    for connection, tool in await store.list_served(session, tenant_id=caller.tenant_id):
        name = _served_name(connection.name, tool.name)
        if name is not None and await within(tool.scope or connection.scope):
            out.append(SharedTool(name, connection, tool))
    return out


async def served_tool(session: AsyncSession, caller: SharedCaller, name: str) -> SharedTool | None:
    """One served tool by its `<connection>_<tool>` name, None when `caller` may not use it."""
    if not is_served_name(name):
        return None
    return next((t for t in await served_tools(session, caller) if t.name == name), None)


def decide_shared_call(
    policy: ToolSafetyPolicy,
    name: str,
    arguments: dict[str, object],
    *,
    writes: bool,
    attended: bool,
    agent_rule: ResolvedAgentToolRule | None,
) -> ToolVerdict:
    """Run, ask or refuse one call by the rules for any third-party tool: never trusted, a
    write asks (and is refused unattended), the operator's and the agent's rules apply.
    `name` is the served name; `writes` is the admin's flag."""
    call = ToolCall(
        tool_use_id=f"shared-{uuid.uuid4()}",
        server_name=DAIMON_SERVER_NAME,
        tool_name=name,
        input=arguments,
    )
    # A shared write always asks, whatever the operator's global switch for tools.
    return decide_tool_call(
        policy.model_copy(update={"enabled": True}),
        call,
        attended=attended,
        trusted_servers=frozenset(),
        annotations=ToolAnnotations(read_only_hint=not writes, destructive_hint=None),
        agent_rule=agent_rule,
    )


def seen_digest(tool: McpConnectionToolRow) -> str:
    """A short fingerprint of the words the upstream shows now, to approve exactly those."""
    text = json.dumps([tool.seen_description, tool.seen_schema], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def approve_tool(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    name: str,
    seen: str,
) -> None:
    """Serve a tool under the description and schema the admin was shown (`seen_digest`)."""
    rows = await store.list_tools(session, tenant_id=tenant_id, connection_id=connection_id)
    tool = next((t for t in rows if t.name == name), None)
    if tool is None:
        raise SharedConnectionError("This tool isn't here.")
    if seen_digest(tool) != seen:
        raise SharedConnectionError("The server changed this tool just now. Review it again.")
    await store.update_tool(
        session,
        tenant_id=tenant_id,
        connection_id=connection_id,
        name=name,
        values={
            "description": tool.seen_description,
            "input_schema": tool.seen_schema,
            "enabled": True,
        },
    )


def _clean_token(token: str | None) -> str | None:
    if token is None or not token.strip():
        return None
    token = token.strip()
    if len(token) > _TOKEN_MAX or not token.isprintable() or " " in token:
        raise SharedConnectionError("The token isn't valid.")
    return token


def encrypt_auth(fernet: MultiFernet, token: str) -> bytes | None:
    """The stored form of a token; None for a blank one."""
    clean = _clean_token(token)
    return None if clean is None else encrypt_token(fernet, clean)


def reveal_auth(fernet: MultiFernet, connection: McpConnectionRow) -> str | None:
    return (
        None
        if connection.encrypted_auth is None
        else decrypt_token(fernet, connection.encrypted_auth)
    )


async def add_connection(
    session: AsyncSession,
    fernet: MultiFernet,
    *,
    tenant_id: uuid.UUID,
    name: str,
    url: str,
    token: str | None,
    scope: str | None = None,
    declared: bool = False,
) -> McpConnectionRow:
    """Store a connection; the address must be public https and the name unused."""
    if NAME_PATTERN.fullmatch(name) is None:
        raise SharedConnectionError("Use lowercase letters, digits and dashes in the name.")
    try:
        address = canonical_mcp_url(assert_public_https_url(url.strip(), what="address"))
        if "@" in urlsplit(address).netloc:
            raise McpUrlError("address has credentials")
    except McpUrlError as err:
        raise SharedConnectionError("The address must be a public https address.") from err
    if await store.get_connection_by_name(session, tenant_id=tenant_id, name=name) is not None:
        raise SharedConnectionError("A connection with this name already exists.")
    return await store.create_connection(
        session,
        tenant_id=tenant_id,
        name=name,
        url=address,
        encrypted_auth=None if token is None else encrypt_auth(fernet, token),
        scope=scope,
        declared=declared,
    )


def _discovered(
    policy: ToolSafetyPolicy, connection: str, listed: tuple[dict[str, JsonValue], ...]
) -> list[DiscoveredTool]:
    found: list[DiscoveredTool] = []
    for item in listed[:_TOOLS_MAX]:
        name, description, schema = (
            item.get("name"),
            item.get("description"),
            item.get("inputSchema"),
        )
        hints = item.get("annotations")
        if not isinstance(name, str) or _served_name(connection, name) is None:
            continue
        hint = hints if isinstance(hints, dict) else {}
        read_only, destructive = hint.get("readOnlyHint"), hint.get("destructiveHint")
        effect = classify_tool(
            policy,
            server_name=DAIMON_SERVER_NAME,
            tool_name=name,
            annotations=ToolAnnotations(
                read_only_hint=read_only if isinstance(read_only, bool) else None,
                destructive_hint=destructive if isinstance(destructive, bool) else None,
            ),
        )
        found.append(
            DiscoveredTool(
                name=name,
                description=(description if isinstance(description, str) else "")[
                    :_DESCRIPTION_MAX
                ],
                input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                writes=effect == "write",
            )
        )
    return found


async def refresh_connection(
    sessions: Sessions,
    fernet: MultiFernet,
    policy: ToolSafetyPolicy,
    connection: McpConnectionRow,
    *,
    open_session: OpenUpstream | None = None,
) -> int:
    """Read the upstream's tool list into the store; how it went is kept on the connection."""
    try:
        async with (open_session or open_upstream)(
            connection.url, reveal_auth(fernet, connection)
        ) as upstream:
            listed = await upstream.list_tools()
    except McpCallError as err:
        async with sessions.begin() as session:
            await store.mark_connection(
                session,
                tenant_id=connection.tenant_id,
                connection_id=connection.id,
                status="error",
                last_error=f"The server could not be read ({err.kind}).",
            )
        raise SharedConnectionError("The server could not be read.") from err
    found = _discovered(policy, connection.name, listed)
    async with sessions.begin() as session:
        await store.sync_tools(
            session, tenant_id=connection.tenant_id, connection_id=connection.id, discovered=found
        )
        await store.update_connection(
            session,
            tenant_id=connection.tenant_id,
            connection_id=connection.id,
            values={"status": "active", "last_error": None, "refreshed_at": func.now()},
        )
    return len(found)


@dataclass(frozen=True, slots=True)
class SharedResult:
    """What the upstream answered, as plain text: no images, links or embedded resources."""

    text: str
    is_error: bool


def _result_of(raw: dict[str, JsonValue]) -> SharedResult:
    content = raw.get("content")
    parts: list[str] = []
    for item in content if isinstance(content, list) else []:
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ):
            parts.append(str(item["text"]))
        else:
            parts.append("[content that is not text was left out]")
    text = "\n".join(parts)
    if not text and raw.get("structuredContent") is not None:
        text = json.dumps(raw["structuredContent"])
    return SharedResult(text[:_RESULT_MAX] or "Done.", raw.get("isError") is True)


async def call_upstream(
    fernet: MultiFernet,
    shared: SharedTool,
    arguments: dict[str, JsonValue],
    *,
    open_session: OpenUpstream | None = None,
) -> SharedResult:
    """Call the connection's tool by its own name; an unreachable server is one safe message."""
    try:
        async with (open_session or open_upstream)(
            shared.connection.url, reveal_auth(fernet, shared.connection)
        ) as up:
            return _result_of(await up.call_tool_result(shared.tool.name, arguments))
    except McpCallError as err:
        raise SharedConnectionError("The connection didn't answer. Try again later.") from err
