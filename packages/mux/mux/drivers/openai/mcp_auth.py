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


def _destination(value: object) -> bool:
    """Accept only a literal HTTPS destination without credential carriers."""
    if not isinstance(value, str) or any(c.isspace() for c in value) or "\\" in value:
        return False
    try:
        url = urlsplit(value)
        return (
            value.startswith("https://")
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and "%" not in url.netloc
            and "?" not in value
            and "#" not in value
            and (url.port is None or 0 < url.port <= 65535)
        )
    except ValueError:
        return False


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
    if not _destination(connection.url):
        raise context.unsupported("credential_https_mcp_destination")
    if connection.credential_ref is not None and _REF.fullmatch(connection.credential_ref) is None:
        raise context.unsupported("mcp_credential_reference")


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
    if len(authenticated) != len(connections):
        # An authenticated override may copy only resolver-bound MCP tools.
        # Anonymous-only agents retain their credential-free native requests.
        raise context.unsupported("unbound_mcp_destination")
    if len(authenticated) != 1:
        raise context.unsupported("single_bound_mcp_server")
    value = json.dumps([c.model_dump(mode="json") for c in authenticated], separators=(",", ":"))
    if len(value) > 512:
        raise context.unsupported("mcp_credential_metadata_size")
    return {KEY: value}


def _non_mcp_tool(native: Object) -> Object:
    """Rebuild only the exact native shapes admitted by this driver."""
    kind = native.get("type")
    if kind in ("web_search", "tool_search", "programmatic_tool_calling"):
        if set(native) != {"type"}:
            raise ValueError("undeclared builtin tool fields")
        return {"type": kind}
    if kind != "function" or set(native) - {"type", "name", "description", "parameters", "strict"}:
        raise ValueError("unsupported native tool shape")
    name = text(native["name"])
    if not name:
        raise ValueError("empty function name")
    result: Object = {"type": "function", "name": name}
    if "description" in native:
        if not isinstance(native["description"], str):
            raise ValueError("invalid function description")
        result["description"] = native["description"]
    result["parameters"] = object_json(native["parameters"])
    if "strict" in native:
        if not isinstance(native["strict"], bool):
            raise ValueError("invalid function strict flag")
        result["strict"] = native["strict"]
    return result


def _validate_native_auth(tools: Sequence[Object]) -> None:
    """Saved provider tools cannot contribute authentication to a session.

    Validate the complete list before resolving the first credential. Only
    session_tools may add authorization, using the scoped resolver's output.
    This also applies to anonymous agents with no credential-reference map.
    """
    for native in tools:
        if native.get("type") != "mcp":
            _non_mcp_tool(native)
            continue
        transport = object_json(native["transport"])
        if (
            set(native)
            - {
                "type",
                "server_label",
                "transport",
                "connection_origin",
                "allowed_tools",
                "required",
            }
            or set(transport) - {"type", "server_url"}
            or transport.get("type") != "http"
            or not _destination(transport.get("server_url"))
            or native.get("connection_origin") not in (None, "service")
        ):
            # Never include the rejected value or provider payload in errors.
            raise ValueError("native MCP authentication is not host-resolver bound")


def decode(raw: Object, context: Context) -> tuple[MCPConnection, ...]:
    tools = objects(raw.get("tools", []))
    _validate_native_auth(tools)
    metadata = object_json(raw.get("metadata") or {})
    if KEY not in metadata:
        return ()
    value = text(metadata[KEY])
    if len(value) > 512:
        raise ValueError("oversized MCP credential intent")
    connections = _CONNECTIONS.validate_json(value)
    if len(connections) != 1:
        raise ValueError("invalid MCP credential references")
    if len([t for t in tools if t.get("type") == "mcp"]) != len(connections):
        raise ValueError("native MCP destination is not host-resolver bound")
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
    connection = connections[0]
    tools: list[JsonValue] = []
    for native in objects(agent["tools"]):
        if native.get("type") != "mcp":
            tools.append(_non_mcp_tool(native))
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
        expected = tool(connection, context)
        transport: Object = {
            **object_json(expected["transport"]),
            "authorization": authorization,
        }
        tools.append({**expected, "transport": transport, "connection_origin": "service"})
    return tools
