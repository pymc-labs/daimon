"""Real catalog/channel references and native drivers; no fabricated database state."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from daimon.adapters.cli.commands.skills import delete_skill
from daimon.adapters.cli.runtime import CliRuntime
from daimon.adapters.cli.tenant import TenantSelector
from daimon.core import channel_skills as product
from daimon.core.channel_skills import (
    NativeSkillDeleteGuard,
    NativeSkillDeleteUnavailable,
    NativeSkillStillAttached,
    refuse_native_skill_delete,
)
from daimon.core.config import Settings
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import StoreError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.neutral_resources import skill_upload
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.ingest import SkillBundle, bundle_from_markdown
from daimon.core.stores.channel_skills import add_channel_skill
from daimon.core.stores.neutral_catalog import (
    AgentRevision,
    CatalogConflict,
    PostgresNeutralCatalog,
)
from daimon.testing.admin_bindings import AdminBindings, AdminCommand
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.skill_delete_bindings import SkillDeleteAdminBindings
from mux.contracts.ids import ModelRef, Scope, SkillRef
from mux.contracts.ports import ManagedAgents
from mux.contracts.resources import AgentSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ScopeViolation
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma import MARouter
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.isolated_sdk import build_async_openai
from openai import AsyncOpenAI
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytest_plugins = ["daimon.testing.db"]
Provider = Literal["openai", "gemini"]
MARKDOWN = """---
name: qa-echo-skill
description: QA fixture skill. When asked for the QA echo phrase, reply ECHO-SKILL-OK.
---

# qa-echo-skill

When the user asks for "the QA echo phrase", reply exactly `ECHO-SKILL-OK`.
"""
FROZEN = Path(__file__).resolve().parents[3] / "tests/judge/fixtures/admin_operations.json"


@dataclass
class Case:
    sessions: async_sessionmaker[AsyncSession]
    scope: Scope
    foreign: Scope
    provider: Provider
    backend: ManagedAgents
    catalog: PostgresNeutralCatalog
    trace: ScriptedTransport
    gemini_trace: FakeTransport
    bundle: SkillBundle
    pin: SkillRef
    sdk: AsyncOpenAI | None
    binding: AdminBindings

    def guard(self, **updates: Any) -> NativeSkillDeleteGuard:
        return replace(
            NativeSkillDeleteGuard(self.scope, self.provider, "qa-workspace", True), **updates
        )

    async def publish(
        self,
        *,
        pins: tuple[SkillRef, ...] | None = None,
        expected: int = 0,
        catalog_id: str = "fork",
    ) -> AgentRevision:
        spec = AgentSpec(
            name="qa-fork",
            model=ModelRef(
                provider=self.provider,
                id="gpt-6-luna" if self.provider == "openai" else "gemini-3.8-flash",
            ),
            skills=(self.pin,) if pins is None else pins,
            metadata={"daimon_name": "qa-fork"},
        )
        native = await self.backend.agents.create(self.scope, spec, key=f"{catalog_id}:{expected}")
        return await self.catalog.put_agent(
            self.scope, catalog_id, native, expected_revision=expected
        )

    async def check(
        self, guard: NativeSkillDeleteGuard | None = None, name: str = "qa-echo-skill"
    ) -> None:
        recorded = tuple(self.trace.requests)
        with pytest.raises(NativeSkillStillAttached) as caught:
            await refuse_native_skill_delete(self.sessions, guard=guard or self.guard(), skill=name)
        assert caught.value.skill_id == self.pin.id
        assert tuple(self.trace.requests) == recorded and not self.gemini_trace.requests
        assert not any(r.method == "DELETE" for r in self.trace.requests)
        retained = await self.backend.skills.retrieve(self.scope, self.pin.id)
        assert retained.id == self.pin.id

    def runtime(self) -> CliRuntime:
        settings = Settings(
            _env_file=None,
            _env_prefix="N6_A2_UNUSED_",
            database={"url": "postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n6"},
            anthropic={"api_key": "offline-fixture"},
            cli={"local_user": "qa-native-operator"},
        )
        return CliRuntime(
            settings=settings,
            anthropic=self.binding.anthropic,
            sessionmaker=self.sessions,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )


@pytest.fixture(params=["openai", "gemini"])
async def case(
    request: pytest.FixtureRequest, db_engine: AsyncEngine, db_clean: None
) -> AsyncIterator[Case]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        if (
            db_engine.url.database == "daimon_test"
            and db_engine.url.host in ("localhost", "127.0.0.1")
            and not db_engine.url.query
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip(
                "native delete proofs require an owned lane DB or registered isolated CI schema"
            )
        raise
    owned = await provision_tenant(sessions, platform="discord", workspace_id="123456789012345678")
    other = await provision_tenant(sessions, platform="discord", workspace_id="923456789012345678")
    scope = Scope(
        tenant_id=str(owned.tenant_id),
        account_id=str(owned.account_id),
        principal_id="qa-operator",
        authorization_id="qa-explicit-operator",
    )
    foreign = Scope(
        tenant_id=str(other.tenant_id),
        account_id=str(other.account_id),
        principal_id="qa-operator",
        authorization_id="qa-explicit-operator",
    )
    provider = cast(Provider, request.param)
    router, gemini_trace = MARouter(), FakeTransport()
    skills: dict[str, dict[str, object]] = {}
    agents: dict[str, dict[str, object]] = {}

    def create_skill(req: httpx.Request, match: object) -> httpx.Response:
        sid = f"skill-{len(skills) + 1}"
        row = {
            "id": sid,
            "name": "qa-echo-skill",
            "created_at": 0,
            "latest_version": "1",
            "default_version": "1",
        }
        skills[sid] = row
        return httpx.Response(200, json=row)

    def create_agent(req: httpx.Request, match: object) -> httpx.Response:
        aid = f"agent-{len(agents) + 1}"
        row = {"id": aid, "created_at": 0, "tools": [], **json.loads(req.content)}
        agents[aid] = row
        return httpx.Response(200, json=row)

    router.add("POST", r"/v1/skills", create_skill)
    router.add("POST", r"/v1/agents", create_agent)
    router.add(
        "GET",
        r"/v1/skills/(?P<id>[^/]+)/versions/(?P<v>[^/]+)",
        lambda req, m: httpx.Response(
            200,
            json={
                "skill_id": m.group("id"),
                "version": m.group("v"),
                "name": "qa-echo-skill",
                "created_at": 0,
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/skills/(?P<id>[^/]+)",
        lambda req, m: httpx.Response(200, json=skills[m.group("id")]),
    )
    router.add(
        "GET",
        r"/v1/agents/(?P<id>[^/]+)",
        lambda req, m: httpx.Response(200, json=agents[m.group("id")]),
    )
    trace = ScriptedTransport(router=router)
    sdk = None
    if provider == "openai":
        sdk = build_async_openai(
            transport=httpx.MockTransport(trace.dispatch), base_url="https://openai.invalid/v1"
        )
        backend = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="qa-workspace",
            authorization=lambda authorized, kind, id_: authorized in (scope, foreign),
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
    else:
        backend = GeminiManagedAgents(
            gemini_trace,
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id="qa-workspace",
        )
    bundle = bundle_from_markdown(MARKDOWN)
    uploaded = await backend.skills.create(scope, skill_upload(bundle), key="qa-upload")
    assert uploaded.latest_version is not None
    catalog = PostgresNeutralCatalog(sessions, provider=provider, account_scope_id="qa-workspace")
    await catalog.put_skill(
        scope,
        skill_id=uploaded.id,
        version=uploaded.latest_version.version or "",
        agent_name="qa-fork",
        display_title=tenant_scoped_display_title(
            tenant_id=owned.tenant_id, name=bundle.preview.name, agent_name="qa-fork"
        ),
        bundle=bundle,
    )
    binding = AdminBindings(
        sessions=sessions,
        scope=scope,
        backend=provider,
        platform="discord",
        workspace_id="123456789012345678",
        channels=frozenset({"223456789012345678"}),
        trace=trace,
    )
    bound = Case(
        sessions,
        scope,
        foreign,
        provider,
        backend,
        catalog,
        trace,
        gemini_trace,
        bundle,
        uploaded.latest_version,
        sdk,
        binding,
    )
    try:
        yield bound
    finally:
        await binding.close()
        if sdk is not None:
            await sdk.close()


async def test_real_native_agent_pin_refuses_deletion_and_keeps_resource(case: Case) -> None:
    await case.publish()
    await case.check()
    latest = await case.catalog.get_agent(case.scope, "fork")
    assert latest is not None and latest.agent.spec.skills == (case.pin,)


async def test_retained_prior_revision_is_not_mistaken_for_detachment(case: Case) -> None:
    await case.publish()
    await case.publish(pins=(), expected=1)
    latest = await case.catalog.get_agent(case.scope, "fork")
    assert latest is not None and latest.local_revision == 2 and latest.agent.spec.skills == ()
    await case.check()


async def test_channel_reference_without_an_agent_pin_refuses_delete(case: Case) -> None:
    await case.publish(pins=())
    async with case.sessions() as session, session.begin():
        await add_channel_skill(
            session,
            tenant_id=uuid.UUID(case.scope.tenant_id),
            platform="discord",
            channel_id="223456789012345678",
            skill_id=case.pin.id,
            version=case.pin.version or "",
            name=case.bundle.preview.name,
            owner_agent_name=None,
            actor_account_id=uuid.UUID(case.scope.account_id),
        )
    await case.check()


async def test_unattached_delete_never_claims_unsupported_provider_mutation(case: Case) -> None:
    await case.publish(pins=())
    requests = tuple(case.trace.requests)
    with pytest.raises(NativeSkillDeleteUnavailable) as caught:
        await refuse_native_skill_delete(case.sessions, guard=case.guard(), skill=case.pin.id)
    assert caught.value.reason == "unattached_delete_unbound"
    assert tuple(case.trace.requests) == requests and not case.gemini_trace.requests
    assert (
        await case.catalog.get_skill(case.scope, case.pin.id, case.pin.version or "")
    ) is not None


@pytest.mark.parametrize(
    "change", ["account", "tenant", "workspace", "provider", "platform_scope", "legacy_scope"]
)
async def test_foreign_scope_or_catalog_refuses_before_native_requests(
    case: Case, change: str
) -> None:
    await case.publish()
    guard = case.guard()
    if change == "account":
        guard = replace(
            guard, scope=case.scope.model_copy(update={"account_id": case.foreign.account_id})
        )
    elif change == "tenant":
        guard = replace(
            guard, scope=case.scope.model_copy(update={"tenant_id": case.foreign.tenant_id})
        )
    elif change == "workspace":
        guard = replace(guard, account_scope_id="foreign-workspace")
    elif change == "provider":
        guard = replace(guard, provider="gemini" if case.provider == "openai" else "openai")
    elif change == "platform_scope":
        guard = replace(guard, scope=Scope.platform(reason="not allowed"))
    else:
        guard = replace(guard, scope=Scope.legacy_host_authorized(call_site="not-allowed"))
    requests = tuple(case.trace.requests)
    with pytest.raises((ScopeViolation, StoreError)):
        await refuse_native_skill_delete(case.sessions, guard=guard, skill=case.pin.id)
    assert tuple(case.trace.requests) == requests and not case.gemini_trace.requests


async def test_explicit_stale_cas_snapshot_cannot_authorize_deletion(case: Case) -> None:
    await case.publish()
    with pytest.raises(NativeSkillDeleteUnavailable) as caught:
        await refuse_native_skill_delete(
            case.sessions,
            guard=case.guard(expected_agent_revisions=(("fork", 999),)),
            skill=case.pin.id,
        )
    assert caught.value.reason == "catalog_changed"
    await case.check()


async def test_actual_concurrent_catalog_writes_preserve_cas_winner(case: Case) -> None:
    await case.publish()
    original = await case.catalog.get_agent(case.scope, "fork")
    assert original is not None
    results = await asyncio.gather(
        case.catalog.put_agent(case.scope, "fork", original.agent, expected_revision=1),
        case.catalog.put_agent(case.scope, "fork", original.agent, expected_revision=1),
        return_exceptions=True,
    )
    assert sum(isinstance(r, AgentRevision) for r in results) == 1
    assert sum(isinstance(r, CatalogConflict) for r in results) == 1
    await case.check()
    winner = await case.catalog.get_agent(case.scope, "fork")
    assert (
        winner is not None and winner.local_revision == 2 and winner.agent.ref == original.agent.ref
    )


async def test_attach_commits_while_delete_inspects_history(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    await case.publish(pins=())
    started, release = asyncio.Event(), asyncio.Event()
    original = PostgresNeutralCatalog.get_agent
    paused = False

    async def intercepted(
        self: PostgresNeutralCatalog, scope: Scope, id_: str, *, local_revision: int | None = None
    ) -> AgentRevision | None:
        nonlocal paused
        row = await original(self, scope, id_, local_revision=local_revision)
        if local_revision is not None and not paused:
            paused = True
            started.set()
            await release.wait()
        return row

    monkeypatch.setattr(PostgresNeutralCatalog, "get_agent", intercepted)
    task = asyncio.create_task(
        refuse_native_skill_delete(case.sessions, guard=case.guard(), skill=case.pin.id)
    )
    try:
        await asyncio.wait_for(started.wait(), 30)
        await case.publish(expected=1)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        release.set()
    with pytest.raises(NativeSkillDeleteUnavailable) as caught:
        await task
    assert caught.value.reason == "catalog_changed"
    await case.check()


async def test_channel_attach_during_inspection_cannot_race_a_delete(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    await case.publish(pins=())
    original = product.list_channel_skills
    calls = 0

    async def intercepted(
        session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str | None = None
    ):
        nonlocal calls
        rows = await original(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
        )
        calls += 1
        if calls == 1:
            async with case.sessions() as writer, writer.begin():
                await add_channel_skill(
                    writer,
                    tenant_id=tenant_id,
                    platform=platform,
                    channel_id="223456789012345678",
                    skill_id=case.pin.id,
                    version=case.pin.version or "",
                    name="qa-echo-skill",
                    owner_agent_name=None,
                    actor_account_id=uuid.UUID(case.scope.account_id),
                )
        return rows

    monkeypatch.setattr(product, "list_channel_skills", intercepted)
    with pytest.raises(NativeSkillDeleteUnavailable) as caught:
        await refuse_native_skill_delete(case.sessions, guard=case.guard(), skill=case.pin.id)
    assert caught.value.reason == "catalog_changed"
    await case.check()


async def test_native_cli_branch_refuses_attached_with_no_anthropic_fallback(case: Case) -> None:
    await case.publish()
    requests = tuple(case.trace.requests)
    output = StringIO()
    with pytest.raises(NativeSkillStillAttached, match="Skill is still attached"):
        await delete_skill(
            case.runtime(),
            Console(file=output),
            name="qa-echo-skill",
            yes=True,
            selector=TenantSelector(guild_id=case.binding.workspace_id),
            native_guard=case.guard(),
        )
    assert tuple(case.trace.requests) == requests and not output.getvalue()


async def test_native_cli_foreign_selected_tenant_is_refused(case: Case) -> None:
    await case.publish()
    with pytest.raises(ScopeViolation, match="CLI destination"):
        await delete_skill(
            case.runtime(),
            Console(file=StringIO()),
            name="qa-echo-skill",
            yes=True,
            selector=TenantSelector(guild_id="923456789012345678"),
            native_guard=case.guard(),
        )


async def test_admin_binding_matches_exact_frozen_attached_refusal(case: Case) -> None:
    await case.publish()
    row = next(
        r
        for r in json.loads(FROZEN.read_text())["scenarios"]
        if r["scenario_id"] == "QA-NEW19-SKILL-DELETE-WHILE-ATTACHED"
    )
    operation = next(
        op["source"] for op in row["operations"] if " delete " in str(op["source"]["args"])
    )
    case.binding.resource_edge = SkillDeleteAdminBindings(
        case.binding, provider_workspace="qa-workspace", operator_enabled=True
    )
    requests = tuple(case.trace.requests)
    receipt = await case.binding.execute(operation, {})
    assert receipt.status == "refused" and receipt.code == "ADMIN_NATIVE_SKILL_STILL_ATTACHED"
    assert receipt.observations["exit_code"] in operation["allow_fail_exit_codes"]
    assert re.search(operation["allow_fail_pattern"], str(receipt.observations["message"]))
    assert receipt.observations["deleted"] is False and tuple(case.trace.requests) == requests
    assert receipt.observations["agent_revisions"] == [["fork", 1]]


async def test_operator_capability_defaults_false_in_product_and_admin(case: Case) -> None:
    await case.publish()
    guard = NativeSkillDeleteGuard(case.scope, case.provider, "qa-workspace")
    with pytest.raises(NativeSkillDeleteUnavailable) as caught:
        await refuse_native_skill_delete(case.sessions, guard=guard, skill=case.pin.id)
    assert caught.value.reason == "guard_disabled"
    edge = SkillDeleteAdminBindings(case.binding, provider_workspace="qa-workspace")
    receipt = await edge(
        AdminCommand(
            "skills_delete",
            "discord",
            case.binding.workspace_id,
            args=("qa-echo-skill",),
            options={"--yes": "true"},
        )
    )
    assert (
        receipt.status == "pending" and receipt.code == "ADMIN_NATIVE_SKILL_DELETE_GUARD_DISABLED"
    )


async def test_foreign_catalog_skill_id_is_not_resolved_from_the_owned_scope(case: Case) -> None:
    uploaded = await case.backend.skills.create(
        case.foreign, skill_upload(case.bundle), key="foreign-upload"
    )
    assert uploaded.latest_version is not None
    await case.catalog.put_skill(
        case.foreign,
        skill_id=uploaded.id,
        version=uploaded.latest_version.version or "",
        agent_name="foreign-agent",
        display_title=tenant_scoped_display_title(
            tenant_id=uuid.UUID(case.foreign.tenant_id),
            name=case.bundle.preview.name,
            agent_name="foreign-agent",
        ),
        bundle=case.bundle,
    )
    recorded = tuple(case.trace.requests)
    with pytest.raises(StoreError, match="No unique skill"):
        await refuse_native_skill_delete(case.sessions, guard=case.guard(), skill=uploaded.id)
    assert tuple(case.trace.requests) == recorded
    assert (
        await case.catalog.get_skill(
            case.foreign, uploaded.id, uploaded.latest_version.version or ""
        )
        is not None
    )


async def test_missing_retained_history_fails_closed(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    await case.publish()
    await case.publish(pins=(), expected=1)
    original = PostgresNeutralCatalog.get_agent

    async def missing_history(
        self: PostgresNeutralCatalog, scope: Scope, id_: str, *, local_revision: int | None = None
    ) -> AgentRevision | None:
        if local_revision == 1:
            return None
        return await original(self, scope, id_, local_revision=local_revision)

    monkeypatch.setattr(PostgresNeutralCatalog, "get_agent", missing_history)
    recorded = tuple(case.trace.requests)
    with pytest.raises(NativeSkillDeleteUnavailable) as caught:
        await refuse_native_skill_delete(case.sessions, guard=case.guard(), skill=case.pin.id)
    assert caught.value.reason == "catalog_changed" and tuple(case.trace.requests) == recorded


async def test_disabled_native_cli_refuses_before_database_or_anthropic_fallback(
    case: Case,
) -> None:
    from sqlalchemy import event

    connections: list[object] = []
    engine = case.sessions.kw["bind"]

    def checkout(*args: object) -> None:
        connections.append(args)

    event.listen(engine.sync_engine, "checkout", checkout)
    recorded = tuple(case.trace.requests)
    try:
        with pytest.raises(NativeSkillDeleteUnavailable) as caught:
            await delete_skill(
                case.runtime(),
                Console(file=StringIO()),
                name="qa-echo-skill",
                yes=True,
                selector=TenantSelector(guild_id=case.binding.workspace_id),
                native_guard=replace(case.guard(), operator_enabled=False),
            )
        assert caught.value.reason == "guard_disabled"
        assert not connections and tuple(case.trace.requests) == recorded
    finally:
        event.remove(engine.sync_engine, "checkout", checkout)


@pytest.mark.parametrize("options", [{}, {"--yes": "false"}, {"--yes": "true", "--agent": "x"}])
async def test_admin_delete_accepts_only_the_authored_literal_shape(
    case: Case, options: dict[str, str]
) -> None:
    await case.publish()
    edge = SkillDeleteAdminBindings(case.binding, "qa-workspace", True)
    recorded = tuple(case.trace.requests)
    receipt = await edge(
        AdminCommand(
            "skills_delete",
            "discord",
            case.binding.workspace_id,
            args=("qa-echo-skill",),
            options=options,
        )
    )
    assert receipt.status == "pending" and receipt.code == "ADMIN_SKILL_DELETE_SHAPE_UNBOUND"
    assert tuple(case.trace.requests) == recorded


async def test_anthropic_binding_only_observes_actual_sdk_and_scoped_references(case: Case) -> None:
    from daimon.testing.ma_models import ma_agent
    from mux.testing.drivers.anthropic.ma import list_response

    router = case.trace.router
    assert router is not None
    title = tenant_scoped_display_title(
        tenant_id=uuid.UUID(case.scope.tenant_id), name="qa-echo-skill", agent_name="qa-fork"
    )
    foreign_title = tenant_scoped_display_title(
        tenant_id=uuid.UUID(case.foreign.tenant_id), name="qa-echo-skill", agent_name="qa-fork"
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda r, m: list_response(
            [
                {
                    "id": "skill-owned",
                    "source": "custom",
                    "display_title": title,
                    "latest_version": "1",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": "skill-foreign",
                    "source": "custom",
                    "display_title": foreign_title,
                    "latest_version": "1",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ]
        ),
    )
    router.add_agent_list(
        ma_agent(
            id="agent-owned",
            tenant_id=case.scope.tenant_id,
            skills=[{"type": "custom", "skill_id": "skill-owned", "version": "1"}],
        ),
        ma_agent(
            id="agent-foreign",
            tenant_id=case.foreign.tenant_id,
            skills=[{"type": "custom", "skill_id": "skill-foreign", "version": "1"}],
        ),
    )
    async with case.sessions() as session, session.begin():
        await add_channel_skill(
            session,
            tenant_id=uuid.UUID(case.scope.tenant_id),
            platform="discord",
            channel_id="223456789012345678",
            skill_id="skill-owned",
            version="1",
            name="qa-echo-skill",
            owner_agent_name="qa-fork",
            actor_account_id=uuid.UUID(case.scope.account_id),
        )
        await add_channel_skill(
            session,
            tenant_id=uuid.UUID(case.foreign.tenant_id),
            platform="discord",
            channel_id="foreign-channel",
            skill_id="skill-owned",
            version="1",
            name="qa-echo-skill",
            owner_agent_name="qa-fork",
            actor_account_id=uuid.UUID(case.foreign.account_id),
        )
    observer = AdminBindings(
        sessions=case.sessions,
        scope=case.scope,
        backend="anthropic",
        platform="discord",
        workspace_id=case.binding.workspace_id,
        channels=case.binding.channels,
        trace=case.trace,
    )
    start = len(case.trace.requests)
    try:
        receipt = await SkillDeleteAdminBindings(observer)(
            AdminCommand(
                "skills_delete",
                "discord",
                case.binding.workspace_id,
                args=("qa-echo-skill",),
                options={"--yes": "true"},
            )
        )
    finally:
        await observer.close()
    assert receipt.status == "pending"
    assert receipt.code == "ADMIN_ANTHROPIC_SKILL_DELETE_OBSERVATION_ONLY"
    assert receipt.observations == {
        "skill_ids": ["skill-owned"],
        "agents": ["agent-owned"],
        "channels": ["223456789012345678"],
        "observation_only": True,
        "deleted": False,
    }
    assert [(r.method, r.path) for r in case.trace.requests[start:]] == [
        ("GET", "/v1/skills"),
        ("GET", "/v1/agents"),
    ]


async def test_cli_boundary_renders_the_actual_product_refusal_as_exit_one(case: Case) -> None:
    from daimon.adapters.cli.errors import run_cli
    from typer import Exit

    await case.publish()
    with pytest.raises(NativeSkillStillAttached) as caught:
        await refuse_native_skill_delete(case.sessions, guard=case.guard(), skill=case.pin.id)
    actual_refusal = caught.value

    async def refused() -> None:
        raise actual_refusal

    output = StringIO()

    def boundary() -> None:
        with pytest.raises(Exit) as exited:
            run_cli(refused(), console=Console(file=output, width=160))
        assert exited.value.exit_code == 1

    await asyncio.to_thread(boundary)
    assert (
        "Skill is still attached to an agent or channel. Nothing was deleted." in output.getvalue()
    )


async def test_foreign_tenant_channel_reference_does_not_enter_owned_observation(
    case: Case,
) -> None:
    await case.publish(pins=())
    async with case.sessions() as session, session.begin():
        await add_channel_skill(
            session,
            tenant_id=uuid.UUID(case.foreign.tenant_id),
            platform="discord",
            channel_id="foreign-channel",
            skill_id=case.pin.id,
            version=case.pin.version or "",
            name="qa-echo-skill",
            owner_agent_name=None,
            actor_account_id=uuid.UUID(case.foreign.account_id),
        )
    recorded = tuple(case.trace.requests)
    with pytest.raises(NativeSkillDeleteUnavailable) as caught:
        await refuse_native_skill_delete(case.sessions, guard=case.guard(), skill=case.pin.id)
    assert caught.value.reason == "unattached_delete_unbound"
    assert tuple(case.trace.requests) == recorded


async def test_ambiguous_bare_name_refuses_instead_of_choosing_another_agents_skill(
    case: Case,
) -> None:
    await case.publish()
    other_bundle = bundle_from_markdown(MARKDOWN + "\nSecond agent's owned bundle.\n")
    other = await case.backend.skills.create(
        case.scope, skill_upload(other_bundle), key="other-skill"
    )
    assert other.latest_version is not None and other.id != case.pin.id
    await case.catalog.put_skill(
        case.scope,
        skill_id=other.id,
        version=other.latest_version.version or "",
        agent_name="second-agent",
        display_title=tenant_scoped_display_title(
            tenant_id=uuid.UUID(case.scope.tenant_id),
            name="qa-echo-skill",
            agent_name="second-agent",
        ),
        bundle=other_bundle,
    )
    recorded = tuple(case.trace.requests)
    with pytest.raises(StoreError, match="No unique skill"):
        await refuse_native_skill_delete(case.sessions, guard=case.guard(), skill="qa-echo-skill")
    assert tuple(case.trace.requests) == recorded
    await case.check(name="qa-fork/qa-echo-skill")
