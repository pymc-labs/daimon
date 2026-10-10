"""Host credential references in agent intent; bearer values only in session input."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

from pydantic import JsonValue, TypeAdapter

from mux.contracts.ids import Scope
from mux.contracts.resources import MCPConnection
from mux.drivers.openai._common import Context, objects, text
from mux.drivers.openai.secret_value import RedactedCredential
from mux.drivers.openai.transport import Object, object_json
from mux.errors import ProviderError

KEY = "mux_mcp_credential_refs"
_CONNECTIONS = TypeAdapter(tuple[MCPConnection, ...])
_REF = re.compile(r"[A-Za-z0-9_.:/-]{1,128}\Z")
# The host must authorize both the secret reference and exact destination.
MCPSecretResolver = Callable[[Scope, str, str], Awaitable[str]]


def validate(connection: MCPConnection, context: Context) -> None:
    if connection.transport != "streamable_http":
        raise context.unsupported("mcp_transport")
    policy = connection.tool_policy
    if set(policy) - {"allowed_tools", "required"}:
        raise context.unsupported("mcp_tool_policy")
    allowed = policy.get("allowed_tools")
    if "allowed_tools" in policy and (
        not isinstance(allowed, list)
        or any(not isinstance(name, str) or not name for name in allowed)
        or len(set(allowed)) != len(allowed)
    ):
        raise context.unsupported("mcp_allowed_tools")
    if "required" in policy and not isinstance(policy["required"], bool):
        raise context.unsupported("mcp_required")
    if connection.credential_ref is not None:
        if _REF.fullmatch(connection.credential_ref) is None:
            raise context.unsupported("mcp_credential_reference")
        url = urlsplit(connection.url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise context.unsupported("credential_https_mcp_destination")


def tool(connection: MCPConnection, context: Context) -> Object:
    validate(connection, context)
    return {
        "type": "mcp",
        "server_label": connection.name,
        "transport": {"type": "http", "server_url": connection.url},
        **dict(connection.tool_policy),
    }


def encode(connections: Sequence[MCPConnection], context: Context) -> Object:
    if len({c.name for c in connections}) != len(connections):
        raise context.unsupported("duplicate_mcp_server")
    authenticated = [c for c in connections if c.credential_ref is not None]
    if not authenticated:
        return {}
    for connection in authenticated:
        validate(connection, context)
    value = json.dumps([c.model_dump(mode="json") for c in authenticated], separators=(",", ":"))
    if len(value) > 512:
        raise context.unsupported("mcp_credential_metadata_size")
    return {KEY: value}


def decode(raw: Object, context: Context) -> tuple[MCPConnection, ...]:
    metadata = object_json(raw.get("metadata") or {})
    if KEY not in metadata:
        return ()
    value = text(metadata[KEY])
    if len(value) > 512:
        raise ValueError("oversized MCP credential intent")
    connections = _CONNECTIONS.validate_json(value)
    if not connections or len({c.name for c in connections}) != len(connections):
        raise ValueError("invalid MCP credential references")
    tools = objects(raw.get("tools", []))
    for connection in connections:
        if connection.credential_ref is None:
            raise ValueError("missing MCP credential reference")
        expected = tool(connection, context)
        matches = [
            t for t in tools if t.get("type") == "mcp" and t.get("server_label") == connection.name
        ]
        if len(matches) != 1:
            raise ValueError("MCP credential destination or policy changed")
        native = matches[0]
        transport = object_json(native["transport"])
        expected_transport = object_json(expected["transport"])
        if (
            transport.get("type") != "http"
            or transport.get("server_url") != expected_transport["server_url"]
            or transport.get("authorization") is not None
            or transport.get("headers")
            or native.get("credential_id") is not None
            or native.get("connection_origin") not in (None, "service")
            or native.get("allowed_tools") != expected.get("allowed_tools")
            or native.get("required", False) != expected.get("required", False)
        ):
            raise ValueError("MCP credential destination or policy changed")
    return connections


def _authorization(value: object) -> str:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise ProviderError("permission", retryable=False, native_code="mcp_secret_unavailable")
    return RedactedCredential("Bearer " + value)


async def session_tools(
    scope: Scope,
    agent: Object,
    context: Context,
    resolver: MCPSecretResolver | None,
    *,
    vaults_attached: bool,
) -> list[JsonValue] | None:
    connections = decode(agent, context)
    if not connections:
        return None
    if resolver is None:
        raise context.unsupported("host_mcp_secret_resolver")
    if vaults_attached:
        raise context.unsupported("mixed_mcp_authentication")
    by_name = {c.name: c for c in connections}
    tools: list[JsonValue] = []
    for native in objects(agent["tools"]):
        connection = (
            by_name.get(str(native.get("server_label"))) if native.get("type") == "mcp" else None
        )
        if connection is None:
            tools.append(native)
            continue
        credential_ref = connection.credential_ref
        if credential_ref is None:
            raise ValueError("missing MCP credential reference")
        try:
            authorization = _authorization(await resolver(scope, credential_ref, connection.url))
        except Exception:
            raise ProviderError(
                "permission", retryable=False, native_code="mcp_secret_unavailable"
            ) from None
        transport: Object = {
            **object_json(native["transport"]),
            "authorization": authorization,
        }
        tools.append({**native, "transport": transport, "connection_origin": "service"})
    return tools
