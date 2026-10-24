"""Gemini host edits use the real driver, SDK and retained Postgres catalog.

Driver Storage/StateStore are explicit test fixtures. These tests prove inline
interaction requests, not production process-restart persistence or live turns.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from daimon.core import agent_fork, channel_rules
from daimon.core.agent_fork import fork_host_agent
from daimon.core.authz import Subject
from daimon.core.channel_rules import set_channel_rule
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_CHANNEL_COPY,
    MA_METADATA_KEY_NAME,
    tenant_scoped_display_title,
)
from daimon.core.neutral_resources import NeutralResourceHost, skill_upload
from daimon.core.permissions import channel_rule
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.add import add_host_agent_skill
from daimon.core.skills.ingest import SkillBundle, bundle_from_files, bundle_from_markdown
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope, SkillRef
from mux.contracts.resources import Agent, AgentSpec, Session, SessionSpec, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.transport import SDKTransport
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from mux.testing.drivers.gemini.fake import MemoryStorage
from mux.testing.isolated_sdk import (
    TLS_PROXY_ENV_VARS,
    AmbientSDKConfigurationError,
    build_gemini_client,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

type Setup = tuple[async_sessionmaker[AsyncSession], Scope, Scope]


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


async def allow(_agent: Agent) -> None:
    pass


def bundle(name: str = "notes", body: str = "Write notes.") -> SkillBundle:
    return bundle_from_markdown(f"---\nname: {name}\ndescription: Work carefully\n---\n{body}\n")


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_artwork(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(agent_fork, "queue_agent_face", no_artwork)


def spec(name: str = "source", pins: tuple[SkillRef, ...] = ()) -> AgentSpec:
    return AgentSpec(
        name=name,
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        system="Help this workspace.",
        tools=(
            ToolSpec(name="google_search", kind="builtin"),
            ToolSpec(name="helper", kind="custom", description="", input_schema={}),
        ),
        skills=pins,
        metadata={MA_METADATA_KEY_NAME: name},
    )


class SDKCase:
    def __init__(self, fixture: Setup) -> None:
        self.sessions, self.scope, self.foreign = fixture
        self.trace = ScriptedTransport()
        self.headers: list[str] = []
        self.storage = MemoryStorage()
        self.state = MemoryStateStore()
        self.catalog = PostgresNeutralCatalog(
            self.sessions, provider="gemini", account_scope_id="project"
        )

    @asynccontextmanager
    async def driver(self) -> AsyncIterator[GeminiManagedAgents]:
        def handle(request: httpx.Request) -> httpx.Response:
            self.headers.append(request.headers["Api-Revision"])
            return self.trace.dispatch(request)

        client = build_gemini_client(
            api_key="offline-fixture",
            base_url="https://gemini.invalid",
            transport=httpx.MockTransport(handle),
        )
        try:
            yield GeminiManagedAgents(
                SDKTransport(client),
                storage=self.storage,
                state_store=self.state,
                account_scope_id="project",
            )
        finally:
            client.close()
            await client.aio.aclose()

    def host(self, driver: GeminiManagedAgents, key: str = "claimed-add") -> NeutralResourceHost:
        return NeutralResourceHost(
            driver,
            self.sessions,
            scope=self.scope,
            account_scope_id="project",
            operation_key=key,
        )

    async def seed(self, driver: GeminiManagedAgents, requested: AgentSpec) -> Agent:
        return (
            await self.host(driver).publish(
                requested.name, requested, expected_revision=0, key="seed:" + requested.name
            )
        ).agent

    async def retain(
        self, driver: GeminiManagedAgents, owned_by: str, data: SkillBundle, key: str
    ) -> SkillRef:
        native = await driver.skills.create(self.scope, skill_upload(data), key=key)
        pin = native.latest_version
        assert pin is not None and pin.version is not None
        await self.catalog.put_skill(
            self.scope,
            skill_id=pin.id,
            version=pin.version,
            agent_name=owned_by,
            display_title=tenant_scoped_display_title(
                tenant_id=uuid.UUID(self.scope.tenant_id),
                name=data.preview.name,
                agent_name=None if owned_by == "@catalog" else owned_by,
            ),
            bundle=data,
        )
        return pin

    async def session(self, driver: GeminiManagedAgents, agent: Agent, key: str) -> Session:
        return await driver.sessions.create(
            self.scope,
            SessionSpec(
                agent=agent.ref,
                agent_revision=agent.revision,
                config_revision=1,
                extensions={
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={
                            "thread": {
                                "channel": {
                                    "tenant_id": self.scope.tenant_id,
                                    "platform": "discord",
                                    "channel_id": key,
                                },
                                "thread_id": key,
                            },
                            "binding_id": key,
                        },
                    )
                },
            ),
            key=key,
        )

    async def send(self, driver: GeminiManagedAgents, session: Session, key: str) -> None:
        stamp = datetime.now(UTC).isoformat()
        self.trace.queue(
            ScriptedReply(
                "POST",
                "/v1beta/interactions",
                httpx.Response(
                    200,
                    json={
                        "id": key,
                        "created": stamp,
                        "updated": stamp,
                        "status": "completed",
                        "environment_id": "workspace:" + key,
                        "steps": [],
                        "usage": None,
                    },
                ),
            )
        )
        await driver.events.send(
            self.scope,
            session.ref,
            (UserMessage(content=(TextPart(text="Use the retained skills."),)),),
            key=key,
        )


def sources(case: SDKCase, index: int) -> dict[str, str]:
    body = json.loads(case.trace.requests[index].body)
    assert body["background"] is True and body["store"] is True
    assert body["agent_config"] == {"type": "antigravity", "model": "gemini-3.8-flash"}
    return {source["target"]: source["content"] for source in body["environment"]["sources"]}


async def test_add_replaces_inline_agent_and_old_session_keeps_its_exact_bundle(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    async with case.driver() as driver:
        original = bundle(body="Original notes.")
        pin = await case.retain(driver, "source", original, "original-skill")
        before = await case.seed(driver, spec(pins=(pin,)))
        old_session = await case.session(driver, before, "old-session")
        changed = bundle(body="Updated notes.")
        result = await add_host_agent_skill(
            case.host(driver), catalog_id="source", bundle=changed, recheck=allow
        )
        assert result.action == "updated" and not result.newly_attached
        current = await case.host(driver).read("source")
        pins = current.agent.spec.skills
        assert pins
        updated = pins[0]
        assert current.local_revision == 2 and current.agent.ref != before.ref
        assert updated.id == pin.id and updated.version != pin.version
        assert updated.digest == updated.version and updated.source == pin.source
        retained = await case.catalog.get_skill(case.scope, updated.id, updated.version or "")
        assert retained is not None and retained.bundle == changed
        new_session = await case.session(driver, current.agent, "new-session")
        await case.send(driver, old_session, "old-turn")
        await case.send(driver, new_session, "new-turn")
    case.trace.assert_consumed()
    mount = f".agents/skills/{pin.id}/SKILL.md"
    assert sources(case, 0)[mount].endswith("Original notes.\n")
    assert sources(case, 1)[mount].endswith("Updated notes.\n")
    assert case.headers == ["2026-05-20", "2026-05-20"]


async def test_fork_clones_retained_utf8_files_keeps_shared_pins_and_omits_foreign(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    data = bundle_from_files(
        {
            "SKILL.md": b"---\nname: notes\ndescription: Work carefully\n---\nRetained notes.\n",
            "scripts/notes.py": b"print('exact UTF-8')\n",
            "references/settings.json": b'{"enabled":true}\n',
        }
    )
    async with case.driver() as driver:
        own = await case.retain(driver, "source", data, "own")
        shared = await case.retain(driver, "@catalog", bundle("library"), "shared")
        foreign = await case.retain(driver, "other-agent", bundle("foreign"), "foreign")
        await case.seed(driver, spec(pins=(own, shared, foreign)))
        await case.seed(driver, spec("other-agent"))
        result = await fork_host_agent(
            case.host(driver, "claimed-copy"),
            case.sessions,
            source_name="source",
            new_name="copy",
            subject=Subject(is_admin=True),
            default_agent_name=None,
        )
        pins = result.agent.revision.agent.spec.skills or ()
        clone = next(pin for pin in pins if pin.id != shared.id)
        assert shared in pins and clone.id != own.id and foreign not in pins
        assert result.copied_skills == ("copy/notes",) and result.dropped_skills == ("foreign",)
        assert ToolSpec(name="code_execution", kind="builtin") in (
            result.agent.revision.agent.spec.tools or ()
        )
        retained = await case.catalog.get_skill(case.scope, clone.id, clone.version or "")
        assert retained is not None and retained.bundle == data
        session = await case.session(driver, result.agent.revision.agent, "copied-session")
        await case.send(driver, session, "copied-turn")
    case.trace.assert_consumed()
    mounted = sources(case, 0)
    assert mounted[f".agents/skills/{clone.id}/scripts/notes.py"] == "print('exact UTF-8')\n"
    assert mounted[f".agents/skills/{clone.id}/references/settings.json"] == '{"enabled":true}\n'
    assert not any(own.id in target or foreign.id in target for target in mounted)


async def test_binary_inline_add_refuses_before_catalog_or_native_agent_mutation(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    async with case.driver() as driver:
        before = await case.seed(driver, spec())
        data = bundle_from_files(
            {
                "SKILL.md": b"---\nname: notes\ndescription: Work carefully\n---\nNotes.\n",
                "references/data.bin": b"\x00\xff",
            }
        )
        with pytest.raises(UnsupportedCapability) as refused:
            await add_host_agent_skill(
                case.host(driver), catalog_id="source", bundle=data, recheck=allow
            )
        assert refused.value.missing == ("skill_bundle_gcs_deployment",)
        current = await case.catalog.get_agent(case.scope, "source")
        assert current is not None and current.local_revision == 1 and current.agent == before
        assert not await case.catalog.list_skills(case.scope)
    assert not case.trace.requests


async def test_unsupported_native_delete_leaves_active_catalog_revision_intact(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    async with case.driver() as driver:
        before = await case.seed(driver, spec())
        with pytest.raises(UnsupportedCapability) as refused:
            await case.host(driver).retire("source")
        assert refused.value.missing == ("agent_delete",)
        current = await case.host(driver).read("source")
        assert current.local_revision == 1 and current.agent == before
    assert not case.trace.requests


async def test_foreign_agent_ref_refuses_before_catalog_publication(setup: Setup) -> None:
    case = SDKCase(setup)
    async with case.driver() as driver:
        foreign = await driver.agents.create(case.foreign, spec(), key="foreign")
        with pytest.raises(ScopeViolation):
            case.host(driver).check(foreign)
        assert not await case.catalog.list_agents(case.scope)
    assert not case.trace.requests


async def test_actual_channel_rule_fork_retains_utf8_skills_and_repeat_reuses_the_copy(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    anthropic = ScriptedTransport()
    async with case.driver() as driver, anthropic.client() as legacy:
        own = await case.retain(driver, "source", bundle(), "own")
        shared = await case.retain(driver, "@catalog", bundle("library"), "shared")
        foreign = await case.retain(driver, "other-agent", bundle("foreign"), "foreign")
        await case.seed(driver, spec(pins=(own, shared, foreign)))
        await case.seed(driver, spec("other-agent"))
        host = case.host(driver, "claimed-channel-copy")
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
        assert changed.copied_from == "source" and changed.agent_name == "team-alpha"
        assert changed.dropped_skills == ("foreign",)
        assert not repeated.changed and repeated.copied_from is None
        copy = await host.read("team-alpha")
        assert copy.local_revision == 1 and copy.agent.revision.local == 1
        assert (copy.agent.spec.metadata or {})[MA_METADATA_KEY_CHANNEL_COPY] == "123"
        pins = copy.agent.spec.skills or ()
        assert shared in pins and own not in pins and foreign not in pins
        await case.send(driver, await case.session(driver, copy.agent, "channel-copy"), "copy-turn")
    anthropic.assert_consumed()
    assert not anthropic.requests
    case.trace.assert_consumed()
    mounted = sources(case, 0)
    assert not any(own.id in target or foreign.id in target for target in mounted)
    async with case.sessions() as session:
        policy = await load_access_policy(session, tenant_id=uuid.UUID(case.scope.tenant_id))
        assert channel_rule(policy, "123").readers == "own"
        _, channels = await list_propagations_for_tenant(
            session, tenant_id=uuid.UUID(case.scope.tenant_id)
        )
        assert any(row.channel_id == "123" and row.agent_name == "team-alpha" for row in channels)


async def test_failed_channel_write_cleanup_keeps_gemini_catalog_and_native_identity(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = SDKCase(setup)
    anthropic = ScriptedTransport()
    async with case.driver() as driver, anthropic.client() as legacy:
        await case.seed(driver, spec())

        async def fail_write(*args: object, **kwargs: object) -> None:
            raise OSError("offline channel write failed")

        monkeypatch.setattr(channel_rules, "set_fields", fail_write)
        with pytest.raises(OSError, match="offline channel write failed"):
            await set_channel_rule(
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
                resource_host=case.host(driver),
                actor_account_id=uuid.UUID(case.scope.account_id),
            )
        current = await case.host(driver).read("team-alpha")
        assert current.local_revision == 1 and current.agent.lifecycle == "active"
    assert not anthropic.requests and not case.trace.requests


async def test_rebuilt_driver_and_host_reuse_the_retained_add_without_a_second_agent(
    setup: Setup,
) -> None:
    case = SDKCase(setup)
    async with case.driver() as driver:
        await case.seed(driver, spec())
        first = await add_host_agent_skill(
            case.host(driver), catalog_id="source", bundle=bundle(), recheck=allow
        )
        assert first.action == "created" and first.newly_attached
        before = await case.host(driver).read("source")
    case.state = case.state.restart()
    async with case.driver() as driver:
        repeated = await add_host_agent_skill(
            case.host(driver, "claimed-repeated-add"),
            catalog_id="source",
            bundle=bundle(),
            recheck=allow,
        )
        assert repeated.action == "unchanged" and not repeated.newly_attached
        after = await case.host(driver).read("source")
        assert after == before and len(await case.catalog.list_skills(case.scope)) == 1
        await case.send(
            driver, await case.session(driver, after.agent, "repeat-session"), "repeat-turn"
        )
    case.trace.assert_consumed()
    assert len(sources(case, 0)) == 1


@pytest.mark.parametrize(
    "name",
    [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GEMINI_API_KEY",
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

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(AmbientSDKConfigurationError):
        async with case.driver():
            raise AssertionError("ambient configuration reached the product driver")
    assert frozenset(os.environ) == before
    assert not case.trace.requests


@pytest.mark.parametrize("name", TLS_PROXY_ENV_VARS)
@pytest.mark.parametrize("empty", [False, True])
async def test_host_sdk_refuses_tls_proxy_and_keylog_before_any_client_effect(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, empty: bool
) -> None:
    from google import genai

    case = SDKCase(setup)
    keylog = tmp_path / "must-not-create-keylog"
    value = "" if empty else str(keylog)
    monkeypatch.setenv(name, value)
    before = frozenset(os.environ)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("hostile TLS/proxy configuration reached SDK or HTTP construction")

    monkeypatch.setattr(genai, "Client", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(httpx, "create_ssl_context", forbidden)
    with pytest.raises(AmbientSDKConfigurationError):
        async with case.driver():
            raise AssertionError("hostile configuration reached the resource driver")
    assert frozenset(os.environ) == before
    assert os.environ[name] == value
    assert not keylog.exists()
    assert not case.trace.requests
