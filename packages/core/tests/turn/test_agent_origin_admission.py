"""Normal ingress refuses incomplete source authority before revision publication."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core.stores import mux_state
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_hold import RequesterOrg
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.resources import Agent, AgentSpec
from structlog.testing import capture_logs

from .test_backend_selection import (
    initial_backend as initial_backend,
)
from .test_backend_selection import (
    preference_for,
)
from .test_backend_selection import (
    selector_host as selector_host,
)
from .test_provider_dispatch import REVISION, TENANT


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("accepted", [False, True])
async def test_real_admission_refuses_even_a_reader_accepting_the_native_component(
    selector_host, provider, accepted
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, provider, 0, actor)
    reads = []

    async def proof(request):
        candidate = request.admission.backend_revision
        assert candidate is not None and candidate.backend == provider
        assert request.admission.backend_revision_origin == "agent"
        assert request.admission.agent.id == "ag_parity_test"
        assert request.scope.tenant_id == str(TENANT)
        assert request.scope.account_id == str(request.admission.account_id)
        async with host.sessions() as db:
            assert (
                await mux_state.get_config_revision(db, candidate.channel, candidate.local) is None
            )
        row = AgentRevision(
            catalog_id="verified",
            local_revision=1,
            principal_id=request.scope.principal_id,
            agent=Agent(
                ref=ResourceRef(
                    id="native-counterpart",
                    kind="agent",
                    provider=provider,
                    account_scope_id="project",
                    tenant_id=request.scope.tenant_id,
                    account_id=request.scope.account_id,
                ),
                revision=Revision(local=0 if provider == "openai" else 1),
                spec=AgentSpec(
                    name="retained", model=ModelRef(provider=provider, id=candidate.model)
                ),
                created_at=datetime.now(UTC),
            ),
        )
        reads.append((candidate, row))
        if not accepted:
            raise RuntimeError("private provider diagnostic")
        return row

    existing = next(iter(host.deps.turn_runtimes.values()))
    profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
    deps = replace(
        host.deps,
        agent_backends=True,
        turn_runtimes={profile: replace(existing, read_agent_counterpart=proof)},
    )
    kwargs = dict(
        tenant_id=TENANT,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id="new",
        now=datetime.now(UTC),
    )
    with (
        capture_logs() as logs,
        pytest.raises(AdmissionDenied, match="backend_unsupported") as refused,
    ):
        await admit_impl(deps, **kwargs)
    diagnostics = [row for row in logs if row["event"] == "turn.backend_unsupported"]
    assert diagnostics[-1]["stage"] == "preparation_counterpart"
    first = diagnostics[-1]["causes"][0]
    # The refusal names the code site that raised it (SMOKE-1: 12+ candidate sites).
    assert first.pop("site").startswith("turn.runtime_manifest.read_preparation_counterpart:")
    assert first == {"type": "AdmissionDenied", "reason": "backend_unsupported"}
    assert any(cause["type"] == "UnsupportedCapability" for cause in diagnostics[-1]["causes"])
    assert "private" not in str(refused.value)
    assert (refused.value.cause, refused.value.provider) == ("agent_backend", provider)
    async with host.sessions() as db:
        assert await mux_state.latest_config_revision(db, REVISION.channel) is None
        assert await mux_state.next_config_local(db, REVISION.channel) == 1
    assert reads == [], "refusal must precede native component readback and INSERT"
    assert host.wire.requests == [] and host.created == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_prewrite_source_projection_receives_effective_seal_and_memory_policy(
    selector_host, provider, monkeypatch
):
    from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.turn import runtime_manifest

    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, provider, 0, actor)
    policy = TenantAccessPolicy(
        enabled_backends=("anthropic", "openai", "gemini"),
        channel_rules={"channel": ChannelRule(readers="inside")},
        agent_rules={"test-agent": AgentRule(runs_in=("channel",))},
    )
    async with host.sessions.begin() as db:
        await set_access_policy(db, tenant_id=TENANT, policy=policy)
    seen = []

    async def refuse(request):
        captured = request.admission
        assert captured.source_sealed
        assert captured.memory_read_only
        assert captured.asks_before_publishing
        assert captured.origin_seal_ids == frozenset({"channel"})
        assert captured.origin_channel_id == "channel" and captured.origin_thread_id == "new"
        assert captured.channel_skills == ()
        assert request.scope.account_id == str(captured.account_id)
        seen.append(request)
        raise AdmissionDenied(reason="backend_unsupported")

    # Isolate the context passed to the awaited proof; the proof itself refuses.
    monkeypatch.setattr(runtime_manifest, "read_preparation_counterpart", refuse)
    existing = next(iter(host.deps.turn_runtimes.values()))
    profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
    deps = replace(host.deps, agent_backends=True, turn_runtimes={profile: existing})
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await admit_impl(
            deps,
            tenant_id=TENANT,
            requester_org=RequesterOrg.HOST,
            platform="slack",
            external_user_id="caller",
            channel_id="channel",
            thread_id="new",
            now=datetime.now(UTC),
        )
    assert len(seen) == 1
    async with host.sessions() as db:
        assert await mux_state.latest_config_revision(db, REVISION.channel) is None
    assert host.created == [] and host.wire.requests == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("owner", ["load_backend_agent", "resolve_turn_backend"])
async def test_agent_preference_owner_refusal_logs_its_stage_and_cause(
    selector_host, monkeypatch, owner
):
    from daimon.core import channel_backend

    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "openai", 0, actor)

    async def refuse(*args, **kwargs):
        raise channel_backend.AgentBackendRefused("source_unavailable")

    monkeypatch.setattr(channel_backend, owner, refuse)
    with (
        capture_logs() as logs,
        pytest.raises(AdmissionDenied, match="backend_unsupported") as refused,
    ):
        await host.admit("new-thread")
    # The copy names the agent's backend as the cause, not the channel.
    assert refused.value.cause == "agent_backend"
    diagnostic = next(row for row in logs if row["event"] == "turn.backend_unsupported")
    assert diagnostic["stage"] == (
        "source_agent_lookup" if owner == "load_backend_agent" else "agent_preference"
    )
    assert diagnostic["causes"] == [{"type": "AgentBackendRefused", "reason": "source_unavailable"}]
    assert host.wire.requests == [] and host.created == []


async def test_reauthorization_logs_a_removed_workspace_backend(selector_host):
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.turn.admission import reauthorize

    host = selector_host
    admission = await host.admit("thread")
    async with host.sessions.begin() as db:
        await set_access_policy(db, tenant_id=TENANT, policy=TenantAccessPolicy())
    before = (len(host.wire.requests), len(host.calls))
    with (
        capture_logs() as logs,
        pytest.raises(AdmissionDenied, match="backend_unsupported") as refused,
    ):
        await reauthorize(host.deps, admission)
    assert refused.value.cause == "provider_not_allowed"
    assert refused.value.provider == admission.backend_revision.backend
    assert any(row.get("stage") == "workspace_policy" for row in logs)
    assert any(row.get("stage") == "reauthorize_policy" for row in logs)
    assert (len(host.wire.requests), len(host.calls)) == before


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_an_agent_on_a_provider_the_allowlist_removed_names_that_provider(selector_host):
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "openai", 0, actor)
    async with host.sessions.begin() as db:
        await set_access_policy(db, tenant_id=TENANT, policy=TenantAccessPolicy())
    with pytest.raises(AdmissionDenied, match="backend_unsupported") as refused:
        await host.admit("new-thread")
    assert (refused.value.cause, refused.value.provider) == ("provider_not_allowed", "openai")
    assert host.wire.requests == [] and host.created == []
