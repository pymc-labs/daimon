"""J10 offline E2E (Claude-only sweep item 1): fleet limits changed from a GPT turn.

An OpenAI or Gemini turn shows no confirmation card, so fleet config, the provider
allowlist and the channel pin were refused there outright. Now the confirm call
posts Daimon's own requester-only card (as the provider switch does) and the press
re-verifies and saves. A card that couldn't be posted never blocks asking again.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod
import pytest
from daimon.adapters.mcp.tools.fleet_config import (
    _request_fleet_config_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.switch_cards import parse_custom_id, press_switch_card
from daimon.core.turn.deps import build_turn_deps

from .test_backend_switch import active_chat as active_chat
from .test_fleet_config import config


async def test_j10_a_gpt_turn_saves_fleet_limits_through_daimons_own_card(active_chat, monkeypatch):
    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    gpt_turn = replace(auth, no_confirmation_surface=True)
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    agent()
    preview = await _request_fleet_config_impl(
        runtime,
        gpt_turn,
        agent_name="Researcher",
        config=config(max_workers=2, budget_usd="1"),
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview"
    args = dict(
        agent_name="Researcher",
        config=preview.config,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    agent()
    agent()
    awaiting = await _request_fleet_config_impl(runtime, gpt_turn, **args)
    assert awaiting.status == "awaiting_confirmation"
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_owned")
    assert await service.read_fleet_grant(auth.tenant_id, agent_id, now=datetime.now(UTC)) is None
    card = posted.await_args.kwargs["card"]
    card_id, _ = parse_custom_id(card.buttons[0].custom_id)

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
    stored = await service.read_fleet_grant(auth.tenant_id, agent_id, now=datetime.now(UTC))
    assert stored is not None and stored.max_workers == 2


async def test_j10_a_card_that_could_not_be_posted_never_blocks_asking_again(
    active_chat, monkeypatch
):
    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    gpt_turn = replace(auth, no_confirmation_surface=True)
    failing = AsyncMock(side_effect=RuntimeError("Discord is down"))
    monkeypatch.setattr(switch_card_mod, "post_switch_card", failing)
    agent()
    preview = await _request_fleet_config_impl(
        runtime,
        gpt_turn,
        agent_name="Researcher",
        config=config(max_workers=2, budget_usd="1"),
        origin_context_id=str(origin.id),
    )
    args = dict(
        agent_name="Researcher",
        config=preview.config,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    agent()
    agent()
    with pytest.raises(RuntimeError):
        await _request_fleet_config_impl(runtime, gpt_turn, **args)
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    agent()
    agent()
    again = await _request_fleet_config_impl(runtime, gpt_turn, **args)
    assert again.status == "awaiting_confirmation"
    posted.assert_awaited_once()


async def test_j10_an_admins_gpt_turn_saves_the_provider_allowlist_through_the_card(
    active_chat, monkeypatch
):
    from daimon.adapters.mcp.tools.fleet_config import (
        _request_provider_allowlist_impl,  # pyright: ignore[reportPrivateUsage]
    )

    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    gpt_turn = replace(auth, no_confirmation_surface=True)
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    preview = await _request_provider_allowlist_impl(
        runtime, gpt_turn, providers=("anthropic", "openai"), origin_context_id=str(origin.id)
    )
    awaiting = await _request_provider_allowlist_impl(
        runtime,
        gpt_turn,
        providers=preview.providers,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    assert awaiting.status == "awaiting_confirmation"
    assert "gemini" in (await service.tenant_policy(auth.tenant_id)).enabled_backends
    card_id, _ = parse_custom_id(posted.await_args.kwargs["card"].buttons[0].custom_id)
    press = await press_switch_card(
        deps=None,
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
    assert (await service.tenant_policy(auth.tenant_id)).enabled_backends == (
        "anthropic",
        "openai",
    )
