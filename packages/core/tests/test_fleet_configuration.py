"""The admin sees and confirms the exact root-inclusive fleet authority."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from daimon.core.confirmation import backend_switch_callbacks, prompt_for_tool_call
from daimon.core.fleet_configuration import (
    ConfigurationReference,
    FleetConfigInput,
    configured_fleet_grant,
    fleet_default_line,
    fleet_detail_lines,
    provider_preview_digest,
)
from daimon.core.neutral_resources import AgentFleetGrant
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    ToolCall,
    ToolSafetyPolicy,
    backend_switch_tools,
    decide_tool_call,
)
from mux.contracts.ids import ModelRef
from pydantic import ValidationError

from .test_backend_switch_confirmation import owned_preview as owned_preview

NOW = datetime.now(UTC)


def config(**changes):
    return FleetConfigInput.model_validate(
        {
            "max_workers": 4,
            "budget_usd": "2",
            "max_worker_seconds": 120,
            **changes,
        }
    )


def call(tool="request_fleet_config", **data):
    return ToolCall(
        tool_use_id="config-call", server_name=DAIMON_SERVER_NAME, tool_name=tool, input=data
    )


def test_expiry_is_90_days_but_worker_deadline_stays_mandatory():
    value = config().grant(
        NOW, models=(ModelRef(provider="anthropic", id="claude-haiku-5-5"),), worker_ids=None
    )
    assert value.expires_at == NOW + timedelta(days=90)
    assert value.max_worker_seconds == 120
    assert value.limits.provider_limits_usd is None
    assert value.worker_agents is None
    assert "worker_agents" not in value.model_dump(mode="json")
    with pytest.raises(ValueError, match="worker deadline"):
        config(max_worker_seconds=None).grant(
            NOW, models=(ModelRef(provider="anthropic", id="claude-haiku-5-5"),), worker_ids=None
        )
    with pytest.raises(ValidationError):
        config(expires_at=NOW.replace(tzinfo=None)).grant(
            NOW, models=(ModelRef(provider="anthropic", id="claude-haiku-5-5"),), worker_ids=None
        )


@pytest.mark.parametrize("agents", ["any", [str(uuid4())]])
def test_worker_agent_field_roundtrips_without_widening_legacy_grants(agents):
    value = config().grant(
        NOW, models=(ModelRef(provider="anthropic", id="claude-haiku-5-5"),), worker_ids=None
    )
    stored = value.model_dump(mode="json")
    stored["worker_agents"] = agents
    changed = AgentFleetGrant.model_validate_json(json.dumps(stored))
    assert changed.model_dump(mode="json")["worker_agents"] == agents
    for bad in ([], ["not-a-logical-id"], [str(uuid4())] * 2):
        with pytest.raises(ValidationError):
            AgentFleetGrant.model_validate_json(json.dumps({**stored, "worker_agents": bad}))


@pytest.mark.parametrize(
    "tool", ["request_fleet_config", "request_provider_allowlist", "request_channel_pin_clear"]
)
@pytest.mark.parametrize("safety", [False, True])
def test_both_writes_always_ask_and_cannot_run_unattended(tool, safety):
    selected = backend_switch_tools(enabled=True)
    assert tool in selected
    trusted = frozenset({DAIMON_SERVER_NAME})
    for attended, digest, expected in (
        (True, None, "allow"),
        (True, "a" * 64, "ask"),
        (False, "a" * 64, "deny"),
    ):
        assert (
            decide_tool_call(
                ToolSafetyPolicy(enabled=safety, unattended_writes=("*",)),
                call(tool, preview_digest=digest),
                attended=attended,
                trusted_servers=trusted,
                confirmed_tools=selected,
            ).outcome
            == expected
        )
    assert not backend_switch_tools(enabled=False)


@pytest.mark.parametrize("revision", [True, "2", -1, None])
def test_fleet_confirmation_rejects_invalid_revisions_and_unfrozen_expiry(revision):
    with pytest.raises(ValidationError):
        ConfigurationReference.model_validate(
            {
                "operation": "fleet_set",
                "agent_name": "Researcher",
                "config": config(),
                "expected_revision": revision,
                "preview_digest": "a" * 64,
            }
        )
    with pytest.raises(ValidationError, match="exact expiry"):
        ConfigurationReference.model_validate(
            {
                "operation": "fleet_set",
                "agent_name": "Researcher",
                "config": config(),
                "expected_revision": 0,
                "preview_digest": "a" * 64,
            }
        )


async def test_fleet_card_reloads_effective_models_and_rechecks_permission_and_caps(owned_preview):
    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    value = config(provider_limits_usd={"openai": "2"}, expires_at=NOW + timedelta(days=90))
    grant = await configured_fleet_grant(value, service, client, sessions, agent, now=NOW)
    preview = await service.preview_fleet_grant(agent, grant, now=NOW)
    assert grant.worker_agents == "any"
    assert grant.limits.for_provider("anthropic").budget_usd == Decimal(0)
    lines = fleet_detail_lines(preview)
    assert any("Cannot spend on: anthropic" in line for line in lines)
    prompt = prompt_for_tool_call(
        call(
            agent_name="Researcher",
            config=value.model_dump(mode="json"),
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
            detail_lines=["Free and unlimited"],
        ),
        requester_platform_user_id="requester",
        now=NOW,
    )
    assert prompt.admin and prompt.configuration
    assert not prompt.detail_lines and "Free" not in prompt.model_dump_json()
    admin = AsyncMock(return_value=True)
    prepare, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=admin,
    )
    trace.queue(reply())
    prepared = await prepare(prompt)
    # The agent had no grant: the card says it runs on the default today.
    assert prepared.detail_lines == (fleet_default_line("default", revoke=False) + lines)
    assert "any on this server" in prepared.consequence
    assert "own model and settings" in prepared.consequence
    trace.queue(reply())
    assert await check(prompt, "requester") is None
    assert await check(prompt, "someone-else")
    admin.return_value = False
    trace.queue(reply())
    assert await check(prompt, "requester")
    admin.return_value = True
    await service.set_fleet_grant(
        agent,
        grant,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=NOW,
    )
    trace.queue(reply())
    assert await check(prompt, "requester")
    assert not trace.replies and not trace.violations


async def test_provider_card_is_bound_to_tenant_policy_and_current_admin(owned_preview):
    service, agent, actor, sessions, client, trace, _, _, _ = owned_preview
    policy = await service.tenant_policy(agent.tenant_id)
    providers = ("anthropic", "gemini")
    prompt = prompt_for_tool_call(
        call(
            "request_provider_allowlist",
            providers=providers,
            expected_revision=policy.revision,
            preview_digest=provider_preview_digest(policy, providers),
            summary="No risk",
            detail_lines=["Free"],
        ),
        requester_platform_user_id="requester",
        now=NOW,
    )
    admin = AsyncMock(return_value=True)
    prepare, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=admin,
    )
    prepared = await prepare(prompt)
    assert "anthropic, gemini" in prepared.consequence and "Free" not in prepared.consequence
    assert await check(prompt, "requester") is None
    admin.return_value = False
    assert await check(prompt, "requester")
    admin.return_value = True
    await service.set_tenant_allowlist(agent.tenant_id, providers, policy.revision, actor)
    assert await check(prompt, "requester")
    assert not trace.replies and not trace.violations


async def test_owner_confirmation_is_refused_if_ownership_is_removed(owned_preview):
    from daimon.core.stores import agent_owners

    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    async with sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=actor.account_id,
            source="created",
        )
    value = config(expires_at=NOW + timedelta(days=90))
    grant = await configured_fleet_grant(value, service, client, sessions, agent, now=NOW)
    preview = await service.preview_fleet_grant(agent, grant, now=NOW)
    prompt = prompt_for_tool_call(
        call(
            agent_name="Researcher",
            config=value.model_dump(mode="json"),
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
        ),
        requester_platform_user_id="requester",
        now=NOW,
    )
    prepare, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=False),
    )
    trace.queue(reply())
    assert (await prepare(prompt)).configuration
    async with sessions.begin() as session:
        await agent_owners.clear_owner(session, tenant_id=agent.tenant_id, agent_id=agent.agent_id)
    trace.queue(reply())
    assert await check(prompt, "requester")
    assert not trace.replies and not trace.violations


def fully_bounded_grant():
    return config(
        worker_agents=None,
        max_run_seconds=180,
        max_turns=2,
        max_root_turns=2,
        expires_at=NOW + timedelta(days=3),
        provider_limits_usd={"anthropic": "1", "openai": "1"},
        max_model_requests=3,
        max_input_tokens=4000,
        max_output_tokens=2000,
        native_budget_usd="0.50",
        native_root_budget_usd="0.25",
        root_cash_budget_usd="1",
    ).grant(NOW, models=(ModelRef(provider="anthropic", id="claude-haiku-5-5"),), worker_ids=None)


async def test_partial_edit_preserves_every_omitted_limit_and_original_expiry():
    from daimon.core.fleet_configuration import complete_fleet_config, fleet_config_from_grant

    before = fully_bounded_grant()
    patch = FleetConfigInput(budget_usd=Decimal("3"))
    complete = await complete_fleet_config(patch, before, AsyncMock(), tenant_id=uuid4())
    after = complete.grant(NOW + timedelta(hours=1), models=before.models, worker_ids=None)
    expected = before.model_dump(mode="json")
    expected["limits"]["budget_usd"] = "3"
    assert after.model_dump(mode="json") == expected
    assert complete.model_dump() == fleet_config_from_grant(after, worker_names=None).model_dump()
    assert complete.expires_at == before.expires_at
    assert complete.worker_agents is None


async def test_round_trip_current_config_preserves_every_ceiling():
    from daimon.core.fleet_configuration import fleet_config_from_grant

    before = fully_bounded_grant()
    current = fleet_config_from_grant(before, worker_names=None)
    after = FleetConfigInput.model_validate_json(current.model_dump_json()).grant(
        NOW + timedelta(hours=1), models=before.models, worker_ids=None
    )
    assert after == before


async def test_explicit_removal_is_retained_and_listed_on_authoritative_card():
    from daimon.core.fleet_configuration import complete_fleet_config, fleet_removal_lines

    before = fully_bounded_grant()
    patch = FleetConfigInput(
        provider_limits_usd=None,
        max_model_requests=None,
        max_input_tokens=None,
        max_output_tokens=None,
        native_budget_usd=None,
        native_root_budget_usd=None,
        root_cash_budget_usd=None,
    )
    complete = await complete_fleet_config(patch, before, AsyncMock(), tenant_id=uuid4())
    after = complete.grant(NOW, models=before.models, worker_ids=None)
    assert after.limits.provider_limits_usd is None
    lines = fleet_removal_lines(before, after)
    assert len(lines) == 7 and all(line.startswith("Removed:") for line in lines)
    from types import SimpleNamespace
    from typing import cast

    from daimon.core.stores.domain import CompatibilityPreview

    card = fleet_detail_lines(
        cast(CompatibilityPreview, SimpleNamespace(fleet_grant=after.model_dump(mode="json"))),
        previous=before,
    )
    assert all(line in card for line in lines)
    assert complete.expires_at == before.expires_at
    assert after.max_worker_seconds == before.max_worker_seconds


async def test_removing_only_one_of_the_paired_root_budgets_is_refused():
    from daimon.core.fleet_configuration import complete_fleet_config

    before = fully_bounded_grant()
    complete = await complete_fleet_config(
        FleetConfigInput(native_root_budget_usd=None), before, AsyncMock(), tenant_id=uuid4()
    )
    with pytest.raises(ValidationError):
        complete.grant(NOW, models=before.models, worker_ids=None)


async def test_channel_pin_card_binds_the_chat_channel_and_a_current_admin(owned_preview):
    from daimon.core.channel_backend import channel_ref, clear_channel_backend, set_channel_backend
    from daimon.core.fleet_configuration import channel_pin_digest
    from mux.contracts.config import BackendConfig

    service, agent, actor, sessions, client, trace, _, _, _ = owned_preview
    here = channel_ref(agent.tenant_id, "discord", "pinned-channel")
    elsewhere = channel_ref(agent.tenant_id, "discord", "never-pinned")
    async with sessions.begin() as session:
        pin = await set_channel_backend(
            session,
            here,
            BackendConfig(
                backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"
            ),
        )
    prompt = prompt_for_tool_call(
        call(
            "request_channel_pin_clear",
            expected_revision=pin.local,
            preview_digest=channel_pin_digest(pin),
            # A model-supplied channel or text is never read.
            channel_id="never-pinned",
            summary="No risk",
        ),
        requester_platform_user_id="requester",
        now=NOW,
    )
    assert (
        prompt.configuration is not None and prompt.configuration.operation == "channel_pin_clear"
    )
    admin = AsyncMock(return_value=True)

    def callbacks(channel):
        return backend_switch_callbacks(
            service,
            sessionmaker=sessions,
            anthropic=client,
            tenant_id=agent.tenant_id,
            account_id=actor.account_id,
            current_admin=admin,
            pin_channel=channel,
        )

    prepare, check = callbacks(here)
    prepared = await prepare(prompt)
    assert prepared.title == "Clear this channel's provider pin?"
    assert "This channel pins OpenAI (gpt-6-luna)." in prepared.consequence
    assert "No risk" not in prepared.consequence
    assert await check(prompt, "requester") is None
    # Another chat's card, or one with no bound chat, never clears this pin.
    assert await callbacks(elsewhere)[1](prompt, "requester")
    assert await callbacks(None)[1](prompt, "requester")
    admin.return_value = False
    assert await check(prompt, "requester"), "a non-admin cannot confirm"
    admin.return_value = True
    async with sessions.begin() as session:
        await clear_channel_backend(session, here)
    assert await check(prompt, "requester"), "a cleared pin's card is stale"
    assert not trace.replies and not trace.violations


async def test_an_owners_card_can_not_raise_fleet_limits_above_the_default(owned_preview):
    """The Confirm path: a non-admin owner's raise is refused when the card is prepared."""
    from daimon.core.stores import agent_owners

    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    async with sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=actor.account_id,
            source="created",
        )
    value = config(max_workers=8, expires_at=NOW + timedelta(days=90))
    grant = await configured_fleet_grant(value, service, client, sessions, agent, now=NOW)
    preview = await service.preview_fleet_grant(agent, grant, now=NOW)
    prompt = prompt_for_tool_call(
        call(
            agent_name="Researcher",
            config=value.model_dump(mode="json"),
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
        ),
        requester_platform_user_id="requester",
        now=NOW,
    )
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=False),
    )
    trace.queue(reply())
    with pytest.raises(ValueError, match="an admin can raise them"):
        await prepare(prompt)
    # The same card from an admin prepares.
    admin_prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
    )
    trace.queue(reply())
    assert (await admin_prepare(prompt)).configuration
