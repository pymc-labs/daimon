"""Resolve setup identities without changing routing or starting a session."""

from __future__ import annotations

import uuid
from typing import Final

from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.errors import DaimonError

# Copy for the surfaces that open or describe a setup conversation. It lives
# here so the Discord panel, the Slack panel and the MCP outcome cannot drift:
# an agent named on one platform and the same agent named on the other must
# read identically.
SETUP_ACTION_LABEL: Final = "💬 Set up with Daimon"
EMPTY_ROSTER_COPY: Final = "No agent answers here yet. Ask Daimon to help set one up."

# Discord rejects a thread name longer than 100 characters outright, so the
# name is truncated rather than left to fail at thread creation.
_MAX_SETUP_THREAD_NAME_CHARS: Final = 100


def setup_thread_name(target_name: str | None) -> str:
    """Name the thread a setup conversation opens in.

    ``target_name`` is None before the person has chosen what to set up.
    """
    return f"Set up {target_name or 'an agent'} with Daimon"[:_MAX_SETUP_THREAD_NAME_CHARS]


def setup_target_label(agent_name: str | None) -> str:
    """Name the roster action's target within both platforms' button width."""
    label = f"💬 Set up {agent_name or 'an agent'}"
    return label if len(label) <= 30 else f"{label[:29]}…"


def shared_keys_sentence(agent_name: str) -> str:
    """Say who a stored key reaches: everyone who talks to the agent, not its owner.

    Keys are attached to the agent, not to the person who supplied them, and
    people reliably assume the opposite — so every surface that lists an
    agent's keys says this next to the list.
    """
    return f"Anyone who talks to {agent_name} can use these."


async def get_setup_agent(
    anthropic: AsyncAnthropic, *, tenant_id: uuid.UUID, ma_agent_id: str
) -> BetaManagedAgentsAgent:
    """Retrieve the exact live identity; never substitute an agent with the same name."""
    try:
        agent = await anthropic.beta.agents.retrieve(ma_agent_id)
    except APIStatusError as error:
        if error.status_code in (400, 404):
            raise DaimonError(
                "That agent no longer exists. Choose another agent for setup."
            ) from error
        raise
    if agent.archived_at is not None or agent.metadata.get(MA_METADATA_KEY_TENANT) != str(
        tenant_id
    ):
        raise DaimonError(
            "That agent is no longer available in this workspace. Choose another agent."
        )
    return agent


async def get_setup_responder(
    anthropic: AsyncAnthropic, *, tenant_id: uuid.UUID, ma_agent_id: str
) -> BetaManagedAgentsAgent:
    try:
        responder = await get_setup_agent(anthropic, tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    except DaimonError as error:
        raise DaimonError(
            "This setup conversation's Daimon responder is missing. "
            "Ask the operator to restore it, then open a new setup conversation."
        ) from error
    if (
        responder.metadata.get(MA_METADATA_KEY_MANAGED) != "true"
        or responder.metadata.get(MA_METADATA_KEY_NAME) != "daimon"
    ):
        raise DaimonError(
            "This conversation's responder is no longer the built-in Daimon. "
            "Open a new setup conversation."
        )
    return responder


async def resolve_setup_agents(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    target_ma_agent_id: str | None = None,
) -> tuple[BetaManagedAgentsAgent, BetaManagedAgentsAgent | None]:
    responders = [
        agent
        for agent in await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name="daimon")
        if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    ]
    if len(responders) != 1:
        raise DaimonError(
            "The built-in Daimon is unavailable. Ask the operator to restore it, then retry setup."
        )
    target = None
    if target_ma_agent_id is not None:
        target = await get_setup_agent(
            anthropic, tenant_id=tenant_id, ma_agent_id=target_ma_agent_id
        )
    return responders[0], target


def build_setup_opener(
    *,
    target_name: str | None,
    bot_mention: str,
) -> str:
    question = (
        f"What would you like to change about {target_name}?"
        if target_name is not None
        else "Which agent would you like to set up?"
    )
    return f"{question}\nMention {bot_mention} to reply."
