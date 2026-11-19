"""The Discord slash-command tree keeps ``/agent-setup``."""

from __future__ import annotations

from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
from daimon.testing.shared_helpers import all_app_command_names as _all_app_command_names


def test_agent_setup_slash_is_registered() -> None:
    """``/agent-setup`` must be present on AgentSetupCog after Plan 25-03."""
    names = _all_app_command_names(AgentSetupCog)
    assert "agent-setup" in names, (
        f"/agent-setup must be registered on AgentSetupCog; got {names!r}"
    )
