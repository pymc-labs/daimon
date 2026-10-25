"""J5 offline E2E (smoke 857a82bb J5r1): another change between a switch's preview and its confirm.

"Switch it to GPT and turn off web search" in one message: when the model previewed
the switch, then turned web search off, then confirmed, the preview was stale and the
reply was "the approval card could not be shown… do not retry", so the switch never
happened. The card now re-previews the same request against the agent as it is and
posts ONE card with the fresh drops; the person approves what they see. The agent is
on Haiku; no model runs. (Adapted from dsh's stale-preview patch.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from daimon.core.confirmation import backend_switch_callbacks
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolCall,
    backend_switch_tools,
)
from daimon.core.turn.approvals import chat_tool_confirmation
from daimon.core.turn.posture import PolicyApproval

from .test_backend_switch_confirmation import owned_preview as owned_preview


def _confirm_call(preview) -> ToolCall:
    return ToolCall(
        tool_use_id="switch-call",
        server_name=DAIMON_SERVER_NAME,
        tool_name="request_agent_backend_switch",
        input={
            "agent_name": "Researcher",
            "target_backend": "openai",
            "expected_revision": preview.setting_revision,
            "preview_digest": preview.digest,
        },
    )


async def test_j5_a_change_between_preview_and_confirm_posts_one_fresh_card(owned_preview):
    from daimon.core.channel_backend import load_backend_agent

    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
    )
    posted: list[object] = []

    async def confirm(prompt):
        trace.queue(reply())
        prepared = await prepare(prompt)  # the adapter prepares the card before posting
        posted.append(prepared)
        return "approved"

    posture = chat_tool_confirmation(
        OPEN_TOOL_SAFETY,
        requester_platform_user_id="requester",
        confirm=confirm,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=backend_switch_tools(enabled=True),
    )
    assert isinstance(posture, PolicyApproval)
    # 1. Preview the switch.
    stale = await service.preview(agent, "openai")
    # 2. In the same turn, web search is turned off: the agent's setup moves.
    payload["tools"] = [
        *(payload.get("tools") or []),
        {
            "type": "agent_toolset_20260401",
            "configs": [{"name": "web_search", "enabled": False}],
        },
    ]
    # 3. The confirm with the old preview: one card, re-previewed, never "do not retry".
    result = await posture.decide(_confirm_call(stale))
    assert result.allow is True, result.deny_message
    assert len(posted) == 1
    trace.queue(reply())
    moved = await load_backend_agent(
        client, sessions, tenant_id=agent.tenant_id, agent_name="Researcher"
    )
    fresh = await service.preview(moved, "openai")
    assert fresh.digest != stale.digest
    shown = posted[0].backend_switch
    assert shown.preview_digest == fresh.digest and shown.preview_digest != stale.digest
    assert shown.expected_revision == fresh.setting_revision
    assert shown.accepted_drops == (tuple(fresh.declared_drops) or None)
    assert shown.target_backend == "openai"
