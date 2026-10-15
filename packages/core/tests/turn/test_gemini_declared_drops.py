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


async def test_ordinary_confirmed_target_native_readback_and_two_real_host_turns(ordinary):
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
            reuse_existing=True,
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
        assert body["tools"] == []
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
