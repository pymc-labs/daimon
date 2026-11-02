"""Executable record: a failure that does not stop a turn is the agent's to tell.

No chat adapter writes Daimon's own words into or beside the agent's reply. MCP
servers MA dropped, a GitHub key restart and a lost workspace reach the agent
(`daimon.core.turn.agent_notices`, the turn controls), which decides whether to
tell the person. A failure that stops a turn, or happens when none is running,
is shown on the platform's error card instead. Replace this record, rather
than deleting it, if an adapter must ever render one of these itself.
"""

from __future__ import annotations

import inspect

import daimon.adapters.discord.lifecycle as discord_lifecycle
import daimon.adapters.slack.lifecycle as slack_lifecycle
import daimon.adapters.teams.lifecycle as teams_lifecycle
import pytest

_LIFECYCLES = (discord_lifecycle, slack_lifecycle, teams_lifecycle)


@pytest.mark.parametrize("module", _LIFECYCLES, ids=lambda m: m.__name__.split(".")[2])
def test_no_chat_adapter_renders_a_failure_the_agent_is_told_about(module: object) -> None:
    source = inspect.getsource(module)  # pyright: ignore[reportArgumentType]
    assert "mcp_failures" not in source, "a dropped MCP server is the agent's to mention"
    assert "answer_prefix" not in source, "nothing is written above the agent's answer"
