"""Original consent binds actual scoped source reads to one prepared target."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    resolve_turn_backend,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import TurnRuntime
from daimon.core.scope import ResolvedConfig
from daimon.core.stores import mux_state
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.admission import Admission
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
from daimon.core.turn.runtime_manifest import (
    capture_preparation_source,
    classify_source_obligations,
    prepare_source,
    read_preparation_counterpart,
)
from daimon.core.turn.runtimes import scope_channel_runtimes
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef, ResourceRef, Revision, Scope
from mux.contracts.resources import Agent
from mux.profiles import backend_selection

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned


@pytest.fixture
async def consent_request(owned):
    _, identity, actor, sessions = owned
    now = datetime(2026, 10, 14, tzinfo=UTC)
    native = ma_agent(tenant_id=identity.tenant_id, system="Authoritative source instructions")
    environment = ma_environment(tenant_id=identity.tenant_id)
    router, reads, constructions, counterpart_reads = MARouter(), [], [], []
    router.add_agent(native)
    router.add_environment(environment)

    def transport(request):
        reads.append(request)
        return router.dispatch(request)

    async with build_fake_anthropic(transport) as client:
        revision = ConfigRevision.create(
            ChannelRef(tenant_id=str(identity.tenant_id), platform="slack", channel_id="channel"),
            1,
            backend_selection("openai"),
        )
        admitted = Admission(
            account_id=actor.account_id,
            agent=native,
            environment=environment,
            config=ResolvedConfig(agent_name="test-agent", environment_name="test-env"),
            backend_revision=revision,
            backend_revision_origin="agent",
        )
        request = ProviderPreparationRequest(
            deps=replace(deps_for(client, sessions), agent_backends=True, channel_backends=True),
            admission=admitted,
            scope=Scope(
                tenant_id=str(identity.tenant_id),
                account_id=str(actor.account_id),
                principal_id="caller",
                authorization_id="admitted",
            ),
            tenant_id=identity.tenant_id,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=actor.account_id,
            reuse_existing=True,
            capabilities=DEFAULT_MA_CAPABILITIES,
            transfer=None,
            deadline=now + timedelta(seconds=30),
            now=lambda: now,
        )
        source = await capture_preparation_source(request, fresh=True)
        assert source.agent_id == derive_agent_uuid(
            tenant_id=identity.tenant_id, ma_agent_id=native.id
        )

        async def capture(agent):
            assert agent.agent_id == source.agent_id
            return await capture_preparation_source(request, fresh=True)

        service = AgentBackendService(sessions, enabled=True, capture_source=capture)
        await enable(service, source, actor)
        quote = await service.preview(source, "openai")
        accepted = await service.set_backend(
            source,
            "openai",
            0,
            actor,
            accepted_drops=quote.declared_drops,
            expected_preview_digest=quote.digest,
        )
        async with sessions.begin() as db:
            await mux_state.put_config_revision(
                db, revision, origin="agent", agent_revision=accepted
            )

        def construct(selected):
            constructions.append(selected)

            async def read(actual):
                counterpart_reads.append(actual)
                return AgentRevision(
                    catalog_id="original-counterpart",
                    local_revision=1,
                    principal_id="host",
                    agent=Agent(
                        ref=ResourceRef(
                            id="native-counterpart",
                            kind="agent",
                            provider="openai",
                            account_scope_id="project",
                            tenant_id=request.scope.tenant_id,
                            account_id=request.scope.account_id,
                        ),
                        revision=Revision(local=1),
                        created_at=now,
                        spec=actual.target_spec,
                    ),
                )

            return TurnRuntime(
                transport_factory=lambda *args: None,
                journal=object(),
                usage_store=object(),
                read_agent_counterpart=read,
            )

        request, lookup = scope_channel_runtimes(
            replace(request, deps=replace(request.deps, turn_runtime_factory=construct))
        )
        reads.clear()
        yield (
            request,
            service,
            source,
            actor,
            accepted,
            router,
            reads,
            constructions,
            counterpart_reads,
            lookup,
        )


async def test_real_preparation_uses_original_consent_after_latest_setting_changes(consent_request):
    request, service, source, actor, accepted, _, reads, constructions, targets, lookup = (
        consent_request
    )
    await service.clear_backend(source, accepted.local, actor)
    selected = await read_preparation_counterpart(request)
    assert selected.worker is None
    assert selected.target_spec.declared_drops == accepted.declared_drops
    assert selected.target_spec.system.startswith("Authoritative source instructions\n\n")
    assert selected.verified_counterpart == selected.counterpart
    assert len(constructions) == len(targets) == 1
    assert constructions[0].target_spec == selected.target_spec
    assert constructions[0].source == selected.source
    assert (
        selected.deps.turn_runtimes[request.admission.backend_revision.profile] is lookup.resolved
    )
    assert [(r.method, r.url.path) for r in reads] == [
        ("GET", "/v1/agents/ag_test"),
        ("GET", "/v1/environments/env_test"),
        ("GET", "/v1/agents/ag_test"),
        ("GET", "/v1/environments/env_test"),
    ]


@pytest.mark.parametrize("change", ["system", "version", "publishing"])
async def test_real_source_drift_refuses_before_target_runtime(consent_request, change):
    request, _, _, _, _, router, reads, constructions, targets, _ = consent_request
    if change == "publishing":
        request = replace(
            request, admission=replace(request.admission, asks_before_publishing=True)
        )
    else:
        native = request.admission.agent.model_copy(
            update={change: "Changed" if change == "system" else 2}
        )
        router.routes.clear()
        router.add_agent(native)
        router.add_environment(request.admission.environment)
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert refused.value.reason == "backend_unsupported"
    assert not constructions and not targets
    assert all(r.method == "GET" for r in reads)
    if change != "publishing":
        assert isinstance(refused.value.__cause__, AgentBackendRefused)
        assert refused.value.__cause__.reason == "re_confirm"


async def test_a_caller_target_cannot_replace_the_confirmed_target(consent_request):
    request, _, _, _, accepted, _, _, _, _, _ = consent_request
    original = await prepare_source(request)
    selected = await prepare_source(
        replace(request, target_spec=original.target_spec.model_copy(update={"system": "Forged"}))
    )
    assert selected.target_spec == original.target_spec
    assert selected.target_spec.declared_drops == accepted.declared_drops


async def test_source_drift_during_counterpart_read_requires_reconfirmation(consent_request):
    request, _, _, _, _, router, reads, constructions, targets, _ = consent_request
    construct = request.deps.turn_runtime_factory

    def drifting(selected):
        runtime = construct(selected)

        async def read(actual):
            result = await runtime.read_agent_counterpart(actual)
            router.routes.clear()
            router.add_agent(
                request.admission.agent.model_copy(update={"system": "Changed after proof"})
            )
            router.add_environment(request.admission.environment)
            return result

        return replace(runtime, read_agent_counterpart=read)

    request, _ = scope_channel_runtimes(
        replace(
            request, deps=replace(request.deps, turn_runtimes={}, turn_runtime_factory=drifting)
        )
    )
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert isinstance(refused.value.__cause__, AgentBackendRefused)
    assert refused.value.__cause__.reason == "re_confirm"
    assert len(constructions) == len(targets) == 1
    assert len(reads) == 4 and all(r.method == "GET" for r in reads)


async def test_expired_request_has_zero_native_source_or_target_reads(consent_request):
    request, _, _, _, _, _, reads, constructions, targets, _ = consent_request
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(replace(request, deadline=request.now()))
    assert reads == constructions == targets == []


@pytest.mark.parametrize("drift", [False, True])
async def test_selector_passes_original_consent_to_the_existing_prewrite_proof(
    consent_request, drift
):
    request, _, source, _, accepted, router, reads, _, _, _ = consent_request
    channel = request.admission.backend_revision.channel.model_copy(update={"channel_id": "new"})
    checks = []

    async def validate(selection, origin, retained, *, agent_revision):
        assert origin == "agent" and agent_revision == accepted
        revision = retained or ConfigRevision.create(channel, 1, selection)
        selected = await prepare_source(
            replace(
                request,
                admission=replace(
                    request.admission,
                    backend_revision=revision,
                    backend_agent_revision=agent_revision,
                ),
            )
        )
        checks.append(selected)

    if drift:
        router.routes.clear()
        router.add_agent(request.admission.agent.model_copy(update={"system": "Changed source"}))
        router.add_environment(request.admission.environment)
        with pytest.raises(AgentBackendRefused, match="re confirm"):
            async with request.deps.sessionmaker.begin() as db:
                await resolve_turn_backend(
                    db, channel, source, enabled=True, validate_origin=validate
                )
        async with request.deps.sessionmaker() as db:
            assert await mux_state.get_config_revision(db, channel, 1) is None
        assert checks == []
    else:
        async with request.deps.sessionmaker.begin() as db:
            revision, shared, origin = await resolve_turn_backend(
                db, channel, source, enabled=True, validate_origin=validate
            )
        assert not shared and origin == "agent"
        assert len(checks) == 2 and all(
            value.target_spec.declared_drops == accepted.declared_drops for value in checks
        )
        async with request.deps.sessionmaker() as db:
            assert (
                await mux_state.get_config_agent_revision(db, revision, agent_id=source.agent_id)
                == accepted
            )
    assert reads and all(r.method == "GET" for r in reads)


@pytest.mark.parametrize("unknown", ["policy", "null_policy", "skill_policy", "mcp_policy"])
async def test_unclassified_sdk_source_refuses_preview_commit_and_handoff(consent_request, unknown):
    request, service, source, actor, accepted, router, reads, constructions, targets, _ = (
        consent_request
    )
    from anthropic.types.beta import (
        BetaManagedAgentsCustomSkill,
        BetaManagedAgentsMCPServerURLDefinition,
    )

    policy = {"requires_confirmation": True}
    if unknown in {"policy", "null_policy"}:
        native = request.admission.agent.model_copy(
            update={"future_execution_policy": policy if unknown == "policy" else None}
        )
        path = "future_execution_policy"
    elif unknown == "skill_policy":
        native = request.admission.agent.model_copy(
            update={
                "skills": [
                    BetaManagedAgentsCustomSkill.model_validate(
                        {
                            "type": "custom",
                            "skill_id": "skill",
                            "version": "1",
                            "future_execution_policy": policy,
                        }
                    )
                ]
            }
        )
        path = "skills.0.future_execution_policy"
    else:
        native = request.admission.agent.model_copy(
            update={
                "mcp_servers": [
                    BetaManagedAgentsMCPServerURLDefinition.model_validate(
                        {
                            "type": "url",
                            "name": "public",
                            "url": "https://public.invalid/mcp",
                            "future_execution_policy": policy,
                        }
                    )
                ]
            }
        )
        path = "mcp_servers.0.future_execution_policy"
    router.routes.clear()
    router.add_agent(native)
    router.add_environment(request.admission.environment)
    captured = await capture_preparation_source(request, fresh=True)
    extension = next(
        item
        for item in captured.effective_spec.extensions
        if item.namespace == "anthropic.unclassified_source"
    )
    assert extension.value == {"fields": [path]}
    assert "source_definition" in classify_source_obligations(captured, None, environment=None)
    before = await service.audit(source.tenant_id)
    with pytest.raises(AgentBackendRefused) as preview:
        await service.preview(source, "openai")
    assert preview.value.reason == "source_unavailable"
    with pytest.raises(AgentBackendRefused) as commit:
        await service.set_backend(
            source,
            "openai",
            accepted.local,
            actor,
            accepted_drops=accepted.declared_drops,
            expected_preview_digest=accepted.preview_digest,
        )
    assert commit.value.reason == "source_unavailable"
    assert await service.current(source) == accepted
    assert await service.audit(source.tenant_id) == before
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert refused.value.reason == "backend_unsupported"
    assert isinstance(refused.value.__cause__, AgentBackendRefused)
    assert refused.value.__cause__.reason == "re_confirm"
    assert not constructions and not targets
    assert all(call.method == "GET" for call in reads)


@pytest.mark.parametrize("unknown", ["root", "null_config", "config"])
async def test_unclassified_sdk_environment_refuses_consent_and_handoff(consent_request, unknown):
    request, service, source, actor, accepted, router, reads, constructions, targets, _ = (
        consent_request
    )
    native = request.admission.environment
    if unknown == "config":
        native = native.model_copy(
            update={
                "config": native.config.model_copy(
                    update={"future_execution_policy": {"requires_confirmation": True}}
                )
            }
        )
    else:
        updates = {"future_execution_policy": {"requires_confirmation": True}}
        if unknown == "null_config":
            updates["config"] = None
        native = native.model_copy(update=updates)
    router.routes.clear()
    router.add_agent(request.admission.agent)
    router.add_environment(native)
    captured = await capture_preparation_source(request, fresh=True)
    assert captured.environment.spec.native_config is not None
    assert "source_definition" in classify_source_obligations(captured, None, environment=None)
    before = await service.audit(source.tenant_id)
    with pytest.raises(AgentBackendRefused) as quote:
        await service.preview(source, "openai")
    assert quote.value.reason == "source_unavailable"
    with pytest.raises(AgentBackendRefused) as commit:
        await service.set_backend(
            source,
            "openai",
            accepted.local,
            actor,
            accepted_drops=accepted.declared_drops,
            expected_preview_digest=accepted.preview_digest,
        )
    assert commit.value.reason == "source_unavailable"
    assert await service.current(source) == accepted
    assert await service.audit(source.tenant_id) == before
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert isinstance(refused.value.__cause__, AgentBackendRefused)
    assert refused.value.__cause__.reason == "re_confirm"
    assert constructions == targets == []
    assert all(call.method == "GET" for call in reads)


async def test_known_implicit_environment_policy_remains_an_explicit_sandbox_loss(consent_request):
    request, service, source, _, _, router, _, _, _, _ = consent_request
    native = request.admission.environment.model_copy(update={"config": {"type": "cloud"}})
    router.routes.clear()
    router.add_agent(request.admission.agent)
    router.add_environment(native)
    captured = await capture_preparation_source(request, fresh=True)
    assert captured.environment.spec.native_config is not None
    assert "source_definition" not in classify_source_obligations(captured, None, environment=None)
    quote = await service.preview(source, "openai")
    assert quote.allowed and quote.declared_drops == ("memory", "sandbox")


@pytest.mark.parametrize(
    "surface",
    [
        "agent.root",
        "agent.root_null",
        "agent.model",
        "agent.skills",
        "agent.mcp_visible",
        "agent.mcp_hidden",
        "tools.builtin_root",
        "tools.builtin_default",
        "tools.builtin_config",
        "tools.builtin_policy",
        "tools.custom_root",
        "tools.mcp_root",
        "tools.mcp_default",
        "tools.mcp_config",
        "tools.mcp_policy",
        "tools.hidden_mcp_root",
        "tools.hidden_mcp_root_null",
        "tools.hidden_mcp_default",
        "tools.hidden_mcp_config",
        "tools.hidden_mcp_policy",
        "environment.root",
        "environment.root_null",
        "environment.null_config",
        "environment.config",
        "environment.networking",
        "environment.packages",
        "mount.file_binding",
        "mount.file_source",
        "mount.file_reference",
        "mount.repository",
        "mount.file_native",
        "mount.repository_native",
        "mount.repository_checkout",
        "value.agent.root.type",
        "value.agent.model.speed",
        "value.agent.multiagent.type",
        "value.agent.roster.type",
        "value.agent.skills.type",
        "value.agent.mcp_visible.type",
        "value.agent.mcp_hidden.type",
        "value.tools.builtin_root.type",
        "value.tools.builtin_config.name",
        "value.tools.builtin_config.type",
        "value.tools.builtin_default_policy.type",
        "value.tools.builtin_config_policy.type",
        "value.tools.custom_root.type",
        "value.tools.mcp_root.type",
        "value.tools.mcp_default_policy.type",
        "value.tools.mcp_config_policy.type",
        "value.tools.hidden_mcp_root.type",
        "value.tools.hidden_mcp_default_policy.type",
        "value.tools.hidden_mcp_config_policy.type",
        "value.environment.root.type",
        "value.environment.root.scope",
        "value.environment.config.type",
        "value.environment.networking.type",
        "value.environment.packages.type",
        "value.mount.file_native.type",
        "value.mount.repository_native.type",
        "value.mount.repository_checkout.type",
        "value.mount.memory_native.type",
        "value.mount.memory_native.access",
        "null.mount.memory_native.access",
        "value.mount.file_binding.kind",
        "value.mount.file_source.kind",
        "value.mount.file_reference.provider",
        "value.mount.repository.kind",
        "null.agent.root.type",
        "null.agent.multiagent.type",
        "null.agent.roster.type",
        "null.agent.skills.type",
        "null.agent.mcp_visible.type",
        "null.agent.mcp_hidden.type",
        "null.tools.builtin_root.type",
        "null.tools.builtin_config.name",
        "null.tools.builtin_config.type",
        "null.tools.builtin_default_policy.type",
        "null.tools.builtin_config_policy.type",
        "null.tools.custom_root.type",
        "null.tools.mcp_root.type",
        "null.tools.mcp_default_policy.type",
        "null.tools.mcp_config_policy.type",
        "null.tools.hidden_mcp_root.type",
        "null.tools.hidden_mcp_default_policy.type",
        "null.tools.hidden_mcp_config_policy.type",
        "null.environment.root.type",
        "null.environment.config.type",
        "null.environment.networking.type",
        "null.mount.file_native.type",
        "null.mount.repository_native.type",
        "null.mount.repository_checkout.type",
        "null.mount.memory_native.type",
        "null.mount.file_binding.kind",
        "null.mount.file_source.kind",
        "null.mount.file_reference.provider",
        "null.mount.repository.kind",
        "missing.agent.root.type",
        "missing.environment.root.type",
        "missing.agent.mcp_visible.type",
        "missing.agent.mcp_hidden.type",
        "missing.agent.skills.type",
        "missing.tools.hidden_mcp_root.type",
    ],
)
async def test_unknown_source_surface_cannot_be_confirmed_or_prepared(consent_request, surface):
    from uuid import uuid4

    import httpx
    from anthropic.types.beta import (
        BetaManagedAgentsFileResourceConfig,
        BetaManagedAgentsGitHubRepositoryResourceConfig,
        BetaManagedAgentsMemoryStoreResourceConfig,
    )
    from daimon.core.turn.runtime_manifest import preview_source_drops
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import ResourceBinding, WorkspaceSource
    from mux.errors import UnsupportedCapability
    from pydantic import ValidationError

    from ..test_sessions import _record_personal_sign_in

    request, service, source, actor, accepted, router, reads, constructions, targets, _ = (
        consent_request
    )
    before = await service.audit(source.tenant_id)
    policy = {"requires_confirmation": True}
    key = "future_execution_policy"
    value_case = surface.startswith(("value.", "null.", "missing."))
    omitted = surface.startswith("missing.")
    if value_case:
        value_kind, surface = surface.split(".", 1)
        surface, key = surface.rsplit(".", 1)
        policy = None if value_kind == "null" else "future_native_kind"
    if surface in {
        "mount.file_native",
        "mount.repository_native",
        "mount.repository_checkout",
        "mount.memory_native",
    }:
        captured = await capture_preparation_source(request, fresh=True)
        payload = (
            {"type": "file", "file_id": "owned-file", "mount_path": "document.txt"}
            if surface == "mount.file_native"
            else {"type": "memory_store", "memory_store_id": "owned-memory", "access": "read_only"}
            if surface == "mount.memory_native"
            else {"type": "github_repository", "url": "https://public.invalid/repo"}
        )
        container = payload
        if surface == "mount.repository_checkout":
            payload["checkout"] = {"type": "branch", "name": "main"}
            container = payload["checkout"]
        container[key] = policy
        native = (
            BetaManagedAgentsFileResourceConfig.model_construct(**payload)
            if surface == "mount.file_native" and value_case
            else BetaManagedAgentsFileResourceConfig.model_validate(payload)
            if surface == "mount.file_native"
            else BetaManagedAgentsMemoryStoreResourceConfig.model_construct(**payload)
            if surface == "mount.memory_native"
            else BetaManagedAgentsGitHubRepositoryResourceConfig.model_construct(**payload)
            if value_case
            else BetaManagedAgentsGitHubRepositoryResourceConfig.model_validate(payload)
        )
        raw_native = native.model_dump(mode="json", exclude_unset=True)
        if surface == "mount.memory_native" and key == "access" and policy is None:
            from mux.drivers.anthropic.sessions_lifecycle import MemoryMount

            assert MemoryMount.model_validate(raw_native).access is None
        assert (
            raw_native["checkout"][key]
            if surface == "mount.repository_checkout"
            else raw_native[key]
        ) == policy
        captured = captured.model_copy(
            update={
                "resource_requirements": (
                    ResourceBinding(
                        id="opaque-mount",
                        kind="artifact" if surface == "mount.file_native" else "repository",
                        target_path="document.txt" if surface == "mount.file_native" else ".",
                        native=ExtensionConfig(
                            namespace="anthropic.session_resource_create",
                            version=1,
                            value={"resource": raw_native},
                        ),
                    ),
                )
            }
        )
        with pytest.raises(UnsupportedCapability) as refused:
            preview_source_drops(captured, ModelRef(provider="openai", id="gpt-6-luna"))
        assert "source_definition" in refused.value.missing
        assert await service.current(source) == accepted
        assert await service.audit(source.tenant_id) == before
        assert constructions == targets == []
        assert reads and all(call.method == "GET" for call in reads)
        return
    if surface.startswith("mount."):
        if surface == "mount.file_binding":
            schema, payload = (
                ResourceBinding,
                {"id": "owned-file", "kind": "artifact", "target_path": "document.txt"},
            )
        else:
            schema, payload = (
                WorkspaceSource,
                {
                    "kind": "repository" if surface == "mount.repository" else "file",
                    "target_path": "." if surface == "mount.repository" else "document.txt",
                },
            )
            if surface == "mount.repository":
                payload["repository_url"] = "https://public.invalid/repo"
            if surface == "mount.file_reference":
                payload["artifact"] = {**source.source.ref.model_dump(mode="json"), "kind": "file"}
        schema.model_validate(payload)
        container = payload["artifact"] if surface == "mount.file_reference" else payload
        container[key] = policy
        with pytest.raises(ValidationError) as refused:
            schema.model_validate(payload)
        assert {error["type"] for error in refused.value.errors()} == {
            "literal_error" if value_case else "extra_forbidden"
        }
        assert await service.current(source) == accepted
        assert await service.audit(source.tenant_id) == before
        assert reads == constructions == targets == []
        return

    agent = request.admission.agent.model_dump(mode="json", exclude_unset=True)
    environment = request.admission.environment.model_dump(mode="json", exclude_unset=True)
    hidden = "hidden" in surface
    if hidden:
        async with request.deps.sessionmaker() as db:
            await _record_personal_sign_in(
                db, tenant_id=source.tenant_id, account_id=uuid4(), agent_uuid=source.agent_id
            )
    container = agent
    if surface == "agent.model":
        container = agent["model"]
    elif surface in {"agent.multiagent", "agent.roster"}:
        agent["multiagent"] = {
            "type": "coordinator",
            "agents": [{"type": "agent", "id": "owned-agent", "version": 1}],
        }
        container = agent["multiagent"]
        if surface == "agent.roster":
            container = container["agents"][0]
    elif surface == "agent.skills":
        agent["skills"] = [{"type": "custom", "skill_id": "bundle", "version": "1"}]
        container = agent["skills"][0]
    elif "mcp" in surface:
        agent["mcp_servers"] = [
            {"type": "url", "name": "docs", "url": "https://mcp.example.com/docs"}
        ]
        if value_case:
            agent["tools"] = [
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "docs",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ]
        container = agent["mcp_servers"][0]
    if surface.startswith("tools."):
        if surface == "tools.custom_root":
            tool = {
                "type": "custom",
                "name": "helper",
                "description": "Read-only helper",
                "input_schema": {"type": "object", "properties": {}},
            }
        else:
            tool = {
                "type": "mcp_toolset" if "mcp" in surface else "agent_toolset_20260401",
                "configs": [
                    {
                        "name": "helper" if "mcp" in surface else "bash",
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
            if "mcp" in surface:
                tool["mcp_server_name"] = "docs"
            if surface in {"tools.hidden_mcp_root", "tools.hidden_mcp_root_null"}:
                tool["configs"] = []
        agent["tools"] = [tool]
        container = tool
        if surface.endswith("_default"):
            container = tool["default_config"]
        elif surface.endswith("_config"):
            container = tool["configs"][0]
        elif surface.endswith("_config_policy"):
            container = tool["configs"][0]["permission_policy"]
        elif surface.endswith("_policy"):
            container = tool["default_config"]["permission_policy"]
    elif surface.startswith("environment."):
        container = environment
        if surface == "environment.null_config":
            environment["config"] = None
        elif surface not in {"environment.root", "environment.root_null"}:
            environment["config"] = {
                "type": "cloud",
                "networking": {"type": "unrestricted"},
                "packages": {name: [] for name in ("apt", "pip", "npm", "cargo", "gem", "go")},
            }
            container = environment["config"]
            if surface == "environment.networking":
                container = container["networking"]
            elif surface == "environment.packages":
                container = container["packages"]
    if omitted:
        del container[key]
    else:
        container[key] = None if surface.endswith("_null") else policy
        if value_case and surface == "environment.root" and key == "scope" and policy is not None:
            container[key] = "future_per_session"
        if value_case and surface == "agent.mcp_hidden" and policy is not None:
            container[key] = "future_private_url"
    router.routes.clear()
    router.add(
        "GET",
        "/v1/agents/" + request.admission.agent.id,
        lambda call, match: httpx.Response(200, json=agent),
    )
    router.add(
        "GET",
        "/v1/environments/" + request.admission.environment.id,
        lambda call, match: httpx.Response(200, json=environment),
    )
    captured = await capture_preparation_source(request, fresh=True)
    if hidden:
        assert captured.effective_spec.mcp_servers == ()
        if not (value_case and surface.startswith("tools.") and key == "type"):
            assert not any(
                extension.value.get("tools") for extension in captured.effective_spec.extensions
            )
    with pytest.raises(AgentBackendRefused) as preview:
        await service.preview(source, "openai")
    assert preview.value.reason == "source_unavailable"
    with pytest.raises(AgentBackendRefused) as commit:
        await service.set_backend(
            source,
            "openai",
            accepted.local,
            actor,
            accepted_drops=accepted.declared_drops,
            expected_preview_digest=accepted.preview_digest,
        )
    assert commit.value.reason == "source_unavailable"
    assert await service.current(source) == accepted
    assert await service.audit(source.tenant_id) == before
    with pytest.raises(AdmissionDenied) as handoff:
        await read_preparation_counterpart(request)
    assert isinstance(handoff.value.__cause__, AgentBackendRefused)
    assert handoff.value.__cause__.reason == "re_confirm"
    assert constructions == targets == []
    assert reads and all(call.method == "GET" for call in reads)


async def test_known_hidden_oauth_toolset_keeps_existing_projection(consent_request):
    from uuid import uuid4

    from anthropic.types.beta import BetaManagedAgentsAgent

    from ..test_sessions import _record_personal_sign_in

    request, service, source, _, _, router, _, _, _, _ = consent_request
    async with request.deps.sessionmaker() as db:
        await _record_personal_sign_in(
            db, tenant_id=source.tenant_id, account_id=uuid4(), agent_uuid=source.agent_id
        )
    payload = request.admission.agent.model_dump(mode="json", exclude_unset=True)
    payload["mcp_servers"] = [
        {"type": "url", "name": "docs", "url": "https://mcp.example.com/docs"}
    ]
    payload["tools"] = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "docs",
            "configs": [],
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        }
    ]
    router.routes.clear()
    router.add_agent(BetaManagedAgentsAgent.model_validate(payload))
    router.add_environment(request.admission.environment)
    captured = await capture_preparation_source(request, fresh=True)
    assert captured.effective_spec.mcp_servers == ()
    assert not any(extension.value.get("tools") for extension in captured.effective_spec.extensions)
    assert "source_definition" not in classify_source_obligations(captured, None, environment=None)
    quote = await service.preview(source, "openai")
    assert quote.allowed and quote.declared_drops == ("memory", "sandbox")


@pytest.mark.parametrize(
    "surface",
    [
        "old.agent_policy",
        "old.agent_null",
        "old.raw_model",
        "old.environment_root",
        "old.environment_config",
        "old.skill_field",
        "old.hidden_mcp_field",
        "old.hidden_mcp_tool",
        "known.mcp",
        "known.hidden_mcp",
        "known.skill",
        "new.mcp_future",
        "new.mcp_null",
        "new.hidden_mcp_future",
        "new.hidden_mcp_null",
        "new.skill_future",
        "new.skill_null",
    ],
)
async def test_raw_source_pipeline_discriminators_keep_public_consent_closed(
    consent_request, surface
):
    from uuid import uuid4

    import httpx

    from ..test_sessions import _record_personal_sign_in

    request, service, source, actor, accepted, router, reads, constructions, targets, _ = (
        consent_request
    )
    agent = request.admission.agent.model_dump(mode="json", exclude_unset=True)
    environment = request.admission.environment.model_dump(mode="json", exclude_unset=True)
    unknown = {"requires_confirmation": True}
    if surface in {"old.agent_policy", "old.agent_null"}:
        agent["future_execution_policy"] = None if surface.endswith("null") else unknown
    elif surface == "old.raw_model":
        agent["model"]["future_execution_policy"] = unknown
    elif surface.startswith("old.environment"):
        container = environment if surface.endswith("root") else environment["config"]
        container["future_execution_policy"] = unknown
    if "mcp" in surface:
        kind = (
            None
            if surface.endswith("null")
            else "future_oauth_gateway"
            if "future" in surface
            else "url"
        )
        agent["mcp_servers"] = [
            {"type": kind, "name": "docs", "url": "https://mcp.example.com/docs"}
        ]
        if surface == "old.hidden_mcp_field":
            agent["mcp_servers"][0]["future_execution_policy"] = unknown
        if surface == "old.hidden_mcp_tool":
            agent["tools"] = [
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "docs",
                    "configs": [],
                    "future_execution_policy": {},
                }
            ]
    if "skill" in surface:
        kind = (
            None
            if surface.endswith("null")
            else "future_secret_catalog"
            if "future" in surface
            else "custom"
        )
        agent["skills"] = [{"type": kind, "skill_id": "bundle", "version": "1"}]
        if surface == "old.skill_field":
            agent["skills"][0]["future_execution_policy"] = unknown
    if "hidden" in surface:
        async with request.deps.sessionmaker() as db:
            await _record_personal_sign_in(
                db, tenant_id=source.tenant_id, account_id=uuid4(), agent_uuid=source.agent_id
            )
    router.routes.clear()
    router.add(
        "GET",
        "/v1/agents/" + request.admission.agent.id,
        lambda *_: httpx.Response(200, json=agent),
    )
    router.add(
        "GET",
        "/v1/environments/" + request.admission.environment.id,
        lambda *_: httpx.Response(200, json=environment),
    )
    before = await service.audit(source.tenant_id)
    captured = await capture_preparation_source(request, fresh=True)
    assert captured.source.native == agent
    assert captured.environment.native == environment
    if surface.startswith("known."):
        quote = await service.preview(source, "openai")
        assert quote.allowed
        assert set(quote.declared_drops) == {"memory", "sandbox"} | (
            {"skills"} if surface == "known.skill" else {"mcp"} if surface == "known.mcp" else set()
        )
    else:
        with pytest.raises(AgentBackendRefused) as preview:
            await service.preview(source, "openai")
        assert preview.value.reason == "source_unavailable"
        with pytest.raises(AgentBackendRefused) as commit:
            await service.set_backend(
                source,
                "openai",
                accepted.local,
                actor,
                accepted_drops=accepted.declared_drops,
                expected_preview_digest=accepted.preview_digest,
            )
        assert commit.value.reason == "source_unavailable"
        with pytest.raises(AdmissionDenied) as handoff:
            await read_preparation_counterpart(request)
        assert isinstance(handoff.value.__cause__, AgentBackendRefused)
        assert handoff.value.__cause__.reason == "re_confirm"
    assert await service.current(source) == accepted
    assert await service.audit(source.tenant_id) == before
    assert constructions == targets == []
    assert reads and all(call.method == "GET" for call in reads)


@pytest.mark.parametrize(
    "surface", ["agent.model.speed", "environment.scope", "environment.packages.type"]
)
async def test_schema_supported_null_enum_keeps_existing_consent(consent_request, surface):
    import httpx

    request, service, source, _, accepted, router, _, constructions, targets, _ = consent_request
    agent = request.admission.agent.model_dump(mode="json", exclude_unset=True)
    environment = request.admission.environment.model_dump(mode="json", exclude_unset=True)
    if surface == "agent.model.speed":
        agent["model"]["speed"] = None
    elif surface == "environment.scope":
        environment["scope"] = None
    else:
        environment["config"]["packages"]["type"] = None
    router.routes.clear()
    router.add(
        "GET",
        "/v1/agents/" + request.admission.agent.id,
        lambda *_: httpx.Response(200, json=agent),
    )
    router.add(
        "GET",
        "/v1/environments/" + request.admission.environment.id,
        lambda *_: httpx.Response(200, json=environment),
    )
    before = await service.audit(source.tenant_id)
    quote = await service.preview(source, "openai")
    assert quote.allowed and quote.declared_drops == ("memory", "sandbox")
    assert await service.current(source) == accepted
    assert await service.audit(source.tenant_id) == before
    assert constructions == targets == []
