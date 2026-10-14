"""Actual SDK resource traffic and real product provisioning/catalog writes."""

from __future__ import annotations

import inspect
import io
import json
import os
import re
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from daimon.core import agent_fork
from daimon.core._models import Tenant
from daimon.core.authz import Subject
from daimon.core.channel_rules import set_channel_rule
from daimon.core.defaults.metadata import build_metadata, tenant_scoped_display_title
from daimon.core.mux_compat import create_agent, create_skill
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.testing.admin_bindings import (
    AdminBindings,
    AdminCommand,
    AdminReceipt,
    provision_admin_run,
)
from daimon.testing.admin_resources import ResourceAdminBindings
from daimon.testing.db import isolated_test_schema, require_owned_sessionmaker
from daimon.testing.drivers.gemini.fake import MemoryStorage
from daimon.testing.isolated_sdk import (
    AmbientSDKConfigurationError,
    build_async_openai,
    build_gemini_client,
)
from daimon.testing.ma import FakeMAState, MARouter, list_response, make_fake_ma_handler
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from google.genai import Client
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.ports import ManagedAgents
from mux.contracts.resources import AgentSpec, SessionSpec, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.transport import SDKTransport as GeminiTransport
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport as OpenAITransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ScopeViolation
from mux.state.memory import MemoryStateStore
from openai import AsyncOpenAI
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytest_plugins = ["daimon.testing.db"]
FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "judge" / "fixtures" / "admin_operations.json"
)
WORKSPACE, CHANNEL = "123456789012345678", "223456789012345678"
DATA = bundle_from_markdown(
    "---\nname: qa-echo-skill\ndescription: Offline echo\n---\nEcho the requested text.\n"
)


@dataclass
class Case:
    binding: AdminBindings
    resources: ResourceAdminBindings
    trace: ScriptedTransport
    openai: AsyncOpenAI
    google: Client
    sdk_trace: ScriptedTransport

    async def execute(self, source: dict[str, Any], **values: str) -> AdminReceipt:
        return await self.binding.execute(
            source,
            {
                "channel_id": CHANNEL,
                "channel:A": CHANNEL,
                "channel:B": "323456789012345678",
                "env.qa_base_with_own_skill": "daimon",
                "env.qa_library_skill": "qa-echo-skill",
                "env.qa_user_id": "423456789012345678",
                **values,
            },
        )


def source(command: str) -> dict[str, Any]:
    return {"do": "admin", "tool": "cli", "args": command}


def require_fixture_sdk_environment() -> None:
    if any(
        name.startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_", "GENAI_"))
        for name in os.environ
    ):
        raise ValueError("offline resource fixtures refuse ambient provider SDK configuration")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    require_fixture_sdk_environment()

    def no_artwork(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(agent_fork, "queue_agent_face", no_artwork)


class AnthropicServer:
    """Native fixture state changes only in response to actual SDK mutations."""

    def __init__(self) -> None:
        self.state = FakeMAState()
        self.agent_handler = make_fake_ma_handler(self.state)
        self.skills: list[dict[str, Any]] = []
        self.router = MARouter()
        for method, path in (("GET", r"/v1/agents(?:/[^/]+)?"), ("POST", r"/v1/agents(?:/[^/]+)?")):
            self.router.add(method, path, lambda request, match: self.agent_handler(request))
        self.router.add("GET", r"/v1/skills", lambda request, match: list_response(self.skills))
        self.router.add("POST", r"/v1/skills", self.upload)
        self.router.add(
            "GET",
            r"/v1/skills/(?P<id>[^/]+)/versions",
            lambda request, match: httpx.Response(
                200, json={"data": [{"id": "version_1", "version": "1"}], "next_page": None}
            ),
        )
        self.router.add(
            "GET",
            r"/v1/skills/(?P<id>[^/]+)/versions/version_1/content",
            lambda request, match: httpx.Response(200, content=DATA.zip_bytes),
        )
        self.router.add("POST", r"/v1/agents/(?P<id>[^/]+)/archive", self.archive)

    def upload(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        title = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
        assert title is not None
        skill = {
            "id": f"skill_{len(self.skills)}",
            "type": "custom",
            "source": "custom",
            "display_title": title[1].decode(),
            "latest_version": "1",
            "created_at": "2026-10-10T00:00:00Z",
            "updated_at": "2026-10-10T00:00:00Z",
        }
        self.skills.append(skill)
        return httpx.Response(200, json=skill)

    def archive(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        raw = self.state.agents.pop(match["id"])
        return httpx.Response(200, json={**raw, "archived_at": "2026-10-10T00:00:00Z"})


class OpenAIServer:
    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.router = MARouter()
        self.router.add("POST", r"/v1/agents", self.create)
        self.router.add(
            "GET",
            r"/v1/agents/(?P<id>[^/]+)",
            lambda request, match: httpx.Response(200, json=self.agents[match["id"]]),
        )
        self.skills: dict[str, dict[str, Any]] = {}
        self.router.add("POST", r"/v1/skills", self.upload)
        self.router.add(
            "GET",
            r"/v1/skills/(?P<id>[^/]+)/versions/1",
            lambda request, match: httpx.Response(
                200,
                json={
                    "skill_id": match["id"],
                    "version": "1",
                    "name": "qa-echo-skill",
                    "created_at": 0,
                },
            ),
        )

    def upload(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        id_ = f"skill_{len(self.skills)}"
        self.skills[id_] = {
            "id": id_,
            "name": "qa-echo-skill",
            "latest_version": "1",
            "default_version": "1",
            "created_at": 0,
        }
        return httpx.Response(200, json=self.skills[id_])

    def create(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        raw = json.loads(request.content)
        id_ = f"agent_{len(self.agents)}"
        self.agents[id_] = {**raw, "id": id_, "created_at": 0}
        return httpx.Response(200, json=self.agents[id_])


def build_resource_clients(trace: ScriptedTransport) -> tuple[AsyncOpenAI, Client]:
    """The fixture's real SDK construction boundary; only injected transports."""
    require_fixture_sdk_environment()
    return (
        build_async_openai(
            api_key="offline-fixture",
            base_url="https://openai.invalid/v1",
            transport=httpx.MockTransport(trace.dispatch),
        ),
        build_gemini_client(
            api_key="offline-fixture",
            base_url="https://gemini.invalid",
            transport=httpx.MockTransport(trace.dispatch),
        ),
    )


@pytest.fixture(params=["anthropic", "openai", "gemini"])
async def case(
    request: pytest.FixtureRequest, db_session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Case]:
    require_fixture_sdk_environment()
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError:
        bound = db_session_factory.kw.get("bind")
        engine = bound.engine if isinstance(bound, AsyncConnection) else bound
        if (
            isinstance(engine, AsyncEngine)
            and engine.url.get_backend_name() == "postgresql"
            and engine.url.host in ("localhost", "127.0.0.1")
            and engine.url.database == "daimon_test"
            and not engine.url.query
            and not db_session_factory.kw.get("binds")
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip("admin proofs require a lane database or registered isolated CI schema")
        raise
    provider = cast(Provider, request.param)
    anthropic_server = AnthropicServer()
    anthropic_trace = ScriptedTransport(router=anthropic_server.router)
    binding = await provision_admin_run(
        db_session_factory,
        workspace_id=WORKSPACE,
        backend=provider,
        channels=frozenset({CHANNEL, "323456789012345678"}),
        trace=anthropic_trace,
    )
    binding.default = DeploymentDefault(agent_name="daimon")
    trace = ScriptedTransport(router=OpenAIServer().router if provider == "openai" else None)
    openai, google = build_resource_clients(trace)
    backend: ManagedAgents | None = None
    if provider == "openai":
        backend = OpenAIDriver(
            OpenAITransport(openai),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == binding.scope,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
    elif provider == "gemini":
        backend = GeminiManagedAgents(
            GeminiTransport(google),
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id="project",
        )
    resources = ResourceAdminBindings(
        binding,
        backend=backend,
        provider_workspace="project",
        operation_key="qa:resource",
        bundles={"fixtures/SKILL.md": DATA},
        channel_labels={CHANNEL: "qa-room", "323456789012345678": "qa-other-room"},
    )
    binding.resource_edge = resources
    metadata = build_metadata(
        tenant_id=uuid.UUID(binding.scope.tenant_id),
        account_id=uuid.UUID(binding.scope.account_id),
        name="daimon",
    )
    try:
        if resources.host is not None:
            await resources.host.publish(
                "daimon",
                AgentSpec(
                    name="daimon",
                    model=ModelRef(
                        provider=provider,
                        id="gpt-6-luna" if provider == "openai" else "gemini-3.8-flash",
                    ),
                    system="Help offline.",
                    tools=(
                        ToolSpec(
                            name="bash" if provider == "openai" else "code_execution",
                            kind="builtin",
                        ),
                    ),
                    skills=(),
                    metadata=metadata,
                ),
                expected_revision=0,
                key="qa:seed",
            )
        else:
            await create_agent(
                binding.anthropic,
                cast(
                    dict[str, object],
                    {
                        "name": "daimon",
                        "model": "claude-haiku-4-5",
                        "system": "Help offline.",
                        "metadata": metadata,
                        "tools": [
                            {
                                "type": "agent_toolset_20260401",
                                "configs": [],
                                "default_config": {
                                    "enabled": True,
                                    "permission_policy": {"type": "always_allow"},
                                },
                            }
                        ],
                        "skills": [],
                    },
                ),
                scope=binding.scope,
            )
        await binding.configure_channel(CHANNEL)
        yield Case(
            binding,
            resources,
            trace if backend is not None else anthropic_trace,
            openai,
            google,
            trace,
        )
    finally:
        await binding.close()
        google.close()
        await google.aio.aclose()
        await openai.close()


async def test_resource_callback_returns_actual_copy_identity_and_repeat_does_not_copy_twice(
    case: Case,
) -> None:
    command = source(
        "daimon channels rule set discord {guild_id} {channel_id} --readers own --copy-from daimon"
    )
    receipt = await case.execute(command)
    again = await case.execute(command)
    assert receipt.status == again.status == "executed"
    assert receipt.values["fork.name"] == again.values["fork.name"] == "qa-room"
    if case.resources.host is not None:
        row = await case.resources.host.catalog.get_agent(
            case.binding.scope, receipt.values["fork.catalog_id"]
        )
        assert (
            row is not None and str(row.local_revision) == receipt.values["fork.catalog_revision"]
        )
        assert row.agent.ref.id == receipt.values["fork.id"]
    async with case.binding.sessions() as session:
        policy = await load_access_policy(
            session, tenant_id=uuid.UUID(case.binding.scope.tenant_id)
        )
        _, rows = await list_propagations_for_tenant(
            session, tenant_id=uuid.UUID(case.binding.scope.tenant_id)
        )
    assert policy.agent_rules["qa-room"].runs_in == (CHANNEL,)
    assert next(row for row in rows if row.channel_id == CHANNEL).agent_name == "qa-room"
    assert len(
        [r for r in case.trace.requests if r.method == "POST" and r.path == "/v1/agents"]
    ) == (0 if case.binding.backend == "gemini" else 2)
    if case.binding.backend != "anthropic":
        assert not case.binding.trace.requests
    case.trace.assert_consumed()


async def test_confirmed_add_uses_actual_sdk_and_retains_concrete_bundle(case: Case) -> None:
    fork = await case.execute(
        source(
            "daimon channels rule set discord {guild_id} {channel_id} --readers own --copy-from daimon"
        )
    )
    receipt = await case.execute(
        source("daimon skills --guild {guild_id} add fixtures/SKILL.md --agent {fork.name} --yes"),
        **fork.values,
    )
    assert receipt.status == "executed" and receipt.observations["newly_attached"] is True
    host = case.resources.host
    if host is not None:
        row = await host.catalog.get_agent(case.binding.scope, fork.values["fork.catalog_id"])
        assert row is not None and row.local_revision == 2 and len(row.agent.spec.skills or ()) == 1
        skill = await host.catalog.get_skill(
            case.binding.scope,
            skill_id=str(receipt.observations["skill_id"]),
            version=str(receipt.observations["version"]),
        )
        assert (
            skill is not None
            and skill.bundle == DATA
            and skill.agent_name == fork.values["fork.name"]
        )
        if case.binding.backend == "gemini":
            trace = case.trace
            trace.queue(
                ScriptedReply(
                    "POST",
                    "/v1beta/interactions",
                    httpx.Response(
                        200,
                        json={
                            "id": "offline-interaction",
                            "status": "completed",
                            "created": "2026-10-10T00:00:00Z",
                            "updated": "2026-10-10T00:00:00Z",
                            "environment_id": "workspace:qa",
                            "steps": [],
                            "usage": None,
                        },
                    ),
                )
            )
            session = await host.backend.sessions.create(
                host.scope,
                SessionSpec(
                    agent=row.agent.ref,
                    agent_revision=row.agent.revision,
                    config_revision=1,
                    extensions={
                        "gemini.session": ExtensionConfig(
                            namespace="gemini.session",
                            version=1,
                            value={
                                "thread": {
                                    "channel": {
                                        "tenant_id": host.scope.tenant_id,
                                        "platform": "discord",
                                        "channel_id": CHANNEL,
                                    },
                                    "thread_id": "thread",
                                },
                                "binding_id": "binding",
                            },
                        )
                    },
                ),
                key="qa:session",
            )
            await host.backend.events.send(
                host.scope,
                session.ref,
                (UserMessage(content=(TextPart(text="Echo offline."),)),),
                key="qa:send",
            )
            body = trace.requests[-1].json()
            assert "qa-echo-skill" in json.dumps(body) and "Echo the requested text" in json.dumps(
                body
            )
        else:
            upload = next(
                r for r in case.trace.requests if r.path == "/v1/skills" and r.method == "POST"
            )
            start = upload.body.index(b"PK\x03\x04")
            with zipfile.ZipFile(io.BytesIO(upload.body[start:])) as archive:
                assert archive.read("bundle/SKILL.md").endswith(b"Echo the requested text.\n")
    else:
        upload = next(
            r for r in case.trace.requests if r.path == "/v1/skills" and r.method == "POST"
        )
        assert b"qa-room/qa-echo-skill" in upload.body
    case.trace.assert_consumed()


async def test_unconfirmed_or_unknown_fixture_add_refuses_before_io(case: Case) -> None:
    before = len(case.trace.requests)
    for command in (
        "daimon skills --guild {guild_id} add fixtures/SKILL.md --agent daimon",
        "daimon skills --guild {guild_id} add /etc/passwd --agent daimon --yes",
    ):
        receipt = await case.execute(source(command))
        assert (
            receipt.status == "pending" and receipt.code == "ADMIN_CONFIRMED_SKILL_FIXTURE_UNBOUND"
        )
    assert len(case.trace.requests) == before


async def test_foreign_channel_is_refused_before_resource_io(case: Case) -> None:
    before = len(case.trace.requests)
    with pytest.raises(ScopeViolation):
        await case.execute(
            source(
                "daimon channels rule set discord {guild_id} 999999999999999999 --readers own --copy-from daimon"
            )
        )
    with pytest.raises(ScopeViolation):
        await case.resources(
            AdminCommand(
                "channel_rule_set",
                "discord",
                WORKSPACE,
                "999999999999999999",
                options={"--readers": "own", "--copy-from": "daimon"},
            )
        )
    assert len(case.trace.requests) == before


async def test_native_cleanup_remains_typed_pending_and_does_not_change_catalog(case: Case) -> None:
    fork = await case.execute(
        source(
            "daimon channels rule set discord {guild_id} {channel_id} --readers own --copy-from daimon"
        )
    )
    receipt = await case.execute(
        {"do": "admin", "tool": "archive_channel_forks", "args": {"channel": "{channel_id}"}}
    )
    if case.resources.host is None:
        assert (
            receipt.status == "executed"
            and len(cast(list[str], receipt.observations["archived"])) == 1
        )
        return
    assert receipt.status == "pending" and receipt.code == (
        "ADMIN_GEMINI_AGENT_DELETE_UNSUPPORTED"
        if case.binding.backend == "gemini"
        else "ADMIN_NEUTRAL_COPY_ARCHIVE_UNBOUND"
    )
    row = await case.resources.host.catalog.get_agent(
        case.binding.scope, fork.values["fork.catalog_id"]
    )
    assert row is not None and row.agent.lifecycle == "active"
    assert not any(r.method == "DELETE" for r in case.trace.requests)


@pytest.mark.parametrize("case", ["openai", "gemini"], indirect=True)
@pytest.mark.parametrize("verb", ["add", "remove"])
async def test_neutral_channel_skills_never_substitute_agent_upload(case: Case, verb: str) -> None:
    before = len(case.trace.requests)
    receipt = await case.execute(
        source(f"daimon channels skills {verb} discord {{guild_id}} {{channel_id}} qa-echo-skill")
    )
    assert receipt.status == "pending" and receipt.code == "ADMIN_NEUTRAL_CHANNEL_SKILLS_UNBOUND"
    async with case.binding.sessions() as session:
        assert not await list_channel_skills(
            session,
            tenant_id=uuid.UUID(case.binding.scope.tenant_id),
            platform="discord",
            channel_id=CHANNEL,
        )
    assert len(case.trace.requests) == before


@pytest.mark.parametrize(
    "scenario",
    [
        "QA-D8-GITHUB-CONNECT-OFFERED",
        "QA-ISO-TEAM-CHANNEL-ISOLATION",
        "QA-NEW12-AGENT-SWITCH-HANDOFF-COPY",
        "QA-NEW17-MCP-USABLE-NEXT-MESSAGE",
        "QA-NEW18-SKILL-ADD-YES-PLEASE",
    ],
)
async def test_exact_frozen_resource_operations_execute_or_retain_specific_native_cut(
    case: Case, scenario: str
) -> None:
    row = next(
        row
        for row in json.loads(FIXTURE.read_text())["scenarios"]
        if row["scenario_id"] == scenario
    )
    for operation in row["operations"]:
        receipt = await case.execute(operation["source"])
        if (
            operation["source"]["tool"] == "archive_channel_forks"
            and case.binding.backend != "anthropic"
        ):
            assert receipt.status == "pending"
        else:
            assert receipt.status == "executed"
    case.trace.assert_consumed()


async def test_d14_source_owned_skill_is_uploaded_again_and_retained_on_actual_fork(
    case: Case,
) -> None:
    added = await case.execute(
        source("daimon skills --guild {guild_id} add fixtures/SKILL.md --agent daimon --yes")
    )
    assert added.status == "executed"
    row = next(
        row
        for row in json.loads(FIXTURE.read_text())["scenarios"]
        if row["scenario_id"] == "QA-D14-FORK-KEEPS-SKILLS"
    )
    fork = await case.execute(row["operations"][0]["source"])
    assert fork.status == "executed" and fork.observations["dropped_skills"] == []
    if case.resources.host is not None:
        source_agent = await case.resources.host.find("daimon")
        copy = await case.resources.host.find(fork.values["fork.name"])
        assert source_agent is not None and copy is not None
        source_pins, copy_pins = (
            source_agent.revision.agent.spec.skills,
            copy.revision.agent.spec.skills,
        )
        assert source_pins and copy_pins
        source_pin, copy_pin = source_pins[0], copy_pins[0]
        assert source_pin.id != copy_pin.id and copy_pin.version not in (None, "latest")
        retained = await case.resources.host.catalog.get_skill(
            case.binding.scope, skill_id=copy_pin.id, version=copy_pin.version
        )
        assert retained is not None and retained.bundle == DATA
    else:
        uploads = [r for r in case.trace.requests if r.method == "POST" and r.path == "/v1/skills"]
        assert len(uploads) == 2 and b"qa-room/qa-echo-skill" in uploads[-1].body
        assert any(r.path.endswith("/versions/version_1/content") for r in case.trace.requests)
    cleanup = await case.execute(row["operations"][1]["source"])
    assert cleanup.status == ("executed" if case.binding.backend == "anthropic" else "pending")
    case.trace.assert_consumed()


@pytest.mark.parametrize("case", ["anthropic"], indirect=True)
async def test_anthropic_resource_callback_preserves_direct_product_request_bytes(
    case: Case,
) -> None:
    binding = case.binding
    start = len(case.trace.requests)
    await set_channel_rule(
        binding.anthropic,
        binding.sessions,
        tenant_id=uuid.UUID(binding.scope.tenant_id),
        platform="discord",
        channel_id=CHANNEL,
        readers="own",
        subject=Subject(is_admin=True),
        default=binding.default,
        actor_account_id=uuid.UUID(binding.scope.account_id),
        copy=True,
        copy_from="daimon",
        channel_label="qa-room",
    )
    direct = case.trace.requests[start:]
    await case.execute(
        {"do": "admin", "tool": "archive_channel_forks", "args": {"channel": "{channel_id}"}}
    )
    await set_channel_rule(
        binding.anthropic,
        binding.sessions,
        tenant_id=uuid.UUID(binding.scope.tenant_id),
        platform="discord",
        channel_id=CHANNEL,
        readers="any",
        subject=Subject(is_admin=True),
        default=binding.default,
    )
    start = len(case.trace.requests)
    receipt = await case.execute(
        source(
            "daimon channels rule set discord {guild_id} {channel_id} --readers own --copy-from daimon"
        )
    )
    assert receipt.status == "executed"
    assert case.trace.requests[start:] == direct
    assert any(r.method == "POST" and r.path == "/v1/agents" for r in direct)


@pytest.mark.parametrize("case", ["anthropic"], indirect=True)
async def test_anthropic_extra_channel_skill_uses_the_actual_channel_service_and_readback(
    case: Case,
) -> None:
    created = await create_skill(
        case.binding.anthropic,
        display_title=tenant_scoped_display_title(
            tenant_id=uuid.UUID(case.binding.scope.tenant_id), name="qa-echo-skill"
        ),
        data=DATA.zip_bytes,
        scope=case.binding.scope,
    )
    assert created.id
    for scenario in ("QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS", "QA-D7-BIG-WORKSPACE-CHECKPOINT"):
        row = next(
            row
            for row in json.loads(FIXTURE.read_text())["scenarios"]
            if row["scenario_id"] == scenario
        )
        for operation in row["operations"]:
            receipt = await case.execute(operation["source"])
            assert receipt.status == "executed"
    receipt = await case.execute(
        source("daimon channels skills add discord {guild_id} {channel_id} qa-echo-skill")
    )
    assert receipt.status == "executed" and receipt.observations["attached"] is True
    # Removal accepts exactly the existing CLI's id or mount name.
    removed = await case.execute(
        source("daimon channels skills remove discord {guild_id} {channel_id} qa-echo-skill")
    )
    assert removed.status == "executed" and removed.observations["attached"] is False
    case.trace.assert_consumed()


async def test_new19_attached_delete_is_pending_not_an_invented_expected_failure(
    case: Case,
) -> None:
    scenario = next(
        row
        for row in json.loads(FIXTURE.read_text())["scenarios"]
        if row["scenario_id"] == "QA-NEW19-SKILL-DELETE-WHILE-ATTACHED"
    )
    fork = await case.execute(scenario["operations"][0]["source"])
    added = await case.execute(scenario["operations"][1]["source"], **fork.values)
    assert added.status == "executed"
    if case.resources.host is not None:
        assert added.values["fork.catalog_revision"] == "2"
        assert added.values["fork.id"] != fork.values["fork.id"]
    before = len(case.trace.requests)
    refused = await case.execute(scenario["operations"][2]["source"])
    assert refused.status == "pending" and refused.code == "ADMIN_SKILL_DELETE_PRODUCT_EDGE_UNBOUND"
    assert len(case.trace.requests) == before


async def test_ci_resource_callbacks_preserve_real_catalog_and_isolated_database(
    case: Case, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_database = db_engine.url.database
    actual_schema = isolated_test_schema(db_engine)
    assert actual_schema is not None and str(os.getpid()) in actual_schema
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(db_engine.sync_engine, "url", db_engine.url.set(database="daimon_test"))
    fork = await case.execute(
        source(
            "daimon channels rule set discord {guild_id} {channel_id} --readers own --copy-from daimon"
        )
    )
    added = await case.execute(
        source("daimon skills --guild {guild_id} add fixtures/SKILL.md --agent {fork.name} --yes"),
        **fork.values,
    )
    assert fork.status == added.status == "executed"
    assert added.observations["newly_attached"] is True
    async with case.binding.sessions() as session:
        database, schema = (
            await session.execute(text("SELECT current_database(), current_schema()"))
        ).one()
        policy = await load_access_policy(
            session, tenant_id=uuid.UUID(case.binding.scope.tenant_id)
        )
    assert database == actual_database
    assert schema == actual_schema
    assert policy.agent_rules[fork.values["fork.name"]].runs_in == (CHANNEL,)
    if case.resources.host is not None:
        row = await case.resources.host.catalog.get_agent(
            case.binding.scope, fork.values["fork.catalog_id"]
        )
        assert row is not None and row.local_revision == 2 and len(row.agent.spec.skills or ()) == 1
    # Gemini publishes concrete bundles through its storage SPI; its actual
    # SDK interaction proof remains in the confirmed-Add regression above.
    if case.binding.backend != "gemini":
        assert case.trace.requests
    case.trace.assert_consumed()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test",
        "postgresql+asyncpg://offline:fixture@remote.invalid/daimon_test_nc_n6",
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n6?host=remote.invalid",
    ],
)
async def test_direct_resource_callback_refuses_unowned_db_before_sdk_or_connection(
    case: Case, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    engine = create_async_engine(url)
    connections: list[None] = []

    def forbid_connection(*_args: object, **_kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("resource callback attempted an unowned DB connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    monkeypatch.setattr(case.binding, "sessions", async_sessionmaker(engine))
    before = len(case.trace.requests)
    try:
        with pytest.raises(ValueError, match="owned local lane engines"):
            await case.resources(
                AdminCommand(
                    "channel_rule_set",
                    "discord",
                    WORKSPACE,
                    CHANNEL,
                    options={"--readers": "own", "--copy-from": "daimon"},
                )
            )
        assert connections == [] and len(case.trace.requests) == before
    finally:
        await engine.dispose()


@pytest.mark.parametrize("name", ["ANTHROPIC_WEBHOOK_SIGNING_KEY", "ANTHROPIC_CUSTOM_HEADERS"])
@pytest.mark.parametrize("value", ["", "x-secret: synthetic-sdk-override"])
async def test_direct_resource_callback_refuses_late_sdk_environment_before_io(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(name, value)
    caplog.set_level("DEBUG")
    before = len(case.trace.requests)
    bind = case.binding.sessions.kw["bind"]
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    assert isinstance(engine, AsyncEngine)
    statements: list[None] = []

    def forbid_statement(*_args: object, **_kwargs: object) -> None:
        statements.append(None)
        raise AssertionError("late SDK override allowed a resource DB statement")

    event.listen(engine.sync_engine, "before_cursor_execute", forbid_statement)
    try:
        with pytest.raises(ValueError, match="ambient") as error:
            await case.resources(
                AdminCommand(
                    "channel_rule_set",
                    "discord",
                    WORKSPACE,
                    CHANNEL,
                    options={"--readers": "own", "--copy-from": "daimon"},
                )
            )
        assert len(case.trace.requests) == before and not statements
        assert "synthetic-sdk-override" not in str(error.value) + caplog.text
        assert name in os.environ
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", forbid_statement)


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "OPENAI_API_KEY",
        "OPENAI_ORG_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GENAI_FUTURE_OVERRIDE",
    ],
)
@pytest.mark.parametrize("value", ["", "synthetic-sdk-override"])
def test_resource_sdk_fixture_refuses_presence_without_erasing_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    def forbid_allocation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resource SDK fixture allocated an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", forbid_allocation)
    with pytest.raises(ValueError, match="refuse ambient provider SDK configuration"):
        require_fixture_sdk_environment()
    assert name in os.environ


@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://offline:fixture@localhost/operator_database",
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test",
        "postgresql+asyncpg://offline:fixture@remote.invalid/daimon_test_nc_n6",
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n6?host=remote.invalid",
    ],
)
async def test_direct_resource_callback_refuses_foreign_model_bind_before_io(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    bind_kind: str,
    url: str,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    foreign = create_async_engine(url)
    connections: list[None] = []

    def forbid_connection(*_args: object, **_kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("resource callback connected through a foreign model bind")

    event.listen(foreign.sync_engine, "do_connect", forbid_connection)
    original = case.binding.sessions.kw["bind"]
    routed = foreign if bind_kind == "engine" else AsyncConnection(foreign)
    sessions = async_sessionmaker(original, binds={Tenant: routed}, expire_on_commit=False)
    monkeypatch.setattr(case.binding, "sessions", sessions)
    before = len(case.trace.requests)
    try:
        with pytest.raises(ValueError, match="owned local lane engines"):
            await case.resources(
                AdminCommand(
                    "channel_rule_set",
                    "discord",
                    WORKSPACE,
                    CHANNEL,
                    options={"--readers": "own", "--copy-from": "daimon"},
                )
            )
        assert not connections and len(case.trace.requests) == before
    finally:
        await foreign.dispose()


@pytest.mark.parametrize(
    "name", ["OPENAI_ADMIN_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS"]
)
@pytest.mark.parametrize("method", ["copy", "with_options", "transport"])
async def test_resource_owned_openai_clones_refuse_late_settings_before_io(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    method: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(name, "x-fixture-secret: synthetic-sdk-override")
    caplog.set_level("DEBUG")
    before = len(case.sdk_trace.requests)
    with pytest.raises(ValueError, match="ambient") as error:
        if method == "copy":
            case.openai.copy()
        elif method == "with_options":
            case.openai.with_options()
        else:
            OpenAITransport(case.openai)
    assert len(case.sdk_trace.requests) == before
    assert "synthetic-sdk-override" not in str(error.value) + caplog.text
    assert name in os.environ


class ValueReadTrap(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name
        self.reads: list[str] = []

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, name: str) -> str:
        if name == self.name:
            self.reads.append(name)
            raise AssertionError("fixture SDK read an ambient TLS/proxy value")
        raise KeyError(name)


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY"])
async def test_resource_sdk_constructors_do_not_read_tls_proxy_or_allocate_network(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    with monkeypatch.context() as isolated:
        ambient = ValueReadTrap(name)
        isolated.setattr(os, "environ", ambient)

        def forbid_network(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("resource fixture allocated a default network transport")

        isolated.setattr(httpx, "HTTPTransport", forbid_network)
        isolated.setattr(httpx, "AsyncHTTPTransport", forbid_network)
        isolated.setattr("httpx._client.HTTPTransport", forbid_network)
        isolated.setattr("httpx._client.AsyncHTTPTransport", forbid_network)
        trace = ScriptedTransport()
        with pytest.raises(AmbientSDKConfigurationError, match=name):
            build_resource_clients(trace)
        assert not ambient.reads and not trace.requests


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
@pytest.mark.parametrize(
    ("actions", "ci"),
    [
        (None, None),
        ("true", None),
        (None, "true"),
        ("true", "false"),
        ("false", "true"),
        ("TRUE", "true"),
        ("true", "true"),
    ],
)
async def test_actual_resource_fixture_skips_plain_local_database_outside_exact_ci(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    bind_kind: str,
    actions: str | None,
    ci: str | None,
) -> None:
    for name, value in (("GITHUB_ACTIONS", actions), ("CI", ci)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    engine = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test")
    connections: list[None] = []

    def forbid_connection(*args: object, **kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("contributor fixture attempted a database connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    sessions = async_sessionmaker(
        engine if bind_kind == "engine" else AsyncConnection(engine), expire_on_commit=False
    )
    fixture = inspect.unwrap(case)
    generator = fixture(SimpleNamespace(param=provider), sessions)
    expected = ValueError if actions == ci == "true" else pytest.skip.Exception
    try:
        with pytest.raises(expected, match="owned local|lane database"):
            await anext(generator)
        assert not connections
    finally:
        await generator.aclose()
        await engine.dispose()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_actual_resource_fixture_does_not_skip_foreign_per_model_bind(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    shared = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test")
    foreign = create_async_engine(
        "postgresql+asyncpg://offline:fixture@localhost/operator_database"
    )
    connections: list[None] = []

    def forbid_connection(*args: object, **kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("foreign fixture attempted a database connection")

    event.listen(shared.sync_engine, "do_connect", forbid_connection)
    event.listen(foreign.sync_engine, "do_connect", forbid_connection)
    sessions = async_sessionmaker(shared, binds={Tenant: foreign}, expire_on_commit=False)
    generator = inspect.unwrap(case)(SimpleNamespace(param=provider), sessions)
    try:
        with pytest.raises(ValueError, match="owned local"):
            await anext(generator)
        assert not connections
    finally:
        await generator.aclose()
        await foreign.dispose()
        await shared.dispose()
