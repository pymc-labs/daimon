"""J5/J10 offline E2E (latent M30): a member's change to their own agent's fleet limits.

Live, the owner asked to raise the limits and the reply was "The approval card
couldn't be shown, so nothing changed": the card's own refusal was swallowed as a
display failure. The refusal is worded for the person, so the model must hear it as
is. A lowering by the same owner prepares the card. No model runs; the agent is on
Haiku.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.core.confirmation import CardRefusal, backend_switch_callbacks, prompt_for_tool_call
from daimon.core.fleet_configuration import configured_fleet_grant
from daimon.core.stores import agent_owners
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolCall,
    backend_switch_tools,
)
from daimon.core.turn.approvals import chat_tool_confirmation
from daimon.core.turn.posture import PolicyApproval

from .test_backend_switch_confirmation import owned_preview as owned_preview
from .test_fleet_configuration import config

NOW = datetime.now(UTC)


async def _owner_card(owned_preview, **limits):
    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    async with sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=actor.account_id,
            source="created",
        )
    value = config(expires_at=NOW + timedelta(days=90), **limits)
    grant = await configured_fleet_grant(value, service, client, sessions, agent, now=NOW)
    preview = await service.preview_fleet_grant(agent, grant, now=NOW)
    call = ToolCall(
        tool_use_id="config-call",
        server_name=DAIMON_SERVER_NAME,
        tool_name="request_fleet_config",
        input={
            "agent_name": "Researcher",
            "config": value.model_dump(mode="json"),
            "expected_revision": preview.setting_revision,
            "preview_digest": preview.digest,
        },
    )
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        # A member: the agent's owner, not an admin.
        current_admin=AsyncMock(return_value=False),
    )
    return call, prepare, trace, reply


async def test_j5_a_members_raise_reaches_the_model_as_the_real_reason(owned_preview):
    call, prepare, trace, reply = await _owner_card(owned_preview, max_workers=6, budget_usd="3")

    async def confirm(prompt):
        trace.queue(reply())
        await prepare(prompt)  # the adapter prepares the card before posting it
        raise AssertionError("a raise above the default never reaches the card")

    posture = chat_tool_confirmation(
        OPEN_TOOL_SAFETY,
        requester_platform_user_id="requester",
        confirm=confirm,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=backend_switch_tools(enabled=True),
    )
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(call)
    assert result.allow is False
    said = result.deny_message or ""
    assert "Only an admin can raise fleet limits" in said, said
    assert "couldn't be shown" not in said and "could not be shown" not in said


async def test_j5_the_same_owner_may_lower_the_limits(owned_preview):
    call, prepare, trace, reply = await _owner_card(owned_preview, max_workers=2, budget_usd="1")
    prompt = prompt_for_tool_call(call, requester_platform_user_id="requester", now=NOW)
    trace.queue(reply())
    prepared = await prepare(prompt)
    assert prepared.configuration is not None


async def test_j5_a_raise_refusal_is_a_card_refusal(owned_preview):
    call, prepare, trace, reply = await _owner_card(owned_preview, max_workers=8)
    prompt = prompt_for_tool_call(call, requester_platform_user_id="requester", now=NOW)
    trace.queue(reply())
    with pytest.raises(CardRefusal, match="Only an admin can raise fleet limits"):
        await prepare(prompt)
