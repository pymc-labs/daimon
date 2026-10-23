"""Installed runtime readers execute the owned counterpart proof at the host edge."""

import json
import os
from contextlib import AsyncExitStack
from dataclasses import replace

import httpx
import mux.drivers.gemini as gemini_transport
import mux.drivers.openai as openai_transport
import pytest
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings, TurnSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.turn import gemini_deployment, openai_deployment
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.runtime_manifest import read_preparation_counterpart
from mux.drivers.gemini.transport import SDKTransport as GeminiSDKTransport
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai, build_gemini_client
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from .native_counterpart_support import (
    application as application,
)
from .native_counterpart_support import (
    clean_sdk_environment as clean_sdk_environment,
)
from .native_counterpart_support import (
    db_preflight as db_preflight,
)
from .native_counterpart_support import (
    owned as owned,
)
from .native_counterpart_support import (
    owned_database_preflight as owned_database_preflight,
)
from .native_counterpart_support import (
    provider as provider,
)
from .test_openai_host import Wire as OpenAIWire
from .test_openai_worker_plan import target as target


@pytest.fixture
def db_session_factory(db_preflight, db_engine, db_clean):
    """Worker claims and native uncertainty commit on separate pooled connections."""
    from daimon.testing.db import require_owned_sessionmaker

    require_owned_sessionmaker(db_engine)
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def native_request(owned, monkeypatch):
    selected = owned.request
    agent = selected.admission.agent.model_copy(
        update={
            "metadata": {
                MA_METADATA_KEY_TENANT: str(selected.tenant_id),
                MA_METADATA_KEY_NAME: selected.admission.config.agent_name,
            }
        }
    )
    selected = replace(
        selected,
        admission=replace(selected.admission, agent=agent),
        deps=replace(selected.deps, agent_backends=True),
    )
    original = os.environ.get
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda name, default=None: (
            "offline" if name == owned.plan.api_key_env else original(name, default)
        ),
    )

    class TurnWire(OpenAIWire):
        def session(self):
            raw = super().session()
            raw["agent"]["id"] = owned.retained.agent.ref.id
            return raw

    turn_wire = TurnWire()
    turn_wire.expected_spend_limit = None
    posts = []

    def native(request):
        if request.method == "GET" and (
            request.url.path.startswith("/v1beta/agents/")
            or request.url.path == "/v1/agents/owned-agent"
            or "/environments/templates/" in request.url.path
        ):
            return owned.wire.handle(request)
        if owned.wire.provider == "openai":
            return turn_wire.handle(request)
        if request.url.path == "/v1beta/interactions" and request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": f"interaction-{len(posts)}",
                    "status": "completed",
                    "environment_id": "workspace",
                    "outputs": [],
                    "usage": {
                        "total_input_tokens": 12,
                        "total_output_tokens": 3,
                        "total_thought_tokens": 0,
                    },
                },
            )
        raise AssertionError("unexpected native execution request")

    async with AsyncExitStack() as stack:
        if owned.wire.provider == "openai":
            sdk = await stack.enter_async_context(
                build_async_openai(
                    transport=httpx.MockTransport(native),
                    api_key="offline",
                    base_url="https://openai.invalid/v1",
                )
            )
            transport = SDKTransport(sdk)
        else:
            sdk = build_gemini_client(transport=httpx.MockTransport(native), api_key="offline")
            stack.callback(sdk.close)
            stack.push_async_callback(sdk.aio.aclose)
            transport = GeminiSDKTransport(sdk)
        selected, runtime = install_runtime(selected, owned, monkeypatch, transport)
        yield selected, runtime, turn_wire, posts


def install_runtime(selected, owned, monkeypatch, transport):
    if owned.wire.provider == "openai":
        monkeypatch.setattr(
            openai_transport,
            "configured_transport",
            lambda **kwargs: transport,
        )
        runtime = openai_deployment.build_openai_runtime(selected)
    else:
        source = owned.resources.backend.agents
        monkeypatch.setattr(
            gemini_deployment, "SQLiteGeminiStorage", lambda *args, **kwargs: source._storage
        )
        monkeypatch.setattr(gemini_transport, "configured_transport", lambda **kwargs: transport)
        monkeypatch.setattr(
            gemini_transport,
            "configured_catalog_transport",
            lambda **kwargs: source._catalog_transport,
        )
        runtime = gemini_deployment.build_gemini_runtime(selected)
    selected = replace(
        selected,
        deps=replace(
            selected.deps, turn_runtimes={selected.admission.backend_revision.profile: runtime}
        ),
    )
    return selected, runtime


async def test_installed_runtime_reader_uses_real_scoped_catalog_and_sdk_readback(
    native_request, owned
):
    request, runtime, _, _ = native_request
    assert runtime.read_agent_counterpart is not None
    row = await runtime.read_agent_counterpart(request)
    assert row == owned.retained
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    if owned.wire.provider == "openai":
        planned = await runtime.session_plan(replace(request, counterpart=row))
        assert planned.agent == row.agent.ref and planned.agent_revision.local == 0


@pytest.mark.parametrize(
    "failure",
    [
        "source_version",
        "native_id",
        "catalog_revision",
        "revoked",
        "flag_off",
        "native_drift",
        "native_missing",
    ],
)
async def test_installed_runtime_proof_failures_have_zero_native_mutations(
    native_request, owned, failure
):
    request, _, _, _ = native_request
    if failure == "source_version":
        request = replace(
            request,
            admission=replace(
                request.admission, agent=request.admission.agent.model_copy(update={"version": 2})
            ),
        )
    elif failure == "flag_off":
        request = replace(request, deps=replace(request.deps, agent_backends=False))
    elif failure == "native_drift":
        if owned.wire.provider == "gemini":
            owned.wire.agent["tools"] = [{"type": "google_search"}]
        else:
            owned.wire.agent["model"] = "foreign-model"
    elif failure == "native_missing":
        owned.wire.error = ("/v1/agents/owned-agent", 404)
    else:
        import json

        values = owned.plan.model_dump(mode="json")
        if failure == "native_id":
            values["agent_id"] = "foreign"
        elif failure == "catalog_revision":
            values["counterpart_catalog_revision"] = 2
        else:
            values = None
        owned.manifest_path.write_text(json.dumps([] if values is None else [values]))
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(request)
    assert not owned.wire.requests, "unproved source refuses before target drift reads"


async def test_real_host_refuses_complete_native_component_as_source_execution_proof(
    native_request, owned
):
    request, runtime, wire, posts = native_request
    assert runtime.read_agent_counterpart is not None
    row = await runtime.read_agent_counterpart(request)
    assert row == owned.retained
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    owned.wire.requests.clear()
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await bind_session_impl(
            request.deps,
            replace(request.admission, backend_counterpart=row),
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=True,
            deadline=request.deadline,
            now=request.now,
        )
    assert owned.wire.requests == []
    assert wire.requests == [] and posts == []
    assert runtime is not None


@pytest.mark.parametrize("enabled", [False, True])
async def test_explicit_execution_setting_reaches_real_dependencies_without_allocating_runtime(
    application, enabled
):
    settings = Settings(
        anthropic=AnthropicSettings(api_key=SecretStr("offline")),
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://offline/unused")),
        turn=TurnSettings(agent_backends=enabled),
    )
    deps = build_turn_deps(
        settings,
        application.deps.anthropic,
        application.deps.sessionmaker,
        deployment_default=application.deps.deployment_default,
        resolver_cache=application.deps.resolver_cache,
        billing_config=None,
    )
    assert deps.agent_backends is enabled
    assert deps.turn_path == "legacy" and deps.channel_backends is False
    assert deps.turn_runtimes == {} and deps.state_store is None


@pytest.mark.parametrize("obligation", ["skill", "mcp", "instructions", "tool_approval"])
async def test_real_host_refuses_source_obligations_missing_from_installed_counterpart(
    native_request, owned, obligation
):
    request, _, _, _ = native_request
    values = request.admission.agent.model_dump()
    if obligation == "skill":
        values["skills"] = [{"type": "custom", "skill_id": "required-source-skill", "version": "1"}]
    elif obligation == "mcp":
        values["mcp_servers"] = [
            {"type": "url", "name": "required-source-mcp", "url": "https://mcp.invalid/mcp"}
        ]
    elif obligation == "tool_approval":
        values["tools"] = [
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            }
        ]
    else:
        values["system"] = "Required source instructions absent from counterpart"
    agent = type(request.admission.agent).model_validate(values)
    request = replace(request, admission=replace(request.admission, agent=agent))
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=True,
            deadline=request.deadline,
            now=request.now,
        )
    assert not any(r.method != "GET" for r in owned.wire.requests)


@pytest.mark.parametrize(
    "obligation",
    [
        "instructions",
        "tools",
        "skills",
        "mcp",
        "credentials",
        "files",
        "memory",
        "environment",
    ],
)
async def test_complete_source_obligation_capture_refuses_before_target_io(
    native_request, owned, obligation, monkeypatch
):
    from daimon.core import sessions
    from daimon.core._models import AgentFile
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_mcp_credentials, agent_memory_stores
    from mux.errors import UnsupportedCapability

    request, _, wire, posts = native_request
    baseline_values = request.admission.agent.model_dump()
    baseline_values.update(system=None, tools=[], skills=[], mcp_servers=[])
    baseline_agent = type(request.admission.agent).model_validate(baseline_values)
    request = replace(
        request,
        admission=replace(request.admission, agent=baseline_agent),
        deps=replace(request.deps, agent_github_app=None),
    )
    with pytest.raises(AdmissionDenied) as baseline_refusal:
        await read_preparation_counterpart(request)
    baseline = baseline_refusal.value.__cause__
    assert isinstance(baseline, UnsupportedCapability)
    assert set(baseline.missing) == {"memory", "environment"}
    assert obligation not in baseline.missing or obligation in {"memory", "environment"}
    assert owned.wire.requests == [] and wire.requests == [] and posts == []
    source_id = derive_agent_uuid(
        tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
    )
    values = request.admission.agent.model_dump()
    if obligation == "instructions":
        values["system"] = "Source instructions must survive"
    elif obligation == "tools":
        values["tools"] = [
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            }
        ]
    elif obligation == "skills":
        values["skills"] = [{"type": "custom", "skill_id": "required-source-skill", "version": "1"}]
    elif obligation == "mcp":
        values["mcp_servers"] = [
            {"type": "url", "name": "required-source-mcp", "url": "https://mcp.invalid/mcp"}
        ]
    agent = type(request.admission.agent).model_validate(values)
    request = replace(request, admission=replace(request.admission, agent=agent))
    async with request.deps.sessionmaker.begin() as db:
        if obligation == "credentials":
            await agent_mcp_credentials.upsert_credential(
                db,
                tenant_id=request.tenant_id,
                agent_id=source_id,
                mcp_server_url="https://mcp.invalid/mcp",
                encrypted_token=b"unread-credential-marker",
            )
        elif obligation == "files":
            db.add(
                AgentFile(
                    tenant_id=request.tenant_id,
                    agent_id=source_id,
                    key="PRIVATE_SAMPLE",
                    content="intentionally-invalid-ciphertext",
                    encoding="fernet_v1",
                )
            )
        assert (
            await agent_memory_stores.get_memory_store_id(
                db,
                tenant_id=request.tenant_id,
                agent_id=source_id,
            )
            is None
        )
    if obligation == "credentials":
        from cryptography.fernet import Fernet, MultiFernet
        from daimon.core.config import McpSettings

        request = replace(
            request,
            deps=replace(
                request.deps,
                fernet=MultiFernet([Fernet(Fernet.generate_key())]),
                mcp=McpSettings(public_url="https://mcp.invalid", jwt_secret=SecretStr("offline")),
            ),
        )
    if obligation == "environment":
        raw = request.admission.environment.model_dump()
        raw["config"]["packages"]["pip"] = ["required-source-package==1"]
        environment = type(request.admission.environment).model_validate(raw)
        request = replace(request, admission=replace(request.admission, environment=environment))
    captured = []
    original = sessions.project_session_resources

    async def capture(*args, **kwargs):
        result = await original(*args, **kwargs)
        captured.append(result)
        return result

    def no_decryption(*args, **kwargs):
        raise AssertionError("source projection decoded a secret")

    monkeypatch.setattr(sessions, "project_session_resources", capture)
    monkeypatch.setattr("daimon.core.stores.agent_files._cipher", no_decryption)
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    detail = refused.value.__cause__
    assert isinstance(detail, UnsupportedCapability)
    expected = {"memory", "environment", obligation}
    if obligation == "credentials":
        expected.add("mcp")
    if obligation == "files":
        expected.add("credentials")
    assert set(detail.missing) == expected
    assert len(captured) == 1
    source = captured[0]
    assert source.source is not None and source.source.ref.id == agent.id
    assert source.source.ref.account_scope_id == "", "unknown SDK workspace cannot be fabricated"
    assert source.resource_scope == request.scope
    assert source.effective_spec is not None
    assert source.memory_required is True and source.memory_store_id is None
    assert (
        source.memory_instructions and source.memory_read_only is request.admission.memory_read_only
    )
    if obligation == "instructions":
        assert source.effective_spec.system == values["system"]
    elif obligation == "tools":
        extension = next(
            e for e in source.effective_spec.extensions if e.namespace == "anthropic.agent_tools"
        )
        assert (
            extension.value["tools"][0]["configs"][0]["permission_policy"]["type"] == "always_ask"
        )
    elif obligation == "skills":
        assert source.effective_spec.skills[0].id == "required-source-skill"
    elif obligation == "mcp":
        assert source.effective_spec.mcp_servers[0].name == "required-source-mcp"
    elif obligation in ("files", "credentials"):
        assert source.resource_requirements and source.resource_generations
    elif obligation == "environment":
        assert source.environment is not None
        assert source.environment.native["config"]["packages"]["pip"] == [
            "required-source-package==1"
        ]
    assert source.model_dump() == {
        key: value
        for key, value in source.model_dump().items()
        if key
        not in {
            "source",
            "effective_spec",
            "environment",
            "resource_scope",
            "resource_requirements",
            "repository_sources",
            "resource_generations",
            "memory_required",
            "memory_store_id",
            "memory_read_only",
            "memory_instructions",
        }
    }
    assert "Source instructions must survive" not in repr(source)
    assert "intentionally-invalid-ciphertext" not in repr(source)
    assert owned.wire.requests == [] and wire.requests == [] and posts == []


async def test_opaque_caller_resource_is_an_unproved_source_definition(native_request, owned):
    from mux.errors import UnsupportedCapability

    request, _, wire, posts = native_request
    request = replace(
        request, admission=replace(request.admission, private_dm_id="caller-private-context")
    )
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    detail = refused.value.__cause__
    assert isinstance(detail, UnsupportedCapability)
    assert {"source_definition", "memory", "environment"} <= set(detail.missing)
    assert owned.wire.requests == [] and wire.requests == [] and posts == []


ROOT_PROMPT_FILLER = (
    "You are the team's assistant. Answer plainly, cite what you used, and say when "
    "you are unsure. Keep private details private and follow the channel's rules. "
) * 200


@pytest.fixture
async def explicit_worker_request(owned, monkeypatch, request):
    worker_token_caps = getattr(request, "param", False)
    from datetime import timedelta

    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.neutral_resources import resolve_worker_spec
    from mux.contracts.ids import ThreadRef

    from ..test_fleet_permissions import POLICY, parent

    request = replace(
        owned.request,
        admission=replace(
            owned.request.admission,
            agent=owned.request.admission.agent.model_copy(
                update={
                    "metadata": {
                        MA_METADATA_KEY_TENANT: str(owned.request.tenant_id),
                        MA_METADATA_KEY_NAME: owned.request.admission.config.agent_name,
                    }
                }
            ),
        ),
    )
    source_values = request.admission.agent.model_dump()
    source_values.update(
        system="Retained instructions",
        skills=[{"type": "custom", "skill_id": "source-required-skill", "version": "1"}],
        mcp_servers=[
            {"type": "url", "name": "source-required-mcp", "url": "https://mcp.invalid/mcp"}
        ],
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            }
        ],
    )
    request = replace(
        request,
        admission=replace(
            request.admission, agent=type(request.admission.agent).model_validate(source_values)
        ),
    )
    root = parent(owned.wire.provider)
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=request.scope,
            root=ThreadRef(
                channel=request.admission.backend_revision.channel, thread_id=request.thread_id
            ),
            account_id=request.admission.account_id,
        ),
        agent_id=derive_agent_uuid(
            tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
        ),
        spec=owned.retained.agent.spec,
        base_system=owned.retained.agent.spec.system,
        limits=root.limits.model_copy(update={"deadline": request.now() + timedelta(seconds=20)}),
    )
    if worker_token_caps == "derived":
        # A real-size inherited root prompt, as staging's daimon-fleet root has (~24k chars).
        large = (owned.retained.agent.spec.system + "\n\n" + ROOT_PROMPT_FILLER)[:24_000]
        root = replace(root, spec=root.spec.model_copy(update={"system": large}), base_system=large)
    worker_spec = root.spec.model_copy(
        update={
            "declared_drops": (
                "memory",
                "sandbox",
                "skills",
                "mcp",
                "tools",
                "files",
                "repository",
            )
        }
    )
    limits = (
        root.limits.model_copy(
            # Explicit caps whose worst case at Flash's tariff fits the $0.05 hold.
            update={"max_input_tokens": 16384, "max_output_tokens": 8192, "max_turns": 1}
        )
        if worker_token_caps is True
        else None
    )
    worker = resolve_worker_spec(root, worker_spec, policy=POLICY, now=request.now(), limits=limits)
    if worker_token_caps == "derived":
        from daimon.core.neutral_resources import bounded_gemini_worker_tokens

        # The plain-language spawn path: limits derived from the resolved prompt.
        derived = bounded_gemini_worker_tokens(
            worker.spec.system,
            worker.limits.budget_usd,
            model=worker.spec.model.id,
            hold_usd=owned.plan.hold_usd,
        )
        worker = resolve_worker_spec(
            root,
            worker_spec,
            policy=POLICY,
            now=request.now(),
            limits=worker.limits.model_copy(update={**derived, "max_turns": 1}),
        )
    scope = worker.permissions.fleet.scope
    agent = await owned.resources.backend.agents.create(scope, worker.spec, key="restricted-worker")
    row = await owned.resources.catalog.put_agent(
        scope,
        owned.retained.catalog_id,
        agent,
        expected_revision=owned.retained.local_revision,
    )
    plan = owned.plan.model_copy(
        update={
            "agent_name": worker.spec.name,
            "system": worker.spec.system,
            "tools": (),
            "agent_id": agent.ref.id,
            "skill_ids": (),
            "counterpart_catalog_revision": row.local_revision,
        }
    )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    request = replace(
        request,
        scope=scope,
        worker=worker,
        deps=replace(request.deps, agent_backends=True, agent_github_app=None),
    )
    from .test_gemini_codec import TrackedTransport

    transport = TrackedTransport()
    original = os.environ.get
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda name, default=None: (
            "offline" if name == plan.api_key_env else original(name, default)
        ),
    )
    request, runtime = install_runtime(request, owned, monkeypatch, transport)
    owned.wire.requests.clear()
    yield request, runtime, owned, transport


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_complete_declared_worker_proof_consumes_actual_native_counterpart(
    explicit_worker_request,
):
    request, runtime, owned, transport = explicit_worker_request
    selected = await read_preparation_counterpart(request)
    assert selected.deadline == min(request.deadline, request.worker.limits.deadline)
    assert selected.worker == request.worker
    assert selected.source.resource_scope == request.scope
    assert selected.source.memory_required is True
    assert selected.counterpart.agent.spec == request.worker.spec
    assert selected.counterpart.agent.ref.id == owned.wire.native_id
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
        deadline=request.deadline,
        now=request.now,
        worker=request.worker,
    )
    assert prepared.backend is not None
    assert prepared.runtime.journal.deployment.native_agent == selected.counterpart.agent
    assert prepared.admission.backend_revision_origin == "agent"
    assert prepared.runtime.journal.deployment.agent == request.worker.spec
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart

    from .test_gemini_codec import interaction

    transport.responses.append(interaction("verified-worker-turn", status="completed"))
    await prepared.backend.events.send(
        request.scope,
        prepared.session_ref,
        (UserMessage(content=(TextPart(text="run the restricted task"),)),),
        key="worker-input",
    )
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent["agent"] == selected.counterpart.agent.ref.id
    assert sent["tools"] == []
    assert sent["environment"] == {"type": "remote", "sources": []}


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize(
    "failure",
    [
        "memory",
        "sandbox",
        "instructions",
        "tools",
        "skills",
        "mcp",
        "ask_without_tools",
        "files",
        "private",
        "scope",
        "expired",
        "foreign_root",
        "missing_reader",
        "flag_off",
        "native_drift",
        "source_drift",
        "model_controls",
    ],
)
async def test_explicit_drops_do_not_discharge_unproved_obligations(
    explicit_worker_request, failure, monkeypatch
):
    from daimon.core import sessions
    from daimon.core._models import AgentFile
    from mux.contracts.resources import ResourceBinding

    request, runtime, owned, transport = explicit_worker_request
    worker = request.worker
    if failure in ("tools", "skills", "mcp"):
        removed = {failure, "tools"} if failure == "mcp" else {failure}
        spec = worker.spec.model_copy(
            update={"declared_drops": tuple(x for x in worker.declared_drops if x not in removed)}
        )
        worker = replace(worker, spec=spec, permissions=replace(worker.permissions, spec=spec))
    elif failure in ("memory", "sandbox"):
        spec = worker.spec.model_copy(
            update={"declared_drops": tuple(x for x in worker.declared_drops if x != failure)}
        )
        worker = replace(worker, spec=spec, permissions=replace(worker.permissions, spec=spec))
    elif failure == "instructions":
        agent = request.admission.agent.model_copy(
            update={"system": "Protected source instructions"}
        )
        request = replace(request, admission=replace(request.admission, agent=agent))
    elif failure == "ask_without_tools":
        request = replace(
            request,
            admission=replace(
                request.admission,
                asks_before_publishing=True,
                agent=request.admission.agent.model_copy(update={"tools": []}),
            ),
        )
    elif failure == "model_controls":
        agent = request.admission.agent.model_copy(
            update={"model": request.admission.agent.model.model_copy(update={"speed": "fast"})}
        )
        request = replace(request, admission=replace(request.admission, agent=agent))
    elif failure == "source_drift":
        read = runtime.read_agent_counterpart

        async def drift(selected):
            checked = await read(selected)
            async with request.deps.sessionmaker.begin() as db:
                db.add(
                    AgentFile(
                        tenant_id=request.tenant_id,
                        agent_id=worker.permissions.agent_id,
                        key="LATE_PRIVATE",
                        content="invalid-ciphertext",
                        encoding="fernet_v1",
                    )
                )
            return checked

        request = replace(
            request,
            deps=replace(
                request.deps,
                turn_runtimes={
                    request.admission.backend_revision.profile: replace(
                        runtime, read_agent_counterpart=drift
                    )
                },
            ),
        )
    elif failure in ("credentials", "files"):
        # A stored secret is left out of the worker ceiling (#1357): without "files"
        # declared it refuses; declared, see
        # test_a_stored_secret_never_reaches_a_worker_that_declares_files.
        if failure == "files":
            spec = worker.spec.model_copy(
                update={"declared_drops": tuple(x for x in worker.declared_drops if x != "files")}
            )
            worker = replace(worker, spec=spec, permissions=replace(worker.permissions, spec=spec))
        async with request.deps.sessionmaker.begin() as db:
            db.add(
                AgentFile(
                    tenant_id=request.tenant_id,
                    agent_id=worker.permissions.agent_id,
                    key="UNREAD_PRIVATE",
                    content="invalid-ciphertext",
                    encoding="fernet_v1",
                )
            )
    elif failure == "private":
        capture = sessions.project_session_resources

        async def private(*args, **kwargs):
            source = await capture(*args, **kwargs)
            return source.model_copy(
                update={
                    "resource_requirements": (ResourceBinding(id="opaque-private", kind="native"),)
                }
            )

        monkeypatch.setattr(sessions, "project_session_resources", private)
    elif failure == "scope":
        request = replace(request, scope=request.scope.model_copy(update={"account_id": "foreign"}))
    elif failure == "expired":
        limits = worker.limits.model_copy(update={"deadline": request.now()})
        worker = replace(
            worker, limits=limits, permissions=replace(worker.permissions, limits=limits)
        )
    elif failure == "foreign_root":
        worker = replace(worker, permissions=replace(worker.permissions, agent_id=None))
    elif failure == "missing_reader":
        request = replace(
            request,
            deps=replace(
                request.deps,
                turn_runtimes={
                    request.admission.backend_revision.profile: replace(
                        runtime, read_agent_counterpart=None
                    )
                },
            ),
        )
    elif failure == "flag_off":
        request = replace(request, deps=replace(request.deps, agent_backends=False))
    elif failure == "native_drift":
        owned.wire.agent["system_instruction"] = "Dropped protected instructions"
    request = replace(request, worker=worker)
    with pytest.raises(AdmissionDenied, match="backend_unsupported") as refused:
        await read_preparation_counterpart(request)
    from mux.errors import UnsupportedCapability

    if failure == "expired":
        from daimon.core.errors import TurnError

        assert isinstance(refused.value.__cause__, TurnError)
        assert refused.value.__cause__.kind == "ceiling"
    elif failure not in {"native_drift", "scope", "flag_off"}:
        assert isinstance(refused.value.__cause__, UnsupportedCapability)
        expected = {
            "sandbox": "environment",
            "private": "source_definition",
            "expired": "worker_context",
            "foreign_root": "worker_context",
            "missing_reader": "native_counterpart",
            "source_drift": "source_drift",
            "ask_without_tools": "tools",
        }.get(failure, failure)
        assert expected in refused.value.__cause__.missing
    assert not any(r.method != "GET" for r in owned.wire.requests)
    assert transport.requests == []
    if failure not in {"native_drift", "source_drift"}:
        assert owned.wire.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_worker_source_refusal_precedes_revision_publication(explicit_worker_request):
    from daimon.core.authz import Subject
    from daimon.core.channel_backend import AgentBackendService, BackendActor, resolve_turn_backend
    from daimon.core.stores import mux_state
    from sqlalchemy import text

    request, _, _, transport = explicit_worker_request
    selected = await read_preparation_counterpart(request)
    source = selected.source
    service = AgentBackendService(request.deps.sessionmaker, enabled=True)
    actor = BackendActor(request.tenant_id, request.session_account_id, Subject(is_admin=True))
    policy = await service.tenant_policy(request.tenant_id)
    await service.set_tenant_allowlist(
        request.tenant_id, ("anthropic", "gemini"), policy.revision, actor
    )
    await service.set_backend(source, "gemini", 0, actor)
    drops = tuple(x for x in request.worker.declared_drops if x != "skills")
    spec = request.worker.spec.model_copy(update={"declared_drops": drops})
    worker = replace(
        request.worker, spec=spec, permissions=replace(request.worker.permissions, spec=spec)
    )

    async def verify(selection, origin, revision):
        if revision is not None:
            candidate = replace(
                request,
                worker=worker,
                admission=replace(
                    request.admission, backend_revision=revision, backend_revision_origin=origin
                ),
            )
            await read_preparation_counterpart(candidate)

    channel = request.admission.backend_revision.channel
    async with request.deps.sessionmaker.begin() as db:
        with pytest.raises(AdmissionDenied):
            await resolve_turn_backend(db, channel, source, enabled=True, validate_origin=verify)
        assert await mux_state.latest_config_revision(db, channel) is None
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM channel_config_revision WHERE tenant_id=:tenant AND channel_id=:channel"
                ),
                {"tenant": request.tenant_id, "channel": channel.channel_id},
            )
            == 0
        )
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_worker_proof_cancellation_does_not_reach_preparation(explicit_worker_request):
    import asyncio

    request, runtime, owned, transport = explicit_worker_request

    async def cancelled(selected):
        raise asyncio.CancelledError

    request = replace(
        request,
        deps=replace(
            request.deps,
            turn_runtimes={
                request.admission.backend_revision.profile: replace(
                    runtime, read_agent_counterpart=cancelled
                )
            },
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        await read_preparation_counterpart(request)
    assert not owned.wire.requests and not transport.requests


@pytest.mark.parametrize("provider", ["openai"], indirect=True)
async def test_openai_hosted_component_cannot_erase_the_legacy_source_environment(
    target,
):
    from datetime import timedelta

    from daimon.core.ma_identity import derive_agent_uuid
    from mux.errors import UnsupportedCapability

    request, worker, _, _, owned = target
    agent = request.admission.agent.model_copy(
        update={
            "metadata": {
                MA_METADATA_KEY_TENANT: str(request.tenant_id),
                MA_METADATA_KEY_NAME: request.admission.config.agent_name,
            }
        }
    )
    limits = worker.limits.model_copy(update={"deadline": request.now() + timedelta(seconds=20)})
    worker = replace(
        worker,
        limits=limits,
        permissions=replace(
            worker.permissions,
            limits=limits,
            agent_id=derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=agent.id),
        ),
    )
    request = replace(
        request,
        worker=worker,
        admission=replace(request.admission, agent=agent),
        deps=replace(request.deps, agent_github_app=None),
    )
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert isinstance(refused.value.__cause__, UnsupportedCapability)
    assert "environment" in refused.value.__cause__.missing
    assert not owned.wire.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("route", ["channel", "legacy"])
async def test_worker_cannot_bypass_source_proof_through_channel_origin(
    explicit_worker_request, monkeypatch, route
):
    from daimon.core.turn import prepare as preparation

    request, _, owned, transport = explicit_worker_request

    async def forbidden(selected):
        raise AssertionError("a worker reached the preparer without its source proof")

    monkeypatch.setitem(
        preparation._PROVIDER_PREPARATIONS, request.admission.backend_revision.profile, forbidden
    )
    admission = replace(request.admission, backend_revision_origin="channel")
    if route == "legacy":
        from mux.contracts.config import ConfigRevision
        from mux.profiles import backend_selection

        admission = replace(
            request.admission,
            backend_revision=ConfigRevision.create(
                request.admission.backend_revision.channel, 1, backend_selection("anthropic")
            ),
        )
    with pytest.raises(AdmissionDenied):
        await bind_session_impl(
            request.deps,
            admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=True,
            deadline=request.deadline,
            now=request.now,
            worker=request.worker,
        )
    assert not owned.wire.requests and not transport.requests


async def _bind_worker(request, **kwargs):
    return await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
        deadline=request.deadline,
        now=request.now,
        worker=request.worker,
        **kwargs,
    )


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("entry", ["proof", "registered"])
async def test_worker_readback_cannot_outlive_its_shorter_authority(
    explicit_worker_request, monkeypatch, entry
):
    from datetime import timedelta

    from daimon.core.errors import TurnError
    from daimon.core.turn import prepare as preparation

    request, runtime, owned, transport = explicit_worker_request
    clock = [request.now()]
    bound = request.worker.limits.deadline
    assert bound < request.deadline
    prepared_callbacks = []
    preparer_calls = []
    readback_bounds = []

    async def slow_readback(selected):
        readback_bounds.append(selected.deadline)
        result = await runtime.read_agent_counterpart(selected)
        clock[0] = bound + timedelta(seconds=1)
        assert clock[0] < request.deadline
        return result

    async def forbidden(selected):
        preparer_calls.append(selected)
        raise AssertionError("expired proof reached native preparation")

    async def on_prepared(prepared):
        prepared_callbacks.append(prepared)

    profile = request.admission.backend_revision.profile
    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, forbidden)
    request = replace(
        request,
        now=lambda: clock[0],
        deps=replace(
            request.deps,
            turn_runtimes={profile: replace(runtime, read_agent_counterpart=slow_readback)},
        ),
    )
    with pytest.raises(AdmissionDenied) as refused:
        if entry == "proof":
            await read_preparation_counterpart(request)
        else:
            await _bind_worker(request, _on_prepared=on_prepared)
    assert isinstance(refused.value.__cause__, TurnError)
    assert refused.value.__cause__.kind == "ceiling"
    assert readback_bounds == [bound]
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert preparer_calls == [] and prepared_callbacks == [] and transport.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_stricter_caller_bound_reaches_the_registered_preparer(
    explicit_worker_request, monkeypatch
):
    from datetime import timedelta

    from daimon.core.turn import prepare as preparation

    request, runtime, owned, transport = explicit_worker_request
    original_worker = request.worker
    bound = request.now() + timedelta(seconds=10)
    assert bound < original_worker.limits.deadline
    profile = request.admission.backend_revision.profile
    assert preparation.ensure_channel_runtime(
        profile, ready=lambda: profile in preparation._PROVIDER_PREPARATIONS
    )
    prepare = preparation._PROVIDER_PREPARATIONS[profile]
    seen = []

    async def bounded_preparation(selected):
        assert selected.deadline == bound
        assert selected.worker == original_worker
        seen.append(selected)
        return await prepare(selected)

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, bounded_preparation)
    selected = await read_preparation_counterpart(replace(request, deadline=bound))
    assert selected.deadline == bound and selected.worker == original_worker
    prepared = await _bind_worker(replace(request, deadline=bound))
    assert prepared.backend is not None and len(seen) == 1
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_hanging_final_source_capture_is_bounded_and_cancelled(
    explicit_worker_request, monkeypatch
):
    import asyncio
    from datetime import timedelta

    from daimon.core import sessions

    request, runtime, owned, transport = explicit_worker_request
    clock = [request.now()]
    bound = request.worker.limits.deadline

    async def readback(selected):
        result = await runtime.read_agent_counterpart(selected)
        clock[0] = bound - timedelta(seconds=0.05)
        return result

    request = replace(
        request,
        now=lambda: clock[0],
        deps=replace(
            request.deps,
            turn_runtimes={
                request.admission.backend_revision.profile: replace(
                    runtime, read_agent_counterpart=readback
                )
            },
        ),
    )
    capture = sessions.project_session_resources
    calls = []
    cancelled = []

    async def hanging_capture(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            return await capture(*args, **kwargs)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(sessions, "project_session_resources", hanging_capture)
    with pytest.raises(AdmissionDenied) as refused:
        await asyncio.wait_for(read_preparation_counterpart(request), timeout=1)
    assert isinstance(refused.value.__cause__, TimeoutError)
    assert len(calls) == 2 and cancelled == [True]
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("stage", ["capture", "preparation", "callback"])
async def test_expiry_is_rechecked_before_successful_worker_handoff(
    explicit_worker_request, monkeypatch, stage, capsys
):
    from datetime import timedelta

    from daimon.core import sessions
    from daimon.core.errors import TurnError
    from daimon.core.turn import prepare as preparation

    request, _, owned, transport = explicit_worker_request
    clock = [request.now()]
    bound = request.worker.limits.deadline
    request = replace(request, now=lambda: clock[0])
    profile = request.admission.backend_revision.profile
    assert preparation.ensure_channel_runtime(
        profile, ready=lambda: profile in preparation._PROVIDER_PREPARATIONS
    )
    prepare = preparation._PROVIDER_PREPARATIONS[profile]
    capture = sessions.project_session_resources
    capture_calls = []
    preparation_calls = []
    callbacks = []

    async def timed_capture(*args, **kwargs):
        result = await capture(*args, **kwargs)
        capture_calls.append(None)
        if stage == "capture" and len(capture_calls) == 2:
            clock[0] = bound + timedelta(seconds=1)
        return result

    async def timed_prepare(selected):
        assert selected.deadline == bound
        preparation_calls.append(selected)
        result = await prepare(selected)
        if stage == "preparation":
            clock[0] = bound + timedelta(seconds=1)
        return result

    async def on_prepared(prepared):
        callbacks.append(prepared)
        if stage == "callback":
            clock[0] = bound + timedelta(seconds=1)

    monkeypatch.setattr(sessions, "project_session_resources", timed_capture)
    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, timed_prepare)
    with pytest.raises((AdmissionDenied, TurnError)) as refused:
        await _bind_worker(request, _on_prepared=on_prepared)
    error = refused.value
    if isinstance(error, AdmissionDenied):
        error = error.__cause__
    assert isinstance(error, TurnError) and error.kind == "ceiling"
    assert len(preparation_calls) == (stage != "capture")
    assert len(callbacks) == (stage == "callback")
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_proven_worker_scope_survives_the_real_host_binding_and_codec(
    explicit_worker_request,
):
    from daimon.core.turn.binding import prepared_persistence
    from daimon.core.turn.run import _turn_port_kwargs

    request, _, _, _ = explicit_worker_request
    prepared = await _bind_worker(request)
    assert prepared._scope == request.worker.permissions.fleet.scope
    port = _turn_port_kwargs(
        request.deps,
        prepared.admission,
        prepared.ma_session_id,
        tenant_id=request.tenant_id,
        provider_backend=prepared.backend,
        provider_session=prepared.session_ref,
        provider_runtime=prepared.runtime,
        provider_scope=prepared._scope,
    )
    assert port["scope"] == request.worker.permissions.fleet.scope
    assert port["backend_request"].scope == port["scope"]
    persistence = await prepared_persistence(
        request.deps,
        prepared.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        session_id=prepared.ma_session_id,
        mapping_id=prepared.mapping_id,
        session_ref=prepared.session_ref,
        scope=prepared._scope,
    )
    assert persistence.scope == port["scope"]
    assert persistence.binding.native_refs["session"] == prepared.ma_session_id


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("stage", ["preparation", "callback"])
async def test_worker_expiry_attempts_cleanup_of_the_actual_prepared_session_once(
    explicit_worker_request, monkeypatch, stage, capsys
):
    from datetime import timedelta

    from daimon.core.errors import TurnError
    from daimon.core.mux_backend import TurnBackendRequest
    from daimon.core.turn import prepare as preparation
    from daimon.core.turn.io import turn_io
    from daimon.core.turn.run import _archive_orphaned_session

    request, _, _, transport = explicit_worker_request
    clock = [request.now()]
    request = replace(request, now=lambda: clock[0])
    bound = request.worker.limits.deadline
    profile = request.admission.backend_revision.profile
    assert preparation.ensure_channel_runtime(
        profile, ready=lambda: profile in preparation._PROVIDER_PREPARATIONS
    )
    prepare = preparation._PROVIDER_PREPARATIONS[profile]
    archived = []
    prepared_callbacks = []

    async def timed_prepare(selected):
        result = await prepare(selected)
        if stage == "preparation":
            clock[0] = bound + timedelta(seconds=1)
        return result

    async def on_prepared(result):
        prepared_callbacks.append(result)
        clock[0] = bound + timedelta(seconds=1)

    async def orphan(result):
        assert result._scope == request.worker.permissions.fleet.scope
        io = turn_io(
            request.deps.anthropic,
            result.ma_session_id,
            path="mux",
            backend=result.backend,
            scope=result._scope,
            session_ref=result.session_ref,
            profile=profile,
            backend_request=TurnBackendRequest(
                profile,
                request.deps.anthropic,
                result._scope,
                result.ma_session_id,
                deps=request.deps,
                config=request.admission.backend_revision,
                session=result.session_ref,
                runtime=result.runtime,
            ),
        )
        await _archive_orphaned_session(
            request.deps.anthropic, session_id=result.ma_session_id, io=io
        )
        archived.append(result)

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, timed_prepare)
    with pytest.raises(TurnError) as refused:
        await _bind_worker(request, _on_prepared=on_prepared, _on_orphan=orphan)
    assert refused.value.kind == "ceiling"
    assert len(archived) == 1 and len(prepared_callbacks) == (stage == "callback")
    session = await archived[0].backend.sessions.retrieve(
        archived[0]._scope, archived[0].session_ref
    )
    assert session.state == "idle"
    assert "session_archive" in capsys.readouterr().out
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_normalized_source_prefix_is_consumed_by_actual_preparation(
    explicit_worker_request, monkeypatch
):
    from daimon.core.neutral_resources import resolve_worker_spec
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    from ..test_fleet_permissions import POLICY

    request, _, owned, transport = explicit_worker_request
    captured = await capture_preparation_source(request)
    root = replace(
        request.worker.permissions,
        fleet=replace(request.worker.permissions.fleet, depth=0),
        base_system="Protected root instruction",
    )
    worker = resolve_worker_spec(
        root,
        root.spec.model_copy(update={"system": root.base_system}),
        policy=POLICY,
        now=request.now(),
        captured_source=captured,
    )
    assert worker.spec.system.startswith("Retained instructions\n\nProtected root instruction\n\n")
    assert worker.spec.system.count(root.base_system) == 1
    scope = worker.permissions.fleet.scope
    actual = await owned.resources.backend.agents.create(
        scope, worker.spec, key="normalized-prefix"
    )
    previous = await owned.resources.catalog.get_agent(scope, owned.retained.catalog_id)
    assert previous is not None
    row = await owned.resources.catalog.put_agent(
        scope,
        owned.retained.catalog_id,
        actual,
        expected_revision=previous.local_revision,
    )
    plan = owned.plan.model_copy(
        update={
            "agent_id": actual.ref.id,
            "agent_name": worker.spec.name,
            "tools": tuple(worker.spec.tools or ()),
            "skill_ids": tuple(pin.id for pin in worker.spec.skills or ()),
            "system": worker.spec.system,
            "counterpart_catalog_revision": row.local_revision,
        }
    )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    request, _ = install_runtime(
        replace(request, scope=scope, worker=worker), owned, monkeypatch, transport
    )
    selected = await read_preparation_counterpart(request)
    assert selected.counterpart.agent.spec == worker.spec
    assert selected.source.effective_spec.system == "Retained instructions"


@pytest.fixture(params=[False])
def clone_source(request):
    return request.param


@pytest.fixture
async def claimed_worker_request(explicit_worker_request, monkeypatch, clone_source):
    from decimal import Decimal

    from daimon.core.authz import Surface
    from daimon.core.neutral_resources import resolve_worker_spec
    from daimon.core.stores import access_policy, fleet, mux_state, tenant_ledger, tenants
    from daimon.core.turn.admission import AdmissionGrant
    from daimon.core.turn.runtime_manifest import capture_preparation_source
    from mux.contracts.config import BackendConfig
    from mux.contracts.resources import ProviderBinding

    from ..test_fleet_budget import tariffs
    from ..test_fleet_permissions import POLICY, parent

    request, _, owned, transport = explicit_worker_request
    root = parent(owned.wire.provider)
    root = replace(
        root,
        fleet=replace(request.worker.permissions.fleet, depth=0, scope=owned.request.scope),
        agent_id=request.worker.permissions.agent_id,
        spec=owned.retained.agent.spec.model_copy(
            update={"system": request.worker.permissions.base_system}
        ),
        base_system=request.worker.permissions.base_system,
        limits=request.worker.limits,
    )
    sm = request.deps.sessionmaker
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=request.tenant_id, policy=POLICY)
        await tenants.set_funding_mode(db, tenant_id=request.tenant_id, funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=request.tenant_id,
            delta_usd=Decimal("10"),
            reason="trial_credit",
            idempotency_key="worker-origin-credit",
        )
        await mux_state.put_config_revision(db, request.admission.backend_revision, origin="agent")
        binding = ProviderBinding(
            id="orchestrator-binding",
            thread=root.fleet.root,
            legacy_account_id=str(root.fleet.account_id),
            provider=root.spec.model.provider,
            profile=root.backend_grants[0].profile,
            native_refs={"session": "orchestrator-session"},
            generation=1,
            config_revision=request.admission.backend_revision.local,
        )
        await mux_state.put_binding(db, binding, expected_generation=0)
        run = await fleet.create_run(
            db,
            tenant_id=request.tenant_id,
            orchestrator_account_id=root.fleet.account_id,
            orchestrator_session_id="orchestrator-session",
            orchestrator_binding_id=binding.id,
            platform=request.platform,
            channel_id=root.fleet.root.channel.channel_id,
            thread_id=root.fleet.root.thread_id,
            operation_key="worker-origin-run",
            total_budget_usd=root.limits.budget_usd,
            deadline_at=root.limits.deadline,
            max_workers=2,
            created_by="host",
        )
    root = replace(root, fleet=replace(root.fleet, run_id=run.id))
    source = await capture_preparation_source(
        replace(request, scope=root.fleet.scope, source=None, worker=None)
    )
    if clone_source:
        assert source.source is not None
        root = replace(
            root,
            source_agents=(source.source.ref,),
            source_revisions=((source.source.ref, source.source.revision),),
        )
    from mux.contracts.resources import AgentPatch

    resolved = resolve_worker_spec(
        root,
        source.source if clone_source else request.worker.spec,
        overrides=AgentPatch.model_validate(request.worker.spec.model_dump())
        if clone_source
        else None,
        policy=POLICY,
        now=request.now(),
        backend=BackendConfig.model_validate(
            request.admission.backend_revision.model_dump(include=set(BackendConfig.model_fields))
        ),
        resources=request.worker.resources,
        environment=request.worker.environment,
        limits=request.worker.limits,
        captured_source=source,
    )
    counterpart = await owned.resources.backend.agents.create(
        resolved.permissions.fleet.scope, resolved.spec, key="claimed-worker-counterpart"
    )
    retained = await owned.resources.catalog.get_agent(
        resolved.permissions.fleet.scope, owned.retained.catalog_id
    )
    assert retained is not None
    retained = await owned.resources.catalog.put_agent(
        resolved.permissions.fleet.scope,
        retained.catalog_id,
        counterpart,
        expected_revision=retained.local_revision,
    )
    plan = type(owned.plan).model_validate(json.loads(owned.manifest_path.read_text())[0])
    plan = plan.model_copy(
        update={
            "system": resolved.spec.system,
            "agent_id": counterpart.ref.id,
            "counterpart_catalog_revision": retained.local_revision,
        }
    )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    owned.wire.native_id = counterpart.ref.id
    owned.wire.requests.clear()
    async with sm.begin() as db:
        await fleet.add_worker(
            db,
            tenant_id=request.tenant_id,
            run_id=run.id,
            ordinal=0,
            role="researcher",
            spec=resolved.spec.model_dump(mode="json"),
            spec_digest=resolved.digest,
            backend=resolved.backend.backend,
            profile=resolved.backend.profile,
            model=resolved.spec.model.id,
            budget_usd=resolved.limits.budget_usd,
            deadline_at=resolved.limits.deadline,
            max_turns=resolved.limits.max_turns,
        )
    pool = fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1)
    worker = await pool.claim(sm, tenant_id=request.tenant_id, run_id=run.id)
    assert worker is not None and worker.status == "starting"
    grant = AdmissionGrant(
        tenant_id=request.tenant_id,
        subject=root.subject,
        surface=Surface.CHANNEL,
        turn_place=root.fleet.place,
        agent=root.agent,
        run_place=root.fleet.place,
        channel_id=root.fleet.root.channel.channel_id,
        thread_id=root.fleet.root.thread_id,
        is_dm=False,
    )
    selected = replace(
        request,
        scope=resolved.permissions.fleet.scope,
        worker=resolved,
        admission=replace(request.admission, grant=grant),
        deps=replace(request.deps, state_store=mux_state.PostgresStateStore(sm)),
    )
    native_requests = []

    def native(call):
        if call.url.path.endswith(":countTokens"):
            native_requests.append(call)
            # The real count never exceeds the cap; a derived cap is sized above it.
            cap = resolved.limits.max_input_tokens or 262144
            return httpx.Response(200, json={"totalTokens": min(262144, cap)})
        assert (call.method, call.url.path) in {
            ("POST", "/v1beta/interactions"),
            ("GET", "/v1beta/interactions/worker-result"),
        }
        native_requests.append(call)
        return httpx.Response(
            200,
            json={
                "id": "worker-result",
                "status": "completed",
                "outputs": [{"type": "text", "text": "verified-worker-result"}],
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "verified-worker-result"}],
                    }
                ],
                "usage": {
                    "total_input_tokens": 12,
                    "total_output_tokens": 3,
                    "total_thought_tokens": 0,
                },
            },
        )

    sdk = build_gemini_client(transport=httpx.MockTransport(native), api_key="offline")
    async with AsyncExitStack() as stack:
        stack.callback(sdk.close)
        stack.push_async_callback(sdk.aio.aclose)
        selected, _ = install_runtime(selected, owned, monkeypatch, GeminiSDKTransport(sdk))
        try:
            yield selected, root, worker, tariffs(root), owned, native_requests
        finally:
            pool.release()


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_claimed_worker_opaque_source_is_refused_before_funding_or_native_input(
    claimed_worker_request,
    monkeypatch,
):
    import asyncio

    from daimon.core import sessions
    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import WorkerSpecRefused
    from daimon.core.stores import fleet
    from mux.contracts.extensions import ExtensionConfig

    request, root, worker, prices, owned, calls = claimed_worker_request
    project = sessions.project_session_resources

    async def opaque(*args, **kwargs):
        source = await project(*args, **kwargs)
        assert source.effective_spec.extensions
        spec = source.effective_spec.model_copy(
            update={
                "extensions": tuple(source.effective_spec.extensions)
                + (
                    ExtensionConfig(
                        namespace="anthropic.unrepresented_policy", version=1, value={}
                    ),
                )
            }
        )
        return source.model_copy(update={"effective_spec": spec})

    monkeypatch.setattr(sessions, "project_session_resources", opaque)
    with pytest.raises(WorkerSpecRefused) as refused:
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert refused.value.field == "extensions" and refused.value.reason == "unsupported"
    assert calls == [] and owned.wire.requests == []
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db,
            tenant_id=request.tenant_id,
            actor_id=root.fleet.account_id,
            run_id=worker.run_id,
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == worker.id)
    assert persisted.status == "failed" and persisted.error_code == "preparation_refused"
    assert persisted.reserved_usd == 0
    assert persisted.turns_used == 0 and persisted.binding_id is None and persisted.result is None


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("clone_source", [False, True], indirect=True)
async def test_claimed_worker_runs_through_budget_binding_and_journal_result(
    claimed_worker_request,
):
    import asyncio

    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import mux_state

    request, root, worker, prices, owned, native_requests = claimed_worker_request
    if request.worker.source_agent is not None:
        assert request.worker.source_agent[0] in root.source_agents
        assert request.worker.source_agent in root.source_revisions
    result = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Complete the assigned task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.id == worker.id and result.status == "succeeded"
    assert result.turns_used == 1, "only the winning primary input consumes one turn"
    assert result.result is not None and result.result.excerpt == "verified-worker-result"
    async with request.deps.sessionmaker() as db:
        entries = await mux_state.read_events(
            db,
            result.result.session_id,
            after=result.result.sequence - 1,
            limit=1,
        )
    assert len(entries) == 1 and entries[0].type == "agent.message"
    assert entries[0].id == f"fleet:{worker.id}:result"
    posts = [call for call in native_requests if call.method == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].content)["agent"] == owned.wire.native_id
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
    async with request.deps.sessionmaker() as db:
        outcomes = await list_for_tenant(db, request.tenant_id)
    assert any(
        row.origin == "worker"
        and row.session_id == result.result.session_id
        and row.reason.value == "completed"
        for row in outcomes
    ), "the actual successful worker must also persist its terminal diagnostic"


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "disabled",
        "expired",
        "cancelled",
        "digest",
        "source",
        "turns",
        "model",
        "funding",
        "policy",
        "revision",
    ],
)
async def test_claimed_worker_refuses_invalid_dispatch_before_native_io(
    claimed_worker_request, fault
):
    import asyncio
    from datetime import timedelta

    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.errors import TurnError
    from daimon.core.fleet import FleetConflict
    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import WorkerSpecRefused

    request, root, worker, prices, owned, calls = claimed_worker_request
    cancel = asyncio.Event()
    if fault == "disabled":
        request = replace(request, deps=replace(request.deps, agent_backends=False))
    elif fault == "expired":
        worker = replace(worker, deadline_at=request.now() - timedelta(seconds=1))
    elif fault == "cancelled":
        cancel.set()
    elif fault == "digest":
        worker = replace(worker, spec_digest="another-intent")
    elif fault == "source":
        request = replace(
            request,
            admission=replace(
                request.admission,
                agent=request.admission.agent.model_copy(
                    update={"system": "Changed source instructions"}
                ),
            ),
        )
    elif fault == "turns":
        worker = replace(worker, turns_used=worker.max_turns)
    elif fault == "model":
        worker = replace(worker, model="ungranted-model")
    elif fault == "funding":
        from decimal import Decimal

        from daimon.core.stores import tenant_ledger

        async with request.deps.sessionmaker.begin() as db:
            await tenant_ledger.insert_entry(
                db,
                tenant_id=request.tenant_id,
                delta_usd=Decimal("-10"),
                reason="usage",
                idempotency_key="worker-funding-removed",
            )
    elif fault == "policy":
        from daimon.core.access_policy import ChannelRule
        from daimon.core.stores import access_policy

        async with request.deps.sessionmaker.begin() as db:
            policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
            await access_policy.set_access_policy(
                db,
                tenant_id=request.tenant_id,
                policy=policy.model_copy(
                    update={
                        "channel_rules": {
                            root.fleet.root.channel.channel_id: ChannelRule(writers="none"),
                        }
                    }
                ),
            )
    elif fault == "revision":
        from mux.contracts.config import ConfigRevision

        request = replace(
            request,
            admission=replace(
                request.admission,
                backend_revision=ConfigRevision.create(
                    request.admission.backend_revision.channel,
                    999,
                    root.backend_grants[0],
                ),
            ),
        )
    with pytest.raises(
        (FleetConflict, TurnError, AgentBackendRefused, WorkerSpecRefused, asyncio.CancelledError)
    ) as refused:
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Assigned task",
            prices=prices,
            cancel=cancel,
        )
    if fault == "policy":
        assert isinstance(refused.value, WorkerSpecRefused)
        assert refused.value.reason == "writers_none"
    assert calls == [] and owned.wire.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("fault", ["funding", "cancelled", "expired"])
async def test_claimed_worker_rechecks_authority_after_binding_before_native_input(
    claimed_worker_request, monkeypatch, fault
):
    import asyncio
    from datetime import timedelta
    from decimal import Decimal

    from daimon.core import headless_runner
    from daimon.core.errors import TurnError
    from daimon.core.fleet import FleetConflict
    from daimon.core.stores import fleet, tenant_ledger

    request, root, worker, prices, _, calls = claimed_worker_request
    cancel = asyncio.Event()
    clock = request.now()
    request = replace(request, now=lambda: clock)
    real_bind = headless_runner.bind_session_impl
    prepared = []

    async def bind(*args, **kwargs):
        publish = kwargs["_on_prepared"]

        async def revoke(result):
            nonlocal clock
            await publish(result)
            prepared.append(result)
            if fault == "funding":
                async with request.deps.sessionmaker.begin() as db:
                    await tenant_ledger.insert_entry(
                        db,
                        tenant_id=request.tenant_id,
                        delta_usd=Decimal("-10"),
                        reason="usage",
                        idempotency_key="worker-funding-removed-after-binding",
                    )
            elif fault == "cancelled":
                cancel.set()
            else:
                clock = worker.deadline_at + timedelta(seconds=1)

        kwargs["_on_prepared"] = revoke
        return await real_bind(*args, **kwargs)

    monkeypatch.setattr(headless_runner, "bind_session_impl", bind)
    with pytest.raises((FleetConflict, TurnError, asyncio.CancelledError)):
        await headless_runner.run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Assigned task",
            prices=prices,
            cancel=cancel,
        )
    assert len(prepared) == 1, "the real binding callback must finish before authority changes"
    assert calls == [], "revoked authority must prevent the first native model request"
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db,
            tenant_id=request.tenant_id,
            actor_id=root.fleet.account_id,
            run_id=worker.run_id,
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == worker.id)
    assert persisted.turns_used == 0 and persisted.result is None


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_worker_lost_native_ack_never_reposts_or_counts_a_second_input(
    claimed_worker_request,
    monkeypatch,
):
    import asyncio
    from datetime import timedelta

    from daimon.core.errors import TurnError
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet

    request, root, worker, prices, _, calls = claimed_worker_request
    request = replace(request, deadline=request.now() + timedelta(seconds=5))
    send = httpx.MockTransport.handle_async_request

    async def lost_ack(transport, native_request):
        response = await send(transport, native_request)
        if native_request.method == "POST" and native_request.url.path == "/v1beta/interactions":
            raise httpx.ReadError("offline lost acknowledgment", request=native_request)
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", lost_ack)
    with pytest.raises(TurnError) as refused:
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert refused.value.kind == "ceiling"
    assert len([call for call in calls if call.method == "POST"]) == 1
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db,
            tenant_id=request.tenant_id,
            actor_id=root.fleet.account_id,
            run_id=worker.run_id,
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == worker.id)
    assert persisted.turns_used == 1 and persisted.result is None
    assert persisted.status == "running" and persisted.reserved_usd > 0


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_readonly_worker_proof_never_invokes_optional_materialization(
    explicit_worker_request,
):
    request, runtime, owned, transport = explicit_worker_request
    assert runtime.materialize_worker is None

    async def forbidden(selected):
        raise AssertionError("read-only proof attempted native materialization")

    request = replace(
        request,
        deps=replace(
            request.deps,
            turn_runtimes={
                request.admission.backend_revision.profile: replace(
                    runtime, materialize_worker=forbidden
                )
            },
        ),
    )
    selected = await read_preparation_counterpart(request)
    assert selected.counterpart is not None
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_claimed_worker_supplies_its_fresh_guard_to_registered_materialization(
    claimed_worker_request,
):
    import asyncio

    from daimon.core.headless_runner import run_worker
    from daimon.core.turn.persistence import current_persistence
    from mux.errors import UnsupportedCapability

    request, root, worker, prices, owned, calls = claimed_worker_request
    profile = request.admission.backend_revision.profile
    runtime = request.deps.turn_runtimes[profile]
    reached = []

    async def materialize(selected):
        persistence = current_persistence.get()
        assert persistence is not None
        assert persistence.scope == selected.scope == selected.worker.permissions.fleet.scope
        assert persistence.store is selected.deps.state_store
        assert persistence.slot.thread.thread_id == selected.thread_id
        assert selected.source.source.ref.id == selected.admission.agent.id
        assert selected.before_native is not None
        await selected.before_native()
        reached.append(selected)
        raise UnsupportedCapability(("unsupported_worker_source",), profile)

    request = replace(
        request,
        deps=replace(
            request.deps, turn_runtimes={profile: replace(runtime, materialize_worker=materialize)}
        ),
    )
    with pytest.raises(AdmissionDenied) as refused:
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert refused.value.reason == "backend_unsupported"
    assert len(reached) == 1
    assert calls == [] and owned.wire.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize(
    "fault", ["missing_context", "unleased", "scope", "store", "thread", "account"]
)
async def test_materialization_requires_the_exact_active_preparation_owner(
    explicit_worker_request,
    fault,
):
    from contextlib import nullcontext

    from daimon.core.turn.persistence import TurnPersistence
    from mux.contracts.ids import ThreadRef
    from mux.state.lease import Slot
    from mux.state.memory import MemoryStateStore

    request, runtime, owned, transport = explicit_worker_request
    profile = request.admission.backend_revision.profile
    calls = []

    async def forbidden(selected):
        calls.append(selected)
        raise AssertionError("unauthorized materialization")

    async def guard():
        calls.append("guard")

    store = request.deps.state_store
    scope = request.scope
    thread = ThreadRef(
        channel=request.admission.backend_revision.channel, thread_id=request.thread_id
    )
    if fault == "scope":
        scope = scope.model_copy(update={"authorization_id": "another-authority"})
    if fault == "store":
        store = MemoryStateStore()
    if fault == "thread":
        thread = thread.model_copy(update={"thread_id": "another-thread"})
    slot = Slot(thread=thread, account_id=None)
    if fault != "account":
        slot = slot.model_copy(update={"account_id": scope.account_id})
    persistence = TurnPersistence(store, None, scope, slot=slot, operation_key="proof-spawn")
    request = replace(
        request,
        before_native=guard,
        deps=replace(
            request.deps, turn_runtimes={profile: replace(runtime, materialize_worker=forbidden)}
        ),
    )
    with (
        persistence.activate() if fault != "missing_context" else nullcontext(),
        pytest.raises(AdmissionDenied),
    ):
        await read_preparation_counterpart(request)
    assert calls == [] and owned.wire.requests == [] and not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("fault", ["none", "ack_drift", "expired", "cancelled"])
async def test_materialization_handoff_retains_verified_ack_and_deadline(
    explicit_worker_request,
    fault,
):
    import asyncio
    from datetime import timedelta

    from daimon.core.turn.persistence import TurnPersistence
    from mux.contracts.ids import ThreadRef
    from mux.state.lease import Slot

    request, runtime, owned, transport = explicit_worker_request
    profile = request.admission.backend_revision.profile
    # This proves callback handoff of an already-published real catalog component;
    # fresh provider allocation is exercised by each provider's actual caller test.
    read = runtime.read_agent_counterpart
    clock = [request.now()]
    stages = []

    async def guard():
        stages.append("guard")
        if fault == "cancelled":
            raise asyncio.CancelledError

    async def materialize(selected):
        stages.append("materialize")
        assert selected.source.resource_scope == request.scope
        assert selected.before_native is guard
        component = await read(selected)
        if fault == "expired":
            clock[0] = selected.deadline + timedelta(seconds=1)
        if fault == "ack_drift":
            component = component.model_copy(
                update={"local_revision": component.local_revision + 1}
            )
        return component

    request = replace(
        request,
        now=lambda: clock[0],
        before_native=guard,
        deps=replace(
            request.deps, turn_runtimes={profile: replace(runtime, materialize_worker=materialize)}
        ),
    )
    slot = Slot(
        thread=ThreadRef(
            channel=request.admission.backend_revision.channel, thread_id=request.thread_id
        ),
        account_id=request.scope.account_id,
    )
    persistence = TurnPersistence(
        request.deps.state_store,
        None,
        request.scope,
        slot=slot,
        operation_key="proof-spawn",
        now=request.now,
    )

    async def prove():
        return await read_preparation_counterpart(request)

    with persistence.activate():
        if fault == "none":
            selected = await persistence.run(prove)
            assert selected.counterpart.agent.spec == request.worker.spec
        else:
            with pytest.raises(asyncio.CancelledError if fault == "cancelled" else AdmissionDenied):
                await persistence.run(prove)
    assert stages == (["guard"] if fault == "cancelled" else ["guard", "materialize"])
    assert all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize(
    "fault", [None, "source_id", "source_revision", "catalog", "revision", "native"]
)
async def test_dynamic_worker_pins_require_source_authority_without_a_fixed_target(
    explicit_worker_request,
    fault,
):
    from daimon.core.turn.runtime_manifest import validate_counterpart_pins

    _, _, owned, _ = explicit_worker_request
    values = {
        "agent_id": None,
        "counterpart_catalog_id": None,
        "counterpart_catalog_revision": None,
    }
    if fault == "source_id":
        values["source_agent_id"] = None
    elif fault == "source_revision":
        values["source_agent_version"] = None
    elif fault == "catalog":
        values["counterpart_catalog_id"] = "fixed-target"
    elif fault == "revision":
        values["counterpart_catalog_revision"] = 1
    elif fault == "native":
        values["agent_id"] = "fixed-agent"
    # Exercise the existing pin owner directly; provider plan validators call it
    # before manifest selection or native work.
    plan = owned.plan.model_copy(update=values)
    if fault is None:
        validate_counterpart_pins(plan, worker_materialization=True)
        with pytest.raises(ValueError, match="complete source and catalog revision pins"):
            validate_counterpart_pins(plan)
    else:
        with pytest.raises(ValueError, match="source pins without fixed target pins"):
            validate_counterpart_pins(plan, worker_materialization=True)


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("fault", ["identity", "revision", "missing_source_grant"])
async def test_clone_dispatch_cannot_invent_or_reuse_source_lineage(
    claimed_worker_request,
    fault,
):
    import asyncio

    from daimon.core.fleet import FleetConflict
    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import WorkerSpecRefused
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    request, root, worker, prices, owned, calls = claimed_worker_request
    source = await capture_preparation_source(
        replace(request, scope=root.fleet.scope, source=None, worker=None)
    )
    ref, revision = source.source.ref, source.source.revision
    if fault == "identity":
        ref = ref.model_copy(update={"id": "another-source"})
    elif fault == "revision":
        revision = revision.model_copy(update={"local": revision.local + 1})
    request = replace(request, worker=replace(request.worker, source_agent=(ref, revision)))
    with pytest.raises((FleetConflict, WorkerSpecRefused)):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert calls == [] and owned.wire.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_worker_cleanup_uses_prepared_ports_and_preserves_the_original_failure(
    claimed_worker_request,
    monkeypatch,
    capsys,
):
    import asyncio

    from daimon.core import headless_runner
    from daimon.core.fleet import FleetConflict
    from daimon.core.turn.persistence import TurnPersistence

    request, root, worker, prices, _, calls = claimed_worker_request
    entered = []

    async def refused(self, *args, **kwargs):
        entered.append(self.binding.native_refs["session"])
        raise FleetConflict("offline final message refusal")

    def forbidden_codec(*args, **kwargs):
        raise AssertionError("cleanup rebuilt a codec outside its turn context")

    # Result publication fails after the real SDK input and terminal frame.
    monkeypatch.setattr(TurnPersistence, "publish_worker_result", refused)
    monkeypatch.setattr(headless_runner, "turn_io", forbidden_codec)
    with pytest.raises(FleetConflict, match="offline final message refusal"):
        await headless_runner.run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert len(entered) == 1
    assert len([call for call in calls if call.method == "POST"]) == 1
    output = capsys.readouterr().out
    assert output.count("turn.recovery_orphan_archive_failed") == 1
    assert "session_archive" in output
    from daimon.core.stores import fleet

    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=request.tenant_id, actor_id=root.fleet.account_id, run_id=worker.run_id
        )
        retained = next(row for _, rows in graph for row in rows if row.id == worker.id)
    assert retained.turns_used == 1 and retained.result is None
    assert retained.reserved_usd > 0 and retained.status == "running"


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_a_stored_secret_never_reaches_a_worker_that_declares_files(
    explicit_worker_request,
):
    """#1357: a root's stored .env is left out of the worker ceiling, not carried."""
    from daimon.core._models import AgentFile
    from daimon.core.turn.runtime_manifest import worker_source_ceiling

    request, _, owned, transport = explicit_worker_request
    assert "files" in request.worker.declared_drops
    async with request.deps.sessionmaker.begin() as db:
        db.add(
            AgentFile(
                tenant_id=request.tenant_id,
                agent_id=request.worker.permissions.agent_id,
                key="UNREAD_PRIVATE",
                content="invalid-ciphertext",
                encoding="fernet_v1",
            )
        )
    selected = await read_preparation_counterpart(request)
    stored = [
        binding
        for binding in selected.source.resource_requirements
        if (binding.credential_ref or "").startswith("agent-env:")
    ]
    assert stored, "the projector still records the root's stored secret"
    ceiling = worker_source_ceiling(selected.source)
    dumped = json.dumps(
        [
            ceiling.effective_spec.model_dump(mode="json"),
            [binding.model_dump(mode="json") for binding in ceiling.resource_requirements],
            selected.counterpart.agent.spec.model_dump(mode="json"),
        ]
    )
    assert "agent-env:" not in dumped and "UNREAD_PRIVATE" not in dumped
    assert "invalid-ciphertext" not in dumped
    assert all(r.method == "GET" for r in owned.wire.requests)
    assert transport.requests == []
