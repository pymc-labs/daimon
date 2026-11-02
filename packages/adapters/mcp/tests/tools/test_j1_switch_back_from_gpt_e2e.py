"""J1 offline E2E (P0, Claude-only sweep item 1): switching back to Claude from a GPT root.

A GPT (or Gemini) turn has no confirmation card of its own, so the switch tool used to
be refused outright there and the agent could never come back to Claude. Now the
preview is read-only, the confirm call posts Daimon's own requester-only card in the
same thread, and the press re-verifies and applies, with the source read the same
way the preview read it, so the card's digest is the click's digest.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod
from daimon.adapters.mcp.tools.agents import (
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_backend import load_backend_agent
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.stores.backend_switch_cards import get_card
from daimon.core.switch_cards import parse_custom_id, press_switch_card
from daimon.core.turn.deps import build_turn_deps

from .test_backend_switch import _without_own_tools
from .test_backend_switch import active_chat as active_chat


async def _agent(runtime, auth, queue):
    queue()
    return await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )


async def test_j1_a_gpt_root_switches_itself_back_to_claude_with_daimons_own_card(
    active_chat, monkeypatch
):
    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    # Cheap models only (ROOT): Haiku on Claude, gpt-6-luna on GPT.
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard"}
    # 1. Researcher is on GPT (a confirmed switch from a Claude turn).
    to_gpt = {
        "agent_name": "Researcher",
        "target_backend": "openai",
        "model": "gpt-6-luna",
        "origin_context_id": str(origin.id),
    }
    gate()
    agent()
    quote = await _request_agent_backend_switch_impl(runtime, auth, **to_gpt)
    gate()
    agent()
    agent()
    await _request_agent_backend_switch_impl(
        runtime,
        auth,
        **to_gpt,
        expected_revision=quote.expected_revision,
        preview_digest=quote.preview_digest,
        accepted_drops=quote.declared_drops,
    )
    assert (await service.current(await _agent(runtime, auth, agent))).backend == "openai"

    # 2. On GPT, "switch yourself back to Claude": the preview, then the confirm call
    #    posts Daimon's card into this thread instead of switching.
    gpt_turn = replace(auth, no_confirmation_surface=True)
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    back = {
        "agent_name": "Researcher",
        "target_backend": "anthropic",
        "origin_context_id": str(origin.id),
    }
    agent()
    preview = await _request_agent_backend_switch_impl(runtime, gpt_turn, **back)
    agent()
    agent()
    awaiting = await _request_agent_backend_switch_impl(
        runtime,
        gpt_turn,
        **back,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert awaiting.status == "awaiting_confirmation"
    assert (await service.current(await _agent(runtime, auth, agent))).backend == "openai"
    card = posted.await_args.kwargs["card"]
    assert posted.await_args.kwargs["channel_id"] == origin.thread_id
    card_id, action = parse_custom_id(card.buttons[0].custom_id)
    assert action == "confirm"

    # 3. The requester presses Confirm: re-verified with the source read in this
    #    conversation (the same digest the card showed), then applied.
    deps = build_turn_deps(
        runtime.settings,
        runtime.client,
        runtime.session_factory,
        deployment_default=runtime.deployment_default,
        resolver_cache=new_resolver_cache(),
        billing_config=None,
    )
    agent()
    agent()
    press = await press_switch_card(
        deps=deps,
        sessionmaker=runtime.session_factory,
        anthropic=runtime.client,
        turn_settings=runtime.settings.turn,
        deployment_default=runtime.deployment_default,
        tenant_id=auth.tenant_id,
        card_id=card_id,
        action="confirm",
        clicker=auth.platform_user_id,
        current_admin=AsyncMock(return_value=True),
        place_word="server",
        now=datetime.now(UTC),
        message_id="card-message",
    )
    assert press is not None and press.applied, press
    assert press.text.startswith("Switched Researcher to Anthropic.")
    current = await service.current(await _agent(runtime, auth, agent))
    assert current is not None and current.backend in (None, "anthropic")
    async with runtime.session_factory() as session:
        row = await get_card(session, tenant_id=auth.tenant_id, card_id=card_id)
    assert row is not None and row.status == "applied"
