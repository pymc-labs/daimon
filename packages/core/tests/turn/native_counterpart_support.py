"""Shared owned counterpart fixtures; native provider imports are selected lazily."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import httpx
import pytest
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.testing import pytest_plugin as database_plugin
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.isolated_sdk import SDK_ENV_VARS, build_async_openai, build_gemini_client
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.resources import Agent, AgentSpec, SkillUpload, SkillUploadFile
from mux.state.memory import MemoryStateStore

from .test_openai_deployment import application as application
from .test_provider_dispatch import ACCOUNT, REVISION, SCOPE, TENANT

if TYPE_CHECKING:
    from daimon.core.turn.gemini_deployment import GeminiPlan
    from daimon.core.turn.openai_deployment import OpenAIDeployment

SOURCE_ID = derive_agent_uuid(tenant_id=TENANT, ma_agent_id="host-metadata-not-a-provider-agent")


PINS = {
    "source_agent_id": str(SOURCE_ID),
    "source_agent_version": 1,
    "counterpart_catalog_id": "owned-counterpart",
    "counterpart_catalog_revision": 1,
}


@pytest.fixture(params=["openai", "gemini"])
def provider(request: pytest.FixtureRequest) -> Literal["openai", "gemini"]:
    return request.param


def deployment(provider: str = "openai", tmp_path: Path = Path("/offline")) -> dict[str, Any]:
    profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
    model = "gpt-6-luna" if provider == "openai" else "gemini-3.8-flash"
    row = {
        "profile": profile,
        "tenant_id": SCOPE.tenant_id,
        "platform": "slack",
        "channel_id": "channel",
        "account_id": SCOPE.account_id,
        "configuration_grant": {
            "profiles": [profile],
            "models": [model],
            "thread_modes": ["per_caller"],
            "requires": {},
        },
        "project": "project",
        "agent_id": "owned-agent",
        "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
    }
    if provider == "openai":
        row["environment_id"] = "owned-environment"
    else:
        row.update(
            records_path=str(tmp_path / "records.sqlite"),
            holds_path=str(tmp_path / "holds.sqlite"),
            hold_usd="0.05",
            cap_usd="0.25",
            agent_name="unrelated-display-label",
            system="Retained instructions",
            tools=[],
            environment={"name": "inline"},
        )
    return row


def plan_type(provider: str) -> type[OpenAIDeployment] | type[GeminiPlan]:
    if provider == "openai":
        from daimon.core.turn.openai_deployment import OpenAIDeployment

        return OpenAIDeployment
    from daimon.core.turn.gemini_deployment import GeminiPlan

    return GeminiPlan


class Wire:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.requests: list[httpx.Request] = []
        self.agent: dict[str, Any] = {
            "id": "owned-agent",
            "name": "unrelated-display-label",
            "model": "gpt-6-luna",
            "instructions": "Retained instructions",
            "tools": [],
            "metadata": {"mux_tenant": str(TENANT)},
            "created_at": 1791792000,
        }
        self.environment: dict[str, Any] = {
            "id": "owned-environment",
            "name": "hosted",
            "files": [],
            "network": {"access": "disabled"},
            "created_at": 1791792000,
        }
        self.error: tuple[str, int] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.provider == "gemini":
            assert request.headers["Api-Revision"] == "2026-05-20"
            if request.method == "POST":
                assert request.url.path == "/v1beta/agents"
                self.agent = json.loads(request.content)
                self.native_id = self.agent["id"]
                return httpx.Response(200, json=self.agent)
            assert request.method == "GET"
            assert request.url.path == "/v1beta/agents/" + self.agent_id
            if self.error is not None:
                return httpx.Response(
                    self.error[1], json={"error": {"message": "private provider text"}}
                )
            return httpx.Response(200, json=self.agent)
        assert request.method == "GET", "counterpart readback cannot mutate native state"
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        path = request.url.path
        assert path in (
            "/v1/agents/owned-agent",
            "/v1/agents/environments/templates/owned-environment",
        )
        if self.error is not None and self.error[0] == path:
            return httpx.Response(
                self.error[1], json={"error": {"message": "private provider text"}}
            )
        raw = self.agent if path == "/v1/agents/owned-agent" else self.environment
        return httpx.Response(200, json=raw)

    @property
    def agent_id(self) -> str:
        return self.native_id


class Owned(NamedTuple):
    request: ProviderPreparationRequest
    source: BackendAgent
    plan: OpenAIDeployment | GeminiPlan
    resources: NeutralResourceHost
    retained: AgentRevision
    wire: Wire
    manifest_path: Path


db_preflight = database_plugin.db_preflight


@pytest.fixture(scope="session", autouse=True)
def owned_database_preflight(db_preflight: DatabasePreflight) -> None:
    """Admit every engine/bind before fixture construction can connect."""


@pytest.fixture(autouse=True)
def clean_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SDK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def owned(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> AsyncIterator[Owned]:
    require_owned_sessionmaker(application.deps.sessionmaker)
    async with application.deps.sessionmaker.begin() as db:
        tenant = await make_tenant(db, id=TENANT)
        await make_account(db, id=ACCOUNT, tenant=tenant)
    selected = replace(application.admission, backend_revision_origin="agent")
    assert selected.agent.version == 1
    request = replace(application, admission=selected)
    if provider == "gemini":
        config = ConfigRevision.create(
            REVISION.channel,
            1,
            resolve_default(
                BackendConfig(
                    backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
                )
            ),
        )
        request = replace(
            request,
            admission=replace(request.admission, backend_revision=config),
            deps=replace(request.deps, state_store=MemoryStateStore()),
        )
    source = BackendAgent(
        tenant_id=TENANT,
        agent_id=SOURCE_ID,
        name="source-display-name",
        configuration_digest="trusted-owner-source-projection",
    )
    wire = Wire(provider)
    async with AsyncExitStack() as stack:
        if provider == "openai":
            from mux.drivers.openai import OpenAIDriver
            from mux.drivers.openai.transport import SDKTransport
            from mux.drivers.openai.turn import MemoryRecoveryJournal
            from mux.drivers.openai.usage import MemoryUsageRevisions

            sdk = await stack.enter_async_context(
                build_async_openai(
                    transport=httpx.MockTransport(wire.handle),
                    api_key="offline",
                    base_url="https://openai.invalid/v1",
                )
            )
            backend = OpenAIDriver(
                SDKTransport(sdk),
                account_scope_id="project",
                journal=MemoryRecoveryJournal(),
                usage_revisions=MemoryUsageRevisions(),
                authorization=lambda scope, kind, id_: (
                    scope == SCOPE
                    and (kind, id_)
                    in {("agent", "owned-agent"), ("environment", "owned-environment")}
                ),
            )
        else:
            from daimon.testing.drivers.gemini.fake import MemoryStorage
            from mux.drivers.gemini import GeminiManagedAgents
            from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
            from mux.drivers.gemini.transport import SDKTransport as GeminiSDKTransport

            client = build_gemini_client(
                transport=httpx.MockTransport(wire.handle),
                api_key="offline",
                base_url="https://gemini.invalid",
            )
            stack.callback(client.close)
            stack.push_async_callback(client.aio.aclose)
            backend = GeminiManagedAgents(
                GeminiSDKTransport(client),
                storage=MemoryStorage(),
                state_store=MemoryStateStore(),
                account_scope_id="project",
                resource_catalog_enabled=True,
                resource_catalog_transport=SDKAgentCatalogTransport(client),
            )
        resources = NeutralResourceHost(
            backend,
            request.deps.sessionmaker,
            scope=SCOPE,
            account_scope_id="project",
            operation_key="owned-counterpart-read",
            resource_catalog_enabled=provider == "gemini",
            catalog_adapter=getattr(backend, "catalog_adapter", None),
        )
        spec = AgentSpec(
            name="unrelated-display-label",
            model=ModelRef(
                provider=provider, id="gpt-6-luna" if provider == "openai" else "gemini-3.8-flash"
            ),
            system="Retained instructions",
            tools=(),
            mcp_servers=(),
            skills=None,
        )
        if provider == "openai":
            agent = Agent(
                ref=ResourceRef(
                    id="owned-agent",
                    kind="agent",
                    provider="openai",
                    account_scope_id="project",
                    tenant_id=SCOPE.tenant_id,
                    account_id=SCOPE.account_id,
                ),
                revision=Revision(local=0),
                spec=spec,
                created_at=datetime(2026, 10, 12, tzinfo=UTC),
            )
        else:
            skill = await backend.skills.create(
                SCOPE,
                SkillUpload(
                    files=(
                        SkillUploadFile(path="SKILL.md", content=b"# Retained\nUse every file.\n"),
                        SkillUploadFile(path="scripts/check.sh", content=b"true\n"),
                    )
                ),
                key="concrete-counterpart-skill",
            )
            assert skill.latest_version is not None
            spec = spec.model_copy(update={"skills": (skill.latest_version,)})
            agent = await backend.agents.create(SCOPE, spec, key="counterpart-fixture")
        wire.native_id = agent.ref.id
        wire.requests.clear()
        retained = await resources.catalog.put_agent(
            SCOPE, "owned-counterpart", agent, expected_revision=0
        )
        plan = plan_type(provider).model_validate(
            {
                **deployment(provider, tmp_path),
                **PINS,
                "agent_id": agent.ref.id,
                "skill_ids": [pin.id for pin in spec.skills or ()],
            }
        )
        manifest_path = tmp_path / "operator-runtime.json"
        manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
        monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest_path))
        original = os.environ.get

        def refuse_key(name: str, default: str | None = None) -> str | None:
            if name == plan.api_key_env:
                raise AssertionError("native readback component discovered a credential")
            return original(name, default)

        monkeypatch.setattr(os.environ, "get", refuse_key)
        yield Owned(request, source, plan, resources, retained, wire, manifest_path)


def build_runtime(provider: str, request: ProviderPreparationRequest):
    if provider == "openai":
        from daimon.core.turn.openai_deployment import build_openai_runtime

        return build_openai_runtime(request)
    from daimon.core.turn.gemini_deployment import build_gemini_runtime

    return build_gemini_runtime(request)
