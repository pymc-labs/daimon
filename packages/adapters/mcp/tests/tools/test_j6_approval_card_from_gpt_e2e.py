"""J6 offline E2E (Claude-only sweep item 2): publishing and add_skill from a GPT turn.

A GPT or Gemini turn can't hold a tool call for the person's Approve, so publishing
and add_skill were refused there outright. Now publishing runs without a card where
the agent may publish without asking; where it must ask (or for add_skill), Daimon
posts a requester-only Approve card for that exact call, nothing runs, and once the
person approves, the agent's identical retry runs once.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod
import pytest
from daimon.adapters.mcp.tools._channel_policy import require_publishable
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.tool_approvals import (
    args_digest,
    parse_custom_id,
    pending_call,
    press_tool_approval,
)
from fastmcp.exceptions import ToolError

from .test_backend_switch import active_chat as active_chat

PUBLISH = "publish_report"


def _calling(tool: str, arguments: dict[str, object]):
    """What the MCP middleware records for a gated call on a provider turn."""
    return pending_call.set((tool, args_digest(tool, arguments), arguments))


async def _approve(runtime, auth, posted) -> None:
    card_id, action = parse_custom_id(posted.await_args.kwargs["card"].buttons[0].custom_id)
    assert action == "approve"
    press = await press_tool_approval(
        sessionmaker=runtime.session_factory,
        tenant_id=auth.tenant_id,
        card_id=card_id,
        action="approve",
        clicker=auth.platform_user_id,
        message_id="card-message",
        now=datetime.now(UTC),
    )
    assert press is not None and press.applied, press


async def test_j6_a_gpt_turn_publishes_without_a_card_where_no_approval_is_needed(active_chat):
    runtime, auth, origin, *_ = active_chat
    gpt_turn = replace(auth, no_confirmation_surface=True)
    recorded = _calling(PUBLISH, {"title": "Q3 notes", "origin_context_id": str(origin.id)})
    try:
        await require_publishable(
            runtime, gpt_turn, tool_name=PUBLISH, origin_context_id=str(origin.id)
        )
    finally:
        pending_call.reset(recorded)


async def test_j6_a_gpt_turns_publish_asks_on_daimons_card_and_its_retry_runs_once(
    active_chat, monkeypatch
):
    from mux.testing.drivers.anthropic.ma import list_response

    runtime, auth, origin, service, trace, payload, *_ = active_chat
    # The rule check reads the tenant's agents on every call.
    trace.router.add("GET", r"/v1/agents", lambda *_: list_response([payload]))
    async with runtime.session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=auth.tenant_id,
            policy=TenantAccessPolicy(agent_rules={"Researcher": AgentRule(runs_in=("channel",))}),
        )
    gpt_turn = replace(auth, no_confirmation_surface=True)
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    call = {"title": "Q3 notes", "origin_context_id": str(origin.id)}

    async def publish(arguments) -> None:
        recorded = _calling(PUBLISH, arguments)
        try:
            await require_publishable(
                runtime, gpt_turn, tool_name=PUBLISH, origin_context_id=str(origin.id)
            )
        finally:
            pending_call.reset(recorded)

    with pytest.raises(ToolError, match="I posted an Approve card"):
        await publish(call)
    card = posted.await_args.kwargs["card"]
    assert posted.await_args.kwargs["channel_id"] == origin.thread_id
    assert card.requester_platform_user_id == auth.platform_user_id
    # Asking again before the press posts no second card.
    with pytest.raises(ToolError, match="already an Approve card waiting"):
        await publish(call)
    assert posted.await_count == 1
    await _approve(runtime, auth, posted)
    # The identical retry runs, once; another call needs its own card.
    await publish(call)
    with pytest.raises(ToolError, match="I posted an Approve card"):
        await publish(call)
    with pytest.raises(ToolError, match="already an Approve card waiting"):
        await publish(call)
    with pytest.raises(ToolError, match="I posted an Approve card"):
        await publish({**call, "title": "something else"})


async def test_j6_only_the_requester_on_the_cards_own_message_can_approve(active_chat, monkeypatch):
    runtime, auth, origin, *_ = active_chat
    gpt_turn = replace(auth, no_confirmation_surface=True)
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    recorded = _calling("add_skill", {"content_hash": "abc", "origin_context_id": str(origin.id)})
    try:
        with pytest.raises(ToolError, match="I posted an Approve card"):
            await session_card_gap(runtime, gpt_turn, origin, tool_name="add_skill")
    finally:
        pending_call.reset(recorded)
    card_id, _ = parse_custom_id(posted.await_args.kwargs["card"].buttons[0].custom_id)
    for clicker, message in (("someone-else", "card-message"), (auth.platform_user_id, "other")):
        assert (
            await press_tool_approval(
                sessionmaker=runtime.session_factory,
                tenant_id=auth.tenant_id,
                card_id=card_id,
                action="approve",
                clicker=clicker,
                message_id=message,
                now=datetime.now(UTC),
            )
            is None
        )


async def test_j6_an_add_skill_preview_on_a_gpt_turn_posts_no_card(active_chat, monkeypatch):
    runtime, auth, origin, *_ = active_chat
    gpt_turn = replace(auth, no_confirmation_surface=True)
    posted = AsyncMock(return_value="card-message")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", posted)
    # No gating field (content_hash): the middleware records nothing.
    assert await session_card_gap(runtime, gpt_turn, origin, tool_name="add_skill") is None
    posted.assert_not_awaited()


async def test_j6_a_card_that_could_not_be_posted_says_so_plainly(active_chat, monkeypatch):
    runtime, auth, origin, *_ = active_chat
    gpt_turn = replace(auth, no_confirmation_surface=True)
    monkeypatch.setattr(
        switch_card_mod, "post_switch_card", AsyncMock(side_effect=RuntimeError("boom 503"))
    )
    recorded = _calling("add_skill", {"content_hash": "abc"})
    try:
        with pytest.raises(ToolError) as unposted:
            await session_card_gap(runtime, gpt_turn, origin, tool_name="add_skill")
    finally:
        pending_call.reset(recorded)
    assert str(unposted.value) == "Couldn't post the approval card; nothing ran."
