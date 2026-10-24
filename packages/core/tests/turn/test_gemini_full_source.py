"""Full default source survives consent, native ACK/readback and ordinary turns.

All SDK wire calls are intercepted. GCS receipts represent separately owned
provisioning; these tests perform no GCS or provider writes.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import os
import re
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
import pytest
import yaml
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.mcp_auth import token_jti
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores import mux_state
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.tool_safety import confirmation_gated_tools
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_deployment import GeminiPlan, build_gemini_runtime
from daimon.core.turn.gemini_state import SQLiteGeminiStorage
from daimon.core.turn.prepare import admitted_session_scope, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import (
    capture_preparation_source,
    prepare_source,
    read_preparation_counterpart,
    source_preservation_pin,
)
from daimon.core.turn.runtimes import build_channel_runtime, scope_channel_runtimes
from daimon.testing import f2_mcp_binding
from daimon.testing.db import sibling_nullpool_engine
from daimon.testing.f2_mcp_binding import AgentMCPAuthority, AgentMCPBinding
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, SkillRef
from mux.contracts.resources import EnvironmentSpec, SkillUpload, SkillUploadFile
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.bundles import SKILL_SOURCE, validate_bundle
from mux.drivers.gemini.deployment import configured_catalog_transport, configured_transport
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.drivers.gemini.skill_deployment import (
    GCSBundleDeployment,
    RetainedSkillDeployments,
    file_manifest,
)
from mux.errors import ScopeViolation, UnsupportedCapability

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_codec import interaction
from .test_gemini_runtime_transport import clients_for

ROOT = Path(__file__).resolve().parents[4]
DEFAULT = yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
# Anthropic's built-in skills have no bytes to represent: Gemini runs without them.
# A platform-limited custom skill (microsoft-365) is not on the seeded agent.
CUSTOM = [s for s in DEFAULT["skills"] if s["type"] == "custom" and "platforms" not in s]
BUILT_IN = [s for s in DEFAULT["skills"] if s["type"] == "anthropic"]
PARAMS = {
    "backend": "gemini",
    "caller_mcp": True,
    "tools": [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {"name": t["name"], "enabled": True, "permission_policy": {"type": "always_allow"}}
                for t in DEFAULT["tools"][0]["configs"]
            ],
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "configs": [],
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        },
    ],
    "skills": [
        *(
            {"type": "custom", "skill_id": f"source-{s['skill_id']}", "version": "1"}
            for s in CUSTOM
        ),
        *({"type": "anthropic", "skill_id": s["skill_id"], "version": "1"} for s in BUILT_IN),
    ],
}

LATEST_PARAMS = {**PARAMS, "skills": [{**skill, "version": "latest"} for skill in PARAMS["skills"]]}

GATED_CATALOGUE = (
    "add_skill",
    "call_tool",
    "create_attachment_upload_url",
    "create_notebook_upload_url",
    "publish_report",
    "search_tools",
)
GATED_PARAMS = {
    **PARAMS,
    "tools": [
        PARAMS["tools"][0],
        {
            **PARAMS["tools"][1],
            "configs": [
                {"name": "add_skill", "enabled": True, "permission_policy": {"type": "always_ask"}}
            ],
        },
    ],
}

# The recorded F2 source explicitly disables web tools and enumerates its MCP
# tools; the recorded root above instead enables both builtin defaults.
F2_PARAMS = {
    **PARAMS,
    "tools": [
        {
            **PARAMS["tools"][0],
            "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
            "configs": [
                *PARAMS["tools"][0]["configs"],
                *[
                    {"name": name, "enabled": False, "permission_policy": {"type": "always_allow"}}
                    for name in ("web_fetch", "web_search")
                ],
            ],
        },
        {
            **PARAMS["tools"][1],
            "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {"name": name, "enabled": True, "permission_policy": {"type": "always_allow"}}
                for name in ("describe_agent", "list_my_sessions")
            ],
        },
    ],
}


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as packed:
        for path, content in files.items():
            packed.writestr(f"retained/{path}", content)
    return stream.getvalue()


@functools.cache
def default_skill_bundles() -> tuple[tuple[str, SkillUpload, str, bytes], ...]:
    """Read, validate and zip the checked-in default skills once per process.

    Everything returned is immutable (frozen upload models, a version string and
    zip bytes); the fixture writes the bytes to a per-test file, so no test can
    change what another reads.
    """
    bundles = []
    for item in CUSTOM:
        name = item["skill_id"]
        directory = ROOT / "defaults/skills" / name
        files = {
            p.relative_to(directory).as_posix(): p.read_bytes()
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        }
        bundle = SkillUpload(
            files=tuple(SkillUploadFile(path=p, content=b) for p, b in files.items())
        )
        bundles.append((name, bundle, validate_bundle(bundle, external=True), archive(files)))
    return tuple(bundles)


@pytest.fixture
async def source_mcp(consent_request):
    """Actual verifier, identity narrowing and search collapse at the HTTP edge."""
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
    from daimon.adapters.mcp.middleware.mcp_identity import (
        IdentityMiddleware,
        production_agent_id_resolver,
        production_internal_resolver,
        production_is_admin_resolver,
        production_role_resolver,
        production_subject_resolver,
        production_tenant_resolver,
    )
    from daimon.adapters.mcp.search_transform import AgentChatAwareBM25SearchTransform
    from fastmcp import FastMCP

    request = consent_request[0]
    mcp = FastMCP(
        "source-catalogue",
        auth=DaimonJWTVerifier(
            secret=request.deps.mcp.jwt_secret.get_secret_value().encode(),
            sessionmaker=request.deps.sessionmaker,
        ),
    )
    identity = IdentityMiddleware(
        subject_resolver=production_subject_resolver,
        tenant_resolver=production_tenant_resolver,
        role_resolver=production_role_resolver,
        agent_id_resolver=production_agent_id_resolver,
        is_admin_resolver=production_is_admin_resolver,
        internal_resolver=production_internal_resolver,
        sessionmaker=request.deps.sessionmaker,
    )
    mcp.add_middleware(identity)
    for name in ("describe_agent", "list_my_sessions"):

        async def read_tool() -> str:
            return "offline"

        mcp.tool(read_tool, name=name, tags={"agent-chat"})
    mcp.add_transform(AgentChatAwareBM25SearchTransform(max_results=5))
    app = mcp.http_app()
    try:
        yield app
    finally:
        await identity.drain_audit()


@pytest.fixture
async def full(consent_request, source_mcp, monkeypatch, tmp_path, request):
    gated = getattr(request, "param", False)
    request, service, source, actor, accepted, router, source_reads, *_ = consent_request
    from daimon.core.scope import DeploymentDefault

    sessions = request.deps.sessionmaker
    router.add_environment_list(request.admission.environment)
    scope = admitted_session_scope(
        request.admission, tenant_id=request.tenant_id, session_id="preparation"
    )
    request = replace(
        request,
        scope=scope,
        admission=replace(
            request.admission,
            origin_channel_id=request.admission.backend_revision.channel.channel_id,
        ),
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        deps=replace(
            request.deps,
            deployment_default=DeploymentDefault(
                agent_name=source.name, environment_name="test-env"
            ),
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
            state_store=PostgresStateStore(sessions),
        ),
    )
    bundles, archives, deployments = [], [], []
    for name, bundle, version, data in default_skill_bundles():
        bundles.append(bundle)
        # Each test gets its own archive file: tests overwrite it to model drift.
        path = tmp_path / f"{name}.zip"
        path.write_bytes(data)
        archives.append(path)
        skill_id = f"source-{name}"
        router.add(
            "GET",
            rf"/v1/skills/{skill_id}/versions",
            lambda *_, sid=skill_id: httpx.Response(
                200, json={"data": [{"id": f"version-{sid}", "version": "1"}], "has_more": False}
            ),
        )
        router.add(
            "GET",
            rf"/v1/skills/{skill_id}/versions/version-{skill_id}/content",
            lambda *_, body=data: httpx.Response(200, content=body),
        )
        deployments.append(
            GCSBundleDeployment(
                scope.tenant_id,
                scope.account_id,
                "project",
                version,
                f"gs://owned-offline/{scope.tenant_id}/{scope.account_id}/{version}/",
                file_manifest(bundle),
            )
        )

    def no_catalogue(*_):
        pytest.fail("Source attachment resolution must not enumerate other custom skills")

    router.add("GET", r"/v1/skills", no_catalogue)
    for item in CUSTOM:
        skill_id = f"source-{item['skill_id']}"
        router.add(
            "GET",
            rf"/v1/skills/{skill_id}",
            lambda *_, sid=skill_id: httpx.Response(
                200,
                json={
                    "id": sid,
                    "type": "skill",
                    "source": "custom",
                    "latest_version": "1",
                    "display_title": sid,
                    "created_at": "2026-10-14T00:00:00Z",
                    "updated_at": None,
                },
            ),
        )
    native, calls, posts = {}, [], []

    def handle(call):
        if call.url.host == "sts.googleapis.com":
            assert call.method == "POST" and call.url.path == "/v1/token"
            body = parse_qs(call.content.decode())
            assert body["subject_token"] == ["offline-storage-token"]
            boundary = json.loads(unquote(body["options"][0]))
            rules = boundary["accessBoundary"]["accessBoundaryRules"]
            assert len(rules) == 1
            rule = rules[0]
            assert (
                rule["availableResource"]
                == "//storage.googleapis.com/projects/_/buckets/owned-offline"
            )
            assert rule["availablePermissions"] == ["inRole:roles/storage.objectViewer"]
            assert (
                f".startsWith('{scope.tenant_id}/{scope.account_id}/')"
                in rule["availabilityCondition"]["expression"]
            )
            return httpx.Response(
                200,
                json={
                    "access_token": "offline-downscoped-token",
                    "expires_in": 3300,
                    "token_type": "Bearer",
                },
            )
        calls.append(call)
        if call.url.path.startswith("/v1beta/agents"):
            if call.method == "POST":
                native.update(json.loads(call.content))
            else:
                assert call.method == "GET" and call.url.path.endswith(native["id"])
            return httpx.Response(200, json=native)
        if call.method == "POST":
            posts.append(json.loads(call.content))
            return httpx.Response(200, json=interaction(f"turn-{len(posts)}"))
        return httpx.Response(200, json=interaction(call.url.path.rsplit("/", 1)[-1]))

    # Exercise actual MCP initialize/tools-list and generation-pinned GCS GETs.
    # Only the HTTP edge is replaced; all validation stays production code.
    class ReadbackClient(httpx.AsyncClient):
        pass

    readback_calls = []
    catalogue_state = {"names": list(GATED_CATALOGUE) if gated else None}
    storage = {}
    for deployment, bundle in zip(deployments, bundles, strict=True):
        prefix = deployment.source.split("owned-offline/", 1)[1]
        for file in bundle.files:
            storage[prefix + file.path] = file.content

    async def readback(call):
        readback_calls.append(call)
        if call.url.host == "mcp.invalid":
            body = json.loads(call.content)
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "offline", "version": "1"},
            }
            if body["method"] == "tools/list":
                from daimon.testing.asgi import mcp_session

                result = (
                    await mcp_session(
                        source_mcp,
                        token=call.headers["Authorization"].removeprefix("Bearer "),
                        method="tools/list",
                    )
                )["result"]
                if catalogue_state["names"] is not None:
                    result = {
                        "tools": [
                            {"name": name, "inputSchema": {"type": "object"}}
                            for name in catalogue_state["names"]
                        ]
                    }
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        assert call.url.host == "storage.googleapis.com" and call.method == "GET"
        assert call.headers["Authorization"] == "Bearer offline-downscoped-token"
        if call.url.params.get("alt") == "media":
            assert call.url.params["generation"] == "123"
            return httpx.Response(200, content=storage[call.url.path.split("/o/", 1)[1]])
        prefix = call.url.params["prefix"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": name,
                        "bucket": "owned-offline",
                        "generation": "123",
                        "retention": {"mode": "Locked", "retainUntilTime": "2027-01-01T00:00:00Z"},
                    }
                    for name in storage
                    if name.startswith(prefix)
                ]
            },
        )

    clients = clients_for(monkeypatch, handle)
    sdk_client = httpx.AsyncClient

    class RouteClient(sdk_client):
        def __init__(self, **kwargs):
            if "transport" in kwargs:
                ReadbackClient.__init__(self, **kwargs)
            elif "verify" in kwargs:
                # Preserve the real SDK's explicit network-policy assertion.
                super().__init__(**kwargs)
            else:
                ReadbackClient.__init__(self, transport=httpx.MockTransport(readback), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RouteClient)

    async def never_resolve(*_):
        pytest.fail("Catalog persistence must never resolve a bearer")

    backend = GeminiManagedAgents(
        configured_transport(api_key="offline-full-source"),
        storage=SQLiteGeminiStorage(
            tmp_path / "records.sqlite",
            namespace=json.dumps(
                [scope.tenant_id, scope.account_id, "project"], separators=(",", ":")
            ),
        ),
        state_store=request.deps.state_store,
        account_scope_id="project",
        resource_catalog_enabled=True,
        resource_catalog_transport=configured_catalog_transport(api_key="offline-full-source"),
        mcp_secrets=never_resolve,
        skill_bundle_publisher=RetainedSkillDeployments(tuple(deployments)),
    )
    mappings = []
    for item, bundle, path in zip(CUSTOM, bundles, archives, strict=True):
        skill = await backend.skills.create(scope, bundle, key=item["skill_id"])
        version = skill.latest_version.version
        mappings.append(
            {
                "source": {"id": f"source-{item['skill_id']}", "source": "custom", "version": "1"},
                "target": SkillRef(
                    id=skill.id, source=SKILL_SOURCE, version=version, digest=version
                ).model_dump(mode="json"),
                "archive": str(path),
            }
        )
    manifest = tmp_path / "runtime.json"
    capabilities = {
        "source_pin": source_preservation_pin(source),
        "model": ModelRef(provider="gemini", id=FLASH_PRIMARY).model_dump(mode="json"),
        "skills": mappings,
        "mcp_tools": list(GATED_CATALOGUE) if gated else ["search_tools", "call_tool"],
    }
    manifest.write_text(
        json.dumps(
            [
                {
                    "tenant_id": scope.tenant_id,
                    "account_id": scope.account_id,
                    "profile": "gemini.inline_reuse",
                    "platform": request.platform,
                    "channel_id": request.admission.backend_revision.channel.channel_id,
                    "source_agent_id": str(source.agent_id),
                    "source_agent_version": source.source.revision.local,
                    "source_capabilities": capabilities,
                }
            ]
        )
    )
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    from daimon.core.channel_backend import AgentBackendService

    async def capture_bound_source(actual_source):
        # Drive the actual public preview owner: it intentionally has no
        # selected backend revision before the caller confirms a target.
        from daimon.core.authz import Place
        from daimon.core.scope import ScopeContext
        from daimon.core.turn.admission import capture_backend_source

        return await capture_backend_source(
            request.deps,
            actual_source,
            context=ScopeContext(
                tenant_id=request.tenant_id,
                account_id=request.admission.account_id,
                platform=request.platform,
                channel_id=request.admission.origin_channel_id,
                thread_id=request.thread_id,
            ),
            subject=actor.subject,
            place=Place(
                channel_id=request.thread_id, parent_channel_id=request.admission.origin_channel_id
            ),
            is_dm=False,
            slack_turn_context_id=None,
            private_dm_id=None,
            external_user_id=request.external_user_id,
        )

    service = AgentBackendService(sessions, enabled=True, capture_source=capture_bound_source)
    # This is the same fresh owner capture used by both preview and preparation.
    represented = await capture_preparation_source(request, fresh=True)
    assert source_preservation_pin(represented) == source_preservation_pin(source)
    quote = await service.preview(source, "gemini")
    if not quote.allowed or {"tools", "skills", "mcp"}.intersection(quote.declared_drops):
        # Full-mode consent never accepts loss of any represented capability.
        with pytest.raises(AgentBackendRefused):
            await service.set_backend(
                source,
                "gemini",
                accepted.local,
                actor,
                accepted_drops=("memory", "sandbox"),
                expected_preview_digest=quote.digest,
            )
        yield {"quote": quote, "calls": calls, "represented": represented}
        return
    assert not {"tools", "skills", "mcp"}.intersection(quote.declared_drops)
    if gated:
        with pytest.raises(AgentBackendRefused, match="drops mismatch"):
            await service.set_backend(
                source,
                "gemini",
                accepted.local,
                actor,
                accepted_drops=("memory", "sandbox"),
                expected_preview_digest=quote.digest,
            )
    accepted = await service.set_backend(
        source,
        "gemini",
        accepted.local,
        actor,
        accepted_drops=quote.declared_drops,
        expected_preview_digest=quote.digest,
    )
    revision = ConfigRevision.create(
        request.admission.backend_revision.channel, 2, request.admission.backend_revision
    )
    async with sessions.begin() as db:
        await mux_state.put_config_revision(db, revision, origin="agent", agent_revision=accepted)
    request = replace(request, admission=replace(request.admission, backend_revision=revision))
    canonical = await prepare_source(request)
    host = NeutralResourceHost(
        backend,
        sessions,
        scope=scope,
        account_scope_id="project",
        operation_key="full-publication",
        resource_catalog_enabled=True,
        catalog_adapter=backend.catalog_adapter,
    )
    counterpart = await host.publish(
        "full-counterpart", canonical.target_spec, expected_revision=0, key="full-publication"
    )
    assert native and all("Bearer" not in json.dumps(body) for body in [native])
    # Use the same after-ACK owner the provisioner invokes, including actual
    # registry commit/readback, durable intent and exact-JTI revocation.
    signer = tmp_path / "mcp.signer"
    signer.write_text(request.deps.mcp.jwt_secret.get_secret_value())
    signer.chmod(0o600)
    auth_output = tmp_path / "after-ack"
    auth_output.mkdir(mode=0o700)
    # The binding disposes its engine on close; give it a sibling, not the shared pool.
    monkeypatch.setattr(
        f2_mcp_binding,
        "create_async_engine",
        lambda *_, **__: sibling_nullpool_engine(sessions.kw["bind"]),
    )
    redacted = []
    # The captured native read uses a service account. Canonical source pins
    # intentionally omit that transport account; the verified caller resource
    # scope below supplies the mint authority, exactly as source consent does.
    assert source.resource_scope.tenant_id == scope.tenant_id
    assert source.resource_scope.account_id == scope.account_id
    canonical_ref = source.source.ref.model_copy(update={"account_id": None})
    binding = AgentMCPBinding(
        authority=AgentMCPAuthority(
            provider="gemini",
            scope=scope,
            account_scope_id="project",
            output=auth_output,
            deadline_s=600,
            database_url=os.environ["DAIMON_DATABASE__TEST_URL"],
            mcp_signing_secret_file=signer,
            canonical_source=canonical_ref,
            chat_execution=True,
            acknowledged=lambda actual: (
                actual == counterpart.agent.ref
                and any(
                    call.method == "GET" and call.url.path.endswith(actual.id) for call in calls
                )
            ),
            redact=redacted.extend,
        )
    )

    token = await binding.bind(counterpart.agent.ref)
    assert token in redacted
    credential = tmp_path / "caller.token"
    credential.write_text(token)
    credential.chmod(0o600)
    gcs_credential = tmp_path / "gcs.token"
    gcs_credential.write_text(
        json.dumps(
            {
                "access_token": "offline-storage-token",
                "expires_at": (request.now() + timedelta(minutes=55)).isoformat(),
            }
        )
    )
    gcs_credential.chmod(0o600)
    plan = GeminiPlan(
        profile=revision.profile,
        tenant_id=scope.tenant_id,
        platform=request.platform,
        channel_id=revision.channel.channel_id,
        account_id=scope.account_id,
        config_digest=revision.digest,
        project="project",
        api_key_env="OFFLINE_FULL_GEMINI_KEY",
        records_path=tmp_path / "records.sqlite",
        holds_path=tmp_path / "holds.sqlite",
        hold_usd="0.05",
        cap_usd="0.25",
        agent_name="operator-label",
        tools=canonical.target_spec.tools,
        environment=EnvironmentSpec(name="remote"),
        skill_ids=tuple(s.id for s in canonical.target_spec.skills),
        agent_id=counterpart.agent.ref.id,
        source_agent_id=source.agent_id,
        source_agent_version=source.source.revision.local,
        counterpart_catalog_id=counterpart.catalog_id,
        counterpart_catalog_revision=counterpart.local_revision,
        source_capabilities=capabilities,
        mcp_credential_file=credential,
        gcs_credential_file=gcs_credential,
        gcs_mount_bucket="owned-offline",
        gcs_mount_prefix=f"{scope.tenant_id}/{scope.account_id}/",
        skill_deployments=tuple(deployments),
    )
    manifest.write_text(json.dumps([plan.model_dump(mode="json")]))
    monkeypatch.setenv(plan.api_key_env, "offline-full-source")
    calls.clear()
    try:
        yield (
            request,
            plan,
            canonical,
            native,
            calls,
            posts,
            token,
            archives,
            source_reads,
            router,
            storage,
            catalogue_state,
            readback_calls,
            capture_bound_source,
        )
    finally:
        await binding.close()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "consent_request,full",
    [(PARAMS, False), (LATEST_PARAMS, False), (GATED_PARAMS, True)],
    indirect=True,
    ids=["root-concrete", "root-latest", "gated-mcp-tool"],
)
async def test_full_default_preparation_and_fresh_followup_turns(full, request):
    gated = request.node.callspec.params["full"]
    assert isinstance(full, tuple), "full source could not retain its authority"
    request, plan, canonical, native, calls, posts, token, *_ = full
    assert BUILT_IN, "the default agent carries Anthropic built-ins for this test to skip"
    assert len(canonical.target_spec.skills) == len(CUSTOM) == 11, (
        "every authored skill is represented and the Anthropic built-ins are left out"
    )
    # Assert the native request carries the complete represented authority.
    expected_tools = {tool.name for tool in canonical.target_spec.tools}
    assert {
        tool["type"] for tool in native["tools"] if tool["type"] != "mcp_server"
    } == expected_tools
    assert len(native["base_environment"]["sources"]) == 11
    assert all(s["type"] == "gcs" for s in native["base_environment"]["sources"])
    selected = await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    assert selected.verified_counterpart == selected.counterpart
    assert all(call.method == "GET" for call in calls)
    session = None
    for index in range(2):
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=index != 0,
        )
        if session:
            assert prepared.ma_session_id == session
        session = prepared.ma_session_id

        async def no_recovery():
            pytest.fail("full source must not recover into a reduced backend")

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            external_user_id=request.external_user_id,
            user_message="Use the default skills and MCP.",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key=f"full:{index}",
        )
        assert outcome.state.error is None and not outcome.recovered
    assert len(posts) == 2
    assert posts[0]["environment"]["sources"] == native["base_environment"]["sources"]
    assert posts[1]["environment"]["environment_id"] == "workspace"
    assert "sources" not in posts[1]["environment"]
    assert posts[1]["previous_interaction_id"] == "turn-1"
    for body in posts:
        assert body["environment"]["network"] == {
            "allowlist": [
                {
                    "domain": "storage.googleapis.com",
                    "transform": {"Authorization": "Bearer offline-downscoped-token"},
                }
            ]
        }
        assert body["agent"] == native["id"]
        assert body["system_instruction"] == canonical.target_spec.system
        assert token in json.dumps(body["tools"])
        remote = next(tool for tool in body["tools"] if tool["type"] == "mcp_server")
        expected_mcp = (
            set(GATED_CATALOGUE) - confirmation_gated_tools()
            if gated
            else {"search_tools", "call_tool"}
        )
        assert set(remote["allowed_tools"][0]["tools"]) == expected_mcp
    assert token not in json.dumps(native) and token not in plan.model_dump_json()
    assert (
        "offline-storage-token" not in json.dumps(native)
        and "offline-storage-token" not in plan.model_dump_json()
    )
    assert all(
        b"offline-storage-token" not in path.read_bytes()
        and b"offline-downscoped-token" not in path.read_bytes()
        for path in plan.records_path.parent.glob("records.sqlite*")
    )


@pytest.mark.parametrize("consent_request", [GATED_PARAMS], indirect=True)
@pytest.mark.parametrize("full", [True], indirect=True)
async def test_gemini_gated_mcp_tool_loss_is_explicit_and_keeps_siblings(full):
    from daimon.core.turn.runtime_manifest import classify_source_obligations

    assert isinstance(full, tuple), "gated source lost the whole MCP representation"
    request, plan, canonical, native, _, posts, *_ = full
    target = canonical.target_spec
    # No Gemini turn has a confirmation card: every gated or publishing tool in
    # the catalogue is a declared loss, pinned row or template alike.
    gated = sorted(set(GATED_CATALOGUE) & confirmation_gated_tools())
    markers = tuple(f"mcp_tool:daimon-mcp:{name}" for name in gated)
    assert {"add_skill", "publish_report"} <= set(gated)
    assert target.declared_drops == (*markers, "memory", "sandbox")
    assert all(marker in target.system for marker in markers)
    assert not {"tools", "skills", "mcp"}.intersection(target.declared_drops)
    assert len(target.skills) == len(native["base_environment"]["sources"]) == 11
    allowed = set(GATED_CATALOGUE) - confirmation_gated_tools()
    assert set(target.mcp_servers[0].tool_policy["allowed_tools"]) == allowed
    remote = next(tool for tool in native["tools"] if tool["type"] == "mcp_server")
    assert remote["allowed_tools"] == [
        {"mode": "auto", "tools": list(target.mcp_servers[0].tool_policy["allowed_tools"])}
    ]
    assert not any(name in json.dumps(remote) for name in gated)
    # Catalogue verification still covers the complete original source. The
    # provider execution allowlist is a reduction with its own consent marker.
    assert set(plan.source_capabilities.mcp_tools) == set(GATED_CATALOGUE)
    source = await capture_preparation_source(request, fresh=True)
    missing = classify_source_obligations(
        source,
        target.model_copy(update={"declared_drops": ("memory", "sandbox")}),
        environment=source.representations[0].environment,
    )
    assert set(markers) <= set(missing)
    assert not posts


@pytest.mark.parametrize("consent_request", [GATED_PARAMS], indirect=True)
@pytest.mark.parametrize("full", [True], indirect=True)
async def test_gemini_gated_loss_does_not_waive_runtime_catalogue_verification(full):
    request, _, canonical, _, _, posts, *_ = full
    runtime = build_gemini_runtime(canonical)
    await runtime.read_agent_counterpart(canonical)
    # Seeing only the reduced execution allowlist is not sufficient authority
    # to resolve a runtime credential for the complete captured source.
    full[11]["names"] = list(canonical.target_spec.mcp_servers[0].tool_policy["allowed_tools"])
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    connection = canonical.target_spec.mcp_servers[0]
    with pytest.raises(ScopeViolation):
        await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
    assert not posts


@pytest.mark.parametrize("consent_request", [GATED_PARAMS], indirect=True)
@pytest.mark.parametrize("full", [True], indirect=True)
async def test_gemini_native_readback_cannot_reenable_a_declared_unavailable_tool(full):
    request, _, _, native, _, posts, *_ = full
    remote = next(tool for tool in native["tools"] if tool["type"] == "mcp_server")
    remote["allowed_tools"][0]["tools"].append("add_skill")
    with pytest.raises((AdmissionDenied, UnsupportedCapability)):
        await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("defect", ["archive", "source_bytes", "native", "permissions", "revoked"])
async def test_full_source_drift_and_credential_failures_never_post(full, defect):
    request, plan, _, native, _, posts, token, archives, *_ = full
    if defect == "archive":
        archives[0].write_bytes(archive({"SKILL.md": b"changed"}))
    elif defect == "source_bytes":
        router = full[9]
        body = archive({"SKILL.md": b"source bytes changed under the same version"})
        router.routes.insert(
            0,
            (
                "GET",
                re.compile(r"/v1/skills/source-cli-auth/versions/version-source-cli-auth/content"),
                lambda *_: httpx.Response(200, content=body),
            ),
        )
    elif defect == "native":
        native["tools"] = []
    elif defect == "permissions":
        plan.mcp_credential_file.chmod(0o644)
    else:
        async with request.deps.sessionmaker.begin() as db:
            await revoke_mcp_token(db, jti=token_jti(token), now=request.now())
    if defect in ("archive", "source_bytes", "native"):
        with pytest.raises((AdmissionDenied, UnsupportedCapability)):
            await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    else:
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=False,
        )

        async def no_recovery():
            pytest.fail("invalid auth must refuse")

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
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key="refusal",
        )
        assert outcome.state.error is not None
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_full_source_credential_cannot_resolve_before_native_verification(full):
    request, _, canonical, _, _, posts, token, *_ = full
    runtime = build_gemini_runtime(canonical)
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    connection = canonical.target_spec.mcp_servers[0]
    with pytest.raises(ScopeViolation, match="verified source binding"):
        await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("unusable", ["absent", "malformed"])
async def test_unrelated_manifest_failure_preserves_ordinary_source_capture(
    consent_request, monkeypatch, tmp_path, unusable
):
    request, _, source, *_ = consent_request
    path = tmp_path / "unusable.json"
    if unusable == "malformed":
        path.write_text("invalid JSON")
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    captured = await capture_preparation_source(request, fresh=True)
    assert captured == source
    assert source_preservation_pin(captured) == source_preservation_pin(source)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("surface", ["other_channel", "dm", "routine", "anthropic", "worker"])
async def test_full_mode_row_does_not_affect_unrelated_source_capture(full, monkeypatch, surface):
    request, _, _, _, _, posts, _, archives, *_ = full
    from mux.profiles import backend_selection

    # Even retained skill drift must not trigger representation I/O elsewhere.
    archives[0].write_bytes(b"not an archive")
    admission = request.admission
    if surface == "other_channel":
        old = admission.backend_revision
        other = old.channel.model_copy(update={"channel_id": "another-channel"})
        admission = replace(admission, backend_revision=ConfigRevision.create(other, 1, old))
    elif surface == "dm":
        admission = replace(admission, private_dm_id="private-dm")
    elif surface == "routine":
        admission = replace(admission, backend_revision=None, backend_revision_origin=None)
    elif surface == "anthropic":
        old = admission.backend_revision
        admission = replace(
            admission,
            backend_revision=ConfigRevision.create(old.channel, 3, backend_selection("anthropic")),
        )
    else:
        request = replace(request, worker=object())
    request = replace(request, admission=admission)
    captured = await capture_preparation_source(request, fresh=True)
    assert not captured.representations
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("defect", ["subset", "nonexistent", "extra"])
async def test_full_mode_mcp_catalogue_must_equal_actual_source_tools(full, defect):
    request, *_ = full
    path = Path(os.environ["DAIMON_TURN__PROVIDER_RUNTIME_FILE"])
    rows = json.loads(path.read_text())
    tools = rows[0]["source_capabilities"]["mcp_tools"]
    if defect == "subset":
        tools.pop()
    elif defect == "nonexistent":
        tools[0] = "get_agent"
    else:
        tools.append("unknown_tool")
    path.write_text(json.dumps(rows))
    captured = await capture_preparation_source(request, fresh=True)
    assert not captured.representations
    with pytest.raises((AdmissionDenied, UnsupportedCapability, AgentBackendRefused)):
        await prepare_source(request)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("phase", ["counterpart_read", "provider_exposure"])
@pytest.mark.parametrize("defect", ["bytes", "extra", "missing"])
async def test_live_gcs_drift_refuses_before_provider_send(full, defect, phase):
    from mux.errors import ProviderError

    request, _, canonical, _, _, posts, *_ = full
    runtime = build_gemini_runtime(canonical)
    if phase == "provider_exposure":
        await runtime.read_agent_counterpart(canonical)
    storage = full[10]
    path = next(iter(storage))
    if defect == "bytes":
        storage[path] = b"overwritten"
    elif defect == "missing":
        del storage[path]
    else:
        storage[path.rsplit("/", 1)[0] + "/extra.txt"] = b"unexpected"
    expected = AdmissionDenied if phase == "counterpart_read" else ProviderError
    with pytest.raises(expected):
        if phase == "counterpart_read":
            await read_preparation_counterpart(scope_channel_runtimes(request)[0])
        else:
            transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
            connection = canonical.target_spec.mcp_servers[0]
            await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_target_credential_must_see_the_complete_captured_mcp_catalogue(full):
    request, _, canonical, _, _, posts, *_ = full
    runtime = build_gemini_runtime(canonical)
    await runtime.read_agent_counterpart(canonical)
    full[11]["names"] = ["describe_agent"]
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    connection = canonical.target_spec.mcp_servers[0]
    with pytest.raises(ScopeViolation):
        await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_scoped_mcp_validator_retains_the_shorter_provisioning_deadline(full):
    from daimon.core.mcp_auth import validate_agent_mcp_token

    request, plan, _, _, _, _, token, *_ = full
    with pytest.raises(ValueError, match="invalid scoped agent token"):
        await validate_agent_mcp_token(
            request.deps.sessionmaker,
            token=token,
            secret=request.deps.mcp.jwt_secret.get_secret_value().encode(),
            scope=request.scope,
            agent_id=plan.source_agent_id,
            now=request.now(),
            max_ttl_s=30,
            chat_execution=True,
        )
    await validate_agent_mcp_token(
        request.deps.sessionmaker,
        token=token,
        secret=request.deps.mcp.jwt_secret.get_secret_value().encode(),
        scope=request.scope,
        agent_id=plan.source_agent_id,
        now=request.now(),
        max_ttl_s=600,
        chat_execution=True,
    )


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_readback_cannot_verify_a_receipt_for_an_unmounted_directory(full):
    request, plan, _, _, _, posts, *_ = full
    foreign = replace(
        plan.skill_deployments[0],
        source=plan.skill_deployments[0].source.replace("owned-offline", "unmounted-copy"),
    )
    wrong_plan = plan.model_copy(
        update={"skill_deployments": (foreign, *plan.skill_deployments[1:])}
    )
    path = Path(os.environ["DAIMON_TURN__PROVIDER_RUNTIME_FILE"])
    path.write_text(json.dumps([wrong_plan.model_dump(mode="json")]))
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    assert not any(call.url.host == "storage.googleapis.com" for call in full[12])
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("fault", ["version_unavailable", "provider_unavailable"])
async def test_optional_skill_proof_outage_does_not_break_native_public_preview(
    full, monkeypatch, fault
):
    from anthropic import APIConnectionError
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.defaults import ma_index

    request, _, canonical, _, _, posts, *_ = full

    async def unavailable(*args, **kwargs):
        if fault == "version_unavailable":
            raise ma_index.SkillVersionNotFoundError("fixture version unavailable")
        raise APIConnectionError(request=httpx.Request("GET", "https://offline.invalid/source"))

    monkeypatch.setattr(ma_index, "download_skill_version", unavailable)
    captured = await capture_preparation_source(request, fresh=True)
    assert not captured.representations
    service = AgentBackendService(request.deps.sessionmaker, enabled=True, capture_source=full[13])
    quote = await service.preview(canonical.source, "anthropic")
    assert quote.allowed and "tools" not in quote.declared_drops
    with pytest.raises(AgentBackendRefused, match="re confirm"):
        await prepare_source(request)
    assert not posts


@pytest.mark.parametrize("consent_request", [F2_PARAMS], indirect=True)
async def test_recorded_restricted_mcp_source_refuses_chat_catalogue_narrowing(full):
    assert {"tools", "skills", "mcp"}.intersection(full["quote"].declared_drops)
    assert not full["represented"].representations
    assert not any(call.method == "POST" for call in full["calls"])


@pytest.mark.parametrize(
    "consent_request",
    [
        {**PARAMS, "tools": PARAMS["tools"][:1]},
        {
            **PARAMS,
            "tools": [
                PARAMS["tools"][0],
                {
                    **PARAMS["tools"][1],
                    "default_config": {
                        "enabled": False,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
            ],
        },
    ],
    indirect=True,
    ids=["absent", "disabled"],
)
async def test_source_must_have_an_enabled_daimon_mcp_toolset(full):
    assert {"tools", "skills", "mcp"}.intersection(full["quote"].declared_drops)
    assert not full["represented"].representations
    assert not any(call.method == "POST" for call in full["calls"])


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_registered_chat_catalogue_matches_source_without_expanding_agent_keys(
    full, source_mcp
):
    from daimon.core.mcp_auth import (
        mint_agent_mcp_token,
        mint_chat_mcp_token,
        validate_agent_mcp_token,
    )
    from daimon.core.stores.mcp_tokens import list_live_tokens_by_label
    from daimon.testing.asgi import mcp_session

    request, plan, _, _, _, posts, runtime_token, *_ = full
    secret = request.deps.mcp.jwt_secret.get_secret_value().encode()
    now = request.now()
    async with request.deps.sessionmaker.begin() as db:
        external = await mint_agent_mcp_token(
            db,
            account_id=request.admission.account_id,
            tenant_id=request.tenant_id,
            agent_id=plan.source_agent_id,
            label="test-external",
            secret=secret,
            now=now,
        )
        ordinary, ordinary_jti, _ = await mint_chat_mcp_token(
            db,
            account_id=request.admission.account_id,
            agent_id=plan.source_agent_id,
            secret=secret,
            now=now,
        )
    try:
        visible = []
        for token in (ordinary, runtime_token, external):
            result = await mcp_session(source_mcp, token=token, method="tools/list")
            visible.append({tool["name"] for tool in result["result"]["tools"]})
        assert visible == [
            {"search_tools", "call_tool"},
            {"search_tools", "call_tool"},
            {"describe_agent", "list_my_sessions"},
        ]
        with pytest.raises(ValueError, match="invalid scoped agent token"):
            await validate_agent_mcp_token(
                request.deps.sessionmaker,
                token=external,
                secret=secret,
                scope=request.scope,
                agent_id=plan.source_agent_id,
                now=now,
                chat_execution=True,
            )
        async with request.deps.sessionmaker() as db:
            assert not await list_live_tokens_by_label(
                db, tenant_id=request.tenant_id, label="source-catalogue"
            )
    finally:
        async with request.deps.sessionmaker.begin() as db:
            await revoke_mcp_token(db, jti=token_jti(external), now=request.now())
            await revoke_mcp_token(db, jti=ordinary_jti, now=request.now())
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize(
    "defect", ["account", "tenant", "agent", "revoked", "extra_claim", "external_mode"]
)
async def test_registered_chat_runtime_refuses_foreign_revoked_or_expanded_authority(full, defect):
    import uuid

    import jwt
    from daimon.core.mcp_auth import validate_agent_mcp_token

    request, plan, _, _, _, posts, token, *_ = full
    scope, agent = request.scope, plan.source_agent_id
    secret = request.deps.mcp.jwt_secret.get_secret_value().encode()
    if defect in ("account", "tenant"):
        scope = scope.model_copy(update={f"{defect}_id": str(uuid.uuid4())})
    elif defect == "agent":
        agent = uuid.uuid4()
    elif defect == "revoked":
        async with request.deps.sessionmaker.begin() as db:
            await revoke_mcp_token(db, jti=token_jti(token), now=request.now())
    elif defect == "extra_claim":
        claims = jwt.decode(token, secret, algorithms=["HS256"])
        token = jwt.encode({**claims, "is_admin": True}, secret, algorithm="HS256")
    with pytest.raises(ValueError, match="invalid scoped agent token"):
        await validate_agent_mcp_token(
            request.deps.sessionmaker,
            token=token,
            secret=secret,
            scope=scope,
            agent_id=agent,
            now=request.now(),
            chat_execution=defect != "external_mode",
        )
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("fault", ["register", "revoke"])
async def test_optional_catalogue_registry_outage_preserves_native_preview(
    full, monkeypatch, fault
):
    from daimon.core import mcp_auth
    from daimon.core.stores import mcp_tokens
    from sqlalchemy.exc import SQLAlchemyError

    request, _, canonical, _, _, posts, *_ = full
    original_revoke = mcp_tokens.revoke_mcp_token

    async def unavailable(*args, **kwargs):
        raise SQLAlchemyError("fixture registry unavailable")

    try:
        with monkeypatch.context() as patch:
            if fault == "register":
                patch.setattr(mcp_auth, "mint_agent_mcp_token", unavailable)
            else:
                patch.setattr(mcp_tokens, "revoke_mcp_token", unavailable)
            captured = await capture_preparation_source(request, fresh=True)
            assert not captured.representations
            from daimon.core.channel_backend import AgentBackendService

            service = AgentBackendService(
                request.deps.sessionmaker, enabled=True, capture_source=full[13]
            )
            quote = await service.preview(canonical.source, "anthropic")
            assert quote.allowed and "tools" not in quote.declared_drops
            with pytest.raises(AgentBackendRefused, match="re confirm"):
                await prepare_source(request)
            assert not posts
    finally:
        # Simulate registry recovery and revoke every token owned by this test's
        # isolated tenant. No failed-cleanup credential was ever exported.
        async with request.deps.sessionmaker.begin() as db:
            pending = await mcp_tokens.list_live_tokens_by_label(
                db, tenant_id=request.tenant_id, label="source-catalogue"
            )
            for row in pending:
                await original_revoke(db, jti=row.jti, now=request.now())


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("defect", ["missing", "expired", "short", "unknown", "permissions"])
async def test_private_skill_mount_requires_fresh_bounded_storage_credential(full, defect):
    request, plan, canonical, _, _, posts, *_ = full
    if defect == "missing":
        plan.gcs_credential_file.unlink()
    elif defect == "permissions":
        plan.gcs_credential_file.chmod(0o644)
    else:
        value = json.loads(plan.gcs_credential_file.read_text())
        if defect == "expired":
            value["expires_at"] = (request.now() - timedelta(seconds=1)).isoformat()
        elif defect == "short":
            value["expires_at"] = (request.deadline - timedelta(seconds=1)).isoformat()
        else:
            value["unknown"] = True
        plan.gcs_credential_file.write_text(json.dumps(value))
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(scope_channel_runtimes(canonical)[0])
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_storage_mount_resolver_requires_verified_exact_counterpart_and_sources(full):
    request, plan, canonical, _, _, posts, *_ = full
    runtime = build_gemini_runtime(canonical)
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    sources = tuple(item.source for item in plan.skill_deployments)
    assert transport.gcs_secrets is not None
    with pytest.raises(ScopeViolation):
        await transport.gcs_secrets(request.scope, sources)
    await runtime.read_agent_counterpart(canonical)
    with pytest.raises(ScopeViolation):
        await transport.gcs_secrets(request.scope, ("gs://other/version/",))
    with pytest.raises(ScopeViolation):
        await transport.gcs_secrets(
            request.scope.model_copy(update={"account_id": "foreign"}), sources
        )
    assert not posts
