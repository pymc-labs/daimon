"""Closed MCP intent; scoped credentials exist only in native request memory."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import NoReturn, Self, cast
from urllib.parse import urlsplit

from pydantic import JsonValue

from mux.contracts.ids import Scope
from mux.contracts.resources import MCPConnection
from mux.errors import ProviderError, UnsupportedCapability

MCPSecretResolver = Callable[[Scope, str, str], Awaitable[str]]
GCSSecretResolver = Callable[[Scope, tuple[str, ...]], Awaitable[str]]
type Object = dict[str, JsonValue]
_REF = re.compile(r"[A-Za-z0-9_.:/-]{1,128}\Z")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")
_PROFILE = "gemini.inline_reuse"


@dataclass(frozen=True)
class Binding:
    scope: Scope
    connection: MCPConnection
    resolver: MCPSecretResolver
    request_json: str
    gcs_resolver: GCSSecretResolver | None = None
    gcs_sources: tuple[str, ...] = ()


class RedactedCredential(str):
    def __repr__(self) -> str:
        return repr("[redacted]")


class BoundCredential(RedactedCredential):
    binding: Binding

    def __new__(cls, value: str, binding: Binding) -> Self:
        result = super().__new__(cls, value)
        result.binding = binding
        return result

    def __deepcopy__(self, _memo: dict[int, object]) -> Self:
        return self


def refuse(reason: str) -> NoReturn:
    raise UnsupportedCapability((reason,), _PROFILE)


def destination(value: object) -> bool:
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


def compile_mcp(connections: Sequence[MCPConnection], *, enabled: bool) -> list[JsonValue]:
    bound = any(c.credential_ref is not None for c in connections)
    if bound and (not enabled or len(connections) != 1):
        refuse("one_resolver_bound_mcp_server")
    tools: list[JsonValue] = []
    for connection in connections:
        if (
            connection.transport != "streamable_http"
            or not destination(connection.url)
            or not _NAME.fullmatch(connection.name)
        ):
            refuse("https_mcp_destination")
        tool: Object = {"type": "mcp_server", "name": connection.name, "url": connection.url}
        if bound:
            reference = connection.credential_ref
            if reference is None or not _REF.fullmatch(reference):
                refuse("mcp_credential_reference")
            policy = connection.tool_policy
            allowed = policy.get("allowed_tools")
            if (
                set(policy) != {"allowed_tools"}
                or not isinstance(allowed, list)
                or not allowed
                or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in allowed)
                or len(set(str(name) for name in allowed)) != len(allowed)
            ):
                refuse("mcp_tool_policy")
            tool["allowed_tools"] = [{"mode": "auto", "tools": allowed}]
        elif connection.tool_policy:
            refuse("mcp_credentials_or_policy")
        tools.append(tool)
    return tools


def validate_tools(value: JsonValue) -> None:
    if not isinstance(value, list):
        refuse("mcp_tool_list")
    for tool in value:
        if not isinstance(tool, dict):
            refuse("mcp_tool_shape")
        kind = tool.get("type")
        if kind in ("code_execution", "google_search", "url_context"):
            if set(tool) != {"type"}:
                refuse("mcp_adjacent_tool_fields")
        elif kind == "function":
            if (
                set(tool) != {"type", "name", "description", "parameters"}
                or not isinstance(tool["name"], str)
                or not isinstance(tool["description"], str)
                or not isinstance(tool["parameters"], dict)
            ):
                refuse("mcp_adjacent_tool_fields")
        elif kind == "mcp_server":
            if set(tool) not in (
                {"type", "name", "url"},
                {"type", "name", "url", "allowed_tools"},
            ):
                refuse("unbound_mcp_authentication")
            if (
                not destination(tool["url"])
                or not isinstance(tool["name"], str)
                or not _NAME.fullmatch(tool["name"])
            ):
                refuse("https_mcp_destination")
        else:
            refuse("mcp_adjacent_tool_type")


async def authenticated_template(
    scope: Scope,
    persisted: Object,
    expected: Object,
    connections: Sequence[MCPConnection],
    resolver: MCPSecretResolver | None,
    *,
    overrides: Object,
    gcs_secrets: GCSSecretResolver | None = None,
) -> Object:
    validate_intent(persisted, expected, connections, enabled=resolver is not None)
    if set(overrides) - {"input", "background", "store", "previous_interaction_id", "environment"}:
        refuse("mcp_request_override")
    request = {**copy.deepcopy(expected), **copy.deepcopy(overrides)}
    bound = [c for c in connections if c.credential_ref is not None]
    if not bound:
        if gcs_secrets is not None:
            refuse("gcs_mount_requires_authenticated_source")
        return request
    if len(bound) != 1 or resolver is None:
        refuse("one_resolver_bound_mcp_server")
    connection = bound[0]
    reference = connection.credential_ref
    if reference is None:
        refuse("mcp_credential_reference")
    try:
        secret = credential(await resolver(scope, reference, connection.url))
    except Exception:
        raise ProviderError(
            "permission", retryable=False, native_code="mcp_secret_unavailable"
        ) from None
    if contains_secret(request, secret):
        raise ProviderError("permission", retryable=False, native_code="mcp_secret_outside_binding")
    tools = request["tools"]
    if not isinstance(tools, list):
        refuse("mcp_tool_list")
    sources = gcs_sources(expected) if gcs_secrets is not None else ()
    if gcs_secrets is not None and not sources:
        refuse("gcs_mount_sources")
    binding = Binding(
        scope, connection, resolver, json.dumps(request, sort_keys=True), gcs_secrets, sources
    )
    if gcs_secrets is not None:
        await authenticate_storage(request, binding, bound=True)
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "mcp_server":
            tool["headers"] = {"Authorization": BoundCredential("Bearer " + secret, binding)}
    return request


def gcs_sources(request: Object) -> tuple[str, ...]:
    environment = request.get("environment")
    if not isinstance(environment, dict):
        refuse("gcs_mount_environment")
    sources = environment.get("sources")
    if not isinstance(sources, list):
        refuse("gcs_mount_sources")
    result: list[str] = []
    for source in sources:
        if not isinstance(source, dict) or source.get("type") != "gcs":
            refuse("gcs_mount_sources")
        uri = source.get("source")
        if not isinstance(uri, str) or not uri.startswith("gs://") or not uri.endswith("/"):
            refuse("gcs_mount_sources")
        result.append(uri)
    if not result or len(set(result)) != len(result):
        refuse("gcs_mount_sources")
    return tuple(result)


async def authenticate_storage(request: Object, binding: Binding, *, bound: bool) -> None:
    resolver = binding.gcs_resolver
    if resolver is None:
        return
    environment = request.get("environment")
    if isinstance(environment, str) and environment:
        refreshed: Object = {"type": "remote", "environment_id": environment}
        request["environment"] = refreshed
        environment = refreshed
    if not isinstance(environment, dict) or "network" in environment:
        refuse("gcs_mount_environment")
    try:
        secret = credential(await resolver(binding.scope, binding.gcs_sources))
    except Exception:
        raise ProviderError(
            "permission", retryable=False, native_code="gcs_secret_unavailable"
        ) from None
    if contains_secret(request, secret):
        raise ProviderError("permission", retryable=False, native_code="gcs_secret_outside_binding")
    value = "Bearer " + secret
    header = BoundCredential(value, binding) if bound else RedactedCredential(value)
    network: Object = {
        "allowlist": [{"domain": "storage.googleapis.com", "transform": {"Authorization": header}}]
    }
    environment["network"] = network


def strip_storage_authentication(request: Object, binding: Binding) -> None:
    if binding.gcs_resolver is None:
        return
    environment = request.get("environment")
    if not isinstance(environment, dict):
        refuse("gcs_mount_environment")
    network = environment.pop("network", None)
    if not isinstance(network, dict) or set(network) != {"allowlist"}:
        refuse("unbound_gcs_authentication")
    rules = network["allowlist"]
    if not isinstance(rules, list) or len(rules) != 1:
        refuse("unbound_gcs_authentication")
    rule = rules[0]
    if (
        not isinstance(rule, dict)
        or set(rule) != {"domain", "transform"}
        or rule["domain"] != "storage.googleapis.com"
    ):
        refuse("unbound_gcs_authentication")
    headers = rule["transform"]
    if not isinstance(headers, dict) or set(headers) != {"Authorization"}:
        refuse("unbound_gcs_authentication")
    header = headers["Authorization"]
    if not isinstance(header, BoundCredential) or header.binding is not binding:
        refuse("unbound_gcs_authentication")
    expected = json.loads(binding.request_json)["environment"]
    if isinstance(expected, str):
        if environment != {"type": "remote", "environment_id": expected}:
            refuse("changed_gcs_request_intent")
        request["environment"] = expected


def credential(value: object) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("unavailable MCP credential")
    return value


def contains_secret(value: JsonValue, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, list):
        return any(contains_secret(child, secret) for child in value)
    if isinstance(value, dict):
        return any(secret in key or contains_secret(child, secret) for key, child in value.items())
    return False


def stable_request(request: Object) -> Object:
    """Bind the whole owned payload, preserving only existing wrapper controls.

    Flash changes the selected model; LimitedTransport sets max_total_tokens.
    Those values keep their owning wrappers' policy, never accept nested carriers.
    """
    stable = copy.deepcopy(request)
    configuration = stable.get("agent_config")
    if not isinstance(configuration, dict):
        refuse("mcp_request_configuration")
    for key in ("model", "max_total_tokens"):
        if key not in configuration:
            continue
        value = configuration.pop(key)
        if (key == "model" and (not isinstance(value, str) or not value)) or (
            key == "max_total_tokens" and (type(value) is not int or value <= 0)
        ):
            refuse("mcp_request_configuration")
    return stable


def model_request(request: Object) -> bool:
    """A capped model request cannot carry managed execution or authentication."""
    if not {"model", "generation_config"} & request.keys():
        return False
    config = request.get("generation_config")
    if (
        set(request)
        - {
            "model",
            "generation_config",
            "tools",
            "system_instruction",
            "input",
            "background",
            "store",
            "previous_interaction_id",
        }
        or not isinstance(request.get("model"), str)
        or not request["model"]
        or request.get("tools") != []
        or not isinstance(config, dict)
        or set(config) != {"max_output_tokens"}
        or type(config.get("max_output_tokens")) is not int
        or not 0 < cast(int, config["max_output_tokens"]) <= 65_536
    ):
        refuse("capped_model_request")
    return True


async def refresh_bound_request(request: Object) -> Object:
    """Refresh driver-minted intent at each SDK dispatch, including Flash fallback.

    The marker lives only in request memory. Credential-free requests retain
    their native shape. Bare headers are never an authenticated binding.
    """
    if model_request(request):
        return request
    if set(request) - {
        "agent",
        "agent_config",
        "system_instruction",
        "environment",
        "tools",
        "input",
        "background",
        "store",
        "previous_interaction_id",
    }:
        refuse("mcp_request_fields")
    tools = request.get("tools", [])
    if not isinstance(tools, list):
        refuse("mcp_tool_list")
    marked: list[BoundCredential] = []
    for tool in tools:
        if isinstance(tool, dict):
            headers = tool.get("headers")
            if isinstance(headers, dict):
                for value in headers.values():
                    if isinstance(value, BoundCredential):
                        marked.append(value)
    if not marked:
        environment = request.get("environment")
        if isinstance(environment, dict) and "network" in environment:
            refuse("unbound_gcs_authentication")
        validate_tools(tools)
        return request
    if len(marked) != 1:
        refuse("one_resolver_bound_mcp_server")
    binding = marked[0].binding
    fresh = copy.deepcopy(request)
    fresh_tools = fresh.get("tools")
    if not isinstance(fresh_tools, list):
        refuse("mcp_tool_list")
    target: Object | None = None
    for tool in fresh_tools:
        if isinstance(tool, dict) and "headers" in tool:
            headers = tool["headers"]
            if (
                target is not None
                or not isinstance(headers, dict)
                or set(headers) != {"Authorization"}
                or not isinstance(headers["Authorization"], BoundCredential)
            ):
                refuse("unbound_mcp_authentication")
            target = tool
            del tool["headers"]
    validate_tools(fresh_tools)
    strip_storage_authentication(fresh, binding)
    expected = cast(Object, json.loads(binding.request_json))
    if (
        stable_request(fresh) != stable_request(expected)
        or target is None
        or target.get("type") != "mcp_server"
        or target.get("url") != binding.connection.url
    ):
        refuse("changed_mcp_request_intent")
    try:
        secret = credential(
            await binding.resolver(
                binding.scope, binding.connection.credential_ref or "", binding.connection.url
            )
        )
    except Exception:
        raise ProviderError(
            "permission", retryable=False, native_code="mcp_secret_unavailable"
        ) from None
    if contains_secret(fresh, secret):
        raise ProviderError("permission", retryable=False, native_code="mcp_secret_outside_binding")
    await authenticate_storage(fresh, binding, bound=False)
    target["headers"] = {"Authorization": RedactedCredential("Bearer " + secret)}
    return fresh


def validate_intent(
    persisted: Object, expected: Object, connections: Sequence[MCPConnection], *, enabled: bool
) -> None:
    validate_tools(persisted.get("tools", []))
    compiled = compile_mcp(connections, enabled=enabled)
    native = persisted.get("tools", [])
    if not isinstance(native, list):
        refuse("mcp_tool_list")
    matches = [t for t in native if isinstance(t, dict) and t.get("type") == "mcp_server"]
    if matches != compiled:
        refuse("changed_mcp_request_intent")
    if persisted != expected:
        refuse("changed_mcp_request_intent")
