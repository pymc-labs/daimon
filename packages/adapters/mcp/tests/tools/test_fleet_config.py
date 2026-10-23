"""Real chat-origin/session gates and database CAS for fleet configuration."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from daimon.adapters.mcp.tools.fleet_config import (
    _request_fleet_config_impl,
    _request_provider_allowlist_impl,
)
from daimon.core.fleet_configuration import FleetConfigInput
from daimon.core.ma_identity import derive_agent_uuid
from fastmcp.exceptions import ToolError

from .test_backend_switch import active_chat as active_chat


def config(**changes):
    return FleetConfigInput.model_validate(
        {
            "max_workers": 4,
            "budget_usd": "2",
            "max_worker_seconds": 120,
            **changes,
        }
    )


def gates(payload, frozen):
    for agent in (payload, frozen["agent"]):
        agent["tools"][0]["configs"] = [
            {"name": name, "enabled": True, "permission_policy": {"type": "always_ask"}}
            for name in (
                "request_agent_backend_switch",
                "request_fleet_config",
                "request_provider_allowlist",
            )
        ]


async def test_fleet_preview_confirm_read_replay_and_revoke(active_chat):
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, _ = active_chat
    gates(payload, frozen)
    gate()
    agent()
    before = datetime.now(UTC)
    preview = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=config(),
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview" and preview.expected_revision == 0
    assert (
        before + timedelta(days=90)
        <= preview.config.expires_at
        <= datetime.now(UTC) + timedelta(days=90)
    )
    assert any("own model and settings" in line for line in preview.details)
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_owned")
    assert await service.read_fleet_grant(auth.tenant_id, agent_id, now=datetime.now(UTC)) is None
    args = dict(
        agent_name="Researcher",
        config=preview.config,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    gate()
    agent()
    agent()
    changed = await _request_fleet_config_impl(runtime, auth, **args)
    assert changed.status == "changed"
    stored = await service.read_fleet_grant(auth.tenant_id, agent_id, now=datetime.now(UTC))
    assert stored and stored.max_workers == 4 and stored.worker_agents == "any"
    gate()
    agent()
    agent()
    with pytest.raises(ToolError, match="changed"):
        await _request_fleet_config_impl(runtime, auth, **args)
    gate()
    agent()
    current = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        origin_context_id=str(origin.id),
    )
    assert current.status == "current" and any(
        "Shared across providers" in line for line in current.details
    )
    gate()
    agent()
    revoke = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        revoke=True,
        origin_context_id=str(origin.id),
    )
    gate()
    agent()
    agent()
    done = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        revoke=True,
        origin_context_id=str(origin.id),
        expected_revision=revoke.expected_revision,
        preview_digest=revoke.preview_digest,
    )
    assert done.status == "changed"
    assert await service.read_fleet_grant(auth.tenant_id, agent_id, now=datetime.now(UTC)) is None
    events = await service.audit(auth.tenant_id, agent_id=agent_id)
    assert {event.operation for event in events} == {"fleet_grant_set", "fleet_grant_revoke"}
    assert not trace.replies and not trace.violations
    assert all(request.method == "GET" for request in trace.requests)


async def test_provider_preview_confirm_stale_and_no_backfill(active_chat):
    runtime, auth, origin, service, trace, payload, frozen, gate, _, _ = active_chat
    gates(payload, frozen)
    gate()
    preview = await _request_provider_allowlist_impl(
        runtime,
        auth,
        providers=("anthropic",),
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview"
    assert (await service.tenant_policy(auth.tenant_id)).enabled_backends == (
        "anthropic",
        "openai",
        "gemini",
    )
    args = dict(
        providers=preview.providers,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    gate()
    assert (await _request_provider_allowlist_impl(runtime, auth, **args)).status == "changed"
    assert (await service.tenant_policy(auth.tenant_id)).enabled_backends == ("anthropic",)
    gate()
    with pytest.raises(ToolError, match="changed"):
        await _request_provider_allowlist_impl(runtime, auth, **args)
    assert not trace.replies and not trace.violations


@pytest.mark.parametrize("blocked", ["non_admin", "agent_key", "ungated"])
async def test_fleet_mutation_requires_admin_chat_and_gated_session(active_chat, blocked):
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, _ = active_chat
    if blocked == "non_admin":
        auth = replace(auth, is_admin=False)
    elif blocked == "agent_key":
        auth = replace(auth, agent_id=auth.chat_agent_id)
    else:
        gate()
    with pytest.raises(ToolError):
        await _request_fleet_config_impl(
            runtime,
            auth,
            agent_name="Researcher",
            config=config(),
            origin_context_id=str(origin.id),
        )
    assert not trace.replies and not trace.violations
    assert not await service.audit(auth.tenant_id, agent_id=auth.chat_agent_id)


async def test_invalid_provider_sum_refused_and_partial_map_explained(active_chat):
    runtime, auth, origin, _, trace, payload, frozen, gate, agent, _ = active_chat
    gates(payload, frozen)
    gate()
    agent()
    with pytest.raises(ToolError, match="add up to more"):
        await _request_fleet_config_impl(
            runtime,
            auth,
            agent_name="Researcher",
            config=config(provider_limits_usd={"openai": "3"}),
            origin_context_id=str(origin.id),
        )
    gate()
    agent()
    preview = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=config(provider_limits_usd={"openai": "2"}),
        origin_context_id=str(origin.id),
    )
    assert any("Cannot spend on: anthropic" in line for line in preview.details)
    assert not trace.replies and not trace.violations


async def test_named_worker_agents_resolve_to_scoped_ids_and_card_binds_scope(active_chat):
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, _ = active_chat
    gates(payload, frozen)
    gate()
    agent()
    agent()
    preview = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=config(worker_agents=("Researcher",)),
        origin_context_id=str(origin.id),
    )
    assert any("Worker agents: Researcher" in line for line in preview.details)
    gate()
    agent()
    agent()
    agent()
    await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=preview.config,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    grant = await service.read_fleet_grant(
        auth.tenant_id, auth.chat_agent_id, now=datetime.now(UTC)
    )
    assert tuple(map(str, grant.worker_agents)) == (str(auth.chat_agent_id),)
    gate()
    agent()
    agent()
    current = await _request_fleet_config_impl(
        runtime, auth, agent_name="Researcher", origin_context_id=str(origin.id)
    )
    assert current.config.worker_agents == ("Researcher",)
    assert any("Worker agents: Researcher" in line for line in current.details)
    assert not trace.replies and not trace.violations


async def test_owner_can_configure_fleet_but_cannot_change_provider_allowlist(active_chat):
    from daimon.core.stores import agent_owners
    from daimon.core.stores.domain import Role

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, _ = active_chat
    gates(payload, frozen)
    async with runtime.session_factory.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=auth.tenant_id,
            agent_id=auth.chat_agent_id,
            owner_account_id=auth.account_id,
            source="created",
        )
    auth = replace(auth, is_admin=False, role=Role.USER)
    gate()
    agent()
    preview = await _request_fleet_config_impl(
        runtime, auth, agent_name="Researcher", config=config(), origin_context_id=str(origin.id)
    )
    gate()
    agent()
    agent()
    changed = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=preview.config,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    assert changed.status == "changed"
    with pytest.raises(ToolError):
        await _request_provider_allowlist_impl(
            runtime, auth, providers=("anthropic",), origin_context_id=str(origin.id)
        )
    assert not trace.replies and not trace.violations


@pytest.mark.parametrize("names", [(), ("",), ("Researcher", "researcher")])
def test_invalid_worker_name_lists_are_rejected(names):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        config(worker_agents=names)


async def test_pre_config_recorded_session_cannot_gate_the_new_tools(active_chat):
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.core.session_snapshot import (
        desired_snapshot,
        fingerprint_identity,
        fingerprint_mutable,
    )
    from daimon.core.stores.thread_sessions import record_snapshot

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    payload["tools"][0]["configs"] = []
    frozen["agent"]["tools"][0]["configs"] = []
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
        confirmed_tools=frozenset({"request_agent_backend_switch"}),
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
        await _request_fleet_config_impl(
            runtime,
            auth,
            agent_name="Researcher",
            config=config(),
            origin_context_id=str(origin.id),
        )
    assert not trace.replies and not trace.violations


async def test_natural_model_name_applies_to_agent_settings_and_leaves_fleet_unchanged(active_chat):
    from daimon.adapters.mcp.tools.agents import _request_agent_backend_switch_impl

    from .test_backend_switch import _without_own_tools

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, _ = active_chat
    _without_own_tools(payload)
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        model="ARGON",
        origin_context_id=str(origin.id),
    )
    assert preview.model == "gemini-4-argon"
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        model="argon",
        origin_context_id=str(origin.id),
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert changed.status == "changed" and changed.model == "gemini-4-argon"
    assert (
        await service.read_fleet_grant(auth.tenant_id, auth.chat_agent_id, now=datetime.now(UTC))
        is None
    )
    assert not trace.replies and not trace.violations


async def test_budget_edit_does_not_widen_legacy_worker_eligibility(active_chat):
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, _ = active_chat
    gates(payload, frozen)
    gate()
    agent()
    preview = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=config(worker_agents=None),
        origin_context_id=str(origin.id),
    )
    gate()
    agent()
    agent()
    await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=preview.config,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        origin_context_id=str(origin.id),
    )
    gate()
    agent()
    edited = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=config(budget_usd="3"),
        origin_context_id=str(origin.id),
    )
    assert edited.config.worker_agents is None
    assert any("this agent only" in line for line in edited.details)
    assert not trace.replies and not trace.violations


async def test_current_config_partial_edit_and_explicit_cap_removal(active_chat):
    from decimal import Decimal

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, _ = active_chat
    gates(payload, frozen)
    value = config(
        worker_agents=None,
        provider_limits_usd={"anthropic": "2"},
        native_budget_usd="0.50",
        expires_at=datetime.now(UTC) + timedelta(days=3),
        max_run_seconds=180,
        max_root_turns=2,
    )

    async def request(**args):
        gate()
        agent()
        if args.get("preview_digest"):
            agent()
        return await _request_fleet_config_impl(
            runtime, auth, agent_name="Researcher", origin_context_id=str(origin.id), **args
        )

    async def confirm(preview):
        return await request(
            config=preview.config,
            expected_revision=preview.expected_revision,
            preview_digest=preview.preview_digest,
        )

    first = await request(config=value)
    await confirm(first)
    current = await request()
    assert current.config is not None
    assert current.config.model_dump() == first.config.model_dump()
    edit = await request(config=FleetConfigInput(budget_usd=Decimal("3")))
    assert edit.config.expires_at == value.expires_at
    assert edit.config.provider_limits_usd == value.provider_limits_usd
    assert edit.config.native_budget_usd == value.native_budget_usd
    assert edit.config.max_worker_seconds == value.max_worker_seconds
    assert edit.config.max_run_seconds == value.max_run_seconds
    assert edit.config.worker_agents is None
    assert not any(line.startswith("Removed:") for line in edit.details)
    await confirm(edit)
    removed = await request(
        config=FleetConfigInput(provider_limits_usd=None, native_budget_usd=None)
    )
    assert any(line.startswith("Removed: provider caps") for line in removed.details)
    assert any("Removed: provider spending cap per worker" in line for line in removed.details)
    await confirm(removed)
    stored = await service.read_fleet_grant(
        auth.tenant_id, auth.chat_agent_id, now=datetime.now(UTC)
    )
    assert stored.limits.provider_limits_usd is None and stored.limits.native_budget_usd is None
    assert stored.limits.budget_usd == Decimal("3") and stored.expires_at == value.expires_at
    assert not trace.replies and not trace.violations


async def test_fleet_preview_checks_the_target_agent_not_the_channel_default(
    active_chat, monkeypatch
):
    """The target (Researcher) is not this deployment's default agent (internal-test)."""
    from daimon.core.scope import DeploymentDefault
    from daimon.core.turn import admission

    runtime, auth, origin, _, _, payload, frozen, gate, agent, _ = active_chat
    runtime = replace(
        runtime,
        deployment_default=DeploymentDefault(
            agent_name="internal-test", environment_name="default"
        ),
    )
    captured = []
    real = admission.capture_backend_source

    async def spy(deps, source, **kwargs):
        result = await real(deps, source, **kwargs)
        captured.append((source.name, result.agent_id))
        return result

    monkeypatch.setattr(admission, "capture_backend_source", spy)
    gates(payload, frozen)
    gate()
    agent()
    preview = await _request_fleet_config_impl(
        runtime,
        auth,
        agent_name="Researcher",
        config=config(),
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview"
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_owned")
    assert captured == [("Researcher", agent_id)]


@pytest.mark.parametrize("platform", ["slack", "teams", None])
def test_the_status_says_plainly_where_the_default_fleet_cant_start(platform):
    from daimon.adapters.mcp.tools.fleet_config import fleet_status_summary

    line = fleet_status_summary("default", platform)
    assert line.startswith("Fleets aren't available here yet.")
    assert "Discord" not in line and "Slack" not in line and "Teams" not in line
    assert fleet_status_summary("default", "discord").startswith("Fleet is on with the default")
    assert fleet_status_summary("custom", platform) == "Current fleet limits (custom)."
