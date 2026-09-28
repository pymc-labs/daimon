"""Teams turn deps: derived from settings like the other chat adapters'."""

from __future__ import annotations

from unittest.mock import MagicMock

from daimon.adapters.teams.runtime import build_turn_deps
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing import build_fake_anthropic, make_agent_env_echo_handler


def test_turn_deps_carry_the_tool_safety_policy() -> None:
    """Without it, a Teams turn runs gated writes unconfirmed while tool safety is on."""
    settings = MagicMock()
    settings.crypto.keys = ()
    settings.mcp.public_url = None
    settings.tool_safety = ToolSafetyPolicy(enabled=True)

    deps = build_turn_deps(
        settings,
        build_fake_anthropic(make_agent_env_echo_handler()),
        MagicMock(),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        billing_config=None,
    )

    assert deps.tool_safety is settings.tool_safety, "Teams must gate writes like Discord and Slack"
