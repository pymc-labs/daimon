"""The same plain words on Discord, Slack and Teams, a blank line between lines.

Each adapter keeps its own copy of these refusals (none may import another),
so this is the guard that they stay word for word the same.
"""

from __future__ import annotations

from daimon.adapters.discord.agent_setup import add_skill as discord_add_skill
from daimon.adapters.discord.agent_setup import authz as discord_authz
from daimon.adapters.discord.agent_setup.mcp_access import CODING_TOOLS_REFUSAL
from daimon.adapters.slack import agent_policy as slack_policy
from daimon.adapters.slack.agent_setup.coding_tools import NEEDS_ADMIN_MESSAGE
from daimon.adapters.teams import add_skill as teams_add_skill
from daimon.adapters.teams import credential_requests as teams_requests
from daimon.adapters.teams import setup_panel as teams_setup

STARTING = (
    "You can't change a starting agent.\n\n"
    "Ask me to make you a new agent, or ask an admin to copy this one."
)
SHARED = (
    "Other people use this agent. Changing its repo or keys needs an admin.\n\n"
    "Ask me to draft a request for an admin, or to make you a new agent."
)
SHARED_SKILLS = (
    "Other people use this agent. Adding skills needs an admin.\n\n"
    "Ask me to draft a request for an admin, or to make you a new agent."
)
CODING_TOOLS = (
    "You can't create this token.\n\n"
    'Ask an admin to open this agent\'s Details and press "Use from your coding tools".'
)


def test_starting_agent_refusal_is_the_same_everywhere() -> None:
    assert {
        slack_policy.MANAGED_AGENT_MESSAGE,
        discord_authz._SYSTEM_AGENT_MESSAGE,  # pyright: ignore[reportPrivateUsage]
        discord_add_skill.BUILT_IN_MESSAGE,
        teams_add_skill.BUILT_IN,
    } == {STARTING}


def test_shared_agent_refusals_are_the_same_everywhere() -> None:
    assert {
        slack_policy.SHARED_AGENT_MESSAGE,
        discord_authz._SHARED_AGENT_MESSAGE,  # pyright: ignore[reportPrivateUsage]
        teams_requests._SHARED_AGENT,  # pyright: ignore[reportPrivateUsage]
    } == {SHARED}
    assert {
        slack_policy.SHARED_AGENT_SKILLS_MESSAGE,
        teams_requests._SHARED_AGENT_SKILLS,  # pyright: ignore[reportPrivateUsage]
    } == {SHARED_SKILLS}


def test_coding_tools_refusal_is_the_same_everywhere() -> None:
    assert {CODING_TOOLS_REFUSAL, NEEDS_ADMIN_MESSAGE, teams_setup.NEEDS_ADMIN} == {CODING_TOOLS}
