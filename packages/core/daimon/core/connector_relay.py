"""Relay a person's MCP connectors through Daimon's own MCP server.

A provider that cannot hold a connector's credential (OpenAI, Gemini, a
self-hosted harness) is pointed at `{public_url}/connectors/{server_key}` with
the turn's own Daimon credential. Daimon resolves the key within the turn's
tenant and agent, gates each call, and calls the connector with a token the
provider never sees. This module is the pure part: URLs, per-tool permissions,
the call verdict and the requester-keyed catalogue cache.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall
from pydantic import JsonValue

if TYPE_CHECKING:
    from daimon.core.tool_safety import ToolSafetyPolicy
    from mux.contracts.resources import AgentSpec

__all__ = [
    "CACHE_TTL_S",
    "RELAY_PATH",
    "Connector",
    "ToolsCache",
    "connectors_of",
    "daimon_destination",
    "relay_key",
    "relay_url",
    "relay_verdict",
]

RELAY_PATH: Final = "/connectors"
CACHE_TTL_S: Final = 300.0
CACHE_MAX_ENTRIES: Final = 512

Permission = Literal["auto", "ask", "disabled"]
Verdict = Literal["run", "ask", "refuse"]


def relay_url(public_url: str, key: str) -> str:
    return f"{public_url.rstrip('/')}{RELAY_PATH}/{key}"


def relay_key(url: str) -> str | None:
    """The server key a relay URL names, else None (any origin: callers check it)."""
    head, _, key = urlsplit(url).path.rstrip("/").rpartition("/")
    return key if key and head.endswith(RELAY_PATH) else None


def daimon_destination(destination: str, public_url: str) -> str | None:
    """`public_url` when `destination` is Daimon's MCP server or one of its relay URLs.

    The relay authenticates the same turn credential, so a mount resolves it
    for Daimon's own server; anything else is no Daimon destination.
    """
    base = public_url.rstrip("/")
    target = destination.rstrip("/")
    if target == base:
        return public_url
    key = relay_key(target)
    return public_url if key is not None and target == relay_url(base, key) else None


@dataclass(frozen=True, slots=True)
class Connector:
    """One source connector the relay may serve: its upstream URL and tool permissions."""

    key: str
    url: str
    default: Permission
    tools: Mapping[str, Permission] = field(default_factory=dict[str, Permission])

    def permission(self, tool: str) -> Permission:
        return self.tools.get(tool, self.default)


def _native_permission(enabled: bool | None, policy: str | None) -> Permission | None:
    if enabled is False:
        return "disabled"
    if policy == "always_allow":
        return "auto"
    # An unresolved policy is never permission to run: a person is asked.
    return "ask" if policy is not None or enabled is not None else None


def connectors_of(spec: AgentSpec) -> dict[str, Connector]:
    """Every user connector `spec` mounts with a toolset, keyed by its server name.

    A server without a toolset is not callable on its own provider, so it
    is not relayed either.
    """
    from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE, MCPToolset, agent_tools

    servers = {s.name: s.url for s in spec.mcp_servers or () if s.name != DAIMON_SERVER_NAME}
    found: dict[str, Connector] = {}
    for tool in spec.tools or ():
        if tool.kind == "mcp_toolset" and tool.name in servers:
            found[tool.name] = Connector(tool.name, servers[tool.name], tool.permission)
    for extension in spec.extensions or ():
        if extension.namespace != AGENT_TOOLS_NAMESPACE:
            continue
        for native in agent_tools(extension).tools:
            if not isinstance(native, MCPToolset) or native.mcp_server_name not in servers:
                continue
            defaults = native.default_config
            default = _native_permission(
                defaults.enabled if defaults else None,
                defaults.permission_policy.type
                if defaults and defaults.permission_policy
                else None,
            )
            tools: dict[str, Permission] = {}
            for config in native.configs or ():
                own = _native_permission(
                    config.enabled,
                    config.permission_policy.type if config.permission_policy else None,
                )
                if own is not None:
                    tools[config.name] = own
            name = native.mcp_server_name
            found[name] = Connector(name, servers[name], default or "ask", tools)
    return found


async def relay_verdict(
    policy: ToolSafetyPolicy, connector: Connector, call: ToolCall, *, attended: bool
) -> Verdict:
    """Run, card or refuse one relayed call: the agent's permission, then the operator's.

    A connector is never a trusted server. Nobody can press a card on an
    unattended turn, so there it runs only what the unattended rules allow.
    """
    from daimon.core.turn.approvals import tool_call_verdict

    permission = connector.permission(call.tool_name)
    if permission == "disabled":
        return "refuse"
    verdict = await tool_call_verdict(
        policy,
        call,
        attended=attended,
        trusted_servers=frozenset(),
        confirmed_tools=frozenset(),
        agent_rule=None,
    )
    if verdict.outcome == "deny":
        return "refuse"
    if verdict.outcome == "allow" and permission == "auto":
        return "run"
    return "ask" if attended else "refuse"


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_Owner = tuple[uuid.UUID, uuid.UUID | None, str]
Tools = tuple[dict[str, JsonValue], ...]


class ToolsCache:
    """`tools/list` per (tenant, requester, server, token): one caller's catalogue never
    lists for another, and a changed credential drops the old entry."""

    def __init__(
        self, *, ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl, self._clock = ttl_s, clock
        self._entries: OrderedDict[_Owner, tuple[str, float, Tools]] = OrderedDict()

    def get(
        self, tenant_id: uuid.UUID, requester: uuid.UUID | None, url: str, token: str
    ) -> Tools | None:
        owner = (tenant_id, requester, url)
        entry = self._entries.get(owner)
        if entry is None:
            return None
        fingerprint, stored, tools = entry
        if fingerprint != token_fingerprint(token) or self._clock() - stored > self._ttl:
            del self._entries[owner]
            return None
        self._entries.move_to_end(owner)
        return tools

    def put(
        self,
        tenant_id: uuid.UUID,
        requester: uuid.UUID | None,
        url: str,
        token: str,
        tools: Tools,
    ) -> None:
        owner = (tenant_id, requester, url)
        self._entries[owner] = (token_fingerprint(token), self._clock(), tools)
        self._entries.move_to_end(owner)
        while len(self._entries) > CACHE_MAX_ENTRIES:
            self._entries.popitem(last=False)

    def drop(self, tenant_id: uuid.UUID, requester: uuid.UUID | None, url: str) -> None:
        self._entries.pop((tenant_id, requester, url), None)
