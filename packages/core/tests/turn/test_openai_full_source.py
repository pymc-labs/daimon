"""Public switch, retained catalogue and real OpenAI factory with the full default.

Only HTTP edges are scripted. All source, consent, credential, SDK and session
owners are production paths; no staging or native provider writes occur.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import httpx
import pytest
from daimon.core.channel_backend import AgentBackendService
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_deployment import OpenAIDeployment, build_openai_runtime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.prepare import admitted_session_scope, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import (
    capture_preparation_source,
    prepare_source,
    source_preservation_pin,
)
from daimon.core.turn.runtimes import build_channel_runtime
from daimon.core.turn.unattended import ALLOW_REVIEWED_NATIVE_UNATTENDED_TOOLS
from daimon.testing import f2_mcp_binding
from daimon.testing.db import sibling_nullpool_engine
from daimon.testing.f2_mcp_binding import AgentMCPAuthority, AgentMCPBinding
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, SkillRef
from mux.contracts.resources import SkillUpload, SkillUploadFile
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.deployment import configured_transport
from mux.drivers.openai.skills import bundle_digest

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_full_source import CUSTOM, F2_PARAMS, ROOT, archive
from .test_gemini_full_source import source_mcp as source_mcp
from .test_openai_host import Wire
from .test_openai_runtime_transport import clients_for

RECORDED_ENVIRONMENT = json.loads(
    (ROOT / "packages/core/tests/fixtures/anthropic_live_root.json").read_text()
)["environment"]
PARAMS = {
    **F2_PARAMS,
    "backend": "openai",
    "environment": RECORDED_ENVIRONMENT,
    "tools": [
        F2_PARAMS["tools"][0],
        {
            **F2_PARAMS["tools"][1],
            "configs": [
                {"name": name, "enabled": True, "permission_policy": {"type": "always_allow"}}
                for name in ("search_tools", "call_tool")
            ],
        },
    ],
}


class FullWire(Wire):
    def __init__(self, tenant):
        super().__init__(session_id=f"native-openai-session-{uuid4().hex}")
        self.tenant = tenant
        self.skills = {}
        self.agent = {}
        self.created = None
        self.posts = []
        self.vault = None
        self.vault_credential = None
        config = RECORDED_ENVIRONMENT["config"]
        self.template = {
            "id": "native-template",
            "name": "test-env",
            "created_at": 0,
            "network": {"access": "enabled", "allowed_domains": []},
            "packages": {
                name: config["packages"][native]
                for name, native in (("system", "apt"), ("python", "pip"), ("npm", "npm"))
            },
            "files": [],
        }

    def session(self):
        if self.created is None:
            return super().session()
        body = self.created
        return {
            "id": self.session_id,
            "agent": {"id": "native-agent", **body["agent"]},
            "environment": {"id": "native-environment", **body["environment"]},
            "metadata": body["metadata"],
            "status": "idle",
            "created_at": 0,
        }

    def handle(self, request):
        path = request.url.path.removeprefix("/v1")
        if path == "/vaults":
            self.requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"data": [], "has_more": False})
            self.vault = {"id": "caller-session-vault", **json.loads(request.content)}
            return httpx.Response(200, json=self.vault)
        if path == "/vaults/caller-session-vault":
            self.requests.append(request)
            return httpx.Response(200, json=self.vault)
        if path == "/vaults/caller-session-vault/credentials":
            self.requests.append(request)
            self.vault_credential = {
                "id": "caller-mcp-credential",
                "vault_id": "caller-session-vault",
                "created_at": 0,
                **json.loads(request.content),
            }
            return httpx.Response(200, json=self.vault_credential)
        if path.startswith("/skills"):
            self.requests.append(request)
            if request.method == "POST":
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: "
                    + request.headers["content-type"].encode()
                    + b"\r\nMIME-Version: 1.0\r\n\r\n"
                    + request.content
                )
                parts = list(message.iter_parts())
                assert len(parts) == 1
                identity = "skill_" + "x" * 42 + str(len(self.skills))
                self.skills[identity] = parts[0].get_payload(decode=True)
                return httpx.Response(
                    200,
                    json={
                        "id": identity,
                        "name": identity,
                        "latest_version": "1",
                        "created_at": 0,
                    },
                )
            identity = path.split("/")[2]
            if path.endswith("/content"):
                return httpx.Response(200, content=self.skills[identity])
            return httpx.Response(200, json={"skill_id": identity, "version": "1"})
        if path == "/agents/native-agent" or path == "/agents":
            self.requests.append(request)
            if request.method == "POST":
                self.agent = {**json.loads(request.content), "id": "native-agent", "created_at": 0}
            return httpx.Response(200, json=self.agent)
        if path == "/agents/environments/templates/native-template":
            self.requests.append(request)
            return httpx.Response(200, json=self.template)
        if path == "/agents/sessions" and request.method == "POST":
            self.requests.append(request)
            self.created = json.loads(request.content)
            self.posts.append(self.created)
            return httpx.Response(200, json=self.session())
        return super().handle(request)


@pytest.fixture
async def full_openai(consent_request, source_mcp, monkeypatch, tmp_path, request):
    gated = getattr(request, "param", False)
    native_defaults = gated == "native-defaults"
    request, _, source, actor, accepted, router, source_reads, *_ = consent_request
    sessions = request.deps.sessionmaker
    scope = admitted_session_scope(
        request.admission, tenant_id=request.tenant_id, session_id="preparation"
    )
    router.add_environment_list(request.admission.environment)
    request = replace(
        request,
        scope=scope,
        admission=replace(
            request.admission,
            origin_channel_id=request.admission.backend_revision.channel.channel_id,
        ),
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        deps=replace(
            request.deps,
            deployment_default=DeploymentDefault(
                agent_name=source.name, environment_name="test-env"
            ),
            state_store=PostgresStateStore(sessions),
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
        ),
    )
    environment = request.admission.environment.model_copy(
        update={
            "metadata": {
                **request.admission.environment.metadata,
                "daimon_managed": "true",
                "daimon_spec_hash": "a" * 64,
            },
            "description": "Default hosted execution environment",
        }
    )
    request = replace(request, admission=replace(request.admission, environment=environment))
    router.add_environment(environment)
    router.routes.insert(0, router.routes.pop())
    router.add_environment_list(environment)
    router.routes.insert(0, router.routes.pop())
    source = await capture_preparation_source(request, fresh=True)
    assert source.environment.spec.scope == "organization"
    assert source.environment.spec.metadata["daimon_managed"] == "true"
    assert source.environment.spec.description == environment.description
    wire = FullWire(scope.tenant_id)
    clients = clients_for(monkeypatch, wire.handle)
    mcp_calls = []
    names = ["search_tools", "call_tool", *(["add_skill"] if gated else [])]
    if native_defaults:
        names = [
            "add_skill",
            "call_tool",
            "create_attachment_upload_url",
            "create_notebook_upload_url",
            "publish_report",
            "search_tools",
        ]
    catalogue_state = {"names": names if gated else None}

    async def mcp(call):
        mcp_calls.append(call)
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

    original = httpx.AsyncClient

    class RouteClient(original):
        def __init__(self, **kwargs):
            if "transport" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(mcp)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RouteClient)
    backend = OpenAIDriver(
        configured_transport(api_key="offline", project="project"),
        account_scope_id="project",
        authorization=lambda actual, *_: actual == scope,
        journal=OpenAIRecoveryJournal(),
        usage_revisions=OpenAIUsageRevisions(),
    )
    mappings, archives = [], []
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
        data = archive(files)
        retained = tmp_path / f"{name}.zip"
        retained.write_bytes(data)
        archives.append(retained)
        identity = f"source-{name}"
        router.add(
            "GET",
            rf"/v1/skills/{identity}/versions",
            lambda *_, sid=identity: httpx.Response(
                200, json={"data": [{"id": f"version-{sid}", "version": "1"}], "has_more": False}
            ),
        )
        router.add(
            "GET",
            rf"/v1/skills/{identity}/versions/version-{identity}/content",
            lambda *_, body=data: httpx.Response(200, content=body),
        )
        native = await backend.skills.create(scope, bundle, key=name)
        mappings.append(
            {
                "source": {"id": identity, "source": "custom", "version": "1"},
                "target": SkillRef(
                    id=native.id, version="1", digest=bundle_digest(bundle)
                ).model_dump(mode="json"),
                "archive": str(retained),
            }
        )
    capabilities = {
        "source_pin": source_preservation_pin(source),
        "model": ModelRef(provider="openai", id="gpt-6-luna").model_dump(mode="json"),
        "skills": mappings,
        "mcp_tools": names,
        "environment": source.environment.spec.model_dump(mode="json"),
    }
    manifest = tmp_path / "runtime.json"
    row = {
        "tenant_id": scope.tenant_id,
        "account_id": scope.account_id,
        "profile": "openai.persistent_workspace",
        "platform": request.platform,
        "channel_id": request.admission.origin_channel_id,
        "source_agent_id": str(source.agent_id),
        "source_agent_version": source.source.revision.local,
        "source_capabilities": capabilities,
    }
    manifest.write_text(json.dumps([row]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))

    async def capture(actual):
        from daimon.core.authz import Place
        from daimon.core.scope import ScopeContext
        from daimon.core.turn.admission import capture_backend_source

        return await capture_backend_source(
            request.deps,
            actual,
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

    service = AgentBackendService(sessions, enabled=True, capture_source=capture)
    quote = await service.preview(source, "openai")
    expected_drops = ("mcp_tool:daimon-mcp:add_skill", "memory") if gated else ("memory",)
    if native_defaults:
        # A pinned row has no confirmation card either: every gated or publishing
        # tool is a declared loss, exactly as in a template.
        expected_drops = (
            "builtin_tool:web_fetch",
            "mcp_tool:daimon-mcp:add_skill",
            "mcp_tool:daimon-mcp:create_attachment_upload_url",
            "mcp_tool:daimon-mcp:create_notebook_upload_url",
            "mcp_tool:daimon-mcp:publish_report",
            "memory",
        )
    assert quote.allowed and quote.declared_drops == expected_drops
    if native_defaults:
        from daimon.core.channel_backend import AgentBackendRefused

        with pytest.raises(AgentBackendRefused) as missing_consent:
            await service.set_backend(
                source,
                "openai",
                accepted.local,
                actor,
                accepted_drops=tuple(
                    drop for drop in quote.declared_drops if drop != "builtin_tool:web_fetch"
                ),
                expected_preview_digest=quote.digest,
            )
        assert missing_consent.value.reason == "drops_mismatch"
        assert wire.posts == []
    accepted = await service.set_backend(
        source,
        "openai",
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
        backend, sessions, scope=scope, account_scope_id="project", operation_key="full-publication"
    )
    counterpart = await host.publish(
        "full-counterpart", canonical.target_spec, expected_revision=0, key="full-publication"
    )
    signer = tmp_path / "signer"
    signer.write_text(request.deps.mcp.jwt_secret.get_secret_value())
    signer.chmod(0o600)
    output = tmp_path / "after-ack"
    output.mkdir(mode=0o700)
    # The binding disposes its engine on close; give it a sibling, not the shared pool.
    monkeypatch.setattr(
        f2_mcp_binding,
        "create_async_engine",
        lambda *_, **__: sibling_nullpool_engine(sessions.kw["bind"]),
    )
    binding = AgentMCPBinding(
        authority=AgentMCPAuthority(
            provider="openai",
            scope=scope,
            account_scope_id="project",
            output=output,
            deadline_s=600,
            database_url=os.environ["DAIMON_DATABASE__TEST_URL"],
            mcp_signing_secret_file=signer,
            canonical_source=source.source.ref.model_copy(update={"account_id": None}),
            chat_execution=True,
            acknowledged=lambda actual: actual == counterpart.agent.ref and bool(wire.agent),
            redact=lambda _: None,
        )
    )
    token = await binding.bind(counterpart.agent.ref)
    from daimon.core.mcp_auth import token_jti
    from daimon.core.stores.mcp_tokens import get_mcp_token

    async with sessions() as db:
        registered = await get_mcp_token(db, jti=token_jti(token))
    assert registered is not None and registered.surface == "provider_turn"
    credential = tmp_path / "caller.token"
    credential.write_text(token)
    credential.chmod(0o600)
    plan = OpenAIDeployment(
        **row,
        config_digest=revision.digest,
        project="project",
        agent_id=counterpart.agent.ref.id,
        environment_id="native-template",
        api_key_env="OFFLINE_FULL_OPENAI_KEY",
        skill_ids=tuple(item["target"]["id"] for item in mappings),
        counterpart_catalog_id=counterpart.catalog_id,
        counterpart_catalog_revision=counterpart.local_revision,
        mcp_credential_file=credential,
    )
    manifest.write_text(json.dumps([plan.model_dump(mode="json")]))
    monkeypatch.setenv(plan.api_key_env, "offline")
    wire.requests.clear()
    try:
        yield (
            request,
            plan,
            canonical,
            wire,
            token,
            archives,
            catalogue_state,
            mcp_calls,
            binding,
            router,
            manifest,
        )
    finally:
        await binding.close()
    assert all(client.is_closed() for client in clients)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_full_default_public_switch_real_factory_and_authenticated_session(
    full_openai, source_mcp
):
    request, plan, canonical, wire, token, *_ = full_openai
    assert canonical.target_spec.declared_drops == ("memory",)
    assert len(canonical.target_spec.skills) == 11
    assert tuple(tool.name for tool in canonical.target_spec.tools) == ("bash",)
    assert any(len(body) > 2_000_000 for body in wire.skills.values())
    runtime = build_openai_runtime(canonical)
    assert runtime.mcp_secrets is not None
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
        assert prepared.session_ref.provider == "openai"

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
    assert len(wire.posts) == 1
    body = wire.posts[0]
    assert len(body["environment"]["skills"]) == 11
    mcp = body["agent"]["tools"][0]
    import jwt

    assert "authorization" not in mcp["transport"]
    assert body["vault_ids"] == ["caller-session-vault"]
    assert mcp["credential_id"] == "caller-mcp-credential"
    installed = wire.vault_credential["auth"]["token"]
    assert installed not in json.dumps(body) and token not in json.dumps(body)
    claims = jwt.decode(installed, options={"verify_signature": False})
    original = jwt.decode(token, options={"verify_signature": False})
    assert claims.pop("chat_session_id") == prepared.native_chat_session_id
    assert claims == original, "installed derivative retains source expiry and revocation"
    assert mcp["allowed_tools"] == ["search_tools", "call_tool"]
    from daimon.testing.asgi import mcp_session

    for name in ("describe_agent", "list_my_sessions"):
        result = await mcp_session(
            source_mcp,
            token=token,
            method="tools/call",
            params={
                "name": "call_tool",
                "arguments": {"name": name, "arguments": {}},
            },
        )
        assert not result["result"].get("isError", False)
    # The pinned row's credential has no confirmation card: the server refuses
    # every gated or publishing tool for it by name (the proxy refuses them anyway).
    for name in ("publish_report", "create_attachment_upload_url", "add_skill"):
        refused = await mcp_session(
            source_mcp,
            token=token,
            method="tools/call",
            params={"name": name, "arguments": {}},
        )
        assert "confirmation card" in json.dumps(refused), name
    assert "Bearer" not in json.dumps(wire.agent)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize(
    "defect",
    [
        "archive",
        "native_skill",
        "native_tools",
        "permissions",
        "revoked",
        "catalogue",
        "environment",
        "native_environment",
    ],
)
async def test_full_source_drift_refuses_before_session_post(full_openai, defect):
    from daimon.core.mcp_auth import token_jti
    from daimon.core.stores.mcp_tokens import revoke_mcp_token
    from daimon.core.turn.errors import AdmissionDenied
    from mux.errors import ProviderError, UnsupportedCapability

    request, plan, _, wire, token, archives, catalogue_state, _, _, _, manifest = full_openai
    if defect == "archive":
        archives[0].write_bytes(archive({"SKILL.md": b"changed source archive"}))
    elif defect == "native_skill":
        wire.skills[next(iter(wire.skills))] = archive({"SKILL.md": b"changed native bytes"})
    elif defect == "native_tools":
        wire.agent["tools"] = []
    elif defect == "permissions":
        plan.mcp_credential_file.chmod(0o644)
    elif defect == "revoked":
        async with request.deps.sessionmaker.begin() as db:
            await revoke_mcp_token(db, jti=token_jti(token), now=request.now())
    elif defect == "catalogue":
        catalogue_state["names"] = ["search_tools", "call_tool", "additional_authority"]
    elif defect == "native_environment":
        wire.template["network"]["access"] = "disabled"
    else:
        # A different complete operator policy must not become source evidence.
        rows = json.loads(manifest.read_text())
        rows[0]["source_capabilities"]["environment"]["network"]["mode"] = "none"
        manifest.write_text(json.dumps(rows))
    with pytest.raises((AdmissionDenied, UnsupportedCapability, ProviderError)):
        await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=False,
        )
    assert wire.posts == []
    assert "Bearer" not in json.dumps(wire.agent)


ASK_PARAMS = {
    **PARAMS,
    "tools": [
        PARAMS["tools"][0],
        {
            **PARAMS["tools"][1],
            "configs": [
                *PARAMS["tools"][1]["configs"],
                {"name": "add_skill", "enabled": True, "permission_policy": {"type": "always_ask"}},
            ],
        },
    ],
}


@pytest.mark.parametrize("consent_request", [ASK_PARAMS], indirect=True)
@pytest.mark.parametrize("full_openai", [True], indirect=True)
async def test_confirmation_gated_mcp_tool_is_declared_without_losing_siblings(full_openai):
    request, _, canonical, wire, *_ = full_openai
    from daimon.core.turn.runtime_manifest import classify_source_obligations

    target = canonical.target_spec
    assert target.declared_drops == ("mcp_tool:daimon-mcp:add_skill", "memory")
    assert "mcp_tool:daimon-mcp:add_skill" in target.system
    assert len(target.skills) == 11
    assert target.mcp_servers[0].tool_policy["allowed_tools"] == ["search_tools", "call_tool"]
    assert wire.agent["tools"][-1]["allowed_tools"] == ["search_tools", "call_tool"]
    # Exact captured evidence alone cannot waive this loss: explicit consent
    # is mandatory even when all siblings are represented.
    source = await capture_preparation_source(request, fresh=True)
    missing = classify_source_obligations(
        source,
        target.model_copy(update={"declared_drops": ("memory",)}),
        environment=source.representations[0].environment,
    )
    assert "mcp_tool:daimon-mcp:add_skill" in missing
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
    assert prepared.session_ref.provider == "openai"
    assert len(wire.posts) == 1
    assert wire.posts[0]["agent"]["tools"][0]["allowed_tools"] == ["search_tools", "call_tool"]


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("policy", ["disabled", "ask"])
async def test_no_executable_mcp_tools_never_publishes_empty_allowlist(
    full_openai, monkeypatch, policy
):
    request, _, _, wire, _, _, _, _, _, _, manifest = full_openai
    from anthropic.types.beta import BetaManagedAgentsAgent

    payload = request.admission.agent.model_dump(mode="json")
    mcp = next(tool for tool in payload["tools"] if tool["type"] == "mcp_toolset")
    mcp["default_config"] = {
        "enabled": policy != "disabled",
        "permission_policy": {"type": "always_ask" if policy == "ask" else "always_allow"},
    }
    mcp["configs"] = []
    request = replace(
        request,
        source=None,
        admission=replace(request.admission, agent=BetaManagedAgentsAgent.model_validate(payload)),
    )
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    source = await capture_preparation_source(request)
    rows = json.loads(manifest.read_text())
    rows[0]["source_capabilities"]["source_pin"] = source_preservation_pin(source)
    manifest.write_text(json.dumps(rows))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    represented = await capture_preparation_source(request, capability_preview=True)
    assert represented.representations == ()
    assert wire.posts == []


NATIVE_DEFAULT_PARAMS = {
    **ASK_PARAMS,
    "tools": [
        {
            **ASK_PARAMS["tools"][0],
            "configs": [
                config
                for config in ASK_PARAMS["tools"][0]["configs"]
                if config["name"] not in ("web_fetch", "web_search")
            ],
            "default_config": {
                "enabled": True,
                "permission_policy": {"type": "always_allow"},
            },
        },
        {
            **ASK_PARAMS["tools"][1],
            "configs": [
                {"name": "add_skill", "enabled": True, "permission_policy": {"type": "always_ask"}}
            ],
            "default_config": {
                "enabled": True,
                "permission_policy": {"type": "always_allow"},
            },
        },
    ],
}


@pytest.mark.parametrize("consent_request", [NATIVE_DEFAULT_PARAMS], indirect=True)
@pytest.mark.parametrize("full_openai", ["native-defaults"], indirect=True)
async def test_live_native_defaults_drop_gated_tools_and_preserve_siblings(full_openai):
    """A pinned row with the full live catalogue still admits and runs: its skills,
    builtins and non-gated MCP tools stay executable; gated/publishing tools are losses."""
    request, _, canonical, wire, *_ = full_openai
    target = canonical.target_spec
    assert {tool.name for tool in target.tools} == {"bash", "web_search"}
    assert len(target.skills) == 11
    assert target.declared_drops == (
        "builtin_tool:web_fetch",
        "mcp_tool:daimon-mcp:add_skill",
        "mcp_tool:daimon-mcp:create_attachment_upload_url",
        "mcp_tool:daimon-mcp:create_notebook_upload_url",
        "mcp_tool:daimon-mcp:publish_report",
        "memory",
    )
    assert "builtin_tool:web_fetch" in target.system
    expected = ["call_tool", "search_tools"]
    assert target.mcp_servers[0].tool_policy["allowed_tools"] == expected
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
    assert prepared.session_ref.provider == "openai"
    tools = wire.posts[0]["agent"]["tools"]
    assert any(tool["type"] == "web_search" for tool in tools)
    assert next(tool for tool in tools if tool["type"] == "mcp")["allowed_tools"] == expected


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_native_creation_grant_human_reuse_unattended_and_child_handoffs(full_openai):
    """Capture the real creation bearer, then use it through registered MCP in the driver."""
    import jwt
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
    from daimon.adapters.mcp.server import create_mcp_app
    from daimon.core._models import Account, Tenant
    from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
    from daimon.core.stores.domain import Role
    from daimon.core.stores.task_continuations import list_pending_continuations
    from daimon.core.stores.turn_origins import create_origin, delete_origin, get_active_origin
    from daimon.core.turn_origin import execution_origin, turn_origin
    from daimon.testing.asgi import call_mcp_tool
    from daimon.testing.factories import make_platform_principal
    from daimon.testing.ma_models import ma_agent
    from mux.testing.drivers.anthropic.ma import list_response
    from pydantic import SecretStr

    request, _, _, wire, source_token, *rest = full_openai
    router = rest[4]
    sessions = request.deps.sessionmaker
    account_id = request.session_account_id
    async with sessions.begin() as db:
        tenant = await db.get(Tenant, request.tenant_id)
        tenant.platform = request.platform
        await make_platform_principal(
            db,
            tenant=tenant,
            account=await db.get(Account, account_id),
            platform=request.platform,
            external_id="caller",
        )
    target = ma_agent(id="handoff-target", name="daimon", tenant_id=request.tenant_id)
    # This test exercises signed-origin authorization, so the destination
    # shares the source provider. Cross-provider handoffs retain their own fence.
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_backends

    async with sessions.begin() as db:
        await agent_backends.put_revision(
            db,
            tenant_id=request.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=target.id),
            local=1,
            backend="openai",
            digest="d" * 64,
            previous_local=None,
            agent_name=target.name,
            preview_digest="p" * 64,
        )
    router.add(
        "GET",
        r"/v1/agents",
        lambda *_: list_response(
            [request.admission.agent.model_dump(mode="json"), target.model_dump(mode="json")]
        ),
    )
    router.routes.insert(0, router.routes.pop())
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url="postgresql+asyncpg://offline/db"),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            mcp=request.deps.mcp,
        ),
        sessionmaker=sessions,
        anthropic=request.deps.anthropic,
        auth=DaimonJWTVerifier(
            secret=request.deps.mcp.jwt_secret.get_secret_value().encode(), sessionmaker=sessions
        ),
    )
    previous_grant = None
    for index, kind in enumerate(("human", "human", "timer", "worker")):
        worker = kind == "worker"
        if worker:
            wire.session_id = "source-child-session"
            wire.turns.clear()
            wire.items.clear()
        thread_id = "child-thread" if worker else request.thread_id
        async with turn_origin(
            sessions,
            tenant_id=request.tenant_id,
            account_id=account_id,
            platform=request.platform,
            parent_channel_id="channel",
            thread_id=thread_id,
            responder_ma_agent_id=request.admission.agent.id,
            responder_name="test-agent",
            role=Role.USER,
            attended=True,
            sender_foreign=False,
            shared_channel=False,
        ) as origin:
            # Simulate an inherited parent execution during child construction.
            # Construction must install a fresh slot grant, never this origin's.
            parent = None
            if worker:
                async with sessions.begin() as db:
                    parent = await create_origin(
                        db,
                        tenant_id=request.tenant_id,
                        account_id=account_id,
                        platform=request.platform,
                        parent_channel_id="channel",
                        thread_id=request.thread_id,
                        responder_ma_agent_id=request.admission.agent.id,
                        responder_name="test-agent",
                        configuration_target_ma_agent_id=None,
                        configuration_target_name=None,
                        role=Role.USER,
                        now=datetime.now(UTC),
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                        attended=True,
                        chat_session_id=previous_grant,
                        sender_foreign=False,
                        shared_channel=False,
                    )
            with execution_origin(parent.id if parent is not None else None):
                prepared = await bind_session_impl(
                    request.deps,
                    request.admission,
                    tenant_id=request.tenant_id,
                    platform=request.platform,
                    external_user_id="caller",
                    thread_id=thread_id,
                    session_account_id=account_id,
                    reuse_existing=index in (1, 2),
                )
            mcp = wire.posts[-1]["agent"]["tools"][0]
            assert "authorization" not in mcp["transport"]
            installed = wire.vault_credential["auth"]["token"]
            assert installed not in json.dumps(wire.posts[-1])
            grant = jwt.decode(installed, options={"verify_signature": False})["chat_session_id"]
            assert grant == prepared.native_chat_session_id and grant != str(origin.id)
            if index in (1, 2):
                assert grant == previous_grant and prepared.reused
            elif worker:
                assert grant != previous_grant
            previous_grant = grant

            async def live_origin():
                async with sessions() as db:
                    return await get_active_origin(
                        db,
                        origin_id=origin.id,
                        tenant_id=request.tenant_id,
                        account_id=account_id,
                        platform=request.platform,
                        now=datetime.now(UTC),
                    )

            assert (await live_origin()).chat_session_id is None, (
                "preparation alone grants no origin authority"
            )

            async def handoff(installed=installed, index=index):
                return await call_mcp_tool(
                    app,
                    token=installed,
                    name="call_tool",
                    arguments={
                        "name": "hand_off_task",
                        "arguments": {
                            "origin_context_id": str(origin.id),
                            "agent_id": target.id,
                            "continuation": f"finish requested chart {index}",
                        },
                    },
                )

            async def before_send(
                worker=worker, grant=grant, thread_id=thread_id, index=index, kind=kind
            ):
                bound = await live_origin()
                assert bound.chat_session_id == (None if worker else grant)
                result = await handoff()
                assert bool(result.get("result", result).get("isError", False)) is worker, result
                async with sessions() as db:
                    rows = await list_pending_continuations(
                        db,
                        tenant_id=request.tenant_id,
                        platform=request.platform,
                        thread_id=thread_id,
                    )
                assert len(rows) == (0 if worker else index + 1), result
                if rows:
                    assert rows[-1].attended is (kind == "human")

            async def no_recovery():
                pytest.fail("native session must not recover into another backend")

            # Main's signed-grant assertions above still run at before_send.
            # Preview's default refuses this source's automatic sandbox tools
            # before a provider message, including timer and child execution.
            refused = kind != "human" and not ALLOW_REVIEWED_NATIVE_UNATTENDED_TOOLS
            sent_before = [
                call
                for call in wire.requests
                if call.method == "POST" and call.url.path.endswith("/events")
            ]
            with (
                pytest.raises(AdmissionDenied, match="backend_unsupported")
                if refused
                else nullcontext()
            ):
                outcome = await run_prepared_turn(
                    request.deps,
                    prepared,
                    tenant_id=request.tenant_id,
                    platform=request.platform,
                    thread_id=thread_id,
                    external_user_id="caller",
                    user_message="do requested work",
                    lifecycle=RecordingLifecycle(),
                    cancel=asyncio.Event(),
                    reseed_user_message=no_recovery,
                    recovery_lifecycle=lambda _: RecordingLifecycle(),
                    before_send=before_send,
                    attended=kind == "human",
                    origin="worker" if worker else "chat",
                    operation_key=f"origin-lifecycle:{index}",
                )
                assert outcome.state.error is None, (index, kind, outcome.state.error)
            if refused:
                assert [
                    call
                    for call in wire.requests
                    if call.method == "POST" and call.url.path.endswith("/events")
                ] == sent_before
            assert (await live_origin()).chat_session_id is None
            assert "isError" in str(await handoff()), (
                "an ended execution cannot reuse its installed bearer"
            )
            if parent is not None:
                async with sessions.begin() as db:
                    retained = await get_active_origin(
                        db,
                        origin_id=parent.id,
                        tenant_id=request.tenant_id,
                        account_id=account_id,
                        platform=request.platform,
                        now=datetime.now(UTC),
                    )
                    assert retained.attended and retained.chat_session_id == parent.chat_session_id
                    await delete_origin(db, origin_id=parent.id)

    assert len(wire.posts) == 2, (
        "human reuse retains one provider session; child gets a separate one"
    )
    assert (
        jwt.decode(source_token, options={"verify_signature": False}).get("chat_session_id") is None
    )


@pytest.mark.parametrize("consent_request", [NATIVE_DEFAULT_PARAMS], indirect=True)
@pytest.mark.parametrize("full_openai", ["native-defaults"], indirect=True)
async def test_live_native_defaults_declare_only_fetch_loss_and_preserve_siblings(full_openai):
    request, _, canonical, wire, *_ = full_openai
    target = canonical.target_spec
    assert {tool.name for tool in target.tools} == {"bash", "web_search"}
    assert len(target.skills) == 11
    assert target.declared_drops == (
        "builtin_tool:web_fetch",
        "mcp_tool:daimon-mcp:add_skill",
        "mcp_tool:daimon-mcp:create_attachment_upload_url",
        "mcp_tool:daimon-mcp:create_notebook_upload_url",
        "mcp_tool:daimon-mcp:publish_report",
        "memory",
    )
    assert "builtin_tool:web_fetch" in target.system
    expected = ["call_tool", "search_tools"]
    assert target.mcp_servers[0].tool_policy["allowed_tools"] == expected
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
    assert prepared.session_ref.provider == "openai"
    tools = wire.posts[0]["agent"]["tools"]
    assert any(tool["type"] == "web_search" for tool in tools)
    assert next(tool for tool in tools if tool["type"] == "mcp")["allowed_tools"] == expected
