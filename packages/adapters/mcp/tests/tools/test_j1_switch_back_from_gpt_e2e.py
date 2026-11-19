"""J1 offline E2E (P0, Claude-only sweep item 1): switching back to Claude from a GPT root.

Every switch applies on the first call, like a model change, from a Claude turn
or a GPT turn alike, and no card is posted. What the new provider can't carry is
said as information in the reply.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod
from daimon.adapters.mcp.tools.agents import (
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_backend import load_backend_agent

from .test_backend_switch import _without_own_tools
from .test_backend_switch import active_chat as active_chat


async def _agent(runtime, auth, queue):
    queue()
    return await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )


async def test_j1_a_gpt_root_switches_back_and_forth_at_once_with_no_card(active_chat, monkeypatch):
    """To GPT, back to Claude from the GPT turn, and to GPT again: each applies at once."""
    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    # Cheap models only (ROOT): Haiku on Claude, gpt-6-luna on GPT.
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard"}
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    to_gpt = {
        "agent_name": "Researcher",
        "target_backend": "openai",
        "model": "gpt-6-luna",
        "origin_context_id": str(origin.id),
    }
    agent()
    first = await _request_agent_backend_switch_impl(runtime, auth, **to_gpt)
    assert first.status == "changed" and first.declared_drops, "applied; OpenAI loses something"
    assert (await service.current(await _agent(runtime, auth, agent))).backend == "openai"

    gpt_turn = replace(auth, no_confirmation_surface=True)
    back = {
        "agent_name": "Researcher",
        "target_backend": "anthropic",
        "origin_context_id": str(origin.id),
    }
    agent()
    applied = await _request_agent_backend_switch_impl(runtime, gpt_turn, **back)
    assert applied.status == "changed", "back to Claude applies from the GPT turn"
    now = await service.current(await _agent(runtime, auth, agent))
    assert now is not None and now.backend in (None, "anthropic"), "the agent is on Claude"

    agent()
    again = await _request_agent_backend_switch_impl(runtime, gpt_turn, **to_gpt)
    assert again.status == "changed", "a lossy switch from a GPT turn applies too"
    assert "come back when you switch back" in again.summary, again.summary
    current = await service.current(await _agent(runtime, auth, agent))
    assert current is not None and current.backend == "openai", "written at once"
    posted.assert_not_awaited()
    assert not trace.replies and not trace.violations, "every scripted reply was used"
