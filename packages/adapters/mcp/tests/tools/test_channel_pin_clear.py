"""Clearing this chat's channel provider pin: real chat-origin/session gates and the store."""

from dataclasses import replace

import pytest
from daimon.adapters.mcp.tools.fleet_config import _request_channel_pin_clear_impl
from daimon.core.channel_backend import channel_ref, current_backend, set_channel_backend
from daimon.core.stores import mux_state
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from fastmcp.exceptions import ToolError
from mux.contracts.config import BackendConfig

from .test_backend_switch import active_chat as active_chat


def gates(payload, frozen):
    for agent in (payload, frozen["agent"]):
        agent["tools"][0]["configs"] = [
            {"name": name, "enabled": True, "permission_policy": {"type": "always_ask"}}
            for name in (
                "request_agent_backend_switch",
                "request_fleet_config",
                "request_provider_allowlist",
                "request_channel_pin_clear",
            )
        ]


PROFILES = {"openai": "openai.persistent_workspace", "gemini": "gemini.inline_reuse"}


def config(backend, model):
    return BackendConfig(backend=backend, profile=PROFILES[backend], model=model)


async def pin(runtime, origin, backend="openai", model="gpt-6-luna"):
    channel = channel_ref(origin.tenant_id, origin.platform, origin.parent_channel_id)
    async with runtime.session_factory.begin() as session:
        await set_channel_backend(session, channel, config(backend, model))
    return channel


async def state(runtime, channel):
    async with runtime.session_factory() as session:
        latest = await current_backend(session, channel)
        return latest, await mux_state.config_origin(session, channel, latest.local)


async def test_no_pin_reads_as_not_pinned(active_chat):
    runtime, auth, origin, _, trace, payload, frozen, gate, _, _ = active_chat
    gates(payload, frozen)
    gate()
    info = await _request_channel_pin_clear_impl(runtime, auth, origin_context_id=str(origin.id))
    assert info.status == "not_pinned" and info.pinned is None
    assert not trace.replies and not trace.violations


async def test_preview_confirm_clears_only_this_channel(active_chat):
    runtime, auth, origin, _, trace, payload, frozen, gate, _, _ = active_chat
    gates(payload, frozen)
    channel = await pin(runtime, origin)
    other = channel_ref(origin.tenant_id, origin.platform, "another-channel")
    async with runtime.session_factory.begin() as session:
        await set_channel_backend(session, other, config("gemini", "gemini-3.8-flash"))
    gate()
    preview = await _request_channel_pin_clear_impl(runtime, auth, origin_context_id=str(origin.id))
    assert preview.status == "preview" and preview.pinned == "OpenAI (gpt-6-luna)"
    assert (await state(runtime, channel))[1] == "channel", "a preview changes nothing"
    gate()
    cleared = await _request_channel_pin_clear_impl(
        runtime,
        auth,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    assert cleared.status == "cleared"
    assert (await state(runtime, channel))[1] == "channel_clear"
    from daimon.core.stores.security_audit import list_events

    async with runtime.session_factory() as session:
        (event,) = await list_events(session, tenant_id=auth.tenant_id, tool_name="channel_backend")
    # Who cleared which channel's pin.
    assert (event.operation, event.outcome, event.target_channel_id, event.platform) == (
        "clear_pin",
        "allowed",
        origin.parent_channel_id,
        origin.platform,
    )
    assert event.account_id == auth.account_id
    assert event.platform_user_id == auth.platform_user_id
    assert event.scope == f"cleared openai pin local {preview.expected_revision}"
    assert (await state(runtime, other))[1] == "channel", "another channel's pin is untouched"
    gate()
    after = await _request_channel_pin_clear_impl(runtime, auth, origin_context_id=str(origin.id))
    assert after.status == "not_pinned"
    assert not trace.replies and not trace.violations


async def test_a_changed_pin_refuses_a_stale_confirm(active_chat):
    runtime, auth, origin, _, trace, payload, frozen, gate, _, _ = active_chat
    gates(payload, frozen)
    channel = await pin(runtime, origin)
    gate()
    preview = await _request_channel_pin_clear_impl(runtime, auth, origin_context_id=str(origin.id))
    await pin(runtime, origin, backend="gemini", model="gemini-4-argon")
    gate()
    with pytest.raises(ToolError, match="changed"):
        await _request_channel_pin_clear_impl(
            runtime,
            auth,
            expected_revision=preview.expected_revision,
            preview_digest=preview.preview_digest,
            origin_context_id=str(origin.id),
        )
    latest, origin_kind = await state(runtime, channel)
    assert (latest.backend, origin_kind) == ("gemini", "channel"), "nothing cleared"


@pytest.mark.parametrize("blocked", ["non_admin", "agent_key", "ungated"])
async def test_clearing_requires_an_admin_chat_and_a_gated_session(active_chat, blocked):
    runtime, auth, origin, _, trace, payload, frozen, gate, _, _ = active_chat
    channel = await pin(runtime, origin)
    if blocked == "non_admin":
        gates(payload, frozen)
        gate()
        auth = replace(auth, is_admin=False, is_platform_admin=False)
    elif blocked == "agent_key":
        gates(payload, frozen)
        gate()
        auth = replace(auth, agent_id=auth.chat_agent_id)
    else:
        gate()  # the session never gated the new tool
    with pytest.raises(ToolError):
        await _request_channel_pin_clear_impl(runtime, auth, origin_context_id=str(origin.id))
    assert (await state(runtime, channel))[1] == "channel"


async def test_a_session_recorded_before_the_pin_tool_cannot_gate_it(active_chat):
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.core.session_snapshot import (
        desired_snapshot,
        fingerprint_identity,
        fingerprint_mutable,
    )
    from daimon.core.stores.thread_sessions import record_snapshot

    runtime, auth, origin, _, trace, payload, frozen, gate, _, mapping = active_chat
    # The agent asks for none of these itself: only the recorded session's gates count.
    payload["tools"][0]["configs"] = []
    frozen["agent"]["tools"][0]["configs"] = []
    await pin(runtime, origin)
    snapshot = desired_snapshot(
        BetaManagedAgentsAgent.model_validate(payload),
        hidden_mcp_server_names=frozenset(),
        environment_id=frozen["environment_id"],
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        public_url=str(runtime.settings.mcp.public_url),
        confirmed_tools=frozenset(
            {"request_agent_backend_switch", "request_fleet_config", "request_provider_allowlist"}
        ),
        tool_safety=OPEN_TOOL_SAFETY,
    )
    async with runtime.session_factory.begin() as session:
        await record_snapshot(
            session,
            id=mapping.id,
            snapshot=snapshot,
            identity_fingerprint=fingerprint_identity(snapshot),
            mutable_fingerprint=fingerprint_mutable(snapshot),
        )
    gate()
    with pytest.raises(ToolError, match="cannot verify"):
        await _request_channel_pin_clear_impl(runtime, auth, origin_context_id=str(origin.id))


async def test_the_clear_rechecks_the_confirmed_pin_under_its_own_lock(active_chat):
    from daimon.core.authz import Subject
    from daimon.core.channel_backend import AgentBackendRefused, BackendActor, clear_channel_backend
    from daimon.core.stores.security_audit import list_events

    runtime, auth, origin, *_ = active_chat
    channel = await pin(runtime, origin)
    async with runtime.session_factory() as session:
        confirmed = await current_backend(session, channel)
    await pin(runtime, origin, backend="gemini", model="gemini-4-argon")
    actor = BackendActor(
        auth.tenant_id, auth.account_id, Subject(is_admin=True, platform_user_id="u")
    )
    with pytest.raises(AgentBackendRefused, match="stale"):
        async with runtime.session_factory.begin() as session:
            await clear_channel_backend(session, channel, actor=actor, expected=confirmed)
    latest, kind = await state(runtime, channel)
    assert (latest.backend, kind) == ("gemini", "channel"), "nothing cleared"
    async with runtime.session_factory() as session:
        assert not await list_events(session, tenant_id=auth.tenant_id, tool_name="channel_backend")
