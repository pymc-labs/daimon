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

from .native_counterpart_support import application as application
from .native_counterpart_support import clean_sdk_environment as clean_sdk_environment
from .native_counterpart_support import db_preflight as db_preflight
from .native_counterpart_support import owned as owned
from .native_counterpart_support import owned_database_preflight as owned_database_preflight
from .native_counterpart_support import provider as provider
from .test_openai_host import Wire as OpenAIWire


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
    # The other turn flags keep their own (on) defaults: each one is a separate stop.
    assert deps.turn_path == "mux" and deps.channel_backends is True
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


@pytest.fixture(params=[False])
def clone_source(request):
    return request.param
