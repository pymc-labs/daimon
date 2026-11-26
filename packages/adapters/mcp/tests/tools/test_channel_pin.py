"""Pinning this chat's channel to a model: the web route's catalog and write, from chat."""

from dataclasses import replace

import pytest
from daimon.adapters.mcp.tools.provider_config import _request_channel_pin_impl
from daimon.core.channel_backend import channel_ref
from fastmcp.exceptions import ToolError

from .test_backend_switch import active_chat as active_chat
from .test_channel_pin_clear import pin, state


def gates(payload, frozen):
    for agent in (payload, frozen["agent"]):
        agent["tools"][0]["configs"] = [
            {"name": name, "enabled": True, "permission_policy": {"type": "always_ask"}}
            for name in (
                "request_agent_backend_switch",
                "request_provider_allowlist",
                "request_channel_pin",
                "request_channel_pin_clear",
            )
        ]


async def test_preview_then_confirm_pins_only_this_channel(active_chat):
    runtime, auth, origin, _, trace, payload, frozen, gate, _, _ = active_chat
    gates(payload, frozen)
    channel = channel_ref(origin.tenant_id, origin.platform, origin.parent_channel_id)
    gate()
    preview = await _request_channel_pin_impl(
        runtime, auth, model="openai:gpt-6-luna", origin_context_id=str(origin.id)
    )
    assert (preview.status, preview.pinned, preview.expected_revision) == (
        "preview",
        "OpenAI (gpt-6-luna)",
        0,
    )
    async with runtime.session_factory() as session:
        from daimon.core.channel_backend import current_backend

        assert await current_backend(session, channel) is None, "a preview changes nothing"
    gate()
    done = await _request_channel_pin_impl(
        runtime,
        auth,
        model="openai:gpt-6-luna",
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    assert done.status == "pinned"
    latest, kind = await state(runtime, channel)
    assert (latest.backend, latest.model, kind) == ("openai", "gpt-6-luna", "channel")
    assert not trace.replies and not trace.violations


async def test_a_changed_pin_refuses_a_stale_confirm(active_chat):
    runtime, auth, origin, _, trace, payload, frozen, gate, _, _ = active_chat
    gates(payload, frozen)
    gate()
    preview = await _request_channel_pin_impl(
        runtime, auth, model="openai:gpt-6-luna", origin_context_id=str(origin.id)
    )
    channel = await pin(runtime, origin, backend="gemini", model="gemini-4-argon")
    gate()
    with pytest.raises(ToolError, match="changed"):
        await _request_channel_pin_impl(
            runtime,
            auth,
            model="openai:gpt-6-luna",
            expected_revision=preview.expected_revision,
            preview_digest=preview.preview_digest,
            origin_context_id=str(origin.id),
        )
    latest, _ = await state(runtime, channel)
    assert latest.backend == "gemini", "nothing pinned"


async def test_a_claude_model_cannot_be_pinned(active_chat):
    runtime, auth, origin, _, _, payload, frozen, gate, _, _ = active_chat
    gates(payload, frozen)
    gate()
    with pytest.raises(ToolError, match="Nothing was changed"):
        await _request_channel_pin_impl(
            runtime, auth, model="anthropic:claude-opus-5-5", origin_context_id=str(origin.id)
        )


@pytest.mark.parametrize("blocked", ["non_admin", "agent_key", "ungated"])
async def test_pinning_requires_an_admin_chat_and_a_gated_session(active_chat, blocked):
    runtime, auth, origin, _, _, payload, frozen, gate, _, _ = active_chat
    if blocked == "ungated":
        gate()  # the session never gated the new tool
    else:
        gates(payload, frozen)
        gate()
        auth = (
            replace(auth, is_admin=False, is_platform_admin=False)
            if blocked == "non_admin"
            else replace(auth, agent_id=auth.chat_agent_id)
        )
    with pytest.raises(ToolError):
        await _request_channel_pin_impl(
            runtime, auth, model="openai:gpt-6-luna", origin_context_id=str(origin.id)
        )
