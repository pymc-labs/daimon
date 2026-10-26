"""Root-bound worker derivation, with no provider requests on refused specs."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, FleetFacts, Place, Subject, Surface, authorize
from daimon.core.neutral_resources import (
    WorkerLimits,
    WorkerPermissions,
    WorkerSpecRefused,
    audit_worker_action,
    freeze_root_permissions,
    resolve_worker_spec,
)
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from mux.contracts.config import BackendConfig
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import (
    ChannelRef,
    ModelRef,
    ResourceRef,
    Revision,
    Scope,
    SkillRef,
    ThreadRef,
)
from mux.contracts.resources import (
    Agent,
    AgentPatch,
    AgentSpec,
    EnvironmentSpec,
    MCPConnection,
    NetworkPolicy,
    ResourceBinding,
    ToolSpec,
)
from mux.profiles import backend_selection, get_profile
from pydantic import ValidationError

NOW = datetime(2026, 10, 12, 18, tzinfo=UTC)
POLICY = TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini"))


def parent(provider="anthropic"):
    selection = backend_selection(provider)
    model = ModelRef(provider=provider, id=selection.model or "claude-haiku-5-5")
    scope = Scope(
        tenant_id=str(uuid4()),
        account_id=str(uuid4()),
        principal_id="caller",
        authorization_id="admitted-root",
    )
    root = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="channel"),
        thread_id="thread",
    )
    fleet = FleetFacts(
        scope=scope,
        root=root,
        origin_id=uuid4(),
        account_id=UUID(scope.account_id),
        run_id=uuid4(),
        place=Place(channel_id=root.thread_id, parent_channel_id=root.channel.channel_id),
        enabled=True,
        active=True,
    )
    profile = get_profile(selection.profile)
    tools = (
        ToolSpec(name="helper", kind="custom", input_schema={"type": "object", "properties": {}}),
    )
    if profile.implicit_execution:
        tools += (ToolSpec(name=profile.execution_tool, kind="builtin"),)
    spec = AgentSpec(
        name="root", model=model, system="Protected root instruction", tools=tools, skills=()
    )
    return WorkerPermissions(
        human_invocation=True,
        fleet=fleet,
        subject=Subject(platform_user_id="user"),
        agent=AgentRef.of("root"),
        spec=spec,
        base_system="Protected root instruction",
        backend_grants=(selection,),
        models=(model,),
        limits=WorkerLimits(
            budget_usd=Decimal("1"), deadline=NOW + timedelta(minutes=10), max_turns=4
        ),
        environment=EnvironmentSpec(
            name="root-box",
            network=NetworkPolicy(mode="limited", allowed_hosts=("allowed.invalid",)),
        ),
    )


def resolve(root, spec=None, **kwargs):
    return resolve_worker_spec(
        root, spec or root.spec, policy=kwargs.pop("policy", POLICY), now=NOW, **kwargs
    )


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
def test_root_bound_worker_on_each_declared_profile(provider):
    root = parent(provider)
    result = resolve(root)
    assert result.permissions.fleet.depth == 1
    assert result.permissions.fleet.scope.tenant_id == root.fleet.scope.tenant_id
    assert result.permissions.fleet.scope.account_id == root.fleet.scope.account_id
    assert result.permissions.fleet.scope.authorization_id != root.fleet.scope.authorization_id
    assert result.permissions.subject.is_admin is False
    assert result.backend == root.backend_grants[0]
    assert result.spec == root.spec
    assert result.digest == resolve(root).digest
    with pytest.raises(WorkerSpecRefused, match="worker_depth_exceeded"):
        resolve(result.permissions)


@pytest.mark.parametrize(
    "surface", tuple(surface for surface in Surface if surface is not Surface.FLEET)
)
def test_spawn_only_uses_the_fleet_surface(surface):
    root = parent()
    assert not authorize(
        POLICY,
        subject=root.subject,
        action=Action.SPAWN_WORKER,
        surface=surface,
        agent=root.agent,
        place=Place(channel_id="thread", parent_channel_id="channel"),
        fleet=root.fleet,
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"enabled": False},
        {"enabled": 1},
        {"active": False},
        {"depth": 1},
        {"depth": True},
        {"scope": Scope.platform(reason="not delegated")},
        {"account_id": uuid4()},
    ),
)
def test_disabled_worker_forged_account_and_platform_scope_fail_closed(updates):
    root = parent()
    root = replace(root, fleet=replace(root.fleet, **updates))
    with pytest.raises(WorkerSpecRefused):
        resolve(root)


@pytest.mark.parametrize(
    "policy",
    (
        TenantAccessPolicy(channel_rules={"channel": ChannelRule(writers="none")}),
        TenantAccessPolicy(agent_rules={"root": AgentRule(runs_in=("elsewhere",))}),
        TenantAccessPolicy(invoker_user_ids=("someone-else",)),
    ),
)
def test_current_invoker_writer_and_agent_rules_bind_spawning(policy):
    with pytest.raises(WorkerSpecRefused):
        resolve(parent(), policy=policy)


def test_admin_never_gets_private_surface_agent_exemption():
    root = replace(parent(), subject=Subject(is_admin=True, platform_user_id="admin"))
    with pytest.raises(WorkerSpecRefused, match="runs_elsewhere"):
        resolve(
            root, policy=TenantAccessPolicy(agent_rules={"root": AgentRule(runs_in=("elsewhere",))})
        )


@pytest.mark.parametrize(
    "field",
    ("tool", "schema", "permission", "skill", "mcp", "metadata", "effort", "extensions", "model"),
)
def test_individual_permission_escalations_refuse(field):
    root = parent()
    original = ToolSpec(
        name="helper", kind="custom", input_schema={"type": "object"}, permission="ask"
    )
    root = replace(root, spec=root.spec.model_copy(update={"tools": (original,)}))
    updates = {
        "tool": {"tools": (ToolSpec(name="new", kind="builtin"),)},
        "schema": {"tools": (original.model_copy(update={"input_schema": {"type": "string"}}),)},
        "permission": {"tools": (original.model_copy(update={"permission": "auto"}),)},
        "skill": {"skills": (SkillRef(id="foreign", version="1"),)},
        "mcp": {"mcp_servers": (MCPConnection(name="unbound", url="https://other.invalid"),)},
        "metadata": {"metadata": {"daimon_reader_source": "another-agent"}},
        "effort": {"model": root.spec.model.model_copy(update={"options": {"effort": "high"}})},
        "extensions": {
            "extensions": (ExtensionConfig(namespace="anthropic.agent_tools", version=1, value={}),)
        },
        "model": {"model": root.spec.model.model_copy(update={"id": "ungranted"})},
    }[field]
    with pytest.raises(WorkerSpecRefused):
        resolve(root, root.spec.model_copy(update=updates))


@pytest.mark.parametrize("kind", ("budget", "deadline", "max_turns"))
def test_positive_limits_cannot_be_extended(kind):
    root = parent()
    changes = {
        "budget": {"budget_usd": Decimal("1.01")},
        "deadline": {"deadline": root.limits.deadline + timedelta(seconds=1)},
        "max_turns": {"max_turns": 5},
    }[kind]
    with pytest.raises(WorkerSpecRefused, match=kind):
        resolve(root, limits=WorkerLimits.model_validate(root.limits.model_dump() | changes))


@pytest.mark.parametrize("value", (True, 0, -1))
def test_turn_counter_is_strict_and_positive(value):
    with pytest.raises(ValidationError):
        WorkerLimits(budget_usd=Decimal("1"), deadline=NOW, max_turns=value)


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), True, 1.1))
def test_budget_requires_finite_decimal(value):
    with pytest.raises(ValidationError):
        WorkerLimits(budget_usd=value, deadline=NOW, max_turns=1)


def test_expired_deadline_and_missing_timezone_refuse_before_remaining_floor():
    root = parent()
    with pytest.raises(WorkerSpecRefused, match="deadline"):
        resolve(root, limits=root.limits.model_copy(update={"deadline": NOW}))
    with pytest.raises(ValidationError):
        WorkerLimits(budget_usd=Decimal("1"), deadline=NOW.replace(tzinfo=None), max_turns=1)


@pytest.mark.parametrize("field", ("max_model_requests", "max_input_tokens", "max_output_tokens"))
@pytest.mark.parametrize("value", (True, False, 0, -1, 1.5, "2"))
def test_request_limits_refuse_nonpositive_or_inexact_values(field, value):
    with pytest.raises(ValidationError):
        WorkerLimits.model_validate(parent().limits.model_dump() | {field: value})


@pytest.mark.parametrize("field", ("max_model_requests", "max_input_tokens", "max_output_tokens"))
@pytest.mark.parametrize("replacement", (None, 3))
def test_a_worker_cannot_drop_or_raise_a_retained_request_ceiling(field, replacement):
    root = parent()
    retained = WorkerLimits.model_validate(root.limits.model_dump() | {field: 2})
    root = replace(root, limits=retained)
    narrowed = WorkerLimits.model_validate(retained.model_dump() | {field: replacement})
    with pytest.raises(WorkerSpecRefused, match=field):
        resolve(root, limits=narrowed)


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
@pytest.mark.parametrize("field", ("max_model_requests", "max_input_tokens", "max_output_tokens"))
def test_unknown_native_request_enforcement_refuses_before_worker_compilation(provider, field):
    root = parent(provider)
    limits = WorkerLimits.model_validate(root.limits.model_dump() | {field: 2})
    with pytest.raises(WorkerSpecRefused, match="budget: unsupported"):
        resolve(root, limits=limits)
    with pytest.raises(WorkerSpecRefused, match="budget: unsupported"):
        limits.require_profile(get_profile(root.backend_grants[0].profile))


def test_an_emulated_output_ceiling_cannot_claim_native_enforcement():
    root = parent()
    limits = WorkerLimits.model_validate(root.limits.model_dump() | {"max_output_tokens": 2})
    profile = get_profile(root.backend_grants[0].profile)
    profile = profile.model_copy(
        update={"support": dict(profile.support) | {"output_token_limit": "emulated"}}
    )
    with pytest.raises(WorkerSpecRefused, match="budget: unsupported"):
        limits.require_profile(profile)


@pytest.mark.parametrize("value", (True, 0, -1, 0.02, "0.02", Decimal("NaN"), Decimal("0.001")))
def test_native_budget_requires_positive_exact_cents(value):
    with pytest.raises(ValidationError):
        WorkerLimits.model_validate(parent().limits.model_dump() | {"native_budget_usd": value})


def test_native_budget_requires_single_input_and_short_deadline():
    root = parent()
    values = root.limits.model_dump() | {
        "native_budget_usd": Decimal("0.02"),
        "deadline": NOW + timedelta(seconds=600),
        "max_turns": 1,
    }
    WorkerLimits.model_validate(values).require_native_dispatch(NOW)
    for changes, field in (
        ({"deadline": NOW + timedelta(seconds=601)}, "deadline"),
        ({"deadline": NOW}, "deadline"),
        ({"max_turns": 2}, "max_turns"),
        ({"budget_usd": Decimal("0.01")}, "native_budget_usd"),
    ):
        with pytest.raises(WorkerSpecRefused, match=field):
            WorkerLimits.model_validate(values | changes).require_native_dispatch(NOW)


@pytest.mark.parametrize("provider", ["anthropic"])
def test_soft_native_budget_does_not_waive_unsupported_hard_limits(provider):
    root = parent(provider)
    values = root.limits.model_dump() | {"native_budget_usd": Decimal("0.02")}
    # Gemini has no native cap; nor has OpenAI on our project (`spend_control` -> 400).
    for unsupported in ("gemini", "openai"):
        with pytest.raises(WorkerSpecRefused, match="native_budget_usd: unsupported"):
            WorkerLimits.model_validate(values).require_profile(
                get_profile(parent(unsupported).backend_grants[0].profile)
            )
    supported = get_profile(root.backend_grants[0].profile)
    WorkerLimits.model_validate(values).require_profile(supported)
    for field in ("max_model_requests", "max_input_tokens", "max_output_tokens"):
        with pytest.raises(WorkerSpecRefused, match="budget: unsupported"):
            WorkerLimits.model_validate(values | {field: 1}).require_profile(supported)


def test_native_budget_cannot_be_dropped_or_increased_by_a_child():
    root = parent()
    limits = WorkerLimits.model_validate(
        root.limits.model_dump()
        | {
            "native_budget_usd": Decimal("0.02"),
            "max_turns": 1,
            "deadline": NOW + timedelta(seconds=120),
        }
    )
    root = replace(root, limits=limits)
    for value in (None, Decimal("0.03")):
        candidate = WorkerLimits.model_validate(limits.model_dump() | {"native_budget_usd": value})
        with pytest.raises(WorkerSpecRefused, match="native_budget_usd: not_subset"):
            resolve(root, limits=candidate)


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), True, 0.1))
def test_provider_sublimits_require_exact_finite_money(value):
    with pytest.raises(ValidationError):
        WorkerLimits.model_validate(
            parent().limits.model_dump() | {"provider_limits_usd": {"anthropic": value}}
        )


def test_worker_provider_lines_cannot_expand_or_disappear():
    root = parent()
    retained = WorkerLimits.model_validate(
        root.limits.model_dump() | {"provider_limits_usd": {"anthropic": Decimal("0.5")}}
    )
    root = replace(root, limits=retained)
    for lines in (None, {"anthropic": Decimal("0.6")}, {"openai": Decimal("0.1")}):
        limits = WorkerLimits.model_validate(retained.model_dump() | {"provider_limits_usd": lines})
        with pytest.raises(WorkerSpecRefused, match="provider_limits_usd: not_subset"):
            resolve(root, limits=limits)
    for lines in ({}, {"anthropic": Decimal(0)}):
        limits = WorkerLimits.model_validate(retained.model_dump() | {"provider_limits_usd": lines})
        with pytest.raises(WorkerSpecRefused, match="provider_limits_usd: not_subset"):
            resolve(root, limits=limits)


def test_narrower_provider_limits_are_in_the_existing_intent_digest():
    root = parent()
    broad = resolve(root)
    limits = WorkerLimits.model_validate(
        root.limits.model_dump()
        | {"budget_usd": Decimal("0.5"), "provider_limits_usd": {"anthropic": Decimal("0.5")}}
    )
    narrowed = resolve(root, limits=limits)
    assert narrowed.digest != broad.digest
    assert narrowed.permissions.limits == limits


def test_a_worker_budget_cannot_exceed_its_narrowed_provider_line():
    root = parent()
    limits = WorkerLimits.model_validate(
        root.limits.model_dump() | {"provider_limits_usd": {"anthropic": Decimal("0.5")}}
    )
    with pytest.raises(WorkerSpecRefused, match="provider_limits_usd: not_subset"):
        resolve(root, limits=limits)


def test_unrequested_request_limits_preserve_existing_serialized_intent():
    limits = parent().limits
    expected = {
        "budget_usd": Decimal("1"),
        "deadline": NOW + timedelta(minutes=10),
        "max_turns": 4,
    }
    assert limits.model_dump() == expected
    assert WorkerLimits.model_validate(limits.model_dump()).model_dump() == expected


def test_backend_allowlist_and_model_grants_are_independent():
    root = parent("openai")
    with pytest.raises(WorkerSpecRefused, match="backend"):
        resolve(root, policy=TenantAccessPolicy())
    with pytest.raises(WorkerSpecRefused, match="backend"):
        resolve(
            root,
            backend=BackendConfig(
                backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
            ),
        )


def test_hosted_implicit_shell_is_not_removed_by_tools_empty():
    root = parent("openai")
    with pytest.raises(WorkerSpecRefused, match="tools"):
        resolve(replace(root, spec=root.spec.model_copy(update={"tools": ()})))
    with pytest.raises(WorkerSpecRefused, match="sandbox"):
        resolve(replace(root, environment=None))
    with pytest.raises(WorkerSpecRefused, match="sandbox"):
        resolve(replace(root, environment=EnvironmentSpec(name="no-execution", execution="none")))


def test_environment_network_never_widens():
    root = parent()
    with pytest.raises(WorkerSpecRefused, match="sandbox"):
        resolve(
            root,
            environment=root.environment.model_copy(
                update={"network": NetworkPolicy(mode="unrestricted")}
            ),
        )
    result = resolve(
        root,
        environment=root.environment.model_copy(update={"network": NetworkPolicy(mode="none")}),
    )
    assert result.environment.network.mode == "none"


def test_resource_identity_and_native_mount_authority_are_not_fabricated():
    root = parent()
    ref = ResourceRef(
        id="file",
        kind="artifact",
        provider=root.spec.model.provider,
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    resource = ResourceBinding(id="input", kind="artifact", resource=ref, target_path="input.txt")
    root = replace(root, resources=(resource,))
    assert resolve(root, resources=(resource,)).resources == (resource,)
    with pytest.raises(WorkerSpecRefused, match="files"):
        resolve(root, resources=(resource.model_copy(update={"target_path": "../escape"}),))
    foreign = resource.model_copy(
        update={"resource": ref.model_copy(update={"tenant_id": str(uuid4())})}
    )
    with pytest.raises(WorkerSpecRefused, match="scope"):
        resolve(replace(root, resources=(foreign,)), resources=(foreign,))


@pytest.mark.parametrize(
    "url",
    (
        "https://user:pass@server.invalid",
        "https://server.invalid?token=hidden",
        "https://server.invalid#secret",
        "https://[invalid",
        "http://server.invalid",
    ),
)
def test_even_matching_parent_url_cannot_delegate_hidden_credentials(url):
    root = parent("openai")
    server = MCPConnection(
        name="server",
        url=url,
        credential_ref="bound-ref",
        tool_policy={"allowed_tools": ["read"], "required": True},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (server,)}))
    with pytest.raises(WorkerSpecRefused) as caught:
        resolve(root)
    assert url not in str(caught.value)


def test_mcp_policy_narrows_but_does_not_change_required_controls():
    root = parent("openai")
    server = MCPConnection(
        name="server",
        url="https://server.invalid",
        credential_ref="bound-ref",
        tool_policy={"allowed_tools": ["read", "write"], "required": True},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (server,)}))
    narrowed = server.model_copy(
        update={"tool_policy": {"allowed_tools": ["read"], "required": True}}
    )
    assert resolve(
        root, root.spec.model_copy(update={"mcp_servers": (narrowed,)})
    ).spec.mcp_servers == (narrowed,)
    for policy in (
        {"allowed_tools": ["read"], "required": False},
        {
            "allowed_tools": ["read"],
            "required": True,
            "headers": {"authorization": "hidden"},
        },
    ):
        with pytest.raises(WorkerSpecRefused):
            resolve(
                root,
                root.spec.model_copy(
                    update={"mcp_servers": (server.model_copy(update={"tool_policy": policy}),)}
                ),
            )


def test_clone_requires_exact_revision_and_sparse_override_does_not_mutate_source():
    root = parent()
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider=root.spec.model.provider,
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    source = Agent(ref=ref, revision=Revision(local=1), spec=root.spec, created_at=NOW)
    root = replace(root, source_agents=(ref,), source_revisions=((ref, source.revision),))
    result = resolve(root, source, overrides=AgentPatch(tools=(), system="Worker role"))
    assert result.spec.tools == ()
    assert result.spec.system == root.base_system + "\n\nWorker role"
    assert source.spec == root.spec
    assert result.source_agent == (source.ref, source.revision)
    with pytest.raises(WorkerSpecRefused, match="stale"):
        resolve(root, source.model_copy(update={"revision": Revision(local=2)}))
    with pytest.raises(WorkerSpecRefused):
        resolve(root, source, overrides=AgentPatch(model=None))


def test_current_worker_rule_cannot_be_hidden_by_root_rule():
    root = parent()
    with pytest.raises(WorkerSpecRefused, match="runs_elsewhere"):
        resolve(
            root,
            root.spec.model_copy(update={"name": "blocked"}),
            policy=TenantAccessPolicy(agent_rules={"blocked": AgentRule(runs_in=())}),
        )


@pytest.mark.parametrize("unknown", (False, True))
def test_resolver_retains_real_root_category_authority(unknown):
    root = parent()
    policy = TenantAccessPolicy(category_rules={"protected": ChannelRule(writers="none")})
    real_place = replace(
        root.fleet.place, category_id=None if unknown else "protected", category_unresolved=unknown
    )
    root = replace(root, fleet=replace(root.fleet, place=real_place))
    original = authorize(
        policy,
        subject=root.subject,
        action=Action.START_TURN,
        surface=Surface.CHANNEL,
        agent=root.agent,
        place=real_place,
    )
    assert not original and original.reason == "writers_none"
    with pytest.raises(WorkerSpecRefused, match="origin: writers_none"):
        resolve(root, policy=policy)


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
def test_unknown_network_default_is_not_an_effective_permission_ceiling(provider):
    root = parent(provider)
    root = replace(root, environment=root.environment.model_copy(update={"network": None}))
    with pytest.raises(WorkerSpecRefused, match="sandbox: unsupported"):
        resolve(root)


@pytest.mark.parametrize("control", ("network", "packages"))
def test_native_environment_incompatibility_still_requires_driver_preflight(control):
    from mux.drivers.gemini.core import compile_environment
    from mux.errors import UnsupportedCapability

    root = parent("gemini")
    env = EnvironmentSpec(
        name="root-box",
        **(
            {"network": NetworkPolicy(mode="none")}
            if control == "network"
            else {"packages": {"python": ("private-package",)}}
        ),
    )
    root = replace(root, environment=env)
    with pytest.raises(UnsupportedCapability):
        compile_environment(env)
    if env.network is None:
        with pytest.raises(WorkerSpecRefused, match="sandbox: unsupported"):
            resolve(root)
    else:
        # Known authority can be derived; this is never native launch admission.
        # The unchanged driver refuses this control before allocation or I/O.
        result = resolve(root)
        with pytest.raises(UnsupportedCapability):
            compile_environment(result.environment)


def test_derived_worker_retains_complete_root_place_and_refreshes_policy():
    root = parent()
    real_place = replace(
        root.fleet.place, category_id="category", own_dm=True, parent_unresolved=True
    )
    root = replace(root, fleet=replace(root.fleet, place=real_place))
    result = resolve(root)
    assert result.permissions.fleet.place is real_place
    assert (
        result.digest
        != resolve(
            replace(
                root, fleet=replace(root.fleet, place=replace(real_place, category_id="different"))
            )
        ).digest
    )
    with pytest.raises(WorkerSpecRefused, match="writers_none"):
        resolve(
            root,
            policy=TenantAccessPolicy(category_rules={"category": ChannelRule(writers="none")}),
        )


@pytest.mark.parametrize("updates", ({"channel_id": "other"}, {"parent_channel_id": "other"}))
def test_root_place_must_match_the_real_thread_and_channel(updates):
    root = parent()
    root = replace(root, fleet=replace(root.fleet, place=replace(root.fleet.place, **updates)))
    with pytest.raises(WorkerSpecRefused, match="fleet_origin_required"):
        resolve(root)


def test_direct_spawn_cannot_replace_the_category_facts_with_a_narrow_place():
    root = parent()
    real_place = replace(root.fleet.place, category_id="protected")
    root = replace(root, fleet=replace(root.fleet, place=real_place))
    decision = authorize(
        POLICY,
        subject=root.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=root.agent,
        place=Place(channel_id="thread", parent_channel_id="channel"),
        fleet=root.fleet,
    )
    assert not decision and decision.reason == "fleet_origin_required"


def test_nested_schema_and_input_are_not_shared_with_result():
    root = parent()
    result = resolve(root)
    assert result.spec is not root.spec
    assert result.spec.tools[0].input_schema is not root.spec.tools[0].input_schema


def test_identical_specs_cannot_share_worker_identity_between_runs():
    root = parent()
    other = replace(root, fleet=replace(root.fleet, run_id=uuid4()))
    assert resolve(root).digest != resolve(other).digest
    assert resolve(root).permissions.fleet.scope != resolve(other).permissions.fleet.scope


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
def test_neutral_confirmation_is_refused_until_its_compiler_maps_it(provider):
    root = parent(provider)
    tool = root.spec.tools[0].model_copy(update={"permission": "ask"})
    root = replace(root, spec=root.spec.model_copy(update={"tools": (tool,)}))
    with pytest.raises(WorkerSpecRefused, match="tools: unsupported"):
        resolve(root)


def test_old_profile_rows_default_to_no_implicit_execution_or_mcp_policy():
    from mux.contracts.profile import Profile

    row = get_profile(parent("openai").backend_grants[0].profile).model_dump()
    row.pop("implicit_execution")
    row.pop("mcp_policy_keys")
    restored = Profile.model_validate(row)
    assert restored.implicit_execution is False
    assert restored.mcp_policy_keys == ()


def test_unsupported_private_native_facts_never_appear_in_permission_repr():
    root = parent()
    secret = "private-native-environment-value"
    root = replace(
        root,
        environment=EnvironmentSpec(
            name="root-box",
            native_config=ExtensionConfig(
                namespace="anthropic.environment_config", version=1, value={"token": secret}
            ),
        ),
    )
    assert secret not in repr(root)
    with pytest.raises(WorkerSpecRefused) as caught:
        resolve(root)
    assert secret not in repr(caught.value)


def test_native_mcp_policy_that_is_dropped_by_compilation_refuses():
    root = parent()
    server = MCPConnection(
        name="server",
        url="https://server.invalid",
        credential_ref="ref",
        tool_policy={"allowed_tools": ["read"]},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (server,)}))
    with pytest.raises(WorkerSpecRefused, match="mcp: unsupported"):
        resolve(root)


async def test_openai_attenuated_mcp_allowlist_reaches_actual_sdk_request():
    from mux.drivers.openai._common import Context
    from mux.drivers.openai.agents import OpenAIAgents
    from mux.drivers.openai.transport import SDKTransport
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
    from mux.testing.isolated_sdk import build_async_openai

    root = parent("openai")
    server = MCPConnection(
        name="server",
        url="https://server.invalid",
        credential_ref="ref",
        tool_policy={"allowed_tools": ["read", "write"], "required": True},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (server,)}))
    narrow = server.model_copy(
        update={"tool_policy": {"allowed_tools": ["read"], "required": True}}
    )
    result = resolve(root, root.spec.model_copy(update={"tools": (), "mcp_servers": (narrow,)}))
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "POST",
            "/v1/agents",
            httpx.Response(
                200,
                json={
                    "id": "worker",
                    "name": "root",
                    "model": root.spec.model.id,
                    "created_at": 0,
                    "tools": [],
                    "metadata": {"mux_tenant": root.fleet.scope.tenant_id},
                },
            ),
        )
    )
    async with build_async_openai(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(trace.dispatch),
    ) as sdk:
        context = Context(
            SDKTransport(sdk),
            "project",
            result.backend.profile,
            lambda scope, kind, id_: scope == result.permissions.fleet.scope,
        )
        await OpenAIAgents(context).create(result.permissions.fleet.scope, result.spec, key="spawn")
    trace.assert_consumed()
    body = trace.requests[0].json()
    assert body["tools"] == [
        {
            "type": "mcp",
            "server_label": "server",
            "transport": {"type": "http", "server_url": "https://server.invalid"},
            "allowed_tools": ["read"],
            "required": True,
        }
    ]
    assert body["instructions"] == root.base_system


def test_gemini_native_allowlist_compiles_but_profile_refuses_worker_credentials():
    from mux.drivers.gemini.core import compile_agent

    root = parent("gemini")
    server = MCPConnection(
        name="server",
        url="https://server.invalid",
        credential_ref="ref",
        tool_policy={"allowed_tools": ["read", "write"]},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (server,)}))
    narrow = server.model_copy(update={"tool_policy": {"allowed_tools": ["read"]}})
    requested = root.spec.model_copy(update={"tools": (), "mcp_servers": (narrow,)})
    with pytest.raises(WorkerSpecRefused, match="mcp: unsupported"):
        resolve(root, requested)
    assert compile_agent(requested, authenticated_mcp=True)["tools"] == [
        {
            "type": "mcp_server",
            "name": "server",
            "url": "https://server.invalid",
            "allowed_tools": [{"mode": "auto", "tools": ["read"]}],
        }
    ]


def test_inherited_native_skill_cannot_cross_backend_without_provider_proof():
    root = parent()
    target = parent("gemini")
    pin = SkillRef(id="skill_anthropic_custom", source="custom", version="3")
    root = replace(
        root,
        spec=root.spec.model_copy(update={"skills": (pin,)}),
        backend_grants=(*root.backend_grants, *target.backend_grants),
        models=(*root.models, *target.models),
    )
    assert resolve(root).spec.skills == (pin,)
    with pytest.raises(WorkerSpecRefused, match="skills: unsupported"):
        resolve(root, root.spec.model_copy(update={"model": target.spec.model}))


@pytest.mark.parametrize("capability", ("skills_bundle", "tool_loop", "required_actions", "vaults"))
@pytest.mark.parametrize("support", ("unsupported", "unknown"))
def test_worker_and_preview_share_selected_profile_capability_refusals(
    monkeypatch, capability, support
):
    from daimon.core.channel_backend import compatibility_preview
    from daimon.core.stores.domain import BackendAgent, CompatibilityItem, TenantBackendPolicy
    from mux import profiles

    root = parent("openai")
    profile = get_profile(root.backend_grants[0].profile)
    limited = profile.model_copy(
        update={"core": False, "support": {**profile.support, capability: support}}
    )
    monkeypatch.setattr(profiles, "PROFILES", {**profiles.PROFILES, profile.profile_id: limited})
    if capability == "skills_bundle":
        pin = SkillRef(id="attached", source="custom", version="1")
        root = replace(root, spec=root.spec.model_copy(update={"skills": (pin,)}))
        facts = {"skills": (CompatibilityItem(name=pin.id, requires=("skills_bundle",)),)}
        field = "skills"
    else:
        connection = MCPConnection(
            name="server",
            url="https://server.invalid",
            credential_ref="ref",
            tool_policy={"allowed_tools": ["read"], "required": True},
        )
        root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (connection,)}))
        facts = {
            "mcps": (
                CompatibilityItem(
                    name="server", requires=("tool_loop", "required_actions", "vaults")
                ),
            )
        }
        field = "mcp"
    agent = BackendAgent(
        tenant_id=UUID(root.fleet.scope.tenant_id),
        agent_id=uuid4(),
        name="root",
        configuration_digest="actual",
        **facts,
    )
    preview = compatibility_preview(
        agent,
        "openai",
        TenantBackendPolicy(
            tenant_id=agent.tenant_id, revision="policy", enabled_backends=("openai",)
        ),
    )
    assert preview.unsupported_skills if field == "skills" else preview.unsupported_mcps
    with pytest.raises(WorkerSpecRefused, match=f"{field}: unsupported"):
        resolve(root)


def test_invalid_backend_override_is_a_typed_secret_free_refusal():
    root = parent("openai")
    with pytest.raises(WorkerSpecRefused, match="backend: unsupported") as caught:
        resolve(root, backend=BackendConfig(backend="openai"))
    assert caught.value.field == "backend" and caught.value.reason == "unsupported"


def test_fresh_worker_omitted_metadata_inherits_immutable_parent_stamps():
    root = parent()
    stamps = {
        "daimon_account": root.fleet.scope.account_id,
        "daimon_name": "root",
        "daimon_spec_hash": "retained-source-digest",
        "daimon_tenant": root.fleet.scope.tenant_id,
    }
    root = replace(root, spec=root.spec.model_copy(update={"metadata": stamps}))
    source = AgentSpec(name="worker", model=root.spec.model, system="Review accounting.")
    result = resolve(root, source)
    assert dict(result.spec.metadata) == stamps
    assert dict(result.permissions.spec.metadata) == stamps
    assert dict(root.spec.metadata) == stamps
    assert source.metadata is None and "metadata" not in source.model_fields_set


@pytest.mark.parametrize(
    "change",
    (None, {}, "daimon_account", "daimon_name", "daimon_spec_hash", "daimon_tenant", "extra"),
)
def test_fresh_worker_supplied_metadata_cannot_change_parent_stamps(change):
    root = parent()
    stamps = {
        "daimon_account": root.fleet.scope.account_id,
        "daimon_name": "root",
        "daimon_spec_hash": "retained-source-digest",
        "daimon_tenant": root.fleet.scope.tenant_id,
    }
    root = replace(root, spec=root.spec.model_copy(update={"metadata": stamps}))
    supplied = {**stamps, change: "changed"} if isinstance(change, str) else change
    source = AgentSpec(name="worker", model=root.spec.model, metadata=supplied)
    assert "metadata" in source.model_fields_set
    with pytest.raises(WorkerSpecRefused, match="scope: not_subset"):
        resolve(root, source)
    assert dict(root.spec.metadata) == stamps


def test_fresh_worker_supplied_exact_parent_metadata_remains_allowed():
    root = parent()
    stamps = {"daimon_tenant": root.fleet.scope.tenant_id, "daimon_name": "root"}
    root = replace(root, spec=root.spec.model_copy(update={"metadata": stamps}))
    source = AgentSpec(name="worker", model=root.spec.model, metadata=stamps)
    assert dict(resolve(root, source).spec.metadata) == stamps


@pytest.mark.parametrize("metadata", ({}, {"source_only": None}))
def test_clone_metadata_patch_preserves_unmentioned_root_stamps(metadata):
    root = parent()
    stamps = {"daimon_isolated": "true", "daimon_tenant": root.fleet.scope.tenant_id}
    root = replace(root, spec=root.spec.model_copy(update={"metadata": stamps}))
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider=root.spec.model.provider,
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    source_map = stamps if not metadata else {**stamps, "source_only": "remove-me"}
    source = Agent(
        ref=ref,
        revision=Revision(local=1),
        created_at=NOW,
        spec=root.spec.model_copy(update={"metadata": source_map}),
    )
    root = replace(root, source_agents=(ref,), source_revisions=((ref, source.revision),))
    result = resolve(root, source, overrides=AgentPatch(metadata=metadata))
    assert dict(result.spec.metadata) == stamps
    assert dict(source.spec.metadata) == source_map


@pytest.mark.parametrize(
    "metadata", (None, {"daimon_isolated": None}, {"daimon_isolated": "false"})
)
def test_clone_metadata_cannot_clear_delete_or_change_root_stamps(metadata):
    root = parent()
    root = replace(
        root, spec=root.spec.model_copy(update={"metadata": {"daimon_isolated": "true"}})
    )
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider=root.spec.model.provider,
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    source = Agent(ref=ref, revision=Revision(local=1), spec=root.spec, created_at=NOW)
    root = replace(root, source_agents=(ref,), source_revisions=((ref, source.revision),))
    with pytest.raises(WorkerSpecRefused, match="scope: not_subset"):
        resolve(root, source, overrides=AgentPatch(metadata=metadata))
    with pytest.raises(WorkerSpecRefused, match="scope: not_subset"):
        resolve(root, root.spec.model_copy(update={"metadata": None}))


def test_clone_metadata_map_clear_is_allowed_when_root_has_no_stamps():
    root = parent()
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider=root.spec.model.provider,
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    source = Agent(
        ref=ref,
        revision=Revision(local=1),
        created_at=NOW,
        spec=root.spec.model_copy(update={"metadata": {"source_only": "value"}}),
    )
    root = replace(root, source_agents=(ref,), source_revisions=((ref, source.revision),))
    assert resolve(root, source, overrides=AgentPatch(metadata=None)).spec.metadata is None
    assert dict(source.spec.metadata) == {"source_only": "value"}


async def test_actual_anthropic_clone_refuses_opaque_authority_without_extra_io():
    from daimon.testing.ma_models import ma_agent
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
    from mux.drivers.anthropic.resources.agents import AnthropicAgents
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

    root = parent()
    scope = root.fleet.scope
    native = ma_agent(
        id="source",
        name="root",
        tenant_id=scope.tenant_id,
        model=root.spec.model.id,
        system="Source role",
    )
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            "/v1/agents/source",
            httpx.Response(200, json=native.model_dump(mode="json", exclude_unset=True)),
        )
    )
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider="anthropic",
        account_scope_id="workspace",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    async with trace.client() as sdk:
        source = await AnthropicAgents(
            sdk, "workspace", ResourceAuthorization(scope, frozenset({("agent", "source")}))
        ).retrieve(scope, ref)
        root = replace(
            root,
            spec=root.spec.model_copy(update={"metadata": source.spec.metadata}),
            source_agents=(ref,),
            source_revisions=((ref, source.revision),),
        )
        with pytest.raises(WorkerSpecRefused, match="effort|extensions"):
            resolve(root, source)
        # Explicitly remove the opaque native knobs rather than guessing their
        # authority. The independent parent model remains the only model grant.
        result = resolve(
            root, source, overrides=AgentPatch(extensions=None, model=root.spec.model, tools=())
        )
        assert result.spec.system == root.base_system + "\n\nSource role"
        assert result.spec.extensions is None
        assert source.native == native.model_dump(mode="json", exclude_unset=True)
    trace.assert_consumed()
    assert [(r.method, r.path, r.body) for r in trace.requests] == [
        ("GET", "/v1/agents/source", b"")
    ]


async def test_actual_openai_clone_preserves_implicit_execution_and_refuses_added_tool():
    from mux.drivers.openai._common import Context
    from mux.drivers.openai.agents import OpenAIAgents
    from mux.drivers.openai.transport import SDKTransport
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
    from mux.testing.isolated_sdk import build_async_openai

    root = parent("openai")
    scope = root.fleet.scope
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            "/v1/agents/source",
            httpx.Response(
                200,
                json={
                    "id": "source",
                    "created_at": 0,
                    "name": "root",
                    "model": root.spec.model.id,
                    "instructions": "Source role",
                    "tools": [],
                    "metadata": {"mux_tenant": scope.tenant_id},
                },
            ),
        )
    )
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider="openai",
        account_scope_id="project",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    async with build_async_openai(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(trace.dispatch),
    ) as sdk:
        source = await OpenAIAgents(
            Context(
                SDKTransport(sdk),
                "project",
                root.backend_grants[0].profile,
                lambda requested, kind, id_: (
                    requested == scope and kind == "agent" and id_ == "source"
                ),
            )
        ).retrieve(scope, ref)
        root = replace(
            root,
            spec=root.spec.model_copy(update={"metadata": source.spec.metadata}),
            source_agents=(ref,),
            source_revisions=((ref, source.revision),),
        )
        result = resolve(root, source)
        assert result.spec.tools == (ToolSpec(name="bash", kind="builtin"),)
        with pytest.raises(WorkerSpecRefused, match="tools"):
            resolve(
                root, source, overrides=AgentPatch(tools=(ToolSpec(name="new", kind="custom"),))
            )
    trace.assert_consumed()
    assert len(trace.requests) == 1


async def test_fleet_audit_uses_real_root_identity_and_existing_tenant_store(db_session):
    from daimon.core.stores.security_audit import list_events
    from daimon.testing.factories import make_account, make_tenant

    root = parent()
    tenant = await make_tenant(db_session, id=UUID(root.fleet.scope.tenant_id))
    await make_account(db_session, id=root.fleet.account_id, tenant=tenant)
    root = replace(root, agent_id=uuid4())
    worker_id = uuid4()
    row = await audit_worker_action(
        db_session,
        root,
        worker_id=worker_id,
        operation="spawn",
        outcome="allowed",
        reason="authorized",
    )
    rows = await list_events(db_session, tenant_id=tenant.id)
    assert rows == [row]
    assert row.agent_id == root.agent_id
    assert row.account_id == root.fleet.account_id
    assert row.scope == f"fleet:{root.fleet.run_id}:worker:{worker_id}"
    assert row.turn_ref == f"origin:{root.fleet.origin_id}"
    assert row.operation == "spawn" and row.tool_name == "fleet"
    assert root.base_system not in row.model_dump_json()
    assert await list_events(db_session, tenant_id=uuid4()) == []
    with pytest.raises(WorkerSpecRefused):
        await audit_worker_action(
            db_session,
            replace(root, fleet=replace(root.fleet, account_id=uuid4())),
            worker_id=worker_id,
            operation="spawn",
            outcome="allowed",
            reason="authorized",
        )
    assert len(await list_events(db_session, tenant_id=tenant.id)) == 1


@pytest.mark.parametrize("support", ("unsupported", "unknown"))
def test_anonymous_mcp_compatibility_keeps_sdk_vault_requirement(monkeypatch, support):
    from daimon.core.channel_backend import compatibility_preview, spec_compatibility_items
    from daimon.core.stores.domain import BackendAgent, TenantBackendPolicy
    from daimon.testing.ma_models import ma_agent
    from mux import profiles

    root = parent("openai")
    connection = MCPConnection(
        name="public-server",
        url="https://public.invalid/mcp",
        tool_policy={"allowed_tools": ["read"], "required": True},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (connection,)}))
    profile = get_profile(root.backend_grants[0].profile)
    limited = profile.model_copy(
        update={"core": False, "support": {**profile.support, "vaults": support}}
    )
    monkeypatch.setattr(profiles, "PROFILES", {**profiles.PROFILES, profile.profile_id: limited})
    policy = TenantBackendPolicy(
        tenant_id=UUID(root.fleet.scope.tenant_id), revision="policy", enabled_backends=("openai",)
    )

    def preview(source):
        skills, mcps = (tuple(items) for items in spec_compatibility_items(source))
        return compatibility_preview(
            BackendAgent(
                tenant_id=policy.tenant_id,
                agent_id=uuid4(),
                name="source",
                configuration_digest="scoped",
                skills=skills,
                mcps=mcps,
            ),
            "openai",
            policy,
        )

    # A canonical anonymous connection has no credential mount to preserve.
    assert not preview(root.spec).unsupported_mcps
    # Compatibility alone does not waive the worker's native credential-binding gate.
    with pytest.raises(WorkerSpecRefused, match="mcp: unsupported"):
        resolve(root)
    authenticated = connection.model_copy(update={"credential_ref": "owned-ref"})
    assert preview(
        root.spec.model_copy(update={"mcp_servers": (authenticated,)})
    ).unsupported_mcps == ("public-server",)
    with pytest.raises(WorkerSpecRefused, match="mcp: unsupported"):
        resolve(replace(root, spec=root.spec.model_copy(update={"mcp_servers": (authenticated,)})))

    # Actual SDK metadata cannot prove credential absence, even at the same public URL.
    sdk = ma_agent(mcp_servers=[{"name": connection.name, "type": "url", "url": connection.url}])
    assert preview(sdk).unsupported_mcps == ("public-server",)


@pytest.mark.parametrize("support", ("unsupported", "unknown"))
def test_unsupported_skill_refuses_before_invalid_mcp_projection(monkeypatch, support):
    from mux import profiles

    root = parent("openai")
    profile = get_profile(root.backend_grants[0].profile)
    limited = profile.model_copy(
        update={"core": False, "support": {**profile.support, "skills_bundle": support}}
    )
    monkeypatch.setattr(profiles, "PROFILES", {**profiles.PROFILES, profile.profile_id: limited})
    root = replace(
        root,
        spec=root.spec.model_copy(
            update={
                "skills": (SkillRef(id="attached", source="custom", version="1"),),
                "mcp_servers": (MCPConnection(name="", url="https://invalid.example/mcp"),),
            }
        ),
    )
    # The source spec accepts this MCP name, but compatibility-item validation does not.
    # Preserve the existing skill refusal before inspecting the irrelevant MCP entry.
    with pytest.raises(WorkerSpecRefused, match="skills: unsupported"):
        resolve(root)


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
def test_declared_sandbox_and_memory_drops_are_explicit_narrowings(provider):
    root = parent(provider)
    executable = tuple(
        ToolSpec(name=name, kind="builtin")
        for name in ("bash", "code_execution", "read", "write", "apply_patch")
    )
    root = replace(
        root,
        spec=root.spec.model_copy(
            update={
                "tools": tuple(tool for tool in root.spec.tools or () if tool.kind != "builtin")
                + executable
            }
        ),
    )
    requested = root.spec.model_copy(update={"declared_drops": ("sandbox", "memory")})
    result = resolve(root, requested)
    assert result.declared_drops == ("memory", "sandbox")
    assert result.environment is None and result.permissions.environment is None
    assert all(
        tool.kind != "builtin"
        or tool.name not in get_profile(result.backend.profile).execution_tools
        for tool in result.spec.tools
    )
    assert result.permissions.memory_read_only is root.memory_read_only
    assert result.spec.system.startswith(root.base_system + "\n\n")
    assert (
        "no persistent memory" in result.spec.system
        and "no sandbox execution" in result.spec.system
    )
    assert result.permissions.spec.declared_drops == result.declared_drops
    assert root.spec.declared_drops == () and root.environment is not None
    assert resolve(root, requested).digest == result.digest
    assert resolve(root).digest != result.digest


@pytest.mark.parametrize(
    "invalid", [("unknown",), ("Memory",), ("sandbox ",), ("memory", "memory"), "memory"]
)
def test_declared_drop_field_is_closed_and_revalidated(invalid):
    root = parent()
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(root.spec.model_dump() | {"declared_drops": invalid})
    with pytest.raises(WorkerSpecRefused):
        resolve(root, root.spec.model_copy(update={"declared_drops": invalid}))


def test_empty_declared_drops_preserve_existing_json_and_worker_behavior():
    root = parent()
    assert "declared_drops" not in root.spec.model_dump()
    assert "declared_drops" not in AgentPatch().model_dump()
    result = resolve(root)
    assert result.declared_drops == () and result.spec == root.spec
    assert result.environment == root.environment and result.permissions.memory_read_only


@pytest.mark.parametrize("connections", [None, ()])
@pytest.mark.parametrize("extensions", [None, ()])
def test_native_ceiling_normalization_preserves_neutral_field_presence(connections, extensions):
    from daimon.core.turn.runtime_manifest import worker_spec_ceiling

    spec = parent().spec.model_copy(update={"mcp_servers": connections, "extensions": extensions})
    normalized, excluded = worker_spec_ceiling(spec)
    assert normalized == spec
    assert normalized.model_fields_set == spec.model_fields_set
    assert normalized.model_dump(mode="json", exclude_unset=True) == spec.model_dump(
        mode="json", exclude_unset=True
    )
    assert not excluded


@pytest.mark.parametrize("drop", ("skills", "mcp", "tools"))
def test_declared_tools_skills_and_mcp_are_removed_before_unsupported_target_checks(drop):
    root = parent("gemini")
    if drop == "skills":
        root = replace(
            root,
            spec=root.spec.model_copy(update={"skills": (SkillRef(id="source", version=None),)}),
        )
    else:
        root = replace(
            root,
            spec=root.spec.model_copy(
                update={
                    "mcp_servers": (
                        MCPConnection(
                            name="source", url="https://source.invalid", credential_ref="owned"
                        ),
                    ),
                    "tools": tuple(root.spec.tools or ())
                    + (ToolSpec(name="source", kind="mcp_toolset"),),
                }
            ),
        )
    with pytest.raises(WorkerSpecRefused):
        resolve(root)
    result = resolve(root, root.spec.model_copy(update={"declared_drops": (drop,)}))
    if drop == "skills":
        assert result.spec.skills == ()
    else:
        assert result.spec.mcp_servers == ()
        assert all(tool.kind != "mcp_toolset" for tool in result.spec.tools)
    if drop == "tools":
        assert result.spec.tools == ()


@pytest.mark.parametrize(
    "drop,kind,ref_kind",
    [
        ("files", "artifact", "file"),
        ("repository", "repository", "repository"),
        ("memory", "native", "memory_store"),
    ],
)
def test_declared_resource_drops_still_validate_scope_and_existing_root_bounds(
    drop, kind, ref_kind
):
    root = parent()
    resource = ResourceBinding(
        id="file",
        kind=kind,
        resource=ResourceRef(
            id="file",
            kind=ref_kind,
            provider="anthropic",
            account_scope_id="workspace",
            tenant_id=root.fleet.scope.tenant_id,
            account_id=root.fleet.scope.account_id,
        ),
    )
    root = replace(root, resources=(resource,))
    requested = root.spec.model_copy(update={"declared_drops": (drop,)})
    assert resolve(root, requested, resources=(resource,)).resources == ()
    foreign = resource.model_copy(
        update={"resource": resource.resource.model_copy(update={"tenant_id": str(uuid4())})}
    )
    with pytest.raises(WorkerSpecRefused):
        resolve(replace(root, resources=(foreign,)), requested, resources=(foreign,))
    with pytest.raises(WorkerSpecRefused, match="budget"):
        resolve(root, requested, limits=root.limits.model_copy(update={"budget_usd": Decimal("2")}))
    with pytest.raises(WorkerSpecRefused, match="writers_none"):
        resolve(
            root,
            requested,
            policy=TenantAccessPolicy(channel_rules={"channel": ChannelRule(writers="none")}),
        )


def test_clone_cannot_clear_an_inherited_declared_drop_or_bypass_source_revision():
    root = parent()
    spec = root.spec.model_copy(update={"declared_drops": ("memory",)})
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider="anthropic",
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    source = Agent(ref=ref, revision=Revision(local=1), spec=spec, created_at=NOW)
    root = replace(root, source_agents=(ref,), source_revisions=((ref, source.revision),))
    assert resolve(root, source).declared_drops == ("memory",)
    with pytest.raises(WorkerSpecRefused, match="declared_drops"):
        resolve(root, source, overrides=AgentPatch(declared_drops=()))
    with pytest.raises(WorkerSpecRefused, match="stale"):
        resolve(root, source.model_copy(update={"revision": Revision(local=2)}))


async def test_declared_drops_are_recorded_in_existing_audit_without_source_payload(
    db_preflight, db_session
):
    from daimon.core.stores.security_audit import list_events
    from daimon.testing.factories import make_account, make_tenant

    root = replace(parent(), agent_id=uuid4())
    tenant = await make_tenant(db_session, id=UUID(root.fleet.scope.tenant_id))
    await make_account(db_session, id=root.fleet.account_id, tenant=tenant)
    worker = resolve(root, root.spec.model_copy(update={"declared_drops": ("sandbox", "memory")}))
    row = await audit_worker_action(
        db_session,
        root,
        worker_id=uuid4(),
        operation="spawn",
        outcome="allowed",
        reason="authorized",
        worker=worker,
    )
    assert row.reason == "authorized; declared_drops=memory,sandbox"
    assert root.base_system not in row.model_dump_json()
    assert len(await list_events(db_session, tenant_id=tenant.id)) == 1
    foreign = replace(
        worker,
        permissions=replace(
            worker.permissions, fleet=replace(worker.permissions.fleet, run_id=uuid4())
        ),
    )
    with pytest.raises(WorkerSpecRefused, match="scope"):
        await audit_worker_action(
            db_session,
            root,
            worker_id=uuid4(),
            operation="resume",
            outcome="allowed",
            reason="authorized",
            worker=foreign,
        )
    malformed = replace(
        worker, spec=worker.spec.model_copy(update={"declared_drops": ("private-credential",)})
    )
    with pytest.raises(WorkerSpecRefused, match="declared_drops"):
        await audit_worker_action(
            db_session,
            root,
            worker_id=uuid4(),
            operation="resume",
            outcome="allowed",
            reason="authorized",
            worker=malformed,
        )
    assert len(await list_events(db_session, tenant_id=tenant.id)) == 1


def captured_worker_source(root):
    """Owner facts for represented intent; native preparation is still a separate proof."""
    from daimon.core.stores.domain import BackendAgent
    from mux.contracts.resources import Environment

    root = replace(root, agent_id=uuid4())
    source = Agent(
        ref=ResourceRef(
            provider=root.spec.model.provider,
            kind="agent",
            id="captured-source",
            account_scope_id="owned-workspace",
            tenant_id=root.fleet.scope.tenant_id,
            account_id=root.fleet.scope.account_id,
        ),
        revision=Revision(local=1, native="1"),
        spec=root.spec,
        native={"id": "captured-source", "version": 1},
        created_at=NOW,
    )
    captured = BackendAgent(
        tenant_id=UUID(root.fleet.scope.tenant_id),
        agent_id=root.agent_id,
        name="source",
        configuration_digest="never-authority",
        source=source,
        effective_spec=root.spec,
        resource_scope=root.fleet.scope,
        environment=Environment(
            ref=source.ref.model_copy(update={"kind": "environment", "id": "environment"}),
            revision=Revision(local=1, native="1"),
            spec=root.environment,
            created_at=NOW,
        ),
        memory_required=False,
        memory_read_only=True,
    )
    return root, captured


def test_captured_intent_keeps_root_and_source_instructions_with_role_selected_model():
    root, captured = captured_worker_source(parent("openai"))
    root = replace(root, seal_ids=frozenset({"root-seal"}), memory_read_only=False)
    other = root.spec.model.model_copy(update={"id": "another-authorized-model"})
    root = replace(root, models=root.models + (other,))
    captured = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(
                update={"system": "Source rule", "model": other}
            )
        }
    )
    # The existing finite grant also pins its model, so explicit role choice cannot bypass it.
    with pytest.raises(WorkerSpecRefused):
        resolve(root, root.spec.model_copy(update={"model": other}), captured_source=captured)
    result = resolve(
        root, root.spec.model_copy(update={"system": "Role rule"}), captured_source=captured
    )
    assert result.spec.system == "Source rule\n\nProtected root instruction\n\nRole rule"
    assert result.spec.model == root.spec.model
    assert result.permissions.seal_ids == root.seal_ids
    assert result.permissions.memory_read_only is True
    assert (
        result.digest
        == resolve(
            root, root.spec.model_copy(update={"system": "Role rule"}), captured_source=captured
        ).digest
    )


@pytest.mark.parametrize("field", ("tools", "skills", "mcp"))
def test_source_obligations_cannot_be_silently_omitted(field):
    root = parent("openai")
    additions = {
        "skills": (SkillRef(id="pinned", version="1"),),
    }
    if field == "mcp":
        additions["mcp_servers"] = (
            MCPConnection(
                name="bound",
                url="https://bound.invalid",
                tool_policy={"allowed_tools": ["read"]},
            ),
        )
    root = replace(root, spec=root.spec.model_copy(update=additions))
    root, captured = captured_worker_source(root)
    key = "mcp_servers" if field == "mcp" else field
    with pytest.raises(WorkerSpecRefused) as refusal:
        resolve(root, root.spec.model_copy(update={key: ()}), captured_source=captured)
    assert (refusal.value.field, refusal.value.reason) == (field, "unsupported")
    result = resolve(
        root,
        root.spec.model_copy(update={key: (), "declared_drops": (field,)}),
        captured_source=captured,
    )
    assert getattr(result.spec, key) == ()


@pytest.mark.parametrize(
    "change", ("tenant", "scope", "agent", "native", "environment", "projection")
)
def test_capture_requires_current_scoped_projector_identity(change):
    root, captured = captured_worker_source(parent("openai"))
    updates = {
        "tenant": {"tenant_id": uuid4()},
        "scope": {
            "resource_scope": root.fleet.scope.model_copy(update={"account_id": str(uuid4())})
        },
        "agent": {"agent_id": uuid4()},
        "native": {"source": captured.source.model_copy(update={"native": None})},
        "environment": {"environment": None},
        "projection": {"memory_required": None},
    }
    with pytest.raises(WorkerSpecRefused, match="scope: stale"):
        resolve(root, captured_source=captured.model_copy(update=updates[change]))


@pytest.mark.parametrize("change", ("native", "generation", "environment_revision", "guidance"))
def test_capture_facts_change_canonical_digest_even_when_authored_spec_is_unchanged(change):
    root, captured = captured_worker_source(parent("openai"))
    original = resolve(root, captured_source=captured)
    updates = {
        "native": {
            "source": captured.source.model_copy(
                update={"native": {"id": "captured-source", "version": 2}}
            )
        },
        "generation": {"resource_generations": (("repo", NOW),)},
        "environment_revision": {
            "environment": captured.environment.model_copy(
                update={"revision": Revision(local=1, native="2")}
            )
        },
        "guidance": {"memory_instructions": "Read-only memory instruction"},
    }
    changed = resolve(root, captured_source=captured.model_copy(update=updates[change]))
    assert changed.spec == original.spec
    assert changed.digest != original.digest


@pytest.mark.parametrize(
    "opaque", ("extensions", "effort", "memory", "credential", "native_resource", "environment")
)
def test_unrepresentable_capture_is_typed_refused_before_any_allocation(opaque):
    root, captured = captured_worker_source(parent("openai"))
    changes = {
        "extensions": {
            "effective_spec": captured.effective_spec.model_copy(
                update={
                    "extensions": (
                        ExtensionConfig(
                            namespace="anthropic.model_config",
                            version=1,
                            value={"model": {"model": "ungranted"}},
                        ),
                    )
                }
            )
        },
        "effort": {
            "effective_spec": captured.effective_spec.model_copy(
                update={
                    "model": captured.effective_spec.model.model_copy(
                        update={"options": {"effort": "high"}}
                    )
                }
            )
        },
        "memory": {"memory_required": True},
        "credential": {
            "resource_requirements": (
                ResourceBinding(id="credential", kind="credential", credential_ref="opaque"),
            )
        },
        "native_resource": {
            "resource_requirements": (ResourceBinding(id="opaque", kind="native"),)
        },
        "environment": {
            "environment": captured.environment.model_copy(
                update={
                    "spec": captured.environment.spec.model_copy(
                        update={
                            "native_config": ExtensionConfig(
                                namespace="anthropic.environment_config", version=1, value={}
                            )
                        }
                    )
                }
            )
        },
    }
    with pytest.raises(WorkerSpecRefused):
        resolve(root, captured_source=captured.model_copy(update=changes[opaque]))


def test_clone_capture_must_match_retained_source_revision_and_native_snapshot():
    root, captured = captured_worker_source(parent("openai"))
    root = replace(
        root,
        source_agents=(captured.source.ref,),
        source_revisions=((captured.source.ref, captured.source.revision),),
    )
    result = resolve(root, captured.source, captured_source=captured)
    assert result.source_agent == (captured.source.ref, captured.source.revision)
    for update in ({"revision": Revision(local=1, native="2")}, {"native": {"id": "different"}}):
        with pytest.raises(WorkerSpecRefused, match="scope: stale"):
            resolve(
                root,
                captured.source,
                captured_source=captured.model_copy(
                    update={"source": captured.source.model_copy(update=update)}
                ),
            )


def test_captured_sandbox_and_resources_cannot_replace_root_grants():
    root, captured = captured_worker_source(parent("openai"))
    binding = ResourceBinding(id="file", kind="artifact")
    with pytest.raises(WorkerSpecRefused, match="extensions: unsupported"):
        resolve(
            root, captured_source=captured.model_copy(update={"resource_requirements": (binding,)})
        )
    wider = root.environment.model_copy(update={"network": NetworkPolicy(mode="unrestricted")})
    with pytest.raises(WorkerSpecRefused, match="sandbox: not_subset"):
        resolve(
            root,
            environment=wider,
            captured_source=captured.model_copy(
                update={"environment": captured.environment.model_copy(update={"spec": wider})}
            ),
        )


def test_capture_inherited_drops_remain_restrictions():
    root, captured = captured_worker_source(parent("openai"))
    captured = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(
                update={"declared_drops": ("memory",)}
            )
        }
    )
    with pytest.raises(WorkerSpecRefused, match="declared_drops: not_subset"):
        resolve(root, captured_source=captured)
    result = resolve(
        root, root.spec.model_copy(update={"declared_drops": ("memory",)}), captured_source=captured
    )
    assert result.declared_drops == ("memory",)


def test_existing_memory_store_cannot_disappear_from_capture_obligations():
    root, captured = captured_worker_source(parent("openai"))
    captured = captured.model_copy(
        update={"memory_required": False, "memory_store_id": "memstore-existing"}
    )
    with pytest.raises(WorkerSpecRefused, match="memory: unsupported"):
        resolve(root, captured_source=captured)
    result = resolve(
        root, root.spec.model_copy(update={"declared_drops": ("memory",)}), captured_source=captured
    )
    assert result.declared_drops == ("memory",)


@pytest.mark.parametrize(
    "source,role,expected",
    [
        ("Source rule", "Protected root instruction", "Source rule\n\nProtected root instruction"),
        (
            "Protected root instruction\n\nSource rule",
            "Protected root instruction\n\nRole rule",
            "Protected root instruction\n\nSource rule\n\nRole rule",
        ),
    ],
)
def test_instruction_normalization_matches_preparation_without_duplicate_root(
    source, role, expected
):
    from daimon.core.turn.runtime_manifest import classify_source_obligations

    root, captured = captured_worker_source(parent("openai"))
    captured = captured.model_copy(
        update={"effective_spec": captured.effective_spec.model_copy(update={"system": source})}
    )
    result = resolve(root, root.spec.model_copy(update={"system": role}), captured_source=captured)
    assert result.spec.system == expected
    assert result.spec.system.count(root.base_system) == 1
    assert (
        classify_source_obligations(captured, result.spec, environment=result.environment)
        == frozenset()
    )


def test_without_capture_instruction_composition_retains_existing_bytes():
    root = parent("openai")
    root = replace(root, base_system="Root\n\nProtected rule")
    result = resolve(root, root.spec.model_copy(update={"system": "Root\n\nRole rule"}))
    assert result.spec.system == "Root\n\nProtected rule\n\nRoot\n\nRole rule"


def frozen_root_inputs(provider="openai"):
    from daimon.core.turn.admission import AdmissionGrant

    root, source = captured_worker_source(parent(provider))
    root = replace(
        root,
        source_agents=(source.source.ref,),
        source_revisions=((source.source.ref, source.source.revision),),
    )
    grant = AdmissionGrant(
        tenant_id=source.tenant_id,
        subject=root.subject,
        surface=Surface.CHANNEL,
        turn_place=root.fleet.place,
        run_place=root.fleet.place,
        agent=root.agent,
        channel_id=root.fleet.root.channel.channel_id,
        thread_id=root.fleet.root.thread_id,
        is_dm=False,
        human_invocation=True,
    )
    return root, source, grant


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
def test_freeze_root_preserves_admitted_authority_and_all_finite_grants(provider):
    root, source, admission = frozen_root_inputs(provider)
    root = replace(
        root,
        fleet=replace(root.fleet, place=replace(root.fleet.place, category_id="category")),
        subject=replace(root.subject, is_admin=True),
        models=root.models + (root.spec.model.model_copy(update={"id": "another-finite-model"}),),
        seal_ids=frozenset({"source-seal"}),
    )
    admission = replace(admission, subject=root.subject, run_place=root.fleet.place)
    result = freeze_root_permissions(root, source, admission=admission, policy=POLICY, now=NOW)
    assert result.fleet == root.fleet and result.fleet.depth == 0
    assert result.subject == admission.subject and result.subject.is_admin
    assert result.agent == admission.agent
    assert result.models == root.models and result.backend_grants == root.backend_grants
    assert result.source_revisions == root.source_revisions
    assert result.source_agents == root.source_agents and result.agent_id == source.agent_id
    assert result.seal_ids == root.seal_ids and result.memory_read_only
    assert result.spec == root.spec and result.environment == root.environment
    assert result.limits == root.limits
    child = resolve_worker_spec(result, result.spec, policy=POLICY, now=NOW)
    assert child.permissions.fleet.depth == 1 and not child.permissions.subject.is_admin


@pytest.mark.parametrize(
    "changed",
    (
        "tenant",
        "subject",
        "agent",
        "category",
        "parent",
        "thread",
        "account",
        "source_uuid",
        "source_scope",
        "source_ref",
        "source_revision",
        "source_account",
        "source_archived",
        "source_missing",
        "effective_missing",
        "memory_unknown",
        "environment_missing",
        "metadata",
        "budget",
        "expired",
        "turns",
        "models",
        "backends",
        "duplicate_model",
        "prefix",
    ),
)
def test_freeze_root_refuses_foreign_stale_missing_or_unbounded_facts(changed):
    root, source, admission = frozen_root_inputs()
    if changed == "tenant":
        admission = replace(admission, tenant_id=uuid4())
    elif changed == "subject":
        admission = replace(admission, subject=replace(admission.subject, is_admin=True))
    elif changed == "agent":
        admission = replace(admission, agent=AgentRef.of("neighbour"))
    elif changed == "category":
        admission = replace(admission, run_place=replace(admission.run_place, category_id="other"))
    elif changed == "parent":
        admission = replace(admission, channel_id="neighbour")
    elif changed == "thread":
        admission = replace(admission, thread_id="neighbour")
    elif changed == "account":
        root = replace(root, fleet=replace(root.fleet, account_id=uuid4()))
    elif changed == "source_uuid":
        source = source.model_copy(update={"agent_id": uuid4()})
    elif changed == "source_scope":
        source = source.model_copy(
            update={
                "resource_scope": root.fleet.scope.model_copy(
                    update={"principal_id": "another-caller"}
                )
            }
        )
    elif changed == "source_ref":
        root = replace(root, source_agents=())
    elif changed == "source_revision":
        root = replace(root, source_revisions=((source.source.ref, Revision(local=2)),))
    elif changed == "source_account":
        actual = source.source.model_copy(
            update={"ref": source.source.ref.model_copy(update={"account_id": str(uuid4())})}
        )
        source = source.model_copy(update={"source": actual})
        root = replace(
            root, source_agents=(actual.ref,), source_revisions=((actual.ref, actual.revision),)
        )
    elif changed == "source_archived":
        source = source.model_copy(
            update={"source": source.source.model_copy(update={"lifecycle": "archived"})}
        )
    elif changed == "source_missing":
        source = source.model_copy(update={"source": None})
    elif changed == "effective_missing":
        source = source.model_copy(update={"effective_spec": None})
    elif changed == "memory_unknown":
        source = source.model_copy(update={"memory_required": None})
    elif changed == "environment_missing":
        source = source.model_copy(update={"environment": None})
    elif changed == "metadata":
        root = replace(root, spec=root.spec.model_copy(update={"metadata": {"owner": "foreign"}}))
    elif changed == "budget":
        root = replace(root, limits=root.limits.model_copy(update={"budget_usd": Decimal("NaN")}))
    elif changed == "expired":
        root = replace(root, limits=root.limits.model_copy(update={"deadline": NOW}))
    elif changed == "turns":
        root = replace(root, limits=root.limits.model_copy(update={"max_turns": True}))
    elif changed == "models":
        root = replace(root, models=())
    elif changed == "backends":
        root = replace(root, backend_grants=())
    elif changed == "duplicate_model":
        root = replace(root, models=root.models * 2)
    elif changed == "prefix":
        root = replace(root, base_system="caller instruction")
    with pytest.raises(WorkerSpecRefused):
        freeze_root_permissions(root, source, admission=admission, policy=POLICY, now=NOW)


def test_frozen_root_remains_narrow_after_current_policy_or_source_changes():
    root, source, admission = frozen_root_inputs()
    frozen = freeze_root_permissions(root, source, admission=admission, policy=POLICY, now=NOW)
    source = source.model_copy(
        update={
            "effective_spec": source.effective_spec.model_copy(
                update={
                    "tools": source.effective_spec.tools + (ToolSpec(name="new", kind="builtin"),)
                }
            )
        }
    )
    with pytest.raises(WorkerSpecRefused):
        freeze_root_permissions(root, source, admission=admission, policy=POLICY, now=NOW)
    with pytest.raises(WorkerSpecRefused):
        resolve_worker_spec(
            frozen,
            frozen.spec.model_copy(update={"tools": source.effective_spec.tools}),
            policy=POLICY,
            now=NOW,
        )
    with pytest.raises(WorkerSpecRefused):
        freeze_root_permissions(
            root,
            source,
            admission=admission,
            policy=TenantAccessPolicy(channel_rules={"channel": ChannelRule(writers="none")}),
            now=NOW,
        )
    assert frozen.spec == root.spec and frozen.models == root.models


async def test_root_freeze_uses_actual_sdk_owner_capture_and_owned_resource_projection(
    db_preflight, db_committing_engine
):
    from daimon.core.authz import build_agent_ref
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.mux_compat import retrieve_agent
    from daimon.core.sessions import project_session_resources
    from daimon.core.turn.admission import AdmissionGrant
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import make_account, make_tenant
    from daimon.testing.ma_models import ma_agent, ma_environment
    from mux.testing.drivers.anthropic.ma import MARouter
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    root = parent("openai")
    scope = root.fleet.scope.model_copy(
        update={"tenant_id": str(tenant.id), "account_id": str(account.id)}
    )
    fleet = replace(
        root.fleet,
        scope=scope,
        account_id=account.id,
        root=root.fleet.root.model_copy(
            update={
                "channel": root.fleet.root.channel.model_copy(update={"tenant_id": str(tenant.id)})
            }
        ),
    )
    native = ma_agent(
        id="root-source",
        name="root",
        tenant_id=str(tenant.id),
        system=root.base_system,
        model="claude-haiku-5-5",
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/root-source",
        lambda request, match: httpx.Response(200, json=native.model_dump(mode="json")),
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        captured = await load_backend_agent(
            client, sm, tenant_id=tenant.id, agent_name="root", actual_agent=actual
        )
        captured = await project_session_resources(
            captured,
            agent=actual,
            environment=ma_environment(tenant_id=str(tenant.id)),
            session_factory=sm,
            tenant_id=tenant.id,
            agent_uuid=captured.agent_id,
            account_id=account.id,
            resource_scope=scope,
            memory_read_only=True,
            tool_safety=OPEN_TOOL_SAFETY,
        )
    transport.assert_consumed()
    assert captured.memory_required and captured.memory_store_id is None
    assert captured.source.native and captured.effective_spec.extensions
    spec = captured.effective_spec.model_copy(
        update={
            "model": root.spec.model,
            "extensions": None,
            "tools": (),
            "skills": (),
            "mcp_servers": (),
            "declared_drops": ("memory", "sandbox", "tools"),
        }
    )
    root = replace(
        root,
        fleet=fleet,
        agent=build_agent_ref(actual.name, actual.metadata or {}),
        spec=spec,
        base_system=spec.system,
        environment=None,
        agent_id=captured.agent_id,
        source_agents=(captured.source.ref,),
        source_revisions=((captured.source.ref, captured.source.revision),),
    )
    admission = AdmissionGrant(
        tenant_id=tenant.id,
        subject=root.subject,
        surface=Surface.CHANNEL,
        turn_place=fleet.place,
        run_place=fleet.place,
        agent=root.agent,
        channel_id=fleet.root.channel.channel_id,
        thread_id=fleet.root.thread_id,
        is_dm=False,
        human_invocation=True,
    )
    result = freeze_root_permissions(root, captured, admission=admission, policy=POLICY, now=NOW)
    assert result.agent_id == captured.agent_id and result.source_revisions == root.source_revisions
    assert result.spec.declared_drops == spec.declared_drops
    assert result.environment is None and result.resources == () and result.spec.tools == ()
    assert [(request.method, request.path) for request in transport.requests] == [
        ("GET", "/v1/agents/root-source")
    ]
    # Unknown native intent remains refused by the same obligation classifier.
    opaque = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(
                update={
                    "extensions": captured.effective_spec.extensions
                    + (
                        ExtensionConfig(
                            namespace="anthropic.unrepresented_intent", version=1, value={}
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(WorkerSpecRefused):
        freeze_root_permissions(root, opaque, admission=admission, policy=POLICY, now=NOW)


def test_freeze_checks_nonselected_backend_grants_against_the_original_policy():
    root, source, admission = frozen_root_inputs("anthropic")
    other = backend_selection("openai")
    root = replace(
        root,
        backend_grants=root.backend_grants + (other,),
        models=root.models + (ModelRef(provider=other.backend, id=other.model),),
    )
    with pytest.raises(WorkerSpecRefused, match="backend"):
        freeze_root_permissions(
            root, source, admission=admission, policy=TenantAccessPolicy(), now=NOW
        )
    frozen = freeze_root_permissions(root, source, admission=admission, policy=POLICY, now=NOW)
    assert frozen.backend_grants == root.backend_grants and frozen.models == root.models


@pytest.mark.parametrize(
    "provider,expected", [("anthropic", "0.04"), ("openai", "0.055"), ("gemini", "0.49")]
)
def test_inherited_worker_budget_attenuates_to_its_provider(provider, expected):
    root = parent(provider)
    limits = WorkerLimits.model_validate(
        root.limits.model_dump()
        | {
            "budget_usd": Decimal("0.49"),
            "provider_limits_usd": {
                "anthropic": Decimal("0.04"),
                "openai": Decimal("0.055"),
                "gemini": Decimal("0.49"),
            },
        }
    )
    root = replace(root, limits=limits)
    inherited = resolve(root)
    assert inherited.limits.budget_usd == Decimal(expected)
    assert inherited.limits.provider_limits_usd == limits.provider_limits_usd
    requested = WorkerLimits(budget_usd=Decimal(expected), deadline=limits.deadline, max_turns=1)
    explicit = resolve(root, limits=requested)
    assert explicit.limits.budget_usd == Decimal(expected)
    assert explicit.limits.provider_limits_usd == limits.provider_limits_usd
    smaller = replace(root, limits=limits.model_copy(update={"budget_usd": Decimal("0.02")}))
    assert resolve(smaller).limits.budget_usd == Decimal("0.02")
    assert root.limits == limits


@pytest.mark.parametrize("provider,ceiling", [("anthropic", "0.04"), ("openai", "0.055")])
def test_explicit_worker_budget_cannot_exceed_provider_ceiling(provider, ceiling):
    root = parent(provider)
    root = replace(
        root,
        limits=WorkerLimits.model_validate(
            root.limits.model_dump()
            | {
                "budget_usd": Decimal("0.49"),
                "provider_limits_usd": {provider: Decimal(ceiling)},
            }
        ),
    )
    request = WorkerLimits(
        budget_usd=Decimal(ceiling) + Decimal("0.000001"),
        deadline=root.limits.deadline,
        max_turns=1,
    )
    with pytest.raises(WorkerSpecRefused, match="provider_limits_usd: not_subset"):
        resolve(root, limits=request)


def test_explicit_worker_budget_cannot_exceed_global_ceiling():
    root = parent("gemini")
    root = replace(
        root,
        limits=WorkerLimits.model_validate(
            root.limits.model_dump()
            | {
                "budget_usd": Decimal("0.49"),
                "provider_limits_usd": {"gemini": Decimal("0.50")},
            }
        ),
    )
    request = WorkerLimits(
        budget_usd=Decimal("0.490001"), deadline=root.limits.deadline, max_turns=1
    )
    with pytest.raises(WorkerSpecRefused, match="budget: not_subset"):
        resolve(root, limits=request)


@pytest.mark.parametrize("field", ("max_input_tokens", "max_output_tokens"))
@pytest.mark.parametrize("replacement", (None, 101))
def test_gemini_worker_caps_attenuate_monotonically(field, replacement):
    root = parent("gemini")
    limits = WorkerLimits.model_validate(
        root.limits.model_dump()
        | {"max_input_tokens": 100, "max_output_tokens": 100, "max_turns": 1}
    )
    root = replace(root, limits=limits)
    narrower = limits.model_copy(update={"max_input_tokens": 99, "max_output_tokens": 99})
    assert resolve(root, limits=narrower).limits == narrower
    with pytest.raises(WorkerSpecRefused, match=field):
        resolve(root, limits=limits.model_copy(update={field: replacement}))


@pytest.mark.parametrize(
    "update", ({"max_turns": 2}, {"max_output_tokens": 65537}, {"max_model_requests": 1})
)
def test_gemini_unknown_or_expanded_execution_envelopes_refuse(update):
    limits = WorkerLimits.model_validate(
        parent("gemini").limits.model_dump()
        | {"max_input_tokens": 262144, "max_output_tokens": 65536, "max_turns": 1}
        | update
    )
    with pytest.raises(WorkerSpecRefused):
        limits.require_profile(get_profile("gemini.inline_reuse"))


@pytest.mark.parametrize(
    ("now", "priced"),
    [(datetime(2026, 11, 30, tzinfo=UTC), True), (datetime(2026, 12, 1, tzinfo=UTC), False)],
)
def test_granted_argon_worker_spec_resolves_only_inside_its_price_window(monkeypatch, now, priced):
    from daimon.core import pricing

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing, "datetime", Clock)
    root = parent("gemini")
    argon = backend_selection("gemini").model_copy(update={"model": "gemini-4-argon"})
    model = ModelRef(provider="gemini", id="gemini-4-argon")
    root = replace(
        root,
        spec=root.spec.model_copy(update={"model": model}),
        backend_grants=(argon,),
        models=(model,),
    )
    if priced:
        assert resolve(root).backend == argon
    else:
        with pytest.raises(WorkerSpecRefused, match="backend: unsupported"):
            resolve(root)


@pytest.mark.parametrize(
    ("backend", "kept"), [("anthropic", True), ("gemini", True), ("openai", False)]
)
def test_a_worker_keeps_its_description_wherever_the_provider_has_one(backend, kept) -> None:
    """Only an OpenAI worker goes without it: OpenAI agents have no description (J4)."""
    root = parent(backend)
    spec = root.spec.model_copy(update={"description": "A saved agent people pick by name."})
    resolved = resolve_worker_spec(root, spec, policy=POLICY, now=NOW)
    assert resolved.spec.description == (spec.description if kept else None)


@pytest.mark.parametrize("admin", [False, True])
def test_worker_delegation_into_role_gated_target_requires_human_role(admin: bool) -> None:
    root = parent()
    root = replace(
        root, subject=Subject(platform_user_id="user", is_admin=admin, verified_membership=True)
    )
    policy = TenantAccessPolicy(agent_rules={"target": AgentRule(invoker_role_ids=("123",))})
    target = root.spec.model_copy(update={"name": "target"})
    with pytest.raises(WorkerSpecRefused, match="invoker_not_allowed"):
        resolve(root, target, policy=policy)
    allowed = replace(
        root, subject=Subject(platform_user_id="user", verified_membership=True, role_ids=("123",))
    )
    assert resolve(allowed, target, policy=policy).spec.name == "target"


def test_frozen_root_accepts_fresh_membership_without_changing_invoker_or_grants() -> None:
    from daimon.core.turn.runtime_manifest import (
        _same_root_with_live_membership,  # pyright: ignore[reportPrivateUsage]
    )

    recorded = parent()
    recorded = replace(recorded, subject=replace(recorded.subject, is_admin=True))
    current = replace(
        recorded,
        subject=replace(
            recorded.subject,
            role_ids=("123",),
            verified_membership=True,
            is_admin=False,
        ),
    )
    assert _same_root_with_live_membership(recorded, current)
    assert not _same_root_with_live_membership(
        replace(recorded, subject=replace(recorded.subject, is_admin=False)),
        replace(current, subject=replace(current.subject, is_admin=True)),
    )
    for subject in (
        replace(current.subject, platform_user_id="another-human"),
        replace(current.subject, client=True),
        replace(current.subject, via_agent_key=True),
    ):
        assert not _same_root_with_live_membership(recorded, replace(current, subject=subject))
    assert not _same_root_with_live_membership(
        recorded,
        replace(current, limits=current.limits.model_copy(update={"budget_usd": Decimal("1000")})),
    )


@pytest.mark.parametrize("target_gated", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
def test_gated_root_delegates_with_current_root_and_target_roles(
    target_gated: bool, revoked: bool
) -> None:
    root = parent()
    root = replace(
        root,
        subject=replace(
            root.subject, verified_membership=True, role_ids=() if revoked else ("123", "456")
        ),
    )
    rules = {"root": AgentRule(invoker_role_ids=("123",))}
    if target_gated:
        rules["worker-target"] = AgentRule(invoker_role_ids=("456",))
    policy = TenantAccessPolicy(agent_rules=rules)
    target = root.spec.model_copy(update={"name": "worker-target"})
    if revoked:
        with pytest.raises(WorkerSpecRefused, match="invoker_not_allowed"):
            resolve(root, target, policy=policy)
    else:
        result = resolve(root, target, policy=policy)
        assert result.spec.name == "worker-target" and result.permissions.human_invocation


def test_wake_root_cannot_delegate_into_gated_target_with_recorded_owners_roles() -> None:
    from daimon.core.turn.invocation import human_invocation

    root = parent()
    root = replace(
        root,
        human_invocation=human_invocation("watch_fired", True),
        subject=replace(root.subject, verified_membership=True, role_ids=("123",)),
    )
    target = root.spec.model_copy(update={"name": "worker-target"})
    policy = TenantAccessPolicy(agent_rules={"worker-target": AgentRule(invoker_role_ids=("123",))})
    with pytest.raises(WorkerSpecRefused, match="invoker_not_allowed"):
        resolve(root, target, policy=policy)
