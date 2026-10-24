"""Confirmed ordinary source losses reach native Gemini readback and host preparation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import GeminiJournal
from daimon.core.turn.gemini_deployment import GeminiPlan
from daimon.core.turn.gemini_prepare import prepare_gemini_turn
from daimon.core.turn.gemini_state import SQLiteGeminiStorage
from daimon.core.turn.prepare import admitted_session_scope, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import prepare_source, read_preparation_counterpart
from daimon.core.turn.runtimes import build_channel_runtime, scope_channel_runtimes
from daimon.core.turn.state import extract_final_response
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.resources import EnvironmentSpec, MCPConnection, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.deployment import configured_catalog_transport, configured_transport
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.drivers.gemini.transport import NO_TOOLS_TOOL

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_codec import interaction
from .test_gemini_runtime_transport import clients_for


@pytest.fixture
async def ordinary(consent_request, monkeypatch, tmp_path):
    request, service, source, actor, old, router, source_reads, *_ = consent_request
    quote = await service.preview(source, "gemini")
    accepted = await service.set_backend(
        source,
        "gemini",
        old.local,
        actor,
        accepted_drops=quote.declared_drops,
        expected_preview_digest=quote.digest,
    )
    revision = ConfigRevision.create(
        request.admission.backend_revision.channel,
        2,
        resolve_default(
            BackendConfig(backend="gemini", profile="gemini.inline_reuse", model=FLASH_PRIMARY)
        ),
    )
    sessions = request.deps.sessionmaker
    async with sessions.begin() as db:
        await mux_state.put_config_revision(db, revision, origin="agent", agent_revision=accepted)
    request = replace(
        request,
        scope=admitted_session_scope(
            request.admission, tenant_id=request.tenant_id, session_id="preparation"
        ),
        admission=replace(request.admission, backend_revision=revision),
        deps=replace(
            request.deps,
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
            state_store=PostgresStateStore(sessions),
        ),
    )
    canonical = await prepare_source(request)
    native, calls, posts = {}, [], []
    native_id = None

    def handle(call):
        nonlocal native_id
        calls.append(call)
        if call.url.path.startswith("/v1beta/agents"):
            if call.method == "POST":
                native.update(json.loads(call.content))
                native_id = native["id"]
            else:
                assert call.method == "GET" and call.url.path.endswith(native_id)
            return httpx.Response(200, json=native)
        assert call.url.host == "generativelanguage.googleapis.com"
        if call.method == "POST":
            body = json.loads(call.content)
            posts.append(body)
            return httpx.Response(200, json=interaction(f"turn-{len(posts)}"))
        return httpx.Response(200, json=interaction(call.url.path.rsplit("/", 1)[-1]))

    clients = clients_for(monkeypatch, handle)
    backend = GeminiManagedAgents(
        configured_transport(api_key="offline-declared-source"),
        storage=SQLiteGeminiStorage(
            tmp_path / "records.sqlite",
            namespace=json.dumps(
                [request.scope.tenant_id, request.scope.account_id, "project"],
                separators=(",", ":"),
            ),
        ),
        state_store=request.deps.state_store,
        account_scope_id="project",
        resource_catalog_enabled=True,
        resource_catalog_transport=configured_catalog_transport(api_key="offline-declared-source"),
    )
    host = NeutralResourceHost(
        backend,
        sessions,
        scope=request.scope,
        account_scope_id="project",
        operation_key="ordinary-publication",
        resource_catalog_enabled=True,
        catalog_adapter=backend.catalog_adapter,
    )
    counterpart = await host.publish(
        "ordinary-counterpart",
        canonical.target_spec,
        expected_revision=0,
        key="ordinary-publication",
    )
    plan = GeminiPlan(
        profile=revision.profile,
        tenant_id=request.scope.tenant_id,
        platform=request.platform,
        channel_id=revision.channel.channel_id,
        account_id=request.scope.account_id,
        config_digest=revision.digest,
        project="project",
        api_key_env="OFFLINE_DECLARED_GEMINI_KEY",
        records_path=tmp_path / "records.sqlite",
        holds_path=tmp_path / "holds.sqlite",
        hold_usd="0.05",
        cap_usd="0.25",
        agent_name="operator-label-is-not-the-source",
        system="Operator text must never replace the source.",
        tools=(),
        environment=EnvironmentSpec(name="ordinary-remote"),
        agent_id=counterpart.agent.ref.id,
        source_agent_id=source.agent_id,
        source_agent_version=request.admission.agent.version,
        counterpart_catalog_id=counterpart.catalog_id,
        counterpart_catalog_revision=counterpart.local_revision,
    )
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps([plan.model_dump(mode="json")]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    monkeypatch.setenv(plan.api_key_env, "offline-declared-source")
    calls.clear()
    yield (
        request,
        plan,
        canonical,
        native,
        calls,
        posts,
        clients,
        router,
        service,
        source,
        actor,
        accepted,
    )
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("first_reuse", [True, False])
async def test_ordinary_confirmed_target_native_readback_and_two_real_host_turns(
    ordinary, first_reuse
):
    await assert_ordinary_native_preparation_and_turns(ordinary, first_reuse=first_reuse)


@pytest.mark.parametrize(
    "consent_request",
    [
        {
            "backend": "gemini",
            "caller_mcp": True,
            "model": {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}},
        }
    ],
    indirect=True,
)
async def test_portable_caller_mcp_and_model_losses_reach_native_gemini_preparation(ordinary):
    assert ordinary[2].target_spec.declared_drops == ("mcp", "memory", "model_controls", "sandbox")
    await assert_ordinary_native_preparation_and_turns(ordinary)


async def assert_ordinary_native_preparation_and_turns(ordinary, *, first_reuse=True):
    request, plan, canonical, native, calls, posts, _, *_ = ordinary
    original = request
    service, source, actor, accepted = ordinary[8:]
    await service.clear_backend(source, accepted.local, actor)
    request, lookup = scope_channel_runtimes(request)
    selected = await read_preparation_counterpart(request)
    assert selected.worker is None
    assert selected.target_spec == canonical.target_spec
    assert selected.verified_counterpart == selected.counterpart
    assert isinstance(lookup.resolved.journal, GeminiJournal)
    deployment = lookup.resolved.journal.deployment
    assert deployment.agent == canonical.target_spec
    assert deployment.max_total_tokens is deployment.max_output_tokens is None
    assert selected.target_spec.system.startswith("Authoritative source instructions\n\n")
    assert all(call.method == "GET" for call in calls)
    assert (
        calls
        and native["tools"] == []
        and native["base_environment"] == {"type": "remote", "sources": []}
    )
    session = None
    for index in range(2):
        prepared = await bind_session_impl(
            original.deps,
            original.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=first_reuse if index == 0 else True,
        )
        assert prepared.runtime.journal.deployment.native_agent.spec == canonical.target_spec
        if session is not None:
            assert prepared.ma_session_id == session
        session = prepared.ma_session_id

        async def no_recovery():
            pytest.fail("ordinary Gemini changed its admitted source/session")

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            external_user_id=request.external_user_id,
            user_message="Question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            operation_key=f"ordinary:{index}",
        )
        assert outcome.state.error is None and not outcome.recovered
        assert extract_final_response(outcome.state.content) == "answer"
    assert len(posts) == 2
    entries = await original.deps.state_store.read_events(session)
    for kind in ("agent.message", "session.turn_ended", "usage.observed"):
        records = [entry for entry in entries if entry.type == kind]
        assert len(records) == 2
        assert len({entry.id for entry in records}) == 2
    for body in posts:
        assert body["agent"] == native["id"]
        assert body["agent_config"] == {"type": "antigravity", "model": FLASH_PRIMARY}
        # Every interaction allows only the no-tools sentinel: no built-in runs.
        assert body["tools"] == [NO_TOOLS_TOOL]
        assert body["system_instruction"] == canonical.target_spec.system
        assert "generation_config" not in body
    assert posts[0]["environment"] == {"type": "remote", "sources": []}
    assert posts[1]["environment"] == "workspace"
    assert posts[1]["previous_interaction_id"] == "turn-1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", [{"type": "code_execution"}]),
        (
            "base_environment",
            {
                "type": "remote",
                "sources": [{"type": "repository", "url": "https://example.invalid/source"}],
            },
        ),
        ("system_instruction", "Changed source"),
        ("agent_config", {"type": "antigravity", "model": "gemini-3.5-pro"}),
        ("id", "foreign-agent"),
    ],
)
async def test_ordinary_native_mutation_cannot_reach_session_or_input(ordinary, field, value):
    request, _, _, native, calls, posts, *_ = ordinary
    native[field] = value
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    assert posts == [] and calls and all(call.method == "GET" for call in calls)


@pytest.mark.parametrize(
    "missing", ["source", "target_spec", "counterpart", "verified_counterpart"]
)
async def test_ordinary_preparation_requires_each_shared_native_proof_fact(ordinary, missing):
    request, _, _, _, calls, posts, *_ = ordinary
    selected = await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    calls.clear()
    with pytest.raises(AdmissionDenied):
        await prepare_gemini_turn(replace(selected, **{missing: None}))
    assert calls == posts == []


@pytest.mark.parametrize(
    "defect",
    [
        "memory",
        "system",
        "name",
        "model",
        "tools",
        "mcp",
        "operator_tools",
        "cap",
        "materialization",
    ],
)
def test_ordinary_compiler_refuses_unproven_native_intent(ordinary, defect):
    request, plan, canonical, _, calls, posts, *_ = ordinary
    changes = {
        "memory": {
            "declared_drops": tuple(
                d for d in canonical.target_spec.declared_drops if d != "memory"
            )
        },
        "system": {"system": "Replacement instructions"},
        "name": {"name": "Replacement identity"},
        "model": {"model": canonical.target_spec.model.model_copy(update={"id": "gemini-3.5-pro"})},
        "tools": {"tools": (ToolSpec(kind="builtin", name="code_execution"),)},
        "mcp": {
            "mcp_servers": (MCPConnection(name="unproven", url="https://example.invalid/mcp"),)
        },
    }
    if defect in changes:
        canonical = replace(
            canonical, target_spec=canonical.target_spec.model_copy(update=changes[defect])
        )
    else:
        plan = plan.model_copy(
            update={
                "operator_tools": {"tools": (ToolSpec(kind="builtin", name="code_execution"),)},
                "cap": {"max_output_tokens": 4096},
                "materialization": {"worker_materialization": True},
            }[defect]
        )
    with pytest.raises(AdmissionDenied):
        plan.deployment(canonical)
    assert calls == posts == []


@pytest.mark.parametrize("defect", ["consent", "source_drift", "tenant", "account"])
async def test_unconfirmed_or_foreign_source_refuses_before_factory(ordinary, monkeypatch, defect):
    request, _, canonical, _, calls, posts, _, router, *_ = ordinary
    constructed = []

    def forbidden(actual):
        constructed.append(actual)
        pytest.fail("unconfirmed source reached native runtime construction")

    if defect == "consent":
        request = replace(
            request,
            target_spec=canonical.target_spec,
            admission=replace(
                request.admission,
                backend_revision=request.admission.backend_revision.model_copy(
                    update={"local": 999}
                ),
            ),
        )
    elif defect == "source_drift":
        path = "/v1/agents/" + request.admission.agent.id
        router.routes = [
            row for row in router.routes if not (row[0] == "GET" and row[1].fullmatch(path))
        ]
        router.add_agent(
            request.admission.agent.model_copy(update={"system": "Changed source instructions"})
        )
    elif defect == "tenant":
        from uuid import uuid4

        request = replace(request, tenant_id=uuid4())
    else:
        from uuid import uuid4

        request = replace(request, session_account_id=uuid4())
    request = replace(request, deps=replace(request.deps, turn_runtime_factory=forbidden))
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    assert constructed == calls == posts == []


@pytest.mark.parametrize("model", ["gemini-4-argon"])
def test_ordinary_compiler_runs_a_picked_model_only_when_target_and_admission_agree(
    ordinary, model
):
    request, plan, canonical, _, calls, posts, *_ = ordinary
    revision = canonical.admission.backend_revision
    assert revision is not None
    picked = replace(
        canonical,
        admission=replace(
            canonical.admission, backend_revision=revision.model_copy(update={"model": model})
        ),
        target_spec=canonical.target_spec.model_copy(
            update={"model": canonical.target_spec.model.model_copy(update={"id": model})}
        ),
    )
    deployment = plan.deployment(picked)
    assert deployment.agent.model.id == model
    # A target on another model than the admitted one never compiles.
    with pytest.raises(AdmissionDenied):
        plan.deployment(replace(picked, target_spec=canonical.target_spec))
    with pytest.raises(AdmissionDenied):
        plan.deployment(replace(canonical, admission=picked.admission))
    assert calls == posts == []


def _as_template(plan, tmp_path, monkeypatch):
    """The same deployment as a tenant-wide agent template: no source or counterpart pins."""
    template = plan.model_copy(
        update={
            "platform": "*",
            "channel_id": "*",
            "account_id": "*",
            "agent_id": None,
            "source_agent_id": None,
            "source_agent_version": None,
            "counterpart_catalog_id": None,
            "counterpart_catalog_revision": None,
            "root_materialization": True,
        }
    )
    row = GeminiPlan.model_validate(template.model_dump(mode="json"))
    path = tmp_path / "template.json"
    path.write_text(json.dumps([row.model_dump(mode="json")]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    return row


async def test_a_template_row_publishes_the_agent_version_once_and_runs_a_real_turn(
    ordinary, tmp_path, monkeypatch
):
    from daimon.core.turn.runtime_manifest import root_counterpart_catalog_id

    request, plan, canonical, native, calls, posts, *_ = ordinary
    service, source, actor, accepted = ordinary[8:]
    _as_template(plan, tmp_path, monkeypatch)
    await service.clear_backend(source, accepted.local, actor)

    def agent_posts():
        return [c for c in calls if c.method == "POST" and c.url.path.startswith("/v1beta/agents")]

    scoped, lookup = scope_channel_runtimes(request)
    first = await read_preparation_counterpart(scoped)
    assert first.verified_counterpart == first.counterpart
    assert first.counterpart.catalog_id == root_counterpart_catalog_id(first)
    assert first.counterpart.agent.spec == canonical.target_spec
    assert len(agent_posts()) == 1
    again, _ = scope_channel_runtimes(request)
    second = await read_preparation_counterpart(again)
    assert second.counterpart == first.counterpart
    assert len(agent_posts()) == 1, "a second turn reuses the published counterpart"
    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
    )
    assert prepared.runtime.journal.deployment.native_agent.spec == canonical.target_spec

    async def no_recovery():
        pytest.fail("the template turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        external_user_id=request.external_user_id,
        user_message="Question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key="template:0",
    )
    assert outcome.state.error is None and extract_final_response(outcome.state.content) == "answer"
    assert len(agent_posts()) == 1


def test_root_catalog_ids_change_with_version_model_and_target(ordinary):
    from daimon.core.turn.runtime_manifest import root_counterpart_catalog_id

    canonical = ordinary[2]
    base = root_counterpart_catalog_id(canonical)
    agent = canonical.admission.agent
    bumped = replace(
        canonical,
        admission=replace(
            canonical.admission, agent=agent.model_copy(update={"version": agent.version + 1})
        ),
    )
    revision = canonical.admission.backend_revision
    argon = replace(
        canonical,
        admission=replace(
            canonical.admission,
            backend_revision=revision.model_copy(update={"model": "gemini-4-argon"}),
        ),
    )
    other = replace(
        canonical, target_spec=canonical.target_spec.model_copy(update={"system": "Other"})
    )
    ids = {
        base,
        root_counterpart_catalog_id(bumped),
        root_counterpart_catalog_id(argon),
        root_counterpart_catalog_id(other),
    }
    assert len(ids) == 4 and root_counterpart_catalog_id(canonical) == base


@pytest.mark.parametrize(
    "pin",
    [
        {"source_agent_id": "00000000-0000-0000-0000-000000000001"},
        {"source_agent_version": 3},
        {"counterpart_catalog_id": "pinned"},
        {"agent_id": "native"},
        {"worker_materialization": True},
    ],
)
def test_a_template_row_can_pin_no_identity(ordinary, tmp_path, monkeypatch, pin):
    from pydantic import ValidationError

    row = _as_template(ordinary[1], tmp_path, monkeypatch)
    with pytest.raises(ValidationError):
        GeminiPlan.model_validate({**row.model_dump(mode="json"), **pin})


async def test_two_concurrent_first_turns_publish_one_counterpart(ordinary, tmp_path, monkeypatch):
    from daimon.core.turn.runtime_manifest import root_counterpart_catalog_id

    request, plan, canonical, native, calls, posts, *_ = ordinary
    service, source, actor, accepted = ordinary[8:]
    _as_template(plan, tmp_path, monkeypatch)
    await service.clear_backend(source, accepted.local, actor)
    first, _ = scope_channel_runtimes(request)
    second, _ = scope_channel_runtimes(request)
    a, b = await asyncio.gather(
        read_preparation_counterpart(first), read_preparation_counterpart(second)
    )
    assert a.counterpart == b.counterpart
    assert a.counterpart.catalog_id == root_counterpart_catalog_id(a)
    creates = [c for c in calls if c.method == "POST" and c.url.path.startswith("/v1beta/agents")]
    native_ids = {json.loads(c.content)["id"] for c in creates}
    assert len(native_ids) == 1, "one native agent, whatever the race"
    assert a.counterpart.local_revision == 1


async def test_a_template_never_serves_another_tenant(ordinary, tmp_path, monkeypatch):
    request, plan, *_ = ordinary
    service, source, actor, accepted = ordinary[8:]
    row = _as_template(plan, tmp_path, monkeypatch)
    foreign = row.model_copy(update={"tenant_id": "00000000-0000-0000-0000-00000000000b"})
    (tmp_path / "template.json").write_text(json.dumps([foreign.model_dump(mode="json")]))
    await service.clear_backend(source, accepted.local, actor)
    scoped, _ = scope_channel_runtimes(request)
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(scoped)


async def test_an_entry_holding_another_agents_spec_refuses(ordinary, tmp_path, monkeypatch):
    from daimon.core.turn.runtime_manifest import root_counterpart_catalog_id

    request, plan, canonical, native, calls, *_ = ordinary
    service, source, actor, accepted = ordinary[8:]
    _as_template(plan, tmp_path, monkeypatch)
    await service.clear_backend(source, accepted.local, actor)
    scoped, lookup = scope_channel_runtimes(request)
    prepared = await prepare_source(scoped)
    backend = lookup.resolved  # noqa: F841 - the scoped runtime the preparation uses
    foreign = canonical.target_spec.model_copy(update={"system": "Another agent's instructions"})
    gemini = GeminiManagedAgents(
        configured_transport(api_key="offline-declared-source"),
        storage=SQLiteGeminiStorage(
            tmp_path / "records.sqlite",
            namespace=json.dumps(
                [request.scope.tenant_id, request.scope.account_id, "project"],
                separators=(",", ":"),
            ),
        ),
        state_store=request.deps.state_store,
        account_scope_id="project",
        resource_catalog_enabled=True,
        resource_catalog_transport=configured_catalog_transport(api_key="offline-declared-source"),
    )
    host = NeutralResourceHost(
        gemini,
        request.deps.sessionmaker,
        scope=request.scope,
        account_scope_id="project",
        operation_key="foreign-publication",
        resource_catalog_enabled=True,
        catalog_adapter=gemini.catalog_adapter,
    )
    catalog_id = root_counterpart_catalog_id(prepared)
    await host.publish(catalog_id, foreign, expected_revision=0, key="foreign-publication")
    again, _ = scope_channel_runtimes(request)
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(again)


@pytest.mark.parametrize("consent", ["valid", "missing", "stale"])
async def test_missing_or_stale_consent_refuses_before_publication(
    ordinary, tmp_path, monkeypatch, consent
):
    from daimon.core.stores import agent_backends

    request, plan, canonical, native, calls, *_ = ordinary
    service, source, actor, accepted = ordinary[8:]
    _as_template(plan, tmp_path, monkeypatch)
    drop = None
    if consent == "valid":
        drop = accepted.drop_consent.model_dump(mode="json")
    if consent == "stale":
        drop = {**accepted.drop_consent.model_dump(mode="json"), "source_pin": "0" * 64}
    current = request.admission.backend_revision
    linked = ConfigRevision.create(current.channel, current.local + 1, current)
    async with request.deps.sessionmaker.begin() as db:
        bad = await agent_backends.put_revision(
            db,
            tenant_id=accepted.tenant_id,
            agent_id=accepted.agent_id,
            agent_name=accepted.agent_name,
            local=accepted.local + 1,
            backend="gemini",
            previous_local=accepted.local,
            digest="test-" + consent,
            preview_digest=accepted.preview_digest,
            drop_consent=drop,
        )
        # The turn's agent-origin revision binds the bad consent, as a real switch would.
        await mux_state.put_config_revision(db, linked, origin="agent", agent_revision=bad)
    request = replace(request, admission=replace(request.admission, backend_revision=linked))
    calls.clear()
    scoped, _ = scope_channel_runtimes(request)
    if consent == "valid":
        # The same construction with the confirmed consent publishes: the refusals are consent's.
        assert (await read_preparation_counterpart(scoped)).verified_counterpart is not None
        return
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(scoped)
    assert not [c for c in calls if c.method == "POST"], "nothing published without consent"


def _dropped_mcp(canonical):
    """The prepared target as a switch that also dropped the agent's MCP confirms it."""
    drops = tuple(sorted({"mcp", *canonical.target_spec.declared_drops}))
    target = canonical.target_spec.model_copy(update={"declared_drops": drops, "mcp_servers": ()})
    return replace(canonical, target_spec=target)


def test_an_mcp_template_serves_a_target_that_dropped_mcp_with_no_mcp(
    ordinary, tmp_path, monkeypatch
):
    """J2: a copy's confirmed switch to Gemini dropped its MCP. The code-default
    agent template keeps MCP for an agent that kept it; it must still serve this
    one, with no MCP at all, rather than refuse it as backend_unsupported."""
    canonical = _dropped_mcp(ordinary[2])
    assert canonical.target_spec.mcp_servers == ()
    row = _as_template(ordinary[1], tmp_path, monkeypatch).model_copy(
        update={"template_capabilities": "mcp"}
    )

    deployment = row.deployment(canonical)

    assert deployment.agent == canonical.target_spec
    assert deployment.agent.mcp_servers == ()


def test_an_mcp_template_never_gives_mcp_to_a_target_that_dropped_it(
    ordinary, tmp_path, monkeypatch
):
    """Never more than the template allows: a target that dropped MCP but still
    names a server is refused, as on a template without MCP."""
    canonical = _dropped_mcp(ordinary[2])
    server = MCPConnection(name="daimon-mcp", url="https://mcp.invalid/mcp")
    smuggled = replace(
        canonical, target_spec=canonical.target_spec.model_copy(update={"mcp_servers": (server,)})
    )
    for capabilities in ("mcp", None):
        row = _as_template(ordinary[1], tmp_path, monkeypatch).model_copy(
            update={"template_capabilities": capabilities}
        )
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            row.deployment(smuggled)


def test_an_mcp_template_serves_an_agent_that_never_had_mcp(ordinary, tmp_path, monkeypatch):
    """An agent without Daimon's MCP has nothing to represent and nothing to
    drop: the agent template still serves it, with no MCP."""
    canonical = ordinary[2]
    assert "mcp" not in canonical.target_spec.declared_drops
    assert canonical.target_spec.mcp_servers == ()
    row = _as_template(ordinary[1], tmp_path, monkeypatch).model_copy(
        update={"template_capabilities": "mcp"}
    )

    deployment = row.deployment(canonical)

    assert deployment.agent == canonical.target_spec
    assert deployment.agent.mcp_servers == ()


async def test_an_mcp_template_turn_runs_an_agent_with_no_mcp_through_read(
    ordinary, tmp_path, monkeypatch
):
    """J2 smoke-4 (gemini_deployment.read:787): the compile serves a target that
    carries no MCP server on the MCP agent template; the runtime's read must agree
    and run it with no MCP, not look for a representation that can't exist."""
    request, plan, canonical, native, calls, posts, *_ = ordinary
    service, source, actor, accepted = ordinary[8:]
    assert canonical.target_spec.mcp_servers == ()
    assert "mcp" not in canonical.target_spec.declared_drops
    template = _as_template(plan, tmp_path, monkeypatch).model_copy(
        update={"template_capabilities": "mcp"}
    )
    path = tmp_path / "template.json"
    path.write_text(json.dumps([template.model_dump(mode="json")]))
    await service.clear_backend(source, accepted.local, actor)

    scoped, _ = scope_channel_runtimes(request)
    selected = await read_preparation_counterpart(scoped)
    assert selected.verified_counterpart == selected.counterpart
    assert selected.target_spec.mcp_servers == ()
    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
    )

    async def no_recovery():
        pytest.fail("the template turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        external_user_id=request.external_user_id,
        user_message="Question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key="template-no-mcp:0",
    )
    assert outcome.state.error is None and extract_final_response(outcome.state.content) == "answer"
    assert all(t.get("type") != "mcp_server" for body in posts for t in body.get("tools", []))
