"""A session bound with daimon agent rules still proves its card from the recorded hash."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _session_gate
from daimon.core.session_snapshot import hash_tools, session_tools
from daimon.core.stores.domain import Role
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.ma_models import ma_agent

TENANT = uuid.UUID(int=1)
AUTH = AuthIdentity(tenant_id=TENANT, account_id=uuid.UUID(int=2), role=Role.USER)
POLICY = ToolSafetyPolicy(enabled=True, agent_rules=True)
AGENT = ma_agent(
    id="agent",
    tools=[
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "configs": [],
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        }
    ],
    mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": "https://mcp.example/mcp"}],
)


def _runtime() -> McpRuntime:
    settings = SimpleNamespace(
        tool_safety=POLICY,
        mcp=SimpleNamespace(public_url="https://mcp.example/mcp"),
        turn=SimpleNamespace(),
    )
    return cast(
        McpRuntime, SimpleNamespace(client=object(), session_factory=object(), settings=settings)
    )


@pytest.fixture(autouse=True)
def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_session_gate, "retrieve_agent", AsyncMock(return_value=AGENT))
    monkeypatch.setattr(
        _session_gate, "resolve_hidden_mcp_server_names", AsyncMock(return_value=frozenset())
    )
    monkeypatch.setattr(_session_gate, "backend_selection_enabled", lambda _turn: False)


def _recorded_sha(rule_tools: frozenset[str]) -> str:
    return hash_tools(
        session_tools(
            AGENT,
            frozenset(),
            tool_safety=POLICY,
            public_url="https://mcp.example/mcp",
            asks_before_publishing=True,
            daimon_rule_tools=rule_tools,
        )
    )


async def _gated(
    monkeypatch: pytest.MonkeyPatch, *, recorded: frozenset[str], now: frozenset[str]
) -> bool:
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=now))
    live = cast(Any, SimpleNamespace())
    live.effective_config = SimpleNamespace(tools_sha256=_recorded_sha(recorded))
    return await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
        _runtime(),
        AUTH,
        live,
        tool_name="publish_report",
        ma_agent_id="agent",
        reported_tools_sha256=hash_tools(AGENT.tools),
    )


@pytest.mark.parametrize(
    ("recorded", "now"),
    [
        (frozenset(), frozenset()),
        (frozenset({"hubspot_update_objects"}), frozenset({"hubspot_update_objects"})),
        (frozenset(), frozenset({"hubspot_update_objects"})),
    ],
)
async def test_the_recorded_hash_proves_the_card_with_or_without_daimon_rules(
    monkeypatch: pytest.MonkeyPatch, recorded: frozenset[str], now: frozenset[str]
) -> None:
    assert await _gated(monkeypatch, recorded=recorded, now=now)


async def test_a_hash_from_other_rules_than_either_candidate_is_not_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not await _gated(
        monkeypatch,
        recorded=frozenset({"get_session"}),
        now=frozenset({"hubspot_update_objects"}),
    )


async def test_a_rule_cleared_since_the_bind_reads_as_not_gated_until_the_next_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal direction: the recorded hash cannot be matched, so no card is assumed."""
    assert not await _gated(
        monkeypatch, recorded=frozenset({"hubspot_update_objects"}), now=frozenset()
    )
