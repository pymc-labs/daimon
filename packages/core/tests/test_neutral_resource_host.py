"""Actual OpenAI SDK/native traces with the real two-table Postgres catalog."""

from __future__ import annotations

import asyncio
import io
import json
import os
import uuid
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from daimon.core import agent_fork
from daimon.core.agent_fork import fork_host_agent
from daimon.core.authz import Subject
from daimon.core.channel_rules import set_channel_rule
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_CHANNEL_COPY,
    MA_METADATA_KEY_NAME,
    tenant_scoped_display_title,
)
from daimon.core.errors import DaimonError
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.permissions import channel_rule
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.add import add_host_agent_skill
from daimon.core.skills.ingest import SkillBundle, bundle_from_files, bundle_from_markdown
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.neutral_catalog import CatalogConflict, PostgresNeutralCatalog
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.testing.factories import make_account, make_tenant
from mux import profiles
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.resources import Agent, AgentSpec, MCPConnection, ToolSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import Context
from mux.drivers.openai.skill_bindings import resolve_pins
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles import PERSISTENT_WORKSPACE
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from mux.testing.isolated_sdk import AmbientSDKConfigurationError, build_async_openai
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 10, tzinfo=UTC)
type Setup = tuple[async_sessionmaker[AsyncSession], Scope, Scope]


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # No identity artwork/provider request may run from these offline copies.
    def no_artwork(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(agent_fork, "queue_agent_face", no_artwork)


@pytest_asyncio.fixture
async def setup(db_engine: AsyncEngine, db_clean: None) -> AsyncIterator[Setup]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
        other = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        foreign_account = await make_account(session, tenant=other)
        scope = Scope(
            tenant_id=str(tenant.id),
            account_id=str(account.id),
            principal_id="caller",
            authorization_id="authorized-host",
        )
        foreign = Scope(
            tenant_id=str(other.id),
            account_id=str(foreign_account.id),
            principal_id="caller",
            authorization_id="authorized-host",
        )
    yield sessions, scope, foreign


def spec(name: str = "source", pins: tuple[SkillRef, ...] = ()) -> AgentSpec:
    return AgentSpec(
        name=name,
        model=ModelRef(provider="openai", id="gpt-6-luna"),
        system="Help this workspace.",
        tools=(ToolSpec(name="bash", kind="builtin"), ToolSpec(name="web_search", kind="builtin")),
        skills=pins,
        metadata={MA_METADATA_KEY_NAME: name},
    )


def raw_agent(requested: AgentSpec, scope: Scope, id_: str = "original") -> dict[str, object]:
    tools: list[dict[str, object]] = []
    for tool in requested.tools or ():
        if tool.kind == "builtin" and tool.name != "bash":
            tools.append({"type": tool.name})
        elif tool.kind == "custom":
            tools.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": dict(tool.input_schema or {}),
                }
            )
    tools.extend(
        {
            "type": "mcp",
            "server_label": server.name,
            "transport": {"type": "http", "server_url": server.url},
            **server.tool_policy,
        }
        for server in requested.mcp_servers or ()
    )
    return {
        "id": id_,
        "created_at": 0,
        "name": requested.name,
        "model": requested.model.id,
        "instructions": requested.system,
        "tools": tools,
        "metadata": {
            **(requested.metadata or {}),
            "mux_tenant": scope.tenant_id,
            "mux_skill_pins": json.dumps(
                [pin.model_dump(mode="json", exclude_none=True) for pin in requested.skills or ()],
                separators=(",", ":"),
            ),
        },
    }


def reply(method: str, path: str, body: dict[str, object], status: int = 200) -> ScriptedReply:
    return ScriptedReply(method, "/v1" + path, httpx.Response(status, json=body))


def native_skill(
    id_: str = "uploaded", version: str = "1", name: str = "notes"
) -> dict[str, object]:
    return {
        "id": id_,
        "name": name,
        "created_at": 0,
        "latest_version": version,
        "default_version": version,
    }


def version(id_: str = "uploaded", value: str = "1") -> dict[str, object]:
    return {"skill_id": id_, "version": value, "name": "notes", "created_at": 0}


def bundle(name: str = "notes", body: str = "Write notes.") -> SkillBundle:
    return bundle_from_markdown(f"---\nname: {name}\ndescription: Work carefully\n---\n{body}\n")


class SDKCase:
    def __init__(self, setup: Setup) -> None:
        self.sessions, self.scope, self.foreign = setup
        self.catalog = PostgresNeutralCatalog(
            self.sessions, provider="openai", account_scope_id="project"
        )
        self.trace = ScriptedTransport()
        self.headers: list[tuple[str, str, str | None]] = []

    def sdk(self) -> AsyncOpenAI:
        def handle(request: httpx.Request) -> httpx.Response:
            self.headers.append(
                (
                    request.url.path,
                    request.headers["OpenAI-Beta"],
                    request.headers.get("Idempotency-Key"),
                )
            )
            return self.trace.dispatch(request)

        return build_async_openai(
            api_key="offline-placeholder",
            base_url="https://openai.invalid/v1",
            transport=httpx.MockTransport(handle),
        )

    def host(
        self, sdk: AsyncOpenAI, key: str = "confirmed-upload", scope: Scope | None = None
    ) -> NeutralResourceHost:
        return NeutralResourceHost(
            OpenAIDriver(
                SDKTransport(sdk),
                account_scope_id="project",
                authorization=lambda authorized, kind, id_: authorized == self.scope,
                journal=MemoryRecoveryJournal(),
                usage_revisions=MemoryUsageRevisions(),
            ),
            self.sessions,
            scope=scope or self.scope,
            account_scope_id="project",
            operation_key=key,
        )

    async def seed_agent(self, requested: AgentSpec, id_: str = "original") -> Agent:
        agent = Agent(
            ref=ResourceRef(
                id=id_,
                kind="agent",
                provider="openai",
                account_scope_id="project",
                tenant_id=self.scope.tenant_id,
                account_id=self.scope.account_id,
            ),
            revision=Revision(local=0),
            created_at=NOW,
            spec=requested,
        )
        await self.catalog.put_agent(self.scope, requested.name, agent, expected_revision=0)
        return agent

    async def seed_skill(self, id_: str, owned_by: str, data: SkillBundle, v: str = "1") -> None:
        await self.catalog.put_skill(
            self.scope,
            skill_id=id_,
            version=v,
            agent_name=owned_by,
            display_title=tenant_scoped_display_title(
                tenant_id=uuid.UUID(self.scope.tenant_id),
                name=data.preview.name,
                agent_name=None if owned_by == "@catalog" else owned_by,
            ),
            bundle=data,
        )


async def allow(_agent: Agent) -> None:
    pass


async def test_add_upload_and_publish_replace_native_agents_with_durable_exact_pins(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    original = spec().model_copy(
        update={
            "tools": (
                *(spec().tools or ()),
                ToolSpec(
                    name="owned_helper",
                    kind="custom",
                    description="Own helper",
                    input_schema={
                        "type": "object",
                        "properties": {"route": {"type": "string"}},
                        "required": ["route"],
                    },
                ),
            ),
            "mcp_servers": (
                MCPConnection(name="daimon-mcp", url="https://daimon.example.com/mcp"),
            ),
        }
    )
    first = original.model_copy(update={"skills": (SkillRef(id="uploaded", version="1"),)})
    second = original.model_copy(update={"skills": (SkillRef(id="uploaded", version="2"),)})
    await case.seed_agent(original)
    case.trace.queue(
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("POST", "/skills", native_skill()),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/uploaded/versions/1", version()),
        reply("POST", "/agents", raw_agent(first, case.scope, "replacement-1")),
        reply("GET", "/agents/replacement-1", raw_agent(first, case.scope, "replacement-1")),
        reply("GET", "/skills/uploaded", native_skill()),
        reply("GET", "/agents/replacement-1", raw_agent(first, case.scope, "replacement-1")),
        reply("POST", "/skills/uploaded/versions", version(value="2")),
        reply("GET", "/agents/replacement-1", raw_agent(first, case.scope, "replacement-1")),
        reply("GET", "/skills/uploaded/versions/2", version(value="2")),
        reply("POST", "/agents", raw_agent(second, case.scope, "replacement-2")),
    )
    gates: list[str] = []

    async def recheck(agent: Agent) -> None:
        gates.append(agent.ref.id)

    async with case.sdk() as sdk:
        created = await add_host_agent_skill(
            case.host(sdk), catalog_id="source", bundle=bundle(), recheck=recheck
        )
        updated = await add_host_agent_skill(
            case.host(sdk, "confirmed-update"),
            catalog_id="source",
            bundle=bundle(body="Changed notes."),
            recheck=recheck,
        )
    case.trace.assert_consumed()
    assert (created.action, created.newly_attached, created.version) == ("created", True, "1")
    assert (updated.action, updated.newly_attached, updated.version) == ("updated", False, "2")
    assert gates == ["original"] * 3 + ["replacement-1"] * 3
    history = [
        await case.catalog.get_agent(case.scope, "source", local_revision=n) for n in (1, 2, 3)
    ]
    assert all(row is not None and row.agent.revision.local == 0 for row in history)
    assert history[0] is not None and history[0].agent.spec.skills == ()
    assert history[1] is not None and history[1].agent.spec.skills == first.skills
    assert history[2] is not None and history[2].agent.ref.id == "replacement-2"
    retained = await case.catalog.list_skills(case.scope)
    assert {(row.version, row.bundle.preview.content_hash) for row in retained} == {
        ("1", bundle().preview.content_hash),
        ("2", bundle(body="Changed notes.").preview.content_hash),
    }
    assert all(beta == "agents=v1" for _, beta, _ in case.headers)
    assert [key for _, _, key in case.headers if key is not None] == [
        "confirmed-upload:skill:create",
        "confirmed-upload:agent:attach",
        "confirmed-update:skill:publish",
        "confirmed-update:agent:attach",
    ]
    assert not any(
        request.method == "POST" and request.path.startswith("/v1/agents/")
        for request in case.trace.requests
    )
    upload = case.trace.requests[2]
    assert b'filename="bundle.zip"' in upload.body and b"application/zip" in upload.body
    start = upload.body.index(b"PK\x03\x04")
    with zipfile.ZipFile(io.BytesIO(upload.body[start:])) as archive:
        assert archive.namelist() == ["bundle/SKILL.md"]
        assert (
            archive.read("bundle/SKILL.md")
            == b"---\nname: notes\ndescription: Work carefully\n---\nWrite notes.\n"
        )
    assert (
        json.loads(case.trace.requests[5].body)["metadata"]["mux_skill_pins"]
        == '[{"id":"uploaded","version":"1"}]'
    )
    actual_tools = json.loads(case.trace.requests[5].body)["tools"]
    assert actual_tools == [
        {"type": "web_search"},
        {
            "type": "function",
            "name": "owned_helper",
            "description": "Own helper",
            "parameters": {
                "type": "object",
                "properties": {"route": {"type": "string"}},
                "required": ["route"],
            },
        },
        {
            "type": "mcp",
            "server_label": "daimon-mcp",
            "transport": {"type": "http", "server_url": "https://daimon.example.com/mcp"},
        },
    ]


async def test_restart_reuses_retained_upload_after_failed_attachment_without_another_upload(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    original = spec()
    attached = original.model_copy(update={"skills": (SkillRef(id="uploaded", version="1"),)})
    await case.seed_agent(original)
    case.trace.queue(
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("POST", "/skills", native_skill()),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/uploaded/versions/1", version()),
        reply("POST", "/agents", {"error": {"message": "offline attach failure"}}, 500),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/uploaded", native_skill()),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/uploaded/versions/1", version()),
        reply("POST", "/agents", raw_agent(attached, case.scope, "replacement")),
    )
    async with case.sdk() as sdk:
        with pytest.raises(ProviderError):
            await add_host_agent_skill(
                case.host(sdk), catalog_id="source", bundle=bundle(), recheck=allow
            )
        before = await case.catalog.get_agent(case.scope, "source")
        assert before is not None and before.local_revision == 1
        assert len(await case.catalog.list_skills(case.scope)) == 1
        # New driver + store + host instance, no in-memory operation/upload cache.
        recovered = await add_host_agent_skill(
            case.host(sdk, "retry-attach"), catalog_id="source", bundle=bundle(), recheck=allow
        )
    case.trace.assert_consumed()
    assert recovered.action == "unchanged" and recovered.newly_attached
    assert (
        sum(
            request.method == "POST" and request.path == "/v1/skills"
            for request in case.trace.requests
        )
        == 1
    )


async def test_actual_channel_rule_copy_keeps_owned_bundle_and_shared_pin_and_repeats_without_copying(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    pins = (
        SkillRef(id="owned", version="1"),
        SkillRef(id="shared", version="1"),
        SkillRef(id="foreign", version="1"),
    )
    original = spec(pins=pins)
    await case.seed_agent(original)
    rich = bundle_from_files(
        {
            "SKILL.md": b"---\nname: notes\ndescription: Write notes\n---\nWrite them.\n",
            "scripts/notes.py": b"print('retained exactly')\n",
            "references/settings.bin": b"\x00\xff\x01\n",
        }
    )
    await case.seed_skill("owned", "source", rich)
    await case.seed_skill("shared", "@catalog", bundle("planning"))
    await case.seed_skill("foreign", "another", bundle("private"))
    copied = spec("team-alpha", (pins[1], SkillRef(id="new-owned", version="1")))
    # The real helper supplies guidance and scoped channel-copy metadata; the
    # response deliberately carries only fields the create decoder consumes.
    case.trace.queue(
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("POST", "/skills", native_skill("new-owned")),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/shared/versions/1", version("shared")),
        reply("GET", "/skills/new-owned/versions/1", version("new-owned")),
        reply("POST", "/agents", raw_agent(copied, case.scope, "channel-copy")),
    )
    anthropic = ScriptedTransport()
    async with case.sdk() as sdk, anthropic.client() as legacy:
        host = case.host(sdk, "confirmed-channel-copy")
        changed = await set_channel_rule(
            legacy,
            case.sessions,
            tenant_id=uuid.UUID(case.scope.tenant_id),
            platform="discord",
            channel_id="123",
            subject=Subject(is_admin=True),
            default=DeploymentDefault(agent_name="source"),
            readers="own",
            writers="own",
            copy=True,
            copy_from="source",
            channel_label="Team Alpha",
            resource_host=host,
            actor_account_id=uuid.UUID(case.scope.account_id),
        )
        repeated = await set_channel_rule(
            legacy,
            case.sessions,
            tenant_id=uuid.UUID(case.scope.tenant_id),
            platform="discord",
            channel_id="123",
            subject=Subject(is_admin=True),
            default=DeploymentDefault(agent_name="source"),
            readers="own",
            writers="own",
            copy=True,
            copy_from="source",
            channel_label="Team Alpha",
            resource_host=host,
            actor_account_id=uuid.UUID(case.scope.account_id),
        )
    case.trace.assert_consumed()
    anthropic.assert_consumed()
    assert anthropic.requests == []
    assert changed.copied_from == "source" and changed.agent_name == "team-alpha"
    assert changed.dropped_skills == ("private",)
    assert not repeated.changed and repeated.copied_from is None
    current = await case.catalog.get_agent(case.scope, "team-alpha")
    assert current is not None and current.agent.spec.skills == copied.skills
    assert (current.agent.spec.metadata or {})[MA_METADATA_KEY_CHANNEL_COPY] == "123"
    assert current.agent.revision.local == 0 and current.local_revision == 1
    copied_bundle = await case.catalog.get_skill(case.scope, "new-owned", "1")
    assert copied_bundle is not None and copied_bundle.agent_name == "team-alpha"
    assert copied_bundle.bundle == rich
    wire_bundle = case.trace.requests[2].body
    with zipfile.ZipFile(io.BytesIO(wire_bundle[wire_bundle.index(b"PK\x03\x04") :])) as archive:
        assert set(archive.namelist()) == {
            "bundle/SKILL.md",
            "bundle/scripts/notes.py",
            "bundle/references/settings.bin",
        }
        assert archive.read("bundle/scripts/notes.py") == b"print('retained exactly')\n"
        assert archive.read("bundle/references/settings.bin") == b"\x00\xff\x01\n"
    assert (await case.catalog.get_agent(case.scope, "source")) is not None
    async with case.sessions() as session:
        policy = await load_access_policy(session, tenant_id=uuid.UUID(case.scope.tenant_id))
        assert channel_rule(policy, "123").readers == "own"
        _, channels = await list_propagations_for_tenant(
            session, tenant_id=uuid.UUID(case.scope.tenant_id)
        )
        assert any(row.channel_id == "123" and row.agent_name == "team-alpha" for row in channels)
    native = json.loads(case.trace.requests[-1].body)
    assert native["name"] == "team-alpha"
    assert native["tools"] == [{"type": "web_search"}]
    assert "foreign" not in native["metadata"]["mux_skill_pins"]
    assert json.loads(native["metadata"]["mux_skill_pins"]) == [
        {"id": "shared", "version": "1"},
        {"id": "new-owned", "version": "1"},
    ]


@pytest.mark.parametrize(
    "reason", ["gate", "foreign_scope", "skill_cap", "other_principal", "shared", "mount_clash"]
)
async def test_add_refusals_precede_skill_mutation(setup: Setup, reason: str) -> None:
    case = SDKCase(setup)
    pins: tuple[SkillRef, ...] = ()
    if reason == "skill_cap":
        pins = tuple(SkillRef(id=f"s{i}", version="1") for i in range(AGENT_SKILL_CAP))
    if reason == "mount_clash":
        pins = (SkillRef(id="inherited", version="1"),)
        await case.seed_skill("inherited", "another", bundle())
    original = spec(pins=pins)
    await case.seed_agent(original)
    if reason in ("shared", "other_principal"):
        await case.seed_skill("uploaded", "source", bundle())
    if reason == "shared":
        await case.seed_agent(
            spec("other", (SkillRef(id="uploaded", version="1"),)), "other-native"
        )
    if reason != "foreign_scope":
        case.trace.queue(reply("GET", "/agents/original", raw_agent(original, case.scope)))
    if reason == "shared":
        case.trace.queue(
            reply("GET", "/skills/uploaded", native_skill()),
            reply("GET", "/agents/original", raw_agent(original, case.scope)),
        )

    async def recheck(_agent: Agent) -> None:
        if reason == "gate":
            raise DaimonError("The fresh caller policy refuses.")

    async with case.sdk() as sdk:
        scope = case.foreign if reason == "foreign_scope" else case.scope
        if reason == "other_principal":
            scope = case.scope.model_copy(update={"principal_id": "another-caller"})
            # Authorize only the native read for this same tenant/account. The
            # retained upload's principal guard must refuse before skill I/O.
            driver = OpenAIDriver(
                SDKTransport(sdk),
                account_scope_id="project",
                authorization=lambda authorized, kind, id_: authorized == scope,
                journal=MemoryRecoveryJournal(),
                usage_revisions=MemoryUsageRevisions(),
            )
            host = NeutralResourceHost(
                driver,
                case.sessions,
                scope=scope,
                account_scope_id="project",
                operation_key="refused",
            )
        else:
            host = case.host(sdk, scope=scope)
        with pytest.raises((DaimonError, ScopeViolation)):
            await add_host_agent_skill(
                host, catalog_id="source", bundle=bundle(body="Changed notes."), recheck=recheck
            )
    case.trace.assert_consumed()
    assert all(request.method == "GET" for request in case.trace.requests)


async def test_source_owned_unversioned_pin_needs_native_default_resolution_before_copy_write(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    original = spec(pins=(SkillRef(id="owned"),))
    await case.seed_agent(original)
    await case.seed_skill("owned", "source", bundle(), "1")
    await case.seed_skill("owned", "source", bundle(body="Newer is not necessarily default."), "10")
    case.trace.queue(reply("GET", "/agents/original", raw_agent(original, case.scope)))
    async with case.sdk() as sdk:
        with pytest.raises(UnsupportedCapability) as refused:
            await fork_host_agent(
                case.host(sdk),
                case.sessions,
                source_name="source",
                new_name="copy",
                subject=Subject(is_admin=True),
                default_agent_name=None,
            )
    case.trace.assert_consumed()
    assert refused.value.missing == ("neutral_concrete_skill_pin",)
    assert all(request.method == "GET" for request in case.trace.requests)


async def test_injected_real_driver_pin_resolver_copies_native_default_not_newest_bundle(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    original = spec(pins=(SkillRef(id="owned"),))
    await case.seed_agent(original)
    await case.seed_skill("owned", "source", bundle(), "1")
    await case.seed_skill("owned", "source", bundle(body="Latest differs from the default."), "10")
    copied = spec("copy", (SkillRef(id="copied-owned", version="1"),))
    case.trace.queue(
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        # Exact actual SDK response fields consumed by the existing resolver;
        # nullable/unneeded metadata never determines the bundle selected.
        reply(
            "GET",
            "/skills/owned",
            {"id": "owned", "default_version": "1", "latest_version": "10", "description": None},
        ),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("POST", "/skills", native_skill("copied-owned")),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/copied-owned/versions/1", version("copied-owned")),
        reply("POST", "/agents", raw_agent(copied, case.scope, "copy-agent")),
    )
    async with case.sdk() as sdk:
        host = case.host(sdk, "confirmed-fork")
        context = Context(
            SDKTransport(sdk),
            "project",
            "openai.persistent_workspace",
            lambda authorized, kind, id_: authorized == case.scope,
        )

        async def scoped_resolver(scope: Scope, pin: SkillRef) -> SkillRef:
            return (await resolve_pins((pin,), scope, context))[0]

        host.resolve_pin = scoped_resolver
        result = await fork_host_agent(
            host,
            case.sessions,
            source_name="source",
            new_name="copy",
            subject=Subject(is_admin=True),
            default_agent_name=None,
        )
    case.trace.assert_consumed()
    retained = await case.catalog.get_skill(case.scope, "copied-owned", "1")
    assert retained is not None and retained.bundle == bundle()
    assert result.copied_skills == ("copy/notes",) and result.dropped_skills == ()
    assert result.agent.revision.agent.spec.skills == copied.skills


async def test_failed_owned_skill_copy_is_reported_and_shared_pin_survives(setup: Setup) -> None:
    case = SDKCase(setup)
    original = spec(pins=(SkillRef(id="owned", version="1"), SkillRef(id="shared", version="1")))
    await case.seed_agent(original)
    await case.seed_skill("owned", "source", bundle())
    await case.seed_skill("shared", "@catalog", bundle("planning"))
    copied = spec("copy", (SkillRef(id="shared", version="1"),))
    case.trace.queue(
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("POST", "/skills", {"error": {"message": "offline failed skill copy"}}, 500),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/shared/versions/1", version("shared")),
        reply("POST", "/agents", raw_agent(copied, case.scope, "copy-agent")),
    )
    async with case.sdk() as sdk:
        result = await fork_host_agent(
            case.host(sdk),
            case.sessions,
            source_name="source",
            new_name="copy",
            subject=Subject(is_admin=True),
            default_agent_name=None,
        )
    case.trace.assert_consumed()
    assert result.dropped_skills == ("notes",) and result.copied_skills == ()
    assert result.agent.revision.agent.spec.skills == copied.skills


@pytest.mark.parametrize("subject", [Subject(), Subject(is_admin=True, via_agent_key=True)])
async def test_fork_policy_refuses_before_any_native_io(setup: Setup, subject: Subject) -> None:
    case = SDKCase(setup)
    await case.seed_agent(spec())
    async with case.sdk() as sdk:
        with pytest.raises(DaimonError, match="admin"):
            await fork_host_agent(
                case.host(sdk),
                case.sessions,
                source_name="source",
                new_name="copy",
                subject=subject,
                default_agent_name=None,
            )
    assert not case.trace.requests


async def test_catalog_cas_race_retains_native_replacements_without_a_fenced_unpublished_proof(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = SDKCase(setup)
    original = spec()
    desired = original.model_copy(update={"skills": (SkillRef(id="uploaded", version="1"),)})
    winner = original.model_copy(update={"system": "Another committed change."})
    await case.seed_agent(original)
    case.trace.queue(
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("POST", "/skills", native_skill()),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/uploaded/versions/1", version()),
        reply("POST", "/agents", raw_agent(desired, case.scope, "losing-replacement")),
        reply("POST", "/agents", raw_agent(winner, case.scope, "winning-replacement")),
    )
    async with case.sdk() as sdk:
        host = case.host(sdk)
        put = host.catalog.put_agent

        async def race(scope: Scope, catalog_id: str, agent: Agent, *, expected_revision: int):
            winning = await host.backend.agents.create(scope, winner, key="competing-change")
            await put(
                scope,
                catalog_id,
                winning.model_copy(update={"spec": winner}),
                expected_revision=expected_revision,
            )
            return await put(scope, catalog_id, agent, expected_revision=expected_revision)

        monkeypatch.setattr(host.catalog, "put_agent", race)
        with pytest.raises(CatalogConflict):
            await add_host_agent_skill(host, catalog_id="source", bundle=bundle(), recheck=allow)
    case.trace.assert_consumed()
    current = await case.catalog.get_agent(case.scope, "source")
    assert current is not None and current.local_revision == 2
    assert current.agent.ref.id == "winning-replacement" and current.agent.spec == winner
    assert all(request.method != "DELETE" for request in case.trace.requests)
    assert len(await case.catalog.list_skills(case.scope)) == 1


@pytest.mark.parametrize("historical", [False, True])
async def test_same_key_create_replay_never_deletes_the_committed_native_winner(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, historical: bool
) -> None:
    case = SDKCase(setup)
    desired = spec().model_copy(update={"system": "Changed prompt."})
    later = desired.model_copy(update={"system": "A later committed prompt."})
    await case.seed_agent(spec())
    response = raw_agent(desired, case.scope, "idempotent-winner")
    case.trace.queue(reply("POST", "/agents", response), reply("POST", "/agents", response))
    if historical:
        case.trace.queue(reply("POST", "/agents", raw_agent(later, case.scope, "later-winner")))
    case.trace.queue(reply("GET", "/agents/idempotent-winner", response))
    async with case.sdk() as sdk:
        host = case.host(sdk)
        put = host.catalog.put_agent

        async def replay(scope: Scope, catalog_id: str, agent: Agent, *, expected_revision: int):
            winner = await host.backend.agents.create(scope, desired, key="same-claimed-operation")
            await put(
                scope,
                catalog_id,
                winner.model_copy(update={"spec": desired}),
                expected_revision=expected_revision,
            )
            if historical:
                newest = await host.backend.agents.create(scope, later, key="later-operation")
                await put(
                    scope,
                    catalog_id,
                    newest.model_copy(update={"spec": later}),
                    expected_revision=expected_revision + 1,
                )
            return await put(scope, catalog_id, agent, expected_revision=expected_revision)

        monkeypatch.setattr(host.catalog, "put_agent", replay)
        with pytest.raises(CatalogConflict):
            await host.publish("source", desired, expected_revision=1, key="same-claimed-operation")
        committed = await case.catalog.get_agent(case.scope, "source", local_revision=2)
        assert committed is not None and committed.agent.ref.id == "idempotent-winner"
        native = await host.backend.agents.retrieve(case.scope, committed.agent.ref)
        assert native.ref == committed.agent.ref and native.spec.system == desired.system
    case.trace.assert_consumed()
    current = await case.catalog.get_agent(case.scope, "source")
    assert current is not None
    assert (current.local_revision, current.agent.ref.id) == (
        (3, "later-winner") if historical else (2, "idempotent-winner")
    )
    create_keys = [key for path, _beta, key in case.headers if path == "/v1/agents"]
    assert create_keys[:2] == ["same-claimed-operation", "same-claimed-operation"]
    creates = [request for request in case.trace.requests if request.method == "POST"]
    assert creates[0].body == creates[1].body
    assert all(request.method != "DELETE" for request in case.trace.requests)


@pytest.mark.parametrize(
    "normalization", ["empty_description", "omitted_schema", "builtin_annotations", "toolset"]
)
async def test_native_read_accepts_compiled_optional_fields_and_retains_authored_intent(
    setup: Setup, normalization: str
) -> None:
    case = SDKCase(setup)
    custom = ToolSpec(
        name="helper",
        kind="custom",
        description="" if normalization == "empty_description" else "Help this workspace.",
        input_schema=None
        if normalization == "omitted_schema"
        else {"type": "object", "properties": {}},
    )
    tools = (*(spec().tools or ()), custom)
    servers: tuple[MCPConnection, ...] = ()
    if normalization == "builtin_annotations":
        tools += (
            ToolSpec(
                name="tool_search",
                kind="builtin",
                description="Authored host annotation.",
                input_schema={"type": "object"},
            ),
        )
    elif normalization == "toolset":
        tools += (ToolSpec(name="service", kind="mcp_toolset", description="Host annotation."),)
        servers = (MCPConnection(name="service", url="https://service.example.com/mcp"),)
    requested = spec().model_copy(update={"tools": tools, "mcp_servers": servers})
    case.trace.queue(
        reply("POST", "/agents", raw_agent(requested, case.scope)),
        reply("GET", "/agents/original", raw_agent(requested, case.scope)),
    )
    async with case.sdk() as sdk:
        host = case.host(sdk)
        await host.publish("source", requested, expected_revision=0, key="publish-with-annotations")
        read = await host.read("source")
    case.trace.assert_consumed()
    assert read.agent.spec == requested
    sent = case.trace.requests[0].json()
    assert isinstance(sent, dict)
    assert sent["tools"] == raw_agent(requested, case.scope)["tools"]


@pytest.mark.parametrize("field", ["description", "parameters"])
async def test_native_read_still_refuses_changes_to_consumed_custom_tool_fields(
    setup: Setup, field: str
) -> None:
    case = SDKCase(setup)
    requested = spec().model_copy(
        update={"tools": (ToolSpec(name="helper", kind="custom", description="", input_schema={}),)}
    )
    await case.seed_agent(requested)
    raw = raw_agent(requested, case.scope)
    raw["tools"] = [
        {
            "type": "function",
            "name": "helper",
            "description": "Different description." if field == "description" else "",
            "parameters": {"type": "string"} if field == "parameters" else {},
        }
    ]
    case.trace.queue(reply("GET", "/agents/original", raw))
    async with case.sdk() as sdk:
        with pytest.raises(CatalogConflict):
            await case.host(sdk).read("source")
    case.trace.assert_consumed()


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
async def test_unknown_catalog_commit_keeps_the_published_native_ref_and_restart_performs_no_mutation(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    case = SDKCase(setup)
    original = spec()
    desired = original.model_copy(update={"skills": (SkillRef(id="uploaded", version="1"),)})
    await case.seed_agent(original)
    case.trace.queue(
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("POST", "/skills", native_skill()),
        reply("GET", "/agents/original", raw_agent(original, case.scope)),
        reply("GET", "/skills/uploaded/versions/1", version()),
        reply("POST", "/agents", raw_agent(desired, case.scope, "replacement")),
        reply("GET", "/agents/replacement", raw_agent(desired, case.scope, "replacement")),
        reply("GET", "/skills/uploaded", native_skill()),
        reply("GET", "/agents/replacement", raw_agent(desired, case.scope, "replacement")),
    )
    async with case.sdk() as sdk:
        host = case.host(sdk)
        put = host.catalog.put_agent

        async def lost_ack(scope: Scope, catalog_id: str, agent: Agent, *, expected_revision: int):
            await put(scope, catalog_id, agent, expected_revision=expected_revision)
            raise failure("offline lost catalog commit acknowledgement")

        monkeypatch.setattr(host.catalog, "put_agent", lost_ack)
        with pytest.raises(failure):
            await add_host_agent_skill(host, catalog_id="source", bundle=bundle(), recheck=allow)
        recovered = await add_host_agent_skill(
            case.host(sdk, "restart"), catalog_id="source", bundle=bundle(), recheck=allow
        )
    case.trace.assert_consumed()
    assert recovered.action == "unchanged" and not recovered.newly_attached
    assert all(request.method == "GET" for request in case.trace.requests[6:])
    assert all(request.method != "DELETE" for request in case.trace.requests)
    current = await case.catalog.get_agent(case.scope, "source")
    assert current is not None and current.agent.ref.id == "replacement"


@pytest.mark.parametrize(
    "change",
    ["unused_null", "unused_extra", "instructions", "skills", "tools", "policy_name", "managed"],
)
async def test_native_read_checks_consumed_fields_without_gating_on_unused_fingerprint_data(
    setup: Setup, change: str
) -> None:
    case = SDKCase(setup)
    requested = spec()
    await case.seed_agent(requested)
    raw = raw_agent(requested, case.scope)
    if change == "unused_null":
        raw["description"] = None
    elif change == "unused_extra":
        raw["provider_future_metadata"] = {"unused": [None, "new field"]}
    elif change == "instructions":
        raw["instructions"] = "Different prompt."
    elif change == "skills":
        raw["metadata"] = {"mux_skill_pins": '[{"id":"unknown","version":"1"}]'}
    elif change == "tools":
        raw["tools"] = [{"type": "tool_search"}]
    elif change == "policy_name":
        raw["metadata"] = {"mux_skill_pins": "[]", "daimon_name": "foreign-alias"}
    elif change == "managed":
        raw["metadata"] = {
            "mux_skill_pins": "[]",
            "daimon_name": "source",
            "daimon_managed": "true",
        }
    case.trace.queue(reply("GET", "/agents/original", raw))
    async with case.sdk() as sdk:
        if change.startswith("unused_"):
            read = await case.host(sdk).read("source")
            assert read.agent.spec == requested
        else:
            with pytest.raises(CatalogConflict):
                await case.host(sdk).read("source")
    case.trace.assert_consumed()


@pytest.mark.parametrize(
    "scope_kind", ["platform", "legacy", "blank_principal", "blank_authorization"]
)
async def test_host_scope_requires_real_tenant_account_authorization_before_native_io(
    setup: Setup, scope_kind: str
) -> None:
    case = SDKCase(setup)
    scope = case.scope
    if scope_kind == "platform":
        scope = Scope.platform(reason="unrelated inventory")
    elif scope_kind == "legacy":
        scope = Scope.legacy_host_authorized(call_site="not-authorized-for-this-edge")
    elif scope_kind == "blank_principal":
        scope = scope.model_copy(update={"principal_id": " "})
    else:
        scope = scope.model_copy(update={"authorization_id": " "})
    async with case.sdk() as sdk:
        with pytest.raises(ScopeViolation):
            case.host(sdk, scope=scope)
    assert not case.trace.requests


@pytest.mark.parametrize("foreign", ["tenant", "actor_account"])
async def test_channel_rule_refuses_mismatched_resource_scope_before_lookup(
    setup: Setup, foreign: str
) -> None:
    case = SDKCase(setup)
    legacy_trace = ScriptedTransport()
    async with case.sdk() as sdk, legacy_trace.client() as legacy:
        with pytest.raises(ScopeViolation):
            await set_channel_rule(
                legacy,
                case.sessions,
                tenant_id=uuid.UUID(
                    case.foreign.tenant_id if foreign == "tenant" else case.scope.tenant_id
                ),
                actor_account_id=uuid.UUID(
                    case.foreign.account_id if foreign == "actor_account" else case.scope.account_id
                ),
                platform="discord",
                channel_id="123",
                subject=Subject(is_admin=True),
                default=DeploymentDefault(agent_name="source"),
                readers="own",
                resource_host=case.host(sdk),
            )
    assert not case.trace.requests and not legacy_trace.requests


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_ADMIN_KEY",
        "OPENAI_CUSTOM_HEADERS",
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "OPENAI_FUTURE_TEST_SETTING",
        "anthropic_future_test_setting",
        "GOOGLE_FUTURE_TEST_SETTING",
        "Gemini_future_test_setting",
    ],
)
@pytest.mark.parametrize("value", ["", "ambient-test-value"])
async def test_host_sdk_refuses_ambient_configuration_before_client_allocation(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    case = SDKCase(setup)
    monkeypatch.setenv(name, value)
    before = frozenset(os.environ)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("ambient configuration reached HTTP client allocation")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(AmbientSDKConfigurationError):
        case.sdk()
    assert frozenset(os.environ) == before
    assert not case.trace.requests


@pytest.mark.parametrize(
    "name",
    ["OPENAI_ADMIN_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS"],
)
@pytest.mark.parametrize("value", ["", "ambient-test-value"])
async def test_product_transport_sdk_copy_refuses_late_ambient_configuration(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    case = SDKCase(setup)
    async with case.sdk() as sdk:
        monkeypatch.setenv(name, value)
        before = frozenset(os.environ)
        with pytest.raises(AmbientSDKConfigurationError):
            SDKTransport(sdk)
        assert frozenset(os.environ) == before
    assert not case.trace.requests


async def test_read_uses_declared_projection_with_one_actual_sdk_request(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = SDKCase(setup)
    original = spec()
    await case.seed_agent(original)
    altered = PERSISTENT_WORKSPACE.model_copy(update={"tool_readback": "authored"})
    monkeypatch.setattr(profiles, "PROFILES", {altered.profile_id: altered})
    case.trace.queue(reply("GET", "/agents/original", raw_agent(original, case.scope)))
    async with case.sdk() as sdk:
        with pytest.raises(CatalogConflict, match="native agent differs"):
            await case.host(sdk).read("source")
    assert [(call.method, call.path) for call in case.trace.requests] == [
        ("GET", "/v1/agents/original")
    ]
    case.trace.assert_consumed()


async def test_disabled_catalog_declaration_refuses_every_edge_before_io(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.channel_skills import NativeSkillDeleteGuard

    case = SDKCase(setup)
    disabled = PERSISTENT_WORKSPACE.model_copy(
        update={"support": {**PERSISTENT_WORKSPACE.support, "neutral_catalog": "unsupported"}}
    )
    monkeypatch.setattr(profiles, "PROFILES", {disabled.profile_id: disabled})
    async with case.sdk() as sdk:
        with pytest.raises(UnsupportedCapability):
            case.host(sdk)
    with pytest.raises(ValueError, match="alternate provider"):
        PostgresNeutralCatalog(case.sessions, provider="openai", account_scope_id="project")
    with pytest.raises(ScopeViolation):
        NativeSkillDeleteGuard(
            scope=case.scope, provider="openai", account_scope_id="project", operator_enabled=True
        ).require_enabled()
    assert not case.trace.requests


async def publish_worker(host, worker, persistence, key, source, guard):
    return await persistence.run(
        lambda: host.publish(
            key,
            worker.spec,
            expected_revision=0,
            key=key,
            persistence=persistence,
            guard=guard,
            worker=worker,
            source=source,
        )
    )
