"""Opt-in F2 SDK edge: explicit tool allowlist and documented native budget.

No product driver schema or default request changes. Installation is performed
only inside the admitted F2 callback, after its financial reservation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.resources.beta import AsyncAgents, AsyncSessions
from anthropic.types.beta import AgentCreateParams, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.session_create_params import SessionCreateParams
from daimon.testing.anthropic_live import MODEL, LiveRefused
from daimon.testing.conformance.default_capability import DEFAULT_TOOLS, MCP_TOOLS


def native_budget() -> dict[str, object]:
    """The documented amount is a string of integer US cents, not dollars."""
    return {"type": "limit", "max_list_cost": {"amount": "25", "currency": "USD"}}


def native_tools() -> list[dict[str, object]]:
    return [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {"name": name, "enabled": True, "permission_policy": {"type": "always_allow"}}
                for name in DEFAULT_TOOLS
            ]
            + [
                {
                    "name": "web_fetch",
                    "enabled": False,
                    "permission_policy": {"type": "always_allow"},
                },
                {
                    "name": "web_search",
                    "enabled": False,
                    "permission_policy": {"type": "always_allow"},
                },
            ],
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {"name": name, "enabled": True, "permission_policy": {"type": "always_allow"}}
                for name in sorted(MCP_TOOLS)
            ],
        },
    ]


def verify_tools(raw: Mapping[str, object]) -> None:
    """Validate effective defaults, all explicit configs and autonomous policy."""
    if raw.get("multiagent") is not None:
        raise LiveRefused("F2 delegation/advisor configuration is forbidden")
    tools = raw.get("tools")
    if not isinstance(tools, list) or len(cast(list[object], tools)) != 2:
        raise LiveRefused("F2 native tool inventory is not the exact allowlist")
    seen: set[str] = set()
    for item in cast(list[object], tools):
        if not isinstance(item, dict):
            raise LiveRefused("F2 native toolset is malformed")
        tool = cast(dict[str, object], item)
        kind = tool.get("type")
        if not isinstance(kind, str) or kind in seen:
            raise LiveRefused("F2 native toolset is duplicate or unknown")
        seen.add(kind)
        defaults = tool.get("default_config")
        if not isinstance(defaults, dict):
            raise LiveRefused("F2 native tool defaults are missing")
        defaults = cast(dict[str, object], defaults)
        if defaults.get("enabled") is not False:
            raise LiveRefused("F2 native tools must be disabled by default")
        default_permission = defaults.get("permission_policy")
        if default_permission != {"type": "always_allow"}:
            raise LiveRefused("F2 native tool policy is not confirmed")
        expected = (
            {**dict.fromkeys(DEFAULT_TOOLS, True), "web_fetch": False, "web_search": False}
            if kind == "agent_toolset_20260401"
            else dict.fromkeys(MCP_TOOLS, True)
        )
        if kind not in {"agent_toolset_20260401", "mcp_toolset"} or (
            kind == "mcp_toolset" and tool.get("mcp_server_name") != "daimon-mcp"
        ):
            raise LiveRefused("F2 native toolset is foreign")
        configs = tool.get("configs")
        if not isinstance(configs, list) or len(cast(list[object], configs)) != len(expected):
            raise LiveRefused("F2 effective tool configs are incomplete")
        actual: dict[str, bool] = {}
        for value in cast(list[object], configs):
            if not isinstance(value, dict):
                raise LiveRefused("F2 native tool config is malformed")
            config = cast(dict[str, object], value)
            name, enabled = config.get("name"), config.get("enabled")
            if not isinstance(name, str) or not isinstance(enabled, bool) or name in actual:
                raise LiveRefused("F2 native tool config is ambiguous")
            if (config.get("permission_policy") or default_permission) != {"type": "always_allow"}:
                raise LiveRefused("F2 native tool permission changed")
            actual[name] = enabled
        if actual != expected:
            raise LiveRefused("F2 effective tool allowlist changed")


def verify_budget(raw: Mapping[str, object]) -> None:
    if raw.get("budget") != native_budget():
        raise LiveRefused("F2 native budget was not exactly acknowledged")


class BoundedAgents(AsyncAgents):
    async def create(self, *args: object, **kwargs: object) -> BetaManagedAgentsAgent:
        if args or kwargs.get("model") != MODEL or "extra_body" in kwargs:
            raise LiveRefused("F2 agent request is outside the reviewed recipe")
        # The neutral driver has already applied its resource/closed-schema
        # checks. Only this explicit F2 SDK instance replaces its tool mapping.
        kwargs["tools"] = native_tools()
        native = await super().create(**cast(AgentCreateParams, kwargs))
        verify_tools(native.model_dump(mode="json"))
        return native


class BudgetedSessions(AsyncSessions):
    async def create(self, *args: object, **kwargs: object) -> BetaManagedAgentsSession:
        if args or "extra_body" in kwargs:
            raise LiveRefused("F2 session request contains an unreviewed override")
        native = await super().create(
            **cast(SessionCreateParams, kwargs), extra_body={"budget": native_budget()}
        )
        verify_budget(native.model_dump(mode="json"))
        # Before any user message, independently retrieve the accepted session.
        saved = await self.retrieve(native.id)
        if saved.id != native.id:
            raise LiveRefused("F2 native budget readback identity changed")
        verify_budget(saved.model_dump(mode="json"))
        verify_tools(saved.agent.model_dump(mode="json"))
        return native


def install_f2_bounds(client: AsyncAnthropic) -> None:
    """Public SDK resources; product/native scope and claim fences remain intact."""
    # The SDK exposes these as non-data cached properties, so an instance
    # replacement is supported at runtime; its typing stub calls them property.
    client.beta.agents = BoundedAgents(client)  # pyright: ignore[reportAttributeAccessIssue]
    client.beta.sessions = BudgetedSessions(client)  # pyright: ignore[reportAttributeAccessIssue]
