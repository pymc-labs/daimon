"""Public switch, retained catalogue and real OpenAI factory with the full default.

Only HTTP edges are scripted. All source, consent, credential, SDK and session
owners are production paths; no staging or native provider writes occur.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from daimon.testing import f2_mcp_binding
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
from .test_gemini_full_source import DEFAULT, F2_PARAMS, ROOT, archive
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
async def full_openai(consent_request, source_mcp, monkeypatch, tmp_path):
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
    names = ["search_tools", "call_tool"]
    catalogue_state = {"names": None}

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
    for item in DEFAULT["skills"]:
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
    assert quote.allowed and quote.declared_drops == ("memory",)
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
    monkeypatch.setattr(f2_mcp_binding, "create_async_engine", lambda *_, **__: sessions.kw["bind"])
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
    assert mcp["transport"]["authorization"] == "Bearer " + token
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
